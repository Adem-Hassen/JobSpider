# jobfinder: handoff for the next coding agent

This file lets a new agent, on a new machine, continue the work without the original
conversation. Read it fully before changing anything. The owner's original brief is in
[SPEC.md](SPEC.md); end-user documentation is in [../README.md](../README.md) and
[local-testing.md](local-testing.md).

Last updated: 2026-10-14. At that point ruff, mypy (strict) and pytest were green:
646 passed, 4 skipped.

---

## 1. What this is, in short

`jobfinder` is a personal job-finding service for one owner. A run, normally hourly by
systemd timer on a VPS:

1. **Fetch:** pulls postings from employer ATS boards (Greenhouse, Lever, Ashby), remote
   job APIs (Remotive, Remote OK, Jobicy, Himalayas, Arbeitnow), Adzuna, France Travail
   and IMAP job-alert e-mails.
2. **Normalise and dedupe** the postings.
3. **Prefilter** them cheaply, with no AI.
4. **Score** the survivors against a CV-derived profile, through an OpenAI-compatible
   LLM: Groq in development, DeepSeek in production.
5. **Store** everything in an Excel workbook (or SQLite).
6. **Notify:** sends strong matches to Telegram.

All the milestones the owner has requested so far, M1 to M8, are built. The owner then
ran it on real data, and several fixes followed (section 4). **M9 (`storage migrate` plus
a FastAPI/HTMX review page) must only be built when the owner asks.**

## 2. How to work with the owner (important)

- **Milestone discipline.** After each unit of work, run
  `uv run ruff format --check src tests && uv run ruff check src tests && uv run mypy && uv run pytest -q`,
  show the results and a short summary, then **stop and wait for the go-ahead**. For small
  fix requests, finish the fix, show the checks, and summarise.
- **Never `git commit`.** The owner commits. The repository had no commits at handoff,
  and everything was untracked.
- **Dependencies.** Ask before adding any dependency not in the spec's stack. Already
  approved extras: `PyYAML`, plus dev stubs `types-pyyaml`. Not approved, so avoided:
  `python-ulid` (ULID is written in-house), `email-validator` (a regex is used instead),
  `types-openpyxl` (mypy ignores openpyxl imports via an override instead). `shellcheck`
  was run ad hoc through `uvx --from shellcheck-py`, without adding it.
- **No technology changes without asking.** When the spec is contradictory or wrong, do
  the technically correct thing, explain why clearly, and record it in section 5.
- **Respect source terms exactly.**
  - Never scrape LinkedIn, Indeed or Glassdoor, or any site without an API or feed.
  - Never bypass bot protection.
  - Never apply to jobs.
  - Aggregators require attribution and a link back: keep the source's URL in
    `posting_url`.
- **Communication.** The owner writes informal, fast English with typos. Reply plainly
  and concisely, give exact commands, and use they/them if a pronoun is needed. Verify
  against real data or live APIs when it matters. In this project that repeatedly
  exposed bugs the unit tests had missed.
- **Spending the owner's money.** Small live checks with their Groq key (a few tenths of
  a cent) were acceptable when they verified a fix. Don't loop live calls against
  Remotive, which allows 4 a day.

## 3. Status by milestone

| Milestone | State | Main code |
|---|---|---|
| M1 skeleton, config, logging, state store, selfcheck | done | `config.py`, `logging_setup.py`, `storage/state_store.py`, `selfcheck.py`, `cli.py` |
| M2 CV → profile | done | `cv/extract.py`, `cv/redact.py`, `cv/importer.py`, `cv/profile_store.py`, `ai/client.py`, `ai/structured.py`, `ai/schema.py`, `ai/prompts/profile.md` |
| M3 Greenhouse/Lever/Ashby/Remotive, normalise, both repositories, dedupe | done | `sources/*.py`, `pipeline/normalise.py`, `pipeline/countries.py`, `pipeline/dedupe.py`, `storage/excel_repo.py`, `storage/sql_repo.py`, `storage/tables.py` |
| M4 prefilter, AI scoring, cache, budget, eval | done | `pipeline/prefilter.py`, `ai/scoring.py`, `ai/jobtext.py`, `ai/budget.py`, `ai/ledger.py`, `ai/eval.py`, `pipeline/score.py`, `ai/prompts/score.md` |
| M5 remaining sources, full rate control | done | `sources/{remoteok,jobicy,himalayas,arbeitnow,adzuna,france_travail,email_alerts}.py`, `http/client.py`, `http/ratelimit.py`, `http/robots.py`, `pipeline/fetch.py`, `pipeline/schedule.py` |
| M6 links and liveness | done | `http/links.py`, `pipeline/liveness.py` |
| M7 run orchestration, status/export, Telegram | done | `pipeline/run.py`, `reporting.py`, `notify/{telegram,messages,healthcheck}.py` |
| M8 deployment, backups, README | done (not yet run on a real server) | `deploy/*`, `README.md` |
| M9 storage migrate + review page | **not started; only on request** | `storage migrate` is a stub that exits 2 |

