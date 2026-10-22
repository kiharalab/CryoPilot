// Markdown for agent answers, guides, and .md previews. Code fences get the
// same highlighted CodeBlock as tool calls.
import { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { cx } from '../lib/cx'
import { CodeBlock } from './CodeBlock'

/**
 * Images load by themselves only from this server (or inline data). Agent text
 * could otherwise make the browser send data to any address just by showing it.
 */
function isLocalImage(src) {
  try {
    const url = new URL(src, window.location.href)
    return url.protocol === 'data:' || url.protocol === 'blob:' || url.origin === window.location.origin
  } catch {
    return false
  }
}

const components = {
  a: ({ href, children, node, ...props }) => (
    <a href={href} target="_blank" rel="noreferrer noopener" {...props}>
      {children}
    </a>
  ),
  img: ({ src, alt, node, ...props }) => {
    if (src && isLocalImage(src)) return <img src={src} alt={alt || ''} {...props} />
    if (!src) return alt ? <span>{alt}</span> : null
    return (
      <a href={src} target="_blank" rel="noreferrer noopener">
        {alt || src}
      </a>
    )
  },
  pre: ({ children }) => <>{children}</>,
  code: ({ className, children, node, ...props }) => {
    const match = /language-([\w-]+)/.exec(className || '')
    const text = String(children ?? '')
    const isBlock = Boolean(match) || text.includes('\n')
    if (!isBlock) {
      return (
        <code className={className} {...props}>
          {children}
        </code>
      )
    }
    return <CodeBlock code={text.replace(/\n$/, '')} language={match?.[1]} className="my-3" />
  },
}

export const Markdown = memo(function Markdown({ children, className, size = 'md' }) {
  return (
    <div className={cx('prose', size === 'sm' && 'prose-sm', className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children || ''}
      </ReactMarkdown>
    </div>
  )
})
