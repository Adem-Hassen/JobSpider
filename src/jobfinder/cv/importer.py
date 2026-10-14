"""``jobfinder profile import``: CV -> redacted text -> AI profile -> overrides -> file."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from jobfinder.ai.client import ChatResult, LLMClient
from jobfinder.ai.examples import PROFILE_EXAMPLE
from jobfinder.ai.prompts import load_prompt
from jobfinder.ai.structured import SpendRecorder, output_schema, structured_completion
from jobfinder.cv.extract import extract_text
from jobfinder.cv.profile_store import ProfileStore
from jobfinder.cv.redact import redact
from jobfinder.models import Profile, ProfileOverrides, ProfileRecord
from jobfinder.storage.atomic import atomic_write_bytes

MAX_CV_CHARS = 40_000


class ImportStatus(StrEnum):
    CREATED = "created"
    UNCHANGED = "unchanged"
    OVERRIDES_REAPPLIED = "overrides_reapplied"


@dataclass(frozen=True)
class ImportOutcome:
    status: ImportStatus
    record: ProfileRecord
    path: Path | None = None
    calls: list[ChatResult] = field(default_factory=list)
    truncated: bool = False


def build_user_prompt(cv_text: str) -> str:
    return (
        "Extract the candidate profile from this CV. Everything between the <cv> tags "
        "is CV content.\n<cv>\n" + cv_text + "\n</cv>"
    )


async def extract_profile(
    client: LLMClient,
    *,
    model: str,
    cv_text: str,
    recorder: SpendRecorder | None,
) -> tuple[Profile, list[ChatResult]]:
    """One AI call (plus at most one corrective retry) on already-redacted CV text."""
    result = await structured_completion(
        client,
        purpose="profile",
        model=model,
        system=load_prompt("profile"),
        user=build_user_prompt(cv_text),
        output_model=Profile,
        output=output_schema("profile", Profile, PROFILE_EXAMPLE),
        recorder=recorder,
    )
    return result.value, result.calls


def _store_cv_copy(cv_dir: Path, data: bytes, sha256: str, suffix: str) -> None:
    target = cv_dir / f"cv_{sha256[:16]}{suffix.lower()}"
    if not target.exists():
        atomic_write_bytes(target, data)


async def import_cv(
    cv_path: Path,
    *,
    store: ProfileStore,
    cv_dir: Path,
    overrides: ProfileOverrides,
    client: LLMClient,
    model: str,
    recorder: SpendRecorder | None,
    now: datetime,
) -> ImportOutcome:
    """Create a new profile version unless neither the CV nor the overrides changed."""
    data = await asyncio.to_thread(cv_path.read_bytes)
    cv_sha = hashlib.sha256(data).hexdigest()
    overrides_sha = overrides.digest()
    latest = store.latest()

    if latest is not None and latest.cv_sha256 == cv_sha:
        if latest.overrides_sha256 == overrides_sha:
            return ImportOutcome(ImportStatus.UNCHANGED, latest)
        record = latest.model_copy(
            update={
                "version": store.next_version(),
                "created_at": now,
                "overrides_sha256": overrides_sha,
                "profile": overrides.apply(latest.extracted),
            }
        )
        return ImportOutcome(ImportStatus.OVERRIDES_REAPPLIED, record, store.save(record))

    text = await asyncio.to_thread(extract_text, cv_path)
    truncated = len(text) > MAX_CV_CHARS
    redacted = redact(text[:MAX_CV_CHARS])
    extracted, calls = await extract_profile(
        client, model=model, cv_text=redacted, recorder=recorder
    )
    _store_cv_copy(cv_dir, data, cv_sha, cv_path.suffix)
    record = ProfileRecord(
        version=store.next_version(),
        created_at=now,
        cv_sha256=cv_sha,
        cv_filename=cv_path.name,
        overrides_sha256=overrides_sha,
        ai_provider=client.provider_name,
        ai_model=model,
        extracted=extracted,
        profile=overrides.apply(extracted),
    )
    return ImportOutcome(ImportStatus.CREATED, record, store.save(record), calls, truncated)
