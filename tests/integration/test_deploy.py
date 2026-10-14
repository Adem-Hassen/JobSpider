"""Deployment files: unit directives, and the backup/restore scripts run for real."""

from __future__ import annotations

import configparser
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobfinder.models import RunStats
from jobfinder.storage.excel_repo import ExcelRepository
from jobfinder.storage.state_store import StateStore
from tests.unit.test_repositories import make

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
T0 = datetime(2026, 10, 14, 12, tzinfo=UTC)


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=True, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read(DEPLOY / name)
    return parser


def test_run_service() -> None:
    service = unit("jobfinder.service")
    s, u = service["Service"], service["Unit"]
    assert s["Type"] == "oneshot"
    assert (s["User"], s["Group"]) == ("jobfinder", "jobfinder")
    assert s["EnvironmentFile"] == "/etc/jobfinder/jobfinder.env"
    assert s["ExecStart"] == "/opt/jobfinder/current/.venv/bin/jobfinder run"
    assert s["TimeoutStartSec"] == "15min"  # the effective limit for Type=oneshot
    assert s["MemoryMax"] == "512M"
    for key, value in {
        "NoNewPrivileges": "yes",
        "ProtectSystem": "strict",
        "ProtectHome": "yes",
        "PrivateTmp": "yes",
        "ReadWritePaths": "/var/lib/jobfinder",
    }.items():
        assert s[key] == value, key
    assert u["OnFailure"] == "jobfinder-alert.service"
    assert "network-online.target" in u["After"]


def test_timers() -> None:
    hourly = unit("jobfinder.timer")["Timer"]
    assert (hourly["OnCalendar"], hourly["RandomizedDelaySec"], hourly["Persistent"]) == (
        "hourly",
        "300",
        "true",
    )
    daily = unit("jobfinder-backup.timer")["Timer"]
    assert daily["OnCalendar"].startswith("*-*-* ") and daily["Persistent"] == "true"
    assert unit("jobfinder.timer")["Install"]["WantedBy"] == "timers.target"


def test_alert_and_backup_services() -> None:
    alert = unit("jobfinder-alert.service")["Service"]
    assert alert["ExecStart"].endswith("jobfinder notify --failure")
    backup = unit("jobfinder-backup.service")["Service"]
    assert backup["ExecStart"] == "/opt/jobfinder/current/deploy/backup.sh"
    assert "/var/backups/jobfinder" in backup["ReadWritePaths"]
    assert "BACKUP_KEEP=14" in backup["Environment"]


def test_install_script_matches_the_files() -> None:
    script = (DEPLOY / "install.sh").read_text()
    listed = set(re.search(r"UNITS=\(([^)]*)\)", script, re.S).group(1).split())  # type: ignore[union-attr]
    on_disk = {p.name for p in DEPLOY.iterdir() if p.suffix in (".service", ".timer")}
    assert listed == on_disk
    assert 'chmod 600 "$ENV_FILE"' in script
    assert "--frozen" in script  # the lockfile is honoured
    for path in ("/var/lib/jobfinder", "/var/backups/jobfinder", "/etc/jobfinder"):
        assert path in script
    for name in ("install.sh", "backup.sh", "restore.sh"):
        assert os.access(DEPLOY / name, os.X_OK), name


# --------------------------------------------------------------------------- backup


@pytest.fixture
def data(tmp_path: Path) -> dict[str, Path]:
    data_dir, config_dir, backup_dir = tmp_path / "data", tmp_path / "etc", tmp_path / "backups"
    data_dir.mkdir()
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text("max_age_days: 30\n")
    with StateStore(data_dir / "state.db") as store:
        store.start_run("r1", T0)
        store.finish_run(
            "r1", T0, "success", RunStats(run_id="r1", started_at=T0).model_dump_json()
        )
    repo = ExcelRepository(data_dir / "opportunities.xlsx")
    repo.upsert(make())
    repo.flush()
    (data_dir / "profiles").mkdir()
    (data_dir / "profiles" / "profile_v1.json").write_text("{}")
    (data_dir / "cv").mkdir()
    (data_dir / "cv" / "cv_abc.pdf").write_bytes(b"%PDF")
    return {"data": data_dir, "config": config_dir, "backups": backup_dir}


def run_script(name: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, "PYTHON": sys.executable, **env}
    return subprocess.run(  # noqa: S603
        [str(DEPLOY / name), *args], env=full_env, capture_output=True, text=True, check=False
    )


