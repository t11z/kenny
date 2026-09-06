# 0058. Time-aware findings with an incident/posture split

- Status: accepted
- Boundary moved: the observability/health model — from stateless, per-snapshot section
  grading on counts to time-aware findings scored on activity and persistence, split into
  incidents (time-bound, may alarm) and posture (standing facts, never alarm); and the
  server's LLM verdicts on event patterns become durable server state instead of a
  per-process read-path cache.
- Date: 2026-09-06

## Context and Problem Statement

On the operator's live fleet (three Windows hosts, one Linux host) every Windows host was
`overall=crit`, driven by the `reliability` section, and none of the drivers was a problem:

| Host | What drove `crit` | What the data actually said |
|---|---|---|
| thomas-pc | 12 patterns, "3528 error/critical events in 7d" | CAPI2/4176 ×3381, already suppressed but still in the headline total; DCOM/10010 ×80 all on one day a week ago (a reboot storm); seven one-offs from that same afternoon; one genuinely active pattern (DeviceAssociationService/3503, five of seven days) |
| linus-pc | GameDVR/10005 ×2 "+5 more" | crit came from the "≥ 5 distinct non-benign patterns" rule; the real incident — two security updates failing every ~4 h for three days, disk 96 % full — sat in `win_update`/`disk`, visually on par with "1 of 4 printers offline" |
| maria-pc | Hyper-V VmSwitch/63 ×50 | one day, eight days ago |

The health model had four structural defects, each verified in code:

1. **Scoring counted volume and diversity, not relevance.** `health_rules.py` escalated to
   crit on ≥ 5 distinct non-benign patterns (every Windows PC clears that bar weekly) or
   on a `serious` pattern with ≥ 10 hits; its no-annotation fallback escalated on ≥ 50
   events. The agent already sent `by_day` and `last_seen` per pattern, and nothing read
   them: a burst eight days ago weighed the same as a pattern seen an hour ago.
2. **Two scoring paths disagreed about the same host.** The ADR-0026 LLM severity lived in
   a per-process cache that only the dashboard's two read paths and the `agent_health`
   tool warmed; push alerting, the weekly digest, the fleet trend and the chat tool loop
   read snapshots straight from the store and took the volume fallback. ADR-0041 named this
   divergence and worked around it for suppression only.
3. **There was no time dimension anywhere.** `alert_state` already recorded `since` per
   section, and nothing presented it. A standing fact (BitLocker off, RDP open, updater
   services idle, 112 days of uptime) was re-presented every day as if it had just
   happened.
4. **Five sections were graded by the agent with no server rule** (`services`, `printers`,
   `encryption`, `time_sync`, `uptime`), so the server could never lower them — the bug
   class `reliability` itself had already been cured of ("report, do not grade").

The question: what is a finding, when may it alarm, and where does the judgement live?

## Considered Options

- **Raise the thresholds, or cap one pattern's contribution.** Rejected: ADR-0041 already
  rejected this — a count cannot tell "3439 identical harmless lines" from "3439
  individually relevant errors", and a distinct-pattern count cannot tell a reboot storm
  from a machine falling apart.
- **Hand-maintained tables of benign services, updater families, or event patterns.**
  Rejected: ADR-0026 and ADR-0041 both rejected hand tables for an open-ended, per-fleet
  space; the live fleet's "10 auto services stopped" is entirely trigger-start and updater
  services whose set differs per machine.
- **Keep two scoring paths and add per-host suppression until the alert path is quiet.**
  Rejected: that is what produced two verdicts per host, and it makes the operator carry
  the cognitive load first in order to be rid of it.
- **Tweak the agent's own grading constants.** Rejected: ADR-0007 puts judgement on the
  server so it can change without redeploying binaries; `reliability.rs` already reports
  without grading for exactly this reason.
- **Chosen: a findings model.** A finding is either an *incident* (time-bound — new,
  recurring, escalating — and allowed to alarm) or *posture* (a standing configuration
  fact — visible, aged, never alarming). Reliability is scored on activity and persistence
  derived from the evidence the agent already sends; the LLM verdicts become durable and
  ride the store's read-path seam so every consumer scores the same severity; the five
  agent-graded sections get server rules and the agent stops grading them; and
  presentation shows findings with an age instead of section names with a static verb.

## Decision Outcome

Chosen option: the findings model, delivered in two steps.

**Step 1 — reliability scored on activity and persistence; one scoring path.**

- `health_rules.reliability_patterns` derives, per `(source, event_id)` group and relative
  to the evaluation instant, `active` (last seen within 48 h, or on ≥ 3 distinct days of
  the window while its last hit is still inside it), `recurring` (≥ 2 distinct days) and
  `burst` (one day holds ≥ 80 % of the count and the pattern has gone quiet). The verdict:
  an active `serious` pattern, or a stability index < 3, is **crit**; a `serious` pattern
  that has gone quiet (it self-clears when it leaves the window), an active *and* recurring
  `notable`/`unknown` pattern, or a stability index < 6, is **warn**; everything else —
  benign, one-off, burst, historical, suppressed — is **ok**. There is no count threshold.
  Without any classification every pattern is `unknown`, which can reach warn but never
  crit: a count alone is never a critical finding. ADR-0041's semantics are unchanged: a
  suppressed pattern never scores, and the stability-index overlay is never suppressible.
