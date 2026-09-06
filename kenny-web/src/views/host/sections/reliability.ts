import type { ReliabilityDetails, ReliabilityEvent, ReliabilityPattern } from '../types'

/**
 * The severity the LLM categoriser assigned a group (`docs/protocol.md`,
 * reliability). Distinct from the Windows `level` on the same row: `level` is
 * what the event log recorded, `severity` is what it was judged to mean. A
 * `critical` level with a `benign` severity is the ordinary case — a driver that
 * logs loudly and harms nothing — and showing only the first is why a quiet
 * machine can look alarming.
 */
export type EventSeverity = 'benign' | 'notable' | 'serious' | 'unknown'

const SEVERITY_RANK: Record<EventSeverity, number> = { serious: 3, notable: 2, unknown: 1, benign: 0 }

/** Events the categoriser has not annotated yet fall in here rather than being dropped. */
export const UNCATEGORISED = 'uncategorised'

export interface EventGroup {
  /** The friendly category, or `UNCATEGORISED`. */
  category: string
  events: ReliabilityEvent[]
  /** Summed occurrences, which is what the groups are ordered by. */
  total: number
  /** The worst severity in the group — what the group header has to answer for. */
  worst: EventSeverity
}

export function severityOf(event: ReliabilityEvent): EventSeverity {
  return event.severity ?? 'unknown'
}

/**
 * Bundle events by the categoriser's `category`, loudest group first.
 *
 * Ordering is by total occurrences, not by worst severity: the heatmap beside it
 * is a volume view, and a list ordered differently from the grid it explains
 * makes the two impossible to read together. Severity is carried on the badge,
 * where it does not have to compete with volume for the same axis.
 */
export function groupByCategory(
  events: ReliabilityEvent[],
  patterns: Map<string, ReliabilityPattern> = new Map(),
): EventGroup[] {
  const byCategory = new Map<string, ReliabilityEvent[]>()
  for (const ev of events) {
    const key = ev.category?.trim() || UNCATEGORISED
    const bucket = byCategory.get(key)
    if (bucket) bucket.push(ev)
    else byCategory.set(key, [ev])
  }
  // Within a group, what is still happening outranks what merely happened
  // often: an active pattern sits above a louder one that went quiet a week
  // ago. Between groups the ordering stays by volume (see above).
  const isActive = (ev: ReliabilityEvent) => patterns.get(patternKey(ev))?.active === true
  return [...byCategory.entries()]
    .map(([category, list]) => ({
      category,
      events: [...list].sort((a, b) => Number(isActive(b)) - Number(isActive(a)) || b.count - a.count),
      total: list.reduce((sum, ev) => sum + ev.count, 0),
      worst: list.reduce<EventSeverity>(
        (worst, ev) => (SEVERITY_RANK[severityOf(ev)] > SEVERITY_RANK[worst] ? severityOf(ev) : worst),
        'benign',
      ),
    }))
    .sort((a, b) => b.total - a.total || a.category.localeCompare(b.category))
}

export interface Heatmap {
  /** ISO dates, oldest first — the union of every group's `by_day` keys. */
  days: string[]
  rows: { category: string; counts: number[]; total: number }[]
  /** The busiest single cell, for scaling the shading. 0 when there is nothing to show. */
  peak: number
}

/**
 * Fold the per-event `by_day` histograms into one category × day grid.
 *
 * The days axis is the union of every histogram's keys rather than a fixed
 * window: `window_days` is what the collector asked for, but a day on which
 * nothing happened is simply absent from the payload, and inventing columns for
 * days no event mentions would imply a reading that was never taken.
 */
export function buildHeatmap(groups: EventGroup[]): Heatmap {
  const days = new Set<string>()
  for (const group of groups) {
    for (const ev of group.events) {
      for (const day of Object.keys(ev.by_day ?? {})) days.add(day)
    }
  }
  const sortedDays = [...days].sort()
  let peak = 0
  const rows = groups.map((group) => {
    const counts = sortedDays.map((day) =>
      group.events.reduce((sum, ev) => sum + (ev.by_day?.[day] ?? 0), 0),
    )
    for (const c of counts) if (c > peak) peak = c
    return { category: group.category, counts, total: counts.reduce((a, b) => a + b, 0) }
  })
  return { days: sortedDays, rows, peak }
}

/** `2026-06-27` -> `06-27`; the year is constant across a 7-day window and only costs width. */
export function shortDay(iso: string): string {
  return iso.length === 10 ? iso.slice(5) : iso
}

/** The `(source, event_id)` identity a pattern record and an event group share. */
export function patternKey(item: { source: string | null; event_id: number | null }): string {
  return `${item.source ?? ''}|${item.event_id ?? ''}`
}

/**
 * Index the section's `details.patterns` (ADR-0058) by `(source, event_id)` so
 * an event card can look up its own activity record. Tolerates a section with
 * no details at all (an older server, or a rule that deferred): every lookup
 * then misses and the cards simply carry no activity chip.
 */
export function patternByKey(details: unknown): Map<string, ReliabilityPattern> {
  const out = new Map<string, ReliabilityPattern>()
  const patterns = (details as Partial<ReliabilityDetails> | undefined)?.patterns
  if (!Array.isArray(patterns)) return out
  for (const p of patterns) {
    if (p && typeof p === 'object') out.set(patternKey(p), p)
  }
  return out
}

export type ActivityTone = 'active' | 'quiet'

/**
 * A caps label for what the server decided about a pattern's activity, and
 * whether it should read as live or as history. Pure formatting of the three
 * booleans the rule already computed: the console never re-derives "48 hours"
 * or "three days" from the histogram itself.
 */
export function activityLabel(p: ReliabilityPattern, windowDays: number): { label: string; tone: ActivityTone } {
  const since = p.last_day ? ` · ${shortDay(p.last_day)}` : ''
  if (p.active && p.recurring) return { label: `ACTIVE · ${p.active_days}/${windowDays} DAYS`, tone: 'active' }
  if (p.active) return { label: 'NEW', tone: 'active' }
  if (p.burst && p.recurring) return { label: `BURST${since}`, tone: 'quiet' }
  if (!p.recurring) return { label: `ONE-OFF${since}`, tone: 'quiet' }
  return { label: `QUIET SINCE${since || ' ?'}`, tone: 'quiet' }
}