## 4. What happened after M8 (owner testing locally)

The owner ran it locally (Linux) against live sources with a real Groq key and their own
CV. Each change below came from real data:

1. **Local testing guide.** Written as `docs/local-testing.md`. It covers Linux, Windows
   through WSL2 (chosen over native Windows support, see section 8), and a Multipass VM
   rehearsal of `install.sh`. `.gitattributes` keeps `*.sh` and unit files LF.
2. **"Target countries not respected".** They were respected, but filtered jobs are
   stored, which made it look otherwise. The owner then chose: **jobs outside
   `target_countries` are dropped before storage**. That is `location_reason()` in
   `pipeline/prefilter.py`, applied as the `scope` callback in `pipeline/ingest.py`;
   `store_other_countries: false` turns it off. Jobs with an unknown location and
   region-wide remote jobs (EMEA, worldwide) are kept for the AI to judge.
3. **`max_age_days: 7`**, the owner's choice: "recent opportunities only".
4. **Default Remotive category bug.** The shipped default was `software-dev`; the real
   slug is `software-development`. It is now `devops` for this owner. Jobicy queries
   target `emea`, `france` and `germany` with `industry: admin` (DevOps & Infrastructure),
   with `max_pages: 1` to fit the daily cap of 24.
5. **Groq rejected the scoring schema.** Groq strict mode refuses `anyOf: [$ref-to-enum,
   null]`. `ai/schema.py` now inlines enum `$defs` and collapses nullable primitive or
   enum unions into `{"type": [T, "null"], "enum": [..., null]}`. This was verified live
   against Groq.
6. **The prefilter rejected relevant jobs.**
   - An exact title match counted too little. The title-fit points are now
     `TITLE_FIT_POINTS = 9`: a target title alone scores 60, an exact alternative title
     scores 40.
   - The profile, extracted from an English CV, had no German or French title variants.
     These were added through `config/profile.overrides.yaml`. A live comparison on 608
     in-country jobs showed 6 passing before the change and 19 after.
7. **Groq `json_validate_failed` HTTP 400.** Groq returns this when the model omits a
   schema field. `ai/client.py` now turns it into a normal `ChatResult` built from
   `failed_generation`, so the structured layer retries once with the validation error.
   In `pipeline/score.py`, any 400 now marks only that job `ai_error`. Only auth, rate
   limit and 5xx errors stop scoring for the run.
8. **Workbook shows only strong matches**, the owner's request. The setting
   `workbook_decisions: [strong]` controls it.
   - The Opportunities sheet shows only active jobs with those decisions, plus any job
     the owner has written `application_status` or `notes` on.
   - Everything else goes to a **hidden "Other jobs" sheet**. It must stay stored for
     dedupe, notifications and liveness.
   - SQLite has a matching `visible_opportunities` view.
   - `export` defaults to the same decisions.

**Not yet confirmed by the owner:** a run after items 7 and 8. The owner's last reported
run, before those fixes, was `partial, 712 new, 8 scored`.

## 5. Deviations from the spec, and why

