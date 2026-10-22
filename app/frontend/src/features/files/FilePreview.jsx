// A file opened in a workbench tab: highlighted text with line numbers,
// rendered Markdown, CSV/TSV tables, images, and a short card for models and
// maps (which belong in the viewer) or for files that cannot be shown.
import { memo, useCallback, useEffect, useMemo, useState } from 'react'
import { Atom, Copy, Download, Eye, File, RefreshCw, WrapText } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { basename, formatBytes, formatDateTime } from '../../lib/format'
import { highlight, languageForFile } from '../../lib/highlight'
import { useApp } from '../../store/app'
import { Button, EmptyState, IconButton, InlineError, Segmented, Skeleton } from '../../components/ui'
import { Markdown } from '../../components/Markdown'
import { useProjectRoots } from './roots'
import {
  absolutePath,
  downloadUrl,
  fileIcon,
  fileKind,
  isModelingInput,
  isViewable,
  parentPath,
  structureFormat,
  viewerRequestForFile,
} from './fileTypes'

const LINE_CHUNK = 2000
const ROW_CHUNK = 500

export function FilePreview({ file }) {
  const projectId = useApp((state) => state.projectId)
  const toast = useApp((state) => state.toast)
  const showInViewer = useApp((state) => state.showInViewer)
  const navigate = useApp((state) => state.navigate)
  const closeFile = useApp((state) => state.closeFile)
  const { roots } = useProjectRoots(projectId)
  const name = file.name || basename(file.path)
  const kind = fileKind(name)
  const url = api.fileUrl(projectId, file.root, file.path)
  const root = roots?.find((entry) => entry.key === file.root)

  // Size and modification time come from the parent folder's listing.
  const [meta, setMeta] = useState({ entry: null, missing: false })
  const [revision, setRevision] = useState(0)
  const [mode, setMode] = useState(kind === 'markdown' ? 'rendered' : kind === 'structure' ? 'card' : 'text')
  const [wrap, setWrap] = useState(false)
  const [imageSize, setImageSize] = useState(null)

  useEffect(() => {
    let cancelled = false
    api
      .listDir(projectId, file.root, parentPath(file.path))
      .then((data) => {
        if (cancelled) return
        const entries = data.entries || []
        const entry = entries.find((item) => item.path === file.path) || entries.find((item) => item.name === name) || null
        setMeta({ entry, missing: !entry })
      })
      .catch(() => {
        if (!cancelled) setMeta({ entry: null, missing: false })
      })
    return () => {
      cancelled = true
    }
  }, [projectId, file.root, file.path, name, revision])

  const copyPath = async () => {
    const path = absolutePath(root?.path, file.path)
    try {
      await navigator.clipboard.writeText(path)
      toast({ title: 'Path copied' })
    } catch {
      toast({ title: 'Could not copy the path', description: path, tone: 'error' })
    }
  }
  const openInViewer = () => showInViewer(viewerRequestForFile(url, file.root, file.path, name))
  const attachToModeling = () =>
    navigate({ name: 'new-modeling', attachments: [{ type: 'file', root: file.root, path: file.path, label: name }] })
  const download = () => downloadUrl(url, name)

  const textMode = kind === 'text' || (kind === 'markdown' && mode === 'source') || (kind === 'structure' && mode === 'text')
  const dir = parentPath(file.path)

  if (meta.missing) {
    return (
      <EmptyState
        icon={File}
        title="This file is no longer here"
        action={
          <Button size="sm" onClick={() => closeFile(file)}>
            Close tab
          </Button>
        }
      >
        It was moved or deleted. Refresh the Files tab to see the folder as it is now.
      </EmptyState>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-2 border-b border-line pl-3 pr-1.5">
        <p className="min-w-0 flex-1 truncate text-sm" title={absolutePath(root?.path, file.path)}>
          {dir ? <span className="text-muted">{dir}/</span> : null}
          <span className="font-medium text-fg">{name}</span>
        </p>
        <span className="hidden shrink-0 gap-3 text-xs tabular-nums text-faint sm:flex">
          {imageSize ? <span>{`${imageSize.width} × ${imageSize.height}`}</span> : null}
          {meta.entry?.size !== undefined && meta.entry?.size !== null ? <span>{formatBytes(meta.entry.size)}</span> : null}
        </span>
        {kind === 'markdown' ? (
          <Segmented
            size="sm"
            ariaLabel="Markdown display"
            value={mode}
            onChange={setMode}
            options={[
              { value: 'rendered', label: 'Preview' },
              { value: 'source', label: 'Source' },
            ]}
          />
        ) : null}
        {kind === 'structure' && !name.toLowerCase().endsWith('.gz') ? (
          <Segmented
            size="sm"
            ariaLabel="Model display"
            value={mode}
            onChange={setMode}
            options={[
              { value: 'card', label: 'Overview' },
              { value: 'text', label: 'Text' },
            ]}
          />
        ) : null}
        <div className="flex shrink-0 items-center gap-0.5">
          {textMode ? <IconButton icon={WrapText} label={wrap ? 'Do not wrap lines' : 'Wrap lines'} size="sm" active={wrap} onClick={() => setWrap((value) => !value)} /> : null}
          {isViewable(name) ? <IconButton icon={Eye} label="Open in viewer" size="sm" onClick={openInViewer} /> : null}
          {isModelingInput(name) && kind !== 'structure' && kind !== 'volume' ? (
            <IconButton icon={Atom} label="Use for modeling" size="sm" onClick={attachToModeling} />
          ) : null}
          <IconButton icon={Copy} label="Copy path" size="sm" onClick={copyPath} />
          <IconButton icon={Download} label="Download" size="sm" onClick={download} />
          <IconButton icon={RefreshCw} label="Reload" size="sm" onClick={() => setRevision((value) => value + 1)} />
        </div>
      </div>

      {kind === 'image' ? (
        <ImageBody key={revision} url={revision ? `${url}&v=${revision}` : url} name={name} onSize={setImageSize} />
      ) : kind === 'table' ? (
        <TableBody key={revision} projectId={projectId} file={file} onDownload={download} />
      ) : kind === 'volume' || (kind === 'structure' && mode === 'card') ? (
        <ModelCard
          name={name}
          kind={kind}
          entry={meta.entry}
          onView={isViewable(name) ? openInViewer : null}
          onUse={attachToModeling}
          onDownload={download}
        />
      ) : kind === 'binary' ? (
        <BinaryCard name={name} size={meta.entry?.size} onDownload={download} />
      ) : (
        <TextBody
          key={revision}
          projectId={projectId}
          file={file}
          name={name}
          wrap={wrap}
          rendered={kind === 'markdown' && mode === 'rendered'}
          onDownload={download}
        />
      )}
    </div>
  )
}

// ------------------------------------------------------------------ text
/** NUL bytes or many replacement characters mean the server decoded a binary file. */
function looksBinary(text) {
  const sample = text.slice(0, 8000)
  if (!sample) return false
  if (sample.includes('\u0000')) return true
  let replaced = 0
  for (const character of sample) if (character === '�') replaced += 1
  return replaced / sample.length > 0.02
}

/** One-line JSON is pretty-printed; anything else is shown as written. */
function displayText(text, name) {
  if (!name.toLowerCase().endsWith('.json') || text.length > 2_000_000) return text
  const lines = text.split('\n', 4).length
  if (lines > 3) return text
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

/**
 * Splits highlight.js HTML into one string per line, closing the spans that
 * are open at each line break and reopening them on the next line, so every
 * line renders on its own next to its number.
 */
function splitHighlighted(html) {
  const lines = []
  const open = []
  let current = ''
  const pattern = /(<span[^>]*>)|(<\/span>)|(\n)|([^<\n]+)/g
  let match
  while ((match = pattern.exec(html))) {
    if (match[1]) {
      open.push(match[1])
      current += match[1]
    } else if (match[2]) {
      open.pop()
      current += match[2]
    } else if (match[3]) {
      lines.push(current + '</span>'.repeat(open.length))
      current = open.join('')
    } else {
      current += match[4]
    }
  }
  lines.push(current + '</span>'.repeat(open.length))
  return lines
}

function useFileText(projectId, file) {
  const [state, setState] = useState({ loading: true, error: null, data: null })
  const load = useCallback(() => {
    let cancelled = false
    setState((current) => ({ ...current, loading: true, error: null }))
    api
      .fileText(projectId, file.root, file.path)
      .then((data) => {
        if (!cancelled) setState({ loading: false, error: null, data })
      })
      .catch((err) => {
        if (!cancelled) setState({ loading: false, error: err.message, data: null })
      })
    return () => {
      cancelled = true
    }
  }, [projectId, file.root, file.path])
  useEffect(() => load(), [load])
  return { ...state, reload: load }
}

function TextBody({ projectId, file, name, wrap, rendered, onDownload }) {
  const { loading, error, data, reload } = useFileText(projectId, file)
  const text = data?.text ?? ''
  const binary = useMemo(() => Boolean(data?.binary) || looksBinary(text), [data, text])

  if (loading && !data) return <LoadingLines />
  if (error) {
    return (
      <div className="p-4">
        <InlineError>{error}</InlineError>
        <Button size="sm" variant="ghost" className="mt-2" icon={RefreshCw} onClick={reload}>
          Try again
        </Button>
      </div>
    )
  }
  if (binary) return <BinaryCard name={name} size={data?.size} onDownload={onDownload} />

  const truncatedNote = data?.truncated ? (
    <div className="flex shrink-0 items-center gap-3 border-t border-line px-3 py-2 text-sm text-muted">
      <span className="min-w-0 flex-1">
        Showing the first {formatBytes(text.length)}
        {data.size ? ` of ${formatBytes(data.size)}` : ''}. Download the file to read all of it.
      </span>
      <Button size="xs" icon={Download} onClick={onDownload}>
        Download
      </Button>
    </div>
  ) : null

  if (rendered) {
    return (
      <>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-3xl px-6 py-6">
            <Markdown>{text}</Markdown>
          </div>
        </div>
        {truncatedNote}
      </>
    )
  }

  if (!text) {
    return <EmptyState title="This file is empty" />
  }

  return (
    <>
      <CodeLines text={displayText(text, name)} language={languageForFile(name)} wrap={wrap} />
      {truncatedNote}
    </>
  )
}

const CodeLines = memo(function CodeLines({ text, language, wrap }) {
  const lines = useMemo(() => splitHighlighted(highlight(text, language)), [text, language])
  const [count, setCount] = useState(LINE_CHUNK)
  const gutter = `${String(lines.length).length + 2}ch`
  const shown = lines.length > count ? lines.slice(0, count) : lines

  return (
    <div className="min-h-0 flex-1 overflow-auto">
      <div className={cx('py-2 font-mono text-[12.5px] leading-5 [tab-size:4]', !wrap && 'w-max min-w-full')}>
        {shown.map((html, index) => (
          <div key={index} className="flex">
            <span className="sticky left-0 shrink-0 select-none bg-canvas pr-4 text-right text-faint" style={{ width: gutter }}>
              {index + 1}
            </span>
            <span
              className={cx('pr-4', wrap ? 'min-w-0 flex-1 whitespace-pre-wrap break-words' : 'whitespace-pre')}
              dangerouslySetInnerHTML={{ __html: html }}
            />
          </div>
        ))}
        {lines.length > count ? (
          <div className="sticky left-0 flex items-center gap-3 px-4 py-3 font-sans text-sm text-muted">
            <span>
              {lines.length - count} more {lines.length - count === 1 ? 'line' : 'lines'}
            </span>
            <Button size="xs" onClick={() => setCount((value) => value + LINE_CHUNK)}>
              Show {Math.min(LINE_CHUNK, lines.length - count)} more
            </Button>
            <Button size="xs" variant="ghost" onClick={() => setCount(lines.length)}>
              Show all
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  )
})

// ----------------------------------------------------------------- table
/** A column is numeric when nearly all of its non-empty cells parse as numbers. */
function numericColumns(columns, rows) {
  const sample = rows.slice(0, 300)
  return columns.map((_, column) => {
    let filled = 0
    let numeric = 0
    for (const row of sample) {
      const cell = row[column]
      if (cell === null || cell === undefined || cell === '') continue
      filled += 1
      if (typeof cell === 'number' || (String(cell).trim() !== '' && Number.isFinite(Number(cell))) || /^(nan|-?inf)$/i.test(String(cell).trim())) numeric += 1
    }
    return filled > 0 && numeric / filled >= 0.9
  })
}

function TableBody({ projectId, file, onDownload }) {
  const [state, setState] = useState({ loading: true, error: null, data: null })
  const [count, setCount] = useState(ROW_CHUNK)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let cancelled = false
    setState((current) => ({ ...current, loading: true, error: null }))
    api
      .fileTable(projectId, file.root, file.path)
      .then((data) => {
        if (!cancelled) setState({ loading: false, error: null, data })
      })
      .catch((err) => {
        if (!cancelled) setState({ loading: false, error: err.message, data: null })
      })
    return () => {
      cancelled = true
    }
  }, [projectId, file.root, file.path, attempt])

  const columns = useMemo(() => state.data?.columns || [], [state.data])
  const rows = useMemo(() => state.data?.rows || [], [state.data])
  const numeric = useMemo(() => numericColumns(columns, rows), [columns, rows])

  if (state.loading && !state.data) return <LoadingLines />
  if (state.error) {
    return (
      <div className="p-4">
        <InlineError>{state.error}</InlineError>
        <Button size="sm" variant="ghost" className="mt-2" icon={RefreshCw} onClick={() => setAttempt((value) => value + 1)}>
          Try again
        </Button>
      </div>
    )
  }
  if (!columns.length) return <EmptyState title="This table is empty" />

  const shown = rows.slice(0, count)
  const total = state.data?.total_rows
  let summary = `${shown.length.toLocaleString()} of ${rows.length.toLocaleString()} rows`
  if (state.data?.truncated) {
    summary =
      typeof total === 'number'
        ? `${shown.length.toLocaleString()} of ${total.toLocaleString()} rows; the preview holds the first ${rows.length.toLocaleString()}`
        : `${shown.length.toLocaleString()} of the first ${rows.length.toLocaleString()} rows`
  }

  return (
    <>
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="min-w-full border-separate border-spacing-0 text-sm">
          <thead>
            <tr>
              <th className="sticky left-0 top-0 z-20 border-b border-line bg-canvas px-3 py-1.5 text-right text-xs font-normal text-faint">
                <span className="sr-only">Row</span>
              </th>
              {columns.map((column, index) => (
                <th
                  key={`${column}-${index}`}
                  scope="col"
                  className={cx(
                    'sticky top-0 z-10 whitespace-nowrap border-b border-line bg-canvas px-3 py-1.5 font-medium text-muted',
                    numeric[index] ? 'text-right' : 'text-left',
                  )}
                >
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, rowIndex) => (
              <tr key={rowIndex} className="hover:bg-hover">
                <td className="sticky left-0 border-b border-line bg-canvas px-3 py-1 text-right text-xs tabular-nums text-faint">{rowIndex + 1}</td>
                {columns.map((_, index) => (
                  <td
                    key={index}
                    className={cx(
                      'whitespace-nowrap border-b border-line px-3 py-1',
                      numeric[index] ? 'text-right font-mono text-[12.5px] tabular-nums' : 'text-left',
                    )}
                  >
                    {row[index] ?? ''}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex shrink-0 items-center gap-3 border-t border-line px-3 py-2 text-sm text-muted">
        <span className="min-w-0 flex-1">{summary}</span>
        {rows.length > count ? (
          <Button size="xs" onClick={() => setCount((value) => value + ROW_CHUNK)}>
            Show {Math.min(ROW_CHUNK, rows.length - count)} more
          </Button>
        ) : null}
        {state.data?.truncated ? (
          <Button size="xs" variant="ghost" icon={Download} onClick={onDownload}>
            Download
          </Button>
        ) : null}
      </div>
    </>
  )
}

// ----------------------------------------------------------------- image
function ImageBody({ url, name, onSize }) {
  const [actual, setActual] = useState(false)
  const [failed, setFailed] = useState(false)

  useEffect(() => () => onSize(null), [onSize])

  if (failed) {
    return (
      <EmptyState icon={File} title="The image could not be loaded">
        The file may be damaged or in a format this browser cannot show.
      </EmptyState>
    )
  }
  return (
    <div className={cx('min-h-0 flex-1 overflow-auto bg-inset p-4', !actual && 'flex items-center justify-center')}>
      <img
        src={url}
        alt={name}
        onLoad={(event) => onSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })}
        onError={() => setFailed(true)}
        onClick={() => setActual((value) => !value)}
        className={cx('rounded-md', actual ? 'max-w-none cursor-zoom-out' : 'max-h-full max-w-full cursor-zoom-in object-contain')}
      />
    </div>
  )
}

// ----------------------------------------------------------------- cards
function ModelCard({ name, kind, entry, onView, onUse, onDownload }) {
  const Icon = fileIcon(name)
  const compressed = name.toLowerCase().endsWith('.gz')
  const description =
    kind === 'structure'
      ? `Atomic model, ${structureFormat(name) === 'mmcif' ? 'mmCIF' : 'PDB'} format`
      : 'Density map'
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center overflow-y-auto p-6">
      <div className="w-full max-w-sm rounded-xl border border-line bg-elevated p-5">
        <div className="flex items-start gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent-text">
            <Icon size={18} strokeWidth={1.75} />
          </span>
          <div className="min-w-0">
            <p className="truncate text-base font-medium text-fg" title={name}>
              {name}
            </p>
            <p className="text-sm text-muted">
              {description}
              {entry?.size !== undefined && entry?.size !== null ? `, ${formatBytes(entry.size)}` : ''}
            </p>
            {entry?.modified ? <p className="mt-0.5 text-xs text-faint">Modified {formatDateTime(entry.modified)}</p> : null}
          </div>
        </div>
        {compressed ? <p className="mt-3 text-sm text-muted">This copy is compressed. Download it and decompress it to open it in the viewer.</p> : null}
        <div className="mt-4 flex flex-wrap gap-2">
          {onView ? (
            <Button variant="primary" size="sm" icon={Eye} onClick={onView}>
              Open in viewer
            </Button>
          ) : null}
          <Button size="sm" icon={Atom} onClick={onUse}>
            Use for modeling
          </Button>
          <Button size="sm" variant="ghost" icon={Download} onClick={onDownload}>
            Download
          </Button>
        </div>
      </div>
    </div>
  )
}

function BinaryCard({ name, size, onDownload }) {
  return (
    <EmptyState
      icon={File}
      title="No preview for this file type"
      action={
        <Button size="sm" icon={Download} onClick={onDownload}>
          Download
        </Button>
      }
    >
      {name}
      {size !== undefined && size !== null ? ` is ${formatBytes(size)}.` : '.'} Download it to open it with another program.
    </EmptyState>
  )
}

const SKELETON_WIDTHS = [72, 56, 88, 40, 64, 80, 48]

function LoadingLines() {
  return (
    <div className="flex flex-col gap-2.5 p-4">
      {SKELETON_WIDTHS.map((width, index) => (
        <div key={index} style={{ width: `${width}%` }}>
          <Skeleton className="h-3" />
        </div>
      ))}
    </div>
  )
}
