// The computer the browser runs on, for keyboard shortcuts: Cmd on macOS,
// Ctrl everywhere else (never both).

/** True on macOS (and iOS/iPadOS). */
export function isMac() {
  const platform = navigator.userAgentData?.platform || navigator.platform || navigator.userAgent || ''
  return /mac|iphone|ipad/i.test(platform)
}

/** The shortcut modifier as shown to the user: '⌘' on macOS, 'Ctrl' elsewhere. */
export const modKeyLabel = isMac() ? '⌘' : 'Ctrl'

/** True when the event carries this platform's shortcut modifier and not the other one. */
export function hasModKey(event) {
  return isMac() ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey
}
