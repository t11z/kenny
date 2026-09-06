import { describe, expect, it } from 'vitest'
import { formatSince, severityRank } from './format'

describe('formatSince', () => {
  it('is empty until the alert loop has aged the finding', () => {
    expect(formatSince(null)).toBe('')
    expect(formatSince(undefined)).toBe('')
    expect(formatSince(-5)).toBe('')
  })

  it('scales minutes, hours and days', () => {
    expect(formatSince(30)).toBe('since 1 min')
    expect(formatSince(25 * 60)).toBe('since 25 min')
    expect(formatSince(5 * 3600)).toBe('since 5 h')
    expect(formatSince(47 * 3600)).toBe('since 47 h')
    expect(formatSince(3 * 86_400)).toBe('since 3 d')
  })
})

describe('severityRank', () => {
  it('sorts posture between ok and warn', () => {
    expect(severityRank('ok')).toBeGreaterThan(severityRank('posture'))
    expect(severityRank('posture')).toBeGreaterThan(severityRank('warn'))
    expect(severityRank('warn')).toBeGreaterThan(severityRank('crit'))
  })
})
