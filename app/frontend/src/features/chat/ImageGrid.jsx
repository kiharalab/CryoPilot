// Images the agents looked at or produced: a row of thumbnails in the
// transcript, and a lightbox that steps through them.
import { useCallback, useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight, Download, ExternalLink } from 'lucide-react'
import { cx } from '../../lib/cx'
import { Dialog, DialogContent, IconButton } from '../../components/ui'

/** images: [{src, caption, description}] */
export function ImageGrid({ images, className }) {
  const [open, setOpen] = useState(-1)
  if (!images?.length) return null
  return (
    <>
      <div className={cx('flex flex-wrap gap-2', className)}>
        {images.map((image, index) => (
          <button
            key={`${image.src.slice(0, 120)}-${index}`}
            type="button"
            onClick={() => setOpen(index)}
            aria-label={`Open ${image.caption || 'image'}`}
            title={image.description || image.caption}
            className="group/thumb relative overflow-hidden rounded-lg border border-line bg-inset transition-colors hover:border-line-strong"
          >
            <img
              src={image.src}
              alt={image.description || image.caption || ''}
              loading="lazy"
              decoding="async"
              className="block h-40 w-auto max-w-[min(100%,360px)] object-contain"
            />
          </button>
        ))}
      </div>
      <Lightbox images={images} index={open} onIndexChange={setOpen} />
    </>
  )
}

export function Lightbox({ images, index, onIndexChange }) {
  const image = index >= 0 ? images[index] : null
  const count = images.length
  const go = useCallback((delta) => onIndexChange((current) => (current + delta + count) % count), [count, onIndexChange])

  useEffect(() => {
    if (!image || count < 2) return undefined
    const onKeyDown = (event) => {
      if (event.key === 'ArrowRight') go(1)
      if (event.key === 'ArrowLeft') go(-1)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [image, count, go])

  const isDataUrl = image?.src.startsWith('data:')
  const fileName = downloadName(image)

  return (
    <Dialog open={Boolean(image)} onOpenChange={(value) => !value && onIndexChange(-1)}>
      {image ? (
        <DialogContent
          title={image.caption || 'Image'}
          description={image.description || undefined}
          width="max-w-[min(92vw,1180px)]"
        >
          <div className="relative mx-5 mt-3 flex min-h-0 flex-1 items-center justify-center overflow-hidden rounded-xl border border-line bg-inset">
            <img src={image.src} alt={image.description || image.caption || ''} className="block max-h-[72vh] max-w-full object-contain" />
            {count > 1 ? (
              <>
                <IconButton
                  icon={ChevronLeft}
                  label="Previous image"
                  onClick={() => go(-1)}
                  className="absolute left-2 top-1/2 -translate-y-1/2 bg-elevated/90 shadow-popover ring-1 ring-line"
                />
                <IconButton
                  icon={ChevronRight}
                  label="Next image"
                  onClick={() => go(1)}
                  className="absolute right-2 top-1/2 -translate-y-1/2 bg-elevated/90 shadow-popover ring-1 ring-line"
                />
              </>
            ) : null}
          </div>
          <div className="flex items-center gap-2 px-5 pb-4 pt-3">
            <span className="flex-1 text-sm text-muted">{count > 1 ? `${index + 1} of ${count}` : null}</span>
            {!isDataUrl ? (
              <a
                href={image.src}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-sm font-medium text-muted hover:bg-hover hover:text-fg"
              >
                <ExternalLink size={14} strokeWidth={1.75} />
                Open in new tab
              </a>
            ) : null}
            <a
              href={image.src}
              download={fileName}
              className="inline-flex h-7 items-center gap-1.5 rounded-md border border-line bg-elevated px-2.5 text-sm font-medium text-fg hover:bg-hover"
            >
              <Download size={14} strokeWidth={1.75} />
              Download
            </a>
          </div>
        </DialogContent>
      ) : null}
    </Dialog>
  )
}

function downloadName(image) {
  if (!image) return 'image.png'
  const caption = String(image.caption || 'image').replace(/[^\w.-]+/g, '_')
  if (/\.(png|jpe?g|gif|webp|svg)$/i.test(caption)) return caption
  const mime = /^data:image\/([\w+]+)/.exec(image.src)?.[1]
  const extension = mime === 'jpeg' ? 'jpg' : mime === 'svg+xml' ? 'svg' : mime || 'png'
  return `${caption}.${extension}`
}
