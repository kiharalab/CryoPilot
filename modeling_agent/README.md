# Cryo-EM Modeling Agent

A deliberately small terminal agent for cryo-EM structure modeling and model
validation. It uses
`gpt-5.5` and exposes these local tools:

- `read_file`
- `write_file`
- `bash`
- `grep`
- `view_image`
- `search_pdb_homologs`
- `download_pdb_homolog`
- `run_qscore`
- `compare_map_hands`
- `run_daq`
- `render_pdb`
- `flip_map`
- `align_model_to_map`
- `render_map_model`
- `submit_cryozeta`
- `check_cryozeta_job`
- `wait_for_cryozeta`
- `download_cryozeta_result`

`run_daq` resamples maps to 1 Å in physical coordinates before inference and
writes separate `candidate_NNN/` artifacts for each model alternative. CSV and
summary AA, Cα, SS, and supplementary combined scores are raw; optional smoothing
is separately labeled. Profiles have one row per chain and separate AA/CA/SS
panels against residue numbers, retaining insertion codes. Unscored residues
remain missing and have zero occupancy in annotated PDB/mmCIF files. SS uses a
Cα-distance heuristic, not DSSP. The grid reference is the average prediction
over sampled map points above the contour, so this bridge is not numerically
identical to original DAQ model-position normalization.

Before the first CryoZeta job, the agent searches **experimental PDB only** for
each unique supplied protein sequence. The search records assembly copy counts,
sequence identities, aligned coverage, and query/subject alignments. It downloads
one resolved polymer copy per selected reference, rather than the whole PDB
entry. No AFDB search or ESMFold2 prediction is used. No suitable reference,
unavailable search/scoring, or inconclusive handedness evidence leads to the
CryoZeta fallback on the original map, with the hand labeled provisional.

