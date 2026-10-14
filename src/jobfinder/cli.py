"""Command-line entry point."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from jobfinder import __version__
from jobfinder.ai.client import OpenAICompatibleClient
from jobfinder.ai.errors import AIError
from jobfinder.ai.eval import (
    EvalInputError,
    LabelledJob,
    ModelReport,
    evaluate_model,
    format_report,
    load_labels,
    typical_jobs_per_day,
)
from jobfinder.ai.ledger import SpendLedger
from jobfinder.config import AppConfig, ConfigError, SourceName, load_config
from jobfinder.cv.extract import CVError
from jobfinder.cv.importer import ImportOutcome, ImportStatus, import_cv
from jobfinder.cv.profile_store import ProfileStore, ProfileStoreError
from jobfinder.http.client import HttpClient, create_async_client
from jobfinder.http.identity import user_agent
from jobfinder.locking import LockHeldError, exclusive_lock
from jobfinder.logging_setup import configure_logging, get_logger
from jobfinder.models import ProfileRecord, RunStats, utcnow
from jobfinder.notify.messages import failure_message, run_summary_line
from jobfinder.notify.telegram import NotifyError, TelegramNotifier
from jobfinder.pipeline.fetch import SourceFetch, fetch_source
from jobfinder.pipeline.run import RunDeps, execute_run
from jobfinder.reporting import select_for_export, status_report, to_csv, to_json
from jobfinder.selfcheck import Status, has_failures, run_selfcheck
from jobfinder.sources.registry import implemented_sources
from jobfinder.storage.factory import open_repository
from jobfinder.storage.state_store import StateStore

app = typer.Typer(no_args_is_help=True, add_completion=False, help="jobfinder " + __version__)
profile_app = typer.Typer(no_args_is_help=True, help="Manage the candidate profile.")
sources_app = typer.Typer(no_args_is_help=True, help="Inspect job sources.")
storage_app = typer.Typer(no_args_is_help=True, help="Storage maintenance.")
app.add_typer(profile_app, name="profile")
app.add_typer(sources_app, name="sources")
app.add_typer(storage_app, name="storage")

EXIT_CONFIG = 1
EXIT_NOT_IMPLEMENTED = 2


def _load() -> AppConfig:
    """Load configuration, print readable errors and exit 1 on failure."""
    configure_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    configure_logging(config.settings.log_level, secrets=config.secrets.secret_values())
    return config


def _not_implemented(milestone: str) -> NoReturn:
    typer.echo(f"not implemented yet (planned for {milestone})", err=True)
    raise typer.Exit(EXIT_NOT_IMPLEMENTED)


@app.command()
def selfcheck(
    offline: Annotated[
        bool, typer.Option("--offline", help="Skip checks that need the network.")
    ] = False,
) -> None:
    """Validate configuration, storage and provider access."""
    config = _load()
    results = asyncio.run(run_selfcheck(config, offline=offline))
    width = max(len(r.name) for r in results)
    for r in results:
        typer.echo(f"{r.status.value.upper():<4}  {r.name:<{width}}  {r.detail}")
    failed = has_failures(results)
    get_logger("selfcheck").info(
        "selfcheck_finished",
        failed=failed,
        results={r.name: r.status.value for r in results if r.status is not Status.OK},
    )
    if failed:
        raise typer.Exit(1)


@app.command()
def run() -> None:
    """Fetch, dedupe, score and store new opportunities (one unattended pass)."""
    config = _load()
    try:
        with exclusive_lock(config.run_lock_path):
            outcome = asyncio.run(execute_run(config, RunDeps(hostname=socket.gethostname())))
    except LockHeldError:
        get_logger("run").info("run_skipped", reason="lock held")
        typer.echo("skipped: another run is in progress")
        return
    stats = outcome.stats
    typer.echo(f"run {stats.run_id}: {run_summary_line(stats)}")
    if stats.status == "failed":
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Show recent runs, sources, AI budget and opportunity counts."""
    config = _load()
    with StateStore(config.state_db_path) as store:
        repo = open_repository(config, read_only=True)
        try:
            typer.echo(status_report(config, store, repo, utcnow()))
        finally:
            repo.close()


