# Quality Control

Use this checklist before returning a catalog or publishing derived figures.

## Picking QC

- Report total picks by phase and station.
- Flag stations with abnormal pick counts or no picks.
- Remove duplicate picks within a small station-phase time window unless the user intentionally wants multiple candidate picks.
- Check P before S for each station-event pair.
- Review low SNR picks and first-motion scores near threshold.

## Association QC

- Count picks per event and P/S balance.
- Plot event origin times or inter-event times to detect artificial bursts.
- Inspect rejected picks when the rejected fraction is high.
- Compare REAL and GaMMA on a short time window when both are available.

## Location QC

- Require an explicit velocity model for production locations.
- Inspect RMS, depth bounds, and station residuals.
- Flag events at search-grid boundaries.
- Check map extent and station geometry.
- Preserve the locator method and parameters in final notes.

## ML Magnitude QC

The default helper path follows `seedtools.share.version/utils/calmag.py`:

```text
1. Trim the S-wave window: S - 0.5 s to S + distance-dependent width.
2. Remove station response to velocity with pre-filter and water level.
3. Simulate/integrate velocity to displacement.
4. Convert displacement to micrometers.
5. Measure SME/SMN horizontal half peak-to-trough amplitude with period checks.
6. Compute ML = log10(A_um) + R(distance) using R11/R12/R13/R14/R15.
```

Use `--region R13` or the appropriate regional curve for the network. The older generic formula path is available as `--ml-method wood-anderson-formula`, but the seedtools DD1 path is the default.

For calibrated ML:

- Use StationXML, RESP/dataless metadata, or a seedtools `sta.resp.path` mapping.
- Confirm response units and polarity before trusting amplitudes.
- Use both horizontal components when available; review `max_amp_e_um`, `max_amp_n_um`, `period_e_s`, and `period_n_s`.
- Exclude clipped traces and stations with obvious response problems.
- Report station magnitudes and the event aggregation rule, usually median.

If response metadata is missing, station rows are labeled `raw_scaled_um`; avoid publishing them as calibrated ML.

## Activity And Map Products

- Include event count, time span, magnitude range, and completeness caveats.
- For maps, plot stations and events together.
- Use Cartopy when installed; fallback Matplotlib maps are acceptable for quick QC but not final cartographic products.
- Verify longitude/latitude order when converting legacy files.

## Delivery Notes

Final delivery should include:

- Input paths and date range.
- Picker and selected phases.
- Association algorithm and parameters.
- Locator, velocity model, and uncertainty handling.
- Magnitude method, regional R curve, response source, and station-level amplitude QC.
- Focal-mechanism method and minimum-polarity threshold.
- Known limitations and recommended manual review items.