| Spec says | Implemented | Why |
|---|---|---|
| systemd `RuntimeMaxSec=15min` | `TimeoutStartSec=15min` | `systemd.service(5)`: `RuntimeMaxSec=` has no effect on `Type=oneshot`; `systemd-analyze verify` warned |
| `flock` on `/var/lib/jobfinder/run.lock` in the unit, plus the app's own fcntl lock | app lock only | both use flock(2) on the same file, so the app's lock attempt would fail under the wrapper and every timer run would "skip" |
| `ReadWritePaths` for data **and logs** | data only | logs go to journald (agreed in M1); there is no log directory |
| Dedupe rule 3 merges any fingerprint match | same-source pairs are flagged, not merged; rule 4 skips same-source candidates | live data: GitLab posts one remote role per country with the same title, so merging lost postings; fuzzy matching flagged 74 of 710 jobs falsely (token_set_ratio scores 100 for subset titles) |
| filtered jobs are stored | jobs outside `target_countries` are dropped before storage (configurable) | owner request (section 4.2) |
| exclude keywords (spec unspecific) | checked in the **title** only | descriptions mention "senior engineers" and similar; a filtered job never reaches the AI, so false negatives are costly |
| AI cache keyed by sha256(description) + profile_version | sha256 of the full user prompt (title, company, location, text) + profile version | template descriptions shared across different jobs would return wrong cached scores |
| model's own `decision` | decision derived from the score: strong ≥ 75, possible ≥ 50, else reject | the label and the score can never disagree |
| DeepSeek `supports_reasoning_effort` (spec implies none) | `true`: low for scoring, high for the profile | DeepSeek thinking defaults to effort "high" and is billed; flagged to the owner, who did not object |
| DeepSeek off-peak "windows" | modelled as **peak** windows Mon–Fri 01:00–04:00 and 06:00–10:00 UTC, off-peak otherwise, plus `peak_exempt_dates` | that is how DeepSeek publishes pricing (checked 2026-10-13) |
| token bucket in M5 | built in M3 | needed so several ATS boards are not fetched back to back |

## 6. Decisions beyond the spec (all approved or explained to the owner)

**Configuration**
- `JOBFINDER_CONFIG_DIR` environment variable: default `./config`; production
  `/etc/jobfinder/config`.
- `selfcheck --offline` skips the network checks.
- `settings.yaml` gained:
  - `links:`: daily cap 200, 6 requests per minute per host, and per-run limits for
    link resolution and liveness;
  - `workbook_decisions` and `store_other_countries`;
  - `log_level`.
- `sources.yaml` `queries` and `options` are validated per source with pydantic models in
  `config.py` (`SOURCE_QUERY_MODELS`, `SOURCE_OPTION_MODELS`). The limits from each
  source's terms live in `SOURCE_TERMS` and are ceilings the configuration may not
  exceed. Adzuna has rolling 7- and 30-day caps.

**Logging and storage**
- Logs are JSON on **stderr**; command output goes to stdout. Loggers are lazy structlog
  proxies with a `component` key (section 7).
- `state.db` schema is versioned, currently **v5**:
  - v2: `job_texts`
  - v3: `robots_cache`
  - v4: `link_cache`, `feed_absence`, `liveness`
  - v5: `notified`

  New tables are added by `create_all`, and the version is bumped in place. A database
  newer than the code is refused.
- The trimmed text of jobs waiting for AI (`pending_ai`) is kept in `state.db`
  `job_texts`, not in the workbook. It is deleted after scoring. Filtered jobs keep no
  text, so they **cannot be re-prefiltered** later; the workbook has to be reset to
  re-evaluate them.
- Datetimes are truncated to **milliseconds** everywhere (`models.to_millis`), because
  that is Excel's precision.
- Excel also neutralises formula injection (cells starting with `=+-@` are written as
  strings) and strips illegal characters.

**Fetching**
- `sources test` is a dry run: it neither stores nor sends ETags
  (`HttpClient.use_validators=False`) and records no source state (`record=False`).
- A source with nothing in storage is fetched **without** conditional requests
  (`HttpClient.fresh_sources`), so a 304 cannot hide jobs after a workbook reset or
  restore.
- Scheduler: up to 5 minutes early is allowed for timer jitter, never below a minimum
  interval from the source's terms (Jobicy).
- Circuit breaker: 3 failed runs pause a source for 24 h; 401/403 pauses it immediately.
  A capped run is not a failure.
- Link resolution refuses IP literals, local host names, non-standard ports, credentials
  in URLs (an SSRF guard), aggregators, and never-fetch sites
  (`http/identity.py: never_fetch`). Liveness may check aggregator pages, except Adzuna
  redirect links.
- **Soft 404:** a redirect from a posting to its board root or a parent page, or a URL
  with `?error=`, counts as expired. Greenhouse answers closed jobs this way.

**Scoring**
- One provider-level failure (auth, rate limit, 5xx) stops scoring for that run. The
  remaining jobs stay `pending_ai`.

**Runs and notifications**
- Run status: `success`; `partial` (exit 0, already alerted); `failed` (exit 1, triggers
  OnFailure, no healthcheck ping).