@app.command()
def export(
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="File to write (default: stdout).")
    ] = None,
    fmt: Annotated[str, typer.Option("--format", help="csv or json.")] = "csv",
    include_all: Annotated[
        bool,
        typer.Option("--all", help="Every stored job, not only the workbook_decisions ones."),
    ] = False,
    min_score: Annotated[
        int | None, typer.Option("--min-score", min=0, max=100, help="Minimum relevance score.")
    ] = None,
) -> None:
    """Export active jobs with the workbook_decisions (default: strong), best first."""
    if fmt not in ("csv", "json"):
        typer.echo("--format must be csv or json", err=True)
        raise typer.Exit(2)
    config = _load()
    repo = open_repository(config, read_only=True)
    try:
        chosen = select_for_export(
            repo,
            include_all=include_all,
            min_score=min_score,
            decisions=config.settings.workbook_decisions,
        )
    finally:
        repo.close()
    text = to_csv(chosen) if fmt == "csv" else to_json(chosen)
    if output is None:
        typer.echo(text, nl=False)
    else:
        output.write_text(text, encoding="utf-8")
        typer.echo(f"{len(chosen)} opportunities written to {output}", err=True)


async def _run_eval(
    config: AppConfig, jobs: list[LabelledJob], profile: ProfileRecord, only: list[str]
) -> list[ModelReport]:
    reports: list[ModelReport] = []
    with StateStore(config.state_db_path) as store:
        ledger = SpendLedger(store, config.providers)
        for entry in config.providers.eval_models:
            name = f"{entry.provider.value}/{entry.model}"
            if only and name not in only:
                continue
            key = config.secrets.api_key_for(entry.provider)
            if key is None:
                reports.append(
                    ModelReport(
                        entry.provider.value,
                        entry.model,
                        skipped=f"{entry.provider.value.upper()}_API_KEY not set",
                    )
                )
                continue
            provider = config.providers.providers[entry.provider]
            async with OpenAICompatibleClient(
                name=entry.provider.value,
                provider=provider,
                api_key=key.get_secret_value(),
                user_agent=user_agent(config.secrets.CONTACT_EMAIL),
            ) as client:
                typer.echo(f"evaluating {name} on {len(jobs)} jobs...", err=True)
                reports.append(
                    await evaluate_model(
                        client, provider, entry.model, profile, config.settings, jobs, ledger
                    )
                )
    return reports


@app.command(name="eval")
def eval_models(
    labels: Annotated[
        Path, typer.Option("--labels", help="CSV: title, description, label[, company, location].")
    ],
    max_rows: Annotated[
        int, typer.Option("--max-rows", min=1, help="Use at most this many rows.")
    ] = 50,
    model: Annotated[
        list[str] | None,
        typer.Option("--model", help="Only this provider/model from eval_models (repeatable)."),
    ] = None,
    jobs_per_day: Annotated[
        float | None,
        typer.Option(
            "--jobs-per-day", help="For the monthly projection (default: your history, else 30)."
        ),
    ] = None,
) -> None:
    """Score labelled jobs with every model in eval_models and compare them."""
    config = _load()
    profile = ProfileStore(config.profiles_dir).latest()
    if profile is None:
        typer.echo("no profile yet; run `jobfinder profile import <cv>` first", err=True)
        raise typer.Exit(1)
    try:
        jobs = load_labels(labels, max_rows)
    except EvalInputError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    reports = asyncio.run(_run_eval(config, jobs, profile, model or []))
    if jobs_per_day is None:
        with StateStore(config.state_db_path) as store:
            jobs_per_day = typical_jobs_per_day(store.spend_summary, utcnow()) or 30.0
    typer.echo(format_report(reports, jobs_per_day))
    for report in reports:
        for error in report.errors[:3]:
            typer.echo(f"  {report.provider}/{report.model}: {error}", err=True)


