import { cx } from '../lib/cx'

/** The CryoPilot mark: a hexagonal particle outline around a dense core. */
export function BrandMark({ size = 20, className }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden className={cx('shrink-0', className)}>
      <rect width="32" height="32" rx="8" fill="var(--accent)" />
      <path d="M16 6.5 24.2 11.2v9.6L16 25.5l-8.2-4.7v-9.6z" fill="none" stroke="var(--accent-fg)" strokeWidth="2.2" strokeLinejoin="round" />
      <circle cx="16" cy="16" r="3.1" fill="var(--accent-fg)" />
    </svg>
  )
}

export function Wordmark({ className }) {
  return (
    <span className={cx('inline-flex items-center gap-2 font-semibold tracking-[-0.015em] text-fg', className)}>
      <BrandMark size={20} />
      CryoPilot
    </span>
  )
}
