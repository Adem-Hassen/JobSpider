"""``jobfinder run``: one complete, unattended pass.

Order: choose due sources -> fetch concurrently -> normalise and dedupe -> expire
missing full-feed jobs -> prefilter -> score -> resolve apply links -> liveness ->
write storage (with a Runs row) -> Telegram alerts -> healthcheck ping.

A failing source never stops the others; a failure in a later stage is recorded and the
run continues, so whatever was fetched is still stored. Status: ``success`` (no
problems), ``partial`` (something needs attention; alerts were sent) or ``failed``
(storage could not be written or an unexpected error occurred; the CLI exits non-zero
so systemd OnFailure fires).
"""

from __future__ import annotations

import asyncio
import resource
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from jobfinder.ai.budget import BudgetGuard
from jobfinder.ai.client import LLMClient, OpenAICompatibleClient
from jobfinder.ai.ledger import SpendLedger
from jobfinder.ai.scoring import Scorer
from jobfinder.config import AppConfig, SourceName
from jobfinder.cv.profile_store import ProfileStore
from jobfinder.http.client import HttpClient, create_async_client
from jobfinder.http.identity import user_agent
from jobfinder.http.links import LinkResolver, links_policy
from jobfinder.logging_setup import get_logger
from jobfinder.models import Decision, Opportunity, ProfileRecord, RunStats, SourceRunStats, utcnow
from jobfinder.notify import messages
from jobfinder.notify.healthcheck import ping
from jobfinder.notify.telegram import NotifyError, TelegramNotifier
from jobfinder.pipeline.fetch import FetchStatus, SourceFetch, fetch_source, record_result
from jobfinder.pipeline.ingest import ingest
from jobfinder.pipeline.liveness import check_liveness, expire_missing, resolve_apply_links
from jobfinder.pipeline.normalise import NormalisedJob
from jobfinder.pipeline.prefilter import Prefilter, location_reason
from jobfinder.pipeline.schedule import due_sources
from jobfinder.pipeline.score import prefilter_new, score_pending
from jobfinder.sources.base import Source
from jobfinder.sources.registry import implemented_sources
from jobfinder.storage.factory import open_repository
from jobfinder.storage.repository import OpportunityRepository
from jobfinder.storage.state_store import StateStore
from jobfinder.ulid import new_ulid

FETCH_DEADLINE = timedelta(minutes=8)  # leaves time to store results within RuntimeMaxSec
MAX_JOB_ALERTS_PER_RUN = 10
NOTIFY_MAX_AGE = timedelta(days=7)

log = get_logger("run")


@dataclass
class RunDeps:
    """Everything a run talks to; tests replace parts of it."""

    clock: Callable[[], datetime] = utcnow
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    sources: Mapping[SourceName, Source] | None = None
    ai_client: LLMClient | None = None
    hostname: str = "localhost"


@dataclass
class RunOutcome:
    stats: RunStats
    alerts: list[str] = field(default_factory=list)


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KiB on Linux


async def _fetch_all(
    config: AppConfig,
    names: list[SourceName],
    sources: Mapping[SourceName, Source],
    http: HttpClient,
    store: StateStore,
    repo: OpportunityRepository,
    now: datetime,
) -> list[SourceFetch]:
    def known(source: str, job_id: str) -> bool:
        return repo.get_by_source_key(source, job_id) is not None

    tasks = {
        name: asyncio.create_task(
            fetch_source(config, name, sources[name], http, store, now, is_known=known)
        )
        for name in names
    }
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks.values(), timeout=FETCH_DEADLINE.total_seconds())
    results = []
    for name, task in tasks.items():
        if task in done:
            results.append(task.result())
            continue
        task.cancel()
        timed_out = SourceFetch(
            name=name,
            status=FetchStatus.FAILED,
            fatal=f"fetch did not finish within {FETCH_DEADLINE.total_seconds() / 60:g} min",
        )
        record_result(store, name, timed_out, now)
        results.append(timed_out)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return results


def _ai_client(config: AppConfig) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        name=config.ai_provider.value,
        provider=config.provider,
        api_key=config.ai_api_key.get_secret_value(),
        user_agent=user_agent(config.secrets.CONTACT_EMAIL),
    )


def jobs_to_notify(
    repo: OpportunityRepository, store: StateStore, threshold: int, now: datetime
) -> list[Opportunity]:
    candidates = [
        o
        for o in repo.iter_active()
        if o.decision in (Decision.STRONG, Decision.POSSIBLE)
        and (o.relevance_score or 0) >= threshold
        and not o.is_possible_duplicate
        and now - o.discovered_at <= NOTIFY_MAX_AGE
        and not store.was_notified(o.opportunity_id)
    ]
    return sorted(candidates, key=lambda o: -(o.relevance_score or 0))


