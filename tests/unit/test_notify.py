from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import respx

from jobfinder.models import Decision, RemoteType, RunStats
from jobfinder.notify.healthcheck import ping
from jobfinder.notify.messages import alerts_message, failure_message, job_message, run_summary_line
from jobfinder.notify.telegram import NotifyError, TelegramNotifier
from tests.helpers import FakeSleep
from tests.unit.test_repositories import make

TOKEN = "123456:ABC-secret-token"
SEND = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
T0 = datetime(2026, 10, 14, 12, tzinfo=UTC)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_send_payload_and_spacing(client: httpx.AsyncClient) -> None:
    route = respx.post(SEND).respond(json={"ok": True, "result": {}})
    sleep = FakeSleep()
    notifier = TelegramNotifier(client, TOKEN, "42", sleep=sleep)
    await notifier.send("<b>hi</b>")
    await notifier.send("again")
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "chat_id": "42",
        "text": "<b>hi</b>",
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    assert sleep.calls == [1.1]  # about one message per second per chat


@respx.mock
async def test_429_retry_after(client: httpx.AsyncClient) -> None:
    respx.post(SEND).side_effect = [
        httpx.Response(
            429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 7}}
        ),
        httpx.Response(200, json={"ok": True}),
    ]
    sleep = FakeSleep()
    await TelegramNotifier(client, TOKEN, "42", sleep=sleep).send("x")
    assert sleep.calls == [7.0]


@respx.mock
async def test_errors_never_reveal_the_token(client: httpx.AsyncClient) -> None:
    respx.post(SEND).respond(
        400, json={"ok": False, "description": f"Bad Request: chat not found for bot{TOKEN}"}
    )
    with pytest.raises(NotifyError) as info:
        await TelegramNotifier(client, TOKEN, "42", sleep=FakeSleep()).send("x")
    assert TOKEN not in str(info.value)
    assert "chat not found" in str(info.value)
    assert respx.calls.call_count == 1  # 4xx is not retried


@respx.mock
async def test_network_errors_retried_then_reported(client: httpx.AsyncClient) -> None:
    respx.post(SEND).mock(side_effect=httpx.ConnectError(f"cannot reach {SEND}"))
    with pytest.raises(NotifyError) as info:
        await TelegramNotifier(client, TOKEN, "42", sleep=FakeSleep()).send("x")
    assert respx.calls.call_count == 3
    assert TOKEN not in str(info.value)


def test_job_message_escapes_and_credits_source() -> None:
    opportunity = make(
        source="remotive",
        title="Dev <script> & Co",
        company="A&B",
        posting_url="https://remotive.com/remote-jobs/dev-1?a=1&b=2",
        apply_url=None,
        relevance_score=88,
        decision=Decision.STRONG,
        remote_type=RemoteType.REMOTE,
        summary="Builds <things>",
        matched_skills=["Python"],
        red_flags=["Vague duties"],
    )
    text = job_message(opportunity)
    assert "<b>88 · Dev &lt;script&gt; &amp; Co</b>" in text
    assert "A&amp;B" in text
    assert (
        '<a href="https://remotive.com/remote-jobs/dev-1?a=1&amp;b=2">Listing on Remotive</a>'
        in text
    )
    assert "Apply" not in text  # no separate apply link for this source
    assert "Watch out: Vague duties" in text


def test_job_message_prefers_resolved_apply_link() -> None:
    text = job_message(make(apply_url_final="https://acme.com/apply/1", relevance_score=81))
    assert text.index('href="https://acme.com/apply/1">Apply') < text.index("Listing on Lever")


def test_other_messages() -> None:
    assert alerts_message(["a <b>"]) == "<b>jobfinder: attention needed</b>\n• a &lt;b&gt;"
    assert "journalctl -u jobfinder.service" in failure_message("vps", None)
    line = run_summary_line(
        RunStats(run_id="r", started_at=T0, status="partial", new=3, errors=["x"])
    )
    assert line.startswith("partial, 3 new") and "errors: x" in line


@respx.mock
async def test_healthcheck_ping(client: httpx.AsyncClient) -> None:
    respx.get("https://hc-ping.com/uuid").respond(200)
    respx.get("https://hc-ping.com/bad").respond(404)
    respx.get("https://hc-ping.com/down").mock(side_effect=httpx.ConnectTimeout("x"))
    assert await ping(client, "https://hc-ping.com/uuid")
    assert not await ping(client, "https://hc-ping.com/bad")
    assert not await ping(client, "https://hc-ping.com/down")
