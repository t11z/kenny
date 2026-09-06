import type { HostSection } from '../../api/types'
import { Check, ICON_STROKE_WIDTH } from '../../components/icons'
import { severityColor } from '../../components/tone'
import { sectionIcon, humanizeSectionName } from './sections'
import { formatSince } from './format'
import styles from './SectionList.module.css'

export interface SectionListProps {
  sections: HostSection[]
  onOpenProblem: (section: HostSection) => void
}

function isPosture(s: HostSection): boolean {
  return s.tier === 'posture' || (s.tier === undefined && s.status === 'posture')
}

/**
 * Splits problem cards, the posture list and the healthy checklist purely on
 * the server-computed `attention` / `tier` (`health_rules.py`), never
 * re-derived from `status` here. Posture (ADR-0058) is a standing fact —
 * listed compactly with its age, openable like a problem card so the detail is
 * one click away, but never presented as something that just happened. Only
 * problem and posture rows are clickable (the prototype's healthy grid `<a>`
 * rows carry no `onClick` at all, prototype lines 230-234).
 */
export default function SectionList({ sections, onOpenProblem }: SectionListProps) {
  const problems = sections.filter((s) => s.attention)
  const posture = sections.filter((s) => !s.attention && isPosture(s))
  const healthy = sections.filter((s) => !s.attention && !isPosture(s))

  return (
    <>
      {problems.length > 0 && (
        <>
          <div className={styles.eyebrow}>
            NEEDS ATTENTION · {problems.length} SECTION{problems.length === 1 ? '' : 'S'}
          </div>
          <div className={styles.problems}>
            {problems.map((s) => {
              const Icon = sectionIcon(s.name)
              const color = severityColor(s.status)
              return (
                <button
                  key={s.name}
                  type="button"
                  className={`${styles.problemCard} kc-stagger-row`}
                  onClick={() => onOpenProblem(s)}
                >
                  <div className={styles.problemHead}>
                    <Icon width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} color={color} aria-hidden="true" />
                    <span className={styles.problemName}>{humanizeSectionName(s.name)}</span>
                    <span className={styles.rule} style={{ color }}>
                      {s.reason ? `${s.reason} ⇒ ${s.status}` : s.status.toUpperCase()}
                    </span>
                  </div>
                  {s.summary && <div className={styles.problemSummary}>{s.summary}</div>}
                </button>
              )
            })}
          </div>
        </>
      )}

      {posture.length > 0 && (
        <>
          <div className={styles.eyebrow}>
            POSTURE · {posture.length} STANDING FACT{posture.length === 1 ? '' : 'S'}
          </div>
          <div className={styles.postureList}>
            {posture.map((s) => {
              const Icon = sectionIcon(s.name)
              const since = formatSince(s.age_seconds)
              return (
                <button
                  key={s.name}
                  type="button"
                  className={styles.postureRow}
                  onClick={() => onOpenProblem(s)}
                >
                  <Icon width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} color="var(--text-muted)" aria-hidden="true" />
                  <span className={styles.postureName}>{humanizeSectionName(s.name)}</span>
                  <span className={styles.postureReason}>{s.reason ?? s.summary ?? ''}</span>
                  {since && <span className={styles.postureSince}>{since}</span>}
                </button>
              )
            })}
          </div>
        </>
      )}

      <div className={styles.eyebrow}>
        HEALTHY · {healthy.length} SECTION{healthy.length === 1 ? '' : 'S'}
      </div>
      <div className={styles.healthyGrid}>
        {healthy.map((s) => (
          <div key={s.name} className={styles.healthyCell}>
            <Check width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} color="var(--ok)" aria-hidden="true" />
            {humanizeSectionName(s.name)}
          </div>
        ))}
      </div>
    </>
  )
}
