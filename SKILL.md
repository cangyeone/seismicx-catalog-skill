---
name: seismicx-catalog
description: End-to-end earthquake detection and automatic catalog production from local or continuous waveform directories. Use when Codex needs to process seismic waveform data such as MSEED, SAC, SEED, or any ObsPy-readable format for waveform scanning, phase detection/picking, user-selected phases, first-motion polarity, REAL or GaMMA phase association, earthquake location with a velocity model including optional cangyeone/bayes_location integration, ML magnitude calculation, activity analysis, Cartopy event mapping, and HASH/pyhash focal-mechanism workflows.
---

# SeismicX Catalog

## Overview

Use this skill to help a user run the full earthquake detection workflow from waveform archives to a reproducible earthquake catalog. Prefer a transparent pipeline with explicit intermediate CSV files over a hidden monolithic run.

The bundled helper is `scripts/seismicx_catalog.py`. It scans ObsPy-readable waveform files, detects and picks phases with the bundled SeismicX PNSN TorchScript picker, associates multi-station phases, locates events with a velocity model, calculates ML, estimates P first motion, computes or exports focal-mechanism inputs, summarizes activity, plots event maps, and can locally clone/build optional engines.

## Agent Compatibility

Use this repository as a generic agent skill. `SKILL.md` is the canonical workflow. `AGENTS.md` is provided for OpenCode and other agents that load the AGENTS.md convention. `CLAUDE.md` is provided for Claude Code. All entry files point to the same helper script and references.

## Workflow

1. Confirm inputs: waveform directory, station metadata, response or StationXML if ML is required, velocity model, desired phases, and preferred association/location engines.
2. For a one-shot production-oriented earthquake detection-to-catalog run, use the end-to-end wrapper with GaMMA when it is installed. Use `--association-method simple --smoke-test-simple` only for smoke tests or tiny examples:
   `python scripts/seismicx_catalog.py catalog -w <waveforms> -s stations.csv -v velocity_model.csv -o work/catalog_run --association-method gamma --picker torchscript-pnsn --model pnsn-v3 --phases Pg,Sg,Pn,Sn`
3. For controlled production work, scan waveforms first:
   `python scripts/seismicx_catalog.py scan -w <waveforms> -o work/waveforms.csv --errors work/waveform_errors.csv`
4. Inspect bundled models when needed:
   `python scripts/seismicx_catalog.py list-models`
5. Pick phases. Let the user choose phases such as `Pg,Sg,Pn,Sn`; use the bundled `pnsn-v3` model by default. Do not apply bandpass, highpass, or lowpass filtering to continuous waveforms before or during phase picking. Use `classic` only as a no-model smoke-test fallback:
   `python scripts/seismicx_catalog.py pick -w <waveforms> -o work/picks.csv --picker torchscript-pnsn --model pnsn-v3 --phases Pg,Sg,Pn,Sn`
6. If REAL, HASH, pnsn, `bayes_location`, or `seismological-ai-tools` are needed locally, download/build them before the dependent step:
   `python scripts/seismicx_catalog.py build-tools --tool all --tools-dir external --skip-build -o work/tools_manifest.json`
   Use `--tool real` without `--skip-build` to compile REAL when `gfortran` is available. Use `--tool hash --hash-source ./pyhash` only when a local HASH/pyhash source tree is present.
7. Associate picks with the user's selected engine and always write associated picks with `event_id`:
   `python scripts/seismicx_catalog.py associate --method gamma -p work/picks.csv -s stations.csv -o work/events_gamma.csv --assignments work/assignments.csv --associated-picks work/picks_associated.csv`
8. Recompute or QC first motion on associated picks:
   `python scripts/seismicx_catalog.py polarity -p work/picks_associated.csv -o work/picks_with_polarity.csv`
9. Locate associated events. Always require a velocity model for production work. Use the grid solver for a baseline, or export/run `cangyeone/bayes_location` through `--method bayes` when the user requests it:
   `python scripts/seismicx_catalog.py locate --method grid -p work/picks_with_polarity.csv -s stations.csv -v velocity_model.csv -o work/events_located.csv`
10. Calculate ML with the seedtools-style response simulation and amplitude measurement path. Prefer StationXML, RESP directories, dataless metadata, or a seedtools `sta.resp.path` mapping; choose the regional curve such as `R13`:
   `python scripts/seismicx_catalog.py magnitude-ml -e work/events_located.csv -p work/picks_with_polarity.csv -s stations.csv --inventory stations.xml --region R13 -o work/events_ml.csv --station-output work/station_ml.csv`
11. Compute a first-motion focal-mechanism baseline or export HASH input, then produce the final merged catalog:
   `python scripts/seismicx_catalog.py mechanism -e work/events_ml.csv -p work/picks_with_polarity.csv -s stations.csv -o work/mechanisms.csv --catalog-output work/catalog_final.csv --hash-input work/hash_input.csv`
12. Analyze and plot:
   `python scripts/seismicx_catalog.py analyze -e work/catalog_final.csv -o work/activity.json --rate-output work/daily_counts.csv`
   `python scripts/seismicx_catalog.py plot-map -e work/catalog_final.csv -s stations.csv -o work/catalog_map.png`

## Reference Routing

- Read `references/data-contracts.md` before converting user data, designing CSV schemas, or adapting external tool output.
- Read `references/association-location.md` before using REAL, GaMMA, the grid locator, or `cangyeone/bayes_location`.
- Read `references/focal-mechanism.md` before computing first motions, building HASH/pyhash, or producing focal-mechanism products.
- Read `references/quality-control.md` before final catalog delivery, ML interpretation, activity summaries, or maps.
- Read `references/legacy-mapping.md` when migrating the local `seedtools.share.version`, `pyhash`, `pnsn`, or `seismological-ai-tools` materials into the clean workflow.

## Operating Rules

- Do not commit or publish raw waveform archives, generated catalogs, large model weights, compiled binaries, `.so` files, or external cloned repositories unless the user explicitly asks. Compact in-house bundled models listed in `assets/models/model_registry.json` are part of this skill.
- Keep continuous-waveform phase picking unfiltered. Do not pre-filter files for the picker, and do not add bandpass/highpass/lowpass processing to the PNSN picking path. Filtering belongs only to explicitly requested classic-picker experiments or to later response/magnitude processing.
- Keep each intermediate artifact explicit: waveform scan, picks, association assignments, located events, station magnitudes, polarity table, and plots.
- Record the selected phases, picker, association method, velocity model, magnitude method, response source, regional R curve, and external tool commit paths in the final run notes.
- Treat automatic catalogs as candidates until QC checks pass: station coverage, duplicate picks, event residuals, ML outliers, first-motion quality, and map sanity.
