export type CareEvent = {
  id: string
  type: string
  status: string
  room: string
  timestamp: string
  updated_at?: string
  source: 'real' | 'demo'
  local_demo?: boolean
  notification: 'accepted' | 'failed' | 'pending' | 'not_sent' | 'unknown' | 'simulated'
  details?: string
}
export type DashboardData = {
  connected: boolean
  monitoring: 'active' | 'offline' | 'waiting' | 'unknown'
  dry_run: boolean
  home_name: string
  dashboard_url: string | null
  events: CareEvent[]
  history_available: boolean
  checked_at: string
}
export const EMPTY_DATA: DashboardData = {
  connected: false, monitoring: 'unknown', dry_run: false, home_name: 'Your home',
  dashboard_url: null, events: [], history_available: false, checked_at: '',
}
export const FALL_TYPES = new Set(['fall', 'fall_detected', 'possible_fall', 'immobility', 'help_request'])
export const OPEN_STATUSES = new Set(['new', 'pending_confirmation', 'alerted', 'escalated', 'acknowledged', 'recovered'])
export function isOpenFall(event: CareEvent) { return event.source === 'real' && FALL_TYPES.has(event.type) && OPEN_STATUSES.has(event.status) }
export function eventTitle(event: CareEvent) {
  if (FALL_TYPES.has(event.type)) return event.type === 'immobility' ? 'Time on the floor detected' : event.type === 'help_request' ? 'Help requested' : 'Fall detected'
  return ({ camera_offline: 'Monitoring interrupted', near_fall: 'Stumble detected', low_light: 'Low light noticed', obstacle: 'Walking path needs attention', night_wandering: 'Nighttime activity', inactivity: 'A check-in may be needed', recovered: 'Recovery detected' } as Record<string, string>)[event.type] || 'Care event recorded'
}
export function notificationLabel(event: CareEvent) {
  if (event.local_demo) return event.notification === 'pending' ? 'Demo in progress' : 'Demo notification'
  return ({ accepted: 'Alert service accepted', failed: 'Alert failed', pending: 'Alert pending', not_sent: 'No alert sent', unknown: 'Status unavailable', simulated: 'Demo notification' })[event.notification]
}
export function timeLabel(value: string, options?: Intl.DateTimeFormatOptions) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Time unavailable' : new Intl.DateTimeFormat(undefined, options || { hour: 'numeric', minute: '2-digit' }).format(date)
}
export function sampleEvents(now = new Date()): CareEvent[] {
  return [1, 2].map((day, index) => ({
    id: `example-${index}`, type: 'fall_detected', status: 'resolved', room: index ? 'Bedroom' : 'Living room',
    timestamp: new Date(now.getTime() - day * 86400000 - index * 7200000).toISOString(), source: 'demo', local_demo: true,
    notification: 'simulated', details: 'An example of a recorded fall. This is sample data; no fall occurred and no notification was sent.',
  }))
}
export function safeDashboardUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && !url.username && !url.password && /(^|\.)((cloud\.)?databricks\.com|azuredatabricks\.net)$/.test(url.hostname) && /^\/(?:sql\/)?dashboards(?:v3)?\//.test(url.pathname) ? url.href : null
  } catch { return null }
}
export function parseDashboard(value: unknown): DashboardData {
  if (!value || typeof value !== 'object') throw new Error('Invalid dashboard response')
  const data = value as DashboardData
  if (!Array.isArray(data.events) || typeof data.connected !== 'boolean') throw new Error('Invalid dashboard response')
  const events = data.events.filter(e => e && typeof e.id === 'string' && typeof e.type === 'string' && typeof e.status === 'string' && typeof e.room === 'string' && typeof e.timestamp === 'string' && !Number.isNaN(new Date(e.timestamp).getTime()))
    .map(e => ({ ...e, source: e.source === 'demo' ? 'demo' as const : 'real' as const, local_demo: false, notification: ['accepted', 'failed', 'pending', 'not_sent', 'unknown'].includes(e.notification) ? e.notification : 'unknown' as const }))
  events.sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))
  return { ...EMPTY_DATA, ...data, dashboard_url: safeDashboardUrl(data.dashboard_url), events }
}