async def _send_telegram(config: AppConfig, text: str) -> None:
    secrets = config.secrets
    if secrets.TELEGRAM_BOT_TOKEN is None or secrets.TELEGRAM_CHAT_ID is None:
        raise NotifyError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set")
    async with create_async_client(secrets.CONTACT_EMAIL) as client:
        await TelegramNotifier(
            client, secrets.TELEGRAM_BOT_TOKEN.get_secret_value(), secrets.TELEGRAM_CHAT_ID
        ).send(text)


@app.command()
def notify(
    failure: Annotated[
        bool, typer.Option("--failure", help="Send a failure alert (used by systemd OnFailure).")
    ] = False,
    test: Annotated[bool, typer.Option("--test", help="Send a test message.")] = False,
) -> None:
    """Send a Telegram message: a run-failure alert or a test."""
    if failure == test:
        typer.echo("choose exactly one of --failure or --test", err=True)
        raise typer.Exit(2)
    config = _load()
    if failure:
        last = None
        with StateStore(config.state_db_path) as store:
            runs = store.recent_runs(1)
        if runs and runs[0].stats_json:
            last = run_summary_line(RunStats.model_validate_json(runs[0].stats_json))
        text = failure_message(socket.gethostname(), last)
    else:
        text = f"jobfinder {__version__} on {socket.gethostname()}: Telegram works."
    try:
        asyncio.run(_send_telegram(config, text))
    except NotifyError as exc:
        get_logger("notify").error("notify_failed", error=str(exc))
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo("sent")


async def _import_profile(config: AppConfig, cv_file: Path) -> ImportOutcome:
    with StateStore(config.state_db_path) as state:
        ledger = SpendLedger(state, config.providers)
        async with OpenAICompatibleClient(
            name=config.ai_provider.value,
            provider=config.provider,
            api_key=config.ai_api_key.get_secret_value(),
            user_agent=user_agent(config.secrets.CONTACT_EMAIL),
        ) as client:
            outcome = await import_cv(
                cv_file,
                store=ProfileStore(config.profiles_dir),
                cv_dir=config.cv_dir,
                overrides=config.profile_overrides,
                client=client,
                model=config.profile_model,
                recorder=ledger,
                now=utcnow(),
            )
        cost = sum(ledger.cost_of(call) for call in outcome.calls)
    log = get_logger("profile")
    log.info(
        "profile_import",
        status=outcome.status.value,
        version=outcome.record.version,
        model=outcome.record.ai_model,
        calls=len(outcome.calls),
        input_tokens=sum(c.usage.input_tokens for c in outcome.calls),
        output_tokens=sum(c.usage.output_tokens for c in outcome.calls),
        cost_usd=round(cost, 6),
        truncated=outcome.truncated,
    )
    return outcome


@profile_app.command("import")
def profile_import(cv_file: Annotated[Path, typer.Argument(help="CV as PDF or DOCX.")]) -> None:
    """Build a profile from a CV (no-op when neither the CV nor the overrides changed)."""
    config = _load()
    try:
        outcome = asyncio.run(_import_profile(config, cv_file))
    except (CVError, ProfileStoreError, AIError, OSError) as exc:
        get_logger("profile").error("profile_import_failed", error=str(exc))
        typer.echo(f"profile import failed: {exc}", err=True)
        raise typer.Exit(1) from None
    record = outcome.record
    if outcome.status is ImportStatus.UNCHANGED:
        typer.echo(f"CV and overrides unchanged since profile v{record.version}; nothing to do.")
    elif outcome.status is ImportStatus.OVERRIDES_REAPPLIED:
        typer.echo(
            f"profile v{record.version}: overrides re-applied (no AI call) -> {outcome.path}"
        )
    else:
        typer.echo(f"profile v{record.version} created with {record.ai_model} -> {outcome.path}")
        if outcome.truncated:
            typer.echo("warning: the CV text was long and has been truncated", err=True)


