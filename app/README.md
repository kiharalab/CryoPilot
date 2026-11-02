# CryoPilot web application

A local web interface for the two agents in this repository:

- **Reconstruction**: drives `reconstruction_agent/harness.py` (CryoSPARC single-particle processing).
- **Modeling**: drives the modeling agent in `modeling_agent/` (CryoZeta model building, VESPER fitting
  and handedness checks, Q-scores, residue-wise DAQ, Phenix and Rosetta refinement, and PyMOL overlays).

Each project holds any number of sessions of both kinds. A session is a conversation with an agent:
you start it, follow its reasoning, tool calls, and results as they stream in, and steer or stop it
from the message box. A workbench next to the conversation has a Mol* viewer, the maps from the
CryoSPARC jobs of a reconstruction session, the DAQ profile of a model, and the project files, over a
terminal on the server computer.

## Start it

From the repository root, after `./setup.sh`:

```bash
.venv/bin/python app/serve.py
```

The first run builds the frontend if `app/frontend/dist` is missing or older than its sources (Node.js
20.19 or newer is required); later runs reuse the build. The server binds to `127.0.0.1:8790`, prints an access
link (`http://127.0.0.1:8790/?token=...`), and opens it in a browser tab.

Options: `--port`, `--host`, `--no-open`, `--no-auth`, `--rebuild`, `--dev` (auto-reload the API).
Commands: `start`, `stop`, `restart`, `status`, `log`, `password` (see below).

### Access token

CryoPilot runs on the CryoSPARC computer, which other people often log in to, and the app can start
agents, browse files, and open a terminal. Like Jupyter, the server therefore answers only browsers
that know a token. `serve.py` creates one at startup (or uses `CRYOPILOT_TOKEN`) and puts it in the
printed link (`CRYOPILOT_TOKEN` must be at least 22 characters). Opening the link swaps the token for
an HttpOnly, SameSite=Strict cookie that is valid until the server restarts; the browser that
`serve.py` opens gets a one-time code instead, so the token never appears on a command line. You can
also paste the link into the sign-in screen. `--no-auth` turns the token check off; use it only on a
computer that no one else can log in to, and only with the default `--host`. The token is removed
from the environment of agent processes and terminals.

### Shared password and background server

For a server that others reach over the network, `app/serve.py password` (`cryopilot password` in the
binary) stores a salted scrypt hash in `APP_HOME/password.json` (mode 0600). Once it exists, the server
asks for that password instead of a token, and no token is made or printed. The cookie is an HMAC of
the hash under a secret kept in `APP_HOME/cookie_secret`, so sign-ins survive a restart and a new
password signs every browser out. The password works only on the sign-in screen, not in an
`Authorization` header.

`start`, `stop`, `restart`, `status`, and `log` (in `service.py`) run the server in its own session,
detached from the terminal, like `cryosparcm`. `start --lan` binds to `0.0.0.0`. The address is saved
in `APP_HOME/server.json`, the process ID and computer name in `APP_HOME/server.pid`, and the output in
`APP_HOME/server.log`. A background server always needs the password. Because the home folder can be
shared between computers, the commands act only on the computer that started the server.

In both modes the server answers only requests addressed to `localhost`, `127.0.0.1`, `::1`, or the
`--host` it was started with, and refuses changes and WebSocket connections that come from another
web page. Add other host names (for a reverse proxy, for example) to `CRYOPILOT_ALLOWED_HOSTS`, comma
separated; `--host 0.0.0.0` turns the host check off.

### First run and sign-in

The first time, the app walks through six steps: the license agreement, the OpenAI API key, CryoSPARC
(URL, email, and password), the CryoZeta account on the Kihara Lab EM server, the processing guide, and
a check of the programs the modeling agent needs. On a computer that is not Linux the CryoSPARC step can
be skipped, because the reconstruction agent runs only on the computer where CryoSPARC is installed.
Agreeing to the license is saved in the app database (`license_accepted`), so on a shared server one
person agrees for everyone. An install set up under an earlier license shows the agreement once on its
own before the workspace.

