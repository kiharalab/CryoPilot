// Gates, in order: the access token, first-run setup (which starts with the
// license agreement), the license again for installs set up under an earlier
// license, then signing in to the services whose passwords are not
// remembered. After that, the workspace.
import { useCallback, useEffect, useState } from 'react'
import { api, onUnauthorized } from './lib/api'
import { useApp } from './store/app'
import { AccessGate } from './features/auth/AccessGate'
import { LicenseGate } from './features/setup/License'
import { Onboarding } from './features/setup/Onboarding'
import { SignIn } from './features/setup/SignIn'
import { Workspace } from './layout/Workspace'
import { BrandMark } from './layout/Brand'
import { Button } from './components/ui'
import { servicesNeedingSignIn } from './features/setup/services'

const SIGN_IN_SKIPPED_KEY = 'cryopilot-sign-in-skipped'

/** True when a service the user set up earlier needs its password again (it was not remembered). */
export function needsSignIn(setup) {
  return servicesNeedingSignIn(setup).length > 0
}

function signInSkipped() {
  try {
    return sessionStorage.getItem(SIGN_IN_SKIPPED_KEY) === '1'
  } catch {
    return false
  }
}

export function App() {
  const [auth, setAuth] = useState(null) // null while checking, then {enabled, method, authenticated}
  const [error, setError] = useState(null) // null, or the failed request's error
  // Whether to ask for passwords again. Decided when the settings load after
  // signing in to CryoPilot or after first-run setup, not on every change:
  // signing out of a service in Settings must not replace the workspace.
  const [signInOpen, setSignInOpen] = useState(null)
  const setup = useApp((state) => state.setup)
  const refreshSetup = useApp((state) => state.refreshSetup)
  const syncSystemTheme = useApp((state) => state.syncSystemTheme)

  if (signInOpen === null && setup?.onboarded) setSignInOpen(!signInSkipped() && needsSignIn(setup))

  const check = useCallback(async () => {
    setError(null)
    try {
      const status = await api.authStatus()
      if (status.authenticated) {
        const fresh = await refreshSetup()
        if (fresh.onboarded) setSignInOpen(!signInSkipped() && needsSignIn(fresh))
      }
      setAuth(status)
    } catch (err) {
      setError(err)
    }
  }, [refreshSetup])

  useEffect(() => {
    check()
  }, [check])

  // A link from an alert (?session=<id>) opens that session, once, after sign-in.
  useEffect(() => {
    if (!auth?.authenticated) return
    const url = new URL(window.location.href)
    const sessionId = url.searchParams.get('session')
    if (!sessionId) return
    url.searchParams.delete('session')
    window.history.replaceState(null, '', url)
    useApp.getState().openSession(sessionId)
  }, [auth?.authenticated])

  useEffect(() => onUnauthorized(() => setAuth((current) => (current ? { ...current, authenticated: false } : current))), [])

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    media.addEventListener('change', syncSystemTheme)
    return () => media.removeEventListener('change', syncSystemTheme)
  }, [syncSystemTheme])

  if (error) {
    // Status 0: the server did not answer at all, which the heading already says.
    const detail = error.status !== 0 && error.message ? error.message : ''
    return (
      <Centered>
        <p className="text-base font-medium">Cannot reach the CryoPilot server</p>
        {detail ? <p className="max-w-sm text-center text-sm text-muted">{detail}</p> : null}
        <p className="max-w-sm text-center text-sm text-muted">Check that CryoPilot is still running, then try again.</p>
        <Button onClick={check}>Try again</Button>
      </Centered>
    )
  }
  if (!auth || (auth.authenticated && !setup)) {
    return (
      <Centered>
        <BrandMark size={28} className="animate-pulse" />
      </Centered>
    )
  }
  if (!auth.authenticated) return <AccessGate method={auth.method} onDone={check} />
  if (!setup.onboarded) return <Onboarding onDone={refreshSetup} />
  if (!setup.license.accepted) return <LicenseGate onDone={refreshSetup} />
  if (signInOpen) {
    return (
      <SignIn
        onDone={async () => {
          await refreshSetup()
          setSignInOpen(false)
        }}
        onSkip={() => {
          try {
            sessionStorage.setItem(SIGN_IN_SKIPPED_KEY, '1')
          } catch {
            // not remembered in a private window
          }
          setSignInOpen(false)
        }}
      />
    )
  }
  return <Workspace />
}

function Centered({ children }) {
  return <div className="flex h-full flex-col items-center justify-center gap-3 bg-canvas p-6">{children}</div>
}
