# CLAUDE.md

jobfinder: a personal, AI-assisted job and internship finder for one owner. It runs
hourly on an Ubuntu 24.04 VPS (Python 3.12, uv, systemd).

**Before doing anything, read [docs/HANDOFF.md](docs/HANDOFF.md).** It has the project
state, every deviation from the spec and why, the owner's configuration, and open items.
The owner's original brief is [docs/SPEC.md](docs/SPEC.md).

## Rules the owner set (always apply)

- After each piece of work, run the checks and show the results with a short summary,
  then stop and wait for the go-ahead:
  `uv run ruff format --check src tests && uv run ruff check src tests && uv run mypy && uv run pytest -q`
- **Never run `git commit`.** The owner commits.
- Ask before adding any dependency outside the spec's stack (see HANDOFF section 2).
- Do not change technology choices without asking.
- Never commit secrets. Document every setting in `.env.example` and README.
- Respect source terms exactly.
  - Never scrape LinkedIn, Indeed, Glassdoor or any site without an API or feed.
  - Never bypass CAPTCHAs, logins or bot protection.
  - Never submit applications.
  - Keep aggregator URLs for attribution.
- Small typed functions, no global mutable state, timezone-aware UTC.
- M9 (`storage migrate` + review page) is built only when the owner asks.

## Commands

```sh
uv sync                                   # install
uv run jobfinder selfcheck --offline      # config and storage check
uv run jobfinder run | status | export | sources test [name] | profile import <cv> | eval --labels <csv>
uv run pytest -q                          # about 650 tests, no network
```

Logs are JSON on stderr. Local data lives in `var/` (`DATA_DIR=./var` in `.env`). Both
`var/` and `.env` are gitignored and have to be copied by hand.
