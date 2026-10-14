"""Configuration loading and validation.

Secrets come from the environment (and a local ``.env`` in development); behaviour
comes from the YAML files in the config directory. ``load_config`` validates
everything up front and raises a single ``ConfigError`` listing every problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from jobfinder.models import Decision, EmploymentType, ProfileOverrides

SETTINGS_FILE = "settings.yaml"
SOURCES_FILE = "sources.yaml"
PROVIDERS_FILE = "providers.yaml"
OVERRIDES_FILE = "profile.overrides.yaml"

_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


class ConfigError(Exception):
    """Raised when configuration is missing or invalid. Carries every problem found."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- environment


class AIProvider(StrEnum):
    GROQ = "groq"
    DEEPSEEK = "deepseek"


class StorageBackend(StrEnum):
    EXCEL = "excel"
    SQLITE = "sqlite"


class Secrets(BaseSettings):
    """Values read from the process environment. Never logged."""

    model_config = SettingsConfigDict(
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
        case_sensitive=True,
    )

    AI_PROVIDER: AIProvider
    GROQ_API_KEY: SecretStr | None = None
    DEEPSEEK_API_KEY: SecretStr | None = None
    AI_MODEL_SCORE: str | None = None
    AI_MODEL_PROFILE: str | None = None
    ADZUNA_APP_ID: str | None = None
    ADZUNA_APP_KEY: SecretStr | None = None
    FRANCE_TRAVAIL_CLIENT_ID: str | None = None
    FRANCE_TRAVAIL_CLIENT_SECRET: SecretStr | None = None
    IMAP_HOST: str | None = None
    IMAP_USER: str | None = None
    IMAP_PASSWORD: SecretStr | None = None
    TELEGRAM_BOT_TOKEN: SecretStr | None = None
    TELEGRAM_CHAT_ID: str | None = None
    HEALTHCHECK_URL: SecretStr | None = None
    STORAGE_BACKEND: StorageBackend = StorageBackend.EXCEL
    DATA_DIR: Path = Path("/var/lib/jobfinder")
    CONTACT_EMAIL: Annotated[str, Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    JOBFINDER_CONFIG_DIR: Path = Path("config")

    def api_key_for(self, provider: AIProvider) -> SecretStr | None:
        return {
            AIProvider.GROQ: self.GROQ_API_KEY,
            AIProvider.DEEPSEEK: self.DEEPSEEK_API_KEY,
        }[provider]

    def secret_values(self) -> list[str]:
        """Every secret value currently set, for log redaction."""
        values: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                raw = value.get_secret_value()
                if raw:
                    values.append(raw)
        return values


# --------------------------------------------------------------------------- settings.yaml

CountryCode = Annotated[str, Field(min_length=2, max_length=2)]


class RemotePreference(StrEnum):
    REMOTE_ONLY = "remote_only"
    REMOTE_FIRST = "remote_first"
    ANY = "any"
    ONSITE_ONLY = "onsite_only"


class LinkSettings(_Strict):
    """Apply-link resolution and liveness checks (requests to employer and ATS pages)."""

    daily_request_cap: int = Field(default=200, ge=0)
    requests_per_minute: float = Field(default=6, gt=0, le=60, description="Per host.")
    max_resolutions_per_run: int = Field(default=30, ge=0)
    max_liveness_checks_per_run: int = Field(default=20, ge=0)


class Settings(_Strict):
    target_countries: list[CountryCode] = Field(min_length=1)
    remote_preference: RemotePreference
    employment_types: list[EmploymentType] = Field(min_length=1)
    max_age_days: int = Field(ge=1, le=365)
    prefilter_threshold: int = Field(default=35, ge=0, le=100)
    notify_threshold: int = Field(default=80, ge=0, le=100)
    ai_daily_budget_usd: float = Field(default=1.00, ge=0)
    ai_monthly_budget_usd: float = Field(default=20.0, ge=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    workbook_decisions: list[Decision] = Field(
        default_factory=lambda: [Decision.STRONG],
        min_length=1,
        description="Decisions shown on the Opportunities sheet (and exported by default); "
        "every other job is kept on a hidden 'Other jobs' sheet.",
    )
    store_other_countries: bool = Field(
        default=False,
        description="Store jobs located outside target_countries (as filtered) instead of "
        "dropping them before storage.",
    )
    links: LinkSettings = Field(default_factory=LinkSettings)

    @field_validator("target_countries")
    @classmethod
    def _iso_countries(cls, value: list[str]) -> list[str]:
        normalised = [c.upper() for c in value]
        bad = [c for c in normalised if not _COUNTRY_RE.match(c)]
        if bad:
            raise ValueError(f"not ISO-3166 alpha-2 codes: {', '.join(bad)}")
        return normalised

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.notify_threshold < self.prefilter_threshold:
            raise ValueError(
                f"notify_threshold ({self.notify_threshold}) must be >= "
                f"prefilter_threshold ({self.prefilter_threshold})"
            )
        if self.ai_daily_budget_usd > self.ai_monthly_budget_usd:
            raise ValueError("ai_daily_budget_usd must not exceed ai_monthly_budget_usd")
        return self


# --------------------------------------------------------------------------- sources.yaml


class SourceName(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    REMOTIVE = "remotive"
    REMOTEOK = "remoteok"
    JOBICY = "jobicy"
    HIMALAYAS = "himalayas"
    ARBEITNOW = "arbeitnow"
    ADZUNA = "adzuna"
    FRANCE_TRAVAIL = "france_travail"
    EMAIL_ALERTS = "email_alerts"


ATS_SOURCES = frozenset({SourceName.GREENHOUSE, SourceName.LEVER, SourceName.ASHBY})


@dataclass(frozen=True)
class TermsLimit:
    """Ceilings derived from each source's published terms; config may be stricter, never looser."""

    max_daily_requests: int | None = None
    max_requests_per_minute: float | None = None
    min_interval_minutes: int | None = None
    max_weekly_requests: int | None = None  # rolling 7 days, enforced at runtime
    max_monthly_requests: int | None = None  # rolling 30 days, enforced at runtime


SOURCE_TERMS: dict[SourceName, TermsLimit] = {
    # https://remotive.com/api-documentation: max 4/day advised, >2/min blocked
    SourceName.REMOTIVE: TermsLimit(max_daily_requests=4, max_requests_per_minute=2),
    # https://jobi.cy/apidocs: at most one sync pass per hour
    SourceName.JOBICY: TermsLimit(min_interval_minutes=60),
    # Arbeitnow answers with x-ratelimit-limit: 50 (per minute)
    SourceName.ARBEITNOW: TermsLimit(max_requests_per_minute=50),
    # https://developer.adzuna.com/docs/terms_of_service
    SourceName.ADZUNA: TermsLimit(
        max_daily_requests=250,
        max_requests_per_minute=25,
        max_weekly_requests=1000,
        max_monthly_requests=2500,
    ),
    # France Travail offres d'emploi v2: 10 calls per second
    SourceName.FRANCE_TRAVAIL: TermsLimit(max_requests_per_minute=600),
}


# --------------------------------------------------------------------------- source queries


class RemotiveQuery(_Strict):
    category: str | None = None
    search: str | None = None
    company_name: str | None = None
    limit: int | None = Field(default=None, ge=1)


class JobicyQuery(_Strict):
    geo: str | None = None
    industry: str | None = None
    tag: str | None = None


class ArbeitnowQuery(_Strict):
    visa_sponsorship: bool | None = None


class AdzunaQuery(_Strict):
    country: str = Field(pattern=r"^[a-z]{2}$", description="Adzuna country code, e.g. fr, de.")
    what: str | None = None
    what_exclude: str | None = None
    where: str | None = None
    category: str | None = None
    max_days_old: int | None = Field(default=None, ge=1)
    results_per_page: int = Field(default=50, ge=1, le=50)


class FranceTravailQuery(_Strict):
    motsCles: str | None = None  # noqa: N815 - API parameter names
    codeROME: str | None = None  # noqa: N815
    departement: str | None = None
    commune: str | None = None
    distance: int | None = Field(default=None, ge=0)
    typeContrat: str | None = None  # noqa: N815
    publieeDepuis: Literal[1, 3, 7, 14, 31] | None = 7  # noqa: N815


class PagingOptions(_Strict):
    max_pages: int = Field(default=3, ge=1, le=50)


class FranceTravailOptions(PagingOptions):
    max_pages: int = Field(default=1, ge=1, le=8)
    scope: str = Field(
        default="api_offresdemploiv2 o2dsoffre",
        description="OAuth scope(s) your francetravail.io application is subscribed to.",
    )


# Alert senders with a parser in sources/email_alerts.py (matched by address domain).
SUPPORTED_ALERT_DOMAINS = ("linkedin.com", "indeed.com", "welcometothejungle.com", "glassdoor.com")


def alert_domain(address: str) -> str | None:
    """The supported domain an alert sender belongs to, if any."""
    host = address.rsplit("@", 1)[-1].lower()
    return next((d for d in SUPPORTED_ALERT_DOMAINS if host == d or host.endswith("." + d)), None)


class EmailAlertsOptions(_Strict):
    allowed_senders: list[str] = Field(default_factory=list)
    mailbox: str = "INBOX"
    mark_seen: bool = True
    max_messages: int = Field(default=50, ge=1, le=500)

    @field_validator("allowed_senders")
    @classmethod
    def _addresses(cls, value: list[str]) -> list[str]:
        bad = [v for v in value if "@" not in v]
        if bad:
            raise ValueError(f"not email addresses: {', '.join(bad)}")
        unsupported = [v for v in value if alert_domain(v) is None]
        if unsupported:
            raise ValueError(
                f"no parser for {', '.join(unsupported)}; supported domains: "
                f"{', '.join(SUPPORTED_ALERT_DOMAINS)}"
            )
        return [v.strip().lower() for v in value]


class NoOptions(_Strict):
    pass


SOURCE_QUERY_MODELS: dict[SourceName, type[BaseModel] | None] = {
    SourceName.REMOTIVE: RemotiveQuery,
    SourceName.JOBICY: JobicyQuery,
    SourceName.ARBEITNOW: ArbeitnowQuery,
    SourceName.ADZUNA: AdzunaQuery,
    SourceName.FRANCE_TRAVAIL: FranceTravailQuery,
}
SOURCE_OPTION_MODELS: dict[SourceName, type[BaseModel]] = {
    SourceName.JOBICY: PagingOptions,
    SourceName.HIMALAYAS: PagingOptions,
    SourceName.ARBEITNOW: PagingOptions,
    SourceName.ADZUNA: PagingOptions,
    SourceName.FRANCE_TRAVAIL: FranceTravailOptions,
    SourceName.EMAIL_ALERTS: EmailAlertsOptions,
}


QueryValue = str | int | float | bool


class SourceConfig(_Strict):
    enabled: bool = False
    min_interval_minutes: int = Field(default=60, ge=1)
    daily_request_cap: int = Field(default=100, ge=1)
    requests_per_minute: dict[str, float] = Field(default_factory=dict)
    queries: list[dict[str, QueryValue]] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("requests_per_minute")
    @classmethod
    def _hosts(cls, value: dict[str, float]) -> dict[str, float]:
        problems = [h for h in value if not _HOST_RE.match(h)]
        if problems:
            raise ValueError(f"invalid host names (lowercase, no scheme): {', '.join(problems)}")
        if any(rpm <= 0 for rpm in value.values()):
            raise ValueError("requests_per_minute values must be > 0")
        return value


class AtsCompany(_Strict):
    name: str = Field(min_length=1)
    provider: Literal["greenhouse", "lever", "ashby"]
    board_token: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    region: Literal["global", "eu"] = Field(
        default="global", description="Lever only: 'eu' for boards hosted on api.eu.lever.co."
    )


class SourcesFile(_Strict):
    sources: dict[SourceName, SourceConfig]
    ats_companies: list[AtsCompany] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        problems: list[str] = []
        for name, cfg in self.sources.items():
            problems.extend(_check_queries_and_options(name, cfg))
            terms = SOURCE_TERMS.get(name)
            if terms is None:
                continue
            if terms.max_daily_requests is not None and (
                cfg.daily_request_cap > terms.max_daily_requests
            ):
                problems.append(
                    f"{name}: daily_request_cap {cfg.daily_request_cap} exceeds the source's "
                    f"terms ({terms.max_daily_requests}/day)"
                )
            if terms.max_requests_per_minute is not None:
                for host, rpm in cfg.requests_per_minute.items():
                    if rpm > terms.max_requests_per_minute:
                        problems.append(
                            f"{name}: requests_per_minute for {host} ({rpm}) exceeds the "
                            f"source's terms ({terms.max_requests_per_minute}/min)"
                        )
            if terms.min_interval_minutes is not None and (
                cfg.min_interval_minutes < terms.min_interval_minutes
            ):
                problems.append(
                    f"{name}: min_interval_minutes {cfg.min_interval_minutes} is below the "
                    f"source's terms ({terms.min_interval_minutes})"
                )
        seen: set[tuple[str, str]] = set()
        for company in self.ats_companies:
            key = (company.provider, company.board_token.lower())
            if key in seen:
                problems.append(
                    f"ats_companies: duplicate {company.provider} board_token "
                    f"{company.board_token!r}"
                )
            seen.add(key)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def enabled(self) -> list[SourceName]:
        return [name for name, cfg in self.sources.items() if cfg.enabled]


def _check_queries_and_options(name: SourceName, cfg: SourceConfig) -> list[str]:
    problems: list[str] = []
    query_model = SOURCE_QUERY_MODELS.get(name)
    if cfg.queries and query_model is None:
        problems.append(f"{name}: does not take queries")
    elif query_model is not None:
        for index, query in enumerate(cfg.queries):
            try:
                query_model.model_validate(query)
            except ValidationError as exc:
                for err in exc.errors(include_url=False):
                    loc = ".".join(str(p) for p in err["loc"]) or "(query)"
                    problems.append(f"{name}: queries.{index}.{loc}: {err['msg']}")
    try:
        options = SOURCE_OPTION_MODELS.get(name, NoOptions).model_validate(cfg.options)
    except ValidationError as exc:
        for err in exc.errors(include_url=False):
            loc = ".".join(str(p) for p in err["loc"]) or "(options)"
            problems.append(f"{name}: options.{loc}: {err['msg']}")
    else:
        if isinstance(options, EmailAlertsOptions) and cfg.enabled and not options.allowed_senders:
            problems.append(f"{name}: options.allowed_senders must list at least one sender")
    return problems


# --------------------------------------------------------------------------- providers.yaml


class StructuredOutput(StrEnum):
    STRICT_JSON_SCHEMA = "strict_json_schema"
    JSON_OBJECT = "json_object"


ReasoningEffort = Literal["none", "low", "medium", "high", "max"]
Purpose = Literal["score", "profile"]


class Weekday(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


_WEEKDAYS = list(Weekday)


class Price(_Strict):
    """US dollars per million tokens."""

    input: float = Field(ge=0)
    cached_input: float = Field(ge=0)
    output: float = Field(ge=0)


class ModelInfo(_Strict):
    price: Price = Field(description="Standard rate; the peak rate when off_peak_price is set.")
    off_peak_price: Price | None = None
    extra_body: dict[str, Any] = Field(
        default_factory=dict, description="Extra request fields sent with every call."
    )


class PeakWindow(_Strict):
    """A UTC time-of-day window on the given weekdays; must not wrap midnight."""

    days: list[Weekday] = Field(min_length=1)
    start: time
    end: time

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError("start must be before end (split windows that cross midnight)")
        return self

    def contains(self, at: datetime) -> bool:
        """``at`` must be timezone-aware; it is compared in UTC."""
        utc = at.astimezone(UTC)
        return (
            _WEEKDAYS[utc.weekday()] in self.days
            and self.start <= utc.time().replace(tzinfo=None) < self.end
        )


class DefaultModels(_Strict):
    score: str
    profile: str


def _default_max_output_tokens() -> dict[Purpose, int]:
    return {"score": 2048, "profile": 8192}


class ProviderConfig(_Strict):
    base_url: str = Field(pattern=r"^https://")
    structured_output: StructuredOutput
    supports_reasoning_effort: bool
    reasoning_effort: dict[Purpose, ReasoningEffort] = Field(default_factory=dict)
    max_output_tokens: dict[Purpose, int] = Field(default_factory=_default_max_output_tokens)
    default_models: DefaultModels
    peak_windows_utc: list[PeakWindow] = Field(default_factory=list)
    peak_exempt_dates: list[date] = Field(
        default_factory=list, description="Dates billed off-peak all day (provider holidays)."
    )
    models: dict[str, ModelInfo] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        for role in ("score", "profile"):
            model = getattr(self.default_models, role)
            if model not in self.models:
                raise ValueError(f"default_models.{role} {model!r} has no entry under models")
        if self.reasoning_effort and not self.supports_reasoning_effort:
            raise ValueError("reasoning_effort is set but supports_reasoning_effort is false")
        if any(v < 256 for v in self.max_output_tokens.values()):
            raise ValueError("max_output_tokens values must be >= 256")
        has_off_peak = any(m.off_peak_price is not None for m in self.models.values())
        if has_off_peak and not self.peak_windows_utc:
            raise ValueError("a model has off_peak_price but peak_windows_utc is empty")
        return self

    def is_peak(self, at: datetime) -> bool:
        if at.astimezone(UTC).date() in self.peak_exempt_dates:
            return False
        return any(w.contains(at) for w in self.peak_windows_utc)

    def price_at(self, model: str, at: datetime) -> Price:
        info = self.models[model]
        if info.off_peak_price is not None and not self.is_peak(at):
            return info.off_peak_price
        return info.price


class EvalModel(_Strict):
    provider: AIProvider
    model: str


class ProvidersFile(_Strict):
    providers: dict[AIProvider, ProviderConfig]
    eval_models: list[EvalModel] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        problems = []
        for em in self.eval_models:
            provider = self.providers.get(em.provider)
            if provider is None:
                problems.append(f"eval_models: provider {em.provider} is not defined")
            elif em.model not in provider.models:
                problems.append(f"eval_models: {em.provider}/{em.model} has no price entry")
        if problems:
            raise ValueError("; ".join(problems))
        return self


# --------------------------------------------------------------------------- assembled config


@dataclass(frozen=True)
class AppConfig:
    secrets: Secrets
    settings: Settings
    sources: SourcesFile
    providers: ProvidersFile
    profile_overrides: ProfileOverrides
    config_dir: Path
    data_dir: Path

    @property
    def ai_provider(self) -> AIProvider:
        return self.secrets.AI_PROVIDER

    @property
    def provider(self) -> ProviderConfig:
        return self.providers.providers[self.ai_provider]

    @property
    def ai_api_key(self) -> SecretStr:
        key = self.secrets.api_key_for(self.ai_provider)
        if key is None:  # guaranteed by load_config
            raise RuntimeError("AI key missing after validation")
        return key

    @property
    def score_model(self) -> str:
        return self.secrets.AI_MODEL_SCORE or self.provider.default_models.score

    @property
    def profile_model(self) -> str:
        return self.secrets.AI_MODEL_PROFILE or self.provider.default_models.profile

    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def run_lock_path(self) -> Path:
        return self.data_dir / "run.lock"

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def cv_dir(self) -> Path:
        return self.data_dir / "cv"


def _format_validation_error(origin: str, exc: ValidationError) -> list[str]:
    problems = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in err["loc"]) or "(root)"
        msg = err["msg"].removeprefix("Value error, ")
        if err["type"] == "missing":
            msg = "required but not set"
        elif err["type"] == "extra_forbidden":
            msg = "unknown key (check spelling)"
        problems.append(f"{origin}: {loc}: {msg}")
    return problems


def _read_yaml(path: Path, problems: list[str]) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        problems.append(f"{path}: file not found")
        return None
    except OSError as exc:
        problems.append(f"{path}: cannot read ({exc.strerror})")
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        problems.append(f"{path.name}: invalid YAML: {exc}")
        return None
    if data is None:
        return {}
    if not isinstance(data, dict):
        problems.append(f"{path.name}: top level must be a mapping")
        return None
    return data


def _parse[M: BaseModel](model: type[M], path: Path, problems: list[str]) -> M | None:
    data = _read_yaml(path, problems)
    if data is None:
        return None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems.extend(_format_validation_error(path.name, exc))
        return None


_SOURCE_CREDENTIALS: dict[SourceName, tuple[str, ...]] = {
    SourceName.ADZUNA: ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"),
    SourceName.FRANCE_TRAVAIL: ("FRANCE_TRAVAIL_CLIENT_ID", "FRANCE_TRAVAIL_CLIENT_SECRET"),
    SourceName.EMAIL_ALERTS: ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"),
}


def _cross_check(secrets: Secrets, sources: SourcesFile, providers: ProvidersFile) -> list[str]:
    problems: list[str] = []
    provider_name = secrets.AI_PROVIDER
    provider = providers.providers.get(provider_name)
    if provider is None:
        problems.append(f"providers.yaml: AI_PROVIDER={provider_name} has no providers entry")
    else:
        if secrets.api_key_for(provider_name) is None:
            problems.append(
                f"environment: {provider_name.upper()}_API_KEY: required when "
                f"AI_PROVIDER={provider_name}"
            )
        for var in ("AI_MODEL_SCORE", "AI_MODEL_PROFILE"):
            model = getattr(secrets, var)
            if model is not None and model not in provider.models:
                problems.append(
                    f"environment: {var}={model!r} is not listed under "
                    f"providers.{provider_name}.models in providers.yaml "
                    f"(known: {', '.join(sorted(provider.models))})"
                )
    for source in sources.enabled():
        for var in _SOURCE_CREDENTIALS.get(source, ()):
            if getattr(secrets, var) is None:
                problems.append(f"environment: {var}: required because source {source} is enabled")
    ats_enabled = ATS_SOURCES & set(sources.enabled())
    for company in sources.ats_companies:
        if company.provider not in {s.value for s in sources.sources}:
            problems.append(
                f"sources.yaml: ats_companies: {company.name} uses provider "
                f"{company.provider}, which has no sources entry"
            )
    if ats_enabled and not any(c.provider in ats_enabled for c in sources.ats_companies):
        problems.append(
            "sources.yaml: an ATS source is enabled but ats_companies has no matching company"
        )
    if (secrets.TELEGRAM_BOT_TOKEN is None) != (secrets.TELEGRAM_CHAT_ID is None):
        problems.append("environment: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set together")
    return problems


def load_secrets(env_file: Path | None = None) -> Secrets:
    """Read secrets from the environment, plus ``env_file`` if given and present."""
    return Secrets(_env_file=env_file)


def load_config(env_file: Path | None = Path(".env")) -> AppConfig:
    """Load and validate all configuration, raising ``ConfigError`` with every problem."""
    problems: list[str] = []
    secrets: Secrets | None
    try:
        secrets = load_secrets(env_file)
    except ValidationError as exc:
        problems.extend(_format_validation_error("environment", exc))
        secrets = None

    config_dir = (secrets.JOBFINDER_CONFIG_DIR if secrets else Path("config")).resolve()
    if not config_dir.is_dir():
        problems.append(f"JOBFINDER_CONFIG_DIR: {config_dir} is not a directory")
        raise ConfigError(problems)

    settings = _parse(Settings, config_dir / SETTINGS_FILE, problems)
    sources = _parse(SourcesFile, config_dir / SOURCES_FILE, problems)
    providers = _parse(ProvidersFile, config_dir / PROVIDERS_FILE, problems)
    overrides = _parse(ProfileOverrides, config_dir / OVERRIDES_FILE, problems)

    if secrets and sources and providers:
        problems.extend(_cross_check(secrets, sources, providers))

    if problems or not (secrets and settings and sources and providers and overrides):
        raise ConfigError(problems)

    return AppConfig(
        secrets=secrets,
        settings=settings,
        sources=sources,
        providers=providers,
        profile_overrides=overrides,
        config_dir=config_dir,
        data_dir=secrets.DATA_DIR.resolve(),
    )
