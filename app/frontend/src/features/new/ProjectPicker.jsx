// The CryoSPARC project a new reconstruction continues: a UID field with a
// menu of the signed-in account's projects. Typing a UID still works when the
// list cannot be loaded.
import { useEffect, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { api } from '../../lib/api'
import { formatRelative } from '../../lib/format'
import { Input, Menu, MenuContent, MenuItem, MenuLabel, MenuTrigger } from '../../components/ui'

/** The account's projects, newest first: {projects: null while loading, error}. */
export function useCryosparcProjects(enabled) {
  const [state, setState] = useState({ projects: null, error: '' })
  useEffect(() => {
    if (!enabled) return undefined
    let cancelled = false
    setState({ projects: null, error: '' })
    api
      .cryosparcProjects()
      .then((data) => !cancelled && setState({ projects: data.projects || [], error: '' }))
      .catch((err) => !cancelled && setState({ projects: [], error: err.message }))
    return () => {
      cancelled = true
    }
  }, [enabled])
  return state
}

/** The project in `projects` with this UID (any case, spaces ignored). */
export function findProject(projects, uid) {
  const wanted = String(uid || '').trim().toUpperCase()
  return wanted ? (projects || []).find((project) => project.uid === wanted) : undefined
}

/**
 * Props: id, value (project UID), projects (from useCryosparcProjects),
 * invalid, onChange(uid, title), and the field attributes Input takes.
 */
export function ProjectPicker({ id, value, projects, invalid, onChange, ...field }) {
  const selected = findProject(projects, value)
  return (
    <div className="relative">
      <Input
        id={id}
        mono
        autoComplete="off"
        spellCheck={false}
        placeholder="P12"
        value={value}
        invalid={invalid}
        onChange={(event) => onChange(event.target.value, findProject(projects, event.target.value)?.title || '')}
        className="pr-9"
        {...field}
      />
      <Menu>
        <MenuTrigger asChild>
          <button
            type="button"
            aria-label="Your CryoSPARC projects"
            disabled={!projects?.length}
            className="absolute right-1 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md text-faint hover:bg-hover hover:text-fg disabled:pointer-events-none disabled:opacity-40 data-[state=open]:bg-hover"
          >
            <ChevronDown size={14} strokeWidth={1.75} />
          </button>
        </MenuTrigger>
        <MenuContent align="end" className="w-[min(340px,calc(100vw-32px))]">
          <MenuLabel>Your CryoSPARC projects</MenuLabel>
          <div className="max-h-[300px] overflow-y-auto">
            {(projects || []).map((project) => (
              <MenuItem
                key={project.uid}
                checked={project.uid === selected?.uid}
                hint={project.created_at ? formatRelative(project.created_at) : undefined}
                onSelect={() => onChange(project.uid, project.title)}
              >
                <span className="flex min-w-0 items-baseline gap-2">
                  <span className="shrink-0 font-mono text-[12.5px]">{project.uid}</span>
                  <span className="truncate">{project.title}</span>
                </span>
              </MenuItem>
            ))}
          </div>
        </MenuContent>
      </Menu>
    </div>
  )
}
