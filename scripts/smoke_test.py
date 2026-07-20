#!/usr/bin/env python3
"""Smoke tests for the SeismicX Catalog helper CLI.

These tests use small synthetic CSV and miniSEED inputs so contributors can
verify the CLI contracts without shipping waveform examples in the repository.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "seismicx_catalog.py"
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
STATION_ROWS = [
    ("A", 100.00, 30.10),
    ("B", 100.10, 30.00),
    ("C", 100.00, 29.90),
    ("D", 99.90, 30.00),
    ("E", 100.06, 30.06),
]


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def write_stations(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["station_id", "network", "station", "location", "longitude", "latitude", "elevation_m"])
        for sta, lon, lat in STATION_ROWS:
            writer.writerow([f"XX.{sta}", "XX", sta, "", lon, lat, 0])


def write_velocity(path: Path) -> None:
    path.write_text("depth_km,vp_km_s,vs_km_s\n0,6.0,3.5\n10,6.2,3.6\n", encoding="utf-8")


def pick_row(pick_id: str, sta: str, phase: str, time: dt.datetime, missing: bool = False) -> dict[str, Any]:
    station = "MISSING" if missing else sta
    return {
        "pick_id": pick_id,
        "event_id": "",
        "waveform_path": "",
        "trace_id": f"XX.{station}..3C",
        "network": "XX",
        "station": station,
        "location": "",
        "channel": "3C",
        "phase": phase,
        "time": time.isoformat().replace("+00:00", "Z"),
        "score": "1",
        "snr": "10",
        "amplitude": "100",
        "polarity": "U" if phase.upper().startswith("P") else "",
        "polarity_quality": "I" if phase.upper().startswith("P") else "",
        "polarity_score": "5",
        "picker": "synthetic",
    }


def write_synthetic_picks(path: Path, include_missing_first: bool = False) -> None:
    origin = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    rows: list[dict[str, Any]] = []
    if include_missing_first:
        rows.append(pick_row("p99999999", "MISSING", "P", origin, missing=True))
    index = 0
    for sta, lon, lat in STATION_ROWS:
        dx = (lon - 100.0) * 111.32 * math.cos(math.radians(30.0))
        dy = (lat - 30.0) * 110.57
        dist = math.sqrt(dx * dx + dy * dy + 5.0 * 5.0)
        for phase, velocity in (("P", 6.0), ("S", 3.5)):
            index += 1
            pick_time = origin + dt.timedelta(seconds=dist / velocity)
            rows.append(pick_row(f"p{index:08d}", sta, phase, pick_time))

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PICK_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_gamma_chain(tmp: Path) -> None:
    stations = tmp / "stations.csv"
    velocity = tmp / "velocity.csv"
    picks = tmp / "picks.csv"
    write_stations(stations)
    write_velocity(velocity)
    write_synthetic_picks(picks)

    run(
        [
            "associate",
            "--method",
            "gamma",
            "-p",
            str(picks),
            "-s",
            str(stations),
            "-o",
            str(tmp / "events_gamma.csv"),
            "--assignments",
            str(tmp / "assignments.csv"),
            "--associated-picks",
            str(tmp / "picks_assoc.csv"),
            "--min-picks-per-eq",
            "5",
            "--min-p-picks-per-eq",
            "3",
            "--min-s-picks-per-eq",
            "0",
            "--max-depth",
            "8",
        ]
    )
    events = list(csv.DictReader((tmp / "events_gamma.csv").open(encoding="utf-8")))
    assignments = list(csv.DictReader((tmp / "assignments.csv").open(encoding="utf-8")))
    assert len(events) == 1, events
    assert events[0]["event_id"] == "ev000001"
    assert 0.0 <= float(events[0]["depth_km"]) <= 8.0, events[0]
    assert len(assignments) == 10, assignments

    run(["locate", "-p", str(tmp / "picks_assoc.csv"), "-s", str(stations), "-v", str(velocity), "-o", str(tmp / "events_located.csv"), "--min-picks", "5"])
    run(["analyze", "-e", str(tmp / "events_located.csv"), "-o", str(tmp / "activity.json")])
    activity = json.loads((tmp / "activity.json").read_text(encoding="utf-8"))
    assert activity["event_count"] == 1, activity


def test_gamma_assignment_mapping(tmp: Path) -> None:
    stations = tmp / "stations_mismatch.csv"
    picks = tmp / "picks_mismatch.csv"
    write_stations(stations)
    write_synthetic_picks(picks, include_missing_first=True)
    run(
        [
            "associate",
            "--method",
            "gamma",
            "-p",
            str(picks),
            "-s",
            str(stations),
            "-o",
            str(tmp / "events_mismatch.csv"),
            "--assignments",
            str(tmp / "assignments_mismatch.csv"),
            "--associated-picks",
            str(tmp / "picks_mismatch_assoc.csv"),
            "--min-picks-per-eq",
            "5",
            "--min-p-picks-per-eq",
            "3",
            "--min-s-picks-per-eq",
            "0",
        ]
    )
    rows = list(csv.DictReader((tmp / "picks_mismatch_assoc.csv").open(encoding="utf-8")))
    assert rows[0]["station"] == "MISSING"
    assert rows[0]["event_id"] == ""
    assert rows[1]["event_id"] == "ev000001"


def test_real_chain(tmp: Path) -> None:
    stations = tmp / "stations_real.csv"
    picks = tmp / "picks_real.csv"
    events_path = tmp / "events_real.csv"
    assignments_path = tmp / "assignments_real.csv"
    associated_path = tmp / "picks_real_assoc.csv"
    write_stations(stations)
    write_synthetic_picks(picks)

    completed = run(
        [
            "associate",
            "--method",
            "real",
            "-p",
            str(picks),
            "-s",
            str(stations),
            "-o",
            str(events_path),
            "--assignments",
            str(assignments_path),
            "--associated-picks",
            str(associated_path),
            "--workdir",
            str(tmp / "real_work"),
            "--real-R",
            "0.2/10/0.05/2/3/360/1/30/100",
            "--real-S",
            "3/2/6/2/0.8/0.1/1.5",
            "--real-V",
            "6.0/3.5/5.0/2.8",
            "--real-jobs",
            "2",
        ]
    )
    events = list(csv.DictReader(events_path.open(encoding="utf-8")))
    assignments = list(csv.DictReader(assignments_path.open(encoding="utf-8")))
    associated = list(csv.DictReader(associated_path.open(encoding="utf-8")))
    assert len(events) == 1, (events, completed.stderr)
    assert events[0]["event_id"] == "ev000001"
    assert abs(float(events[0]["latitude"]) - 30.0) <= 0.051, events[0]
    assert abs(float(events[0]["longitude"]) - 100.0) <= 0.060, events[0]
    assert abs((dt.datetime.fromisoformat(events[0]["origin_time"].replace("Z", "+00:00")) - dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)).total_seconds()) <= 0.25
    assert len(assignments) == 10, assignments
    assert {row["event_id"] for row in associated} == {"ev000001"}
    assert (tmp / "real_work" / "real_run.json").exists()

    invalid = run(
        [
            "associate", "--method", "real", "-p", str(picks), "-s", str(stations),
            "-o", str(tmp / "events_real_invalid.csv"),
            "--real-R", "0.2/10/0.05/2/3/360/1/30",
            "--real-S", "3/2/6/2/0.8/0.1/1.5",
            "--real-V", "6.0/3.5",
        ],
        check=False,
    )
    assert invalid.returncode == 2
    assert "requires both latitude and longitude" in invalid.stderr


def test_fail_fast_and_bayes_export(tmp: Path) -> None:
    stations = tmp / "stations_failfast.csv"
    velocity = tmp / "velocity_failfast.csv"
    picks = tmp / "picks_failfast.csv"
    events = tmp / "events_failfast.csv"
    write_stations(stations)
    write_velocity(velocity)
    with picks.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PICK_FIELDS)
        writer.writeheader()
        writer.writerow(pick_row("p1", "A", "P", dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)))
    events.write_text(
        "event_id,origin_time,latitude,longitude,depth_km,magnitude,magnitude_type,rms,n_picks,method\n"
        "ev000001,2024-01-01T00:00:00Z,30,100,5,,,,1,test\n",
        encoding="utf-8",
    )

    locate = run(["locate", "-p", str(picks), "-s", str(stations), "-v", str(velocity), "-o", str(tmp / "located.csv"), "--events", str(events)], check=False)
    assert locate.returncode == 2
    assert "Picks must include event_id" in locate.stderr

    catalog_simple = run(["catalog", "-w", str(tmp), "-s", str(stations), "-v", str(velocity), "-o", str(tmp / "catalog_simple"), "--association-method", "simple", "--picker", "classic"], check=False)
    assert catalog_simple.returncode == 2
    assert "--smoke-test-simple" in catalog_simple.stderr

    catalog_bayes = run(["catalog", "-w", str(tmp), "-s", str(stations), "-v", str(velocity), "-o", str(tmp / "catalog_bayes"), "--location-method", "bayes", "--picker", "classic"], check=False)
    assert catalog_bayes.returncode == 2
    assert "requires --bayes-command" in catalog_bayes.stderr

    bayes_json = tmp / "bayes_input.json"
    run(["locate", "--method", "bayes", "-p", str(picks), "-s", str(stations), "-v", str(velocity), "-o", str(bayes_json)])
    payload = json.loads(bayes_json.read_text(encoding="utf-8"))
    assert payload["stations"], payload


def test_pnsn_waveform_path_keeps_all_sources(tmp: Path) -> None:
    import numpy as np
    import torch
    from obspy import Stream, Trace, UTCDateTime

    class DummyPicker(torch.nn.Module):
        def forward(self, data: torch.Tensor) -> torch.Tensor:
            return data.new_tensor([[0.0, 20.0, 0.9]])

    model = torch.jit.trace(DummyPicker(), torch.zeros((100, 3), dtype=torch.float32))
    model_path = tmp / "dummy_picker.jit"
    model.save(str(model_path))

    start = UTCDateTime(2024, 1, 1)
    for index, channel in enumerate(["BHE", "BHN", "BHZ", "HHZ"], start=1):
        trace = Trace(data=(np.random.randn(200).astype(np.float32) * 0.001))
        trace.stats.network = "XX"
        trace.stats.station = "AAA"
        trace.stats.location = ""
        trace.stats.channel = channel
        trace.stats.starttime = start
        trace.stats.sampling_rate = 100.0
        path = tmp / f"waveform_{index}_{channel}.mseed"
        Stream([trace]).write(str(path), format="MSEED")

    output = tmp / "pnsn_picks.csv"
    run(["pick", "-w", str(tmp), "-o", str(output), "--picker", "torchscript-pnsn", "--model", str(model_path), "--phases", "Pg"])
    rows = list(csv.DictReader(output.open(encoding="utf-8")))
    assert rows, output.read_text(encoding="utf-8")
    paths = rows[0]["waveform_path"].split(";")
    assert len(paths) == 4, rows[0]["waveform_path"]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="seismicx_smoke_") as tmp_dir:
        tmp = Path(tmp_dir)
        run(["list-models", "--json"])
        test_gamma_chain(tmp)
        test_gamma_assignment_mapping(tmp)
        test_real_chain(tmp)
        test_fail_fast_and_bayes_export(tmp)
        test_pnsn_waveform_path_keeps_all_sources(tmp)
        print(f"smoke tests passed in {tmp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
