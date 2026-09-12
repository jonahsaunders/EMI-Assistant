# Changelog

## 0.2.1 — 2026-09-12

- Add explicit third-party attribution and GPL/LGPL license texts, including
  the separate GPL terms for the copied KiCad validation schemas.
- Add an offline **Licenses** viewer and source/replacement information.
- Include notices in the KiCad installer and Python package data; packaging
  checks reject distributions that omit the required license files.
- Clarify that MIT applies to original application code and does not relicense
  third-party files or runtime dependencies. Simulation models are unchanged.

## 0.2.0 — 2026-09-12

- Added ngspice comparisons for capacitor-network impedance, supported RC/LC
  transfer response, and LC transient ringing with candidate damping.
- Added openEMS local microstrip-coupon simulations comparing reflection and
  voltage transfer for baseline and candidate terminations.
- Integrated both engines behind **Analyze EMI**, with background progress,
  cancellation, comparison graphs, assumptions, and explicit missing-input states.
- Added solver-result caching tied to board, settings, saved schematic inputs,
  and solver identity; stale results are discarded.
- Added native saved-schematic connectivity export and checks against PCB pins
  before using matched circuit information. Custom-symbol model metadata is
  retained; arbitrary behavioral models are outside this release's scope.
- Added a dependency-free launcher with visible first-click Python preparation,
  plus verified portable ngspice/openEMS downloads on Windows x64.
- Included simulation results in reports and added real Linux solver evidence,
  an example report, and documented model limitations.

Native Windows/KiCad GUI execution and hardware EMC validation remain pending.
See [the validation record](docs/VALIDATION.md).

## 0.1.1

- Corrected the install-from-file package metadata and resources for KiCad's
  Plugin and Content Manager.
- Added validation against the actual KiCad package and plugin schemas.

## 0.1.0

- Introduced the native KiCad companion with board layout screening, prioritized
  findings, navigation, context settings, reports, and measurement-assisted peak
  diagnosis.
