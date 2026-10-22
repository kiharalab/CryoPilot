// File roots of a project ({key, label, path, writable}), fetched once and
// shared by the explorer, the preview tabs, and the terminal's "New terminal
// in" menu.
import { useEffect } from 'react'
import { create } from 'zustand'
import { api } from '../../lib/api'

const pending = new Map()

export const useRootsStore = create((set, get) => ({
  /** projectId -> roots */
  roots: {},
  /** projectId -> error message */
  errors: {},

  async load(projectId, { force = false } = {}) {
    if (!projectId) return []
    if (!force && get().roots[projectId]) return get().roots[projectId]
    if (pending.has(projectId)) return pending.get(projectId)
    const request = api
      .fileRoots(projectId)
      .then((data) => {
        // The endpoint answers with the list itself; tolerate {roots: [...]} too.
        const roots = Array.isArray(data) ? data : data?.roots || []
        set((state) => ({ roots: { ...state.roots, [projectId]: roots }, errors: { ...state.errors, [projectId]: null } }))
        return roots
      })
      .catch((err) => {
        set((state) => ({ errors: { ...state.errors, [projectId]: err.message } }))
        throw err
      })
      .finally(() => pending.delete(projectId))
    pending.set(projectId, request)
    return request
  },
}))

/** Roots of a project, loaded on first use. `reload()` fetches them again. */
export function useProjectRoots(projectId) {
  const roots = useRootsStore((state) => (projectId ? state.roots[projectId] : null))
  const error = useRootsStore((state) => (projectId ? state.errors[projectId] : null))
  const load = useRootsStore((state) => state.load)

  useEffect(() => {
    if (projectId) load(projectId).catch(() => {})
  }, [projectId, load])

  return {
    roots: roots || null,
    error: error || null,
    reload: () => (projectId ? load(projectId, { force: true }) : Promise.resolve([])),
  }
}
