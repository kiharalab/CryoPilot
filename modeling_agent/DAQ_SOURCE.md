# DAQ provenance

The local DAQ implementation was copied from:

`/Users/pranavpunuru/Code/PhD/DAQweb`

Included components:

- `daqcore/daqcolor/src/{compute,constants,onnx_model,mlx_model}.py`
- `daqcore/LICENSE`
- `models/Multimodel.onnx`
- `models/Multimodel.mlx.npz`
- the standalone workflow from `scripts/daq_web_compute.py`, adapted in
  `daq/daq_compute.py` to emit paths, a residue CSV, and a PNG plot

The DAQweb React UI, Node server, Chrome extension, ChimeraX GUI modules, and
unrelated build assets are not included.

The bridge now standardizes MRC axes and physical origins, resamples to 1 Å,
scores candidate models separately, and exports raw residue scores plus optional
chain-local smoothing and coverage. SS assignment is a Cα-distance heuristic,
not DSSP. The vendored grid scoring retains DAQweb's contour-filtered map-point
reference distribution, not original DAQ's model-position reference; combined
AA/CA/SS is an arithmetic average, not a separately calibrated validation score.
See the [official DAQ workflow](https://github.com/kiharalab/DAQ) for the original
definition and 1 Å preprocessing requirements.

Model SHA-256 checksums:

```text
767abc50fd5eebbdb2cc14b27e89daf301731a977cae6541aaccf09aac5b249d  Multimodel.onnx
969511700353dbd120bef8b4df542af0802f0a170cb111e72b06d03068b1b532  Multimodel.mlx.npz
```
