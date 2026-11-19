// The message box under a conversation. Sends follow-up messages (with
// attachments for the modeling agent), stops a running turn, and explains
// what is missing when the server refuses a message.
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { CircleAlert, X } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { useApp } from '../../store/app'
import { Composer } from '../../components/Composer'
import { Button, IconButton, Tip } from '../../components/ui'
import { AttachMenu, attachmentChip } from '../attachments/AttachMenu'
import { effortLabel } from '../new/AgentChips'
import { SpendingControl } from './SpendingControl'

/** The button that fixes a missing credential, by the service the server names in `err.setup`. */
export const SETUP_ACTIONS = {
  openai: 'Add OpenAI key',
  anthropic: 'Add Anthropic key',
  cryosparc: 'Sign in to CryoSPARC',
  cryozeta: 'Sign in to CryoZeta',
}

function draftKey(sessionId) {
  return `cryopilot-draft:${sessionId}`
}

function readDraft(sessionId) {
  try {
    return localStorage.getItem(draftKey(sessionId)) || ''
  } catch {
    return ''
  }
}

function writeDraft(sessionId, text) {
  try {
    if (text) localStorage.setItem(draftKey(sessionId), text)
    else localStorage.removeItem(draftKey(sessionId))
  } catch {
    // private window; the draft is simply not kept
  }
}

function placeholderFor(kind, { active, paused, alive, resumable, reconstructionSupported }) {
  if (paused) return 'Paused at the spending limit'
  if (active) return 'The agent is working. Stop it to send a new message.'
  if (!alive) {
    if (kind === 'reconstruction' && reconstructionSupported === false) {
      return 'The reconstruction agent resumes only on the Linux computer where CryoSPARC runs'
    }
    // Starting the agent first finishes the interrupted turn; a message waits for it.
    if (resumable) return 'Resume the unfinished turn, or send a message to run after it'
    return 'The agent is stopped. Send a message to start it again.'
  }
  return kind === 'reconstruction'
    ? 'Ask about a job, change the strategy, or tell the agent to continue'
    : 'Ask the modeling agent to build, fit, validate, or refine'
}

/** The attachment as api.js documents it; the label is only for chips. */
function attachmentPayload(attachment) {
  if (attachment.type === 'cryosparc_map') {
    const { type, session_id: sessionId, job_uid: jobUid, name } = attachment
    return { type, session_id: sessionId, job_uid: jobUid, name }
  }
  if (attachment.type === 'file') return { type: attachment.type, root: attachment.root, path: attachment.path }
  return attachment
}

/** {model, effort} the session runs with; either may be empty. */
function modelChoice(session) {
  const config = session.config || {}
  if (session.kind === 'modeling') return { model: config.model, effort: config.reasoning_effort }
  return {
    model: session.harness?.model || config.agent?.model,
    effort: session.harness?.reasoning?.effort || config.agent?.reasoning_effort,
  }
}

/**
 * session, active (a turn is running), paused (a turn is held at the spending limit), alive (the agent process is up),
 * resumable (a stopped reconstruction can finish its interrupted turn),
 * userCount (user messages in the transcript so far), onStop(), and
 * onSent({text, attachments, baseline}) after the server accepted a message;
 * baseline is userCount from before sending, since the transcript may record
 * the message before the request returns.
 * Ref: {focus()}
 */
export const ChatComposer = forwardRef(function ChatComposer(
  { session, active, paused = false, alive, resumable = false, stopping, userCount = 0, onSent, onStop },
  ref,
) {
  const patchSession = useApp((state) => state.patchSession)
  const openSettings = useApp((state) => state.openSettings)
  const reconstructionSupported = useApp((state) => state.setup?.platform?.reconstruction_supported)
  const [draft, setDraft] = useState(() => readDraft(session.id))
  const [attachments, setAttachments] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [spendingOpen, setSpendingOpen] = useState(false)
  const composerRef = useRef(null)
  const modeling = session.kind === 'modeling'

  useImperativeHandle(ref, () => ({ focus: () => composerRef.current?.focus() }), [])

  useEffect(() => {
    writeDraft(session.id, draft)
  }, [session.id, draft])

  const chips = attachments.map((attachment) => attachmentChip(attachment))

  const addAttachment = (attachment) => {
    const key = attachmentChip(attachment).key
    setAttachments((current) => (current.some((entry) => attachmentChip(entry).key === key) ? current : [...current, attachment]))
  }

  const removeAttachment = (key) => {
    setAttachments((current) => current.filter((entry) => attachmentChip(entry).key !== key))
  }

  const submit = async () => {
    const text = draft.trim()
    if ((!text && !(modeling && attachments.length)) || busy) return
    const baseline = userCount
    const sessionId = session.id
    setBusy(true)
    setError(null)
    try {
      const updated = await api.sendMessage(sessionId, text, modeling ? attachments.map(attachmentPayload) : [])
      if (updated?.id) patchSession(updated)
      onSent?.({ text, attachments, baseline })
      // Cleared in storage directly too: if the user has left this session
      // meanwhile, the composer is gone and its draft effect will not run.
      writeDraft(sessionId, '')
      setDraft('')
      setAttachments([])
    } catch (err) {
      setError({ message: err.message, setup: err.setup, spendingLimit: Boolean(err.data?.spending_limit) })
    } finally {
      setBusy(false)
    }
  }

  const { model, effort } = modelChoice(session)

  return (
    <div className="w-full">
      {error ? (
        <div className="mb-2 flex items-start gap-2.5 rounded-xl border border-line bg-elevated px-3.5 py-2.5 shadow-popover">
          <CircleAlert size={15} strokeWidth={1.75} className="mt-0.5 shrink-0 text-danger" />
          <p className="min-w-0 flex-1 text-sm text-fg">{error.message}</p>
          {error.setup ? (
            <Button size="sm" variant="secondary" onClick={() => openSettings('accounts')} className="-my-0.5">
              {SETUP_ACTIONS[error.setup] || 'Open settings'}
            </Button>
          ) : null}
          {error.spendingLimit ? (
            <Button size="sm" variant="secondary" onClick={() => setSpendingOpen(true)} className="-my-0.5">
              Change the limit
            </Button>
          ) : null}
          <IconButton icon={X} label="Dismiss" size="xs" onClick={() => setError(null)} className="-mr-1" />
        </div>
      ) : null}
      <Composer
        ref={composerRef}
        value={draft}
        onChange={setDraft}
        onSubmit={submit}
        canSubmit={Boolean(draft.trim()) || (modeling && attachments.length > 0)}
        running={active || paused}
        onStop={stopping ? undefined : onStop}
        busy={busy}
        autoFocus
        attachments={chips}
        onRemoveAttachment={modeling ? removeAttachment : undefined}
        placeholder={placeholderFor(session.kind, { active, paused, alive, resumable, reconstructionSupported })}
        toolbarLeft={modeling ? <AttachMenu projectId={session.project_id} onAttach={addAttachment} disabled={busy} /> : null}
        toolbarRight={
          <>
            <SpendingControl session={session} open={spendingOpen} onOpenChange={setSpendingOpen} />
            {model || effort ? (
              <Tip label="Model and reasoning effort for this session">
                <span className={cx('mr-1 inline-flex min-w-0 select-none items-center gap-1.5 text-xs text-faint', !modeling && 'pl-2')}>
                  {model ? <span className="truncate">{model}</span> : null}
                  {effort ? <span className="shrink-0">{effortLabel(effort)}</span> : null}
                </span>
              </Tip>
            ) : null}
          </>
        }
      />
    </div>
  )
})
