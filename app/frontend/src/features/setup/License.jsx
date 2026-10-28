// The license agreement: the first step of setup, and a page of its own when
// an install set up under an earlier license opens this version. Agreeing is
// saved on the server, so on a shared server one person agrees for everyone.
import { useEffect, useState } from 'react'
import { Info } from 'lucide-react'
import { api } from '../../lib/api'
import { useApp } from '../../store/app'
import { InlineError, Skeleton } from '../../components/ui'
import { Markdown } from '../../components/Markdown'
import { BrandMark } from '../../layout/Brand'
import { Code, SetupPage, StepFooter, StepLayout } from './SetupPage'

const LICENSE_URL = 'https://polyformproject.org/licenses/noncommercial/1.0.0'

function Contact() {
  return (
    <>
      Daisuke Kihara (
      <a href="mailto:dkihara@purdue.edu" className="text-accent-text hover:underline">
        dkihara@purdue.edu
      </a>
      )
    </>
  )
}

export function LicenseStep({ onBack, onDone, changed = false }) {
  const license = useApp((state) => state.setup?.license)
  const setSetup = useApp((state) => state.setSetup)
  const [text, setText] = useState(null)
  const [loadError, setLoadError] = useState('')
  const [declined, setDeclined] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    api
      .license()
      .then((result) => !cancelled && setText(result.text))
      .catch((err) => !cancelled && setLoadError(err.message))
    return () => {
      cancelled = true
    }
  }, [])

  const agree = async () => {
    if (license?.accepted) {
      onDone()
      return
    }
    setBusy(true)
    setError('')
    try {
      setSetup(await api.acceptLicense())
      await onDone()
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  if (declined) {
    return (
      <StepLayout
        title="You did not agree to the license"
        description="CryoPilot can only be used under its license."
        footer={<StepFooter onPrimary={() => setDeclined(false)} primaryLabel="Read the license again" />}
      >
        <p className="text-sm text-muted">
          For commercial use, contact <Contact />.
        </p>
        <p className="mt-3 text-sm text-muted">
          Otherwise, close this tab. To stop CryoPilot, press Ctrl-C in the terminal where it runs, or run <Code>cryopilot stop</Code> if it runs in the
          background.
        </p>
      </StepLayout>
    )
  }

  return (
    <StepLayout
      title="License agreement"
      description={
        changed
          ? 'This version of CryoPilot has a new license, the PolyForm Noncommercial License 1.0.0. Read the terms, then agree to them to keep using CryoPilot.'
          : 'CryoPilot is free for noncommercial use under the PolyForm Noncommercial License 1.0.0. Read the terms, then agree to them to continue.'
      }
      footer={
        <StepFooter
          onBack={onBack}
          onSkip={license?.accepted ? undefined : () => setDeclined(true)}
          skipLabel="Disagree"
          onPrimary={agree}
          primaryLabel={license?.accepted ? 'Continue' : 'Agree'}
          busy={busy || (text === null && !loadError)}
        />
      }
    >
      <div className="flex items-start gap-2.5 rounded-xl bg-subtle px-3.5 py-3 text-sm text-muted">
        <Info size={15} strokeWidth={1.75} className="mt-0.5 shrink-0 text-faint" />
        <p>
          Academic research, teaching, and personal study are noncommercial. Universities, research institutes, and government labs may use CryoPilot
          whatever their funding. For commercial use, contact <Contact />.
        </p>
      </div>

      <div tabIndex={0} aria-label="License terms" className="mt-4 h-[min(420px,50vh)] overflow-y-auto rounded-xl border border-line px-5 py-4">
        {loadError ? (
          <InlineError>
            Could not load the license: {loadError}. The terms are at{' '}
            <a href={LICENSE_URL} target="_blank" rel="noreferrer noopener" className="underline">
              polyformproject.org
            </a>
            .
          </InlineError>
        ) : text === null ? (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-5 w-2/3" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-11/12" />
            <Skeleton className="h-4 w-4/5" />
          </div>
        ) : (
          <Markdown size="sm">{text}</Markdown>
        )}
      </div>

      {license?.accepted ? (
        <p className="mt-3 text-sm text-muted">You agreed to this license{license.accepted_at ? ` on ${new Date(license.accepted_at).toLocaleDateString()}` : ''}.</p>
      ) : null}
      {error ? <InlineError className="mt-4">{error}</InlineError> : null}
    </StepLayout>
  )
}

/** For installs set up under an earlier license: the agreement alone, before the workspace. */
export function LicenseGate({ onDone }) {
  return (
    <SetupPage width="max-w-[600px]">
      <div className="flex items-center gap-2.5">
        <BrandMark size={28} />
        <span className="text-base font-semibold tracking-[-0.01em]">CryoPilot</span>
      </div>
      <LicenseStep onDone={onDone} changed />
    </SetupPage>
  )
}
