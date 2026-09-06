"""Age stamping and Today ranking over evaluated health (ADR-0058)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kenny_server import findings, health_rules

NOW = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)


def _health() -> dict:
    snapshot = {
        "disk": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 96}]},
        "encryption": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "protection_status": 0}]},
        "memory": {"status": "ok", "summary": "", "percent_used": 20},
    }
    return health_rules.evaluate_snapshot(snapshot, now=NOW)


def test_stamp_age_uses_the_row_for_the_current_status_only() -> None:
    health = _health()
    rows = {
        "section:disk": {"status": "crit", "since": (NOW - timedelta(days=3)).isoformat(), "last_notified_at": None},
        # The loop still holds the old status for encryption: no age rather than a stale one.
        "section:encryption": {"status": "warn", "since": (NOW - timedelta(days=30)).isoformat(), "last_notified_at": None},
    }
    findings.stamp_age(health, rows, now=NOW)
    disk = health["sections"]["disk"]
    assert disk["since"] == (NOW - timedelta(days=3)).isoformat()
    assert disk["age_seconds"] == 3 * 86_400
    assert health["sections"]["encryption"] == {**health["sections"]["encryption"], "since": None, "age_seconds": None}
    # ok sections carry no age keys at all.
    assert "since" not in health["sections"]["memory"]


def test_stamp_age_without_rows_leaves_findings_unaged() -> None:
    health = findings.stamp_age(_health(), {}, now=NOW)
    assert health["sections"]["disk"]["since"] is None
    assert health["sections"]["disk"]["age_seconds"] is None


def test_posture_sections_lists_the_posture_tier() -> None:
    assert findings.posture_sections(_health()) == ["encryption"]
    assert findings.posture_sections({"sections": {}}) == []


def test_rank_today_items_crit_first_then_newest_and_drops_posture() -> None:
    items = [
        {"severity": "warn", "host": "b", "age_seconds": 100},
        {"severity": "crit", "host": "old", "age_seconds": 5000},
        {"severity": "posture", "host": "p", "age_seconds": 1},
        {"severity": "crit", "host": "new", "age_seconds": 60},
        {"severity": "crit", "host": "unseen", "age_seconds": None},
        {"severity": "warn", "host": "a", "age_seconds": 100},
    ]
    ranked = findings.rank_today_items(items)
    assert [i["host"] for i in ranked] == ["unseen", "new", "old", "a", "b"]


# -- the MCP surface carries the same tiers and ages (ADR-0058) --------------


async def test_mcp_fleet_overview_and_agent_health_carry_posture_and_age(tmp_path) -> None:
    """Joined across the telemetry store, alert_state and the MCP tools: a
    posture-only host is `ok` with `posture_sections`, and `agent_health`
    reports the section's tier and the age the alert loop recorded."""

    from fastmcp import Client, FastMCP

    from kenny_server.registry import AgentRegistry
    from kenny_server.store import AlertStateStore, TelemetryStore
    from kenny_server.tools import CallLog, register_tools
    from kenny_server.tunnel import AgentTunnel

    db = str(tmp_path / "mcp.sqlite")
    store, state = TelemetryStore(db), AlertStateStore(db)
    await store.connect()
    await state.connect()
    try:
        snapshot = {
            "encryption": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "protection_status": 0}]},
            "disk": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 10}]},
        }
        await store.insert("pc1", NOW.isoformat(), snapshot)
        await state.upsert("pc1", "section:encryption", status="posture",
                           since=(NOW - timedelta(days=12)).isoformat(), last_notified_at=None)
        registry = AgentRegistry()
        mcp = FastMCP("test")
        register_tools(
            mcp, registry=registry, store=store,
            tunnel=AgentTunnel(registry, store, event_store=None),
            call_log=CallLog(event_store=None), alert_state=state,
        )
        async with Client(mcp) as client:
            fleet = (await client.call_tool("fleet_overview", {})).data
            assert fleet["overall"] == "ok"
            pc1 = fleet["agents"][0]
            assert pc1["flagged_sections"] == []
            assert pc1["posture_sections"] == ["encryption"]
            health = (await client.call_tool("agent_health", {"id": "pc1"})).data
            enc = health["sections"]["encryption"]
            assert enc["status"] == "posture" and enc["tier"] == "posture"
            assert enc["since"] == (NOW - timedelta(days=12)).isoformat()
            assert enc["age_seconds"] >= 12 * 86_400 - 60
            assert health["sections"]["disk"]["tier"] == "none"
    finally:
        await store.close()
        await state.close()
