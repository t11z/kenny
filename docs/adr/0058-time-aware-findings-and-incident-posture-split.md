# 0058. Time-aware findings with an incident/posture split

- Status: accepted
- Boundary moved: the observability/health model — from stateless, per-snapshot section
  grading on counts to time-aware findings scored on activity and persistence, split into
  incidents (time-bound, may alarm) and posture (standing facts, never alarm); and the
  server's LLM verdicts on event patterns become durable server state instead of a
  per-process read-path cache.
- Date: 2026-09-06

## Context and Problem Statement

On the operator's live fleet every Windows host was `overall=crit`, driven by the
`reliability` section, and none of the drivers was a problem: a suppressed firehose still
in the headline count, an 80-event reboot storm a week earlier, a handful of one-off errors
from one afternoon, fifty events on a single day eight days ago. The one real incident on
the fleet — two security updates failing every few hours for three days on a host whose
disk was nearly full — sat in `win_update` at the same visual weight as an offline printer.

Three defects of the health model produced this, none of them a threshold:

1. **Health was a pure function of the latest snapshot.** Nothing distinguished a pattern
   seen an hour ago from one that stopped a week ago, although the agent already sent the
   evidence (`by_day`, `last_seen`), and nothing distinguished an event from a standing
   configuration fact, so an unencrypted drive or an open RDP port was re-presented every
   day as if it had just happened.
2. **Two consumers could reach two verdicts about one host.** The ADR-0026 severity lived
   in a per-process cache warmed only by dashboard reads; push alerting, the digest, the
   fleet trend and the chat loop scored the raw payload. ADR-0041 named the divergence and
   worked around it for suppression alone.
3. **The judgement was not wholly the server's.** Five sections carried only the agent's
   own grade, which the server could never lower — the defect ADR-0007 assigns to the
   server side to avoid, and which `reliability` had already been cured of.

The question: what is a finding, when may it alarm, and where does the judgement live?

## Considered Options

- **Raise thresholds, or cap one pattern's contribution to a count.** Rejected: ADR-0041
  already rejected this — a count cannot tell "3439 identical harmless lines" from "3439
  individually relevant errors", nor a reboot storm from a machine falling apart.
- **Hand-maintained tables of benign services, updater families, or event patterns.**
  Rejected: ADR-0026 and ADR-0041 both rejected hand tables for an open-ended, per-fleet
  space; the set of idle auto-start services differs per machine.
- **Keep two scoring paths and suppress per host until the alert path is quiet.**
  Rejected: that is what produced two verdicts per host, and it makes the operator carry
  the cognitive load first in order to be rid of it.
- **Tweak the agent's own grading constants.** Rejected: ADR-0007 puts judgement on the
  server so it can change without redeploying binaries.
- **Chosen: a findings model** — time-aware scoring, an incident/posture split, one
  scoring path fed by durable verdicts, the server as the only judge.

## Decision Outcome

Chosen option: the findings model, in three boundary moves.

1. **A finding is scored on whether it is still happening, relative to the instant of
   evaluation.** The reliability rule derives each pattern's activity and persistence from
   the evidence the agent already sends and scores on that plus the ADR-0026 severity; raw
   volume and pattern diversity no longer escalate anything, and an unclassified pattern can
   never be critical on its own. Because health now depends on *when* it is evaluated,
   historical points are evaluated as of their own `collected_at`, and a finding carries
   the age of its current status, read from the alert loop's state. The concrete
   derivation, the constants and the reason format live in `health_rules.py` and are
   documented in `docs/telemetry.md`; ADR-0041's suppression semantics are unchanged.

2. **Findings are either incidents or posture.** A fourth server-side status, `posture`,
   marks a standing configuration fact: visible and aged on the host page, listed once in
   the weekly digest, never rolled up into a host's overall status, never pushed as an
   alert, never counted as `attention`. It is a server verdict only — the wire
   `Section.status` stays `ok|warn|crit`. Every section verdict carries a `tier`, so no
   consumer re-derives the split. Which sections are posture is a rule-level choice
   recorded in `health_rules.py` / `docs/telemetry.md`, not here.

3. **LLM verdicts are durable server state on the read-path seam.** Classifications are
   persisted per `(source, event_id)` — a fact about the pattern, not a host — loaded at
   boot, and stamped onto every snapshot read through the ADR-0041 store hook, which
   becomes a list of annotators (suppression, then classification). New patterns are
   classified in the background right after the push that carries them lands; ingestion
   never waits for the model (ADR-0026's "ingest independent of AI" holds). This closes
   the two-verdict divergence by construction: every consumer scores the same annotated
   snapshot.

As a consequence of (3) applied to (1) and (2), the five sections the agent still graded
(`services`, `encryption`, `printers`, `time_sync`, `uptime`) report without grading, as
`reliability` already did — an application of ADR-0007, not a new decision.

### Consequences

- Good, because the live fleet's headline becomes the one incident on it, and a reboot
  storm, a one-off crash or a pattern that stopped a week ago no longer pages anyone while
  a pattern firing every day still does.
- Good, because there is one verdict per host, guaranteed by the seam rather than by
  every call site remembering to annotate; the joined tests compare the store's read
  against the dashboard's.
- Good, because "still happening?" and "how long?" are first-class facts computed once on
  the server; clients and MCP output only format them.
- Good, because standing facts stop competing with incidents for attention without being
  hidden.
- Bad / accepted, because a serious pattern that has gone quiet remains a dated warning
  until it leaves the collector's window — chosen deliberately, so an unclean shutdown
  degrades into a dated finding rather than vanishing.
- Bad / accepted, because event sample messages (already sent to the API under ADR-0026)
  now persist server-side with the verdict; acceptable for a self-hosted deployment, and
  the rows are dropped when the classifier model changes.
- Bad / accepted, because ages exist only while the alert loop runs: with
  `KENNY_ALERT_INTERVAL_SECS=0` nothing writes the state they are read from.
- Bad / accepted, because a fresh install without an API key scores every reliability
  pattern as unclassified: an active, recurring quirk can warn until classified or
  suppressed. It can no longer be critical, which is the property that matters.
- Deliberately not done: turning a service's Running→Stopped transition into a change
  notification. Auto-start updaters flip by design; a stopped auto-start service is
  posture, and a service that fails surfaces as event-log patterns scored by activity.

## More Information

- [ADR-0026](0026-llm-categorization-of-reliability-events.md) — amended: the verdict cache
  is durable and stamped on every read, not only on the dashboard read paths.
- [ADR-0041](0041-reliability-alarm-suppression.md) — amended: the `TelemetryStore`
  annotate hook is a list of annotators; the two-path divergence it observed is closed.
  Suppression semantics are unchanged.
- [ADR-0027](0027-push-alerting-ntfy-webhook-and-weekly-digest.md) — the transition-only
  alert loop this record narrows: posture never notifies.
- [ADR-0007](0007-telemetry-push-model-and-sqlite-storage.md) — judgement lives
  server-side; the remaining agent-graded sections now follow it.
- Thresholds, rule-by-rule verdicts and the reason format: `kenny-server/kenny_server/health_rules.py`,
  [`docs/telemetry.md`](../telemetry.md), [`docs/alerting.md`](../alerting.md).
