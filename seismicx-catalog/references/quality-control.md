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

The helper uses:

```text
ML = log10(A_mm) + a * log10(R_km) + b * R_km + c
```

Default constants are `a=1.11`, `b=0.00189`, `c=-2.09`. These are not universal. Replace them with the regional calibration when available.

For calibrated ML:

- Use StationXML or equivalent response metadata.
- Remove response to displacement before amplitude measurement.
- Use horizontal components when available.
- Exclude clipped traces and stations with obvious response problems.
- Report station magnitudes and the event aggregation rule, usually median.

If response metadata is missing, label results as raw-scaled or preliminary and avoid publishing them as calibrated ML.

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
- Magnitude formula and response source.
- Focal-mechanism method and minimum-polarity threshold.
- Known limitations and recommended manual review items.
