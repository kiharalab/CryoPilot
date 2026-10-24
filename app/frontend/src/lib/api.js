// Every server endpoint the app uses. Components call these, never fetch().
// Errors throw an Error with .status (0 when the server did not answer), .fields (per-field validation messages),
// .setup (which credential is missing: "openai" | "cryosparc" | "cryozeta"),
// and .data (the full JSON body).

export class ApiError extends Error {
  constructor(message, { status, fields, setup, data } = {}) {
    super(message)
    this.status = status
    this.fields = fields || null
    this.setup = setup || null
    this.data = data || {}
  }
}

const listeners = new Set()
/** Called with no arguments whenever the server answers 401 (token missing). */
export function onUnauthorized(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

async function request(path, { method = 'GET', body, signal, formData, raw = false } = {}) {
  const headers = { Accept: 'application/json' }
  let payload
  if (formData) {
    payload = formData
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  let response
  try {
    response = await fetch(path, { method, headers, body: payload, signal, credentials: 'same-origin' })
  } catch (err) {
    if (err.name === 'AbortError') throw err
    // The browser's own wording ("Failed to fetch", "Load failed") says nothing useful.
    throw new ApiError('Cannot reach the CryoPilot server.', { status: 0 })
  }
  if (response.status === 401 && !path.startsWith('/api/auth/')) {
    listeners.forEach((listener) => listener())
  }
  if (raw && response.ok) return response
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new ApiError(data.error || data.message || `Request failed (${response.status})`, {
      status: response.status,
      fields: data.errors,
      setup: data.setup,
      data,
    })
  }
  return data
}

const enc = encodeURIComponent
const qs = (params) => {
  const entries = Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== '')
  return entries.length ? `?${entries.map(([key, value]) => `${enc(key)}=${enc(value)}`).join('&')}` : ''
}

