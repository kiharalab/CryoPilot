// Small display helpers shared across the app.

export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return ''
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = Number(bytes)
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`
}

/** Accepts ISO strings or Unix seconds. */
export function toDate(value) {
  if (value === null || value === undefined || value === '') return null
  const date = typeof value === 'number' ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

export function formatTime(value) {
  const date = toDate(value)
  if (!date) return ''
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

export function formatDateTime(value) {
  const date = toDate(value)
  if (!date) return ''
  return date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

/** "just now", "5 min ago", "3 h ago", "Oct 14". */
export function formatRelative(value, now = Date.now()) {
  const date = toDate(value)
  if (!date) return ''
  const seconds = Math.round((now - date.getTime()) / 1000)
  if (seconds < 45) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`
  if (seconds < 86400 * 6) return `${Math.round(seconds / 86400)} d ago`
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

/** "42 s", "3 min 05 s", "2 h 14 min". */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return ''
  const total = Math.max(0, Math.round(seconds))
  if (total < 60) return `${total} s`
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return `${minutes} min ${String(total % 60).padStart(2, '0')} s`
  const hours = Math.floor(minutes / 60)
  return `${hours} h ${String(minutes % 60).padStart(2, '0')} min`
}

export function formatResolution(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return ''
  return `${Number(value).toFixed(2)} Å`
}

export function pluralize(count, singular, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`
}

/** Agent states in which the composer must not send. */
export const ACTIVE_STATES = new Set(['running', 'starting', 'connecting', 'initializing'])

export function isActiveState(state) {
  return ACTIVE_STATES.has(state)
}

/** Short human label for an agent state. */
export function stateLabel(state) {
  switch (state) {
    case 'running':
      return 'Working'
    case 'starting':
      return 'Starting'
    case 'connecting':
      return 'Connecting to CryoSPARC'
    case 'initializing':
      return 'Setting up the CryoSPARC project'
    case 'idle':
      return 'Waiting for you'
    case 'completed':
      return 'Done'
    case 'interrupted':
      return 'Stopped'
    case 'error':
      return 'Error'
    default:
      return 'Not running'
  }
}

export function basename(path = '') {
  const parts = String(path).split('/').filter(Boolean)
  return parts[parts.length - 1] || path
}
