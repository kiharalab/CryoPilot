// xterm.js colours from the app's design tokens, so the terminal follows the
// light and dark themes. xterm needs plain colour strings, so the tokens are
// read from :root at the moment the theme is applied.

/** '#0b7a88' + 0.3 -> 'rgba(11, 122, 136, 0.3)'; other formats pass through unchanged. */
function withAlpha(color, alpha) {
  const hex = color.replace('#', '')
  if (!/^[0-9a-f]{6}$/i.test(hex)) return color
  const value = Number.parseInt(hex, 16)
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`
}

export function terminalTheme() {
  const style = getComputedStyle(document.documentElement)
  const token = (name) => style.getPropertyValue(name).trim()
  const dark = document.documentElement.dataset.theme === 'dark'
  return {
    background: token('--canvas'),
    foreground: token('--fg'),
    cursor: token('--accent'),
    cursorAccent: token('--canvas'),
    selectionBackground: withAlpha(token('--accent'), dark ? 0.35 : 0.22),
    selectionInactiveBackground: withAlpha(token('--accent'), dark ? 0.2 : 0.12),
    scrollbarSliderBackground: withAlpha(token('--faint'), 0.35),
    scrollbarSliderHoverBackground: withAlpha(token('--faint'), 0.55),
    scrollbarSliderActiveBackground: withAlpha(token('--faint'), 0.7),
    // ANSI colours mapped onto the status and syntax tokens; "black" and
    // "white" swap roles between the themes so both stay readable.
    black: dark ? token('--line-strong') : token('--fg'),
    red: token('--danger'),
    green: token('--success'),
    yellow: token('--warning'),
    blue: token('--code-function'),
    magenta: token('--code-keyword'),
    cyan: token('--accent'),
    white: dark ? token('--muted') : token('--line-strong'),
    brightBlack: token('--faint'),
    brightRed: token('--danger'),
    brightGreen: token('--code-string'),
    brightYellow: token('--code-number'),
    brightBlue: token('--code-function'),
    brightMagenta: token('--code-builtin'),
    brightCyan: token('--accent-text'),
    brightWhite: dark ? token('--fg') : token('--muted'),
  }
}