To run either agent on a Claude model, add an Anthropic API key in **Settings > Accounts**
and choose a model such as `claude-sonnet-5-5`.

URLs, emails, and user names are saved in the app database. Passwords and the API keys stay in the
server's memory, unless you turn on **Remember passwords on this computer**. They are then written to
`~/.cryopilot/app/credentials.json` with mode 0600. When the server restarts without remembered
passwords, a sign-in screen asks for them again. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `CRYOSPARC_BASE_URL`,
`CRYOSPARC_EMAIL`, `CRYOSPARC_PASSWORD`, `CRYOZETA_BASE_URL`, `CRYOZETA_USERNAME`, and
`CRYOZETA_PASSWORD` in the server environment are used as defaults. An agent process reads the
credentials when it starts, so stop and resume a running session after you change them.

### Processing guides

The reconstruction agent follows one of two processing guides in `reconstruction_agent/guides/`: the general guide
(`cryoem-data-processing.md`, the default) or the experimental self-evolution summary
(`self-evolution-summary.md`), which is for now the general framework written for CryoSPARC v5.0.
Choose the default in **Settings → Reconstruction**, or for one session in the new-reconstruction
screen. The server passes the choice to the agent as `CRYOEM_GUIDE`, the variable `harness.py` reads.
A session keeps its guide when it is resumed; a session imported from the terminal resumes with the
general guide, the harness default, unless the import names another.

### Continuing a project

In the new-reconstruction screen, **Existing project** starts a session that continues a CryoSPARC
project you or an earlier session started, for example after a failed run. Choose the project from
your CryoSPARC projects (`GET /api/cryosparc/projects`) or type its UID. The agent imports nothing:
it works in a new workspace in that project, surveys the jobs already there, and then continues from
them. It cannot queue, change or delete jobs outside its own workspace. Earlier sessions for the
project in the harness session database pass their last notes to it. This is the same as
`harness.py --from-project` in the terminal. The **Maps** tab shows the new workspace's jobs only.

## Maps from CryoSPARC

The **Maps** tab lists the jobs of a reconstruction session's CryoSPARC workspace with their maps and
resolutions. When you are signed in to CryoSPARC, the server reads the jobs through cryosparc-tools.
Otherwise it reads `job.json` from the project folder, because the server runs on the CryoSPARC computer.
Map files are served from the project folder (or downloaded through cryosparc-tools when the folder is
not readable). Maps larger than 256 voxels a side open as a binned preview, cached under
`~/.cryopilot/app/cache/`; **View at full resolution** and **Download** use the original file.

**Use for modeling** attaches a map to a new modeling session. The server copies the map into the
project's modeling workspace (`data/P61_J29_map_sharp.mrc`, for example), because the modeling agent's
tools refuse paths outside their workspace, and adds the job, map type, resolution, and pixel size to
the agent's request.

## Where things live

| What | Path |
| --- | --- |
| App state (projects, sessions, modeling events, logs, settings) | `~/.cryopilot/app/app.sqlite3` |
| Remembered passwords (only when turned on) | `~/.cryopilot/app/credentials.json` |
| Uploads and reconstruction run folders | `~/.cryopilot/app/projects/<slug>/` |
| Map previews, downloaded maps, CryoSPARC images | `~/.cryopilot/app/cache/` |
| Reconstruction transcripts | `~/.cryopilot/sessions.sqlite3` (the harness database, shared with the CLI) |
| Modeling conversation memory | `~/.cryopilot/app/modeling_sessions.sqlite3` |
| Modeling workspace per project | `modeling_agent/workspaces/<slug>/` (`~/.cryopilot/app/modeling/workspaces/<slug>/` in the binary) |

Set `CRYOPILOT_APP_HOME` to move the app state, and `CRYOPILOT_SESSION_DB` to point at a different
harness database.

## Code

Server (`app/server/`):

