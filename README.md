# jobfinder

A personal, AI-assisted job and internship finder. It runs unattended on a small Ubuntu 24.04
VPS and once an hour:
- collects postings from employer job boards (Greenhouse, Lever, Ashby), remote-job feeds
  (Remotive, Remote OK, Jobicy, Himalayas, Arbeitnow), Adzuna, France Travail and your
  job-alert e-mails;
- removes duplicates;
- scores each posting against a profile built from your CV;
- keeps everything in an Excel workbook (or SQLite);
- sends the best matches to Telegram.

It uses documented APIs and feeds only. It never scrapes sites without one, never bypasses
logins or bot protection, never fetches LinkedIn, Indeed or Glassdoor, and never applies
for you.

Contents: [Install](#install) · [First-time setup](#first-time-setup) ·
[Everyday use](#everyday-use) · [Configuration reference](#configuration-reference) ·
[How it works](#how-it-works) · [Adding a source](#adding-a-source) ·
[Troubleshooting](#troubleshooting) · [Backup and restore](#backup-and-restore) ·
[Update and rollback](#update-and-rollback) · [Development](#development)

## Install

You need an Ubuntu 24.04 server with outbound HTTPS, root access, and about 1 GB of free
disk space. Then:

```sh
git clone <this repository> jobfinder && cd jobfinder
sudo deploy/install.sh
```

`install.sh` is idempotent, so running it again updates an existing installation. It:

1. installs `sqlite3`, `curl` and `ca-certificates` if they are missing;
2. creates the system user `jobfinder` and the directories below;
3. installs a pinned `uv` into `/opt/jobfinder/bin`. `uv` installs Python 3.12 for
   jobfinder's own use, so system Python updates cannot break it;
4. builds an immutable, root-owned release in `/opt/jobfinder/releases/` from
   `uv.lock` (`uv sync --frozen`), then points `/opt/jobfinder/current` at it;
5. creates `/etc/jobfinder/jobfinder.env` (mode 600) and `/etc/jobfinder/config/*.yaml`
   only if they do not exist yet. Your edits are never overwritten;
6. installs the systemd units and the `/usr/local/bin/jobfinder` wrapper;
7. runs `jobfinder selfcheck --offline`. The timers are enabled only when it passes, so
   an unconfigured install does not fail every hour.

| Path | What | Owner, mode |
|---|---|---|
| `/opt/jobfinder/current` | active release (code and virtualenv) | root, read-only |
| `/etc/jobfinder/jobfinder.env` | secrets and paths | root, 600 |
| `/etc/jobfinder/config/` | `settings.yaml`, `sources.yaml`, `providers.yaml`, `profile.overrides.yaml` | root:jobfinder, 640 |
| `/var/lib/jobfinder/` | `opportunities.xlsx`, `state.db`, `profiles/`, `cv/`, `rotated/` | jobfinder, 700 |
| `/var/backups/jobfinder/` | daily backups | jobfinder, 700 |

### systemd units

| Unit | Purpose |
|---|---|
| `jobfinder.timer` | hourly, with up to 5 min random delay; missed runs are caught up (`Persistent=true`) |
| `jobfinder.service` | one `jobfinder run`; oneshot, limited to 15 min and 512 MB, sandboxed (see below); `OnFailure=` the alert unit |
| `jobfinder-alert.service` | `jobfinder notify --failure`: Telegram message when a run (or backup) fails |
| `jobfinder-backup.timer` / `.service` | daily backup at 03:30 UTC, keeping 14 |

Sandboxing:
- `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp` and
  `PrivateDevices`;
- the data directory is the only writable path;
- no capabilities, and network access limited to IP and Unix sockets.

Logs go to the journal as one JSON object per line.

Two details differ from a naive reading of the plan:
- **Run time limit.** For a `Type=oneshot` service, systemd ignores `RuntimeMaxSec=`. The
  limit is set with `TimeoutStartSec=15min`, which does apply.
- **Locking.** `jobfinder run` takes the lock on `/var/lib/jobfinder/run.lock` itself and
  exits with "skipped" if a run already holds it. The unit does not wrap it in `flock(1)`
  on the same file, because the two locks would conflict and every run would skip.

## First-time setup

```sh
sudoedit /etc/jobfinder/jobfinder.env    # AI key, CONTACT_EMAIL, Telegram, healthcheck
sudoedit /etc/jobfinder/config/settings.yaml /etc/jobfinder/config/sources.yaml

# Build your profile from your CV (PDF or DOCX):
sudo install -o jobfinder -m 600 ~/my-cv.pdf /var/lib/jobfinder/cv-upload.pdf
sudo jobfinder profile import /var/lib/jobfinder/cv-upload.pdf
sudo jobfinder profile show

sudo jobfinder selfcheck          # also checks that the AI provider offers your model
sudo jobfinder notify --test      # sends a Telegram test message
sudo jobfinder sources test       # fetches from each enabled source; stores nothing
sudo systemctl enable --now jobfinder.timer jobfinder-backup.timer
```

Then do three things:
- **Telegram:** create a bot with @BotFather and copy its token into
  `TELEGRAM_BOT_TOKEN`. Send the bot a message, then read your chat id from
  `https://api.telegram.org/bot<token>/getUpdates` and put it in `TELEGRAM_CHAT_ID`.
- **Healthcheck:** create a check at healthchecks.io (or similar) with a 1-hour period
  and put its ping URL in `HEALTHCHECK_URL`. If runs stop succeeding, the service warns
  you.
- **Target companies:** replace the example companies in `ats_companies` in
  `sources.yaml` with companies you want to work for.

## Everyday use

The `/usr/local/bin/jobfinder` wrapper runs commands as the `jobfinder` user with the
service's environment, so prefix them with `sudo`.

| Command | What it does |
|---|---|
| `sudo jobfinder status` | recent runs, each source's state ("due", "next due", "paused until…"), AI spend against the caps, counts by decision, profile version |
| `sudo jobfinder export > jobs.csv` | active strong and possible jobs, best first (`--all`, `--min-score N`, `--format json`, `-o FILE`) |
| `sudo jobfinder run` | one pass now, as the timer would run it |
| `sudo jobfinder sources test [name]` | dry-run fetch, without storing anything or changing any state |
| `sudo jobfinder profile import <cv>` / `profile show` | rebuild or view the profile (no-op if neither the CV nor the overrides changed) |
| `sudo jobfinder eval --labels labels.csv` | compare AI models against your own labels (see [Comparing models](#comparing-models)) |
| `sudo jobfinder selfcheck [--offline]` | configuration, storage and AI provider check |
| `sudo jobfinder notify --test` | Telegram test message |

### The workbook

`/var/lib/jobfinder/opportunities.xlsx` has these sheets:

| Sheet | Contents |
|---|---|
| Opportunities | the jobs worth your time, best score first: by default only `strong` matches that are still open (`workbook_decisions` in `settings.yaml`) |
| Other jobs (hidden) | every other job jobfinder has seen. They are kept so they are not fetched, scored or notified again; unhide the sheet in Excel to look at them |
| Sightings | the same job seen on other sources |
| Runs | one row per run |
| Profile | the profile in use |

**Columns that are yours.** `application_status` and `notes` belong to you. A job you
have written in stays on the Opportunities sheet even after the listing expires. jobfinder
never overwrites them, and re-reads them from the file just before every write, so edits
made while a run is in progress are kept.

To work on the workbook from your own computer:

```sh
ssh vps 'sudo cat /var/lib/jobfinder/opportunities.xlsx' > opportunities.xlsx   # download
# edit only application_status and notes, then upload it back:
cat opportunities.xlsx | ssh vps 'sudo -u jobfinder tee /var/lib/jobfinder/opportunities.xlsx >/dev/null'
```

Upload soon after downloading. Jobs found between your download and the upload are re-added
on later runs. Their AI scores come from the cache, so this costs nothing, and you are not
alerted about them twice. Columns you add yourself are not preserved.

## Configuration reference

All configuration is checked when jobfinder starts. Every problem is listed at once and
the command exits with status 1.

### Environment (`/etc/jobfinder/jobfinder.env`, or `.env` in development)

| Variable | Required | Meaning |
|---|---|---|
| `AI_PROVIDER` | yes | `groq` (development) or `deepseek` (production) |
| `GROQ_API_KEY` / `DEEPSEEK_API_KEY` | for the active provider | API keys |
| `AI_MODEL_SCORE`, `AI_MODEL_PROFILE` | no | model overrides; must be listed under the provider in `providers.yaml`. Defaults: groq `openai/gpt-oss-120b`, deepseek `deepseek-v4-pro` |
| `CONTACT_EMAIL` | yes | sent in the User-Agent: `jobfinder/<version> (+mailto:…)` |
| `DATA_DIR` | no | default `/var/lib/jobfinder` |
| `JOBFINDER_CONFIG_DIR` | no | default `./config`; the installer sets `/etc/jobfinder/config` |
| `STORAGE_BACKEND` | no | `excel` (default) or `sqlite` (`DATA_DIR/opportunities.db`, same columns) |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | if adzuna is enabled | from developer.adzuna.com |
| `FRANCE_TRAVAIL_CLIENT_ID`, `FRANCE_TRAVAIL_CLIENT_SECRET` | if france_travail is enabled | from francetravail.io |
| `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` | if email_alerts is enabled | mailbox with your job alerts; use an app password |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | no (set both or neither) | alerts |
| `HEALTHCHECK_URL` | no | pinged after every successful or partial run |

Empty values count as unset. Secret values are removed from all logs.

The backup unit also uses `BACKUP_DIR` (default `/var/backups/jobfinder`) and
`BACKUP_KEEP` (default 14).

### `settings.yaml`

| Key | Default | Meaning |
|---|---|---|
| `target_countries` | | ISO-3166 alpha-2 codes where you can work on site |
| `remote_preference` | | `remote_only`, `remote_first`, `any` or `onsite_only` |
| `employment_types` | | any of `full_time`, `part_time`, `contract`, `internship`, `apprenticeship`, `temporary` |
| `max_age_days` | | ignore older postings |
| `prefilter_threshold` | 35 | keyword score (0-100) needed before the AI sees a job |
| `notify_threshold` | 80 | AI score needed for a Telegram alert |
| `ai_daily_budget_usd` | 1.00 | AI spend cap per UTC day |
| `ai_monthly_budget_usd` | 20 | AI spend cap per UTC month |
| `log_level` | INFO | DEBUG, INFO, WARNING or ERROR |
| `workbook_decisions` | [strong] | which jobs the Opportunities sheet shows and `export` exports (any of strong, possible, reject, pending_ai, ai_error, filtered); the rest are kept on a hidden "Other jobs" sheet |
| `store_other_countries` | false | jobs located outside `target_countries` (or remote only for other countries) are dropped before storage; `true` keeps them as `filtered` |
| `links.daily_request_cap` | 200 | requests per day for apply-link resolution and liveness checks |
| `links.requests_per_minute` | 6 | per host |
| `links.max_resolutions_per_run` | 30 | apply links resolved per run |
| `links.max_liveness_checks_per_run` | 20 | postings re-checked per run |

### `sources.yaml`

Each entry under `sources:` has these keys:

| Key | Meaning |
|---|---|
| `enabled` | fetch from this source |
| `min_interval_minutes` | minimum time between fetches |
| `daily_request_cap` | requests per UTC day; retries count |
| `requests_per_minute` | `{host: rpm}`; unlisted hosts get 30 |
| `queries` | source-specific searches (below) |
| `options` | source-specific settings (below) |

Limits from a source's terms always apply. A configuration that would exceed them is
rejected at startup.

| Source | `queries` keys | `options` | Limits from the source's terms |
|---|---|---|---|
| greenhouse, lever, ashby | (none; uses `ats_companies`) | | |
| remotive | `category`, `search`, `company_name`, `limit` | | 4/day, 2/min |
| remoteok | (none) | | |
| jobicy | `geo`, `industry`, `tag` | `max_pages` | at most hourly |
| himalayas | (none) | `max_pages` (20 jobs/page) | |
| arbeitnow | `visa_sponsorship` | `max_pages` | 50/min |
| adzuna | `country` (required, e.g. `fr`), `what`, `what_exclude`, `where`, `category`, `max_days_old`, `results_per_page` (≤50) | `max_pages` | 25/min, 250/day, 1000 per 7 days, 2500 per 30 days |
| france_travail | `motsCles`, `codeROME`, `departement`, `commune`, `distance`, `typeContrat`, `publieeDepuis` (1, 3, 7, 14, 31) | `max_pages` (150 offers/page), `scope` | 10/s |
| email_alerts | (none) | `allowed_senders` (linkedin.com, indeed.com, welcometothejungle.com or glassdoor.com addresses), `mailbox`, `mark_seen`, `max_messages` | |

`ats_companies` entries take `name`, `provider` (`greenhouse`, `lever` or `ashby`),
`board_token` (the name in the careers URL, e.g. `boards.greenhouse.io/<token>`), and
for Lever an optional `region: eu`.

### `providers.yaml`

AI providers, all reached through the OpenAI-compatible API. Each provider has:
- `base_url`
- `structured_output`: `strict_json_schema` (Groq) or `json_object` (DeepSeek)
- `reasoning_effort` and `max_output_tokens`, set per purpose (`score`, `profile`)
- `default_models`
- for DeepSeek, `peak_windows_utc` and `peak_exempt_dates`

Each model has `price` and, optionally, `off_peak_price` (USD per million tokens: input,
cached input, output) and `extra_body`.

`eval_models` lists the models `jobfinder eval` compares. Prices and model names change,
so check them against the provider's documentation. `selfcheck` fails if the configured
model is not offered.

### `profile.overrides.yaml`

Any profile field (`target_titles`, `exclude_keywords`, `seniority`, …). A value here
replaces the AI-extracted one. When only this file changed, `profile import` creates a
new version without another AI call.

## How it works

1. **Schedule.** A source is fetched when it is enabled, not paused, its daily cap is
   not used up, and `min_interval_minutes` have passed since its last attempt. The
   interval may be cut by up to 5 minutes to absorb the timer's random delay, but never
   below an interval the source's terms impose.
2. **Fetch.** Due sources are fetched concurrently, at most one request at a time per
   host and four overall. Each request:
   - waits for a per-host token bucket, stored in `state.db`, so the rate holds across
     runs;
   - re-sends the stored ETag/Last-Modified;
   - is retried up to 3 times on timeouts, 5xx and 429, honouring Retry-After up to
     120 s;
   - counts against the daily cap, and for Adzuna also against the 7- and 30-day caps.

   Any request that is not a documented API endpoint is checked against robots.txt
   (cached for 24 h). Fetching stops after 8 minutes, so results are always stored.
3. **Normalise.** Titles, companies and locations are cleaned: entities, inline HTML,
   gender markers such as (m/w/d) or (H/F), and legal suffixes such as GmbH or SAS are
   removed. Country, city, remote type, employment type and seniority are detected.
4. **Dedupe**, in this order:
   1. same source and job id: the stored job is updated;
   2. same canonical URL (tracking parameters removed): the listing is recorded as a
      sighting;
   3. same company, title and city: the listings are merged, and an employer-ATS
      listing becomes the main record, with its apply link;
   4. same company, near-identical title (rapidfuzz token_set_ratio ≥ 92) and published
      within 30 days: the job is stored but flagged `is_possible_duplicate`.

   Two listings from the same source with different ids are never merged.
5. **Country scope.** Jobs located outside `target_countries`, or remote but restricted
   to other countries, are dropped before storage (counted as "off-country" in `status`)
   unless `store_other_countries: true`. Jobs with an unknown location and region-wide
   remote jobs (EMEA, worldwide) are kept for the AI to judge.
6. **Prefilter (no AI).** A job is stored as `filtered`, with the reason in
   `score_reason`, if any of these hold:
   - its employment type is not one you want;
   - it is too old;
   - it is outside your countries or remote preference;
   - its title contains an exclude keyword;
   - its title is two or more levels above your seniority;
   - it asks for more than your years of experience plus 3;
   - its keyword score is below `prefilter_threshold`. Profile skills and their synonyms
     score 3 points in the title and 1 in the text. A title matching one of your target
     titles (or a synonym, in any language you list) adds 9 points, which passes on its
     own; an alternative title adds 6. 15 points scores 100.

   **Tip:** the AI extracts title synonyms from your CV's language. For jobs in other
   languages, list translated titles in `synonym_groups` in `profile.overrides.yaml`, for
   example `[System Administrator, Systemadministrator, IT-Administrator, Administrateur
   systèmes]`.
7. **Score.**
   - **Text:** boilerplate (benefits, about-us, EEO) is removed and the posting is
     trimmed to about 1,200 tokens.
   - **Rubric:** the AI scores it against your profile: skills 40, role fit 20,
     seniority 15, location 15, employment type 10. It is told to separate genuine
     requirements from keyword stuffing.
   - **Decision:** `strong` from 75, `possible` from 50, otherwise `reject`.
   - **Validation:** replies are validated; one invalid reply is retried with the
     error, and a second one gives `ai_error`.
   - **Cache:** results are cached by posting and profile version.
   - **Budget:** every call's tokens and cost are recorded. A job that would exceed a
     budget cap stays `pending_ai` until the budget allows. Groq's rate-limit headers
     are respected.
8. **Apply links.** For strong and possible jobs, the apply link's redirects are
   followed to the final employer or ATS page, using HEAD or a GET whose body is never
   read, at most 5 hops, robots.txt checked on every hop. Results are cached
   permanently. Aggregator links (Adzuna's `redirect_url` and the others) are never
   resolved. IP addresses, local host names and odd ports are refused.
9. **Liveness.**
   - **Full-feed sources** (ATS boards, Remote OK, Remotive): a job missing from two
     complete fetches in a row becomes `expired`.
   - **Other jobs** scoring 50 or more are re-checked weekly. A 404/410, or a redirect
     to a listing page, marks them `expired`. Three other failures in a row mark them
     `inaccessible`.
10. **Store and notify.**
   - **Storage:** the workbook is written once per run, with the user columns merged,
     as a temporary file renamed into place, keeping 7 earlier copies in
     `DATA_DIR/rotated/`. A Runs row and a `run_stats` log line record the duration,
     peak memory, requests and errors per source, counts, tokens and cost.
   - **Telegram:** up to 10 new matches at or above `notify_threshold` are sent per run.
     Each message links the original listing and names its source.
   - **Alerts:** source pauses and AI failures are sent as alerts.
   - **Healthcheck:** pinged at the end.

**Run status.**
- `success`: nothing went wrong.
- `partial`: something needs attention and an alert was sent. The exit code is 0.
- `failed`: storage could not be written, or something unexpected happened. The exit
  code is 1, which triggers the failure alert, and the healthcheck is not pinged.

**Pausing.**
- Three failed runs in a row pause a source for 24 hours.
- A 401 or 403 pauses it immediately, because its credentials need fixing.
- Both send an alert.

### Comparing models

`labels.csv` needs the columns `title`, `description` and `label` (`strong`,
`possible` or `reject`); `company` and `location` are optional. Every model under
`eval_models` whose provider key is set scores each row. The report shows:
- agreement with your labels, exact and within one band;
- the JSON failure rate after one retry, and how many rows needed a retry;
- average latency;
- cost per job, and the projected monthly cost.

Eval spend is recorded but not capped.

## Adding a source

1. **Check the source's terms.** Use a documented API or feed only. Write down its rate
   limits and attribution rules.
2. **Add the name** to `SourceName` in `src/jobfinder/config.py`. Put any limits from
   the terms into `SOURCE_TERMS`; they become ceilings the configuration cannot exceed.
   If the source takes queries or options, add pydantic models to `SOURCE_QUERY_MODELS`
   and `SOURCE_OPTION_MODELS`.
3. **Write `src/jobfinder/sources/<name>.py`** with:
   - a pure `parse_job(record) -> RawJob | None` that sets `source`, `source_job_id`
     and `is_ats`, and keeps the source's own URL as `posting_url` when its terms ask
     for a link back;
   - a class with `name` and `async fetch(ctx, state) -> list[RawJob]` that makes every
     request through `ctx.http.get_json(...)`, so caps, rate limits, ETags, retries and
     robots.txt apply automatically.

   Use `parse_records` so one malformed record is skipped rather than fatal. Use
   `fetch_pages` for paged feeds. Re-raise `SourceAuthError` and `CapReachedError`, and
   add other errors to `ctx.report.errors`.
4. **Register the class** in `sources/registry.py`. If the source returns its complete
   listing on every fetch, add it to `FULL_FEED_SOURCES` in `pipeline/liveness.py`.
5. **Add tests.** Record one real response, trimmed to a few jobs, in
   `tests/fixtures/sources/`, and test the parser against it. Add fetch tests with
   respx.
6. **Configure it.** Add an entry to `config/sources.yaml` with a comment on its terms,
   and add a row to the source table in this README.

## Troubleshooting

**Logs.**
```sh
journalctl -u jobfinder.service -n 200                       # last runs
journalctl -u jobfinder.service -o cat | jq 'select(.event=="run_stats")'
journalctl -u jobfinder.service -o cat | jq 'select(.level=="error")'
systemctl list-timers 'jobfinder*'                            # next and last runs
sudo jobfinder status
```

**A run failed (Telegram alert or `failed` status).** Read the `run_stage_failed` and
`run_stats` lines. Usually the cause is one of these:
- the workbook is damaged: the error says "restore a copy". Copy the newest file from
  `/var/lib/jobfinder/rotated/` over `opportunities.xlsx`;
- the disk is full;
- the configuration is broken: run `sudo jobfinder selfcheck`.

**A source is paused.** `status` shows why and until when.
- **401/403:** fix its credentials in `jobfinder.env`.
- **Three failed runs:** check `sudo jobfinder sources test <name>`.

The pause ends by itself. To lift it now:
```sh
sudo -u jobfinder sqlite3 /var/lib/jobfinder/state.db \
  "UPDATE source_state SET paused_until=NULL, consecutive_failures=0 WHERE source='adzuna';"
```

**A source shows "daily request cap used up".** Its cap resets at 00:00 UTC. Raise
`daily_request_cap` within the source's terms if you need more.

**Jobs stay `pending_ai`.** One of these is the cause:
- **The AI budget is used up:** `status` shows today's and this month's spend against
  the caps. Jobs are scored once the budget allows, so raise the caps or wait.
- **No profile:** run `profile import`.
- **AI provider failing:** see the alerts; `selfcheck` tests the key and the model.

**Many `ai_error` jobs.** The model keeps returning invalid JSON. Try another model with
`jobfinder eval`, and set `AI_MODEL_SCORE`.

**No Telegram messages.** Check these in turn:
- `sudo jobfinder notify --test`;
- that `notify_threshold` is not too high;
- that jobs are being scored at all (`status`).

Messages that fail are sent again on the next run.

**Too many or too few jobs.** Tune `prefilter_threshold`, `target_countries`,
`employment_types` and `max_age_days`, and the profile through
`profile.overrides.yaml` (`exclude_keywords`, `target_titles`).

## Backup and restore

**Backups.** The daily backup writes `/var/backups/jobfinder/jobfinder-<UTC
stamp>.tar.gz` and keeps the newest 14. Each archive contains:
- an online copy of `state.db` (and `opportunities.db`), taken with `sqlite3 .backup` and
  integrity-checked;
- the workbook, copied under its lock;
- `profiles/` and `cv/`;
- the YAML configuration;
- a `MANIFEST` of SHA-256 checksums.

`jobfinder.env` is not included; keep your secrets in a password manager. Copy the
backup directory off the server, for example with `rsync` from your own computer.

```sh
sudo systemctl start jobfinder-backup.service       # back up now
ls -l /var/backups/jobfinder/
sudo /opt/jobfinder/current/deploy/restore.sh /var/backups/jobfinder/jobfinder-20261014T033012Z.tar.gz
```

**Restore.** `restore.sh`:
1. verifies the archive's checksums;
2. stops the timer and waits for a running pass to finish;
3. moves the current data to `/var/lib/jobfinder/pre-restore-<stamp>/`;
4. restores the databases, workbook, profiles and CVs, removing stale SQLite WAL files;
5. fixes ownership and restarts the timer.

The archived configuration is extracted next to the live one for comparison. Add
`--with-config` to restore it as well.

## Update and rollback

```sh
cd jobfinder && git pull
sudo deploy/install.sh               # builds a new release and switches to it atomically
sudo deploy/install.sh --rollback    # switch back to the previous release
```

The last 3 releases are kept in `/opt/jobfinder/releases/`. Updating never touches
`jobfinder.env`, your YAML files or your data. When a release ships a different
default for a YAML file you have edited, the installer tells you and leaves your file
alone. Compare it with the new default in `/opt/jobfinder/current/config/`.

**Database changes.** `state.db` upgrades itself in place; its schema is versioned. An
older release refuses to open a `state.db` written by a newer one. To roll back across
such a change, restore a backup taken before the update.

## Development

To try jobfinder on your own Linux or Windows (WSL2) machine before deploying, and to
rehearse the server install in a local VM, follow
[docs/local-testing.md](docs/local-testing.md).

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh    # once
uv sync
cp .env.example .env && mkdir -p var               # DATA_DIR=./var
uv run jobfinder selfcheck --offline

uv run ruff format --check src tests && uv run ruff check src tests
uv run mypy                                        # strict, on src/
uv run pytest
```

**Test data.** Tests never touch the network. Source fixtures are real responses
recorded from the public APIs and trimmed to a few jobs. Adzuna, France Travail and the
AI provider responses are hand-built in each provider's documented format, because no
keys were available; `tests/fixtures/ai/README.md` explains this. The job-alert e-mails
are synthetic.

**Data use.** Respect each source's terms. Remotive, Remote OK, Jobicy and Himalayas
require a link back and credit, which the Telegram messages and the `posting_url` column
provide. Their jobs may not be republished on other job sites. Himalayas data is for
personal use. Adzuna data may be used for personal research.
