# CryoPilot

CryoPilot is a system of two language-model agents that takes single-particle cryo-EM data from raw movies or micrographs to a refined atomic model.

- **Website and compiled binary:** <https://cryopilot.vercel.app>
- **Maps, atomic models, and agent session records from the paper:** <https://kiharalab.org/CryoPilot/cryopilot_data.tar.gz>
- **Paper:** Punuru, P., Klose, T. & Kihara, D. Agentic cryo-EM structure determination with CryoPilot (in submission).

## Contents

- [Overview](#overview)
- [Repository contents](#repository-contents)
- [System requirements](#system-requirements)
- [Installation guide](#installation-guide)
- [Demo](#demo)
- [Instructions for use](#instructions-for-use)
- [Reproducing the paper](#reproducing-the-paper)
- [Tests](#tests)
- [License](#license)
- [Citation](#citation)
- [Issues](https://github.com/kiharalab/CryoPilot/issues)

# Overview

CryoPilot has two agents.

The **reconstruction agent** processes a single-particle dataset in CryoSPARC. Starting from movies, micrographs, or a particle stack, it runs motion correction, CTF estimation, particle picking, 2D classification, ab initio reconstruction, heterogeneity analysis, and refinement. After each job it examines the results, such as class averages, maps, and FSC curves, and chooses the next job. Every job is an ordinary CryoSPARC job, so you can follow a run in the CryoSPARC interface and continue it by hand.

The **modeling agent** builds an atomic model from a reconstructed map. You give it the map, its resolution, and the sequences and copy numbers of the assembly. It selects the contour level, checks the map hand, builds the model with CryoZeta, scores the density support of each residue with DAQ, removes unsupported residues, and refines the model with Phenix and Rosetta.

You can use both agents from a local web application or from the terminal.

> **Where to install CryoPilot.** The reconstruction agent reads and writes CryoSPARC project files directly, so CryoPilot must be installed on the Linux machine where CryoSPARC is installed, with access to the same file system. On a Mac, you can install CryoPilot to use the modeling agent locally, but the reconstruction agent will not work there.

# Repository contents

| Path | What it holds |
| --- | --- |
| [`harness.py`](./harness.py) | The reconstruction agent: model loop, context compaction, checkpoints, and command-line entry point. |
| [`cryosparc_repl.py`](./cryosparc_repl.py) | The persistent Python session and CryoSPARC helper functions that the reconstruction agent works in. |
| [`session_store.py`](./session_store.py) | SQLite storage for reconstruction sessions, so that a run can be stopped and resumed. |
| [`startup_cli.py`](./startup_cli.py) | Terminal prompts that collect the dataset description for a new reconstruction session. |
| [`setup.sh`](./setup.sh) | Creates the Python environments, builds VESPER and the web frontend, and checks the external programs. |
| [`cryoem-data-processing.md`](./cryoem-data-processing.md), [`cryoem-data-processing-v5.md`](./cryoem-data-processing-v5.md) | The cryo-EM processing guides that the reconstruction agent searches. |
| [`modeling_agent/`](./modeling_agent) | The modeling agent, its tools, the vendored DAQ model, and its tests. |
| [`app/`](./app) | The local web application (FastAPI server and React frontend) for both agents. |
| [`tests/`](./tests) | Unit tests for the reconstruction agent. |

# System requirements

## Hardware requirements

The reconstruction agent must run on the machine where **CryoSPARC** is installed, because it reads and writes the CryoSPARC project files directly. That machine needs the NVIDIA GPUs and storage that CryoSPARC requires for the dataset. CryoPilot itself adds little load: the agents send requests to the OpenAI API, and the modeling agent runs DAQ, fitting, and refinement on the CPU.

The modeling agent alone runs on any standard computer with an internet connection, including a Mac. It does not need a GPU.

## Software requirements

### Operating systems

The compiled binary runs on Linux x86_64 with glibc 2.28 or newer (RHEL, Rocky, or Alma 8+, Ubuntu 20.04+, Debian 10+). From source, both agents run on Linux, and the modeling agent also runs on macOS.

CryoPilot has been tested on:

- Linux (both agents): Ubuntu 26.04.1 LTS (kernel 7.0, glibc 2.43), 48 CPU cores, 247 GB RAM, with CryoSPARC v5.0.2 installed on the same machine and two NVIDIA RTX PRO 6000 Blackwell GPUs.
- macOS (modeling agent): 26.6.2 on Apple M3 Max (arm64), 64 GB RAM.

### Accounts and services

- An **OpenAI API key** (`OPENAI_API_KEY`). Both agents use GPT-5.5 by default, at the `xhigh` reasoning setting for the reconstruction agent and `high` for the modeling agent, as in the paper. You can choose any other OpenAI model (see [Instructions for use](#instructions-for-use)). Support for Anthropic models is coming soon.
- A **CryoSPARC v5.0.x** instance and user account, for the reconstruction agent.
- A **CryoZeta** account on the Kihara Lab EM server (<https://em.kiharalab.org>), for model building by the modeling agent.

### Python dependencies

The reconstruction agent and web server use Python 3.11 (tested 3.11.9 and 3.11.15):

```
openai>=2.0.0           # tested 2.43.0
cryosparc-tools>=5.0,<5.1   # tested 5.0.2 and 5.0.3
matplotlib>=3.8.0
mrcfile>=1.5.0
numpy>=1.26.0
fastapi>=0.104          # web application only
uvicorn>=0.24           # web application only
python-multipart>=0.0.6 # web application only
websockets>=12          # web application only (terminal)
requests>=2.32          # web application only (CryoZeta sign-in)
```

The modeling agent uses Python 3.10 or newer (tested 3.11 and 3.13) and pins its dependencies in [`modeling_agent/uv.lock`](./modeling_agent/uv.lock). Its main dependencies are `openai-agents`, `numpy`, `scipy`, `gemmi`, `mrcfile`, `onnxruntime`, `matplotlib`, and `requests`.

### External programs for the modeling agent

| Program | Tested version | Used for |
| --- | --- | --- |
| [PyMOL](https://pymol.org) | 3.1.8 | Map-model overlay images |
| [UCSF ChimeraX](https://www.cgl.ucsf.edu/chimerax/) with the QScore bundle | 1.12 | Simulated density and Q-scores |
| [VESPER](https://github.com/kiharalab/VESPER) | revision `0670f93` | Fitting models into maps; built by `setup_vesper.py` and included in the binary |
| [Phenix](https://phenix-online.org) | 2.2 | Real-space refinement, map-model correlation, and MolProbity validation |
| [Rosetta](https://rosettacommons.org/software/) | 3.13 | Density-guided refinement |
| [ripgrep](https://github.com/BurntSushi/ripgrep) | any | File search by the agent |

Building the web frontend from source needs Node.js 20.19 or newer (tested 23.6.1). Building VESPER from source needs a C compiler, FFTW, and OpenMP.

# Installation guide

## Option 1: compiled binary (Linux)

On the CryoSPARC machine, run:

```bash
curl -fsSL https://raw.githubusercontent.com/kiharalab/CryoPilot/main/install.sh | sh
```

The installer downloads the newest release from GitHub, checks its SHA-256, keeps it in `~/.local/share/cryopilot/`, links it as `~/.local/bin/cryopilot`, and adds `~/.local/bin` to your PATH. Open a new terminal and start it with `cryopilot`. You can also download `cryopilot-<version>-linux-x86_64` from the [releases page](https://github.com/kiharalab/CryoPilot/releases) or <https://cryopilot.vercel.app>, then run `chmod +x` on it and start it directly.

The binary is one file that contains both agents, the web application, the DAQ model, VESPER, and ripgrep. It does not need Python or Node.js. Install PyMOL, ChimeraX, Phenix, and Rosetta separately for modeling. The first start unpacks the program (about 800 MB) into `~/.cache/cryopilot/`, which takes 10 to 30 seconds; later starts begin at once. Set `CRYOPILOT_CACHE` to unpack it elsewhere.

## Option 2: from source

Install [uv](https://docs.astral.sh/uv/) if you do not have it (`curl -LsSf https://astral.sh/uv/install.sh | sh`), then:

```bash
git clone https://github.com/kiharalab/CryoPilot.git
cd CryoPilot
./setup.sh
```

`setup.sh` does the following:

1. Creates `.venv/` with Python 3.11 for the reconstruction agent and the web server. uv downloads Python 3.11 if it is not installed.
2. Creates `modeling_agent/.venv/` for the modeling agent from the pinned versions in `modeling_agent/uv.lock`.
3. Builds VESPER into `modeling_agent/.deps/vesper/`. On macOS it installs `fftw` and `libomp` with Homebrew first. On Linux it needs `gcc` and the FFTW development files (for example, `sudo apt install build-essential libfftw3-dev`).
4. Builds the web frontend if Node.js 20.19 or newer is installed.
5. Reports which of PyMOL, ChimeraX, VESPER, Phenix, Rosetta, and ripgrep it can find, and how to point CryoPilot to any that are missing.

You can run `./setup.sh` again at any time to update the environments. Use `--skip-vesper` or `--skip-frontend` to leave out those steps. PyMOL, ChimeraX, Phenix, and Rosetta are installed by their own instructions. In ChimeraX, install the Q-score bundle once with `toolshed install QScore`.

`setup.sh` took about 3 minutes on the Linux machine above and less than 1 minute on the Mac.

If you prefer conda, `environment.yml` creates the reconstruction environment instead of step 1 (`conda env create -f environment.yml`, then `pip install -r app/requirements.txt`).

# Demo

## Modeling demo: validate a deposited model

This demo runs the modeling agent on a public map and model pair, EMDB [EMD-29700](https://www.ebi.ac.uk/emdb/EMD-29700) (2.2 Å) with its fitted model PDB [8G3K](https://www.rcsb.org/structure/8G3K). It needs an OpenAI API key, PyMOL, ChimeraX with the QScore bundle, Phenix, VESPER (built during installation), and ripgrep, but not CryoSPARC or CryoZeta.

```bash
cd modeling_agent
mkdir -p data
curl -L https://files.wwpdb.org/pub/emdb/structures/EMD-29700/map/emd_29700.map.gz | gunzip > data/emd_29700.map
curl -L -o data/8g3k.pdb https://files.rcsb.org/download/8G3K.pdb
curl -L -o data/8g3k.fasta https://www.rcsb.org/fasta/entry/8G3K

export OPENAI_API_KEY=sk-...
uv run python modeller.py "validate the model data/8g3k.pdb with map data/emd_29700.map at contour 0.56 and resolution 2.2 angstroms"
```

**Expected output.** The terminal streams the agent's reasoning summaries, tool calls, and tool results, and ends with a written assessment of how well the model fits the map. The agent writes its files to a folder under `modeling_agent/outputs/`, for example `outputs/8g3k_validation/`:

- `validation_report.md` and `validation_summary.json`: the assessment and its numbers
- `phenix_validation/`: map-model correlation (CC_mask, CC_box, CC_volume, and CC_peaks) and MolProbity results
- `daq/`: per-residue DAQ scores as CSV, with a profile plot
- `residue_support.csv`: residues that lie outside the density at the given contour
- `map_views/` and `overlays_all/`: PNG images of the map and of the model in the density

The report should find good global fit and geometry for 8G3K (CC_mask about 0.82, MolProbity score about 1.1, no Ramachandran outliers) and list a few loop and terminal segments with weaker density support. The exact wording and the set of flagged segments can differ between runs.

**Expected run time.** About 4 minutes (219 s in our test, with 14 tool calls), most of it spent on model requests, DAQ, and Phenix validation. We measured this on the Linux machine described in [System requirements](#system-requirements); the demo does not use a GPU, so a desktop computer with a broadband connection should take a similar time.

## Reconstruction demo: EMPIAR-10025 tutorial subset

This demo runs the reconstruction agent on the 20-movie subset of EMPIAR-10025 (*Thermoplasma acidophilum* 20S proteasome) that CryoSPARC provides as its introductory tutorial dataset (about 8 GB). Run all of it on the CryoSPARC machine. First, in a folder that the CryoSPARC user can read:

```bash
cryosparcm downloadtest
tar xvf empiar_10025_subset_v1.tar           # CryoSPARC v4: empiar_10025_subset.tar
```

Then, from the CryoPilot folder:

```bash
source .venv/bin/activate
export OPENAI_API_KEY=sk-...
python harness.py
```

Answer the prompts as follows:

| Prompt | Answer |
| --- | --- |
| Dataset kind | `movies` |
| Dataset path | `<download folder>/*.tif` |
| Pixel size | 0.6575 Å |
| Accelerating voltage | 300 kV |
| Spherical aberration | 2.7 mm |
| Total exposure dose | 53 e⁻/Å² |
| Gain reference | `<download folder>/norm-amibox05-0.mrc` |
| Expected particle diameter | 150 Å |
| Scientific objective | for example, "a high-resolution consensus map" |

Give your CryoSPARC login, a project title, and a parent directory for the new project as well.

**Expected output.** A new CryoSPARC project titled as you entered, with a workspace named "Automated Reconstruction" that holds every job the agent created, and a final refined and sharpened map with its FSC curve. The agent ends the run with a written processing report.

**Expected run time.** The reconstruction runs in the paper took 2–148 h from the first to the last agent step (median 29 h). The run time depends on the size of the dataset, the number of jobs the agent decides to run, and the speed of the CryoSPARC GPUs.

# Instructions for use

From source, activate the environment that `setup.sh` created before you run the commands below:

```bash
source .venv/bin/activate
```

## Web application

Start the server from the binary (`cryopilot`) or from source (`python app/serve.py`). It listens on `127.0.0.1:8790`, prints an access link such as `http://127.0.0.1:8790/?token=...`, and opens it in your browser. The token keeps other users of a shared computer out of your sessions. A new token is made each time the server starts, so you open the new link after a restart. Set `CRYOPILOT_TOKEN` (at least 22 characters) to keep the same link.

The first time, CryoPilot asks for your OpenAI API key, your CryoSPARC URL, email, and password, and your CryoZeta account, and which processing guide the reconstruction agent follows. It also shows which modeling programs it found. Passwords stay in the server's memory unless you choose to remember them on this computer.

The window has three columns, and you can drag each divider:

- On the left, your projects and their sessions.
- In the middle, the conversation with an agent. You start a run, follow it, and steer it from the message box, and you can stop it at any time.
- On the right, a workbench. It has a Mol* viewer, the maps from the CryoSPARC jobs of a reconstruction session, the residue-wise DAQ profile of a model, and the project files. A terminal on the CryoSPARC computer sits underneath.

A reconstruction session starts from the dataset paths, the acquisition parameters, and your goal. A modeling session starts from a request in your own words. You can attach maps that your reconstruction sessions produced, or files from your computer. In **Settings** you can switch between the two processing guides (the general guide and the CryoSPARC v5 guide), change the default models, and sign in to each service again.

Because CryoPilot runs on the CryoSPARC machine, you will usually open it from your own computer. Forward the port over SSH, then open the printed link:

```bash
ssh -L 8790:127.0.0.1:8790 you@workstation
```

Options: `--port N`, `--no-open`, and `--no-auth` (only on a computer that no one else can log in to). From source, `--rebuild` rebuilds the frontend and `--dev` reloads the server when its code changes. See [`app/README.md`](./app/README.md) for details.

## Reconstruction agent from the terminal

```bash
python harness.py                          # start a new session; prompts for the dataset
python harness.py --resume SESSION_ID      # resume a stopped session
```

The prompts ask for your CryoSPARC login, a project title and parent directory, the dataset kind (`movies`, `micrographs`, or `particles`), server-visible paths, pixel size, accelerating voltage, spherical aberration, total exposure dose, an optional gain reference and defect file, an optional sample description, the expected particle diameter, an optional molecular mass, and a scientific objective in your own words. Useful options:

| Option | Default | Effect |
| --- | --- | --- |
| `--processing-guide original\|v5\|PATH` | `original` | Processing guide the agent searches. |
| `--model` | `gpt-5.5` | OpenAI model for a new session (also `OPENAI_MODEL`). |
| `--reasoning-effort` | `xhigh` | Reasoning setting for a new session (also `REASONING_EFFORT`). |
| `--cryosparc-url`, `--cryosparc-email` | prompted | CryoSPARC login. The password is prompted or read from `CRYOSPARC_PASSWORD`. |
| `--session-db` | `~/.cryoagent/sessions.sqlite3` | Session database (also `CRYOAGENT_SESSION_DB`). |

Press Ctrl-C to stop a turn. The session is saved at a checkpoint and can be resumed with `--resume`.

## Modeling agent from the terminal

```bash
cd modeling_agent
export CRYOZETA_USERNAME=you@example.edu
read -s CRYOZETA_PASSWORD; export CRYOZETA_PASSWORD
uv run python modeller.py "Build the most accurate atomic model supported by data/map.mrc at 3.1 Å resolution. The assembly is two copies of data/seq.fasta."
```

Place input files under `modeling_agent/` (for example in `data/`), because the agent's tools only read and write inside its own folder. CryoZeta jobs can take hours; the agent waits for them and checks every 30 minutes. The full modeling runs in the paper took 2.6–10.8 h, most of it spent waiting for CryoZeta. If PyMOL, ChimeraX, Phenix, Rosetta, or VESPER are not on `PATH`, set `PYMOL_EXECUTABLE`, `CHIMERAX_EXECUTABLE`, `PHENIX_BIN`, `ROSETTA_BIN` and `ROSETTA3_DB`, or `VESPER_EXECUTABLE`. `MODELING_MODEL` (default `gpt-5.5`, any OpenAI model), `MODELING_REASONING_EFFORT` (default `high`), and `MODELING_MAX_TURNS` (default 500) control the agent loop. See [`modeling_agent/README.md`](./modeling_agent/README.md) for each tool.

## Where CryoPilot keeps its data

| What | Location |
| --- | --- |
| Reconstruction sessions | `~/.cryoagent/sessions.sqlite3` |
| Web application projects, sessions, uploads, and map previews | `~/.cryoagent/app/` (`CRYOAGENT_APP_HOME`) |
| Modeling workspaces | `modeling_agent/workspaces/` from source, `~/.cryoagent/app/modeling/` in the binary |
| CryoSPARC jobs | The CryoSPARC project you named at startup |

Credentials come from the environment or from the web application. The web application keeps passwords in memory, unless you turn on "Remember passwords on this computer". It then stores them in `~/.cryoagent/app/credentials.json`, which only your user account can read.

# Reproducing the paper

1. Download the datasets from EMPIAR under the accession codes in Extended Data Table 1 of the paper.
2. Download the data package from <https://kiharalab.org/CryoPilot/cryopilot_data.tar.gz>. For each reconstruction run it holds the reported maps, the CryoSPARC job records, and the complete agent session; for each modeling run it holds the inputs and every file the agent wrote. `index.md` inside the package describes the layout.
3. Run each reconstruction with `python harness.py` and the dataset parameters recorded in that run's `session.json` in the package. Run each modeling job with the map, resolution, and sequences in its `inputs/` folder. The default model and reasoning settings are the ones used in the paper.

Language-model outputs are not deterministic, so a new run takes its own processing route. The session records in the data package let you check each decision in the reported runs.

# Tests

```bash
python -m unittest discover -s tests                         # reconstruction agent: 134 tests, about 2 s
python -m pytest app/tests                                   # web application: 108 tests (needs pytest and httpx)
cd modeling_agent && uv run python -m unittest discover -s tests   # modeling agent: 78 tests, about 5 s
```

The tests run offline and do not need CryoSPARC, CryoZeta, or an API key.

# License

CryoPilot is released under the GNU General Public License v3.0 (GPL v3). See [`LICENSE`](./LICENSE).

It includes the DAQ model and scoring code from [DAQ](https://github.com/kiharalab/DAQ) (GPL-3.0, [`modeling_agent/vendor/daqcore/LICENSE`](./modeling_agent/vendor/daqcore/LICENSE)), and the Linux binary includes a build of [VESPER](https://github.com/kiharalab/VESPER) under its own license. CryoSPARC, CryoZeta, PyMOL, ChimeraX, Phenix, and Rosetta are separate programs with their own licenses and are not distributed here.

# Citation

If you use CryoPilot, please cite:

> Punuru, P., Klose, T. & Kihara, D. Agentic cryo-EM structure determination with CryoPilot. (in submission)

For questions, open an [issue](https://github.com/kiharalab/CryoPilot/issues) or contact Daisuke Kihara (dkihara@purdue.edu).
