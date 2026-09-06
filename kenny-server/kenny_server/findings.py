"""Findings: age stamping and ranking over already-evaluated health (ADR-0058).

Pure and I/O-free, like ``health_rules`` and ``fleet_stats``: the caller reads
``alert_state`` rows from the store and hands them in. ``alert_state.since`` is
"since the *current* status" -- the alert loop rewrites it on every transition,
including ``crit -> warn`` -- which is the age a reader wants next to a
finding ("warn since 3 days"), not the start of the whole degraded episode.

Ages exist only while the alert loop runs (``KENNY_ALERT_INTERVAL_SECS`` > 0):
nothing else writes ``alert_state``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .health_rules import parse_ts

__all__ = ["stamp_age", "rank_today_items", "posture_sections"]


def stamp_age(
    health: dict[str, Any],
    state_rows: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Set ``since`` and ``age_seconds`` on every non-ok section of ``health``
    (in place, and returned) from the ``section:<name>`` rows of
    ``state_rows``. A section whose recorded status differs from its current
    one -- the loop has not caught up yet -- gets ``since = None`` rather than
    a stale age."""

    now = now or datetime.now(timezone.utc)
    for name, section in (health.get("sections") or {}).items():
        if section.get("status") == "ok":
            continue
        row = state_rows.get(f"section:{name}")
        since = parse_ts(row.get("since")) if row and row.get("status") == section.get("status") else None
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        section["since"] = since.isoformat() if since else None
        section["age_seconds"] = int((now - since).total_seconds()) if since else None
    return health


def posture_sections(health: dict[str, Any]) -> list[str]:
    """Names of the sections in the ``posture`` tier, in section order."""

    return [n for n, s in (health.get("sections") or {}).items() if s.get("tier") == "posture"]


_SEVERITY_RANK = {"crit": 0, "warn": 1}


def rank_today_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order Today's section items: crit before warn, and within a severity the
    newest finding first (an unknown age counts as newest -- it is what the
    loop has not seen yet). Posture items never belong here; the caller counts
    them instead."""

    def _key(item: dict[str, Any]) -> tuple[int, int, str]:
        age = item.get("age_seconds")
        return (
            _SEVERITY_RANK.get(item.get("severity", ""), 9),
            age if isinstance(age, int) else -1,
            str(item.get("host") or ""),
        )

    return sorted((i for i in items if i.get("severity") in _SEVERITY_RANK), key=_key)
