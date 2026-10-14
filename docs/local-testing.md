# Testing jobfinder locally before the VPS

This guide has two parts:
- **[Part A](#part-a-run-the-app-locally)**: run the app from a checkout on Linux, or on
  Windows through WSL2. This covers your profile, real sources, AI scoring, Telegram and
  the workbook.
- **[Part B](#part-b-rehearse-the-vps-install-in-a-vm)**: rehearse the real server install
  (`install.sh`, systemd timers, backups, update, rollback) in a throwaway Ubuntu 24.04
  virtual machine.

Do Part A on each machine, then Part B once.

Why WSL2 on Windows: jobfinder relies on Linux file locking and systemd, the same as the
VPS. WSL2 runs a real Ubuntu, so the app runs unchanged, and your Windows test exercises
exactly what the server will run.

## What you need

- **A Groq API key.** Create one at <https://console.groq.com>; the free tier is enough
  for testing.
- **Your CV** as a text-based PDF or DOCX. Scanned images will not work.
- **Optional:**
  - a Telegram bot token and your chat id (see [Telegram](#6-telegram-optional));
  - Adzuna or France Travail keys, if you want to try those sources.

## Part A: run the app locally

### 1. Get a Linux shell

**On Linux:** open a terminal. Any recent distribution works.

**On Windows 10 or 11:** install WSL2 with Ubuntu 24.04. In PowerShell as administrator:

```powershell
wsl --install -d Ubuntu-24.04
```

Reboot if asked, open "Ubuntu 24.04" from the Start menu and create your Linux user.
Then work **inside the Linux file system** (`~/...`), not under `/mnt/c/...`:
- Windows drives mounted at `/mnt/c` are slow from Linux;
- file locks are unreliable there;
- Git may convert line endings.

### 2. Install uv and the project

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
exec $SHELL -l                         # picks up ~/.local/bin

git clone <your repository> ~/jobfinder    # or copy the folder; see the note below
cd ~/jobfinder
uv sync                                # downloads Python 3.12 and the locked dependencies
uv run jobfinder --help
```

If the project is not in a Git repository yet, copy the folder instead. From WSL, the
Windows copy is under `/mnt/c/...`:

```sh
rsync -a --exclude .venv --exclude var /mnt/c/path/to/Job_Scrapper/ ~/jobfinder/
```

### 3. Configure

```sh
cp .env.example .env
mkdir -p var                          # local data directory (DATA_DIR=./var in .env)
nano .env                             # or: code .env
```

Set at least these values in `.env`:

```sh
AI_PROVIDER=groq
GROQ_API_KEY=gsk_...
CONTACT_EMAIL=you@example.com         # your real address: it goes in the User-Agent
DATA_DIR=./var
JOBFINDER_CONFIG_DIR=./config
```

Then edit the YAML files in `config/`:

- **`settings.yaml`:**
  - `target_countries` and `employment_types`;
  - while testing, lower `ai_daily_budget_usd` (for example to `0.20`) so a mistake
    cannot cost much.
- **`sources.yaml`:**
  - replace the example `ats_companies` (GitLab, Palantir, Ramp) with two or three
    companies you would apply to;
  - enable any extra sources you want to try.

If you're testing on the development machine where jobfinder was built, `var/` already
holds about 700 test jobs and `.env` has a placeholder key. Start clean:

```sh
rm -rf var && mkdir var
```

### 4. Check the setup

```sh
uv run jobfinder selfcheck --offline   # config, data directory, state.db, lock
uv run jobfinder selfcheck             # also: is your Groq key valid, is the model offered?
```

Every line should read `OK`. `WARN` for Telegram or the healthcheck is fine at this point.

### 5. Try the sources (dry run)

```sh
uv run jobfinder sources test          # every enabled source; stores nothing
uv run jobfinder sources test remotive
```

You should see `OK <source>: N jobs` with a few sample titles. Some details:
- `sources test` uses requests from each source's daily cap but changes no other state;
- Remotive allows only 4 requests a day, so don't loop on it;
- a `FAIL` with `HTTP 404 ... (check board_token)` means a company's `board_token` in
  `sources.yaml` is wrong.

### 6. Telegram (optional)

1. In Telegram, talk to **@BotFather**, send `/newbot`, and copy the token.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
   `"chat":{"id": ...}`.
4. Put both values in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), then:

```sh
uv run jobfinder notify --test         # you should receive a message
```

### 7. Build your profile

```sh
uv run jobfinder profile import ~/my-cv.pdf     # WSL: /mnt/c/Users/<you>/Documents/cv.pdf works too
uv run jobfinder profile show
```

Check the extracted skills, `target_titles` and `seniority`. To correct anything, set it
in `config/profile.overrides.yaml` and run `profile import` again. When only the
overrides changed, it makes a new profile version without another AI call.

### 8. Run it

```sh
uv run jobfinder run
uv run jobfinder status
```

The first run:
- fetches every enabled source;
- prefilters the new jobs;
- scores the best candidates within your budget (the rest wait as `pending_ai`);
- resolves apply links for strong and possible matches;
- sends Telegram messages for jobs scoring at least `notify_threshold` (80).

It prints a line such as
`run 01M…: success, 412 new, 25 scored, $0.0123, 95s`. The JSON log lines go to stderr.

`partial` means something needs attention. The line and `status` say what; a typical
cause is "no profile yet".

### 9. Look at the results

```sh
uv run jobfinder export > ~/jobs.csv           # active strong/possible jobs, best first
uv run jobfinder export --all --format json -o ~/all.json
```

Open the workbook `var/opportunities.xlsx`:
- **On Linux:** use LibreOffice Calc.
- **On WSL:** copy it to Windows first and open the copy:
  ```sh
  cp var/opportunities.xlsx /mnt/c/Users/<you>/Desktop/
  ```
  Opening the live file from Windows can block the next run's save. jobfinder writes a
  new file and swaps it into place, so a file held open by Excel can make that swap fail.

What to check:
- **Opportunities sheet:**
  - `decision`, `relevance_score`, `score_reason`, `red_flags` and `summary` make sense;
  - filtered jobs explain why in `score_reason` (`Filtered: …`);
  - `apply_url_final` is filled for strong and possible jobs.
- **Runs sheet:** one row per run.
- **Profile sheet:** your profile.

### 10. Test the workbook round trip

Make sure your own columns survive a run:

1. Open `var/opportunities.xlsx` in LibreOffice on Linux. On WSL, copy it to Windows,
   edit it, and copy it back while no run is in progress.
2. Type something in `application_status` and `notes` for two jobs. Save, and close the
   file.
3. Run `uv run jobfinder run` again.
4. Reopen the workbook. Your two edits must still be there.

### 11. Simulate the hourly schedule

Each source has a `min_interval_minutes` (Remotive 6 h, ATS boards 6 h, and so on), so
running again right away mostly prints "next due …" in `status`. To watch a few cycles,
pick one of these:

```sh
# Option A: run hourly in a terminal you leave open
while true; do uv run jobfinder run; sleep 3600; done

# Option B: make one source due again immediately (example: lever)
uv run python -c "import sqlite3; c=sqlite3.connect('var/state.db'); c.execute(\"UPDATE source_state SET last_attempt_at=NULL WHERE source='lever'\"); c.commit()"
uv run jobfinder run
```

Things worth checking over a few runs:
- a second run adds no duplicates (`0 new` for a source fetched again);
- the same job never produces a second Telegram message;
- `status` shows the AI spend going up and staying under the caps.

### 12. Compare AI models (optional)

Label 20–30 jobs from your export as `strong`, `possible` or `reject` in a CSV with the
columns `title,company,location,description,label`, then:

```sh
uv run jobfinder eval --labels my-labels.csv
```

Only models whose provider key is set are evaluated; add a `DEEPSEEK_API_KEY` to compare
DeepSeek too. This answers which model agrees with you best, and at what monthly cost.

### 13. Run the test suite (optional)

```sh
uv run pytest -q          # about 630 tests, no network needed
```

### Reset

```sh
rm -rf var && mkdir var    # forget all jobs, runs, caches and spend records; keeps .env and config
```

## Part B: rehearse the VPS install in a VM

`install.sh` has not yet run on a real server, so rehearse it once in a throwaway Ubuntu
24.04 VM. Multipass makes this a few commands on both Linux and Windows.

### 1. Install Multipass

- **Linux:** `sudo snap install multipass`
- **Windows:** download the installer from <https://multipass.run/install>. On Windows
  Pro or Enterprise it uses Hyper-V. On Windows Home, choose VirtualBox during setup and
  install VirtualBox first.

Run the commands below in a terminal: a Linux shell, WSL, or PowerShell (Multipass's
`multipass` command works in all of them).

### 2. Create the VM and copy the project in

```sh
multipass launch 24.04 --name jobfinder-vps --cpus 2 --memory 2G --disk 10G

# from your project directory (Linux or WSL shell):
tar czf /tmp/jobfinder.tgz --exclude=.venv --exclude=var --exclude=.env --exclude=.git .
multipass transfer /tmp/jobfinder.tgz jobfinder-vps:
multipass shell jobfinder-vps
```

Inside the VM:

```sh
mkdir jobfinder && tar xzf jobfinder.tgz -C jobfinder && cd jobfinder
sudo deploy/install.sh
```

The first install ends with "self-check failed: edit /etc/jobfinder/jobfinder.env…".
That is expected, because no keys are set yet.

### 3. Configure it like the real server

Follow the [First-time setup](../README.md#first-time-setup) section of the README inside
the VM: `sudoedit /etc/jobfinder/jobfinder.env`, the YAML files, `profile import` (copy
your CV in with `multipass transfer`), `selfcheck`, `notify --test`, and enabling the
timers.

### 4. Check the server behaviour

```sh
sudo systemctl start jobfinder.service           # one run now, exactly as the timer does it
journalctl -u jobfinder.service -n 50 -o cat     # JSON log lines
systemctl list-timers 'jobfinder*'               # next hourly run and the 03:30 backup
sudo jobfinder status

sudo systemctl start jobfinder-alert.service     # failure alert path: you get a Telegram message
sudo systemctl start jobfinder-backup.service    # backup now
ls -l /var/backups/jobfinder/

sudo /opt/jobfinder/current/deploy/restore.sh /var/backups/jobfinder/jobfinder-*.tar.gz
sudo jobfinder status                            # still works after the restore

sudo deploy/install.sh                           # "update": builds and switches to a new release
sudo deploy/install.sh --rollback                # back to the previous one
ls /opt/jobfinder/releases/

sudo reboot                                      # then: systemctl list-timers 'jobfinder*'
```

Leave the VM running for a few hours and check that the hourly runs happen, using
`journalctl -u jobfinder.service --since today`.

### 5. Clean up

```sh
multipass delete --purge jobfinder-vps
```

If anything in Part B fails, keep the VM and send me the output of
`journalctl -u jobfinder.service -n 100` (or the failing `install.sh` step).
