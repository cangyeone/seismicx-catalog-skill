#!/usr/bin/env python3
"""SeismicX Catalog helper CLI.

This script is intentionally lightweight: it provides clean data contracts,
format conversion, quality-control helpers, and wrappers around established
seismological packages. Heavy models and external engines such as REAL,
GaMMA, HASH, and bayes_location stay outside the skill and are referenced by
path or command template at runtime. Small in-house TorchScript models live
under assets/models and are described by assets/models/model_registry.json.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
MODEL_REGISTRY_PATH = SKILL_ROOT / "assets" / "models" / "model_registry.json"

COMMON_WAVEFORM_EXTENSIONS = {
    ".mseed",
    ".miniseed",
    ".msd",
    ".sac",
    ".seed",
    ".segy",
    ".sgy",
    ".gse",
    ".gcf",
    ".wav",
}

P_PHASES = {"P", "PG", "PN", "PB", "P1"}
S_PHASES = {"S", "SG", "SN", "SB", "S1"}

PICK_FIELDS = [
    "pick_id",
    "event_id",
    "waveform_path",
    "trace_id",
    "network",
    "station",
    "location",
    "channel",
    "phase",
    "time",
    "score",
    "snr",
    "amplitude",
    "polarity",
    "polarity_quality",
    "polarity_score",
    "picker",
]

EVENT_FIELDS = [
    "event_id",
    "origin_time",
    "latitude",
    "longitude",
    "depth_km",
    "magnitude",
    "magnitude_type",
    "rms",
    "n_picks",
    "method",
]

REAL_EVENT_FIELDS = EVENT_FIELDS + [
    "n_p_picks",
    "n_s_picks",
    "n_both_stations",
    "azimuth_gap_deg",
]

MECHANISM_FIELDS = [
    "event_id",
    "mechanism_strike",
    "mechanism_dip",
    "mechanism_rake",
    "mechanism_aux_strike",
    "mechanism_aux_dip",
    "mechanism_aux_rake",
    "mechanism_n_polarities",
    "mechanism_misfit_fraction",
    "mechanism_quality",
    "mechanism_method",
]

STATION_FIELDS = [
    "station_id",
    "network",
    "station",
    "location",
    "longitude",
    "latitude",
    "elevation_m",
]

PNSN_PHASE_MAP = {
    0: "Pg",
    1: "Sg",
    2: "Pn",
    3: "Sn",
    4: "P",
    5: "S",
}

DOWNLOADABLE_TOOL_REPOS = {
    "pnsn": ("https://github.com/cangyeone/pnsn.git", "pnsn"),
    "bayes-location": ("https://github.com/cangyeone/bayes_location.git", "bayes_location"),
    "seismological-ai-tools": ("https://github.com/cangyeone/seismological-ai-tools.git", "seismological-ai-tools"),
}


class CatalogError(RuntimeError):
    """Raised for user-fixable catalog workflow errors."""


@dataclass
class Station:
    station_id: str
    network: str
    station: str
    location: str
    longitude: float
    latitude: float
    elevation_m: float = 0.0


@dataclass
class VelocityLayer:
    depth_km: float
    vp_km_s: float
    vs_km_s: float


def eprint(*parts: object) -> None:
    print(*parts, file=sys.stderr)


def ensure_parent(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def read_csv_rows(path: str | Path) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_model_registry() -> dict[str, dict[str, Any]]:
    if not MODEL_REGISTRY_PATH.exists():
        return {}
    with open(MODEL_REGISTRY_PATH, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return {model["id"]: model for model in payload.get("models", [])}


def resolve_model_path(model: str | None, default_model_id: str = "pnsn-v3") -> tuple[Path, dict[str, Any] | None]:
    registry = read_model_registry()
    selected = model or default_model_id
    if selected in registry:
        metadata = registry[selected]
        return MODEL_REGISTRY_PATH.parent / metadata["file"], metadata
    candidate = Path(selected)
    if candidate.exists():
        return candidate, None
    skill_relative = SKILL_ROOT / selected
    if skill_relative.exists():
        return skill_relative, None
    raise CatalogError(f"Model not found: {selected}. Run `python scripts/seismicx_catalog.py list-models` to see bundled models.")


def write_csv_rows(
    path: str | Path,
    rows: Iterable[dict[str, Any]],
    fieldnames: Sequence[str] | None = None,
) -> None:
    materialized = [dict(row) for row in rows]
    if fieldnames is None:
        keys: list[str] = []
        for row in materialized:
            for key in row:
                if key not in keys:
                    keys.append(key)
        fieldnames = keys
    ensure_parent(path)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(materialized)


def parse_float(value: Any, default: float = float("nan")) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        text = text.replace("T", " ")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise CatalogError(f"Cannot parse datetime: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def datetime_to_text(value: Any) -> str:
    if hasattr(value, "datetime"):
        value = value.datetime
    if isinstance(value, datetime):
        dt = value
    else:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def to_epoch_seconds(value: Any) -> float:
    return parse_datetime(value).timestamp()


def station_id(network: str, station: str, location: str = "") -> str:
    return ".".join([network or "", station or "", location or ""]).rstrip(".")


def phase_group(phase: str) -> str:
    phase_upper = (phase or "").upper()
    if phase_upper in P_PHASES or phase_upper.startswith("P"):
        return "P"
    if phase_upper in S_PHASES or phase_upper.startswith("S"):
        return "S"
    return phase_upper


def choose_phase_label(base_phase: str, requested: set[str]) -> str | None:
    base = phase_group(base_phase)
    if base == "P":
        options = [p for p in requested if phase_group(p) == "P"]
    elif base == "S":
        options = [p for p in requested if phase_group(p) == "S"]
    else:
        options = [p for p in requested if p == base]
    if not options:
        return None
    if base in options:
        return base
    return sorted(options)[0].title()


def iter_waveform_files(root: str | Path, extensions: str = "common") -> Iterable[Path]:
    path = Path(root)
    if path.is_file():
        yield path
        return
    if extensions == "all":
        allowed: set[str] | None = None
    elif extensions == "common":
        allowed = COMMON_WAVEFORM_EXTENSIONS
    else:
        allowed = {ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}" for ext in extensions.split(",")}
    for current, _, names in os.walk(path):
        for name in names:
            candidate = Path(current) / name
            if allowed is None or candidate.suffix.lower() in allowed:
                yield candidate


def import_obspy() -> Any:
    try:
        import obspy
    except ImportError as exc:
        raise CatalogError("ObsPy is required for waveform operations. Install with: pip install obspy") from exc
    return obspy


def read_waveform(path: str | Path, headonly: bool = False) -> Any:
    obspy = import_obspy()
    return obspy.read(str(path), headonly=headonly)


def waveform_path_tokens(value: str | Path) -> list[str]:
    return [part for part in str(value).split(";") if part]


def combine_waveform_paths(rows: Iterable[dict[str, str]]) -> str:
    paths: list[str] = []
    for row in rows:
        for path in waveform_path_tokens(row.get("waveform_path", "")):
            if path and path not in paths:
                paths.append(path)
    return ";".join(paths)


def join_waveform_paths(paths: Iterable[str]) -> str:
    return ";".join(sorted(path for path in paths if path))


def read_waveform_field(value: str | Path, headonly: bool = False) -> Any:
    paths = waveform_path_tokens(value)
    if len(paths) <= 1:
        return read_waveform(paths[0] if paths else value, headonly=headonly)
    obspy = import_obspy()
    stream = obspy.Stream()
    for path in paths:
        stream += obspy.read(path, headonly=headonly)
    return stream


def read_stations(path: str | Path) -> dict[str, Station]:
    source = Path(path)
    lines = [line.strip() for line in source.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise CatalogError(f"Station file is empty: {path}")

    stations: dict[str, Station] = {}
    first = lines[0]
    if "," in first:
        for row in read_csv_rows(source):
            net = row.get("network") or row.get("net") or ""
            sta = row.get("station") or row.get("sta") or ""
            loc = row.get("location") or row.get("loc") or ""
            lon = parse_float(row.get("longitude") or row.get("lon"))
            lat = parse_float(row.get("latitude") or row.get("lat"))
            elev = parse_float(row.get("elevation_m") or row.get("elev_m") or row.get("elevation"), 0.0)
            sid = row.get("station_id") or station_id(net, sta, loc)
            stations[sid] = Station(sid, net, sta, loc, lon, lat, elev)
    else:
        header_tokens = [token.lower() for token in first.split()]
        has_header = {"network", "station"}.issubset(header_tokens) or {"net", "sta"}.issubset(header_tokens)
        data_lines = lines[1:] if has_header else lines
        for line in data_lines:
            parts = line.split()
            if len(parts) < 5:
                continue
            net, sta, loc = parts[0], parts[1], parts[2]
            lon, lat = float(parts[3]), float(parts[4])
            elev = float(parts[5]) if len(parts) > 5 else 0.0
            sid = station_id(net, sta, loc)
            stations[sid] = Station(sid, net, sta, loc, lon, lat, elev)

    if not stations:
        raise CatalogError(f"No stations could be parsed from: {path}")

    aliases = {}
    for sid, sta in stations.items():
        aliases[sid] = sta
        aliases[station_id(sta.network, sta.station)] = sta
    return aliases


def canonical_station_values(stations: dict[str, Station]) -> list[Station]:
    unique: dict[tuple[str, str, str], Station] = {}
    for sta in stations.values():
        key = (sta.network, sta.station, sta.location)
        unique.setdefault(key, sta)
    return list(unique.values())


def read_velocity_model(path: str | Path | None) -> list[VelocityLayer]:
    if not path:
        return [VelocityLayer(0.0, 6.0, 3.5)]
    lines = [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise CatalogError(f"Velocity model is empty: {path}")
    layers: list[VelocityLayer] = []
    header = [token.lower() for token in lines[0].replace(",", " ").split()]
    has_header = "depth_km" in header or "vp_km_s" in header or "vp" in header
    for line in (lines[1:] if has_header else lines):
        parts = line.replace(",", " ").split()
        if len(parts) < 3:
            continue
        layers.append(VelocityLayer(float(parts[0]), float(parts[1]), float(parts[2])))
    if not layers:
        raise CatalogError(f"No velocity layers could be parsed from: {path}")
    return sorted(layers, key=lambda layer: layer.depth_km)


def velocity_at_depth(layers: Sequence[VelocityLayer], depth_km: float, phase: str) -> float:
    selected = layers[0]
    for layer in layers:
        if depth_km >= layer.depth_km:
            selected = layer
    return selected.vp_km_s if phase_group(phase) == "P" else selected.vs_km_s


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    try:
        from obspy.geodetics import gps2dist_azimuth

        return gps2dist_azimuth(lat1, lon1, lat2, lon2)[0] / 1000.0
    except Exception:
        radius_km = 6371.0
        lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2.0) ** 2
        return radius_km * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def project_lonlat(lon: float, lat: float, lon0: float, lat0: float) -> tuple[float, float]:
    x = (lon - lon0) * 111.32 * math.cos(math.radians(lat0))
    y = (lat - lat0) * 110.57
    return x, y


def unproject_lonlat(x_km: float, y_km: float, lon0: float, lat0: float) -> tuple[float, float]:
    cos_lat = math.cos(math.radians(lat0))
    lon = lon0 if abs(cos_lat) < 1e-12 else lon0 + x_km / (111.32 * cos_lat)
    lat = lat0 + y_km / 110.57
    return lon, lat


def stream_trace_id(trace: Any) -> str:
    stats = trace.stats
    return station_id(stats.network, stats.station, stats.location) + f".{stats.channel}"


def infer_trace_parts(trace_id: str) -> dict[str, str]:
    parts = (trace_id or "").split(".")
    while len(parts) < 4:
        parts.append("")
    return {"network": parts[0], "station": parts[1], "location": parts[2], "channel": parts[3]}


def rms(values: Sequence[float]) -> float:
    if not values:
        return float("nan")
    return math.sqrt(sum(value * value for value in values) / len(values))


def window_rms(data: Any, start: int, stop: int) -> float:
    import numpy as np

    start = max(0, start)
    stop = min(len(data), stop)
    if stop <= start:
        return float("nan")
    values = np.asarray(data[start:stop], dtype=float)
    return float(np.sqrt(np.mean(values * values)))


def estimate_pick_snr(data: Any, pick_index: int, sample_rate: float, noise_s: float, signal_s: float) -> float:
    noise = window_rms(data, pick_index - int(noise_s * sample_rate), pick_index)
    signal = window_rms(data, pick_index, pick_index + int(signal_s * sample_rate))
    if not math.isfinite(noise) or noise <= 0:
        return float("nan")
    return signal / noise


def estimate_first_motion(
    trace: Any,
    pick_time: Any,
    pre_window_s: float = 0.25,
    post_window_s: float = 0.12,
    min_score: float = 2.0,
    bandpass: bool = False,
) -> tuple[str, str, float]:
    import numpy as np

    tr = trace.copy()
    try:
        tr.detrend("demean")
        tr.detrend("linear")
        tr.taper(max_percentage=0.02)
        if bandpass:
            tr.filter("bandpass", freqmin=1.0, freqmax=min(20.0, 0.45 * tr.stats.sampling_rate), corners=2, zerophase=True)
    except Exception:
        pass

    obspy = import_obspy()
    t0 = obspy.UTCDateTime(str(pick_time))
    sample_rate = float(tr.stats.sampling_rate)
    pick_index = int(round((t0 - tr.stats.starttime) * sample_rate))
    if pick_index < 1 or pick_index >= len(tr.data) - 2:
        return "N", "", 0.0

    n_pre = max(3, int(pre_window_s * sample_rate))
    n_post = max(3, int(post_window_s * sample_rate))
    noise = np.asarray(tr.data[max(0, pick_index - n_pre):pick_index], dtype=float)
    signal = np.asarray(tr.data[pick_index:min(len(tr.data), pick_index + n_post)], dtype=float)
    if len(noise) < 3 or len(signal) < 3:
        return "N", "", 0.0

    noise_level = float(np.std(noise)) + 1e-12
    baseline = float(np.median(noise))
    early = signal[: max(3, min(len(signal), int(0.04 * sample_rate)))]
    x = np.arange(len(early), dtype=float)
    slope = float(np.polyfit(x, early - baseline, 1)[0]) if len(early) > 2 else float(np.mean(early - baseline))
    score = abs(float(np.mean(early) - baseline)) / noise_level
    if score < min_score:
        return "N", "", score
    polarity = "U" if slope >= 0 else "D"
    quality = "I" if score >= 5.0 else "E"
    return polarity, quality, score


def cmd_init_config(args: argparse.Namespace) -> int:
    template = """\
    # SeismicX Catalog configuration template
    waveforms: ./waveforms
    stations: ./stations.csv
    velocity_model: ./velocity_model.csv
    output_dir: ./catalog_out

    picking:
      picker: torchscript-pnsn
      phases: [Pg, Sg, Pn, Sn]
      device: cpu
      model: pnsn-v3
      filter_continuous_waveforms: false

    association:
      method: gamma
      min_picks_per_eq: 5
      min_p_picks_per_eq: 3
      min_s_picks_per_eq: 2

    location:
      method: grid
      initial_depth_km: 8.0
      max_depth_km: 30.0

    magnitude:
      type: ML
      method: seedtools-dd1
      region: R13
      amplitude_unit: micrometer
      response_source: StationXML_or_RESP_required_for_calibrated_ML
      seedtools_window: true
      raw_to_mm: 1.0

    focal_mechanism:
      first_motion: true
      hash_command: null
    """
    ensure_parent(args.output)
    Path(args.output).write_text(textwrap.dedent(template), encoding="utf-8")
    print(args.output)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in iter_waveform_files(args.waveforms, args.extensions):
        try:
            stream = read_waveform(path, headonly=True)
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        for trace in stream:
            stats = trace.stats
            rows.append(
                {
                    "path": str(path),
                    "format": path.suffix.lower().lstrip("."),
                    "trace_id": stream_trace_id(trace),
                    "network": getattr(stats, "network", ""),
                    "station": getattr(stats, "station", ""),
                    "location": getattr(stats, "location", ""),
                    "channel": getattr(stats, "channel", ""),
                    "start_time": datetime_to_text(getattr(stats, "starttime", "")),
                    "end_time": datetime_to_text(getattr(stats, "endtime", "")),
                    "sampling_rate": getattr(stats, "sampling_rate", ""),
                    "npts": getattr(stats, "npts", ""),
                }
            )
    write_csv_rows(args.output, rows)
    if args.errors:
        write_csv_rows(args.errors, errors, ["path", "error"])
    eprint(f"scanned={len(rows)} rejected={len(errors)}")
    return 0


def classic_pick_file(path: Path, requested: set[str], args: argparse.Namespace) -> list[dict[str, Any]]:
    import numpy as np
    from obspy.signal.trigger import classic_sta_lta, trigger_onset

    picks: list[dict[str, Any]] = []
    stream = read_waveform(path)
    for trace in stream:
        stats = trace.stats
        channel = getattr(stats, "channel", "")
        component = channel[-1:].upper()
        base_phase = "P" if component == "Z" else "S"
        label = choose_phase_label(base_phase, requested)
        if label is None:
            continue

        tr = trace.copy()
        sample_rate = float(tr.stats.sampling_rate)
        nsta = max(1, int(args.sta * sample_rate))
        nlta = max(nsta + 1, int(args.lta * sample_rate))
        if len(tr.data) <= nlta:
            continue
        try:
            tr.detrend("demean")
            tr.detrend("linear")
            tr.taper(max_percentage=0.02)
            if args.classic_bandpass:
                tr.filter(
                    "bandpass",
                    freqmin=args.freqmin,
                    freqmax=min(args.freqmax, 0.45 * sample_rate),
                    corners=2,
                    zerophase=True,
                )
        except Exception as exc:
            eprint(f"preprocess failed for {path} {stream_trace_id(trace)}: {exc}")
            continue

        data = np.asarray(tr.data, dtype=float)
        cft = classic_sta_lta(data, nsta, nlta)
        triggers = trigger_onset(cft, args.trigger_on, args.trigger_off)
        for onset, offset in triggers[: args.max_picks_per_trace]:
            pick_time = tr.stats.starttime + onset / sample_rate
            score = float(np.nanmax(cft[onset:max(onset + 1, offset)]))
            snr = estimate_pick_snr(data, onset, sample_rate, args.noise_window, args.signal_window)
            amp_stop = min(len(data), onset + int(args.signal_window * sample_rate))
            amplitude = float(np.nanmax(np.abs(data[onset:amp_stop]))) if amp_stop > onset else float("nan")
            polarity, polarity_quality, polarity_score = ("N", "", 0.0)
            if phase_group(label) == "P" and component == "Z":
                polarity, polarity_quality, polarity_score = estimate_first_motion(trace, pick_time, min_score=args.polarity_min_score)
            net = getattr(stats, "network", "")
            sta = getattr(stats, "station", "")
            loc = getattr(stats, "location", "")
            cha = getattr(stats, "channel", "")
            picks.append(
                {
                    "pick_id": f"{len(picks) + 1}",
                    "event_id": "",
                    "waveform_path": str(path),
                    "trace_id": stream_trace_id(trace),
                    "network": net,
                    "station": sta,
                    "location": loc,
                    "channel": cha,
                    "phase": label,
                    "time": datetime_to_text(pick_time.datetime),
                    "score": f"{score:.6g}",
                    "snr": f"{snr:.6g}" if math.isfinite(snr) else "",
                    "amplitude": f"{amplitude:.6g}" if math.isfinite(amplitude) else "",
                    "polarity": polarity,
                    "polarity_quality": polarity_quality,
                    "polarity_score": f"{polarity_score:.6g}",
                    "picker": "classic-sta-lta",
                }
            )
    return picks


def component_family(channel: str) -> str | None:
    component = (channel or "")[-1:].upper()
    if component in {"E", "1"}:
        return "E"
    if component in {"N", "2"}:
        return "N"
    if component == "Z":
        return "Z"
    return None


def station_day_key(trace: Any) -> tuple[str, str, str, str]:
    stats = trace.stats
    start = getattr(stats, "starttime", None)
    day = f"{start.year:04d}.{start.julday:03d}" if start else "unknown"
    return getattr(stats, "network", ""), getattr(stats, "station", ""), getattr(stats, "location", ""), day


def matches_station_day(trace: Any, key: tuple[str, str, str, str]) -> bool:
    return station_day_key(trace) == key and component_family(getattr(trace.stats, "channel", "")) is not None


def select_three_components(stream: Any) -> list[tuple[str, Any]]:
    selected: dict[str, Any] = {}
    for trace in stream:
        family = component_family(getattr(trace.stats, "channel", ""))
        if family and family not in selected:
            selected[family] = trace
    if not all(key in selected for key in ("E", "N", "Z")):
        return []
    return [("E", selected["E"]), ("N", selected["N"]), ("Z", selected["Z"])]


def torchscript_pnsn_pick_files(paths: Sequence[Path], requested: set[str], args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    import numpy as np
    import torch
    from obspy import Stream

    model_path, metadata = resolve_model_path(args.model, "pnsn-v3")
    if not model_path.exists():
        raise CatalogError(f"Bundled model file is missing: {model_path}")

    device = torch.device(args.device or "cpu")
    session = torch.jit.load(str(model_path), map_location=device)
    session.eval()
    session.to(device)

    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    for path in paths:
        try:
            stream = read_waveform(path, headonly=True)
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        for trace in stream:
            family = component_family(getattr(trace.stats, "channel", ""))
            if family is None:
                continue
            key = station_day_key(trace)
            entry = grouped.setdefault(key, {"paths": set(), "families": set()})
            entry["paths"].add(str(path))
            entry["families"].add(family)

    all_picks: list[dict[str, Any]] = []
    samplerate = float(metadata.get("sampling_rate_hz", 100) if metadata else 100)
    requested_groups = {phase_group(item) for item in requested}
    for key, entry in grouped.items():
        network, station, location, _ = key
        try:
            if not all(family in entry["families"] for family in ("E", "N", "Z")):
                errors.append({"path": join_waveform_paths(entry["paths"]), "error": "missing E/N/Z components"})
                continue
            stream = Stream()
            waveform_path = join_waveform_paths(entry["paths"])
            for source in sorted(entry["paths"]):
                try:
                    source_stream = read_waveform(source)
                except Exception as exc:
                    errors.append({"path": source, "error": str(exc)})
                    continue
                for trace in source_stream:
                    if matches_station_day(trace, key):
                        stream.append(trace)
            if not stream:
                errors.append({"path": waveform_path, "error": "no readable matching traces"})
                continue
            stream.merge(fill_value=0)
            stream.resample(samplerate)
            components = select_three_components(stream)
            if not components:
                errors.append({"path": waveform_path, "error": "missing E/N/Z components"})
                continue
            start = min(trace.stats.starttime for _, trace in components)
            end = max(trace.stats.endtime for _, trace in components)
            arrays = []
            component_traces = {}
            for family, trace in components:
                tr = trace.copy()
                tr.trim(starttime=start, endtime=end, pad=True, nearest_sample=True, fill_value=0)
                tr.detrend("demean")
                arrays.append(np.asarray(tr.data, dtype=np.float32))
                component_traces[family] = tr
            npts = min(len(array) for array in arrays)
            if npts == 0:
                continue
            data = np.stack([array[:npts] for array in arrays], axis=1).astype(np.float32)
            with torch.no_grad():
                output = session(torch.tensor(data, dtype=torch.float32, device=device)).detach().cpu().numpy()
            if output.size == 0:
                continue
            if output.ndim == 1:
                output = output.reshape(1, -1)
            z_trace = component_traces["Z"]
            z_data = np.asarray(z_trace.data[:npts], dtype=float)
            for row in output:
                if len(row) < 3:
                    continue
                phase_index = int(row[0])
                phase = PNSN_PHASE_MAP.get(phase_index, str(phase_index))
                if phase.upper() in requested:
                    label = phase
                elif phase_group(phase) in requested_groups:
                    label = choose_phase_label(phase, requested)
                else:
                    label = None
                if label is None:
                    continue
                sample_index = int(round(float(row[1])))
                if sample_index < 0 or sample_index >= npts:
                    continue
                pick_time = start + sample_index / samplerate
                snr = estimate_pick_snr(z_data, sample_index, samplerate, args.noise_window, args.signal_window)
                amp_stop = min(len(z_data), sample_index + int(args.signal_window * samplerate))
                amplitude = float(np.nanmax(np.abs(z_data[sample_index:amp_stop]))) if amp_stop > sample_index else float("nan")
                polarity, polarity_quality, polarity_score = ("N", "", 0.0)
                if phase_group(label) == "P":
                    polarity, polarity_quality, polarity_score = estimate_first_motion(z_trace, pick_time, min_score=args.polarity_min_score)
                all_picks.append(
                    {
                        "pick_id": f"p{len(all_picks) + 1:08d}",
                        "event_id": "",
                        "waveform_path": waveform_path,
                        "trace_id": station_id(network, station, location) + ".3C",
                        "network": network,
                        "station": station,
                        "location": location,
                        "channel": "3C",
                        "phase": label,
                        "time": datetime_to_text(pick_time.datetime),
                        "score": f"{float(row[2]):.6g}",
                        "snr": f"{snr:.6g}" if math.isfinite(snr) else "",
                        "amplitude": f"{amplitude:.6g}" if math.isfinite(amplitude) else "",
                        "polarity": polarity,
                        "polarity_quality": polarity_quality,
                        "polarity_score": f"{polarity_score:.6g}",
                        "picker": f"torchscript-pnsn:{model_path.name}",
                    }
                )
        except Exception as exc:
            errors.append({"path": join_waveform_paths(entry["paths"]), "error": str(exc)})
    return all_picks, errors


def cmd_list_models(args: argparse.Namespace) -> int:
    registry = read_model_registry()
    rows = []
    for model_id, metadata in sorted(registry.items()):
        path = MODEL_REGISTRY_PATH.parent / metadata["file"]
        rows.append(
            {
                "id": model_id,
                "task": metadata.get("task", ""),
                "type": metadata.get("type", ""),
                "file": str(path),
                "size_mb": f"{path.stat().st_size / 1024 / 1024:.2f}" if path.exists() else "missing",
                "phases": ",".join(metadata.get("phases", [])),
                "recommended": metadata.get("recommended", False),
            }
        )
    if args.json:
        print(json.dumps({"models": rows}, indent=2))
    else:
        if rows:
            writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    return 0


def cmd_pick(args: argparse.Namespace) -> int:
    requested = {phase.strip().upper() for phase in args.phases.split(",") if phase.strip()}
    if not requested:
        raise CatalogError("At least one phase must be selected with --phases")
    all_picks: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    files = list(iter_waveform_files(args.waveforms, args.extensions))
    if args.picker == "torchscript-pnsn":
        all_picks, errors = torchscript_pnsn_pick_files(files, requested, args)
        write_csv_rows(args.output, all_picks, PICK_FIELDS)
        if args.errors:
            write_csv_rows(args.errors, errors, ["path", "error"])
        eprint(f"picks={len(all_picks)} rejected_groups={len(errors)}")
        return 0

    for file_index, path in enumerate(files, start=1):
        try:
            if args.picker == "classic":
                picks = classic_pick_file(path, requested, args)
            else:
                raise CatalogError(f"Unsupported picker: {args.picker}")
            for pick in picks:
                pick["pick_id"] = f"p{len(all_picks) + 1:08d}"
            all_picks.extend(picks)
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
        if file_index % max(1, args.progress_every) == 0:
            eprint(f"picked files={file_index}/{len(files)} picks={len(all_picks)}")
    write_csv_rows(args.output, all_picks, PICK_FIELDS)
    if args.errors:
        write_csv_rows(args.errors, errors, ["path", "error"])
    eprint(f"picks={len(all_picks)} rejected_files={len(errors)}")
    return 0


def select_trace_for_pick(stream: Any, row: dict[str, str], prefer_vertical: bool = False) -> Any | None:
    net = row.get("network", "")
    sta = row.get("station", "")
    loc = row.get("location", "")
    cha = row.get("channel", "")
    candidates = stream.select(network=net or "*", station=sta or "*", location=loc or "*")
    if cha:
        exact = candidates.select(channel=cha)
        if exact:
            return exact[0]
    if prefer_vertical:
        vertical = [tr for tr in candidates if getattr(tr.stats, "channel", "")[-1:].upper() == "Z"]
        if vertical:
            return vertical[0]
    return candidates[0] if candidates else None


def cmd_polarity(args: argparse.Namespace) -> int:
    rows = read_csv_rows(args.picks)
    cache: dict[str, Any] = {}
    updated = 0
    for row in rows:
        if phase_group(row.get("phase", "")) != "P":
            continue
        path = row.get("waveform_path", "")
        if not path:
            continue
        try:
            if path not in cache:
                cache[path] = read_waveform_field(path)
            trace = select_trace_for_pick(cache[path], row, prefer_vertical=True)
            if trace is None:
                continue
            polarity, quality, score = estimate_first_motion(
                trace,
                row["time"],
                pre_window_s=args.pre_window,
                post_window_s=args.post_window,
                min_score=args.min_score,
                bandpass=args.bandpass,
            )
            row["polarity"] = polarity
            row["polarity_quality"] = quality
            row["polarity_score"] = f"{score:.6g}"
            updated += 1
        except Exception as exc:
            row["polarity"] = row.get("polarity") or "N"
            row["polarity_score"] = row.get("polarity_score") or "0"
            eprint(f"polarity failed for pick {row.get('pick_id')}: {exc}")
    write_csv_rows(args.output, rows, PICK_FIELDS)
    eprint(f"polarity_updated={updated}")
    return 0


def cmd_associate_simple(args: argparse.Namespace) -> int:
    rows = sorted(read_csv_rows(args.picks), key=lambda row: to_epoch_seconds(row["time"]))
    events: list[dict[str, Any]] = []
    assignments: list[dict[str, Any]] = []
    current: list[dict[str, str]] = []
    event_index = 0

    def flush(group: list[dict[str, str]]) -> None:
        nonlocal event_index
        if len(group) < args.min_picks_per_eq:
            return
        event_index += 1
        event_id = f"ev{event_index:06d}"
        times = [to_epoch_seconds(row["time"]) for row in group]
        origin = datetime.fromtimestamp(min(times), tz=timezone.utc)
        for row in group:
            row["event_id"] = event_id
            assignments.append({"event_id": event_id, "pick_id": row.get("pick_id", ""), "time": row["time"], "phase": row["phase"]})
        events.append(
            {
                "event_id": event_id,
                "origin_time": datetime_to_text(origin),
                "latitude": "",
                "longitude": "",
                "depth_km": "",
                "magnitude": "",
                "magnitude_type": "",
                "rms": "",
                "n_picks": len(group),
                "method": "simple-time-window",
            }
        )

    previous_time: float | None = None
    for row in rows:
        current_time = to_epoch_seconds(row["time"])
        if previous_time is not None and current_time - previous_time > args.time_gap:
            flush(current)
            current = []
        current.append(row)
        previous_time = current_time
    flush(current)

    write_csv_rows(args.output, events, EVENT_FIELDS)
    if args.assignments:
        write_csv_rows(args.assignments, assignments)
    if args.associated_picks:
        write_csv_rows(args.associated_picks, rows, PICK_FIELDS)
    return 0


def gamma_event_index(value: Any, fallback: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def format_optional_float(value: Any, precision: int = 3) -> str:
    number = parse_float(value)
    if not math.isfinite(number):
        return ""
    return f"{number:.{precision}f}"


def normalize_gamma_assignment(record: Any) -> tuple[int, int, float] | None:
    if isinstance(record, dict):
        pick_value = record.get("pick_index", record.get("pick_idx", record.get(0)))
        event_value = record.get("event_index", record.get("event_idx", record.get("event_id", record.get(1))))
        score_value = record.get("gamma_score", record.get("probability", record.get("score", record.get(2, ""))))
    elif isinstance(record, (list, tuple)) and len(record) >= 2:
        pick_value = record[0]
        event_value = record[1]
        score_value = record[2] if len(record) > 2 else ""
    else:
        return None
    try:
        pick_index = int(float(pick_value))
        event_index = int(float(event_value))
    except (TypeError, ValueError):
        return None
    score = parse_float(score_value, float("nan"))
    return pick_index, event_index, score


def gamma_xy_bounds(
    station_records: Sequence[dict[str, Any]],
    lon0: float,
    lat0: float,
    args: argparse.Namespace,
) -> tuple[tuple[float, float], tuple[float, float]]:
    xs = [parse_float(row.get("x(km)")) for row in station_records]
    ys = [parse_float(row.get("y(km)")) for row in station_records]
    xs = [value for value in xs if math.isfinite(value)]
    ys = [value for value in ys if math.isfinite(value)]
    if not xs or not ys:
        raise CatalogError("No valid station coordinates available for GaMMA association.")

    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if args.min_longitude is not None and args.max_longitude is not None:
        x_a, _ = project_lonlat(args.min_longitude, lat0, lon0, lat0)
        x_b, _ = project_lonlat(args.max_longitude, lat0, lon0, lat0)
        x_min, x_max = min(x_a, x_b), max(x_a, x_b)
    if args.min_latitude is not None and args.max_latitude is not None:
        _, y_a = project_lonlat(lon0, args.min_latitude, lon0, lat0)
        _, y_b = project_lonlat(lon0, args.max_latitude, lon0, lat0)
        y_min, y_max = min(y_a, y_b), max(y_a, y_b)

    x_pad = max(10.0, 0.2 * max(x_max - x_min, 1.0))
    y_pad = max(10.0, 0.2 * max(y_max - y_min, 1.0))
    return (x_min - x_pad, x_max + x_pad), (y_min - y_pad, y_max + y_pad)


def cmd_associate_gamma(args: argparse.Namespace) -> int:
    try:
        import pandas as pd
        from gamma.utils import association
    except ImportError as exc:
        raise CatalogError("GaMMA association requested but gamma and pandas are not installed.") from exc

    picks = read_csv_rows(args.picks)
    stations = read_stations(args.stations)
    canonical_stations = canonical_station_values(stations)
    lon0 = median([sta.longitude for sta in canonical_stations])
    lat0 = median([sta.latitude for sta in canonical_stations])

    station_records = []
    for sta in canonical_stations:
        x_km, y_km = project_lonlat(sta.longitude, sta.latitude, lon0, lat0)
        station_records.append(
            {
                "id": sta.station_id,
                "station_id": sta.station_id,
                "longitude": sta.longitude,
                "latitude": sta.latitude,
                "elevation_m": sta.elevation_m,
                "x(km)": x_km,
                "y(km)": y_km,
                "z(km)": -sta.elevation_m / 1000.0,
            }
        )

    pick_records = []
    original_indices: list[int] = []
    pick_times: list[float] = []
    for original_index, row in enumerate(picks):
        sid = station_id(row.get("network", ""), row.get("station", ""), row.get("location", ""))
        if sid not in stations:
            sid = station_id(row.get("network", ""), row.get("station", ""))
        if sid not in stations:
            continue
        pick_time = parse_datetime(row["time"])
        phase = phase_group(row.get("phase", "")).lower()
        if phase not in {"p", "s"}:
            continue
        pick_times.append(pick_time.timestamp())
        pick_records.append(
            {
                "id": sid,
                "station_id": sid,
                "timestamp": pd.Timestamp(pick_time),
                "type": phase,
                "prob": parse_float(row.get("score"), 1.0),
                "amp": max(parse_float(row.get("amplitude"), 1.0), 1e-12),
            }
        )
        original_indices.append(original_index)
    if not pick_records:
        raise CatalogError("No picks matched the station table for GaMMA association.")

    x_bounds, y_bounds = gamma_xy_bounds(station_records, lon0, lat0, args)
    time_span = max(pick_times) - min(pick_times) if pick_times else 0.0
    time_bounds = (-300.0, max(300.0, time_span + 300.0))

    z_bounds = (args.min_depth, args.max_depth)
    config = {
        "center": (lon0, lat0),
        "dims": ["x(km)", "y(km)", "z(km)"],
        "x(km)": x_bounds,
        "y(km)": y_bounds,
        "z(km)": z_bounds,
        "bfgs_bounds": [x_bounds, y_bounds, z_bounds, time_bounds],
        "vel": {"p": args.vp, "s": args.vs},
        "use_amplitude": args.use_amplitude,
        "use_dbscan": args.use_dbscan,
        "dbscan_eps": args.dbscan_eps,
        "dbscan_min_samples": args.dbscan_min_samples,
        "min_picks_per_eq": args.min_picks_per_eq,
        "min_p_picks_per_eq": args.min_p_picks_per_eq,
        "min_s_picks_per_eq": args.min_s_picks_per_eq,
        "max_sigma11": args.max_sigma11,
        "max_sigma22": args.max_sigma22,
        "max_sigma12": args.max_sigma12,
        "oversample_factor": max(1, int(round(args.oversampling_factor))),
    }
    config = {key: value for key, value in config.items() if value is not None}

    catalogs, assignments = association(
        pd.DataFrame(pick_records, index=original_indices),
        pd.DataFrame(station_records),
        config,
        method=args.gamma_method,
    )

    raw_events = catalogs.to_dict("records") if hasattr(catalogs, "to_dict") else list(catalogs)
    raw_events = [dict(row) for row in raw_events]
    raw_events.sort(key=lambda row: (str(row.get("time", "")), gamma_event_index(row.get("event_index"), 0)))
    event_id_by_gamma_index: dict[int, str] = {}
    event_rows: list[dict[str, Any]] = []
    for event_number, event in enumerate(raw_events, start=1):
        raw_event_index = gamma_event_index(event.get("event_index"), event_number)
        event_id = f"ev{event_number:06d}"
        event_id_by_gamma_index[raw_event_index] = event_id
        x_km = parse_float(event.get("x(km)"))
        y_km = parse_float(event.get("y(km)"))
        lon, lat = unproject_lonlat(x_km, y_km, lon0, lat0) if math.isfinite(x_km) and math.isfinite(y_km) else (float("nan"), float("nan"))
        origin_time = datetime_to_text(parse_datetime(event["time"])) if event.get("time") else ""
        event_rows.append(
            {
                "event_id": event_id,
                "origin_time": origin_time,
                "latitude": format_optional_float(lat, 6),
                "longitude": format_optional_float(lon, 6),
                "depth_km": format_optional_float(event.get("z(km)"), 3),
                "magnitude": "",
                "magnitude_type": "",
                "rms": format_optional_float(event.get("sigma_time"), 3),
                "n_picks": int(parse_float(event.get("num_picks"), 0)),
                "method": f"gamma-{args.gamma_method}",
            }
        )
    write_csv_rows(args.output, event_rows, EVENT_FIELDS)

    normalized_assignments = [item for item in (normalize_gamma_assignment(record) for record in assignments) if item is not None]
    associated = [dict(row) for row in picks]
    assignment_rows: list[dict[str, Any]] = []
    for pick_index, raw_event_index, score in normalized_assignments:
        event_id = event_id_by_gamma_index.get(raw_event_index)
        if not event_id or not (0 <= pick_index < len(associated)):
            continue
        associated[pick_index]["event_id"] = event_id
        assignment_rows.append(
            {
                "event_id": event_id,
                "pick_id": associated[pick_index].get("pick_id", ""),
                "pick_index": pick_index,
                "gamma_event_index": raw_event_index,
                "gamma_score": f"{score:.6g}" if math.isfinite(score) else "",
                "time": associated[pick_index].get("time", ""),
                "phase": associated[pick_index].get("phase", ""),
            }
        )
    if args.assignments:
        write_csv_rows(args.assignments, assignment_rows, ["event_id", "pick_id", "pick_index", "gamma_event_index", "gamma_score", "time", "phase"])
    if args.associated_picks:
        write_csv_rows(args.associated_picks, associated, PICK_FIELDS)
    eprint(f"gamma_events={len(event_rows)} gamma_assignments={len(assignment_rows)}")
    return 0


def prepare_real_workspace(args: argparse.Namespace) -> dict[str, str]:
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    picks = read_csv_rows(args.picks)
    stations = read_stations(args.stations)
    station_path = workdir / "stations.csv"
    pick_path = workdir / "picks.csv"
    write_csv_rows(station_path, [asdict(sta) for sta in canonical_station_values(stations)], STATION_FIELDS)
    write_csv_rows(pick_path, picks, PICK_FIELDS)
    return {
        "workdir": str(workdir),
        "stations": str(station_path.resolve()),
        "picks": str(pick_path.resolve()),
        "output": str(Path(args.output).resolve()),
    }


def cmd_associate_real(args: argparse.Namespace) -> int:
    if args.real_command:
        placeholders = prepare_real_workspace(args)
        placeholders.update({"R": args.real_R, "G": args.real_G, "V": args.real_V, "S": args.real_S})
        command = args.real_command.format(**placeholders)
        subprocess.run(command, shell=True, cwd=args.workdir, check=True)
        return 0

    try:
        from seismicx_real import RealConfig, RealError, RealPick, RealStation, associate_real
    except ImportError:
        try:
            from scripts.seismicx_real import RealConfig, RealError, RealPick, RealStation, associate_real
        except ImportError as exc:
            raise CatalogError("The bundled Python REAL backend requires NumPy.") from exc

    picks = read_csv_rows(args.picks)
    parsed_stations = read_stations(args.stations)
    canonical_stations = canonical_station_values(parsed_stations)
    if not canonical_stations:
        raise CatalogError("No stations are available for REAL association.")

    real_stations = [
        RealStation(
            network=station.network,
            station=station.station,
            location=station.location,
            longitude=station.longitude,
            latitude=station.latitude,
            elevation_km=station.elevation_m / 1000.0,
        )
        for station in canonical_stations
    ]
    exact_station_indices = {
        station_id(station.network, station.station, station.location): index
        for index, station in enumerate(canonical_stations)
    }
    network_station_indices: dict[str, list[int]] = defaultdict(list)
    for index, station in enumerate(canonical_stations):
        network_station_indices[station_id(station.network, station.station)].append(index)

    real_picks: list[RealPick] = []
    skipped_station = 0
    skipped_phase = 0
    for index, row in enumerate(picks):
        exact_id = station_id(row.get("network", ""), row.get("station", ""), row.get("location", ""))
        station_index = exact_station_indices.get(exact_id)
        if station_index is None:
            candidates = network_station_indices.get(station_id(row.get("network", ""), row.get("station", "")), [])
            if len(candidates) == 1:
                station_index = candidates[0]
        if station_index is None:
            skipped_station += 1
            continue
        phase = phase_group(row.get("phase", ""))
        if phase not in {"P", "S"}:
            skipped_phase += 1
            continue
        real_picks.append(
            RealPick(
                original_index=index,
                pick_id=row.get("pick_id") or f"p{index + 1:08d}",
                station_index=station_index,
                phase=phase,
                time=to_epoch_seconds(row["time"]),
                score=parse_float(row.get("score"), 1.0),
                amplitude=parse_float(row.get("amplitude"), 0.0),
            )
        )
    if not real_picks:
        raise CatalogError("No P/S picks matched the station table for REAL association.")

    active_station_indices = sorted({pick.station_index for pick in real_picks})
    station_remap = {old_index: new_index for new_index, old_index in enumerate(active_station_indices)}
    real_stations = [real_stations[index] for index in active_station_indices]
    real_picks = [
        RealPick(
            original_index=pick.original_index,
            pick_id=pick.pick_id,
            station_index=station_remap[pick.station_index],
            phase=pick.phase,
            time=pick.time,
            score=pick.score,
            amplitude=pick.amplitude,
        )
        for pick in real_picks
    ]

    try:
        config = RealConfig.from_real_strings(
            args.real_R,
            args.real_S,
            args.real_V,
            min_score=args.real_min_score,
            jobs=args.real_jobs,
        )
        result = associate_real(real_stations, real_picks, config)
    except RealError as exc:
        raise CatalogError(str(exc)) from exc

    associated = [dict(row) for row in picks]
    event_rows: list[dict[str, Any]] = []
    assignment_rows: list[dict[str, Any]] = []
    for event_number, event in enumerate(result.events, start=1):
        event_id = f"ev{event_number:06d}"
        event_rows.append(
            {
                "event_id": event_id,
                "origin_time": datetime_to_text(datetime.fromtimestamp(event.origin_time, tz=timezone.utc)),
                "latitude": f"{event.latitude:.6f}",
                "longitude": f"{event.longitude:.6f}",
                "depth_km": f"{event.depth_km:.3f}",
                "magnitude": "",
                "magnitude_type": "",
                "rms": f"{event.rms_s:.4f}",
                "n_picks": event.n_picks,
                "method": "REAL-python-homogeneous",
                "n_p_picks": event.n_p,
                "n_s_picks": event.n_s,
                "n_both_stations": event.n_both,
                "azimuth_gap_deg": f"{event.azimuth_gap_deg:.2f}",
            }
        )
        for assignment in sorted(event.assignments, key=lambda item: (item.time, item.original_index)):
            associated[assignment.original_index]["event_id"] = event_id
            station = real_stations[assignment.station_index]
            assignment_rows.append(
                {
                    "event_id": event_id,
                    "pick_id": assignment.pick_id,
                    "pick_index": assignment.original_index,
                    "station_id": station.station_id,
                    "time": associated[assignment.original_index].get("time", ""),
                    "phase": associated[assignment.original_index].get("phase", assignment.phase),
                    "residual_s": f"{assignment.residual_s:.4f}",
                    "score": f"{assignment.score:.6g}",
                    "azimuth_deg": f"{assignment.azimuth_deg:.2f}",
                }
            )

    write_csv_rows(args.output, event_rows, REAL_EVENT_FIELDS)
    if args.assignments:
        write_csv_rows(
            args.assignments,
            assignment_rows,
            ["event_id", "pick_id", "pick_index", "station_id", "time", "phase", "residual_s", "score", "azimuth_deg"],
        )
    if args.associated_picks:
        write_csv_rows(args.associated_picks, associated, PICK_FIELDS)
    ensure_parent(Path(args.workdir) / "real_run.json")
    (Path(args.workdir) / "real_run.json").write_text(
        json.dumps(
            {
                "R": args.real_R,
                "S": args.real_S,
                "V": args.real_V,
                "input_rows": len(picks),
                "active_stations": len(real_stations),
                "skipped_station": skipped_station,
                "skipped_phase": skipped_phase,
                **asdict(result.stats),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    eprint(
        f"real_events={len(event_rows)} real_assignments={len(assignment_rows)} "
        f"usable_picks={result.stats.usable_picks} skipped_station={skipped_station} "
        f"skipped_phase={skipped_phase} elapsed_s={result.stats.elapsed_s:.3f} "
        f"numba={result.stats.numba_enabled}"
    )
    return 0


def cmd_associate(args: argparse.Namespace) -> int:
    if args.method == "gamma":
        return cmd_associate_gamma(args)
    if args.method == "real":
        return cmd_associate_real(args)
    if args.method == "simple":
        return cmd_associate_simple(args)
    raise CatalogError(f"Unsupported association method: {args.method}")


def run_logged(command: Sequence[str], cwd: str | Path | None = None) -> None:
    printable = " ".join(shlex.quote(str(part)) for part in command)
    eprint(f"+ {printable}")
    subprocess.run(list(command), cwd=cwd, check=True)


def clone_or_update(url: str, destination: Path) -> None:
    if (destination / ".git").exists():
        run_logged(["git", "-C", str(destination), "pull", "--ff-only"])
        return
    if destination.exists() and any(destination.iterdir()):
        raise CatalogError(f"Destination exists and is not an empty git repository: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_logged(["git", "clone", "--depth", "1", url, str(destination)])


def clone_without_ds_store(url: str, destination: Path) -> None:
    if (destination / ".git").exists():
        run_logged(["git", "-C", str(destination), "pull", "--ff-only"])
        return
    if destination.exists() and any(destination.iterdir()):
        raise CatalogError(f"Destination exists and is not an empty git repository: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_logged(["git", "clone", "--depth", "1", "--no-checkout", url, str(destination)])
    run_logged(["git", "-C", str(destination), "sparse-checkout", "init", "--no-cone"])
    run_logged(["git", "-C", str(destination), "sparse-checkout", "set", "/*", "!**/.DS_Store"])
    run_logged(["git", "-C", str(destination), "checkout"])


def make_directories(root: Path) -> list[Path]:
    candidates = [path.parent for path in root.rglob("Makefile") if ".git" not in path.parts]
    candidates.sort(key=lambda value: (len(value.parts), str(value)))
    return candidates


def require_compiler() -> None:
    if shutil.which("gfortran") is None:
        raise CatalogError("gfortran is required for REAL/HASH builds. Install GCC/gfortran first.")


def build_first_makefile(root: Path, jobs: int, preferred: Sequence[str] = ()) -> Path:
    require_compiler()
    candidates = make_directories(root)
    ordered: list[Path] = []
    for name in preferred:
        path = root / name
        if (path / "Makefile").exists():
            ordered.append(path)
    ordered.extend(candidate for candidate in candidates if candidate not in ordered)
    if not ordered:
        raise CatalogError(f"No Makefile found under {root}")
    errors: list[str] = []
    for directory in ordered:
        try:
            run_logged(["make", f"-j{jobs}"], cwd=directory)
            return directory
        except subprocess.CalledProcessError as exc:
            errors.append(f"{directory}: exit {exc.returncode}")
    raise CatalogError("All make builds failed:\n" + "\n".join(errors))


def cmd_build_tools(args: argparse.Namespace) -> int:
    tools = ["pnsn", "real", "bayes-location", "seismological-ai-tools"] if args.tool == "all" else [args.tool]
    root = Path(args.tools_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {}

    for tool_name in ("pnsn", "bayes-location", "seismological-ai-tools"):
        if tool_name in tools:
            repo_url, directory_name = DOWNLOADABLE_TOOL_REPOS[tool_name]
            destination = root / directory_name
            clone_without_ds_store(repo_url, destination)
            manifest[tool_name] = {"path": str(destination), "url": repo_url, "status": "cloned"}

    if "real" in tools:
        destination = root / "REAL"
        clone_or_update("https://github.com/Dal-mzhang/REAL.git", destination)
        if args.skip_build:
            manifest["real"] = {"path": str(destination), "url": "https://github.com/Dal-mzhang/REAL.git", "status": "cloned"}
        else:
            built_dir = build_first_makefile(destination, args.jobs, preferred=("src",))
            executables = [str(path) for path in destination.rglob("REAL") if path.is_file()]
            manifest["real"] = {"path": str(destination), "url": "https://github.com/Dal-mzhang/REAL.git", "build_dir": str(built_dir), "executables": executables}

    if "hash" in tools:
        source = Path(args.hash_source).resolve() if args.hash_source else Path("pyhash").resolve()
        if not source.exists():
            raise CatalogError("HASH source not found. Pass --hash-source /path/to/pyhash or place pyhash in the current directory.")
        if args.skip_build:
            manifest["hash"] = {"path": str(source), "status": "source-found"}
        else:
            built_dir = build_first_makefile(source, args.jobs, preferred=("src", "hash_fortran"))
            extensions = [str(path) for path in source.rglob("pyhash*.so")]
            drivers = [str(path) for path in (source / "hash_fortran").glob("hash_driver*") if path.is_file() and os.access(path, os.X_OK)]
            manifest["hash"] = {"path": str(source), "build_dir": str(built_dir), "extensions": extensions, "drivers": drivers}

    if args.output:
        ensure_parent(args.output)
        Path(args.output).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    else:
        print(json.dumps(manifest, indent=2))
    return 0


def travel_time_seconds(
    event_lat: float,
    event_lon: float,
    depth_km: float,
    sta: Station,
    phase: str,
    velocity_model: Sequence[VelocityLayer],
) -> float:
    horizontal = distance_km(event_lat, event_lon, sta.latitude, sta.longitude)
    path_km = math.sqrt(horizontal * horizontal + depth_km * depth_km)
    velocity = velocity_at_depth(velocity_model, depth_km, phase)
    return path_km / max(velocity, 0.1)


def solve_location_grid(
    picks: list[dict[str, str]],
    stations: dict[str, Station],
    velocity_model: Sequence[VelocityLayer],
    args: argparse.Namespace,
) -> dict[str, Any]:
    observations = []
    for row in picks:
        sid = station_id(row.get("network", ""), row.get("station", ""), row.get("location", ""))
        sta = stations.get(sid) or stations.get(station_id(row.get("network", ""), row.get("station", "")))
        if sta is None:
            continue
        observations.append((to_epoch_seconds(row["time"]), phase_group(row.get("phase", "")), sta))
    if len(observations) < args.min_picks:
        raise CatalogError("Not enough station-matched picks for location")

    lats = [obs[2].latitude for obs in observations]
    lons = [obs[2].longitude for obs in observations]
    lat_min, lat_max = min(lats) - args.pad_degree, max(lats) + args.pad_degree
    lon_min, lon_max = min(lons) - args.pad_degree, max(lons) + args.pad_degree
    depth_min, depth_max = args.min_depth, args.max_depth

    def residual_for(lat: float, lon: float, depth: float) -> tuple[float, float, list[float]]:
        predicted_origin_times = [
            pick_time - travel_time_seconds(lat, lon, depth, sta, phase, velocity_model)
            for pick_time, phase, sta in observations
        ]
        origin = median(predicted_origin_times)
        residuals = [
            pick_time - origin - travel_time_seconds(lat, lon, depth, sta, phase, velocity_model)
            for pick_time, phase, sta in observations
        ]
        return origin, rms(residuals), residuals

    best = None
    for ilat in range(args.grid_lat):
        lat = lat_min + (lat_max - lat_min) * ilat / max(1, args.grid_lat - 1)
        for ilon in range(args.grid_lon):
            lon = lon_min + (lon_max - lon_min) * ilon / max(1, args.grid_lon - 1)
            for idep in range(args.grid_depth):
                depth = depth_min + (depth_max - depth_min) * idep / max(1, args.grid_depth - 1)
                origin, score, _ = residual_for(lat, lon, depth)
                if best is None or score < best["rms"]:
                    best = {"latitude": lat, "longitude": lon, "depth_km": depth, "origin_epoch": origin, "rms": score}

    if best is None:
        raise CatalogError("Location grid search failed")

    try:
        from scipy.optimize import least_squares

        def fun(vector: Sequence[float]) -> list[float]:
            lat, lon, depth, origin = vector
            return [
                pick_time - origin - travel_time_seconds(lat, lon, depth, sta, phase, velocity_model)
                for pick_time, phase, sta in observations
            ]

        result = least_squares(
            fun,
            x0=[best["latitude"], best["longitude"], best["depth_km"], best["origin_epoch"]],
            bounds=(
                [lat_min, lon_min, depth_min, best["origin_epoch"] - args.origin_time_pad],
                [lat_max, lon_max, depth_max, best["origin_epoch"] + args.origin_time_pad],
            ),
        )
        lat, lon, depth, origin = result.x
        residuals = list(fun(result.x))
        best = {"latitude": lat, "longitude": lon, "depth_km": depth, "origin_epoch": origin, "rms": rms(residuals)}
    except ImportError:
        pass

    best["n_picks"] = len(observations)
    return best


def cmd_locate_bayes(args: argparse.Namespace) -> int:
    if not args.bayes_command:
        export_path = Path(args.output)
        payload = {
            "events": read_csv_rows(args.events) if args.events else [],
            "picks": read_csv_rows(args.picks),
            "stations": [asdict(sta) for sta in canonical_station_values(read_stations(args.stations))],
            "velocity_model": [asdict(layer) for layer in read_velocity_model(args.velocity_model)],
            "note": "Pass this JSON to a local bayes_location adapter or rerun with --bayes-command.",
        }
        ensure_parent(export_path)
        export_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        eprint(f"bayes_location input exported to {export_path}; no command executed.")
        return 0
    placeholders = {
        "events": args.events or "",
        "picks": args.picks,
        "stations": args.stations,
        "velocity_model": args.velocity_model or "",
        "output": args.output,
        "bayes_repo": args.bayes_repo or "",
    }
    command = args.bayes_command.format(**placeholders)
    subprocess.run(command, shell=True, cwd=args.bayes_repo or None, check=True)
    return 0


def cmd_locate(args: argparse.Namespace) -> int:
    if args.method == "bayes":
        return cmd_locate_bayes(args)
    if args.method != "grid":
        raise CatalogError(f"Unsupported location method: {args.method}")

    stations = read_stations(args.stations)
    velocity_model = read_velocity_model(args.velocity_model)
    picks = read_csv_rows(args.picks)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in picks:
        event_id = row.get("event_id") or row.get("event_index") or row.get("gamma_event_index")
        if event_id:
            grouped[str(event_id)].append(row)
    if not grouped:
        raise CatalogError("Picks must include event_id before location. Run associate with --associated-picks or convert external assignments first.")

    output_rows = []
    for event_id, event_picks in grouped.items():
        try:
            solution = solve_location_grid(event_picks, stations, velocity_model, args)
        except Exception as exc:
            eprint(f"location failed for {event_id}: {exc}")
            continue
        origin = datetime.fromtimestamp(solution["origin_epoch"], tz=timezone.utc)
        output_rows.append(
            {
                "event_id": event_id,
                "origin_time": datetime_to_text(origin),
                "latitude": f"{solution['latitude']:.6f}",
                "longitude": f"{solution['longitude']:.6f}",
                "depth_km": f"{solution['depth_km']:.3f}",
                "magnitude": "",
                "magnitude_type": "",
                "rms": f"{solution['rms']:.3f}",
                "n_picks": solution["n_picks"],
                "method": "grid-least-squares",
            }
        )
    write_csv_rows(args.output, output_rows, EVENT_FIELDS)
    eprint(f"located_events={len(output_rows)}")
    return 0


def load_inventory(path: str | None) -> Any | None:
    if not path:
        return None
    obspy = import_obspy()
    source = Path(path)
    if source.is_dir():
        inventory = None
        patterns = ("RESP*", "*.resp", "*.RESP", "*.xml", "*.XML", "*.dataless", "*.seed", "*.SEED")
        files: list[Path] = []
        for pattern in patterns:
            files.extend(sorted(source.rglob(pattern)))
        for file_path in files:
            try:
                inv = obspy.read_inventory(str(file_path))
            except Exception:
                continue
            inventory = inv if inventory is None else inventory + inv
        if inventory is None:
            raise CatalogError(f"No readable response files found in inventory directory: {path}")
        return inventory
    try:
        return obspy.read_inventory(str(source))
    except Exception:
        text = source.read_text(encoding="utf-8").strip()
        mapping = ast.literal_eval(text)
        if not isinstance(mapping, dict):
            raise
        inventory = None
        for response_path in mapping.values():
            try:
                inv = obspy.read_inventory(str(response_path))
            except Exception:
                continue
            inventory = inv if inventory is None else inventory + inv
        if inventory is None:
            raise CatalogError(f"No readable response paths found in mapping file: {path}")
        return inventory


def station_for_pick(row: dict[str, str], stations: dict[str, Station]) -> Station | None:
    sid = station_id(row.get("network", ""), row.get("station", ""), row.get("location", ""))
    return stations.get(sid) or stations.get(station_id(row.get("network", ""), row.get("station", "")))


def seedtools_width_time(distance_degree: float) -> float:
    if distance_degree <= 2.0:
        return 5.0
    if distance_degree <= 3.0:
        return 7.0
    if distance_degree <= 4.0:
        return 10.0
    if distance_degree <= 5.0:
        return 12.0
    if distance_degree <= 6.0:
        return 15.0
    if distance_degree <= 7.0:
        return 18.0
    if distance_degree <= 8.0:
        return 20.0
    if distance_degree <= 9.0:
        return 22.0
    if distance_degree <= 10.0:
        return 24.0
    return float("inf")


def seedtools_r_curve(which_r: str) -> list[float]:
    curves = {
        "R11": [1.9, 1.9, 2.0, 2.2, 2.3, 2.5, 2.7, 2.9, 2.9, 3.0, 3.1, 3.2, 3.3, 3.3, 3.4, 3.3, 3.4, 3.4, 3.5, 3.5, 3.6, 3.6, 3.7, 3.7, 3.8, 3.8, 3.9, 3.9, 3.9, 3.9, 4.0, 4.1, 4.1, 4.1, 4.2, 4.2, 4.3, 4.2, 4.3, 4.3, 4.4, 4.4, 4.4, 4.5, 4.5, 4.5, 4.5, 4.6, 4.6, 4.6, 4.6, 4.6, 4.6, 4.7, 4.8, 4.8, 4.8, 4.8, 4.8, 4.9, 4.8, 4.9, 4.9, 5.0, 5.0, 5.1, 5.2, 5.2, 5.2, 5.2, 5.3, 5.3],
        "R12": [1.8, 1.8, 1.9, 2.1, 2.2, 2.4, 2.6, 2.8, 2.9, 3.0, 3.1, 3.2, 3.3, 3.3, 3.4, 3.3, 3.4, 3.4, 3.5, 3.5, 3.6, 3.6, 3.7, 3.7, 3.8, 3.7, 3.8, 3.9, 4.0, 4.0, 4.1, 4.1, 4.2, 4.2, 4.2, 4.3, 4.4, 4.4, 4.5, 4.4, 4.5, 4.5, 4.5, 4.6, 4.6, 4.6, 4.6, 4.7, 4.7, 4.7, 4.7, 4.7, 4.7, 4.7, 4.7, 4.8, 4.8, 4.8, 4.8, 4.9, 4.9, 4.9, 4.9, 5.0, 5.0, 5.1, 5.2, 5.2, 5.2, 5.2, 5.3, 5.3],
        "R13": [2.0, 2.0, 2.0, 2.1, 2.2, 2.4, 2.6, 2.7, 2.8, 2.9, 3.0, 3.1, 3.2, 3.2, 3.3, 3.3, 3.4, 3.4, 3.5, 3.5, 3.6, 3.6, 3.7, 3.7, 3.8, 3.8, 3.9, 3.9, 3.9, 3.9, 4.0, 4.0, 4.0, 4.1, 4.2, 4.1, 4.2, 4.3, 4.4, 4.4, 4.5, 4.5, 4.5, 4.5, 4.5, 4.6, 4.6, 4.7, 4.7, 4.8, 4.8, 4.8, 4.8, 4.8, 4.8, 4.9, 4.9, 4.9, 4.9, 4.9, 4.9, 4.9, 4.9, 5.0, 5.0, 5.1, 5.2, 5.2, 5.2, 5.2, 5.3, 5.3],
        "R14": [2.0, 2.0, 2.1, 2.2, 2.3, 2.5, 2.6, 2.8, 2.9, 3.0, 3.1, 3.2, 3.2, 3.2, 3.3, 3.4, 3.5, 3.5, 3.6, 3.6, 3.7, 3.7, 3.8, 3.7, 3.8, 3.8, 3.9, 3.9, 4.0, 4.0, 4.1, 4.1, 4.1, 4.1, 4.2, 4.1, 4.2, 4.2, 4.3, 4.3, 4.4, 4.4, 4.5, 4.5, 4.4, 4.5, 4.5, 4.5, 4.6, 4.7, 4.8, 4.8, 4.8, 4.8, 4.8, 4.9, 4.9, 4.9, 4.9, 4.9, 4.9, 4.9, 4.9, 5.0, 5.0, 5.1, 5.2, 5.2, 5.2, 5.2, 5.3, 5.3],
        "R15": [2.0, 2.0, 2.1, 2.2, 2.3, 2.5, 2.6, 2.8, 2.8, 2.9, 3.0, 3.1, 3.2, 3.2, 3.3, 3.3, 3.4, 3.4, 3.6, 3.6, 3.6, 3.6, 3.7, 3.7, 3.8, 3.8, 3.9, 3.9, 3.9, 4.0, 4.0, 4.0, 4.1, 4.1, 4.2, 4.1, 4.2, 4.3, 4.4, 4.4, 4.4, 4.4, 4.5, 4.5, 4.5, 4.5, 4.5, 4.6, 4.7, 4.7, 4.8, 4.8, 4.8, 4.8, 4.8, 4.9, 4.9, 4.9, 4.9, 4.9, 4.9, 4.9, 4.9, 5.0, 5.0, 5.1, 5.2, 5.2, 5.2, 5.2, 5.3, 5.3],
    }
    key = which_r.upper()
    if key not in curves:
        raise CatalogError(f"Unknown seedtools ML region {which_r}; choose R11, R12, R13, R14, or R15.")
    return curves[key]


def calculate_seedtools_dd1_ml(max_amp_um: float, epi_dist_km: float, which_r: str) -> float:
    import numpy as np

    dists = np.array([0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 70, 75, 85, 90, 100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250, 260, 270, 280, 290, 300, 310, 320, 330, 340, 350, 360, 370, 380, 390, 400, 420, 430, 440, 450, 460, 470, 500, 510, 530, 540, 550, 560, 570, 580, 600, 610, 620, 650, 700, 750, 800, 850, 900, 1000], dtype=float)
    r_values = np.array(seedtools_r_curve(which_r), dtype=float)
    epi_dist_km = float(max(epi_dist_km, 0.0))
    idx = int(np.abs(dists - epi_dist_km).argmin())
    if idx == 0:
        slope = (r_values[1] - r_values[0]) / (dists[1] - dists[0])
    elif idx >= len(dists) - 1:
        slope = (r_values[-1] - r_values[-2]) / (dists[-1] - dists[-2])
    elif epi_dist_km >= dists[idx]:
        slope = (r_values[idx + 1] - r_values[idx]) / (dists[idx + 1] - dists[idx])
    else:
        slope = (r_values[idx] - r_values[idx - 1]) / (dists[idx] - dists[idx - 1])
    return math.log10(max_amp_um) + float(r_values[idx] + (epi_dist_km - dists[idx]) * slope)


def measure_local_period(data: Any, index: int, sample_rate: float, offset: float) -> tuple[float, float]:
    import numpy as np

    values = np.asarray(data, dtype=float)
    if len(values) < 3 or index <= 0 or index >= len(values) - 1:
        return float("nan"), float("nan")
    sign = values[index] - offset
    if sign == 0:
        sign = 1e-12
    left = index
    while left > 0 and (values[left] - offset) * sign >= 0:
        left -= 1
    right = index
    while right < len(values) - 1 and (values[right] - offset) * sign >= 0:
        right += 1
    half_periods = []
    if left < index:
        half_periods.append(index - left)
    if right > index:
        half_periods.append(right - index)
    if not half_periods:
        return float("nan"), float("nan")
    period = 2.0 * float(np.median(half_periods)) / sample_rate
    spread = 2.0 * float(np.std(half_periods)) / sample_rate if len(half_periods) > 1 else 0.0
    return period, spread


def period_is_valid(period: float, period_std: float) -> bool:
    return math.isfinite(period) and 0.01 <= period <= 5.0 and (not math.isfinite(period_std) or period_std <= 5.0)


def find_seedtools_max_amp_um(trace: Any) -> dict[str, Any] | None:
    import numpy as np

    data = np.asarray(trace.data, dtype=float)
    if len(data) < 3:
        return None
    sample_rate = float(trace.stats.sampling_rate)
    offset = float(np.mean(data))
    candidates = np.argsort(np.abs(data - offset))[::-1]
    for raw_index in candidates[: min(len(candidates), 2000)]:
        index = int(raw_index)
        period, period_std = measure_local_period(data, index, sample_rate, offset)
        if not period_is_valid(period, period_std):
            continue
        radius = max(1, int(round(period * sample_rate)))
        start = max(0, index - radius)
        stop = min(len(data), index + radius + 1)
        window = data[start:stop]
        if len(window) < max(2, int(0.02 * sample_rate)):
            continue
        amplitude_um = (float(np.max(window)) - float(np.min(window))) / 2.0
        if math.isfinite(amplitude_um) and amplitude_um > 0:
            return {
                "amplitude_um": amplitude_um,
                "period_s": period,
                "period_std_s": period_std,
                "time": trace.stats.starttime + index / sample_rate,
                "window_start": trace.stats.starttime + start / sample_rate,
                "window_end": trace.stats.starttime + (stop - 1) / sample_rate,
            }
    return None


def simulated_displacement_trace_um(trace: Any, start: Any, end: Any, inventory: Any | None, args: argparse.Namespace) -> tuple[Any, str]:
    tr = trace.copy()
    tr.trim(starttime=start, endtime=end, pad=True, nearest_sample=False, fill_value=0)
    tr.detrend("demean")
    tr.detrend("linear")
    tr.taper(max_percentage=0.05, max_length=5.0)
    tr.filter("bandpass", freqmin=args.freqmin, freqmax=min(args.freqmax, 0.45 * tr.stats.sampling_rate), corners=2, zerophase=True)
    pre_filt = tuple(args.pre_filt) if args.pre_filt else (1e-3, 1 / 60, 10.0, 20.0)
    if inventory is None:
        tr.data = tr.data.astype(float) * args.raw_to_mm * 1000.0
        return tr, "raw_scaled_um"
    tr.remove_response(
        inventory=inventory,
        output="VEL",
        water_level=args.response_water_level,
        taper=True,
        taper_fraction=0.02,
        pre_filt=pre_filt,
    )
    from obspy.signal.invsim import simulate_seismometer

    remove_paz = {"gain": 1.0, "zeros": [0j], "poles": [], "sensitivity": 1.0}
    tr.data = simulate_seismometer(
        data=tr.data.copy(),
        samp_rate=float(tr.stats.sampling_rate),
        paz_remove=remove_paz,
        remove_sensitivity=True,
        paz_simulate=None,
        pre_filt=pre_filt,
        zero_mean=True,
        taper=True,
        taper_fraction=0.02,
        water_level=None,
    )
    tr.data = tr.data * 1.0e6
    return tr, "seedtools_response_simulated_um"


def trace_amplitude_mm(trace: Any, start: Any, end: Any, inventory: Any | None, raw_to_mm: float) -> float:
    tr = trace.copy()
    try:
        tr.trim(starttime=start, endtime=end, pad=False)
        tr.detrend("demean")
        tr.detrend("linear")
        tr.taper(max_percentage=0.05)
        if inventory is not None:
            tr.remove_response(inventory=inventory, output="DISP", pre_filt=(0.01, 0.02, 20.0, 25.0), water_level=60)
            scale_to_mm = 1000.0
        else:
            scale_to_mm = raw_to_mm
        tr.filter("bandpass", freqmin=0.8, freqmax=min(10.0, 0.45 * tr.stats.sampling_rate), corners=2, zerophase=True)
    except Exception:
        scale_to_mm = raw_to_mm
    if len(tr.data) == 0:
        return float("nan")
    return max(abs(float(value)) for value in tr.data) * scale_to_mm


def calculate_station_ml_seedtools(
    stream: Any,
    event: dict[str, str],
    station_picks: list[dict[str, str]],
    sta: Station,
    distance_km_value: float,
    inventory: Any | None,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    obspy = import_obspy()
    s_picks = [row for row in station_picks if phase_group(row.get("phase", "")) == "S"]
    p_picks = [row for row in station_picks if phase_group(row.get("phase", "")) == "P"]
    if s_picks:
        reference_pick = s_picks[0]
        s_time = obspy.UTCDateTime(reference_pick["time"])
    elif args.allow_p_fallback and p_picks:
        reference_pick = p_picks[0]
        s_time = obspy.UTCDateTime(reference_pick["time"]) + args.p_fallback_s_delay
    else:
        return None

    window_len = seedtools_width_time(distance_km_value / 111.19) if args.seedtools_window else args.window_end
    if not math.isfinite(window_len) or window_len > 1000:
        return None
    start = s_time + args.window_start
    end = s_time + window_len

    component_results: dict[str, dict[str, Any]] = {}
    quality = "raw_scaled_um"
    for trace in stream.select(network=sta.network or "*", station=sta.station or "*", location=sta.location or "*"):
        component = getattr(trace.stats, "channel", "")[-1:].upper()
        if component not in {"E", "N", "1", "2"}:
            continue
        family = "E" if component in {"E", "1"} else "N"
        try:
            simulated, trace_quality = simulated_displacement_trace_um(trace, start, end, inventory, args)
            quality = trace_quality
            measured = find_seedtools_max_amp_um(simulated)
        except Exception as exc:
            eprint(f"seedtools ML simulation failed for {stream_trace_id(trace)}: {exc}")
            continue
        if measured:
            component_results[family] = measured

    if not component_results:
        return None
    amplitudes = [
        item["amplitude_um"]
        for item in component_results.values()
        if math.isfinite(item["amplitude_um"]) and item["amplitude_um"] > 0
    ]
    if not amplitudes:
        return None
    if "E" in component_results and "N" in component_results:
        max_amp_um = (component_results["E"]["amplitude_um"] + component_results["N"]["amplitude_um"]) / 2.0
    else:
        max_amp_um = amplitudes[0]
    if not math.isfinite(max_amp_um) or max_amp_um <= 0:
        return None

    if args.ml_method == "seedtools-dd1":
        ml = calculate_seedtools_dd1_ml(max_amp_um, distance_km_value, args.region)
        method = f"seedtools-dd1-{args.region.upper()}"
    else:
        amplitude_mm = max_amp_um / 1000.0
        ml = math.log10(amplitude_mm) + args.a * math.log10(distance_km_value) + args.b * distance_km_value + args.c
        method = "wood-anderson-formula"

    e_result = component_results.get("E", {})
    n_result = component_results.get("N", {})
    return {
        "ml": ml,
        "max_amp_um": max_amp_um,
        "max_amp_e_um": e_result.get("amplitude_um", ""),
        "max_amp_n_um": n_result.get("amplitude_um", ""),
        "period_e_s": e_result.get("period_s", ""),
        "period_n_s": n_result.get("period_s", ""),
        "time_sme": datetime_to_text(e_result["time"].datetime) if e_result.get("time") else "",
        "time_smn": datetime_to_text(n_result["time"].datetime) if n_result.get("time") else "",
        "distance_km": distance_km_value,
        "method": method,
        "quality": quality,
    }


def cmd_magnitude(args: argparse.Namespace) -> int:
    events = {row["event_id"]: row for row in read_csv_rows(args.events) if row.get("event_id")}
    picks = read_csv_rows(args.picks)
    stations = read_stations(args.stations)
    inventory = load_inventory(args.inventory)
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for pick in picks:
        event_id = pick.get("event_id")
        sta = station_for_pick(pick, stations)
        if event_id and sta:
            grouped[(event_id, sta.station_id)].append(pick)

    station_rows = []
    event_magnitudes: dict[str, list[float]] = defaultdict(list)
    stream_cache: dict[str, Any] = {}
    for (event_id, sid), station_picks in grouped.items():
        event = events.get(event_id)
        sta = stations.get(sid)
        if event is None or sta is None:
            continue
        event_lat = parse_float(event.get("latitude"))
        event_lon = parse_float(event.get("longitude"))
        if not math.isfinite(event_lat) or not math.isfinite(event_lon):
            continue
        distance = max(distance_km(event_lat, event_lon, sta.latitude, sta.longitude), args.min_distance_km)
        s_picks = [row for row in station_picks if phase_group(row.get("phase", "")) == "S"]
        waveform_path = combine_waveform_paths(station_picks)
        if not waveform_path:
            continue
        try:
            if waveform_path not in stream_cache:
                stream_cache[waveform_path] = read_waveform_field(waveform_path)
            stream = stream_cache[waveform_path]
            result = calculate_station_ml_seedtools(stream, event, station_picks, sta, distance, inventory, args)
            if result is None:
                continue
            ml = float(result["ml"])
            event_magnitudes[event_id].append(ml)
            station_rows.append(
                {
                    "event_id": event_id,
                    "station_id": sid,
                    "max_amp_um": f"{result['max_amp_um']:.6g}",
                    "max_amp_e_um": f"{result['max_amp_e_um']:.6g}" if isinstance(result["max_amp_e_um"], (float, int)) else "",
                    "max_amp_n_um": f"{result['max_amp_n_um']:.6g}" if isinstance(result["max_amp_n_um"], (float, int)) else "",
                    "period_e_s": f"{result['period_e_s']:.6g}" if isinstance(result["period_e_s"], (float, int)) else "",
                    "period_n_s": f"{result['period_n_s']:.6g}" if isinstance(result["period_n_s"], (float, int)) else "",
                    "time_sme": result["time_sme"],
                    "time_smn": result["time_smn"],
                    "distance_km": f"{distance:.3f}",
                    "ml": f"{ml:.3f}",
                    "method": result["method"],
                    "quality": result["quality"],
                }
            )
        except Exception as exc:
            eprint(f"ML failed for {event_id} {sid}: {exc}")

    event_rows = []
    for event_id, event in events.items():
        values = event_magnitudes.get(event_id, [])
        event = dict(event)
        if values:
            event["magnitude"] = f"{median(values):.3f}"
            event["magnitude_type"] = "ML"
        event_rows.append(event)
    write_csv_rows(args.output, event_rows, EVENT_FIELDS)
    if args.station_output:
        write_csv_rows(args.station_output, station_rows)
    eprint(f"events_with_ml={sum(1 for vals in event_magnitudes.values() if vals)} station_magnitudes={len(station_rows)}")
    return 0


def polarity_sign(value: str) -> int | None:
    text = (value or "").upper()
    if text == "U":
        return 1
    if text == "D":
        return -1
    return None


def moment_tensor_ned(strike: float, dip: float, rake: float) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    strike_r = math.radians(strike)
    dip_r = math.radians(dip)
    rake_r = math.radians(rake)
    sd, cd = math.sin(dip_r), math.cos(dip_r)
    sl, cl = math.sin(rake_r), math.cos(rake_r)
    sp, cp = math.sin(strike_r), math.cos(strike_r)
    s2p, c2p = math.sin(2.0 * strike_r), math.cos(2.0 * strike_r)
    s2d, c2d = math.sin(2.0 * dip_r), math.cos(2.0 * dip_r)

    mnn = -(sd * cl * s2p + s2d * sl * sp * sp)
    mee = sd * cl * s2p - s2d * sl * cp * cp
    mdd = s2d * sl
    mne = sd * cl * c2p + 0.5 * s2d * sl * s2p
    mnd = -(cd * cl * cp + c2d * sl * sp)
    med = -(cd * cl * sp - c2d * sl * cp)
    return ((mnn, mne, mnd), (mne, mee, med), (mnd, med, mdd))


def p_radiation_sign(moment: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]], ray: tuple[float, float, float]) -> int:
    amplitude = 0.0
    for i in range(3):
        for j in range(3):
            amplitude += ray[i] * moment[i][j] * ray[j]
    return 1 if amplitude >= 0.0 else -1


def ray_vector_ned(event: dict[str, str], station: Station) -> tuple[float, float, float] | None:
    event_lat = parse_float(event.get("latitude"))
    event_lon = parse_float(event.get("longitude"))
    depth = max(parse_float(event.get("depth_km"), 0.0), 0.0)
    if not math.isfinite(event_lat) or not math.isfinite(event_lon):
        return None
    east_km, north_km = project_lonlat(station.longitude, station.latitude, event_lon, event_lat)
    down_km = -depth
    norm = math.sqrt(north_km * north_km + east_km * east_km + down_km * down_km)
    if norm <= 0.0:
        return None
    return north_km / norm, east_km / norm, down_km / norm


def auxiliary_plane_safe(strike: float, dip: float, rake: float) -> tuple[str, str, str]:
    try:
        from obspy.imaging.beachball import aux_plane

        aux_strike, aux_dip, aux_rake = aux_plane(strike, dip, rake)
        return f"{aux_strike:.1f}", f"{aux_dip:.1f}", f"{aux_rake:.1f}"
    except Exception:
        return "", "", ""


def mechanism_quality(n_polarities: int, misfit: float, min_polarities: int) -> str:
    if n_polarities < min_polarities:
        return "insufficient"
    if misfit <= 0.15 and n_polarities >= max(12, min_polarities):
        return "A"
    if misfit <= 0.25:
        return "B"
    if misfit <= 0.35:
        return "C"
    return "D"


def solve_first_motion_grid(event: dict[str, str], picks: list[dict[str, str]], stations: dict[str, Station], args: argparse.Namespace) -> dict[str, Any]:
    observations: list[tuple[int, tuple[float, float, float]]] = []
    for row in picks:
        if phase_group(row.get("phase", "")) != "P":
            continue
        sign = polarity_sign(row.get("polarity", ""))
        if sign is None:
            continue
        sta = station_for_pick(row, stations)
        if sta is None:
            continue
        ray = ray_vector_ned(event, sta)
        if ray is None:
            continue
        observations.append((sign, ray))

    if len(observations) < args.min_polarities:
        return {
            "mechanism_n_polarities": len(observations),
            "mechanism_misfit_fraction": "",
            "mechanism_quality": "insufficient",
            "mechanism_method": "first-motion-grid",
        }

    best: dict[str, Any] | None = None
    strike_step = max(1, args.strike_step)
    dip_step = max(1, args.dip_step)
    rake_step = max(1, args.rake_step)
    for strike in range(0, 360, strike_step):
        for dip in range(dip_step, 91, dip_step):
            for rake in range(-180, 181, rake_step):
                moment = moment_tensor_ned(strike, dip, rake)
                mismatches = sum(1 for observed, ray in observations if p_radiation_sign(moment, ray) != observed)
                misfit = mismatches / len(observations)
                if best is None or misfit < best["misfit"]:
                    best = {"strike": strike, "dip": dip, "rake": rake, "misfit": misfit}
                    if mismatches == 0:
                        break
            if best and best["misfit"] == 0.0:
                break
        if best and best["misfit"] == 0.0:
            break

    assert best is not None
    aux_strike, aux_dip, aux_rake = auxiliary_plane_safe(best["strike"], best["dip"], best["rake"])
    return {
        "mechanism_strike": f"{best['strike']:.1f}",
        "mechanism_dip": f"{best['dip']:.1f}",
        "mechanism_rake": f"{best['rake']:.1f}",
        "mechanism_aux_strike": aux_strike,
        "mechanism_aux_dip": aux_dip,
        "mechanism_aux_rake": aux_rake,
        "mechanism_n_polarities": len(observations),
        "mechanism_misfit_fraction": f"{best['misfit']:.3f}",
        "mechanism_quality": mechanism_quality(len(observations), best["misfit"], args.min_polarities),
        "mechanism_method": "first-motion-grid",
    }


def union_fieldnames(rows: Sequence[dict[str, Any]], preferred: Sequence[str] = ()) -> list[str]:
    fields = list(preferred)
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    return fields


def cmd_mechanism(args: argparse.Namespace) -> int:
    events = [dict(row) for row in read_csv_rows(args.events)]
    picks = read_csv_rows(args.picks)
    stations = read_stations(args.stations)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in picks:
        event_id = row.get("event_id", "")
        if event_id:
            grouped[event_id].append(row)

    mechanism_rows: list[dict[str, Any]] = []
    for event in events:
        event_id = event.get("event_id", "")
        row = {"event_id": event_id}
        if args.method == "first-motion-grid":
            row.update(solve_first_motion_grid(event, grouped.get(event_id, []), stations, args))
        elif args.method == "hash-export":
            row.update(
                {
                    "mechanism_n_polarities": sum(
                        1
                        for pick in grouped.get(event_id, [])
                        if phase_group(pick.get("phase", "")) == "P" and polarity_sign(pick.get("polarity", "")) is not None
                    ),
                    "mechanism_quality": "hash-exported",
                    "mechanism_method": "hash-export",
                }
            )
        else:
            raise CatalogError(f"Unsupported mechanism method: {args.method}")
        mechanism_rows.append(row)

    write_csv_rows(args.output, mechanism_rows, MECHANISM_FIELDS)
    if args.hash_input:
        hash_rows = []
        for event in events:
            event_id = event.get("event_id", "")
            for pick in grouped.get(event_id, []):
                if phase_group(pick.get("phase", "")) == "P" and polarity_sign(pick.get("polarity", "")) is not None:
                    sta = station_for_pick(pick, stations)
                    if sta:
                        hash_rows.append(
                            {
                                "event_id": event_id,
                                "origin_time": event.get("origin_time", ""),
                                "latitude": event.get("latitude", ""),
                                "longitude": event.get("longitude", ""),
                                "depth_km": event.get("depth_km", ""),
                                "network": sta.network,
                                "station": sta.station,
                                "location": sta.location,
                                "longitude_station": sta.longitude,
                                "latitude_station": sta.latitude,
                                "elevation_m": sta.elevation_m,
                                "polarity": pick.get("polarity", ""),
                                "onset_quality": pick.get("polarity_quality", ""),
                                "pick_time": pick.get("time", ""),
                            }
                        )
        write_csv_rows(args.hash_input, hash_rows)
    if args.catalog_output:
        by_event = {row["event_id"]: row for row in mechanism_rows if row.get("event_id")}
        final_rows = []
        for event in events:
            merged = dict(event)
            mechanism = by_event.get(event.get("event_id", ""), {})
            for key, value in mechanism.items():
                if key != "event_id":
                    merged[key] = value
            final_rows.append(merged)
        preferred = EVENT_FIELDS + [field for field in MECHANISM_FIELDS if field != "event_id"]
        write_csv_rows(args.catalog_output, final_rows, union_fieldnames(final_rows, preferred))
    eprint(f"mechanism_events={len(mechanism_rows)}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    rows = read_csv_rows(args.events)
    times = []
    mags = []
    for row in rows:
        if row.get("origin_time"):
            times.append(parse_datetime(row["origin_time"]))
        mag = parse_float(row.get("magnitude"))
        if math.isfinite(mag):
            mags.append(mag)
    summary = {
        "event_count": len(rows),
        "start_time": datetime_to_text(min(times)) if times else None,
        "end_time": datetime_to_text(max(times)) if times else None,
        "magnitude_count": len(mags),
        "magnitude_min": min(mags) if mags else None,
        "magnitude_median": median(mags) if mags else None,
        "magnitude_max": max(mags) if mags else None,
    }
    ensure_parent(args.output)
    Path(args.output).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if args.rate_output and times:
        counts: dict[str, int] = defaultdict(int)
        for value in times:
            counts[value.date().isoformat()] += 1
        write_csv_rows(args.rate_output, [{"date": key, "count": counts[key]} for key in sorted(counts)])
    return 0


def cmd_plot(args: argparse.Namespace) -> int:
    events = read_csv_rows(args.events)
    stations = read_stations(args.stations) if args.stations else {}
    event_lons = [parse_float(row.get("longitude")) for row in events]
    event_lats = [parse_float(row.get("latitude")) for row in events]
    event_mags = [parse_float(row.get("magnitude"), 1.0) for row in events]
    points = [(lon, lat, mag) for lon, lat, mag in zip(event_lons, event_lats, event_mags) if math.isfinite(lon) and math.isfinite(lat)]
    if not points:
        raise CatalogError("No plottable event coordinates found.")

    import matplotlib.pyplot as plt

    ensure_parent(args.output)
    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature

        projection = ccrs.PlateCarree()
        fig = plt.figure(figsize=(args.width, args.height), dpi=args.dpi)
        ax = plt.axes(projection=projection)
        ax.add_feature(cfeature.LAND, facecolor="0.96")
        ax.add_feature(cfeature.OCEAN, facecolor="0.90")
        ax.add_feature(cfeature.COASTLINE, linewidth=0.6)
        ax.add_feature(cfeature.BORDERS, linewidth=0.4)
        ax.gridlines(draw_labels=True, linewidth=0.3, alpha=0.5)
        transform = projection
    except Exception:
        fig, ax = plt.subplots(figsize=(args.width, args.height), dpi=args.dpi)
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        transform = None

    lons = [point[0] for point in points]
    lats = [point[1] for point in points]
    mags = [point[2] if math.isfinite(point[2]) else 1.0 for point in points]
    sizes = [max(12.0, (mag + 1.0) ** 2 * 10.0) for mag in mags]
    scatter_kwargs = {"s": sizes, "c": mags, "cmap": "viridis", "alpha": 0.75, "edgecolors": "black", "linewidths": 0.3}
    if transform is not None:
        scatter_kwargs["transform"] = transform
    sc = ax.scatter(lons, lats, **scatter_kwargs)
    if stations:
        station_values = canonical_station_values(stations)
        sx = [sta.longitude for sta in station_values]
        sy = [sta.latitude for sta in station_values]
        station_kwargs = {"marker": "^", "s": 28, "c": "#d94801", "edgecolors": "black", "linewidths": 0.3, "label": "Stations"}
        if transform is not None:
            station_kwargs["transform"] = transform
        ax.scatter(sx, sy, **station_kwargs)
        ax.legend(loc="best")
    pad = args.pad_degree
    ax.set_xlim(min(lons) - pad, max(lons) + pad)
    ax.set_ylim(min(lats) - pad, max(lats) + pad)
    ax.set_title(args.title)
    fig.colorbar(sc, ax=ax, label="Magnitude")
    fig.tight_layout()
    fig.savefig(args.output)
    plt.close(fig)
    print(args.output)
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    if args.location_method == "bayes" and not args.bayes_command:
        raise CatalogError("catalog --location-method bayes requires --bayes-command. Use locate --method bayes separately to export a bayes_location JSON payload.")
    if args.association_method == "simple" and not args.smoke_test_simple:
        raise CatalogError("catalog --association-method simple is a smoke-test path. Pass --smoke-test-simple to use it intentionally, or use --association-method gamma for production association.")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    script = str(Path(__file__).resolve())
    base = [sys.executable, script]

    waveforms_csv = output_dir / "waveforms.csv"
    waveform_errors = output_dir / "waveform_errors.csv"
    picks_csv = output_dir / "picks.csv"
    pick_errors = output_dir / "pick_errors.csv"
    events_associated = output_dir / "events_associated.csv"
    assignments_csv = output_dir / "assignments.csv"
    picks_associated = output_dir / "picks_associated.csv"
    picks_polarity = output_dir / "picks_with_polarity.csv"
    events_located = output_dir / "events_located.csv"
    events_ml = output_dir / "events_ml.csv"
    station_ml = output_dir / "station_ml.csv"
    mechanisms_csv = output_dir / "mechanisms.csv"
    hash_input = output_dir / "hash_input.csv"
    final_catalog = output_dir / args.catalog_name
    activity_json = output_dir / "activity.json"
    daily_counts = output_dir / "daily_counts.csv"
    map_png = output_dir / "catalog_map.png"

    run_logged(base + ["scan", "-w", args.waveforms, "-o", str(waveforms_csv), "--errors", str(waveform_errors), "--extensions", args.extensions])

    pick_cmd = [
        "pick",
        "-w",
        args.waveforms,
        "-o",
        str(picks_csv),
        "--errors",
        str(pick_errors),
        "--extensions",
        args.extensions,
        "--picker",
        args.picker,
        "--phases",
        args.phases,
        "--model",
        args.model,
        "--device",
        args.device,
    ]
    run_logged(base + pick_cmd)

    associate_cmd = [
        "associate",
        "--method",
        args.association_method,
        "-p",
        str(picks_csv),
        "-s",
        args.stations,
        "-o",
        str(events_associated),
        "--assignments",
        str(assignments_csv),
        "--associated-picks",
        str(picks_associated),
        "--min-picks-per-eq",
        str(args.min_picks_per_eq),
        "--min-p-picks-per-eq",
        str(args.min_p_picks_per_eq),
        "--min-s-picks-per-eq",
        str(args.min_s_picks_per_eq),
        "--min-depth",
        str(args.association_min_depth),
        "--max-depth",
        str(args.association_max_depth),
    ]
    if args.association_method == "real":
        associate_cmd.extend(
            [
                "--real-R",
                args.real_R,
                "--real-S",
                args.real_S,
                "--real-V",
                args.real_V,
                "--real-min-score",
                str(args.real_min_score),
                "--real-jobs",
                str(args.real_jobs),
                "--workdir",
                str(output_dir / "real_work"),
            ]
        )
    run_logged(base + associate_cmd)

    run_logged(base + ["polarity", "-p", str(picks_associated), "-o", str(picks_polarity), "--min-score", str(args.polarity_min_score)])

    locate_cmd = [
        "locate",
        "-p",
        str(picks_polarity),
        "-s",
        args.stations,
        "-v",
        args.velocity_model,
        "-o",
        str(events_located),
        "--method",
        args.location_method,
        "--min-picks",
        str(args.location_min_picks),
    ]
    if args.bayes_repo:
        locate_cmd.extend(["--bayes-repo", args.bayes_repo])
    if args.bayes_command:
        locate_cmd.extend(["--bayes-command", args.bayes_command])
    run_logged(base + locate_cmd)

    magnitude_cmd = [
        "magnitude-ml",
        "-e",
        str(events_located),
        "-p",
        str(picks_polarity),
        "-s",
        args.stations,
        "-o",
        str(events_ml),
        "--station-output",
        str(station_ml),
        "--raw-to-mm",
        str(args.raw_to_mm),
        "--ml-method",
        args.ml_method,
        "--region",
        args.region,
        "--freqmin",
        str(args.mag_freqmin),
        "--freqmax",
        str(args.mag_freqmax),
        "--response-water-level",
        str(args.response_water_level),
    ]
    if args.inventory:
        magnitude_cmd.extend(["--inventory", args.inventory])
    if args.no_seedtools_window:
        magnitude_cmd.append("--no-seedtools-window")
    if args.allow_p_fallback:
        magnitude_cmd.extend(["--allow-p-fallback", "--p-fallback-s-delay", str(args.p_fallback_s_delay)])
    run_logged(base + magnitude_cmd)

    run_logged(
        base
        + [
            "mechanism",
            "-e",
            str(events_ml),
            "-p",
            str(picks_polarity),
            "-s",
            args.stations,
            "-o",
            str(mechanisms_csv),
            "--catalog-output",
            str(final_catalog),
            "--hash-input",
            str(hash_input),
            "--method",
            args.mechanism_method,
            "--min-polarities",
            str(args.min_polarities),
            "--strike-step",
            str(args.mechanism_grid_step),
            "--dip-step",
            str(args.mechanism_grid_step),
            "--rake-step",
            str(args.mechanism_grid_step),
        ]
    )

    run_logged(base + ["analyze", "-e", str(final_catalog), "-o", str(activity_json), "--rate-output", str(daily_counts)])
    if not args.skip_map:
        try:
            run_logged(base + ["plot-map", "-e", str(final_catalog), "-s", args.stations, "-o", str(map_png)])
        except subprocess.CalledProcessError as exc:
            eprint(f"map generation failed but catalog is complete: {exc}")

    summary = {
        "waveforms": str(waveforms_csv),
        "picks": str(picks_csv),
        "associated_picks": str(picks_associated),
        "polarity_picks": str(picks_polarity),
        "located_events": str(events_located),
        "magnitude_events": str(events_ml),
        "mechanisms": str(mechanisms_csv),
        "final_catalog": str(final_catalog),
        "activity": str(activity_json),
        "map": str(map_png) if map_png.exists() else None,
    }
    (output_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(final_catalog)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SeismicX automated earthquake catalog helper CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    init_config = sub.add_parser("init-config", help="Write a YAML configuration template")
    init_config.add_argument("-o", "--output", default="seismicx_catalog.yaml")
    init_config.set_defaults(func=cmd_init_config)

    scan = sub.add_parser("scan", help="Scan waveform files readable by ObsPy")
    scan.add_argument("-w", "--waveforms", required=True)
    scan.add_argument("-o", "--output", required=True)
    scan.add_argument("--errors")
    scan.add_argument("--extensions", default="common", help="'common', 'all', or comma-separated extensions")
    scan.set_defaults(func=cmd_scan)

    list_models = sub.add_parser("list-models", help="List bundled SeismicX models")
    list_models.add_argument("--json", action="store_true")
    list_models.set_defaults(func=cmd_list_models)

    pick = sub.add_parser("pick", help="Pick phases from waveform files")
    pick.add_argument("-w", "--waveforms", required=True)
    pick.add_argument("-o", "--output", required=True)
    pick.add_argument("--errors")
    pick.add_argument("--extensions", default="common")
    pick.add_argument("--picker", choices=["torchscript-pnsn", "classic"], default="torchscript-pnsn")
    pick.add_argument("--phases", default="Pg,Sg,Pn,Sn")
    pick.add_argument("--model", default="pnsn-v3", help="Bundled model id from list-models or a TorchScript path")
    pick.add_argument("--device", default="cpu")
    pick.add_argument("--sta", type=float, default=0.5)
    pick.add_argument("--lta", type=float, default=5.0)
    pick.add_argument("--trigger-on", type=float, default=3.0)
    pick.add_argument("--trigger-off", type=float, default=1.5)
    pick.add_argument("--classic-bandpass", action="store_true", help="Opt-in bandpass for the classic STA/LTA smoke-test picker only; continuous waveform picking is unfiltered by default")
    pick.add_argument("--freqmin", type=float, default=1.0, help="Classic STA/LTA bandpass low corner, used only with --classic-bandpass")
    pick.add_argument("--freqmax", type=float, default=20.0, help="Classic STA/LTA bandpass high corner, used only with --classic-bandpass")
    pick.add_argument("--noise-window", type=float, default=2.0)
    pick.add_argument("--signal-window", type=float, default=1.0)
    pick.add_argument("--max-picks-per-trace", type=int, default=20)
    pick.add_argument("--polarity-min-score", type=float, default=2.0)
    pick.add_argument("--progress-every", type=int, default=100)
    pick.set_defaults(func=cmd_pick)

    polarity = sub.add_parser("polarity", help="Compute first-motion polarity for P picks")
    polarity.add_argument("-p", "--picks", required=True)
    polarity.add_argument("-o", "--output", required=True)
    polarity.add_argument("--pre-window", type=float, default=0.25)
    polarity.add_argument("--post-window", type=float, default=0.12)
    polarity.add_argument("--min-score", type=float, default=2.0)
    polarity.add_argument("--bandpass", action="store_true", help="Opt-in first-motion bandpass; default preserves the unfiltered pick waveform")
    polarity.set_defaults(func=cmd_polarity)

    associate = sub.add_parser("associate", help="Associate picks with GaMMA, REAL, or a simple time-window fallback")
    associate.add_argument("-p", "--picks", required=True)
    associate.add_argument("-s", "--stations", required=True)
    associate.add_argument("-o", "--output", required=True)
    associate.add_argument("--assignments")
    associate.add_argument("--associated-picks", help="Write picks with event_id populated after association")
    associate.add_argument("--method", choices=["gamma", "real", "simple"], default="gamma")
    associate.add_argument("--workdir", default="real_work", help="REAL run metadata directory or external-command workspace")
    associate.add_argument("--real-command", help="Run an external REAL adapter command instead of the bundled Python backend")
    associate.add_argument("--real-R", default="0.5/20/0.05/2/5", help="REAL rx/rh/dx/dh/tint[/gap/GCarc/latref/lonref]")
    associate.add_argument("--real-G", default="2.0/20/0.01/1", help="Travel-time table parameters for --real-command only")
    associate.add_argument("--real-V", default="6.2/3.5", help="REAL vp/vs[/surface_vp/surface_vs/elevation_flag]")
    associate.add_argument("--real-S", default="3/2/5/2/0.5/0.1/1.5", help="REAL minP/minS/minTotal/minBoth/std/dtps/nrt[/rsel]")
    associate.add_argument("--real-min-score", type=float, default=0.0, help="Ignore REAL input picks below this confidence")
    associate.add_argument("--real-jobs", type=int, default=max(1, min(os.cpu_count() or 1, 8)), help="Numba worker threads for bundled Python REAL")
    associate.add_argument("--vp", type=float, default=6.0)
    associate.add_argument("--vs", type=float, default=3.5)
    associate.add_argument("--gamma-method", default="BGMM")
    associate.add_argument("--use-amplitude", action="store_true")
    associate.add_argument("--use-dbscan", action="store_true")
    associate.add_argument("--dbscan-eps", type=float, default=10.0)
    associate.add_argument("--dbscan-min-samples", type=int, default=3)
    associate.add_argument("--min-picks-per-eq", type=int, default=5)
    associate.add_argument("--min-p-picks-per-eq", type=int, default=3)
    associate.add_argument("--min-s-picks-per-eq", type=int, default=0)
    associate.add_argument("--max-sigma11", type=float, default=2.0)
    associate.add_argument("--max-sigma22", type=float, default=1.0)
    associate.add_argument("--max-sigma12", type=float, default=1.0)
    associate.add_argument("--oversampling-factor", type=float, default=10.0)
    associate.add_argument("--min-depth", type=float, default=0.0)
    associate.add_argument("--max-depth", type=float, default=30.0)
    associate.add_argument("--min-longitude", type=float)
    associate.add_argument("--max-longitude", type=float)
    associate.add_argument("--min-latitude", type=float)
    associate.add_argument("--max-latitude", type=float)
    associate.add_argument("--time-gap", type=float, default=20.0)
    associate.set_defaults(func=cmd_associate)

    build_tools = sub.add_parser("build-tools", help="Download, clone, and locally build optional engines")
    build_tools.add_argument("--tool", choices=["real", "hash", "pnsn", "bayes-location", "seismological-ai-tools", "all"], default="all")
    build_tools.add_argument("--tools-dir", default="external")
    build_tools.add_argument("--hash-source", help="Path to pyhash/HASH source; defaults to ./pyhash when present")
    build_tools.add_argument("--jobs", type=int, default=max(1, min(os.cpu_count() or 1, 8)))
    build_tools.add_argument("--skip-build", action="store_true")
    build_tools.add_argument("-o", "--output", help="Write build manifest JSON")
    build_tools.set_defaults(func=cmd_build_tools)

    locate = sub.add_parser("locate", help="Locate associated events with a grid solver or bayes_location adapter")
    locate.add_argument("-p", "--picks", required=True)
    locate.add_argument("-s", "--stations", required=True)
    locate.add_argument("-v", "--velocity-model")
    locate.add_argument("-o", "--output", required=True)
    locate.add_argument("--events")
    locate.add_argument("--method", choices=["grid", "bayes"], default="grid")
    locate.add_argument("--bayes-repo")
    locate.add_argument("--bayes-command")
    locate.add_argument("--min-picks", type=int, default=4)
    locate.add_argument("--min-depth", type=float, default=0.0)
    locate.add_argument("--max-depth", type=float, default=30.0)
    locate.add_argument("--pad-degree", type=float, default=0.2)
    locate.add_argument("--grid-lat", type=int, default=15)
    locate.add_argument("--grid-lon", type=int, default=15)
    locate.add_argument("--grid-depth", type=int, default=9)
    locate.add_argument("--origin-time-pad", type=float, default=30.0)
    locate.set_defaults(func=cmd_locate)

    magnitude = sub.add_parser("magnitude-ml", help="Compute local magnitude ML")
    magnitude.add_argument("-e", "--events", required=True)
    magnitude.add_argument("-p", "--picks", required=True)
    magnitude.add_argument("-s", "--stations", required=True)
    magnitude.add_argument("-o", "--output", required=True)
    magnitude.add_argument("--station-output")
    magnitude.add_argument("--inventory", help="StationXML, RESP file/directory, dataless metadata, or seedtools sta.resp.path mapping")
    magnitude.add_argument("--ml-method", choices=["seedtools-dd1", "wood-anderson-formula"], default="seedtools-dd1")
    magnitude.add_argument("--region", choices=["R11", "R12", "R13", "R14", "R15"], default="R13", help="Seedtools regional ML correction curve")
    magnitude.add_argument("--seedtools-window", action=argparse.BooleanOptionalAction, default=True, help="Use seedtools distance-dependent S-wave amplitude window")
    magnitude.add_argument("--allow-p-fallback", action="store_true", help="Estimate an S window from P picks when S is unavailable")
    magnitude.add_argument("--p-fallback-s-delay", type=float, default=3.0)
    magnitude.add_argument("--window-start", type=float, default=-0.5)
    magnitude.add_argument("--window-end", type=float, default=3.0)
    magnitude.add_argument("--raw-to-mm", type=float, default=1.0)
    magnitude.add_argument("--min-distance-km", type=float, default=1.0)
    magnitude.add_argument("--freqmin", type=float, default=0.8)
    magnitude.add_argument("--freqmax", type=float, default=10.0)
    magnitude.add_argument("--pre-filt", nargs=4, type=float, default=(1e-3, 1 / 60, 10.0, 20.0))
    magnitude.add_argument("--response-water-level", type=float, default=20.0)
    magnitude.add_argument("--a", type=float, default=1.11)
    magnitude.add_argument("--b", type=float, default=0.00189)
    magnitude.add_argument("--c", type=float, default=-2.09)
    magnitude.set_defaults(func=cmd_magnitude)

    mechanism = sub.add_parser("mechanism", help="Compute or export first-motion focal mechanisms")
    mechanism.add_argument("-e", "--events", required=True)
    mechanism.add_argument("-p", "--picks", required=True)
    mechanism.add_argument("-s", "--stations", required=True)
    mechanism.add_argument("-o", "--output", required=True)
    mechanism.add_argument("--catalog-output", help="Write events merged with mechanism columns")
    mechanism.add_argument("--hash-input", help="Write a HASH-friendly polarity input CSV")
    mechanism.add_argument("--method", choices=["first-motion-grid", "hash-export"], default="first-motion-grid")
    mechanism.add_argument("--min-polarities", type=int, default=6)
    mechanism.add_argument("--strike-step", type=int, default=10)
    mechanism.add_argument("--dip-step", type=int, default=10)
    mechanism.add_argument("--rake-step", type=int, default=10)
    mechanism.set_defaults(func=cmd_mechanism)

    analyze = sub.add_parser("analyze", help="Summarize catalog activity")
    analyze.add_argument("-e", "--events", required=True)
    analyze.add_argument("-o", "--output", required=True)
    analyze.add_argument("--rate-output")
    analyze.set_defaults(func=cmd_analyze)

    plot = sub.add_parser("plot-map", help="Plot event locations and stations")
    plot.add_argument("-e", "--events", required=True)
    plot.add_argument("-o", "--output", required=True)
    plot.add_argument("-s", "--stations")
    plot.add_argument("--title", default="SeismicX earthquake catalog")
    plot.add_argument("--pad-degree", type=float, default=0.2)
    plot.add_argument("--width", type=float, default=8.0)
    plot.add_argument("--height", type=float, default=6.0)
    plot.add_argument("--dpi", type=int, default=180)
    plot.set_defaults(func=cmd_plot)

    catalog = sub.add_parser("catalog", help="Run the end-to-end earthquake catalog workflow")
    catalog.add_argument("-w", "--waveforms", required=True)
    catalog.add_argument("-s", "--stations", required=True)
    catalog.add_argument("-v", "--velocity-model", required=True)
    catalog.add_argument("-o", "--output-dir", required=True)
    catalog.add_argument("--catalog-name", default="catalog_final.csv")
    catalog.add_argument("--extensions", default="common")
    catalog.add_argument("--picker", choices=["torchscript-pnsn", "classic"], default="torchscript-pnsn")
    catalog.add_argument("--phases", default="Pg,Sg,Pn,Sn")
    catalog.add_argument("--model", default="pnsn-v3")
    catalog.add_argument("--device", default="cpu")
    catalog.add_argument("--association-method", choices=["simple", "gamma", "real"], default="gamma")
    catalog.add_argument("--smoke-test-simple", action="store_true", help="Allow the simple time-window associator for tiny smoke tests")
    catalog.add_argument("--min-picks-per-eq", type=int, default=5)
    catalog.add_argument("--min-p-picks-per-eq", type=int, default=3)
    catalog.add_argument("--min-s-picks-per-eq", type=int, default=0)
    catalog.add_argument("--association-min-depth", type=float, default=0.0)
    catalog.add_argument("--association-max-depth", type=float, default=30.0)
    catalog.add_argument("--real-R", default="0.5/20/0.05/2/5")
    catalog.add_argument("--real-V", default="6.2/3.5")
    catalog.add_argument("--real-S", default="3/2/5/2/0.5/0.1/1.5")
    catalog.add_argument("--real-min-score", type=float, default=0.0)
    catalog.add_argument("--real-jobs", type=int, default=max(1, min(os.cpu_count() or 1, 8)))
    catalog.add_argument("--location-method", choices=["grid", "bayes"], default="grid")
    catalog.add_argument("--location-min-picks", type=int, default=4)
    catalog.add_argument("--bayes-repo")
    catalog.add_argument("--bayes-command")
    catalog.add_argument("--inventory")
    catalog.add_argument("--ml-method", choices=["seedtools-dd1", "wood-anderson-formula"], default="seedtools-dd1")
    catalog.add_argument("--region", choices=["R11", "R12", "R13", "R14", "R15"], default="R13")
    catalog.add_argument("--raw-to-mm", type=float, default=1.0)
    catalog.add_argument("--mag-freqmin", type=float, default=0.8)
    catalog.add_argument("--mag-freqmax", type=float, default=10.0)
    catalog.add_argument("--response-water-level", type=float, default=20.0)
    catalog.add_argument("--no-seedtools-window", action="store_true")
    catalog.add_argument("--allow-p-fallback", action="store_true")
    catalog.add_argument("--p-fallback-s-delay", type=float, default=3.0)
    catalog.add_argument("--polarity-min-score", type=float, default=2.0)
    catalog.add_argument("--mechanism-method", choices=["first-motion-grid", "hash-export"], default="first-motion-grid")
    catalog.add_argument("--min-polarities", type=int, default=6)
    catalog.add_argument("--mechanism-grid-step", type=int, default=10)
    catalog.add_argument("--skip-map", action="store_true")
    catalog.set_defaults(func=cmd_catalog)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except CatalogError as exc:
        eprint(f"error: {exc}")
        return 2
    except subprocess.CalledProcessError as exc:
        eprint(f"command failed with exit code {exc.returncode}: {exc.cmd}")
        return exc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
