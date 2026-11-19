import { useEffect, useState } from 'react'

/** The current time in ms, updated every `interval` ms. No interval: it stays put. */
export function useNow(interval) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!interval) return undefined
    const timer = window.setInterval(() => setNow(Date.now()), interval)
    return () => window.clearInterval(timer)
  }, [interval])
  return now
}