@profile_app.command("show")
def profile_show(
    version: Annotated[
        int | None, typer.Option("--version", "-v", help="Version to show (default: latest).")
    ] = None,
    full: Annotated[
        bool, typer.Option("--full", help="Print the whole record, including the AI extract.")
    ] = False,
) -> None:
    """Print the current profile as JSON."""
    config = _load()
    store = ProfileStore(config.profiles_dir)
    try:
        record = store.load(version) if version is not None else store.latest()
    except ProfileStoreError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    if record is None:
        typer.echo("no profile yet; run `jobfinder profile import <cv>`", err=True)
        raise typer.Exit(1)
    typer.echo(
        f"# profile v{record.version}, {record.created_at:%Y-%m-%d %H:%M} UTC, "
        f"{record.ai_provider}/{record.ai_model}, cv {record.cv_filename} "
        f"(sha256 {record.cv_sha256[:12]})",
        err=True,
    )
    typer.echo((record if full else record.profile).model_dump_json(indent=2))


async def _test_sources(config: AppConfig, names: list[SourceName]) -> list[SourceFetch]:
    sources = implemented_sources()
    with StateStore(config.state_db_path) as store:
        async with create_async_client(config.secrets.CONTACT_EMAIL) as client:
            # A dry run must not store ETags: a later real run would then get 304s for
            # jobs that were never saved.
            http = HttpClient(client, store, use_validators=False)
            return list(
                await asyncio.gather(
                    *(
                        fetch_source(
                            config, name, sources[name], http, store, utcnow(), record=False
                        )
                        for name in names
                    )
                )
            )


@sources_app.command("test")
def sources_test(
    name: Annotated[
        str | None, typer.Argument(help="Source to test (default: all enabled).")
    ] = None,
) -> None:
    """Fetch from sources and show what was found; nothing is stored.

    Requests count towards the daily caps.
    """
    config = _load()
    implemented = implemented_sources()
    if name is not None:
        try:
            names = [SourceName(name)]
        except ValueError:
            typer.echo(
                f"unknown source {name!r}; known: {', '.join(s.value for s in SourceName)}",
                err=True,
            )
            raise typer.Exit(1) from None
    else:
        names = [n for n in config.sources.enabled() if n in implemented]
    missing = [n for n in names if n not in implemented]
    if missing:
        typer.echo(f"not implemented yet: {', '.join(missing)}", err=True)
        raise typer.Exit(EXIT_NOT_IMPLEMENTED)
    results = asyncio.run(_test_sources(config, names))
    failed = False
    for result in results:
        report = result.report
        status = result.status.value.upper()
        failed = failed or result.status.value not in ("ok", "partial")
        typer.echo(f"{status:<8} {result.name}: {len(result.jobs)} jobs")
        for job in result.jobs[:3]:
            place = ", ".join(job.locations[:2]) or "-"
            typer.echo(f"         - {job.title} | {job.company} | {place}")
        for error in ([result.fatal] if result.fatal else []) + report.errors:
            typer.echo(f"         ! {error}")
        if report.not_modified:
            typer.echo(
                f"         (not modified since last fetch: {len(report.not_modified)} request(s))"
            )
        if report.skipped_records:
            typer.echo(f"         (skipped {report.skipped_records} malformed record(s))")
    if failed:
        raise typer.Exit(1)


@storage_app.command("migrate")
def storage_migrate(
    to: Annotated[str, typer.Option("--to", help="Target backend.")] = "sqlite",
) -> None:
    """Migrate opportunities to another backend."""
    _not_implemented("M9")


def main() -> None:
    app()