- Telegram: at most 10 job alerts per run; possible duplicates and jobs older than 7 days
  are skipped. Sent jobs are tracked in `state.db` `notified`, so a failed send is
  retried on the next run.

**Sources**
- E-mail alerts use stdlib `imaplib` (no new dependency). The parsers are
  domain-specific: linkedin.com, indeed.com, welcometothejungle.com, glassdoor.com.

**Deployment**
- Releases go to `/opt/jobfinder/releases/<stamp>-v<ver>`, with `current` switched by an
  atomic symlink.
- uv is pinned to 0.13.0 and manages its own Python 3.12. `--rollback` switches back to
  the previous release.
- `/usr/local/bin/jobfinder` is a wrapper that runs `systemd-run` as the service user.
- `backup.sh` falls back to Python's sqlite3 backup API when the `sqlite3` CLI is
  missing. `restore.sh` (an addition) verifies the MANIFEST and removes stale WAL files.

## 7. Technical gotchas learned the hard way

- **The openai SDK (v3.x) uses `httpx2`, not httpx**, so respx cannot mock it. Tests
  inject `httpx2.AsyncClient(transport=httpx2.MockTransport(...))` through
  `OpenAICompatibleClient(http_client=...)`; see `tests/helpers.py: ScriptedTransport`.
  The SDK's own retries are off (`max_retries=0`), and tenacity handles retries.
- The SDK puts the error object in `exc.body` (the `error` dict). Groq's
  `json_validate_failed` carries `failed_generation`.
- **Groq strict JSON schema:**
  - every property must be required, with `additionalProperties: false`;
  - `anyOf` branches must be distinguishable, so no `$ref`-to-enum alongside null;
  - Groq rejects non-conforming output with a 400 rather than returning it.
- **DeepSeek:** `json_object` mode needs the word "json", the schema and an example in
  the prompt. Thinking mode is on by default. Usage is reported as
  `prompt_cache_hit_tokens`.
- **structlog:** a logger created at import time with `.bind()` freezes the default
  console configuration. `get_logger` returns lazy proxies through
  `structlog.get_logger(component=name)`.
- **openpyxl:**
  - `ws.cell(..., value=None)` does **not** clear a cell; assign `.value = None`.
  - Excel stores milliseconds only.
  - `sheet_state = "hidden"` hides a sheet.
- **Remotive:** at most 4 requests a day, and listings are delayed by 24 h. Unknown
  categories are silently ignored.
- **Jobicy, Himalayas and Remote OK** require attribution and a link back.
- **Greenhouse:** `content` is HTML-entity-encoded twice, so it is unescaped repeatedly.
  The list endpoint includes `first_published` and `company_name`.
- **Ashby:** the documented job object has no `id`, but the live API does. The parser
  falls back to the `jobUrl` UUID.
- **France Travail:** the OAuth scope string is **unverified**. The default
  `api_offresdemploiv2 o2dsoffre` is configurable as `options.scope`. Location labels
  look like `"75 - Paris 8e Arrondissement"`; Corsica uses department codes 2A/2B.
- **Location parsing** (`pipeline/normalise.py`, `pipeline/countries.py`):
  - "City, ST" means a US state unless the city is a known non-US city;
  - "Singapore, Singapore" is a city in a country;
  - comma lists of countries ("Estonia, Spain") are expanded;
  - inline HTML in titles (Adzuna `<strong>`) is stripped.
- **Prefilter:** `token_set_ratio` scores 100 when one title's words are a subset of the
  other's. Title similarity therefore averages `token_set` and `token_sort`.
- **This machine has systemd 249 and no root.** The deployment was only checked
  statically (`systemd-analyze verify`, shellcheck) and by running backup/restore against
  temporary directories.

## 8. The owner's situation and current configuration

- **Profile.** A junior (about 1.8 years) **DevOps / Linux sysadmin** profile. Target
  titles: System Administrator, Linux Administrator, DevOps Engineer, IT Support
  Engineer, Site Reliability Engineer. Based in **Tunisia**.
- **`settings.yaml`:**
  - `target_countries: [TN, FR, DE]`
  - `remote_preference: any`
  - `employment_types: [full_time, internship]`
  - `max_age_days: 7`
  - `prefilter_threshold: 35`, `notify_threshold: 80`
  - `workbook_decisions: [strong]`, `store_other_countries: false`
- **`profile.overrides.yaml`:** English, French and German `synonym_groups` for all
  target and alternative titles (section 4.6).