- The reason is finding-shaped — up to three scoring patterns with count, days active of
  the window, age of the last hit and suspected cause, then the historical remainder folded
  into one clause — and never leads with the raw 7-day total. A rule may now return a third
  element, `details`, which `evaluate_section` copies into the section verbatim; reliability
  returns its per-pattern activity there so no client re-derives a threshold.
- LLM verdicts are persisted in `event_classifications` (`source`, `event_id`, `category`,
  `severity`, `cause`, `model`, `classified_at`), keyed exactly as the in-memory cache and
  not per host — a classification is a fact about the pattern. `event_categories.mark`
  stamps them synchronously from the mirror, and `TelemetryStore.annotators` (the ADR-0041
  hook, now a list) runs suppression then classification on every read, so alerting, the
  digest, the fleet list, MCP and the dashboard all read the same severity. Cache misses are
  filled by a fire-and-forget batch the tunnel kicks right after a push is stored
  (`AgentTunnel.after_insert`); ingestion never waits for the model, preserving ADR-0026's
  "ingest independent of AI". A classifier model change drops the old rows at boot.
- History is evaluated "as of" each snapshot's own `collected_at` (`build_health(now=...)`):
  with age-based scoring, judging last month's snapshot by today's date would make every
  historical point look inactive.

**Step 2 — the incident/posture split across sections, and finding-shaped presentation.**

- A fourth server-side health status, `posture`, alongside `ok`/`warn`/`crit`. It is a
  server verdict only: `protocol.Status` stays `ok | warn | crit`, and a status from the
  wire is coerced as before. `worst()` maps posture to ok, so a host whose only findings
  are posture is `overall=ok`; `evaluate_section` adds `tier = incident | posture | none`.
  Posture transitions never notify — `ok↔posture`, `warn→posture`, `crit→posture` update
  alert state silently, so posture findings still carry an age — and posture appears on the
  host page, as a count line on Today, and once in the weekly digest.
- Server rules for the agent-graded sections: auto-start services not running (Windows) and
  an unencrypted system drive are posture; a remote-access port listening is posture (it was
  warn); an offline printer is ok with a reason; a failed systemd unit, an unsynchronized
  clock or a large offset are warn, an unreadable time service is posture; ≥ 30 days of
  Windows uptime is posture, Linux uptime is never a finding. `win_update` is scored on
  recurrence — an update failing ≥ 3 times across ≥ 2 days is an incident (crit), a single
  failure a warn — never on a localized title. The five agent collectors stop grading
  ("report, do not grade", with a test pinning it, as `reliability.rs` has).
- Age comes from `alert_state.since` (meaning: since the *current* status), stamped at read
  time by the callers that already touch the store; `health_rules.py` stays pure. Today ranks
  incidents newest-first and shows their age; alert bodies carry the finding text
  (`[CRIT] disk: C: 97% full`) instead of `disk: ok -> crit`.

### Consequences

- Good, because the live fleet's headline changes from "three machines critical" to the
  one thing that is: security updates failing for days on one host. A reboot storm, a
  one-off crash and a pattern that stopped a week ago no longer page anyone, and a pattern
  firing every day still does.
- Good, because there is one verdict per host: the alert loop can no longer say crit while
  the dashboard says ok. The ADR-0041 seam carries both annotations and the joined tests
  compare the store's read against the dashboard's.
- Good, because "still happening?" is now a first-class fact (`active`, `recurring`,
  `burst`, `since`) computed once on the server and only formatted by clients — no
  threshold is restated in the console or in MCP output.
- Good, because standing facts stop competing with incidents for attention without being
  hidden: posture is listed, aged and digested, it just does not paint a host red.
- Bad / accepted, because a `serious` pattern that stopped three days ago is still a warn
  until it leaves the window. Chosen deliberately: an unclean shutdown must not vanish
  silently; it degrades to a dated finding rather than disappearing.
- Bad / accepted, because event sample messages (already sent to the API under ADR-0026)
  now also persist server-side in `event_classifications.cause` and via the cached sample
  the classifier saw; acceptable for a self-hosted deployment, and the table is dropped
  when the classifier model changes.
- Bad / accepted, because ages depend on the alert loop running: with
  `KENNY_ALERT_INTERVAL_SECS=0` nothing writes `alert_state`, so findings carry no `since`.
- Bad / accepted, because a fresh install without an API key scores every reliability
  pattern as `unknown`: an active, recurring quirk can warn until it is classified or
  suppressed. It can no longer crit, which is the property that matters.
- Deliberately not done: diffing `services.status` so that Running→Stopped becomes a change
  notification. Auto-start updaters flip Running↔Stopped hourly by design; a stopped
  auto-start service is posture, and a service that *fails* surfaces as Service Control
  Manager events in `reliability`, scored by activity there.

## More Information

- [ADR-0026](0026-llm-categorization-of-reliability-events.md) — amended: the verdict cache
  is now durable and stamped on every read, not only on the two dashboard read paths.
- [ADR-0041](0041-reliability-alarm-suppression.md) — amended: the `TelemetryStore.annotate`
  hook is now a list of annotators; its observation that only the dashboard paths carried
  severity is what this record closes. Suppression semantics are unchanged.
- [ADR-0027](0027-push-alerting-ntfy-webhook-and-weekly-digest.md) — the transition-only
  alert loop this record narrows (posture never notifies) and re-phrases (finding text).
- [ADR-0007](0007-telemetry-push-model-and-sqlite-storage.md) — judgement lives
  server-side; the five collectors join `reliability` in reporting without grading.
