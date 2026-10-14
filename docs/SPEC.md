# Original specification

This is the owner's original brief for jobfinder, preserved word for word. Where the
implementation deliberately differs from it, the reason is recorded in
[HANDOFF.md](HANDOFF.md#deviations-from-this-spec-and-why).

---

You are building "jobfinder": a personal, AI-assisted job and internship discovery service that runs unattended on an Ubuntu 24.04 VPS. The architecture below is approved. Do not change technology choices without asking me first.

WORKING RULES
- Build in the milestone order at the end. After each milestone: run ruff, mypy (strict on src/) and pytest, show me the results and a short summary of what changed, then STOP and wait for my go-ahead.
- Ask before adding any dependency not listed here.
- Never commit secrets. Every setting is documented in .env.example and README.
- Prefer small, typed, testable functions. No global mutable state. All times are timezone-aware UTC.
- Respect source terms exactly. Never scrape LinkedIn, Indeed, Glassdoor or any site without a documented API or feed. Never bypass CAPTCHAs, logins or bot protection. Never submit applications.

STACK
Python 3.12, uv (lockfile committed), httpx (async), tenacity, pydantic v2 + pydantic-settings, typer, structlog (JSON), openpyxl, SQLAlchemy Core, rapidfuzz, pdfplumber, python-docx, feedparser, openai (the SDK is used as the client for every OpenAI-compatible provider; no other AI SDK). Dev: pytest, pytest-asyncio, respx, freezegun, ruff, mypy.

LAYOUT
src/jobfinder/{cli.py, config.py, logging_setup.py, models.py, cv/, ai/, sources/, http/, pipeline/, storage/, notify/}; config/{settings.yaml, sources.yaml, providers.yaml, profile.overrides.yaml}; tests/{fixtures, unit, integration}; deploy/{jobfinder.service, jobfinder.timer, jobfinder-backup.service, jobfinder-backup.timer, jobfinder-alert.service, install.sh}. Data dir (default /var/lib/jobfinder, overridable) holds opportunities.xlsx, state.db, profiles/, cv/.

CLI (typer)
jobfinder run | profile import <file> | profile show | sources test [name] | status | export | eval --labels <csv> | storage migrate --to sqlite | selfcheck | notify --failure

CONFIG
- Secrets from environment: AI_PROVIDER (groq|deepseek; groq during development, deepseek in production), GROQ_API_KEY, DEEPSEEK_API_KEY, AI_MODEL_SCORE and AI_MODEL_PROFILE (optional; defaults per provider: groq -> openai/gpt-oss-120b, deepseek -> deepseek-v4-pro), ADZUNA_APP_ID, ADZUNA_APP_KEY, FRANCE_TRAVAIL_CLIENT_ID, FRANCE_TRAVAIL_CLIENT_SECRET, IMAP_HOST, IMAP_USER, IMAP_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HEALTHCHECK_URL, STORAGE_BACKEND (excel|sqlite), DATA_DIR, CONTACT_EMAIL.
- settings.yaml: target countries, remote preference, employment types, max_age_days, prefilter_threshold (35), notify_threshold (80), ai_daily_budget_usd (1.00), ai_monthly_budget_usd (20).
- sources.yaml: per source enabled, min_interval_minutes, daily_request_cap, per-host requests_per_minute, queries, and the ATS company list (provider + board token).
- Validate everything at startup; fail fast with readable errors.

MODELS (pydantic)
RawJob (as fetched), Opportunity, Sighting, Profile, ScoreResult, RunStats. Opportunity fields: opportunity_id (ULID), fingerprint, canonical_url, source, source_job_id, title, company, location_city, location_country (ISO-3166 alpha-2), remote_type, employment_type, seniority, required_skills, summary, posting_url, apply_url, apply_url_final, published_at, deadline_at, discovered_at, last_seen_at, listing_status (active|expired|inaccessible), relevance_score, prefilter_score, decision (strong|possible|reject|ai_error|pending_ai|filtered), matched_skills, missing_skills, score_reason, red_flags, profile_version, ai_model, is_possible_duplicate, duplicate_of, application_status, notes. application_status and notes are USER-OWNED: never overwrite them.

STORAGE
- OpportunityRepository protocol: get_by_source_key, get_by_canonical_url, get_by_fingerprint, fuzzy_candidates(company_norm), upsert, add_sighting, mark_status, iter_active, user_fields.
- ExcelRepository: sheets Opportunities, Sightings, Runs, Profile. Use an fcntl file lock, write to a temp file and os.replace for atomicity, reload user-owned columns before every write and merge them, freeze header row, auto-filter, keep 7 rotated copies.
- SqliteRepository and a PostgreSQL-ready design via shared SQLAlchemy Core tables with identical column names.
- state_store (SQLite, separate file): source last_success, daily counters, ETag/Last-Modified, cursors, token-bucket state, AI result cache keyed by sha256(description)+profile_version, AI spend ledger, run history.

DEDUPE (pipeline/dedupe.py), in order
1 exact source+source_job_id -> update last_seen_at. 2 canonical URL (lowercase host, drop fragment/trailing slash, remove utm_*, gh_src, lever-source, ref, fbclid, gclid, sort query) -> add sighting. 3 fingerprint sha1(norm(company)|norm(title)|norm(city or "remote")), norm strips accents, punctuation, legal suffixes (inc, ltd, llc, gmbh, sarl, sas, sa) and gender markers like (m/w/d), (h/f) -> merge; an ATS record becomes canonical. 4 fuzzy: same company_norm, rapidfuzz token_set_ratio >= 92, published within 30 days -> insert flagged is_possible_duplicate with duplicate_of. Cover each rule with unit tests including false-positive cases.

SOURCES (one class each, implementing Source protocol with fetch(policy, state) -> list[RawJob])
- greenhouse: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
- lever: Lever postings API (mode=json) per company; ashby: Ashby public job posting API per company.
- remotive: GET https://remotive.com/api/remote-jobs with search/category; max 4 calls/day, never more than 2/min; keep posting_url as the Remotive URL.
- remoteok: GET https://remoteok.com/api (skip the legal-notice first element); keep the Remote OK URL.
- jobicy: v2 remote-jobs API; at most hourly; keep the Jobicy URL.
- himalayas: jobs API with cursor paging; personal use only; keep the Himalayas URL.
- arbeitnow: job-board API, stop paging at first fully-seen page.
- adzuna: https://api.adzuna.com/v1/api/jobs/{country}/search/{page}; enforce 25/min and the configured daily cap (60) so totals stay under 250/day, 1000/week, 2500/month; store redirect_url as posting_url and do NOT resolve it.
- france_travail: OAuth2 client credentials, cache token until expiry, offresdemploi v2 search endpoint.
- email_alerts: IMAP, unseen messages from an allow-list of senders, per-sender parsers extracting title, company, location and link; never fetch LinkedIn or Indeed URLs.
- Record a fixture for every source in tests/fixtures and test the parser against it. Mark each parsed RawJob with source and source_job_id.

HTTP LAYER
One shared httpx.AsyncClient; User-Agent "jobfinder/<version> (+mailto:CONTACT_EMAIL)"; timeouts connect 10 s, read 30 s; max 1 concurrent request per host, 4 overall; persistent per-host token bucket; conditional requests with stored ETag/Last-Modified; tenacity retries (3 attempts, exponential back-off with jitter) on timeouts, 5xx and 429 honouring Retry-After; no retry on other 4xx; 401/403 pause the source and alert; circuit breaker pauses a source for 24 h after 3 failed runs. robots.txt (urllib.robotparser, cached 24 h) is checked for every request that is not a documented API endpoint.

CV AND PROFILE
Extract text (pdfplumber / python-docx); fail clearly if empty. Redact emails, phone numbers and street addresses before any AI call. One AI call returns a schema-valid Profile: skills (name, category, evidence), tools, programming languages, spoken languages, education, certifications, years_experience, seniority, target_titles, alternative_titles, synonym_groups, include_keywords, exclude_keywords. Merge profile.overrides.yaml on top (overrides win). Save profiles/profile_v{N}.json with the CV sha256; unchanged CV = no-op.

PREFILTER (no AI)
Hard filters: employment type, country/remote eligibility, exclude keywords, over-senior titles or years, max_age_days. Keyword score 0-100 from weighted overlap of profile skills/titles with synonym groups, title matches weighted 3x. Below prefilter_threshold -> store with decision=filtered, no AI call.

AI SCORING
- One OpenAICompatibleClient (openai SDK with base_url) behind an LLMClient protocol. Providers are defined in config/providers.yaml: groq (base_url https://api.groq.com/openai/v1) and deepseek (base_url https://api.deepseek.com). Moving from development to production means changing AI_PROVIDER and the key only; no code change.
- providers.yaml holds per-provider capability flags and prices: structured_output (strict_json_schema | json_object), supports_reasoning_effort, default models, price per million tokens (input, cached input, output), and for DeepSeek the peak and off-peak UTC windows with their rates.
- Groq (development): default model openai/gpt-oss-120b. Use response_format json_schema with strict: true, so the schema must mark every field required, set additionalProperties: false on every object, and express optional values as a union with null. Use reasoning_effort "low" for scoring and "medium" for profile extraction. Read the x-ratelimit-* response headers and slow down before hitting the limit; on 429 honour Retry-After. Make qwen/qwen3.8-27b selectable as an alternative.
- DeepSeek (production): default model deepseek-v4-pro, with deepseek-flash selectable by config. Use response_format json_object, put the JSON schema plus one valid example in the system prompt, and ignore any reasoning_content in the response. Keep the system prompt prefix (rubric + profile) byte-identical across calls so DeepSeek's automatic prefix caching applies. Compute cost with peak or off-peak rates according to request time.
- Always validate with pydantic regardless of provider; one retry including the validation error, then decision=ai_error.
- Model IDs, parameters and prices change: at build time verify them against each provider's documentation, and make selfcheck call GET /models and fail if the configured model is not listed.
- Prompt files in ai/prompts/ (profile.md, score.md). Rubric: skills 40, role/title fit 20, seniority 15, location/eligibility 15, employment type 10. Instruct the model to separate genuine requirements from keyword stuffing and to list misleading matches in red_flags.
- Output fields: relevance_score, decision, employment_type, seniority, remote_type, matched_skills, missing_skills, required_skills, deadline, red_flags, summary (<=300 chars), score_reason (<=2 sentences).
- Job text trimmed to about 1,200 tokens (strip HTML, boilerplate, benefits sections).
- Budget: record tokens and dollar cost per call from response usage and a price table in config; enforce daily and monthly caps; over budget -> decision=pending_ai, retried next day.
- eval command: score a labelled CSV with every model listed under eval_models in providers.yaml (default: groq openai/gpt-oss-120b, groq qwen/qwen3.8-27b, deepseek deepseek-v4-pro, deepseek deepseek-flash) and print agreement with my labels, average latency, JSON failure rate and projected monthly cost.

LINKS AND LIVENESS
Safe redirect resolution only for employer/ATS hosts: max 5 hops, HEAD then streamed GET without reading the body, robots-checked, cached permanently. Expiry: jobs absent from two consecutive complete fetches of a full-feed source become expired; for other sources check at most weekly and only when relevance_score >= 50 (404/410 -> expired; 3 other failures -> inaccessible).

RUN ORCHESTRATION
jobfinder run: acquire an fcntl lock (exit 0 with "skipped" if held); choose due sources; fetch concurrently within limits; normalise; dedupe; prefilter; score; resolve links; liveness; write storage; write a Runs row and RunStats log line (duration, peak RSS, requests and errors per source, new/updated/expired counts, tokens, cost); send Telegram alerts for new jobs >= notify_threshold; ping HEALTHCHECK_URL on success; non-zero exit on failure so systemd OnFailure fires. A failure in one source must never abort the others.

TESTS (minimum)
Unit: URL canonicalisation, normalisation, every dedupe rule, prefilter, token bucket, daily caps, budget ledger, redaction, Excel user-column preservation, atomic write. Integration (respx + fixtures + freezegun): two consecutive runs add no duplicates; cross-source merge keeps the ATS apply link; 24 simulated hourly ticks never exceed any cap; 429 with Retry-After; source failing 3 runs is paused; malformed AI JSON -> retry -> ai_error; switching AI_PROVIDER from groq to deepseek passes the same scoring tests using recorded responses from each provider (strict json_schema vs json_object paths); Groq 429 with Retry-After is honoured; budget exhaustion -> pending_ai; process killed mid-write leaves the previous workbook valid.

DEPLOYMENT
systemd oneshot service + hourly timer (RandomizedDelaySec=300, Persistent=true), flock on /var/lib/jobfinder/run.lock, RuntimeMaxSec=15min, MemoryMax=512M, sandboxing (NoNewPrivileges, ProtectSystem=strict, ProtectHome, PrivateTmp, ReadWritePaths for data and logs), EnvironmentFile=/etc/jobfinder/jobfinder.env (mode 600). Daily backup timer: sqlite3 .backup of state.db, copy workbook and profiles, dated tar.gz, keep 14. Idempotent install.sh. README: install, configuration reference, adding a source, troubleshooting (logs, paused sources, budget), backup/restore, update/rollback.

MILESTONES
M1 skeleton, config, logging, state store, selfcheck. M2 CV -> profile. M3 Greenhouse, Lever, Ashby, Remotive + normalise + both repositories + dedupe. M4 prefilter + AI scoring + cache + budget + eval. M5 remaining sources + full rate control. M6 links + liveness. M7 run orchestration, status/export, Telegram. M8 deployment files, backups, README. M9 (only when I ask) storage migrate + small FastAPI/HTMX review page.