- **`sources.yaml`:**
  - enabled: greenhouse, lever, ashby, remotive (`devops`), remoteok, jobicy (emea,
    france, germany; admin), himalayas, arbeitnow;
  - disabled: adzuna, france_travail, email_alerts (no keys yet);
  - **`ats_companies` still holds the placeholders GitLab, Palantir and Ramp.** The
    owner was told to replace them with employers hiring in TN, FR or DE.
- **AI:** Groq with `openai/gpt-oss-120b`. The owner has a real Groq key in their local
  `.env`; it is not in the repository.
- **Platforms:** the owner develops on Linux and Windows, using **WSL2** on Windows.
  Native Windows is not supported: `fcntl`, `resource` and a directory fsync are
  Unix-only. The owner chose WSL2 over porting.
- **No Tunisian job board offers an API** (Tanitjobs and Keejob have none), so they are
  out of scope by the rules. Tunisia coverage relies on e-mail alerts and region-wide
  remote jobs.

## 9. Open items and suggested next steps

1. **Confirm the latest fixes with the owner.** Have them run `uv run jobfinder run`
   after section 4 items 7 and 8, and check that scoring completes and the Opportunities
   sheet shows only strong jobs.
2. **Replace the placeholder ATS companies** with the owner's real targets.
3. **More relevant sources for TN, FR and DE.**
   - Enable Adzuna (`country: fr`/`de`) and France Travail. Both need free keys from
     developer.adzuna.com and francetravail.io.
   - Enable `email_alerts`. Ask the owner for **real `.eml` alert samples**; the parsers
     were built from synthetic e-mails (`tests/fixtures/email/*.eml`).
4. **Replace hand-built fixtures** with real recordings once keys exist: AI responses
   (`tests/fixtures/ai/`), Adzuna, France Travail. Verify the France Travail OAuth scope.
5. **Run `jobfinder eval`** with 20–30 jobs the owner has labelled, to choose between
   Groq and DeepSeek models and to check whether DeepSeek JSON mode works while thinking
   is on (unverified).
6. **Rehearse the VPS install** in a Multipass Ubuntu 24.04 VM (local-testing.md Part B).
   `install.sh` has never run on a real server.
7. **Re-evaluating old filtered jobs is impossible** after profile or settings changes,
   because no description is stored. Today the only way is to reset the workbook:
   `rm -rf var/opportunities.xlsx var/rotated`, then make the sources due with
   `UPDATE source_state SET last_attempt_at=NULL`. A future option is to keep the trimmed
   text for filtered jobs too.
8. **Ideas offered to the owner but not chosen:**
   - country-aware fetching: generate Adzuna, Jobicy and Himalayas queries from
     `target_countries`, using Himalayas' search endpoint with `country=TN/FR/DE`;
   - profile-driven search keywords;
   - letting ATS boards bypass `max_age_days` (the owner chose 7 days for everything).

   Revisit these only if the owner asks.
9. **M9**, only when asked.

## 10. Working on the code

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh     # uv 0.13.x; it installs Python 3.12 itself
uv sync
cp .env.example .env && mkdir -p var                # then fill .env (Groq key, CONTACT_EMAIL)
uv run jobfinder selfcheck --offline
uv run ruff format --check src tests && uv run ruff check src tests && uv run mypy && uv run pytest -q
```

**Not in Git:**
- `.env`, which holds the secrets;
- `var/`, which holds the owner's workbook, `state.db`, profile versions and the copy of
  their CV.

To continue with the same data, copy `var/` (at least `var/profiles/` and `var/cv/`) to
the new machine. Otherwise re-import the CV with `jobfinder profile import`; that costs
one AI call.

**Test conventions** (`tests/helpers.py`):
- `ScriptedTransport` for the openai SDK (httpx2), and respx for everything on httpx;
- `FakeClock` (its sleep advances time) and `FakeSleep`;
- `make_raw_job()` and `make_profile_record()` for test data;
- `load_source_fixture()` / `load_ai_fixture()` for fixtures;
- `tests/conftest.py` gives an isolated environment, a copy of `config/` per test
  (`config_dir`, `edit_yaml`), and `app_config`.

Source fixtures are real recordings trimmed to 3 jobs, except Adzuna and France Travail,
which are hand-built.

**Code style.** Small typed functions; frozen dataclasses or pydantic models; no global
mutable state; timezone-aware UTC everywhere; every module and public function has a
docstring that explains why, not what. Comment density is low.
