import { describe, expect, it } from 'vitest'
import { EMPTY_DATA, isOpenFall, parseDashboard, safeDashboardUrl, sampleEvents } from './events'

describe('real event boundaries', () => {
  it('never treats sample events as a real unresolved fall', () => {
    for (const event of sampleEvents()) expect(isOpenFall({ ...event, status: 'alerted' })).toBe(false)
  })
  it('keeps acknowledged real falls visible until resolved', () => {
    const event = { ...sampleEvents()[0], source: 'real' as const, status: 'acknowledged' }
    expect(isOpenFall(event)).toBe(true)
    expect(isOpenFall({ ...event, status: 'resolved' })).toBe(false)
  })
  it('does not claim delivery from unknown backend values', () => {
    const result = parseDashboard({ ...EMPTY_DATA, events: [{ ...sampleEvents()[0], source: 'demo', notification: 'delivered' }] })
    expect(result.events[0].source).toBe('demo')
    expect(result.events[0].notification).toBe('unknown')
  })
  it('sorts recent activity by the time the event was detected', () => {
    const old = { ...sampleEvents()[0], id: 'old', timestamp: '2026-11-10T10:00:00Z', source: 'real' as const, updated_at: '2026-11-15T10:00:00Z' }
    const fresh = { ...old, id: 'fresh', timestamp: '2026-11-14T10:00:00Z', updated_at: '2026-11-14T10:01:00Z' }
    const result = parseDashboard({ ...EMPTY_DATA, events: [old, fresh] })
    expect(result.events.map(event => event.id)).toEqual(['fresh', 'old'])
  })
  it('ignores invalid timestamps and malformed records', () => {
    expect(parseDashboard({ ...EMPTY_DATA, events: [null, {}, { ...sampleEvents()[0], timestamp: 'bad' }] }).events).toEqual([])
  })
})

describe('Databricks dashboard links', () => {
  it('allows direct dashboards on supported workspace domains', () => {
    expect(safeDashboardUrl('https://dbc-example.cloud.databricks.com/dashboardsv3/abc/published')).toBeTruthy()
    expect(safeDashboardUrl('https://adb-123.azuredatabricks.net/sql/dashboards/abc')).toBeTruthy()
  })
  it('rejects script URLs, deceptive domains, and credential URLs', () => {
    for (const value of ['javascript:alert(1)', 'https://databricks.com.evil.org/dashboards/abc', 'https://user:pass@x.cloud.databricks.com/dashboards/abc', 'https://example.com', null]) expect(safeDashboardUrl(value)).toBeNull()
  })
})