| Module | Contents |
| --- | --- |
| `main.py` | App setup, projects, sessions, transcripts, the event stream, and the built frontend |
| `state.py` | Shared objects: database, event bus, agent processes, credentials, settings helpers |
| `auth.py`, `password.py` | Access-token and password middleware, the sign-in routes, and the stored password hash |
| `credentials.py` | API key and passwords, in memory or remembered |
| `routes_setup.py`, `guides.py` | License agreement, first-run setup, settings, connection tests, processing guides |
| `routes_cryosparc.py`, `cryosparc_jobs.py`, `cryosparc_bridge.py`, `volumes.py` | CryoSPARC jobs, maps, previews, and job images |
| `attachments.py` | Copies attached maps and files into the modeling workspace |
| `routes_files.py`, `files.py` | Project file roots, browsing, previews, uploads, server path completion |
| `routes_daq.py`, `daq.py` | DAQ results, per-residue scores, and DAQ-coloured models |
| `routes_terminal.py`, `terminal.py` | Terminals (PTY) over WebSocket |
| `trace.py` | Read-only view of the harness transcripts |
| `drivers/` | One long-lived driver process per agent session |

Frontend (`app/frontend/src/`, React 19, Tailwind CSS 4, zustand):

| Folder | Contents |
| --- | --- |
| `lib/api.js` | Every server endpoint, with the shape of its response |
| `store/app.js` | App state: projects, the current view, layout, viewer requests, notifications |
| `layout/` | The window: sidebar, conversation column, workbench, and terminal, with resizable dividers |
| `components/` | Shared UI: buttons, menus, dialogs, the message box, Markdown, code blocks |
| `features/` | One folder per feature: `auth`, `setup`, `settings`, `home`, `new`, `attachments`, `project`, `chat`, `viewer`, `daq`, `maps`, `files`, `terminal`, `logs` |

Mol* is used as the prebuilt viewer bundle (`public/molstar/`, copied from the `molstar` package before
each build) and driven through `viewer.plugin`.

## How the agents run

`app/server/drivers/` contains one long-lived driver per agent kind. The server starts a driver
subprocess per active session and talks to it with JSON lines on stdin/stdout; stderr becomes the
session's **Agent output** log.

- `reconstruction_driver.py` imports `harness` and runs the same sequence as `harness.main`, but takes
  the manifest as JSON instead of terminal prompts, and stays alive between chat turns so the persistent
  Python REPL and the authenticated CryoSPARC connection survive. Each chat message becomes a
  continuation turn. The transcript is persisted by the harness in the shared session database, and the
  app reads the trace from there, so sessions started from the terminal can be imported into a project
  and continued in the app.
- `modeling_driver.py` runs inside `modeling_agent/.venv`, keeps conversation memory in an openai-agents
  `SQLiteSession`, and streams reasoning summaries, tool calls, tool results (including images), and the
  answer. A `claude-…` model runs through `modeling_agent/claude_model.py`, an Agents SDK model on the
  Anthropic Messages API.

Stopping a turn sends `SIGINT` to the driver, which the harness already handles as a checkpointed
interruption. A stopped session resumes when you send it a message.

## Compiled executable

A compiled Linux x86_64 executable of CryoPilot is available from <https://cryopilot.vercel.app>. It is
one file that holds both agents, this web application, the DAQ model, VESPER, and ripgrep, and it needs
no Python or Node.js. On first start it unpacks itself into `~/.cache/cryopilot/`. The code
here supports that build: when the server runs compiled (`config.PACKAGED`), it starts agent processes
by running its own executable, and it moves the modeling sandbox from `modeling_agent/` to
`~/.cryopilot/app/modeling/`, passing it to the agent as `MODELING_AGENT_SANDBOX`.

## Development

```bash
.venv/bin/python app/serve.py --dev --no-open   # API with auto-reload on :8790
cd app/frontend && npm run dev                  # Vite on :5174, proxies /api (and the terminal WebSocket)
```

Open `http://127.0.0.1:5174` and paste the printed access link into the sign-in screen.
`CRYOPILOT_ALLOW_RECONSTRUCTION=1` lets the reconstruction agent start on a computer that is not Linux,
for development against CryoSPARC project folders that are mounted locally.

```bash
.venv/bin/python -m pytest app/tests -q
```

Everything for the app is under `app/`; the agents themselves are not modified.