`flip_map` writes a map-only reflection and records its physical transform;
original inputs and protein chirality are preserved. `align_model_to_map` uses
[VESPER](https://github.com/kiharalab/VESPER) for rigid-body alignment. ChimeraX
`molmap` simulates the selected model's density; VESPER searches its placement
in a canonicalized, 1 Å experimental map, and its physical rotation/translation
is applied to the atomic coordinates. The tool retains alternative placements,
fitted mmCIFs, the command, settings, and VESPER logs. Reflections and nonrigid
atomic transforms are rejected. ChimeraX does not perform alignment.

The PDB reference is fitted independently to both hands with equivalent VESPER
settings. `run_qscore` uses the ChimeraX QScore bundle with fixed sampling settings
and saves raw per-atom scores, including missing coverage. `compare_map_hands`
compares the same atoms; the agent checks the overlays before choosing a hand.
VESPER DOT scores are not Q-scores. Small or inconsistent Q-score differences
remain unresolved. QScore uses the ChimeraX implementation, which differs slightly
from the original MapQ sampling algorithm. Fitting defaults are a 2 Å translation
grid, 30° initial angular sampling, bandwidth 4, and ten retained placements;
the agent can repeat both fits at a finer grid when needed.

`render_map_model` uses headless PyMOL to return three density-overlay views,
with optional residue selections for local inspection. Whole-model views show
the full density; focused views carve density around the selected atoms.
`render_pdb` renders the model alone, which is not evidence of map fit. DAQ
coloring is red low / white zero / blue high, with unscored residues gray.
All image tools return PNGs to the model's visual context.
On module-based clusters such as Zebra, the default `/usr/local/bin/pymol`
starts interactive Bash and can suspend background agents. The tools resolve
the module-provided noninteractive executable instead; `PYMOL_EXECUTABLE` may
also specify that executable explicitly.

After CryoZeta completes, the agent independently fits each candidate to both
hands with VESPER and computes DAQ with equivalent settings. `compare_map_hands`
rejects mismatched settings or residue identities and saves paired coverage and
scores. If the selected candidate has a **strictly higher matched raw AA DAQ
mean in the opposite hand**, the agent submits that saved map copy to CryoZeta
again using the same complete assembly FASTA and resolution, downloads the new
model, and validates it. A map flip or rigid fit of the old model is not the final
result. Ties and missing common coverage do not trigger a rerun. Each hand is
modeled at most once per request to avoid repeated flip/rerun cycles. A
validation-only request returns a recommendation without submitting a job.

The LLM orchestrates the workflow from `prompts.py`; tools compute searches,
transforms, scores and the comparison deterministically. Submission order,
visual evidence review, and job-history bookkeeping are prompt requirements,
not a separate enforced state machine. Existing artifacts are not recomputed.
Both the terminal and app default to a 160-turn limit to accommodate the
precheck and validation of one corrective rerun; the app also keeps
conversation memory in SQLite and accepts an explicit turn-limit override.

`bash` runs bounded local command-line workflows from an in-workspace working
directory. It returns stdout, stderr, and the exit code, and strips
credential-like environment variables before launching the subprocess.

The CryoZeta tools authenticate with the Kihara Lab EM server, submit a map
plus protein/DNA/RNA FASTA inputs, check job state, and download a completed
mmCIF model. Credentials are read by deterministic client code and are never
accepted as agent tool arguments.

The terminal streams observable agent activity while the run is in progress:

- model-provided reasoning summaries under `[reasoning]`
- tool names and bounded arguments under `[tool]`
- bounded tool results under `[tool result]`
- the final response under `[answer]`

Reasoning summaries explain the agent's approach without exposing private
hidden chain-of-thought. Image payloads are shown as readable markers rather
than base64 data.

## Install

From this directory:

```bash
uv sync
```

Install PyMOL and ChimeraX (macOS application bundles are detected). In ChimeraX,
install its Q-score extension once:

```text
toolshed install QScore
```

The headless bridge stages its Python script and `config.json` together and runs
`chimerax --nogui --exit --script chimerax_support.py` from that directory.
Configuration and workspace paths are passed through JSON, not the launcher's
command line. On module-based clusters such as Zebra, the default
`/usr/local/bin/chimerax` launches interactive Bash and can stop parallel tools
with `SIGTTIN`. The bridge resolves the noninteractive binary with
`module load chimerax` or accepts its path in `CHIMERAX_EXECUTABLE`.

VESPER needs a C compiler, FFTW and OpenMP. On macOS these libraries can be
installed with `brew install fftw libomp`. Then build the pinned upstream revision:

```bash
uv run python setup_vesper.py
```

This installs VESPER revision `0670f93e1164cec9dd74310af28b545fb2b3d84b` into
the ignored `.deps/vesper/` directory. Alternatively, set `VESPER_EXECUTABLE` to
a compatible VESPER binary that emits physical `MTX` and `T` transforms, or put
`VESPER` on PATH. The upstream VESPER license remains applicable. On Linux,
install GCC and FFTW development libraries before running the setup script.

The process reads `OPENAI_API_KEY` from the environment. It does not copy or
write the key.

To use CryoZeta, also provide the EM-server account through environment
variables. Keep these values out of prompts and repository files:

```bash
export CRYOZETA_USERNAME="your-email@example.edu"
read -s CRYOZETA_PASSWORD
export CRYOZETA_PASSWORD
```

`CRYOZETA_BASE_URL` is optional and defaults to
`https://em.kiharalab.org`.

## Run

```bash
uv run python modeller.py "validate the model data/8g3k.pdb with map data/emd_29700.map please and make a nice plot and visualization"
```

To submit the bundled public example to CryoZeta:

```bash
uv run python modeller.py "submit a CryoZeta structure-modeling job using map data/emd_29700.map, protein FASTA data/8g3k.fasta, contour 0.56, and resolution 2.2 angstroms"
```

CryoZeta jobs can take hours. After submission, the agent calls
`wait_for_cryozeta`, which checks immediately and then every 30 minutes without
a deadline until the server reports success. A terminal failure ends the wait
with an error. Ctrl-C stops the local wait without cancelling the remote job.

After `uv sync`, this equivalent command also works while the virtual
environment is active:

```bash
python modeller.py "validate the model data/8g3k.pdb with map data/emd_29700.map please and make a nice plot and visualization"
```

The bundled example pair is EMDB EMD-29700 with its fitted PDB model 8G3K.
The depositor-recommended contour is `0.56`; include it in a request if you want
the agent to pass that exact value to DAQ.

## Layout

Run offline tests from `modeling_agent/`:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

To check only ChimeraX startup and configuration transport, without opening any
structure or map data:

```bash
CRYOPILOT_TEST_CHIMERAX=1 .venv/bin/python -m unittest discover -s tests -p test_chimerax_support.py -v
```

Optional smoke tests exercise installed VESPER, ChimeraX and PyMOL, plus a
public crambin sequence search/download from PDB. They do not submit CryoZeta jobs:

```bash
CRYOPILOT_TEST_VESPER=1 CRYOPILOT_TEST_PDB=1 CRYOPILOT_TEST_QSCORE=1 CRYOPILOT_TEST_PYMOL=1 .venv/bin/python -m unittest discover -s tests -v
```

## Files

```text
modeling_agent/
  modeller.py             terminal entry point
  prompts.py              concise agent instructions
  tools.py                local agent tools
  cryozeta_client.py      EM-server authentication and job API client
  daq/daq_compute.py      standalone DAQ bridge
  map_utils.py           MRC coordinates, 1 Å resampling, and map-only reflection
  pdb_homologs.py         experimental PDB search and reference extraction
  vesper_align.py         VESPER fitting and atomic coordinate transforms
  setup_vesper.py         pinned local VESPER build
  chimerax_support.py     density simulation and Q-score bridge
  handedness.py          matched-coverage Q-score/DAQ comparison
  vendor/daqcore/         copied DAQ inference code and license
  models/                 local ONNX and MLX DAQ weights
  data/                   downloaded EMDB/PDB example pair
  outputs/                generated DAQ and PyMOL artifacts
  tests/                  offline CryoZeta client contract tests
```
