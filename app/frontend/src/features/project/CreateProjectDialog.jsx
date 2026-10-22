// New project: a title, where CryoSPARC should create project folders (only
// reconstruction needs it), and an optional description.
import { useEffect, useState } from 'react'
import { useApp } from '../../store/app'
import { Button, Dialog, DialogClose, DialogContent, Field, InlineError, Input, Textarea } from '../../components/ui'
import { PathInput } from '../new/PathInput'

const PARENT_KEY = 'cryopilot-cryosparc-parent-directory'

function rememberedParent(projects) {
  try {
    const saved = localStorage.getItem(PARENT_KEY)
    if (saved) return saved
  } catch {
    // private window; fall back to other projects
  }
  return projects.find((project) => project.parent_directory)?.parent_directory || ''
}

export function CreateProjectDialog({ open, onOpenChange }) {
  const createProject = useApp((state) => state.createProject)
  const projects = useApp((state) => state.projects)
  const setup = useApp((state) => state.setup)
  const [title, setTitle] = useState('')
  const [parent, setParent] = useState('')
  const [description, setDescription] = useState('')
  const [error, setError] = useState('')
  const [titleError, setTitleError] = useState('')
  const [busy, setBusy] = useState(false)

  const reconstruction = Boolean(setup?.platform?.reconstruction_supported)

  useEffect(() => {
    if (!open) return
    setTitle('')
    setDescription('')
    setError('')
    setTitleError('')
    setBusy(false)
    setParent(rememberedParent(projects))
    // Only reset when the dialog opens; the projects list may change while it is open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const submit = async (event) => {
    event.preventDefault()
    if (busy) return
    if (!title.trim()) {
      setTitleError('Give the project a title')
      return
    }
    setBusy(true)
    setError('')
    try {
      const parentDirectory = reconstruction ? parent.trim().replace(/(.)\/+$/, '$1') : ''
      await createProject({ title: title.trim(), description: description.trim(), parent_directory: parentDirectory })
      if (parentDirectory) {
        try {
          localStorage.setItem(PARENT_KEY, parentDirectory)
        } catch {
          // not remembered in a private window
        }
      }
      onOpenChange(false)
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent
        title="New project"
        description="A project keeps the reconstruction and modeling sessions for one sample together, with the files they share."
        width="max-w-lg"
      >
        <form onSubmit={submit} className="flex flex-col gap-4 px-5 pb-5 pt-4">
          <Field label="Title" error={titleError}>
            {({ id }) => (
              <Input
                id={id}
                autoFocus
                value={title}
                invalid={Boolean(titleError)}
                onChange={(event) => {
                  setTitle(event.target.value)
                  setTitleError('')
                }}
                placeholder="HIV-1 Env trimer"
              />
            )}
          </Field>
          {reconstruction ? (
            <Field
              label="CryoSPARC project location"
              optional
              hint="The folder on this computer where CryoSPARC creates a project folder for each reconstruction. You can also set it when you start one."
            >
              {({ id }) => <PathInput id={id} dirsOnly value={parent} onChange={setParent} placeholder="/data/cryosparc/projects" />}
            </Field>
          ) : null}
          <Field label="Description" optional>
            {({ id }) => (
              <Textarea
                id={id}
                rows={3}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                placeholder="Sample, construct, or the question you are answering"
              />
            )}
          </Field>
          <InlineError>{error}</InlineError>
          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button variant="secondary" disabled={busy}>
                Cancel
              </Button>
            </DialogClose>
            <Button type="submit" variant="primary" loading={busy}>
              Create project
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}
