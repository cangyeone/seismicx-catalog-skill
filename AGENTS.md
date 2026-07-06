# SeismicX Catalog Agent Instructions

This repository is an agent-agnostic skill for the full earthquake detection-to-catalog workflow. Use it when a user asks for earthquake phase detection/picking, multi-station phase association, event location, ML magnitude, activity analysis, mapping, or focal-mechanism workflows from local waveform data.

## Canonical Workflow

- Read `SKILL.md` first; it is the source of truth for the workflow.
- Load `references/data-contracts.md` before converting waveform, pick, event, station, magnitude, or mechanism tables.
- Load `references/association-location.md` before REAL, GaMMA, grid location, or `bayes_location` work.
- Load `references/focal-mechanism.md` before first-motion polarity, HASH/pyhash, or focal-mechanism products.
- Load `references/quality-control.md` before final delivery.

## Core Commands

```bash
python scripts/seismicx_catalog.py list-models
python scripts/seismicx_catalog.py catalog -w <waveforms> -s stations.csv -v velocity_model.csv -o work/catalog_run
python scripts/seismicx_catalog.py build-tools --tool all --tools-dir external --skip-build -o work/tools_manifest.json
```

The final one-shot output is `work/catalog_run/catalog_final.csv`, produced after waveform scanning, phase detection, association, location, ML, and focal-mechanism steps. For production association, prefer GaMMA or REAL over the simple smoke-test associator.

ML magnitude calculation should follow the seedtools-style path: remove response to velocity, simulate/integrate to displacement, measure SME/SMN horizontal amplitudes in micrometers, and apply the selected regional R curve such as `R13`. Treat raw-scaled ML as a smoke-test fallback only.

## External Tools

- Download PNSN, REAL, `bayes_location`, and `seismological-ai-tools` with `build-tools --tool all --skip-build`.
- Compile REAL with `build-tools --tool real` when `gfortran` is available.
- Compile HASH/pyhash only from an explicit local source tree: `build-tools --tool hash --hash-source ./pyhash`.
- Do not commit external cloned repositories, generated catalogs, raw waveform archives, compiled binaries, or large model weights.

## Validation

Before finishing changes, run:

```bash
python -m py_compile scripts/seismicx_catalog.py
python scripts/seismicx_catalog.py list-models
```

If the skill structure changed, also run the local skill validator when available:

```bash
python /Users/yuziye/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
```
