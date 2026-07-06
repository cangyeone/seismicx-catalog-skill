![SeismicX Catalog](logo.png)

# SeismicX Catalog Skill

Agent-friendly workflows and helper tools for full earthquake detection and automatic catalog production from local seismic waveform archives. The package is usable from Codex-style skills, OpenCode `AGENTS.md`, Claude Code `CLAUDE.md`, or any agent that can read Markdown instructions and run local scripts.

## What It Does

This repository packages a publishable, agent-agnostic skill for the full earthquake detection workflow, from continuous waveform directories to a final catalog with location, magnitude, and focal-mechanism products:

- Analyze waveform directory structure and scan MSEED, SAC, SEED, and other ObsPy-readable formats.
- Detect earthquake phases and pick user-selected arrivals such as Pg/Sg/Pn/Sn with the bundled SeismicX PNSN TorchScript model. Continuous-waveform picking is intentionally unfiltered; the classic picker is only a smoke-test fallback.
- Associate picks with GaMMA or REAL, with local build support for REAL.
- Locate events with an explicit velocity model, including a hook for `cangyeone/bayes_location`.
- Calculate local magnitude ML with seedtools-style response simulation, horizontal-component amplitude measurement, regional R curves, and station-level outputs.
- Summarize seismic activity and plot locations with Cartopy when available.
- Estimate P first-motion polarity and support HASH/pyhash focal-mechanism workflows with local build support.

## Repository Layout

```text
SKILL.md
AGENTS.md
CLAUDE.md
agents/openai.yaml
scripts/seismicx_catalog.py
references/
assets/
logo.png
README.md
LICENSE
```

Large waveform examples, large model weights, compiled binaries, and external repositories are intentionally not published in the skill package. The repository does include compact in-house bundled models under `assets/models/`: `pnsn.v3.jit` for Pg/Sg/Pn/Sn picking and `polar.jit` for first-motion polarity workflows.

## Quick Start

One-command baseline detection-to-catalog run:

```bash
python scripts/seismicx_catalog.py catalog \
  -w <waveforms> \
  -s stations.csv \
  -v velocity_model.csv \
  -o work/catalog_run
```

The final merged catalog is written to `work/catalog_run/catalog_final.csv` and includes detected events, associated phases, locations, ML, and focal-mechanism columns when enough data are available. Add `--association-method gamma` when GaMMA is installed and configured for production association.

Step-by-step run:

```bash
python scripts/seismicx_catalog.py init-config -o work/seismicx_catalog.yaml
python scripts/seismicx_catalog.py list-models
python scripts/seismicx_catalog.py scan -w <waveforms> -o work/waveforms.csv
python scripts/seismicx_catalog.py pick -w <waveforms> -o work/picks.csv --picker torchscript-pnsn --model pnsn-v3 --phases Pg,Sg,Pn,Sn
python scripts/seismicx_catalog.py associate --method gamma -p work/picks.csv -s stations.csv -o work/events.csv --assignments work/assignments.csv --associated-picks work/picks_associated.csv
python scripts/seismicx_catalog.py polarity -p work/picks_associated.csv -o work/picks_with_polarity.csv
python scripts/seismicx_catalog.py locate -p work/picks_with_polarity.csv -s stations.csv -v velocity_model.csv -o work/events_located.csv
python scripts/seismicx_catalog.py magnitude-ml -e work/events_located.csv -p work/picks_with_polarity.csv -s stations.csv --inventory stations.xml --region R13 -o work/events_ml.csv --station-output work/station_ml.csv
python scripts/seismicx_catalog.py mechanism -e work/events_ml.csv -p work/picks_with_polarity.csv -s stations.csv -o work/mechanisms.csv --catalog-output work/catalog_final.csv --hash-input work/hash_input.csv
```

Do not bandpass, highpass, or lowpass continuous waveforms before the `pick` or `catalog` phase-detection step. The bundled PNSN model expects the original waveform stream. Filtering is only an explicit non-default option for classic STA/LTA smoke tests and for later response/magnitude processing.

Optional local tool builds:

```bash
python scripts/seismicx_catalog.py build-tools --tool pnsn --tools-dir external -o work/pnsn_manifest.json
python scripts/seismicx_catalog.py build-tools --tool bayes-location --tools-dir external -o work/bayes_manifest.json
python scripts/seismicx_catalog.py build-tools --tool seismological-ai-tools --tools-dir external -o work/ai_tools_manifest.json
python scripts/seismicx_catalog.py build-tools --tool real --tools-dir external --skip-build -o work/real_manifest.json
python scripts/seismicx_catalog.py build-tools --tool hash --hash-source ./pyhash -o work/hash_build_manifest.json
```

Use `--tool all --skip-build` to download the standard external reference repositories (`pnsn`, REAL, `bayes_location`, and `seismological-ai-tools`) into `external/`. HASH/pyhash is intentionally explicit because the source tree and Fortran build vary by deployment.

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