def backup(paths: dict[str, Path], keep: int = 14) -> subprocess.CompletedProcess[str]:
    return run_script(
        "backup.sh",
        {
            "DATA_DIR": str(paths["data"]),
            "BACKUP_DIR": str(paths["backups"]),
            "JOBFINDER_CONFIG_DIR": str(paths["config"]),
            "BACKUP_KEEP": str(keep),
        },
    )


def test_backup_contents(data: dict[str, Path]) -> None:
    result = backup(data)
    assert result.returncode == 0, result.stderr
    (archive,) = data["backups"].glob("jobfinder-*.tar.gz")
    assert archive.stat().st_mode & 0o077 == 0
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
        tar.extractall(data["backups"] / "x", filter="data")
    for member in (
        "jobfinder/state.db",
        "jobfinder/opportunities.xlsx",
        "jobfinder/MANIFEST",
        "jobfinder/profiles/profile_v1.json",
        "jobfinder/cv/cv_abc.pdf",
        "jobfinder/config/settings.yaml",
    ):
        assert member in names, member
    copy = sqlite3.connect(data["backups"] / "x" / "jobfinder" / "state.db")
    assert copy.execute("select run_id from runs").fetchall() == [("r1",)]
    assert not list(data["backups"].glob(".work.*"))


def test_backup_keeps_the_newest(data: dict[str, Path]) -> None:
    data["backups"].mkdir()
    for day in range(1, 17):
        (data["backups"] / f"jobfinder-202001{day:02d}T000000Z.tar.gz").write_bytes(b"old")
    assert backup(data, keep=14).returncode == 0
    remaining = sorted(p.name for p in data["backups"].glob("jobfinder-*.tar.gz"))
    assert len(remaining) == 14
    assert remaining[0] == "jobfinder-20200104T000000Z.tar.gz"
    assert not remaining[-1].startswith("jobfinder-2020")  # today's archive survived


def test_restore_round_trip(data: dict[str, Path]) -> None:
    assert backup(data).returncode == 0
    (archive,) = data["backups"].glob("jobfinder-*.tar.gz")
    # Things change after the backup...
    repo = ExcelRepository(data["data"] / "opportunities.xlsx")
    repo.upsert(make("01J0000000000000000000000B", source_job_id="2"))
    repo.flush()
    (data["data"] / "state.db-wal").write_bytes(b"stale")
    env = {"DATA_DIR": str(data["data"]), "CONFIG_DIR": str(data["config"]), "SKIP_SYSTEMCTL": "1"}
    result = run_script("restore.sh", env, str(archive))
    assert result.returncode == 0, result.stderr
    assert ExcelRepository(data["data"] / "opportunities.xlsx", read_only=True).count() == 1
    assert not (data["data"] / "state.db-wal").exists()
    (aside,) = data["data"].glob("pre-restore-*")
    assert ExcelRepository(aside / "opportunities.xlsx", read_only=True).count() == 2
    assert (aside / "config-from-backup" / "settings.yaml").exists()
    with StateStore(data["data"] / "state.db") as store:
        assert store.recent_runs(1)[0].run_id == "r1"


def test_restore_rejects_bad_archives(data: dict[str, Path], tmp_path: Path) -> None:
    env = {"DATA_DIR": str(data["data"]), "CONFIG_DIR": str(data["config"]), "SKIP_SYSTEMCTL": "1"}
    other = tmp_path / "other.tar.gz"
    with tarfile.open(other, "w:gz") as tar:
        tar.add(data["config"], arcname="something")
    assert run_script("restore.sh", env, str(other)).returncode == 1
    assert backup(data).returncode == 0
    (archive,) = data["backups"].glob("jobfinder-*.tar.gz")
    tampered = tmp_path / "tampered"
    with tarfile.open(archive) as tar:
        tar.extractall(tampered, filter="data")
    (tampered / "jobfinder" / "cv" / "cv_abc.pdf").write_bytes(b"changed")
    bad = tmp_path / "jobfinder-bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        tar.add(tampered / "jobfinder", arcname="jobfinder")
    result = run_script("restore.sh", env, str(bad))
    assert result.returncode == 1
    assert "checksum mismatch" in result.stderr
    assert (data["data"] / "opportunities.xlsx").exists()  # nothing was touched
