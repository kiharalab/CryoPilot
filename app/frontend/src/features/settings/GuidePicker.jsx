// Choice between the reconstruction agent's processing guides, with a
// preview of the Markdown. Used in onboarding and in Settings.
import { useEffect, useId, useState } from 'react'
import { BookOpen } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { pluralize } from '../../lib/format'
import { Button, Dialog, DialogClose, DialogContent, InlineError, Skeleton } from '../../components/ui'
import { Markdown } from '../../components/Markdown'

/**
 * Props:
 *   value             selected guide id
 *   onChange(id)      called when the user picks another guide
 *   disabled          while a change is being saved
 */
export function GuidePicker({ value, onChange, disabled = false }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [preview, setPreview] = useState(null)
  const name = useId()

  useEffect(() => {
    let cancelled = false
    api
      .guides()
      .then((result) => {
        if (!cancelled) setData(result)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (error) return <InlineError>Could not load the processing guides: {error}</InlineError>
  if (!data) {
    return (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-[116px] rounded-xl" />
        <Skeleton className="h-[116px] rounded-xl" />
      </div>
    )
  }

  return (
    <>
      <div role="radiogroup" aria-label="Processing guide" className="flex flex-col gap-2">
        {data.guides.map((guide) => (
          <GuideOption
            key={guide.id}
            guide={guide}
            name={name}
            selected={guide.id === value}
            disabled={disabled || !guide.available}
            onSelect={() => guide.id !== value && onChange(guide.id)}
            onPreview={() => setPreview(guide)}
          />
        ))}
      </div>
      <GuidePreviewDialog
        guide={preview}
        open={Boolean(preview)}
        onOpenChange={(open) => !open && setPreview(null)}
        selected={preview?.id === value}
        onChoose={() => {
          onChange(preview.id)
          setPreview(null)
        }}
      />
    </>
  )
}

function GuideOption({ guide, name, selected, disabled, onSelect, onPreview }) {
  return (
    <div
      className={cx(
        'rounded-xl border transition-colors has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-accent/40',
        selected ? 'border-accent bg-accent-soft' : 'border-line hover:border-line-strong',
        !guide.available && 'opacity-60',
      )}
    >
      <label className={cx('flex gap-3 px-4 pb-2 pt-3.5', disabled ? 'cursor-not-allowed' : 'cursor-pointer')}>
        <input type="radio" name={name} className="sr-only" checked={selected} disabled={disabled} onChange={onSelect} />
        <span
          aria-hidden
          className={cx(
            'mt-0.5 h-4 w-4 shrink-0 rounded-full border transition-[border-width,border-color]',
            selected ? 'border-[5px] border-accent bg-canvas' : 'border-line-strong bg-canvas',
          )}
        />
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-medium text-fg">{guide.label}</span>
          {guide.title ? <span className="block text-xs text-faint">{guide.title}</span> : null}
          {guide.available ? (
            <span className="mt-1.5 line-clamp-3 block text-sm text-muted">{guide.description}</span>
          ) : (
            <span className="mt-1 block break-all text-sm text-danger">The guide file is missing: {guide.path}</span>
          )}
        </span>
      </label>
      <div className="flex items-center justify-between gap-2 pb-2 pl-11 pr-2">
        <span className="text-xs text-faint">{guide.available ? pluralize(guide.sections?.length || 0, 'section') : ''}</span>
        {guide.available ? (
          <Button size="xs" variant="ghost" icon={BookOpen} onClick={onPreview}>
            Preview
          </Button>
        ) : null}
      </div>
    </div>
  )
}

/** The full guide as Markdown, with a button to choose it. */
export function GuidePreviewDialog({ guide, open, onOpenChange, selected, onChoose }) {
  const [markdown, setMarkdown] = useState(null)
  const [error, setError] = useState('')
  const guideId = guide?.id

  useEffect(() => {
    if (!open || !guideId) return
    let cancelled = false
    setMarkdown(null)
    setError('')
    api
      .guide(guideId)
      .then((result) => {
        if (!cancelled) setMarkdown(result.markdown)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [open, guideId])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={guide?.label || 'Processing guide'} description={guide?.title} width="max-w-3xl" className="h-[min(760px,calc(100vh-48px))]">
        <div className="mt-3 min-h-0 flex-1 overflow-y-auto border-t border-line px-6 py-5">
          {error ? (
            <InlineError>Could not load the guide: {error}</InlineError>
          ) : markdown === null ? (
            <div className="flex flex-col gap-3">
              <Skeleton className="h-5 w-2/3" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-11/12" />
              <Skeleton className="h-4 w-4/5" />
            </div>
          ) : (
            <Markdown size="sm">{markdown}</Markdown>
          )}
        </div>
        <div className="flex shrink-0 items-center justify-end gap-2 border-t border-line px-5 py-3">
          <DialogClose asChild>
            <Button variant={selected ? 'primary' : 'secondary'}>Close</Button>
          </DialogClose>
          {!selected ? (
            <Button variant="primary" onClick={onChoose}>
              Use this guide
            </Button>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  )
}
