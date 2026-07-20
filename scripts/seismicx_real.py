#!/usr/bin/env python3
"""Optimized homogeneous-velocity REAL associator for SeismicX Catalog.

The implementation follows the REAL grid-search workflow described by Zhang,
Ellsworth, and Beroza (2019).  It is designed as a small, importable backend:
the public SeismicX CLI handles CSV conversion while this module owns only the
association math and pick assignments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import math
import time
from typing import Sequence

import numpy as np

try:
    from numba import njit, prange, set_num_threads

    NUMBA_AVAILABLE = True
except Exception:  # pragma: no cover - exercised on minimal installations
    NUMBA_AVAILABLE = False

    def njit(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return lambda function: function

    def prange(stop: int) -> range:
        return range(stop)

    def set_num_threads(count: int) -> None:
        del count


DEG2KM = 111.19
INF = 1.0e30


class RealError(ValueError):
    """Raised when REAL inputs or parameters are invalid."""


@dataclass(frozen=True)
class RealStation:
    network: str
    station: str
    location: str
    longitude: float
    latitude: float
    elevation_km: float = 0.0

    @property
    def station_id(self) -> str:
        return ".".join((self.network, self.station, self.location)).rstrip(".")


@dataclass(frozen=True)
class RealPick:
    original_index: int
    pick_id: str
    station_index: int
    phase: str
    time: float
    score: float = 1.0
    amplitude: float = 0.0


@dataclass(frozen=True)
class RealAssignment:
    original_index: int
    pick_id: str
    station_index: int
    phase: str
    time: float
    residual_s: float
    score: float
    amplitude: float
    azimuth_deg: float


@dataclass
class RealEvent:
    origin_time: float
    latitude: float
    longitude: float
    depth_km: float
    rms_s: float
    azimuth_gap_deg: float
    n_p: int
    n_s: int
    n_both: int
    assignments: list[RealAssignment] = field(default_factory=list)

    @property
    def n_picks(self) -> int:
        return self.n_p + self.n_s


@dataclass(frozen=True)
class RealConfig:
    search_radius_deg: float = 0.5
    max_depth_km: float = 20.0
    grid_spacing_deg: float = 0.05
    depth_spacing_km: float = 2.0
    event_window_s: float = 5.0
    max_azimuth_gap_deg: float = 360.0
    max_distance_deg: float = 180.0
    reference_latitude: float | None = None
    reference_longitude: float | None = None
    min_p: int = 3
    min_s: int = 2
    min_total: int = 5
    min_both: int = 2
    max_origin_std_s: float = 0.5
    min_ps_separation_s: float = 0.1
    window_multiplier: float = 1.5
    residual_multiplier: float = 5.0
    vp_km_s: float = 6.2
    vs_km_s: float = 3.5
    surface_vp_km_s: float = 1.0e6
    surface_vs_km_s: float = 1.0e6
    use_station_elevation: bool = False
    min_score: float = 0.0
    jobs: int | None = None

    @classmethod
    def from_real_strings(
        cls,
        r_value: str,
        s_value: str,
        v_value: str,
        *,
        min_score: float = 0.0,
        jobs: int | None = None,
    ) -> "RealConfig":
        r = _parse_numbers("R", r_value, 5, 9)
        s = _parse_numbers("S", s_value, 7, 9)
        v = _parse_numbers("V", v_value, 2, 5)
        if len(r) == 8:
            raise RealError("REAL -R fixed reference requires both latitude and longitude.")
        if len(v) not in {2, 4, 5}:
            raise RealError("REAL -V expects vp/vs or vp/vs/surface_vp/surface_vs[/elevation_flag].")
        if len(s) == 9 and int(s[8]) != 0:
            raise RealError("The bundled Python REAL backend does not support the ires resolution-file flag.")
        return cls(
            search_radius_deg=r[0],
            max_depth_km=r[1],
            grid_spacing_deg=r[2],
            depth_spacing_km=r[3],
            event_window_s=r[4],
            max_azimuth_gap_deg=r[5] if len(r) > 5 else 360.0,
            max_distance_deg=r[6] if len(r) > 6 else 180.0,
            reference_latitude=r[7] if len(r) > 8 else None,
            reference_longitude=r[8] if len(r) > 8 else None,
            min_p=int(s[0]),
            min_s=int(s[1]),
            min_total=int(s[2]),
            min_both=int(s[3]),
            max_origin_std_s=s[4],
            min_ps_separation_s=s[5],
            window_multiplier=s[6],
            residual_multiplier=s[7] if len(s) > 7 else 5.0,
            vp_km_s=v[0],
            vs_km_s=v[1],
            surface_vp_km_s=v[2] if len(v) > 2 else 1.0e6,
            surface_vs_km_s=v[3] if len(v) > 3 else 1.0e6,
            use_station_elevation=bool(int(v[4])) if len(v) > 4 else False,
            min_score=min_score,
            jobs=jobs,
        ).validated()

    def validated(self) -> "RealConfig":
        positive = {
            "search radius": self.search_radius_deg,
            "maximum depth": self.max_depth_km,
            "grid spacing": self.grid_spacing_deg,
            "depth spacing": self.depth_spacing_km,
            "event window": self.event_window_s,
            "Vp": self.vp_km_s,
            "Vs": self.vs_km_s,
            "window multiplier": self.window_multiplier,
            "maximum origin scatter": self.max_origin_std_s,
            "maximum distance": self.max_distance_deg,
            "residual multiplier": self.residual_multiplier,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise RealError(f"REAL parameters must be positive: {', '.join(invalid)}")
        if self.vp_km_s <= self.vs_km_s:
            raise RealError("REAL requires Vp greater than Vs.")
        if self.use_station_elevation and (self.surface_vp_km_s <= 0 or self.surface_vs_km_s <= 0):
            raise RealError("REAL near-surface velocities must be positive when elevation correction is enabled.")
        if min(self.min_p, self.min_s, self.min_total, self.min_both) < 0:
            raise RealError("REAL pick-count thresholds cannot be negative.")
        if self.min_total < max(self.min_p, self.min_s):
            raise RealError("REAL min_total cannot be smaller than min_p or min_s.")
        if (self.reference_latitude is None) != (self.reference_longitude is None):
            raise RealError("REAL fixed reference latitude and longitude must be supplied together.")
        return self


@dataclass(frozen=True)
class RealRunStats:
    input_picks: int
    usable_picks: int
    deduplicated_picks: int
    seed_count: int
    evaluated_seeds: int
    candidate_count: int
    event_count: int
    elapsed_s: float
    numba_enabled: bool


@dataclass(frozen=True)
class RealRunResult:
    events: list[RealEvent]
    stats: RealRunStats


def _parse_numbers(name: str, value: str, minimum: int, maximum: int) -> list[float]:
    try:
        numbers = [float(item) for item in value.split("/") if item != ""]
    except ValueError as exc:
        raise RealError(f"Invalid REAL -{name} value: {value!r}") from exc
    if not minimum <= len(numbers) <= maximum:
        raise RealError(f"REAL -{name} expects {minimum} to {maximum} slash-separated values.")
    return numbers


def _azimuth_gap(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 360.0
    ordered = sorted(value % 360.0 for value in values)
    gaps = [ordered[index + 1] - ordered[index] for index in range(len(ordered) - 1)]
    gaps.append(360.0 + ordered[0] - ordered[-1])
    return max(gaps)


def _deduplicate(picks: Sequence[RealPick], tolerance_s: float) -> list[RealPick]:
    if not picks:
        return []
    ordered = sorted(picks, key=lambda pick: (pick.time, -pick.score, pick.original_index))
    groups: list[list[RealPick]] = [[ordered[0]]]
    for pick in ordered[1:]:
        if pick.time - groups[-1][-1].time < tolerance_s:
            groups[-1].append(pick)
        else:
            groups.append([pick])
    return [max(group, key=lambda pick: (pick.score, -pick.original_index)) for group in groups]


@njit(cache=True, parallel=True)
def _evaluate_grid(
    gx: np.ndarray,
    gy: np.ndarray,
    gz: np.ndarray,
    sx: np.ndarray,
    sy: np.ndarray,
    elevation: np.ndarray,
    p_times: np.ndarray,
    s_times: np.ndarray,
    p_consumed: np.ndarray,
    s_consumed: np.ndarray,
    p_start: np.ndarray,
    p_end: np.ndarray,
    s_start: np.ndarray,
    s_end: np.ndarray,
    seed_time: float,
    seed_station: int,
    vp: float,
    vs: float,
    surface_vp: float,
    surface_vs: float,
    p_half_window: float,
    s_half_window: float,
    min_ps_separation: float,
    max_distance_km: float,
    min_p: int,
    min_s: int,
    min_total: int,
    min_both: int,
) -> np.ndarray:
    node_count = gx.shape[0]
    station_count = sx.shape[0]
    result = np.empty((node_count, 7), dtype=np.float64)
    max_distance_sq = max_distance_km * max_distance_km

    for node in prange(node_count):
        result[node, 0] = -INF
        result[node, 1] = INF
        result[node, 2] = 0.0
        result[node, 3] = 0.0
        result[node, 4] = 0.0
        result[node, 5] = 0.0
        result[node, 6] = INF

        depth = gz[node]
        ref_dx = gx[node] - sx[seed_station]
        ref_dy = gy[node] - sy[seed_station]
        ref_distance = math.sqrt(ref_dx * ref_dx + ref_dy * ref_dy + depth * depth)
        ref_tt = ref_distance / vp + elevation[seed_station] / surface_vp
        seed_origin = seed_time - ref_tt

        origins = np.empty(2 * station_count, dtype=np.float64)
        azimuths = np.empty(2 * station_count, dtype=np.float64)
        total = 0
        p_count = 0
        s_count = 0
        both_count = 0

        for station in range(station_count):
            dx = sx[station] - gx[node]
            dy = sy[station] - gy[node]
            horizontal_sq = dx * dx + dy * dy
            if horizontal_sq > max_distance_sq:
                continue
            distance = math.sqrt(horizontal_sq + depth * depth)
            p_tt = distance / vp + elevation[station] / surface_vp
            s_tt = distance / vs + elevation[station] / surface_vs
            p_prediction = seed_origin + p_tt
            s_prediction = seed_origin + s_tt
            azimuth = math.degrees(math.atan2(dx, dy))
            if azimuth < 0.0:
                azimuth += 360.0

            p_found = False
            p_selected_time = -INF
            best_p_residual = INF
            for pick_index in range(p_start[station], p_end[station]):
                if p_consumed[station, pick_index]:
                    continue
                pick_time = p_times[station, pick_index]
                residual = abs(pick_time - p_prediction)
                if residual <= p_half_window and residual < best_p_residual:
                    best_p_residual = residual
                    p_selected_time = pick_time
                    p_found = True
            if p_found:
                origins[total] = p_selected_time - p_tt
                azimuths[total] = azimuth
                total += 1
                p_count += 1

            if s_prediction - p_prediction > min_ps_separation:
                s_found = False
                s_selected_time = -INF
                best_s_residual = INF
                for pick_index in range(s_start[station], s_end[station]):
                    if s_consumed[station, pick_index]:
                        continue
                    pick_time = s_times[station, pick_index]
                    residual = abs(pick_time - s_prediction)
                    if residual <= s_half_window and residual < best_s_residual:
                        if not p_found or abs(p_selected_time - pick_time) > min_ps_separation:
                            best_s_residual = residual
                            s_selected_time = pick_time
                            s_found = True
                if s_found:
                    origins[total] = s_selected_time - s_tt
                    azimuths[total] = azimuth
                    total += 1
                    s_count += 1
                    if p_found:
                        both_count += 1

        result[node, 2] = p_count
        result[node, 3] = s_count
        result[node, 4] = total
        result[node, 5] = both_count
        if p_count < min_p or s_count < min_s or total < min_total or both_count < min_both:
            continue

        ordered_origins = np.sort(origins[:total])
        if total % 2:
            origin = ordered_origins[total // 2]
        else:
            origin = 0.5 * (ordered_origins[total // 2 - 1] + ordered_origins[total // 2])
        variance = 0.0
        for index in range(total):
            difference = origins[index] - origin
            variance += difference * difference
        origin_std = math.sqrt(variance / max(total - 1, 1))

        ordered_azimuths = np.sort(azimuths[:total])
        gap = 360.0 + ordered_azimuths[0] - ordered_azimuths[total - 1]
        for index in range(total - 1):
            current = ordered_azimuths[index + 1] - ordered_azimuths[index]
            if current > gap:
                gap = current

        result[node, 0] = origin
        result[node, 1] = origin_std
        result[node, 6] = gap

    return result


def _grid_geometry(
    stations: Sequence[RealStation],
    center_latitude: float,
    center_longitude: float,
    config: RealConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    cosine = max(abs(math.cos(math.radians(center_latitude))), 1.0e-6)
    longitude_radius = config.search_radius_deg / cosine
    longitude_step = config.grid_spacing_deg / cosine
    latitudes = np.arange(
        center_latitude - config.search_radius_deg,
        center_latitude + config.search_radius_deg + config.grid_spacing_deg * 0.25,
        config.grid_spacing_deg,
        dtype=np.float64,
    )
    longitudes = np.arange(
        center_longitude - longitude_radius,
        center_longitude + longitude_radius + longitude_step * 0.25,
        longitude_step,
        dtype=np.float64,
    )
    depths = np.arange(
        0.0,
        config.max_depth_km + config.depth_spacing_km * 0.25,
        config.depth_spacing_km,
        dtype=np.float64,
    )
    grid_latitude, grid_longitude, grid_depth = np.meshgrid(latitudes, longitudes, depths, indexing="ij")
    gx = (grid_longitude.ravel() - center_longitude) * cosine * DEG2KM
    gy = (grid_latitude.ravel() - center_latitude) * DEG2KM
    gz = grid_depth.ravel()
    sx = np.asarray([(station.longitude - center_longitude) * cosine * DEG2KM for station in stations], dtype=np.float64)
    sy = np.asarray([(station.latitude - center_latitude) * DEG2KM for station in stations], dtype=np.float64)
    return gx, gy, gz, grid_latitude.ravel(), grid_longitude.ravel(), sx, sy


def _nearest_pick(
    times: np.ndarray,
    scores: np.ndarray,
    source_indices: np.ndarray,
    consumed: np.ndarray,
    prediction: float,
    half_window: float,
    start: int,
    stop: int,
) -> int | None:
    best: int | None = None
    best_key = (float("inf"), float("inf"))
    for column in range(start, stop):
        if consumed[column] or source_indices[column] < 0:
            continue
        residual = abs(float(times[column]) - prediction)
        key = (residual, -float(scores[column]))
        if residual <= half_window and key < best_key:
            best = column
            best_key = key
    return best


def _extract_assignments(
    origin: float,
    latitude: float,
    longitude: float,
    depth: float,
    stations: Sequence[RealStation],
    p_times: np.ndarray,
    s_times: np.ndarray,
    p_scores: np.ndarray,
    s_scores: np.ndarray,
    p_amplitudes: np.ndarray,
    s_amplitudes: np.ndarray,
    p_source_indices: np.ndarray,
    s_source_indices: np.ndarray,
    pick_by_index: dict[int, RealPick],
    p_consumed: np.ndarray,
    s_consumed: np.ndarray,
    p_start: np.ndarray,
    p_end: np.ndarray,
    s_start: np.ndarray,
    s_end: np.ndarray,
    p_half_window: float,
    s_half_window: float,
    config: RealConfig,
) -> list[tuple[RealAssignment, int, int]]:
    assignments: list[tuple[RealAssignment, int, int]] = []
    cosine = math.cos(math.radians(latitude))
    residual_limit = max(config.residual_multiplier * config.max_origin_std_s, p_half_window, s_half_window)
    max_distance_km = config.max_distance_deg * DEG2KM

    for station_index, station in enumerate(stations):
        dx = (station.longitude - longitude) * cosine * DEG2KM
        dy = (station.latitude - latitude) * DEG2KM
        horizontal = math.hypot(dx, dy)
        if horizontal > max_distance_km:
            continue
        distance = math.sqrt(horizontal * horizontal + depth * depth)
        elevation = station.elevation_km if config.use_station_elevation else 0.0
        p_tt = distance / config.vp_km_s + elevation / config.surface_vp_km_s
        s_tt = distance / config.vs_km_s + elevation / config.surface_vs_km_s
        p_prediction = origin + p_tt
        s_prediction = origin + s_tt
        azimuth = math.degrees(math.atan2(dx, dy)) % 360.0

        p_column = _nearest_pick(
            p_times[station_index], p_scores[station_index], p_source_indices[station_index],
            p_consumed[station_index], p_prediction, p_half_window,
            int(p_start[station_index]), int(p_end[station_index]),
        )
        p_time: float | None = None
        if p_column is not None:
            p_time = float(p_times[station_index, p_column])
            residual = p_time - p_prediction
            if abs(residual) <= residual_limit:
                source_index = int(p_source_indices[station_index, p_column])
                pick = pick_by_index[source_index]
                assignments.append((
                    RealAssignment(source_index, pick.pick_id, station_index, "P", pick.time, residual,
                                   pick.score, pick.amplitude, azimuth),
                    station_index,
                    p_column,
                ))

        s_column = _nearest_pick(
            s_times[station_index], s_scores[station_index], s_source_indices[station_index],
            s_consumed[station_index], s_prediction, s_half_window,
            int(s_start[station_index]), int(s_end[station_index]),
        )
        if s_column is not None and s_prediction - p_prediction > config.min_ps_separation_s:
            s_time = float(s_times[station_index, s_column])
            residual = s_time - s_prediction
            if (p_time is None or abs(p_time - s_time) > config.min_ps_separation_s) and abs(residual) <= residual_limit:
                source_index = int(s_source_indices[station_index, s_column])
                pick = pick_by_index[source_index]
                assignments.append((
                    RealAssignment(source_index, pick.pick_id, station_index, "S", pick.time, residual,
                                   pick.score, pick.amplitude, azimuth),
                    station_index,
                    s_column,
                ))
    return assignments


def associate_real(
    stations: Sequence[RealStation],
    picks: Sequence[RealPick],
    config: RealConfig,
) -> RealRunResult:
    """Associate P/S picks and return located candidate events with assignments."""
    started = time.perf_counter()
    config = config.validated()
    if not stations:
        raise RealError("REAL requires at least one station.")
    if config.jobs:
        set_num_threads(max(1, config.jobs))

    station_count = len(stations)
    grouped: dict[tuple[int, str], list[RealPick]] = {}
    usable = 0
    for pick in picks:
        phase = pick.phase.upper()[:1]
        if pick.station_index < 0 or pick.station_index >= station_count or phase not in {"P", "S"}:
            continue
        if not math.isfinite(pick.time) or not math.isfinite(pick.score) or pick.score < config.min_score:
            continue
        grouped.setdefault((pick.station_index, phase), []).append(pick)
        usable += 1

    horizontal_step_km = config.grid_spacing_deg * DEG2KM
    p_window = math.sqrt(2.0 * horizontal_step_km**2 + config.depth_spacing_km**2) / config.vp_km_s
    s_window = math.sqrt(2.0 * horizontal_step_km**2 + config.depth_spacing_km**2) / config.vs_km_s
    p_half_window = config.window_multiplier * p_window * 0.5
    s_half_window = config.window_multiplier * s_window * 0.5
    event_window = max(config.event_window_s, config.window_multiplier * s_window)

    deduplicated: dict[tuple[int, str], list[RealPick]] = {}
    for key, values in grouped.items():
        tolerance = config.window_multiplier * (p_window if key[1] == "P" else s_window)
        deduplicated[key] = _deduplicate(values, tolerance)
    deduplicated_count = sum(len(values) for values in deduplicated.values())
    if not any(key[1] == "P" and values for key, values in deduplicated.items()):
        raise RealError("REAL requires at least one usable P pick.")

    max_columns = max((len(values) for values in deduplicated.values()), default=0)
    shape = (station_count, max_columns)
    p_times = np.full(shape, INF, dtype=np.float64)
    s_times = np.full(shape, INF, dtype=np.float64)
    p_scores = np.zeros(shape, dtype=np.float64)
    s_scores = np.zeros(shape, dtype=np.float64)
    p_amplitudes = np.zeros(shape, dtype=np.float64)
    s_amplitudes = np.zeros(shape, dtype=np.float64)
    p_source_indices = np.full(shape, -1, dtype=np.int64)
    s_source_indices = np.full(shape, -1, dtype=np.int64)
    pick_by_index = {pick.original_index: pick for pick in picks}

    for station_index in range(station_count):
        for phase, times, scores, amplitudes, source_indices in (
            ("P", p_times, p_scores, p_amplitudes, p_source_indices),
            ("S", s_times, s_scores, s_amplitudes, s_source_indices),
        ):
            values = sorted(deduplicated.get((station_index, phase), []), key=lambda pick: pick.time)
            for column, pick in enumerate(values):
                times[station_index, column] = pick.time
                scores[station_index, column] = pick.score
                amplitudes[station_index, column] = pick.amplitude
                source_indices[station_index, column] = pick.original_index

    p_consumed = np.zeros(shape, dtype=np.bool_)
    s_consumed = np.zeros(shape, dtype=np.bool_)
    elevation = np.asarray(
        [station.elevation_km if config.use_station_elevation else 0.0 for station in stations],
        dtype=np.float64,
    )
    latitudes = [station.latitude for station in stations]
    longitudes = [station.longitude for station in stations]
    network_span_km = math.hypot(
        (max(latitudes) - min(latitudes)) * DEG2KM,
        (max(longitudes) - min(longitudes)) * DEG2KM * math.cos(math.radians(float(np.median(latitudes)))),
    )
    broad_half_window = (
        network_span_km + 2.0 * config.search_radius_deg * DEG2KM + config.max_depth_km
    ) / config.vs_km_s + max(p_half_window, s_half_window)

    seeds = sorted(
        (float(p_times[station, column]), station, column)
        for station in range(station_count)
        for column in range(max_columns)
        if p_source_indices[station, column] >= 0
    )

    @lru_cache(maxsize=16)
    def geometry(seed_station: int) -> tuple[np.ndarray, ...]:
        if config.reference_latitude is None:
            center_latitude = stations[seed_station].latitude
            center_longitude = stations[seed_station].longitude
        else:
            center_latitude = config.reference_latitude
            center_longitude = config.reference_longitude
        return _grid_geometry(stations, center_latitude, center_longitude, config)

    candidates: list[RealEvent] = []
    evaluated_seeds = 0
    for seed_time, seed_station, seed_column in seeds:
        if p_consumed[seed_station, seed_column]:
            continue
        evaluated_seeds += 1
        lower = seed_time - broad_half_window
        upper = seed_time + broad_half_window
        p_start = np.empty(station_count, dtype=np.int64)
        p_end = np.empty(station_count, dtype=np.int64)
        s_start = np.empty(station_count, dtype=np.int64)
        s_end = np.empty(station_count, dtype=np.int64)
        for station in range(station_count):
            p_start[station] = np.searchsorted(p_times[station], lower, side="left")
            p_end[station] = np.searchsorted(p_times[station], upper, side="right")
            s_start[station] = np.searchsorted(s_times[station], lower, side="left")
            s_end[station] = np.searchsorted(s_times[station], upper, side="right")

        gx, gy, gz, grid_latitudes, grid_longitudes, sx, sy = geometry(seed_station)
        scores = _evaluate_grid(
            gx, gy, gz, sx, sy, elevation, p_times, s_times, p_consumed, s_consumed,
            p_start, p_end, s_start, s_end, seed_time, seed_station,
            config.vp_km_s, config.vs_km_s, config.surface_vp_km_s, config.surface_vs_km_s,
            p_half_window, s_half_window, config.min_ps_separation_s,
            config.max_distance_deg * DEG2KM,
            config.min_p, config.min_s, config.min_total, config.min_both,
        )
        valid = np.flatnonzero(
            (scores[:, 0] > -INF * 0.5)
            & (scores[:, 1] <= config.max_origin_std_s)
            & (scores[:, 6] <= config.max_azimuth_gap_deg)
        )
        if not len(valid):
            p_consumed[seed_station, seed_column] = True
            continue
        best_node = min(
            valid.tolist(),
            key=lambda node: (
                -scores[node, 4], scores[node, 1], -scores[node, 5],
                scores[node, 6], gz[node], node,
            ),
        )
        origin = float(scores[best_node, 0])
        latitude = float(grid_latitudes[best_node])
        longitude = float(grid_longitudes[best_node])
        depth = float(gz[best_node])
        selected = _extract_assignments(
            origin, latitude, longitude, depth, stations,
            p_times, s_times, p_scores, s_scores, p_amplitudes, s_amplitudes,
            p_source_indices, s_source_indices, pick_by_index, p_consumed, s_consumed,
            p_start, p_end, s_start, s_end, p_half_window, s_half_window, config,
        )
        assignments = [item[0] for item in selected]
        n_p = sum(item.phase == "P" for item in assignments)
        n_s = len(assignments) - n_p
        p_stations = {item.station_index for item in assignments if item.phase == "P"}
        s_stations = {item.station_index for item in assignments if item.phase == "S"}
        n_both = len(p_stations & s_stations)
        gap = _azimuth_gap([item.azimuth_deg for item in assignments])
        residuals = [item.residual_s for item in assignments]
        if residuals:
            residual_median = float(np.median(np.asarray(residuals, dtype=np.float64)))
            rms_s = math.sqrt(
                sum((value - residual_median) ** 2 for value in residuals) / max(len(residuals) - 1, 1)
            )
        else:
            rms_s = INF
        if (
            n_p < config.min_p or n_s < config.min_s or len(assignments) < config.min_total
            or n_both < config.min_both or rms_s > config.max_origin_std_s
            or gap > config.max_azimuth_gap_deg
        ):
            p_consumed[seed_station, seed_column] = True
            continue

        candidates.append(RealEvent(origin, latitude, longitude, depth, rms_s, gap, n_p, n_s, n_both, assignments))
        for assignment, station, column in selected:
            if assignment.phase == "P":
                p_consumed[station, column] = True
            else:
                s_consumed[station, column] = True

    ranked = sorted(
        candidates,
        key=lambda event: (-event.n_picks, event.rms_s, -event.n_both, event.azimuth_gap_deg, event.origin_time),
    )
    selected_events: list[RealEvent] = []
    for candidate in ranked:
        if all(abs(candidate.origin_time - other.origin_time) >= event_window for other in selected_events):
            selected_events.append(candidate)
    selected_events.sort(key=lambda event: event.origin_time)

    elapsed = time.perf_counter() - started
    stats = RealRunStats(
        input_picks=len(picks),
        usable_picks=usable,
        deduplicated_picks=deduplicated_count,
        seed_count=len(seeds),
        evaluated_seeds=evaluated_seeds,
        candidate_count=len(candidates),
        event_count=len(selected_events),
        elapsed_s=elapsed,
        numba_enabled=NUMBA_AVAILABLE,
    )
    return RealRunResult(selected_events, stats)
