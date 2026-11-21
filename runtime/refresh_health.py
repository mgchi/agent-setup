"""Fact-free refresh receipts shared by the publisher and session entry."""

import datetime as dt
from typing import Literal, get_args

from pydantic import BaseModel, Field

SourceName = Literal["github", "granola", "linear", "notion", "slack"]
SOURCE_NAMES = frozenset(get_args(SourceName))
Outcome = Literal["ok", "empty", "unavailable", "needs-sign-in", "truncated", "failed", "unknown", "disabled"]
Reason = Literal["source", "agent", "validation", "publication", "setup", "timeout", "unknown"]
StageIssue = Literal["manifest-missing", "manifest-format", "manifest-coverage", "unexpected-path", "pinned-sources",
                     "core-file-missing", "source-note-missing", "size-limit", "unsafe-content", "broken-reference"]
STAGE_MESSAGES = {
    "manifest-missing": "Staged output is missing source-status.json.",
    "manifest-format": "Source manifest is malformed. Check the required JSON fields.",
    "manifest-coverage": "Source manifest must cover this run and exactly the requested sources.",
    "unexpected-path": "Staged output contains an unexpected path or symlinks. Check the permitted output paths.",
    "pinned-sources": "Pinned source definitions changed. Preserve the text before the Discovered section.",
    "core-file-missing": "Staged context is missing a core digest file.",
    "source-note-missing": "A successful source is missing its required evidence note.",
    "size-limit": "Staged output exceeds a size limit. Consolidate the digest.",
    "unsafe-content": "Staged output failed the content checks. Review the staged output contract.",
    "broken-reference": "Broken digest reference. Check links in generated notes.",
}
ACTIONS = {
    "needs-sign-in": "sign in to the connector and retry",
    "unavailable": "check the connector configuration and retry",
    "truncated": "check the source result limit and retry",
    "failed": "check the source and retry",
}


class SourceHealth(BaseModel):
    status: Outcome = "unknown"
    last_success: float = Field(default=0, ge=0, le=253402300799)
    through: float = Field(default=0, ge=0, le=253402300799)
    since: float = Field(default=0, ge=0, le=253402300799)


class RefreshState(BaseModel):
    run_id: str
    status: Literal["succeeded", "partial", "failed"]
    completed: float
    started: float = 0
    since: float
    mode: str
    detail: str = ""  # Older receipts may contain detail. Never copy this to a session.
    reason: Reason = "unknown"
    issue: StageIssue | None = None
    sources: dict[SourceName, SourceHealth] = Field(default_factory=dict)


class RefreshHealth(BaseModel):
    status: Literal["never-run", "succeeded", "partial", "failed", "unreadable"]
    last_attempt: float = 0
    last_success: float = 0
    age_hours: float | None = None
    reason: Reason = "unknown"
    issue: StageIssue | None = None
    sources: dict[SourceName, SourceHealth] = Field(default_factory=dict)
    warning: str = ""


def summarize(latest: RefreshState | None, success: RefreshState | None, now: float, stale_hours: float,
              *, enabled: set[SourceName] | None = None) -> RefreshHealth:
    last = success.completed if success else 0
    attempt = latest or success
    health = RefreshHealth(status=attempt.status if attempt else "never-run", last_success=last,
                           last_attempt=(attempt.started or attempt.completed) if attempt else 0,
                           age_hours=round(max(0, now - last) / 3600, 2) if last else None)
    if attempt:
        health.reason = attempt.reason
        health.issue = attempt.issue
    notices = []
    for name in sorted(SOURCE_NAMES):
        source = (attempt.sources if attempt else {}).get(name, SourceHealth())
        if success and not source.last_success and name not in (attempt.sources if attempt else {}):
            source = success.sources.get(name, SourceHealth(last_success=last))
        if enabled is not None and name not in enabled:
            source = source.model_copy(update={"status": "disabled"})
        health.sources[name] = source
        if source.status in ACTIONS:
            date = dt.datetime.fromtimestamp(source.last_success, dt.timezone.utc).date() if source.last_success else "unknown"
            notices.append(f"{name} {source.status}, last verified {date}, {ACTIONS[source.status]}")
        elif attempt and source.status in {"ok", "empty"} and source.through and source.through < attempt.started - 1:
            date = dt.datetime.fromtimestamp(source.through, dt.timezone.utc).date()
            notices.append(f"{name} catching up, covered through {date}")
    if enabled == set():
        health.warning = ""
        return health
    if health.status == "never-run":
        notices.append("no recorded attempt; check the refresh schedule")
    elif health.status == "failed" and (health.reason != "source" or not notices):
        notices.append(STAGE_MESSAGES[health.issue].rstrip(".") if health.issue
                       else f"last attempt failed ({health.reason}); check the refresh job")
    elif health.status == "partial":
        date = dt.datetime.fromtimestamp(last, dt.timezone.utc).date() if last else "unknown"
        notices.append(f"read the dated source updates; last full digest {date}")
    if last and now - last > stale_hours * 3600:
        notices.append(f"last full success {health.age_hours:g} hours ago")
    if notices:
        health.warning = "External refresh: " + "; ".join(notices) + "."
    return health
