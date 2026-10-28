// Full-page shell for the screens before the workspace: the access link, the
// license, first-run setup, and signing in again. Also the layout of one
// setup step, with its title, content, and Back/Continue footer.
import { cx } from '../../lib/cx'
import { Button } from '../../components/ui'

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

// ------------------------------------------------------------ setup steps
export function StepLayout({ title, description, children, footer }) {
  return (
    <section className="mt-10 flex flex-1 flex-col">
      <h1 className="text-xl font-semibold tracking-[-0.01em]">{title}</h1>
      {description ? <p className="mt-1.5 text-sm text-muted">{description}</p> : null}
      <div className="mt-6">{children}</div>
      <div className="mt-8 flex items-center gap-2">{footer}</div>
    </section>
  )
}

export function StepFooter({ onBack, onSkip, skipLabel = 'Skip for now', primaryLabel = 'Continue', onPrimary, busy = false, primaryType = 'button' }) {
  return (
    <>
      {onBack ? (
        <Button variant="ghost" onClick={onBack} disabled={busy}>
          Back
        </Button>
      ) : null}
      <span className="flex-1" />
      {onSkip ? (
        <Button variant="ghost" onClick={onSkip} disabled={busy}>
          {skipLabel}
        </Button>
      ) : null}
      <Button type={primaryType} variant="primary" loading={busy} onClick={primaryType === 'submit' ? undefined : onPrimary}>
        {primaryLabel}
      </Button>
    </>
  )
}
