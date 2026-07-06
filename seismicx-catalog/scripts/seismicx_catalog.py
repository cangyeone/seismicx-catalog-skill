#!/usr/bin/env python3
"""SeismicX Catalog helper CLI.

This script is intentionally lightweight: it provides clean data contracts,
format conversion, quality-control helpers, and wrappers around established
seismological packages. Heavy models and external engines such as REAL,
GaMMA, HASH, and bayes_location stay outside the skill and are referenced by
path or command template at runtime.
"""

from __future__ import annotations

import argparse
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

STATION_FIELDS = [
    "station_id",
    "network",
    "station",
    "location",
    "longitude",
    "latitude",
    "elevation_m",
]


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
) -> tuple[str, str, float]:
    import numpy as np

    tr = trace.copy()
    try:
        tr.detrend("demean")
        tr.detrend("linear")
        tr.taper(max_percentage=0.02)
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
      picker: classic
      phases: [P, S]
      device: cpu
      model: null

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
      formula: "log10(A_mm) + a*log10(R_km) + b*R_km + c"
      a: 1.11
      b: 0.00189
      c: -2.09

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
                polarity, polarity_quality, polarity_score = estimate_first_motion(tr, pick_time, min_score=args.polarity_min_score)
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


def seisbench_pick_file(path: Path, requested: set[str], args: argparse.Namespace) -> list[dict[str, Any]]:
    try:
        import seisbench.models as sbm
    except ImportError as exc:
        raise CatalogError("SeisBench picker requested but seisbench is not installed. Install with: pip install seisbench") from exc

    model_class_name = args.model or ("EQTransformer" if args.picker == "seisbench-eqtransformer" else "PhaseNet")
    if not hasattr(sbm, model_class_name):
        raise CatalogError(f"SeisBench model class not found: {model_class_name}")
    model_class = getattr(sbm, model_class_name)
    pretrained = args.weights or "original"
    model = model_class.from_pretrained(pretrained)
    if args.device:
        model.to(args.device)
    stream = read_waveform(path)
    output = model.classify(stream)
    raw_picks = getattr(output, "picks", output)
    rows: list[dict[str, Any]] = []
    for idx, pick in enumerate(raw_picks, start=1):
        phase = getattr(pick, "phase", None) or getattr(pick, "phase_hint", "")
        label = choose_phase_label(str(phase), requested)
        if label is None:
            continue
        trace_id = getattr(pick, "trace_id", "") or getattr(pick, "id", "")
        parts = infer_trace_parts(trace_id)
        peak_time = getattr(pick, "peak_time", None) or getattr(pick, "time", "")
        score = getattr(pick, "peak_value", None) or getattr(pick, "score", "")
        rows.append(
            {
                "pick_id": str(idx),
                "event_id": "",
                "waveform_path": str(path),
                "trace_id": trace_id,
                "network": parts["network"],
                "station": parts["station"],
                "location": parts["location"],
                "channel": parts["channel"],
                "phase": label,
                "time": datetime_to_text(getattr(peak_time, "datetime", peak_time)),
                "score": score,
                "snr": "",
                "amplitude": "",
                "polarity": "",
                "polarity_quality": "",
                "polarity_score": "",
                "picker": f"seisbench-{model_class_name}:{pretrained}",
            }
        )
    return rows