async def _notify(
    notifier: TelegramNotifier,
    repo: OpportunityRepository,
    store: StateStore,
    config: AppConfig,
    alerts: list[str],
    now: datetime,
) -> int:
    if alerts:
        await notifier.send(messages.alerts_message(alerts))
    jobs = jobs_to_notify(repo, store, config.settings.notify_threshold, now)
    sent = 0
    for opportunity in jobs[:MAX_JOB_ALERTS_PER_RUN]:
        await notifier.send(messages.job_message(opportunity))
        store.mark_notified(opportunity.opportunity_id, now)
        sent += 1
    if len(jobs) > MAX_JOB_ALERTS_PER_RUN:
        await notifier.send(messages.more_jobs_message(len(jobs) - MAX_JOB_ALERTS_PER_RUN))
    return sent


class _Run:
    def __init__(self, config: AppConfig, deps: RunDeps, store: StateStore) -> None:
        self.config = config
        self.deps = deps
        self.store = store
        self.started = deps.clock()
        self.stats = RunStats(run_id=new_ulid(self.started), started_at=self.started)
        self.alerts: list[str] = []

    def problem(self, stage: str, error: BaseException | str) -> None:
        text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        self.stats.errors.append(f"{stage}: {text}")
        log.error("run_stage_failed", stage=stage, error=text)

    async def stage_fetch(
        self, http: HttpClient, repo: OpportunityRepository, sources: Mapping[SourceName, Source]
    ) -> list[tuple[NormalisedJob, Opportunity]]:
        names = due_sources(self.config, self.store, sources, self.started)
        http.fresh_sources = {n.value for n in names if not repo.has_source(n.value)}
        fetched = await _fetch_all(
            self.config, names, sources, http, self.store, repo, self.started
        )
        new_jobs: list[tuple[NormalisedJob, Opportunity]] = []
        for result in fetched:
            name = result.name.value
            self.stats.sources[name] = SourceRunStats(
                status=result.status.value,
                requests=http.requests_by_source.get(name, 0),
                errors=http.errors_by_source.get(name, 0) + len(result.report.errors),
                jobs_fetched=len(result.jobs),
            )
            self.alerts.extend(result.alerts)
            if result.status in (FetchStatus.FAILED, FetchStatus.AUTH_FAILED):
                self.problem(f"source {name}", result.fatal or "failed")
            ingested = ingest(
                result.jobs,
                repo,
                preferred_countries=self.config.settings.target_countries,
                now=self.started,
                scope=self._scope(),
            )
            self.stats.out_of_scope += ingested.out_of_scope
            for error in ingested.errors[:5]:
                self.problem(f"ingest {name}", error)
            created = [(job, outcome.opportunity) for job, outcome in ingested.new]
            self.stats.new += len(created)
            self.stats.updated += len(ingested.outcomes) - len(created)
            new_jobs.extend(created)
            self.stats.expired += len(expire_missing(repo, self.store, result, self.started))
        return new_jobs

    def _scope(self) -> Callable[[NormalisedJob], str | None] | None:
        settings = self.config.settings
        if settings.store_other_countries:
            return None
        return lambda job: location_reason(job, settings)

    async def stage_score(
        self,
        repo: OpportunityRepository,
        profile: ProfileRecord,
        new_jobs: list[tuple[NormalisedJob, Opportunity]],
    ) -> None:
        prefiltered = prefilter_new(
            new_jobs,
            repo,
            self.store,
            Prefilter(profile.profile, self.config.settings),
            self.started,
        )
        self.stats.filtered = prefiltered.filtered
        client = self.deps.ai_client or _ai_client(self.config)
        try:
            scorer = Scorer(
                client=client,
                provider=self.config.provider,
                model=self.config.score_model,
                profile=profile,
                settings=self.config.settings,
                store=self.store,
                ledger=SpendLedger(self.store, self.config.providers),
                budget=BudgetGuard(
                    self.store,
                    self.config.settings.ai_daily_budget_usd,
                    self.config.settings.ai_monthly_budget_usd,
                ),
                clock=self.deps.clock,
            )
            scoring = await score_pending(repo, self.store, scorer)
        finally:
            if self.deps.ai_client is None and isinstance(client, OpenAICompatibleClient):
                await client.close()
        self.stats.scored = scoring.scored + scoring.cached
        self.stats.ai_errors = scoring.ai_errors
        self.stats.ai_input_tokens = scoring.input_tokens
        self.stats.ai_output_tokens = scoring.output_tokens
        self.stats.ai_cost_usd = scoring.cost_usd
        if scoring.provider_errors:
            self.problem("scoring", scoring.errors[0])
            self.alerts.append(f"AI scoring stopped: {scoring.errors[0]}")
        if scoring.over_budget:
            log.warning("ai_budget_exhausted", pending=scoring.over_budget)

    async def stage_links(self, http: HttpClient, repo: OpportunityRepository) -> None:
        settings = self.config.settings.links
        policy = links_policy(settings)
        resolver = LinkResolver(http, self.store, policy, self.deps.clock)
        links = await resolve_apply_links(repo, resolver, settings.max_resolutions_per_run)
        self.stats.links_resolved = links.resolved
        liveness = await check_liveness(
            repo, self.store, http, policy, self.started, settings.max_liveness_checks_per_run
        )
        self.stats.expired += len(liveness.expired)
        self.stats.inaccessible = len(liveness.inaccessible)

    async def execute(self) -> RunOutcome:
        self.store.start_run(self.stats.run_id, self.started)
        sources = self.deps.sources or implemented_sources()
        failed = False
        repo: OpportunityRepository | None = None
        try:
            repo = open_repository(self.config)
            profile = ProfileStore(self.config.profiles_dir).latest()
            if profile is not None:
                repo.write_profile(profile)
            async with create_async_client(self.config.secrets.CONTACT_EMAIL) as client:
                http = HttpClient(client, self.store, clock=self.deps.clock, sleep=self.deps.sleep)
                new_jobs = await self.stage_fetch(http, repo, sources)
                if profile is None:
                    self.problem("scoring", "no profile yet: run `jobfinder profile import <cv>`")
                else:
                    try:
                        await self.stage_score(repo, profile, new_jobs)
                    except Exception as exc:  # keep fetched jobs even if scoring breaks
                        self.problem("scoring", exc)
                try:
                    await self.stage_links(http, repo)
                except Exception as exc:
                    self.problem("links", exc)
                self.stats.pending_ai = sum(
                    1 for o in repo.iter_active() if o.decision is Decision.PENDING_AI
                )
                await self._send_notifications(client, repo)
        except Exception as exc:
            failed = True
            self.problem("run", exc)
        finally:
            self._finish(failed)
            if repo is not None:
                try:
                    repo.append_run(self.stats)
                    repo.close()  # writes the workbook
                except Exception as exc:
                    self.stats.status = "failed"
                    self.problem("storage", exc)
        self.store.finish_run(
            self.stats.run_id,
            self.stats.finished_at or self.deps.clock(),
            self.stats.status,
            self.stats.model_dump_json(),
        )
        log.info("run_stats", **self.stats.model_dump(mode="json"))
        if self.stats.status != "failed" and self.config.secrets.HEALTHCHECK_URL is not None:
            async with create_async_client(self.config.secrets.CONTACT_EMAIL) as client:
                await ping(client, self.config.secrets.HEALTHCHECK_URL.get_secret_value())
        return RunOutcome(self.stats, self.alerts)

    async def _send_notifications(
        self, client: httpx.AsyncClient, repo: OpportunityRepository
    ) -> None:
        secrets = self.config.secrets
        if secrets.TELEGRAM_BOT_TOKEN is None or secrets.TELEGRAM_CHAT_ID is None:
            return
        notifier = TelegramNotifier(
            client,
            secrets.TELEGRAM_BOT_TOKEN.get_secret_value(),
            secrets.TELEGRAM_CHAT_ID,
            sleep=self.deps.sleep,
        )
        try:
            self.stats.notified = await _notify(
                notifier, repo, self.store, self.config, self.alerts, self.started
            )
        except NotifyError as exc:
            self.problem("telegram", exc)

    def _finish(self, failed: bool) -> None:
        finished = self.deps.clock()
        self.stats.finished_at = finished
        self.stats.duration_s = (finished - self.started).total_seconds()
        self.stats.peak_rss_mb = peak_rss_mb()
        if failed:
            self.stats.status = "failed"
        elif self.stats.errors:
            self.stats.status = "partial"
        else:
            self.stats.status = "success"


async def execute_run(config: AppConfig, deps: RunDeps | None = None) -> RunOutcome:
    with StateStore(config.state_db_path) as store:
        return await _Run(config, deps or RunDeps(), store).execute()
