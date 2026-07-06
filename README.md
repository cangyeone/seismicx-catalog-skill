![SeismicX Catalog](logo.png)

# SeismicX Catalog Skill

Agent-friendly workflows and helper tools for automated earthquake cataloging from local seismic waveform archives.

## What It Does

This repository packages a publishable Codex skill for end-to-end earthquake automatic catalog work:

- Analyze waveform directory structure and scan MSEED, SAC, SEED, and other ObsPy-readable formats.
- Pick user-selected phases such as P/S, Pg/Sg, or Pn/Sn with a clean classic fallback or SeisBench model wrappers.
- Associate picks with GaMMA or REAL, with local build support for REAL.
- Locate events with an explicit velocity model, including a hook for `cangyeone/bayes_location`.
- Calculate local magnitude ML with station-level outputs.
- Summarize seismic activity and plot locations with Cartopy when available.
- Estimate P first-motion polarity and support HASH/pyhash focal-mechanism workflows with local build support.

## Repository Layout

```text
seismicx-catalog/
  SKILL.md
  agents/openai.yaml
  scripts/seismicx_catalog.py
  references/
  assets/
logo.png
README.md
LICENSE
```

Large waveform examples, trained model weights, compiled binaries, and external repositories are intentionally not published in the skill package.

## Quick Start

```bash
python seismicx-catalog/scripts/seismicx_catalog.py init-config -o work/seismicx_catalog.yaml
python seismicx-catalog/scripts/seismicx_catalog.py scan -w <waveforms> -o work/waveforms.csv
python seismicx-catalog/scripts/seismicx_catalog.py pick -w <waveforms> -o work/picks.csv --phases Pg,Sg
python seismicx-catalog/scripts/seismicx_catalog.py associate --method gamma -p work/picks.csv -s stations.csv -o work/events.csv
python seismicx-catalog/scripts/seismicx_catalog.py locate -p work/picks_associated.csv -s stations.csv -v velocity_model.csv -o work/events_located.csv
```

Optional local tool builds:

```bash
python seismicx-catalog/scripts/seismicx_catalog.py build-tools --tool real --tools-dir external -o work/build_manifest.json
python seismicx-catalog/scripts/seismicx_catalog.py build-tools --tool hash --hash-source ./pyhash -o work/hash_build_manifest.json
```

## Related Tools

- [GaMMA](https://github.com/AI4EPS/GaMMA)
- [REAL](https://github.com/Dal-mzhang/REAL)
- [bayes_location](https://github.com/cangyeone/bayes_location)
- [pnsn](https://github.com/cangyeone/pnsn)
- [seismological-ai-tools](https://github.com/cangyeone/seismological-ai-tools)

## Maintainers

- Xin Liu: xinliu_geo@outlook.com
- Yuqi Cai: caiyuqiming@foxmail.com
- Ziye Yu: yuziye@hotmail.com