def cmd_pick(args: argparse.Namespace) -> int:
    requested = {phase.strip().upper() for phase in args.phases.split(",") if phase.strip()}
    if not requested:
        raise CatalogError("At least one phase must be selected with --phases")
    all_picks: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    files = list(iter_waveform_files(args.waveforms, args.extensions))
    for file_index, path in enumerate(files, start=1):
        try:
            if args.picker == "classic":
                picks = classic_pick_file(path, requested, args)
            elif args.picker in {"seisbench-phasenet", "seisbench-eqtransformer"}:
                picks = seisbench_pick_file(path, requested, args)
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
                cache[path] = read_waveform(path)
            trace = select_trace_for_pick(cache[path], row, prefer_vertical=True)
            if trace is None:
                continue
            polarity, quality, score = estimate_first_motion(
                trace,
                row["time"],
                pre_window_s=args.pre_window,
                post_window_s=args.post_window,
                min_score=args.min_score,
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
    return 0


def cmd_associate_gamma(args: argparse.Namespace) -> int:
    try:
        import pandas as pd
        from gamma.utils import association
    except ImportError as exc:
        raise CatalogError("GaMMA association requested but gamma and pandas are not installed.") from exc

    picks = read_csv_rows(args.picks)
    stations = read_stations(args.stations)
    canonical_stations = {sid: sta for sid, sta in stations.items() if sid.count(".") == 2}
    if not canonical_stations:
        canonical_stations = stations
    lon0 = median([sta.longitude for sta in canonical_stations.values()])
    lat0 = median([sta.latitude for sta in canonical_stations.values()])

    station_records = []
    for sta in canonical_stations.values():
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
    pick_ids = []
    for row in picks:
        sid = station_id(row.get("network", ""), row.get("station", ""), row.get("location", ""))
        if sid not in stations:
            sid = station_id(row.get("network", ""), row.get("station", ""))
        if sid not in stations:
            continue
        pick_ids.append(row.get("pick_id", ""))
        pick_records.append(
            {
                "id": sid,
                "station_id": sid,
                "phase_time": parse_datetime(row["time"]).replace(tzinfo=None),
                "phase_type": phase_group(row.get("phase", "")),
                "phase_score": parse_float(row.get("score"), 1.0),
                "phase_amplitude": max(parse_float(row.get("amplitude"), 1.0), 1e-12),
            }
        )

    config = {
        "center": (lon0, lat0),
        "xlim_degree": [args.min_longitude, args.max_longitude] if args.min_longitude is not None else None,
        "ylim_degree": [args.min_latitude, args.max_latitude] if args.min_latitude is not None else None,
        "dims": ["x(km)", "y(km)", "z(km)"],
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
        "oversampling_factor": args.oversampling_factor,
    }
    config = {key: value for key, value in config.items() if value is not None}

    catalogs, assignments = association(
        pd.DataFrame(pick_records),
        pd.DataFrame(station_records),
        config,
        method=args.gamma_method,
    )
    catalogs.to_csv(args.output, index=False)
    if args.assignments:
        assignment_df = pd.DataFrame(assignments)
        if len(assignment_df) and "pick_index" in assignment_df.columns:
            assignment_df["pick_id"] = assignment_df["pick_index"].apply(lambda idx: pick_ids[int(idx)] if int(idx) < len(pick_ids) else "")
        assignment_df.to_csv(args.assignments, index=False)
    eprint(f"gamma_events={len(catalogs)}")
    return 0


def prepare_real_workspace(args: argparse.Namespace) -> dict[str, str]:
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    picks = read_csv_rows(args.picks)
    stations = read_stations(args.stations)
    station_path = workdir / "stations.csv"
    pick_path = workdir / "picks.csv"
    write_csv_rows(station_path, [asdict(sta) for key, sta in stations.items() if key.count(".") == 2], STATION_FIELDS)
    write_csv_rows(pick_path, picks, PICK_FIELDS)
    return {"workdir": str(workdir), "stations": str(station_path), "picks": str(pick_path), "output": str(args.output)}


def cmd_associate_real(args: argparse.Namespace) -> int:
    placeholders = prepare_real_workspace(args)
    placeholders.update({"R": args.real_R, "G": args.real_G, "V": args.real_V, "S": args.real_S})
    if not args.real_command:
        note_path = Path(args.workdir) / "REAL_COMMAND_TEMPLATE.txt"
        note_path.write_text(
            textwrap.dedent(
                f"""\
                REAL workspace prepared.

                stations: {placeholders['stations']}
                picks: {placeholders['picks']}
                output: {placeholders['output']}

                Re-run with --real-command once your local REAL binary and
                input conversion template are available. The command template
                may use placeholders: {{workdir}}, {{stations}}, {{picks}},
                {{output}}, {{R}}, {{G}}, {{V}}, {{S}}.
                """
            ),
            encoding="utf-8",
        )
        eprint(f"REAL workspace prepared at {args.workdir}; no command executed.")
        return 0
    command = args.real_command.format(**placeholders)
    subprocess.run(command, shell=True, cwd=args.workdir, check=True)
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
    run_logged(["git", "clone", url, str(destination)])


def clone_pnsn(destination: Path) -> None:
    if (destination / ".git").exists():
        run_logged(["git", "-C", str(destination), "pull", "--ff-only"])
        return
    if destination.exists() and any(destination.iterdir()):
        raise CatalogError(f"Destination exists and is not an empty git repository: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_logged(["git", "clone", "--no-checkout", "https://github.com/cangyeone/pnsn.git", str(destination)])
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
    tools = ["real", "hash", "pnsn"] if args.tool == "all" else [args.tool]
    root = Path(args.tools_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {}

    if "pnsn" in tools:
        destination = root / "pnsn"
        clone_pnsn(destination)
        manifest["pnsn"] = {"path": str(destination), "status": "cloned"}

    if "real" in tools:
        destination = root / "REAL"
        clone_or_update("https://github.com/Dal-mzhang/REAL.git", destination)
        if args.skip_build:
            manifest["real"] = {"path": str(destination), "status": "cloned"}
        else:
            built_dir = build_first_makefile(destination, args.jobs, preferred=("src",))
            executables = [str(path) for path in destination.rglob("REAL") if path.is_file()]
            manifest["real"] = {"path": str(destination), "build_dir": str(built_dir), "executables": executables}

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
            "stations": [asdict(sta) for key, sta in read_stations(args.stations).items() if key.count(".") == 2],
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
    if not grouped and args.events:
        for row in picks:
            grouped["ev000001"].append(row)
    if not grouped:
        raise CatalogError("Picks must include event_id, or provide associated picks before location.")

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
    return obspy.read_inventory(path)


def station_for_pick(row: dict[str, str], stations: dict[str, Station]) -> Station | None:
    sid = station_id(row.get("network", ""), row.get("station", ""), row.get("location", ""))
    return stations.get(sid) or stations.get(station_id(row.get("network", ""), row.get("station", "")))


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


def cmd_magnitude(args: argparse.Namespace) -> int:
    obspy = import_obspy()
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
        reference_pick = s_picks[0] if s_picks else station_picks[0]
        waveform_path = reference_pick.get("waveform_path", "")
        if not waveform_path:
            continue
        try:
            if waveform_path not in stream_cache:
                stream_cache[waveform_path] = read_waveform(waveform_path)
            stream = stream_cache[waveform_path]
            pick_time = obspy.UTCDateTime(reference_pick["time"])
            start = pick_time + args.window_start
            end = pick_time + args.window_end
            amplitudes = []
            for trace in stream.select(network=sta.network or "*", station=sta.station or "*", location=sta.location or "*"):
                component = getattr(trace.stats, "channel", "")[-1:].upper()
                if component not in {"E", "N", "1", "2"}:
                    continue
                amp = trace_amplitude_mm(trace, start, end, inventory, args.raw_to_mm)
                if math.isfinite(amp) and amp > 0:
                    amplitudes.append(amp)
            if not amplitudes:
                continue
            amplitude_mm = max(amplitudes)
            ml = math.log10(amplitude_mm) + args.a * math.log10(distance) + args.b * distance + args.c
            event_magnitudes[event_id].append(ml)
            station_rows.append(
                {
                    "event_id": event_id,
                    "station_id": sid,
                    "amplitude_mm": f"{amplitude_mm:.6g}",
                    "distance_km": f"{distance:.3f}",
                    "ml": f"{ml:.3f}",
                    "quality": "response_removed" if inventory is not None else "raw_scaled",
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
        station_values = {key: sta for key, sta in stations.items() if key.count(".") == 2}
        sx = [sta.longitude for sta in station_values.values()]
        sy = [sta.latitude for sta in station_values.values()]
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

    pick = sub.add_parser("pick", help="Pick phases from waveform files")
    pick.add_argument("-w", "--waveforms", required=True)
    pick.add_argument("-o", "--output", required=True)
    pick.add_argument("--errors")
    pick.add_argument("--extensions", default="common")
    pick.add_argument("--picker", choices=["classic", "seisbench-phasenet", "seisbench-eqtransformer"], default="classic")
    pick.add_argument("--phases", default="P,S")
    pick.add_argument("--model")
    pick.add_argument("--weights")
    pick.add_argument("--device", default="cpu")
    pick.add_argument("--sta", type=float, default=0.5)
    pick.add_argument("--lta", type=float, default=5.0)
    pick.add_argument("--trigger-on", type=float, default=3.0)
    pick.add_argument("--trigger-off", type=float, default=1.5)
    pick.add_argument("--freqmin", type=float, default=1.0)
    pick.add_argument("--freqmax", type=float, default=20.0)
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
    polarity.set_defaults(func=cmd_polarity)

    associate = sub.add_parser("associate", help="Associate picks with GaMMA, REAL, or a simple time-window fallback")
    associate.add_argument("-p", "--picks", required=True)
    associate.add_argument("-s", "--stations", required=True)
    associate.add_argument("-o", "--output", required=True)
    associate.add_argument("--assignments")
    associate.add_argument("--method", choices=["gamma", "real", "simple"], default="gamma")
    associate.add_argument("--workdir", default="real_work")
    associate.add_argument("--real-command")
    associate.add_argument("--real-R", default="0.5/20/0.05/2/5")
    associate.add_argument("--real-G", default="2.0/20/0.01/1")
    associate.add_argument("--real-V", default="6.2/3.5")
    associate.add_argument("--real-S", default="3/2/4/2/0.5/0.1/1.5")
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
    associate.add_argument("--min-longitude", type=float)
    associate.add_argument("--max-longitude", type=float)
    associate.add_argument("--min-latitude", type=float)
    associate.add_argument("--max-latitude", type=float)
    associate.add_argument("--time-gap", type=float, default=20.0)
    associate.set_defaults(func=cmd_associate)

    build_tools = sub.add_parser("build-tools", help="Clone and locally build optional engines such as REAL and HASH")
    build_tools.add_argument("--tool", choices=["real", "hash", "pnsn", "all"], default="all")
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
    magnitude.add_argument("--inventory", help="StationXML or dataless metadata for response removal")
    magnitude.add_argument("--window-start", type=float, default=-0.5)
    magnitude.add_argument("--window-end", type=float, default=3.0)
    magnitude.add_argument("--raw-to-mm", type=float, default=1.0)
    magnitude.add_argument("--min-distance-km", type=float, default=1.0)
    magnitude.add_argument("--a", type=float, default=1.11)
    magnitude.add_argument("--b", type=float, default=0.00189)
    magnitude.add_argument("--c", type=float, default=-2.09)
    magnitude.set_defaults(func=cmd_magnitude)

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
