# SeismicX Catalog Claude Code Context

This repository is a generic agent skill, not a Claude-only project. Use `SKILL.md` as the canonical workflow and treat this file as a short Claude Code entrypoint.

## What To Do

- For end-to-end cataloging, run:

```bash
python scripts/seismicx_catalog.py catalog -w <waveforms> -s stations.csv -v velocity_model.csv -o work/catalog_run
```

- The final catalog is `work/catalog_run/catalog_final.csv`.
- ML should use the seedtools-style response simulation path: response removal to velocity, velocity-to-displacement simulation, SME/SMN horizontal amplitude measurement in micrometers, and a regional R curve such as `R13`.
- Use `references/` only as needed:
  - `data-contracts.md` for schemas and converters.
  - `association-location.md` for REAL, GaMMA, grid location, and `bayes_location`.
  - `focal-mechanism.md` for first-motion, HASH/pyhash, and mechanism outputs.
  - `quality-control.md` for final checks.

## Tool Download / Build

```bash
python scripts/seismicx_catalog.py build-tools --tool all --tools-dir external --skip-build -o work/tools_manifest.json
python scripts/seismicx_catalog.py build-tools --tool real --tools-dir external -o work/real_build_manifest.json
python scripts/seismicx_catalog.py build-tools --tool hash --hash-source ./pyhash -o work/hash_build_manifest.json
```

`--tool all --skip-build` downloads PNSN, REAL, `bayes_location`, and `seismological-ai-tools`. HASH/pyhash is explicit because deployments use different source trees and Fortran build setups.

## Guardrails

- Keep raw waveform archives, generated catalogs, external cloned repositories, compiled binaries, and large model weights out of git.
- Keep compact bundled in-house models under `assets/models/`.
- Prefer explicit intermediate files: waveform scan, picks, associated picks, located events, ML, mechanisms, and final catalog.
- Validate script edits with `python -m py_compile scripts/seismicx_catalog.py` and `python scripts/seismicx_catalog.py list-models`.
