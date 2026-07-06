# Focal Mechanism And First Motion

Use this reference before producing HASH or pyhash focal-mechanism solutions.

## First-Motion Polarity

The helper estimates first motion from vertical P picks:

```bash
python scripts/seismicx_catalog.py polarity \
  -p work/picks.csv \
  -o work/picks_with_polarity.csv
```

Interpretation rules:

- `U` means positive first motion in the trace sign convention.
- `D` means negative first motion.
- `N` means not reliable enough to use.
- `I` is impulsive, `E` is emergent.

Before HASH, verify channel orientation, instrument polarity, station reversals, and whether positive vertical counts correspond to upward ground motion for the local network. Automatic first motion should be reviewed for events used in publications.

## When To Run HASH

Run HASH only when an event has:

- Enough reliable P polarities, usually at least 8.
- Good azimuthal coverage and no single-station dominance.
- A velocity model suitable for takeoff-angle calculation.
- A located hypocenter with reasonable uncertainty.

Keep a table of excluded events and exclusion reasons.

## Building HASH / pyhash

The local project includes a `pyhash`-style source tree with `src/Makefile` that builds a f2py extension. The helper can attempt this build:

```bash
python scripts/seismicx_catalog.py build-tools \
  --tool hash \
  --hash-source ./pyhash \
  --tools-dir external \
  -o work/hash_build_manifest.json
```

Prerequisites:

- `gfortran`
- Python headers for the active interpreter
- `numpy` with `numpy.f2py`

If f2py build fails, try the original Fortran drivers under `hash_fortran/`:

```bash
make -C pyhash/hash_fortran hash_driver2
```

Do not publish compiled `.so`, `.o`, or executable files inside the skill repository. Build them in `external/` or another local workspace.

## HASH Input Preparation

Prepare or adapt a HASH input table containing:

- Event origin time, latitude, longitude, depth, and uncertainty.
- Station code, network, component, station coordinates, and elevation.
- P polarity and onset quality.
- Azimuth, distance, and takeoff-angle information, or enough information for HASH to calculate it from the velocity model.

If using pyhash's ObsPy adapter, ensure the ObsPy `Event` object has a preferred origin, arrivals linked to picks, station metadata, and `Pick.polarity` values set.

## Outputs To Preserve

For each focal mechanism, preserve:

- Strike, dip, rake for both nodal planes.
- HASH quality class.
- Number of polarities used.
- Misfit fraction and station distribution ratio.
- Azimuthal and takeoff-angle gaps.
- A polarity beachball or stereonet plot when requested.
