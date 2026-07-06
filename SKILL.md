---
name: seismicx-catalog
description: Automated earthquake catalog construction from local or continuous waveform directories. Use when Codex needs to process seismic waveform data such as MSEED, SAC, SEED, or any ObsPy-readable format for phase picking, first-motion polarity, REAL or GaMMA phase association, earthquake location with a velocity model including optional cangyeone/bayes_location integration, ML magnitude calculation, activity analysis, Cartopy event mapping, and HASH/pyhash focal-mechanism workflows.
---

# SeismicX Catalog

## Overview

Use this skill to help a user turn waveform archives into a reproducible earthquake catalog. Prefer a transparent pipeline with explicit intermediate CSV files over a hidden monolithic run.

The bundled helper is `scripts/seismicx_catalog.py`. It scans ObsPy-readable waveform files, runs a clean classic picker or SeisBench picker wrapper, estimates P first motion, prepares or runs association, locates events with a velocity model, calculates ML, summarizes activity, plots event maps, and can locally clone/build optional engines.

## Workflow

1. Confirm inputs: waveform directory, station metadata, response or StationXML if ML is required, velocity model, desired phases, and preferred association/location engines.
2. Scan waveforms first:
   `python scripts/seismicx_catalog.py scan -w <waveforms> -o work/waveforms.csv --errors work/waveform_errors.csv`
3. Pick phases. Let the user choose phases such as `P,S`, `Pg,Sg`, or `Pn,Sn`. Use SeisBench/AI models when available; otherwise use the classic picker as a documented fallback:
   `python scripts/seismicx_catalog.py pick -w <waveforms> -o work/picks.csv --phases Pg,Sg --picker classic`
4. If REAL, HASH, or pnsn are needed locally, build or clone them before the dependent step:
   `python scripts/seismicx_catalog.py build-tools --tool real --tools-dir external -o work/build_manifest.json`
5. Associate picks with the user's selected engine:
   `python scripts/seismicx_catalog.py associate --method gamma -p work/picks.csv -s stations.csv -o work/events_gamma.csv --assignments work/assignments.csv`
6. Locate associated events. Always require a velocity model for production work. Use the grid solver for a baseline, or export/run `cangyeone/bayes_location` through `--method bayes` when the user requests it.
7. Calculate ML only after confirming amplitude units and response metadata:
   `python scripts/seismicx_catalog.py magnitude-ml -e work/located_events.csv -p work/picks.csv -s stations.csv --inventory stations.xml -o work/events_ml.csv --station-output work/station_ml.csv`
8. Analyze and plot:
   `python scripts/seismicx_catalog.py analyze -e work/events_ml.csv -o work/activity.json --rate-output work/daily_counts.csv`
   `python scripts/seismicx_catalog.py plot-map -e work/events_ml.csv -s stations.csv -o work/catalog_map.png`
9. For focal mechanisms, compute or QC P first motions, then run HASH/pyhash only for events with enough azimuthal coverage and reliable polarities.

## Reference Routing

- Read `references/data-contracts.md` before converting user data, designing CSV schemas, or adapting external tool output.
- Read `references/association-location.md` before using REAL, GaMMA, the grid locator, or `cangyeone/bayes_location`.
- Read `references/focal-mechanism.md` before computing first motions, building HASH/pyhash, or producing focal-mechanism products.
- Read `references/quality-control.md` before final catalog delivery, ML interpretation, activity summaries, or maps.
- Read `references/legacy-mapping.md` when migrating the local `seedtools.share.version`, `pyhash`, `pnsn`, or `seismological-ai-tools` materials into the clean workflow.

## Operating Rules

- Do not commit or publish raw waveform archives, generated catalogs, model weights, compiled binaries, `.so` files, or external cloned repositories unless the user explicitly asks.
- Keep each intermediate artifact explicit: waveform scan, picks, association assignments, located events, station magnitudes, polarity table, and plots.
- Record the selected phases, picker, association method, velocity model, magnitude formula, and external tool commit paths in the final run notes.
- Treat automatic catalogs as candidates until QC checks pass: station coverage, duplicate picks, event residuals, ML outliers, first-motion quality, and map sanity.
