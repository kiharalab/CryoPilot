// Right side of the window: the workbench tabs on top, the terminal and
// agent logs underneath. Fixed tabs stay mounted while hidden so Mol* scenes,
// scroll positions, and terminals survive switching tabs.
import { Box, FileText, Files, FlaskConical, Layers, ScrollText, SquareTerminal, X } from 'lucide-react'
import { useApp } from '../store/app'
import { IconButton, TabStrip } from '../components/ui'
import { cx } from '../lib/cx'
import { basename } from '../lib/format'
import { ViewerPanel } from '../features/viewer/ViewerPanel'
import { MapsPanel } from '../features/maps/MapsPanel'
import { DaqPanel } from '../features/daq/DaqPanel'
import { FilesPanel } from '../features/files/FilesPanel'
import { FilePreview } from '../features/files/FilePreview'
import { TerminalPanel } from '../features/terminal/TerminalPanel'
import { LogsPanel } from '../features/logs/LogsPanel'

const FIXED_TABS = [
  { value: 'viewer', label: 'Viewer', icon: Box },
  { value: 'maps', label: 'Maps', icon: Layers },
  { value: 'daq', label: 'DAQ', icon: FlaskConical },
  { value: 'files', label: 'Files', icon: Files },
]

function Workbench() {
  const tab = useApp((state) => state.workbenchTab)
  const setTab = useApp((state) => state.setWorkbenchTab)
  const openFiles = useApp((state) => state.openFiles)
  const closeFile = useApp((state) => state.closeFile)
  const toggleWorkbench = useApp((state) => state.toggleWorkbench)

  const fileTabs = openFiles.map((file) => ({
    value: `file:${file.root}:${file.path}`,
    label: file.name || basename(file.path),
    title: file.path,
    icon: FileText,
    onClose: () => closeFile(file),
  }))
  const activeFile = openFiles.find((file) => `file:${file.root}:${file.path}` === tab)

  return (
    <section className="flex h-full min-h-0 flex-col bg-canvas" aria-label="Workbench">
      <TabStrip
        tabs={[...FIXED_TABS, ...fileTabs]}
        value={tab}
        onChange={setTab}
        trailing={<IconButton icon={X} label="Close panel" size="sm" onClick={() => toggleWorkbench(false)} />}
      />
      <div className="relative min-h-0 flex-1">
        <Pane active={tab === 'viewer'}>
          <ViewerPanel active={tab === 'viewer'} />
        </Pane>
        <Pane active={tab === 'maps'}>
          <MapsPanel active={tab === 'maps'} />
        </Pane>
        <Pane active={tab === 'daq'}>
          <DaqPanel active={tab === 'daq'} />
        </Pane>
        <Pane active={tab === 'files'}>
          <FilesPanel active={tab === 'files'} />
        </Pane>
        {activeFile ? (
          <Pane active>
            <FilePreview key={`${activeFile.root}:${activeFile.path}`} file={activeFile} />
          </Pane>
        ) : null}
      </div>
    </section>
  )
}

function Bottom() {
  const tab = useApp((state) => state.bottomTab)
  const setTab = useApp((state) => state.setBottomTab)
  const toggleBottom = useApp((state) => state.toggleBottom)
  return (
    <section className="flex h-full min-h-0 flex-col bg-canvas" aria-label="Terminal and logs">
      <TabStrip
        tabs={[
          { value: 'terminal', label: 'Terminal', icon: SquareTerminal },
          { value: 'logs', label: 'Agent output', icon: ScrollText },
        ]}
        value={tab}
        onChange={setTab}
        trailing={<IconButton icon={X} label="Close panel" size="sm" onClick={() => toggleBottom(false)} />}
      />
      <div className="relative min-h-0 flex-1">
        <Pane active={tab === 'terminal'}>
          <TerminalPanel active={tab === 'terminal'} />
        </Pane>
        <Pane active={tab === 'logs'}>
          <LogsPanel active={tab === 'logs'} />
        </Pane>
      </div>
    </section>
  )
}

function Pane({ active, children }) {
  return <div className={cx('absolute inset-0 flex flex-col', active ? 'visible' : 'invisible pointer-events-none')}>{children}</div>
}

export const RightColumn = { Workbench, Bottom }
