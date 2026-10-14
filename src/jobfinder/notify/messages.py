"""Telegram message texts (HTML parse mode)."""

from __future__ import annotations

import html

from jobfinder.models import Opportunity, RunStats

SOURCE_NAMES = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "remotive": "Remotive",
    "remoteok": "Remote OK",
    "jobicy": "Jobicy",
    "himalayas": "Himalayas",
    "arbeitnow": "Arbeitnow",
    "adzuna": "Adzuna",
    "france_travail": "France Travail",
    "email_alerts": "e-mail alert",
}


def _e(text: str) -> str:
    return html.escape(text, quote=False)


def _href(url: str) -> str:
    return html.escape(url, quote=True)


def job_message(opportunity: Opportunity) -> str:
    """One new job. The listing link and its source are always included, as the
    aggregators' terms require attribution and a link back."""
    o = opportunity
    place = ", ".join(p for p in (o.location_city, o.location_country) if p) or "location n/a"
    facts = [place, o.remote_type.value]
    if o.employment_type:
        facts.append(o.employment_type.value.replace("_", " "))
    lines = [
        f"<b>{o.relevance_score} · {_e(o.title)}</b>",
        f"{_e(o.company)} · {_e(' · '.join(facts))}",
    ]
    if o.summary:
        lines.append(_e(o.summary))
    if o.matched_skills:
        lines.append(f"Matches: {_e(', '.join(o.matched_skills[:8]))}")
    if o.missing_skills:
        lines.append(f"Missing: {_e(', '.join(o.missing_skills[:6]))}")
    if o.red_flags:
        lines.append(f"Watch out: {_e('; '.join(o.red_flags[:3]))}")
    if o.deadline_at:
        lines.append(f"Deadline: {o.deadline_at:%Y-%m-%d}")
    source = SOURCE_NAMES.get(o.source, o.source)
    links = [f'<a href="{_href(o.posting_url)}">Listing on {_e(source)}</a>']
    apply = o.apply_url_final or o.apply_url
    if apply and apply != o.posting_url:
        links.insert(0, f'<a href="{_href(apply)}">Apply</a>')
    lines.append(" · ".join(links))
    return "\n".join(lines)


def more_jobs_message(count: int) -> str:
    return f"…and {count} more matching job(s); they follow on the next run."


def alerts_message(alerts: list[str]) -> str:
    return "<b>jobfinder: attention needed</b>\n" + "\n".join(f"• {_e(a)}" for a in alerts)


def failure_message(host: str, stats_line: str | None) -> str:
    text = f"<b>jobfinder run failed</b> on {_e(host)}."
    if stats_line:
        text += f"\nLast run: {_e(stats_line)}"
    return text + "\nSee: <code>journalctl -u jobfinder.service -n 100</code>"


def run_summary_line(stats: RunStats) -> str:
    return (
        f"{stats.status}, {stats.new} new, {stats.scored} scored, "
        f"${stats.ai_cost_usd:.4f}, {stats.duration_s:.0f}s"
        + (f"; errors: {'; '.join(stats.errors)[:300]}" if stats.errors else "")
    )
