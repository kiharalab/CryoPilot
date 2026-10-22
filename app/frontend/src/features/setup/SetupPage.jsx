// Full-page shell for the screens before the workspace: the access link,
// first-run setup, and signing in again.
import { cx } from '../../lib/cx'

export function SetupPage({ children, width = 'max-w-[520px]', centered = false }) {
  return (
    <div className="h-full overflow-y-auto bg-canvas text-fg">
      <div className={cx('mx-auto flex min-h-full w-full flex-col px-6 py-12 sm:py-16', centered && 'justify-center', width)}>{children}</div>
    </div>
  )
}

/** Inline code in setup copy, e.g. a command to run. */
export function Code({ children }) {
  return <code className="rounded border border-line bg-subtle px-1 py-px font-mono text-[12px] text-fg">{children}</code>
}