export const api = {
  // ---------------------------------------------------------------- auth
  authStatus: () => request('/api/auth/status'), // {enabled, method: 'token'|'password', authenticated}
  login: (secret) => request('/api/auth/login', { method: 'POST', body: secret }), // {token} or {password}
  logout: () => request('/api/auth/logout', { method: 'POST' }),

  // ------------------------------------------------------- setup/settings
  // Setup payload: {onboarded, platform:{system, reconstruction_supported},
  //   openai:{configured, source}, cryosparc:{url, email, password_set, verified_at},
  //   cryozeta:{base_url, username, password_set, verified_at}, processing_guide,
  //   remember_credentials, defaults:{reconstruction_model, reconstruction_effort,
  //   modeling_model, modeling_effort, modeling_max_turns}, modeling_tools}
  setup: () => request('/api/setup'),
  completeSetup: () => request('/api/setup/complete', { method: 'POST' }),
  // body: {openai:{api_key}|{clear:true}, cryosparc:{url,email,password,clear_password},
  //        cryozeta:{base_url,username,password,clear_password}, processing_guide,
  //        remember_credentials, defaults:{...}} -> setup payload
  saveSettings: (body) => request('/api/settings', { method: 'PUT', body }),
  testOpenai: () => request('/api/settings/openai/test', { method: 'POST' }),
  testCryosparc: () => request('/api/settings/cryosparc/test', { method: 'POST' }),
  testCryozeta: () => request('/api/settings/cryozeta/test', { method: 'POST' }),
  refreshModelingTools: () => request('/api/settings/modeling-tools/refresh', { method: 'POST' }),
  guides: () => request('/api/guides'), // {guides:[{id,label,title,description,sections,available}], selected, default}
  guide: (id) => request(`/api/guides/${enc(id)}`), // {id,label,path,markdown}

  // ------------------------------------------------------------ projects
  projects: () => request('/api/projects'),
  project: (id) => request(`/api/projects/${enc(id)}`), // project + sessions[] + directories
  createProject: (body) => request('/api/projects', { method: 'POST', body }), // {title, description, parent_directory}
  updateProject: (id, body) => request(`/api/projects/${enc(id)}`, { method: 'PATCH', body }),
  deleteProject: (id) => request(`/api/projects/${enc(id)}`, { method: 'DELETE' }),

  // ------------------------------------------------------------ sessions
  // body: {title?, manifest:{dataset_kind, dataset_path, particle_metadata_path, gain_reference_path,
  //   defect_file_path, eer_enabled, eer_fractions, eer_upsampling, pixel_size_A,
  //   accelerating_voltage_kV, spherical_aberration_mm, total_dose_e_per_A2, negative_stain,
  //   phase_plate, sample_description, particle_diameter_A, molecular_mass_kDa,
  //   expected_symmetry, user_goal}, agent:{model, reasoning_effort}, processing_guide?}
  createReconstruction: (projectId, body) =>
    request(`/api/projects/${enc(projectId)}/sessions/reconstruction`, { method: 'POST', body }),
  importReconstruction: (projectId, body) =>
    request(`/api/projects/${enc(projectId)}/sessions/reconstruction/import`, { method: 'POST', body }),
  // body: {prompt, attachments:[Attachment], model?, reasoning_effort?, max_turns?, title?}
  // Attachment: {type:'cryosparc_map', session_id, job_uid, name} | {type:'file', root, path}
  createModeling: (projectId, body) =>
    request(`/api/projects/${enc(projectId)}/sessions/modeling`, { method: 'POST', body }),
  session: (id) => request(`/api/sessions/${enc(id)}`),
  renameSession: (id, title) => request(`/api/sessions/${enc(id)}`, { method: 'PATCH', body: { title } }),
  deleteSession: (id) => request(`/api/sessions/${enc(id)}`, { method: 'DELETE' }),
  startSession: (id) => request(`/api/sessions/${enc(id)}/start`, { method: 'POST' }),
  // Modeling sessions also accept attachments with a follow-up message.
  sendMessage: (id, content, attachments = []) =>
    request(`/api/sessions/${enc(id)}/messages`, { method: 'POST', body: { content, attachments } }),
  interrupt: (id) => request(`/api/sessions/${enc(id)}/interrupt`, { method: 'POST' }),
  stopSession: (id) => request(`/api/sessions/${enc(id)}/stop`, { method: 'POST' }),
  trace: (id, after = 0) => request(`/api/sessions/${enc(id)}/trace?after=${after}`),
  item: (id, itemId) => request(`/api/sessions/${enc(id)}/items/${itemId}`),
  itemImageUrl: (id, itemId, index) => `/api/sessions/${enc(id)}/items/${itemId}/image/${index}`,
  recursiveItems: (id, callId) => request(`/api/sessions/${enc(id)}/recursive/${callId}`),
  recursiveItemImageUrl: (id, callId, itemId, index) =>
    `/api/sessions/${enc(id)}/recursive/${callId}/items/${itemId}/image/${index}`,
  knownJobs: (id) => request(`/api/sessions/${enc(id)}/jobs`),
  console: (id, after = 0) => request(`/api/sessions/${enc(id)}/console?after=${after}`),
  eventsUrl: (id, after = 0) => `/api/sessions/${enc(id)}/events?after=${after}`,
  harnessSessions: () => request('/api/harness/sessions'),

  // ------------------------------------------------- CryoSPARC jobs and maps
  // {project_uid, workspace_uid, project_dir, project_dir_readable, source:'api'|'filesystem',
  //  cryosparc_url, warning?, jobs:[CryoJob]}; before the agent binds the session to a
  //  CryoSPARC project: {project_uid: null, jobs: [], reason}.
  // CryoJob = {uid, type, type_title, title, status, created_at, completed_at, parents,
  //  workspace_uids, resolution_A, resolution_from, is_final_result, volumes:[Volume]}
  // Volume = {job_uid, name (unique per job), group, group_title, result, label,
  //  kind:'map'|'map_sharp'|'half_map'|'mask'|'other', resolution_A, resolution_from, b_factor,
  //  psize_A, shape, size_bytes, path, available (false: file gone, disable View), stats|null,
  //  metafile, version}
  cryosparcJobs: (sessionId) => request(`/api/sessions/${enc(sessionId)}/cryosparc/jobs`),
  // MRC data for Mol*; preview=true bins large maps to at most 256 voxels a side. The response
  // header X-Map-Stats holds JSON {min, max, mean, rms, bin_factor} of the full map.
  cryosparcVolumeUrl: (sessionId, jobUid, name, { preview = true } = {}) =>
    `/api/sessions/${enc(sessionId)}/cryosparc/volume${qs({ job: jobUid, name, preview: preview ? 1 : 0 })}`,
  // {assets:[{id, filename, kind:'image', format, content_type, size, uploaded_at}], reason?, truncated?}
  // (CryoSPARC API only; `reason` explains an empty list when not signed in)
  cryosparcAssets: (sessionId, jobUid) => request(`/api/sessions/${enc(sessionId)}/cryosparc/jobs/${enc(jobUid)}/assets`),
  cryosparcAssetUrl: (sessionId, jobUid, assetId) =>
    `/api/sessions/${enc(sessionId)}/cryosparc/jobs/${enc(jobUid)}/assets/${enc(assetId)}`,
  // Maps from the completed jobs of every reconstruction session in a project, for the modeling
  // composer: {maps:[Volume & {session_id, session_title, project_uid, job_type, job_type_title,
  // job_title, job_status, is_final_result, completed_at, source}], errors:[{session_id,
  // session_title, message, setup?}]}
  projectMaps: (projectId, { all = false } = {}) => request(`/api/projects/${enc(projectId)}/maps${qs({ all: all ? 1 : 0 })}`),

  // ---------------------------------------------------------------- files
  // Roots (a bare array): [{key, label, path, writable, session_id?, session_title?}]
  // keys: 'modeling' | 'files' | 'reconstruction' | 'cryosparc:<session_id>' (read-only)
  fileRoots: (projectId) => request(`/api/projects/${enc(projectId)}/files/roots`),
  // One directory level: {root, path, truncated, entries:[{name, path, type:'dir'|'file', size,
  // modified, category}]}; category: 'dir' | 'structure' | 'volume' | 'image' | 'data' | 'other'
  listDir: (projectId, root, path = '') => request(`/api/projects/${enc(projectId)}/files/tree${qs({ root, path })}`),
  // preview: for maps, a binned copy (at most 256 voxels a side) that loads quickly in Mol*.
  fileUrl: (projectId, root, path, { preview = false, download = false } = {}) =>
    `/api/projects/${enc(projectId)}/files/raw${qs({ root, path, preview: preview ? 1 : undefined, download: download ? 1 : undefined })}`,
  fileText: (projectId, root, path) => request(`/api/projects/${enc(projectId)}/files/text${qs({ root, path })}`), // {text, truncated, size, binary}
  fileTable: (projectId, root, path) => request(`/api/projects/${enc(projectId)}/files/table${qs({ root, path })}`), // {columns, rows, total_rows (null over 64 MB), truncated, delimiter, size}
  // onConflict: 'replace' (default) overwrites a file with the same name; 'rename' keeps it and
  // stores the upload as name-2.ext. The response's path and name are the final ones.
  uploadFile: (projectId, root, subdir, file, { onConflict } = {}) => {
    const formData = new FormData()
    formData.append('root', root)
    formData.append('subdir', subdir || '')
    if (onConflict) formData.append('on_conflict', onConflict)
    formData.append('file', file)
    return request(`/api/projects/${enc(projectId)}/files/upload`, { method: 'POST', formData })
  },
  deleteFile: (projectId, root, path) => request(`/api/projects/${enc(projectId)}/files${qs({ root, path })}`, { method: 'DELETE' }),
  // Server-side path completion for dataset paths: {path, parent, prefix, exists,
  // type:'dir'|'file'|null, glob, entries:[{name, path, type, size, modified}], truncated};
  // a glob also returns match_count and count_capped. Relative paths are read from $HOME.
  fsList: (path, { dirsOnly = false } = {}) => request(`/api/fs/list${qs({ path, dirs_only: dirsOnly ? 1 : 0 })}`),

  // ------------------------------------------------------------------ DAQ
  // {results:[{path (CSV, workspace-relative; pass unchanged below), folder, label, model_path,
  //  map_path (absolute), model_relative, map_relative (workspace-relative or null),
  //  structure_available, contour, candidate_index, residue_count, scored_count, modified, summary}]}
  daqResults: (projectId) => request(`/api/projects/${enc(projectId)}/daq`),
  // {path, window, chains:[id], chain_summary:[{chain, residues, scored, first, last, mean,
  //  fraction_negative}], residues:[{index, chain, resnum, icode, resname, scored, aa, ca, ss, total,
  //  aa_mean}] (missing scores are null), summary:{residues, scored, window, mean, fraction_negative,
  //  metrics}, metadata}
  daqScores: (projectId, path, { window = 19 } = {}) => request(`/api/projects/${enc(projectId)}/daq/scores${qs({ path, window })}`),
  // PDB text with the chosen DAQ score in the B-factor column, for colouring in Mol*.
  daqStructureUrl: (projectId, path, { metric = 'aa_mean', window = 19 } = {}) =>
    `/api/projects/${enc(projectId)}/daq/structure${qs({ path, metric, window })}`,

  // ------------------------------------------------------------- terminal
  // Terminal = {id, title, cwd, shell, project_id, root, alive, exit_code, created_at, exited_at, cols, rows}
  terminals: () => request('/api/terminals'), // {terminals:[Terminal]}
  // {project_id?, root? ('home' or a file root key), cols?, rows?} -> Terminal; 409 at 12 open terminals
  createTerminal: (body) => request('/api/terminals', { method: 'POST', body }),
  // JSON frames: client {type:'input', data} | {type:'resize', cols, rows}; server {type:'output', data} |
  // {type:'exit', code}. Close codes: 4404 unknown terminal, 1013 too slow (reconnect),
  // 4401 not signed in, 4403 refused host or origin (do not reconnect).
  closeTerminal: (id) => request(`/api/terminals/${enc(id)}`, { method: 'DELETE' }),
  terminalSocketUrl: (id) => {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
    return `${scheme}://${window.location.host}/api/terminals/${enc(id)}/ws`
  },
}
