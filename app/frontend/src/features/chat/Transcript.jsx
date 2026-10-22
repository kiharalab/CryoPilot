// The scrolling conversation column. Renders the latest blocks (earlier ones
// on request), follows new output only while the reader is at the bottom,
// and offers a way back down otherwise.
import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react'
import { ArrowDown } from 'lucide-react'
import { Button } from '../../components/ui'
import { BlockView, gapBefore, isFinalProse } from './blocks'
import { PAGE_SIZE } from './transcriptModel'

/** Within this many pixels of the end counts as "at the bottom". */
const PIN_THRESHOLD = 80

function prefersReducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

/**
 * blocks   stable block objects (see transcript.js)
 * session  passed to the dataset block
 * active   the agent is working: the last block is live and prose there is not final
 * tail     nodes after the blocks (pending message, streaming answer, live line)
 * Ref: {scrollToBottom()}
 */
export const Transcript = forwardRef(function Transcript({ blocks, session, active = false, tail, children }, ref) {
  const scrollRef = useRef(null)
  const contentRef = useRef(null)
  const pinnedRef = useRef(true)
  const jumpingRef = useRef(false)
  const restoreRef = useRef(null)
  const [pinned, setPinned] = useState(true)
  const [limit, setLimit] = useState(PAGE_SIZE)
  // While the reader is scrolled up, the first rendered block stays put so
  // new output does not slide the window under them.
  const [frozenStart, setFrozenStart] = useState(null)

  const autoStart = Math.max(0, blocks.length - limit)
  const start = frozenStart === null ? autoStart : Math.min(frozenStart, autoStart)
  const startRef = useRef(start)
  useLayoutEffect(() => {
    startRef.current = start
  }, [start])

  const pin = useCallback((value) => {
    pinnedRef.current = value
    setPinned(value)
    setFrozenStart(value ? null : startRef.current)
  }, [])

  const scrollToBottom = useCallback(
    ({ smooth = false } = {}) => {
      const element = scrollRef.current
      if (!element) return
      pin(true)
      if (smooth && !prefersReducedMotion()) {
        jumpingRef.current = true
        window.setTimeout(() => {
          jumpingRef.current = false
        }, 900)
        element.scrollTo({ top: element.scrollHeight, behavior: 'smooth' })
      } else {
        element.scrollTop = element.scrollHeight
      }
    },
    [pin],
  )

  useImperativeHandle(ref, () => ({ scrollToBottom }), [scrollToBottom])

  const onScroll = () => {
    const element = scrollRef.current
    if (!element) return
    const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < PIN_THRESHOLD
    if (jumpingRef.current) {
      if (atBottom) jumpingRef.current = false
      return
    }
    if (atBottom !== pinnedRef.current) pin(atBottom)
  }

  // Keep the newest output in view while pinned: content growth (new blocks,
  // streaming text, images loading) and the column itself resizing.
  useEffect(() => {
    const element = scrollRef.current
    const content = contentRef.current
    if (!element || !content) return undefined
    const follow = () => {
      if (pinnedRef.current) element.scrollTop = element.scrollHeight
    }
    const observer = new ResizeObserver(follow)
    observer.observe(content)
    observer.observe(element)
    follow()
    return () => observer.disconnect()
  }, [])

  // After "Show earlier", keep the reader on the block they were looking at.
  useLayoutEffect(() => {
    const restore = restoreRef.current
    const element = scrollRef.current
    if (!restore || !element) return
    restoreRef.current = null
    element.scrollTop = restore.top + (element.scrollHeight - restore.height)
  }, [start])

  const showEarlier = () => {
    const element = scrollRef.current
    if (element) restoreRef.current = { height: element.scrollHeight, top: element.scrollTop }
    pinnedRef.current = false
    setPinned(false)
    setLimit((value) => value + PAGE_SIZE)
    setFrozenStart(Math.max(0, start - PAGE_SIZE))
  }

  const visible = start > 0 ? blocks.slice(start) : blocks
  const last = blocks.length - 1

  return (
    <div className="relative min-h-0 flex-1">
      <div ref={scrollRef} onScroll={onScroll} className="h-full overflow-y-auto [overflow-anchor:none]">
        <div ref={contentRef} className="mx-auto w-full max-w-[792px] px-4 pb-8 pt-6 sm:px-6">
          {start > 0 ? (
            <div className="mb-6 flex justify-center">
              <Button size="sm" variant="ghost" onClick={showEarlier}>
                Show earlier
              </Button>
            </div>
          ) : null}
          {visible.map((block, offset) => {
            const index = start + offset
            return (
              <div key={block.key} className={gapBefore(offset ? visible[offset - 1] : null, block)}>
                <BlockView
                  block={block}
                  live={active && index === last}
                  final={isFinalProse(block, blocks[index + 1]) && !(active && index === last)}
                  session={block.type === 'dataset' ? session : undefined}
                />
              </div>
            )
          })}
          {tail}
          {children}
        </div>
      </div>
      {!pinned && blocks.length ? (
        <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center">
          <button
            type="button"
            onClick={() => scrollToBottom({ smooth: true })}
            className="pointer-events-auto inline-flex h-8 items-center gap-1.5 rounded-full border border-line bg-elevated px-3 text-sm font-medium text-fg shadow-popover transition-colors hover:bg-hover"
          >
            <ArrowDown size={14} strokeWidth={1.75} />
            Jump to latest
          </button>
        </div>
      ) : null}
    </div>
  )
})
