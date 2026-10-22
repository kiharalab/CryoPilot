// What a file is, from its name alone. The categories mirror
// app/server/files.py so the explorer, the preview tabs, and the viewer agree
// on what opens where.
import { Atom, Cuboid, File, FileCode, FileImage, FileJson, FileSpreadsheet, FileText } from 'lucide-react'

const STRUCTURE = ['.pdb.gz', '.cif.gz', '.mmcif', '.pdb', '.cif', '.ent']
const VOLUME = ['.mrc.gz', '.map.gz', '.mrc', '.map', '.ccp4']
const IMAGE = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg']
const TABLE = ['.csv', '.tsv']
const MARKDOWN = ['.md', '.markdown']
const SEQUENCE = ['.fasta', '.fa', '.faa', '.fas', '.fna', '.seq']
const CODE = ['.py', '.sh', '.bash', '.zsh', '.js', '.jsx', '.ts', '.r', '.c', '.cpp', '.h', '.cu', '.tcl', '.cxc', '.pml']
// Formats that never read as text; previewing them would show noise.
const BINARY = [
  '.mrcs', '.cs', '.npy', '.npz', '.gz', '.zip', '.tar', '.tgz', '.bz2', '.xz', '.7z', '.h5', '.hdf5', '.pkl',
  '.pickle', '.pt', '.pth', '.ckpt', '.so', '.o', '.pyc', '.sqlite', '.sqlite3', '.db', '.tif', '.tiff', '.eer',
  '.dm4', '.ser', '.pdf', '.mp4', '.mov', '.bin', '.dat', '.mtz', '.ccp4i', '.mdb',
]

function endsWithAny(name, extensions) {
  const lower = name.toLowerCase()
  return extensions.some((extension) => lower.endsWith(extension))
}

/** 'structure' | 'volume' | 'image' | 'table' | 'markdown' | 'binary' | 'text' */
export function fileKind(name = '') {
  if (endsWithAny(name, STRUCTURE)) return 'structure'
  if (endsWithAny(name, VOLUME)) return 'volume'
  if (endsWithAny(name, IMAGE)) return 'image'
  if (endsWithAny(name, TABLE)) return 'table'
  if (endsWithAny(name, MARKDOWN)) return 'markdown'
  if (endsWithAny(name, BINARY)) return 'binary'
  return 'text'
}

/** Mol* reads plain PDB/mmCIF and MRC/CCP4; compressed copies are downloaded instead. */
export function isViewable(name = '') {
  const kind = fileKind(name)
  return (kind === 'structure' || kind === 'volume') && !name.toLowerCase().endsWith('.gz')
}

export function structureFormat(name = '') {
  return endsWithAny(name, ['.cif', '.mmcif', '.cif.gz']) ? 'mmcif' : 'pdb'
}

export function isSequence(name = '') {
  return endsWithAny(name, SEQUENCE)
}

/** Files the modeling agent can take as input: models, maps, and sequences. */
export function isModelingInput(name = '') {
  const kind = fileKind(name)
  return kind === 'structure' || kind === 'volume' || isSequence(name)
}

export function fileIcon(name = '') {
  const kind = fileKind(name)
  if (kind === 'structure') return Atom
  if (kind === 'volume') return Cuboid
  if (kind === 'image') return FileImage
  if (kind === 'table') return FileSpreadsheet
  if (name.toLowerCase().endsWith('.json')) return FileJson
  if (endsWithAny(name, CODE)) return FileCode
  if (kind === 'text' || kind === 'markdown') return FileText
  return File
}

/** The request that shows a model or map file in Mol*. */
export function viewerRequestForFile(url, root, path, name) {
  const key = `file:${root}:${path}`
  if (fileKind(name) === 'structure') return { kind: 'structure', url, label: name, key, format: structureFormat(name) }
  // Large maps load as a binned preview; the original stays available to download.
  return { kind: 'volume', url: `${url}${url.includes('?') ? '&' : '?'}preview=1`, label: name, key }
}

/** Absolute path on the server for an entry inside a root, for "Copy path". */
export function absolutePath(rootPath, path) {
  if (!rootPath) return path || ''
  if (!path) return rootPath
  return `${rootPath.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`
}

export function parentPath(path = '') {
  const index = path.lastIndexOf('/')
  return index === -1 ? '' : path.slice(0, index)
}

/** Starts a browser download of a server file without leaving the page. */
export function downloadUrl(url, name) {
  const link = document.createElement('a')
  link.href = url
  if (name) link.download = name
  link.rel = 'noopener'
  document.body.appendChild(link)
  link.click()
  link.remove()
}
