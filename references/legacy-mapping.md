# Legacy Mapping

Use this reference when migrating local project materials into the clean SeismicX workflow.

## seedtools.share.version

The old pipeline was script-numbered and path-specific:

| Legacy file | New role |
| --- | --- |
| `1.makeindex.py` | Replaced by `scan` and direct ObsPy reads unless a miniSEED index is explicitly needed. |
| `2.cutdata.py` | Treat as an event-window extraction reference; keep extraction as a separate preprocessing step. |
| `3.makeavailableresppath.py` | Replace with explicit StationXML/RESP metadata paths. |
| `4.mergemseed.py` | Use ObsPy `Stream.merge` inside preprocessing or user-provided merge scripts. |
| `5.resp2xml.py`, `6.xml2dataless.py` | Replace with StationXML-first metadata handling where possible. |
| `7.pick.py` | Replaced by `pick`; keep old model names only as user-supplied external weights. |
| `8.makejson.py`, `9.json2pha.py` | Replace with canonical CSV picks/events and explicit converters only when needed. |
| `10.mseed2seed.py` | Treat as legacy export, not part of the default catalog skill. |
| `utils/calmag.py` | ML concepts migrated into `magnitude-ml`; regional constants must still be reviewed. |

Do not publish bundled `.jit`, `.onnx`, `.pt`, waveform examples, jar files, generated `odata`, or compiled binaries from the legacy tree unless the user explicitly requests a separate data/model release.

## pyhash

The local `pyhash` tree is useful for HASH integration, but contains compiled artifacts and Python-version-specific cache files. Use it as source material only:

- Keep build instructions in the skill.
- Build locally with `build-tools --tool hash`.
- Do not commit `.so`, `.o`, `__pycache__`, or generated drivers.

## pnsn

Clone `cangyeone/pnsn` locally when the user needs PNSN-specific pickers, station conventions, or examples:

```bash
python scripts/seismicx_catalog.py build-tools --tool pnsn --tools-dir external
```

Keep it as a local reference or external dependency unless the user asks to vendor selected files.

Useful defaults observed in the local `pnsn` reference:

- Picker input is generally 100 Hz, three-component data.
- ONNX probability threshold examples use `prob = 0.3`; lower thresholds increase recall and false positives.
- NMS examples use `nmslen = 1000` samples, about 10 seconds at 100 Hz.
- Component groups include common broadband and MEMS variants such as `BHE/BHN/BHZ`, `HHE/HHN/HHZ`, `EIE/EIN/EIZ`, and `HNN/HNE/HNZ`.
- GaMMA examples use P/S velocities near `6.0/3.5 km/s`, `dbscan_eps = 10`, and `min_picks_per_eq = 5`.
- REAL examples include starting values such as `R = 0.4/20/0.02/2/5`, `G = 2.0/20/0.01/1`, `V = 6.2/3.5`, and `S = 2/4/8/2/0.5/0.1/1.5`.

Treat these as regional starting points, not universal defaults.

## seismological-ai-tools

Use `cangyeone/seismological-ai-tools` as an optional reference for AI seismic utilities such as phase picking, polarization, dispersion extraction, and related workflows. The useful migration points are:

- `seismic-event-detection`: Pn/Sn-capable 100 Hz pickers, TorchScript/ONNX export examples, picker output format, REAL/FastLink/GaMMA association examples.
- `p-polarity-and-focal-mac`: P first-motion model notes and HASH/hashpy focal-mechanism workflow.
- `hdf5-dataset-tools`: miniSEED/SAC indexing and HDF5 conversion patterns.
- `phase-picking-method-compare`: picker architecture comparison and model export scripts.

Prefer linking or documenting compatible commands instead of copying large modules, model weights, training artifacts, or generated plots into the skill. Note that some referenced repositories or model assets carry non-commercial or redistribution restrictions; keep them as user-managed dependencies unless the license is explicitly compatible with publication.

## Publication Boundary

The publishable skill should contain:

- `SKILL.md`
- `agents/openai.yaml`
- `scripts/seismicx_catalog.py`
- `references/*.md`
- small templates and the logo asset

Everything else belongs in local working directories, external cloned repositories, or user-managed model/data storage.
