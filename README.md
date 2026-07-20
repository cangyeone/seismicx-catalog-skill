![SeismicX Catalog](logo.png)

# SeismicX Catalog Skill

Agent-friendly workflows and helper tools for full earthquake detection and automatic catalog production from local seismic waveform archives. The package is usable from Codex-style skills, OpenCode `AGENTS.md`, Claude Code `CLAUDE.md`, or any agent that can read Markdown instructions and run local scripts.

## Supported Agent Tools

This skill is not limited to OpenCode. It is designed to work with multiple agent tools:

- Codex or Codex-style skill runners: use `SKILL.md` as the canonical skill file.
- OpenCode and other agents that read repository instructions: use `AGENTS.md`.
- Claude Code: use `CLAUDE.md`.
- Generic local coding agents: read `SKILL.md` first, then use the bundled script and references as needed.

## Install The Skill

In OpenCode, Codex, or another agent that can clone repositories, start a session and type:

```text
Download https://github.com/cangyeone/seismicx-catalog-skill and install it as a SKILL.
```

The agent should clone this repository, keep `SKILL.md` in the skill root, and place the folder where that tool loads user skills. For Codex-style tools, the important file is `SKILL.md`. For OpenCode-style tools, the repository also provides `AGENTS.md`. For Claude Code, the repository provides `CLAUDE.md`.

If the tool supports manual installation, clone the repository into its user-skill directory and keep the repository layout unchanged.

## Use It In An Agent

After installation, open a project directory that contains your seismic data and type the same kind of request in OpenCode, Codex, Claude Code, or another local coding agent:

```text
Based on the data in the current directory, build an earthquake catalog.
```

The skill is designed so the agent can run the full detection-to-catalog workflow:

1. scan the waveform directory;
2. detect and pick phases from continuous waveforms without filtering them;
3. associate multi-station picks with GaMMA or REAL;
4. locate events with a velocity model;
5. calculate ML magnitude;
6. estimate first-motion polarity and focal mechanisms when enough data exist;
7. write the final catalog, station magnitudes, activity summary, and maps.

For best results, give the agent the key paths in plain language:

```text
My continuous waveforms are in ./waveforms, stations are in ./stations.csv,
the velocity model is ./velocity_model.csv, and the StationXML file is ./stations.xml.
Use the PNSN picker, Pg/Sg/Pn/Sn phases, GaMMA association, and R13 ML.
```

Useful input files are:

- waveform directory: MSEED, SAC, SEED, or any ObsPy-readable waveform format;
- station table: start from `assets/stations_template.csv` if needed;
- velocity model: start from `assets/velocity_model_example.csv` if needed;
- response metadata for calibrated ML: StationXML, RESP/dataless metadata, or a seedtools-style response mapping;
- optional external tools: REAL, HASH/pyhash, `bayes_location`, PNSN, or `seismological-ai-tools`.

Do not bandpass, highpass, or lowpass continuous waveforms before the phase-picking step. The bundled PNSN model expects the original waveform stream.

## What It Does

This repository packages a publishable, agent-agnostic skill for the full earthquake detection workflow, from continuous waveform directories to a final catalog with location, magnitude, and focal-mechanism products:

- Analyze waveform directory structure and scan MSEED, SAC, SEED, and other ObsPy-readable formats.
- Detect earthquake phases and pick user-selected arrivals such as Pg/Sg/Pn/Sn with the bundled SeismicX PNSN TorchScript model. Continuous-waveform picking is intentionally unfiltered; the classic picker is only a smoke-test fallback.
- Associate picks with GaMMA or the bundled optimized Python REAL backend; optionally build C REAL for layered travel-time tables.
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
scripts/seismicx_real.py
references/
assets/
logo.png
README.md
LICENSE
```

Large waveform examples, large model weights, compiled binaries, and external repositories are intentionally not published in the skill package. The repository does include compact in-house bundled models under `assets/models/`: `pnsn.v3.jit` for Pg/Sg/Pn/Sn picking and `polar.jit` for first-motion polarity workflows.

## Manual CLI Quick Start

Most users can work through OpenCode or another agent with the natural-language prompts above. The commands below are for manual runs, debugging, or reproducing what the agent does.

One-command baseline detection-to-catalog run:

```bash
python scripts/seismicx_catalog.py catalog \
  -w <waveforms> \
  -s stations.csv \
  -v velocity_model.csv \
  -o work/catalog_run \
  --association-method gamma
```

The final merged catalog is written to `work/catalog_run/catalog_final.csv` and includes detected events, associated phases, locations, ML, and focal-mechanism columns when enough data are available. Use `--association-method simple --smoke-test-simple` only for tiny smoke tests that intentionally use the simple time-window associator.

Use the bundled Python REAL backend in the complete workflow with:

```bash
python scripts/seismicx_catalog.py catalog \
  -w <waveforms> \
  -s stations.csv \
  -v velocity_model.csv \
  -o work/catalog_run \
  --association-method real \
  --real-min-score 0.2
```

## Dependencies

Core waveform operations require ObsPy. The bundled PNSN picker requires PyTorch. The bundled Python REAL backend requires NumPy and uses Numba automatically when available. GaMMA can be installed into the active environment with:

```bash
python -m pip install "git+https://github.com/wayneweiqiang/GaMMA.git"
```

The contributor smoke test also exercises SciPy, Matplotlib/Cartopy-capable plotting paths when available, and the bundled TorchScript models:

```bash
python scripts/smoke_test.py
```

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

REAL can be selected without downloading or compiling another repository:

```bash
python scripts/seismicx_catalog.py associate \
  --method real \
  -p work/picks.csv \
  -s stations.csv \
  -o work/events_real.csv \
  --assignments work/assignments.csv \
  --associated-picks work/picks_associated.csv \
  --workdir work/real \
  --real-R 0.5/20/0.05/2/5 \
  --real-S 3/2/5/2/0.5/0.1/1.5 \
  --real-V 6.2/3.5
```

`events_real.csv` includes residual scatter, P/S counts, stations with both phases, and azimuth gap. `work/real/real_run.json` records the parameters, skipped/usable pick counts, runtime, and whether Numba acceleration was active. For dense AI picks, tune `--real-min-score` and the `S` thresholds against a reviewed time window. Use compiled C REAL through `--real-command` when a layered `-G` travel-time table is required.

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
