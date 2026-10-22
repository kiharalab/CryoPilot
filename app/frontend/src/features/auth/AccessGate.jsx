// Shown when this browser has no access token. CryoPilot prints a link with
// the token when it starts; opening that link signs in automatically, and
// pasting it (or just the token) here does the same.
import { useState } from 'react'
import { api } from '../../lib/api'
import { Button, Field, Input } from '../../components/ui'
import { BrandMark } from '../../layout/Brand'
import { Code, SetupPage } from '../setup/SetupPage'

export function AccessGate({ onDone }) {
  const [value, setValue] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const port = window.location.port || '8790'

  const submit = async (event) => {
    event.preventDefault()
    const token = value.trim()
    if (!token) {
      setError('Paste the access link or the token.')
      return
    }
    setBusy(true)
    setError('')
    try {
      await api.login(token)
      await onDone()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <SetupPage width="max-w-[420px]" centered>
      <BrandMark size={36} />
      <h1 className="mt-6 text-xl font-semibold tracking-[-0.01em]">Sign in to CryoPilot</h1>
      <p className="mt-1.5 text-sm text-muted">Paste the access link that was printed in the terminal where CryoPilot started.</p>

      <form onSubmit={submit} className="mt-7 flex flex-col gap-4" noValidate>
        <Field label="Access link or token" error={error}>
          {({ id }) => (
            <Input
              id={id}
              value={value}
              onChange={(event) => {
                setValue(event.target.value)
                setError('')
              }}
              invalid={Boolean(error)}
              placeholder={`http://localhost:${port}/?token=...`}
              autoComplete="off"
              spellCheck={false}
              autoFocus
              mono
            />
          )}
        </Field>
        <Button type="submit" variant="primary" size="lg" loading={busy} className="w-full">
          Sign in
        </Button>
      </form>

      <div className="mt-8 flex flex-col gap-3 border-t border-line pt-6 text-sm text-muted">
        <p>
          <Code>python app/serve.py</Code> and <Code>cryopilot</Code> print the link when they start. Set <Code>CRYOPILOT_TOKEN</Code> to keep the same link
          across restarts.
        </p>
        <p>
          From another computer, forward the port first with <Code>{`ssh -L ${port}:localhost:${port} you@server`}</Code>, then open the link.
        </p>
      </div>
    </SetupPage>
  )
}
