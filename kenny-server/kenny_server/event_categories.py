"""Server-side LLM categorization of reliability events (ADR-0026).

The agent reports raw Windows event groups (``source`` + ``event_id`` + a sample
message). To draw the reliability heatmaps — and to *score* health — the server
needs a **friendly category**, a **severity**, and a short
**suspected cause** per group. The space of Windows event sources is large and
open-ended, so instead of a hand-maintained table we ask the connected LLM (the
same Haiku model + API key the AI Recommendation uses, see ``recommend.py``).

Two things keep this cheap and safe, mirroring ``recommend.py``:

* a frozen, cacheable **system prompt** (Anthropic prompt caching), and a bounded
  in-memory **result cache** keyed by ``(source, event_id)`` — the set of distinct
  event types on a real fleet is small and stable, so after warm-up this is a
  no-op;
* the Anthropic client is **injected** (``categorize_events(client, groups)``) so
  tests pass a fake and no real key is required, and every result is validated
  against fixed enums (unknown / no key / API error -> ``category="Other"``,
  ``severity="unknown"``), so categorization degrades gracefully and never
  becomes a hard dependency — and "unknown" is scored as at least notable
  rather than silently trusted as benign.

A third thing keeps a cold cache or a slow/broken API from turning into an
unbounded dashboard read: classification never blocks a read beyond a bounded
**wait** (see ``categorize_events``' ``wait`` parameter). A batch that hasn't
finished when the wait elapses is not abandoned — the caller gets the safe
defaults for this response, but the batch keeps running in the background
(``asyncio.shield``) and lands in the cache for the next read. A batch that
fails outright backs off for a short cooldown so a persistently broken API
doesn't get re-hit on every single request. Concurrent callers racing over the
same uncached keys (e.g. ``api_fleet_overview`` and ``api_agent``) ride along
on one in-flight batch instead of firing duplicate calls.

Since ADR-0058 the cache is **durable**: every verdict is written through to
:class:`kenny_server.store.EventClassificationStore` and loaded back at boot
(``bind_store`` / ``load_persisted``), and :func:`mark` stamps verdicts from
the cache alone -- synchronously, no LLM, no key -- so it can sit on the
``TelemetryStore.annotate`` seam next to suppression and reach *every* health
consumer (alerting, the digest, the fleet list, MCP), not just the two
dashboard reads. Cache misses are filled by :func:`schedule_classification`,
kicked fire-and-forget at telemetry insert; ingestion never waits for it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Any, Callable

from .recommend import ai_available  # re-exported for callers

__all__ = [
    "CATEGORIES",
    "FALLBACK",
    "SEVERITIES",
    "SEVERITY_FALLBACK",
    "ai_available",
    "categorize_events",
    "annotate_events",
    "annotate_snapshots",
    "bind_store",
    "default_client",
    "load_persisted",
    "mark",
    "reset_state",
    "schedule_classification",
]

logger = logging.getLogger("kenny.event_categories")

CATEGORIZE_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 512
# Bounds the in-memory mirror of the persisted table. A real fleet has a few
# hundred distinct (source, event_id) patterns at most.
_CACHE_MAX = 4096
# How long a read may block on classification before falling back to the safe
# defaults for this response (the batch keeps running in the background — see
# module docstring). 1.5s is comfortably above a warm-cache no-op (0ms) and a
# typical Haiku batch, while still keeping a dashboard tab switch feeling fast
# on a cold cache.
_CLASSIFY_WAIT_SECONDS = 1.5
# After a batch fails outright (bad response / API error), don't retry any of
# its keys for this long — a broken or rate-limited API would otherwise be
# re-hit by every single dashboard read.
_FAILURE_BACKOFF_SECONDS = 60.0

# Fixed friendly categories — stable heatmap rows/colours, and the only values the
# model may return (anything else is coerced to FALLBACK). Order is display order.
CATEGORIES: list[str] = [
    "Disk & storage",
    "App crash / hang",
    "Bluescreen / bugcheck",
    "Driver & hardware",
    "Power & boot",
    "Windows service",
    "Windows Update",
    "Network",
    "Security",
    "Other",
]
_CATEGORY_SET = set(CATEGORIES)
FALLBACK = "Other"

# Severity of a *recurring pattern*, independent of how many times it repeats —
# this is what lets the health rule tell "300 benign repeats" apart
# from "300 distinct novel errors". The model may also answer "unknown"
# (:data:`SEVERITY_FALLBACK`) when genuinely unsure; unlike category, an unknown
# severity is deliberately treated as at least notable by the health rule, never
# as a silent pass.
SEVERITIES: list[str] = ["benign", "notable", "serious"]
_SEVERITY_SET = set(SEVERITIES)
SEVERITY_FALLBACK = "unknown"


_SYSTEM_TEXT = (
    "You are kenny's Windows event-log triage assistant. You are given a list of "
    "Windows Event Log error/critical sources (a provider name, an event id, and a "
    "sample message). For EACH one, decide:\n\n"
    "1. \"category\" — exactly one of these fixed categories:\n"
    + "\n".join(f"   - {c}" for c in CATEGORIES)
    + "\n\n2. \"severity\" — how much a family-PC operator should care about this "
    "*specific, recurring* pattern, regardless of how often it repeats:\n"
    "   - \"benign\": a known, cosmetic, or pure-nuisance pattern (e.g. a stale "
    "DCOM permission timeout between two installed apps, a harmless driver "
    "warning) — repeating hundreds of times changes nothing.\n"
    "   - \"notable\": worth a look but not urgent (e.g. an occasional app crash, "
    "a transient network blip).\n"
    "   - \"serious\": real risk of data loss, instability, or hardware failure "
    "(e.g. a disk I/O error, a kernel bugcheck, a security-relevant failure).\n"
    "   - \"unknown\": you genuinely cannot tell from the source/message. Prefer "
    "this over guessing — never call something \"benign\" without real evidence.\n\n"
    "3. \"cause\" — a short (<=12 words) plain-language guess at what's actually "
    "happening, e.g. \"two apps colliding over a stale COM registration\".\n\n"
    "Reply with ONLY a JSON array, one object per input in the same order, each "
    "shaped exactly as {\"category\": ..., \"severity\": ..., \"cause\": ...}. No "
    "prose, no markdown, no extra keys."
)


def _cached_system() -> list[dict[str, Any]]:
    """System prompt as a cacheable block (Anthropic prompt caching)."""

    return [{"type": "text", "text": _SYSTEM_TEXT, "cache_control": {"type": "ephemeral"}}]


# -- result cache ---------------------------------------------------------

# Each cached value is a small ``{"category", "severity", "cause"}`` dict — see
# _parse_classifications / _default_classification for the shape.
Classification = dict[str, str]

_cache: "OrderedDict[tuple[str, int], Classification]" = OrderedDict()

# Keys currently being classified by a background task (so a concurrent caller
# rides along on the same batch instead of starting a duplicate one), and keys
# whose last batch failed, mapped to the monotonic time their backoff expires.
_inflight: dict[tuple[str, int], "asyncio.Task[None]"] = {}
_failed_until: dict[tuple[str, int], float] = {}


def _key(source: Any, event_id: Any) -> tuple[str, int]:
    try:
        eid = int(event_id)
    except (TypeError, ValueError):
        eid = -1
    return (str(source or "").strip(), eid)


def _default_classification() -> Classification:
    return {"category": FALLBACK, "severity": SEVERITY_FALLBACK, "cause": ""}


def _cache_put(key: tuple[str, int], value: Classification) -> None:
    _cache[key] = value
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


# -- persistence (ADR-0058) ------------------------------------------------

# The durable home of the cache. ``None`` (tests, or a caller that never
# bound one) keeps the pre-0058 in-memory-only behaviour.
_store: Any = None


def reset_state() -> None:
    """Forget every cached, in-flight and backing-off classification.

    The cache, the in-flight map and the failure backoff are module state
    shared by every ``build_app`` in one process; a test that leaves a key
    backing off after a deliberately broken client would otherwise keep the
    next test's classification of the same pattern from ever starting.
    """

    _cache.clear()
    _inflight.clear()
    _failed_until.clear()


def bind_store(store: Any) -> None:
    """Attach the :class:`~kenny_server.store.EventClassificationStore` that
    verdicts are written through to and loaded from."""

    global _store
    _store = store


async def load_persisted() -> int:
    """Fill the cache from the bound store (call once at startup, after the
    store is connected). Rows from a different ``CATEGORIZE_MODEL`` are
    dropped first so a model upgrade re-classifies instead of serving stale
    verdicts. Returns the number of classifications loaded."""

    if _store is None:
        return 0
    await _store.delete_model_except(CATEGORIZE_MODEL)
    rows = await _store.list()
    for r in rows:
        _cache_put(
            _key(r.get("source"), r.get("event_id")),
            {
                "category": r["category"] if r.get("category") in _CATEGORY_SET else FALLBACK,
                "severity": (
                    r["severity"] if r.get("severity") in _SEVERITY_SET else SEVERITY_FALLBACK
                ),
                "cause": str(r.get("cause") or ""),
            },
        )
    return len(rows)


async def _persist(items: list[tuple[tuple[str, int], Classification]]) -> None:
    """Best-effort write-through; a store hiccup never fails a classification."""

    if _store is None or not items:
        return
    try:
        await _store.upsert_many(
            [
                {
                    "source": key[0],
                    "event_id": key[1],
                    "category": c["category"],
                    "severity": c["severity"],
                    "cause": c["cause"],
                    "model": CATEGORIZE_MODEL,
                }
                for key, c in items
            ]
        )
    except Exception:  # noqa: BLE001 - persistence is an optimization, not a dependency
        logger.exception("persisting %d event classification(s) failed", len(items))


# -- LLM call -------------------------------------------------------------


def _extract_text(resp: Any) -> str:
    """Concatenate the text blocks of an Anthropic ``messages.create`` response."""

    parts = []
    for block in getattr(resp, "content", None) or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    return "".join(parts)


def _parse_classifications(text: str, expected: int) -> list[Classification] | None:
    """Parse the model's JSON array of ``{category, severity, cause}`` objects.

    Validates array shape (a JSON array of the expected length) and coerces
    every field against its fixed enum — an unrecognized or missing category
    becomes :data:`FALLBACK`, an unrecognized or missing severity becomes
    :data:`SEVERITY_FALLBACK`. Only a completely unparseable / wrong-length
    response returns ``None`` (the caller then falls back to defaults for the
    whole batch); a malformed individual element degrades to safe defaults
    rather than discarding the batch.
    """

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        arr = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(arr, list) or len(arr) != expected:
        return None
    out: list[Classification] = []
    for item in arr:
        cat = item.get("category") if isinstance(item, dict) else None
        sev = item.get("severity") if isinstance(item, dict) else None
        cause = item.get("cause") if isinstance(item, dict) else None
        out.append(
            {
                "category": cat if cat in _CATEGORY_SET else FALLBACK,
                "severity": sev if sev in _SEVERITY_SET else SEVERITY_FALLBACK,
                "cause": cause.strip()[:160] if isinstance(cause, str) else "",
            }
        )
    return out


def _user_message(groups: list[dict[str, Any]]) -> dict[str, Any]:
    lines = []
    for i, g in enumerate(groups, 1):
        sample = str(g.get("sample") or "").replace("\n", " ")[:200]
        lines.append(f"{i}. source={g.get('source')!r} id={g.get('event_id')} sample={sample!r}")
    return {"role": "user", "content": "Inputs:\n" + "\n".join(lines)}


async def _classify(client: Any, groups: list[dict[str, Any]]) -> list[Classification] | None:
    """One batched Haiku call classifying ``groups``; None on any failure."""

    try:
        resp = await asyncio.to_thread(
            client.messages.create,
            model=CATEGORIZE_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_cached_system(),
            messages=[_user_message(groups)],
        )
    except Exception:  # noqa: BLE001 - best-effort; caller falls back to defaults
        return None
    return _parse_classifications(_extract_text(resp), len(groups))


def _start_classify_task(
    client: Any, todo: list[tuple[tuple[str, int], dict[str, Any]]]
) -> "asyncio.Task[None]":
    """Kick off one batched classify call for ``todo`` as a background task.

    Registers every key in ``_inflight`` before the first ``await`` (i.e.
    synchronously, before the caller can yield to another coroutine), so a
    concurrent caller racing over the same keys (``api_fleet_overview`` vs.
    ``api_agent``) sees them as already being classified and waits on this
    same task instead of firing a duplicate batch call.
    """

    keys = [key for key, _ in todo]

    async def _run() -> None:
        try:
            classifications = await _classify(client, [g for _, g in todo])
            if classifications is None:
                until = time.monotonic() + _FAILURE_BACKOFF_SECONDS
                for key in keys:
                    _failed_until[key] = until
                return
            landed: list[tuple[tuple[str, int], Classification]] = []
            for (key, _), classification in zip(todo, classifications):
                _cache_put(key, classification)
                landed.append((key, classification))
            await _persist(landed)
        finally:
            for key in keys:
                if _inflight.get(key) is task:
                    _inflight.pop(key, None)

    task: "asyncio.Task[None]" = asyncio.ensure_future(_run())
    for key in keys:
        _inflight[key] = task
    # Best-effort background work: retrieve any exception so a task abandoned
    # by a timed-out wait_for() never logs "exception was never retrieved".
    # _classify() already swallows API/parse errors internally, so this only
    # guards against a genuine bug in _run() itself.
    task.add_done_callback(lambda t: t.exception())
    return task


async def categorize_events(
    client: Any, groups: list[dict[str, Any]], *, wait: float = _CLASSIFY_WAIT_SECONDS
) -> dict[tuple[str, int], Classification]:
    """Map each ``(source, event_id)`` in ``groups`` to a classification.

    Cached pairs are returned from the cache immediately. Uncached pairs are
    classified in one batched call, but this call blocks on that batch for at
    most ``wait`` seconds (see module docstring) — on timeout, on ``client is
    None`` (no API key), or on any API/parse failure, the uncached pairs
    resolve to the safe default (:func:`_default_classification`:
    ``category="Other"``, ``severity="unknown"``). A timed-out batch is not
    abandoned: it keeps running via ``asyncio.shield`` and populates the cache
    for the next call. A pair already being classified by another concurrent
    call (or cooling down after a recent failure) rides along / defers rather
    than starting a duplicate batch.
    """

    result: dict[tuple[str, int], Classification] = {}
    todo: list[tuple[tuple[str, int], dict[str, Any]]] = []
    uncached: list[tuple[str, int]] = []
    seen_todo: set[tuple[str, int]] = set()
    wait_tasks: "set[asyncio.Task[None]]" = set()
    now = time.monotonic()

    for g in groups:
        key = _key(g.get("source"), g.get("event_id"))
        if key in _cache:
            _cache.move_to_end(key)
            result[key] = _cache[key]
            continue
        uncached.append(key)
        existing = _inflight.get(key)
        if existing is not None:
            wait_tasks.add(existing)
        elif _failed_until.get(key, 0.0) > now:
            pass  # cooling down after a recent failure; resolves to default below
        elif key not in seen_todo:
            seen_todo.add(key)
            todo.append((key, g))

    if todo and client is not None:
        wait_tasks.add(_start_classify_task(client, todo))

    if wait_tasks:
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(*wait_tasks, return_exceptions=True)),
                timeout=wait,
            )
        except asyncio.TimeoutError:
            pass
        for key in uncached:
            if key in _cache:
                result[key] = _cache[key]

    for key in uncached:
        result.setdefault(key, _default_classification())
    return result


def annotate_events(
    events: list[dict[str, Any]], mapping: dict[tuple[str, int], Classification]
) -> None:
    """Stamp ``category``, ``severity``, and ``suspected_cause`` onto each event
    group in place from ``mapping`` (falling back to safe defaults for any group
    not present in ``mapping``)."""

    for e in events:
        if isinstance(e, dict):
            info = mapping.get(_key(e.get("source"), e.get("event_id"))) or _default_classification()
            e["category"] = info["category"]
            e["severity"] = info["severity"]
            e["suspected_cause"] = info["cause"]


def _reliability_events(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    rel = snapshot.get("reliability") if isinstance(snapshot, dict) else None
    if not isinstance(rel, dict):
        return []
    events = rel.get("events")
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def mark(agent_id: str, snapshot: dict[str, Any] | None) -> None:
    """Stamp ``category``/``severity``/``suspected_cause`` onto each reliability
    event group in ``snapshot`` from the cache alone -- synchronous, no LLM, no
    API key -- for the ``TelemetryStore.annotate`` seam (ADR-0058).

    A group whose pattern is not cached yet is left **unstamped** (the health
    rule then treats it as ``unknown``), never stamped with the fallback: the
    fallback would be indistinguishable from a real "unknown" verdict, and a
    later :func:`annotate_snapshots` on the same dict must still see that the
    pattern needs classifying. ``suppressed``/``suppressed_by`` (ADR-0041) are
    never touched.
    """

    for e in _reliability_events(snapshot):
        info = _cache.get(_key(e.get("source"), e.get("event_id")))
        if info is None:
            continue
        e["category"] = info["category"]
        e["severity"] = info["severity"]
        e["suspected_cause"] = info["cause"]


def schedule_classification(
    agent_id: str,
    snapshot: dict[str, Any] | None,
    *,
    client_factory: Callable[[], Any] | None = None,
) -> bool:
    """Kick one background batch for every uncached reliability pattern in
    ``snapshot`` and return immediately (fire-and-forget) -- the telemetry
    insert path's hook, so the alert loop finds a warm cache on its next pass
    without any read having paid for the classify call. Honors the in-flight
    and failure-backoff maps like :func:`categorize_events`; does nothing
    without an API key. Returns whether a batch was started. Must be called
    from within a running event loop.
    """

    if not ai_available():
        return False
    now = time.monotonic()
    todo: list[tuple[tuple[str, int], dict[str, Any]]] = []
    seen: set[tuple[str, int]] = set()
    for e in _reliability_events(snapshot):
        key = _key(e.get("source"), e.get("event_id"))
        if key in _cache or key in _inflight or key in seen:
            continue
        if _failed_until.get(key, 0.0) > now:
            continue
        seen.add(key)
        todo.append((key, e))
    if not todo:
        return False
    factory = client_factory or default_client
    _start_classify_task(factory(), todo)
    return True


@lru_cache(maxsize=1)
def default_client() -> Any:
    """Construct the real Anthropic client (lazy import; needs ``ANTHROPIC_API_KEY``).

    A tiny, dependency-free default so callers outside the dashboard (e.g. the
    ``agent_health`` MCP tool) don't need their own Anthropic wiring. Cached for
    the life of the process — this runs on every dashboard read (even a fully
    cached one, see ``annotate_snapshots``), and building a fresh
    ``anthropic.Anthropic()`` (httpx client, connection pool) per read is pure
    overhead once the cache is warm. Not used by tests, which always inject
    ``client_factory``.
    """

    import anthropic

    return anthropic.Anthropic()


async def annotate_snapshots(
    snapshots: list[dict[str, Any] | None],
    *,
    client_factory: Callable[[], Any] | None = None,
    wait: float = _CLASSIFY_WAIT_SECONDS,
) -> None:
    """Stamp category/severity/suspected_cause onto every reliability event
    across ``snapshots`` (mutating the in-memory dicts in place).

    One batched LLM call for the whole set, cached and deduped by
    :func:`categorize_events`; a no-op when there are no events, and — with no
    API key, or a cold cache the classify call doesn't finish within ``wait``
    seconds — every event resolves to the safe defaults (ADR-0026). A slow
    batch keeps running in the background and warms the cache for the next
    read. ``client_factory`` defaults to :func:`default_client`.
    """

    groups: list[dict[str, Any]] = []
    for snap in snapshots:
        rel = snap.get("reliability") if isinstance(snap, dict) else None
        if isinstance(rel, dict):
            groups.extend(e for e in (rel.get("events") or []) if isinstance(e, dict))
    if not groups:
        return
    factory = client_factory or default_client
    client = factory() if ai_available() else None
    mapping = await categorize_events(client, groups, wait=wait)
    for snap in snapshots:
        rel = snap.get("reliability") if isinstance(snap, dict) else None
        if isinstance(rel, dict) and isinstance(rel.get("events"), list):
            annotate_events(rel["events"], mapping)
