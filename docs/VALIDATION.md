# Validation and the ten development steps

This document separates implemented software from evidence still requiring
KiCad installations, representative user projects, or an EMC laboratory.

## License and packaging patch: 0.2.1

This patch adds third-party notices, complete GPL/LGPL texts, and an offline
Licenses viewer. It does not change the numerical simulation models. The
updated automated suite passed **287 tests and 43 subtests**, with five optional
native-solver tests skipped in this invocation. The actual solver evidence
below remains the separately recorded 0.2.0 evidence.

The Python wheel was built and extracted: all six license documents matched
the source bytes, and the installed offline document reader loaded every one.
The updated application and build modules also compile with native Python
3.11. The KiCad ZIP passes schema and required-payload checks, including checks
for missing notices. `validation/license-package-check.txt` records this patch's
verification scope. Native Windows execution remains unverified here.

## Development scope: 0.2.0

| Step | Implemented in this release | Remaining validation or extension |
|---|---|---|
| 1. KiCad integration | IPC plugin manifest, toolbar entry, live snapshots, native item selection, guarded edit adapter | Real installed KiCad 10 sessions on each platform |
| 2. Integrated interface | Native Qt companion, board view, finding navigation, advisory previews | A genuinely docked KiCad panel and temporary native canvas overlays require additional API/upstream work |
| 3. Design representation | PCB parser, actual fills/holes, physical layers, pads/tracks/vias/arcs, stable IDs; optional saved schematic enrichment | Expand representative fixture corpus and support currently reported geometry omissions |
| 4. Circuit recognition | Fast-net inference, saved pin/component metadata, native saved schematic netlist export, board/pin matching, oscillator values, explicit regulator/cable context | Broader curated component/topology library; unsaved schematic API and arbitrary behavioral models |
| 5. EMI checks | Core layout screening; actual ngspice passive-network AC/transient comparisons and openEMS local straight-microstrip port responses | Whole-board field/emissions solving, arbitrary circuits, general filter/differential-pair checking, calibrated parasitic extraction |
| 6. Recommendations | Plain explanations, evidence, confidence, priority, duplicate grouping and persistent exclusions | Blind review by several EMC engineers on real designs |
| 7. Fixes | Advisory previews; native-copy/DRC-gated return-via transaction with stale-state and rollback checks | Live DRC/Undo validation; complex placement and autorouting are guided |
| 8. Failure diagnosis | Harmonic hypotheses, CSV import, next experiment and measurement notes | Time-domain correlation and instrument integrations |
| 9. Validation | Automated parser/geometry/diagnosis/persistence/UI/adapter, solver-model, cache, cancellation and bootstrap tests | Real A/B hardware, conducted/radiated measurements and usability study |
| 10. Distribution | Schema-validated install-from-file ZIP, stdlib-only action registration, visible first-click dependency preparation, verified private Windows x64 solver downloads, complete source | Public PCM submission, native Windows/macOS/Linux installation matrix, production release |

## Automated evidence

The exact command output for this build is in `validation/test-results.txt`.
Tests exercise physical counterexamples, state preservation, malformed inputs,
Python 3.10-compatible syntax, Qt interaction, and rejected/rolled-back edits.
Controlled API and CLI doubles test adapter logic; they do not substitute for
real KiCad operation. Example PCBs are illustrative geometry fixtures, not
manufactured or EMC-tested boards.

Release integration includes real Linux solver execution with **ngspice 42**
and **openEMS 0.0.35**, in addition to process doubles. The ngspice test group
passed 33 tests; the openEMS group passed 20 tests including an actual FDTD run
and a comparison that correctly did not recommend a worse termination. Actual
Python 3.11.2 compilation and 32 bootstrap/runtime tests were also checked.
These counts describe the independently exercised groups, not the final full
suite; see the release test output for the combined result.

The final combined suite passed **286 tests and 32 subtests**. Its optional
openEMS test was skipped in that invocation; that test passed separately using
the actual solver. The complete controller workflow on `examples/clean.kicad_pcb`
then completed capacitor impedance, LC transfer, LC transient and openEMS
termination comparisons in **43.70 seconds**. Repeating unchanged inputs reused
all four results in **0.054 seconds**. The 50-ohm field-model load had worst
S11 of -24.28 dB; the proposed 72-ohm load had -14.98 dB and was not recommended.
These are results for the stated example model, not measured PCB emissions.

`validation/simulation-summary.json` records the numerical evidence and timing.
`validation/native-evidence/` includes the actual generated solver inputs, logs
and output data. Paths in this summary and `docs/example-simulation-report.html`
are relative to the installed plugin directory. The simulation screenshot in
`docs/screenshots/simulations.png` was rendered in Qt using the real cached
results; the automatically launched background worker completed without
blocking the window.

The official Windows portable downloads are ngspice 47 and openEMS 0.0.36.
Their archive hashes and contents were verified, but those Windows binaries and
the native Windows GUI were not executed in this Linux build environment.

## Live KiCad acceptance procedure

1. Install the ZIP in an otherwise clean KiCad 10 profile. Confirm Analyze EMI
   appears before runtime dependencies are downloaded. Click it and verify the
   visible preparation window opens the assistant automatically. Test a path
   containing spaces/non-ASCII. Repeat offline: setup failure must retain the
   action, show the log, and recover on a later click with connectivity restored.
2. Open a disposable copy of each example. Refill zones in KiCad. Confirm every
   parser warning and unexpected finding; never assume hand-authored cached
   fills are the same as the native refill.
3. Analyze a real board. Verify UUID selections, layer visibility, multiple open
   PCB sessions, and unsaved geometry changes. Background refresh should retain
   the selected issue where possible and never show stale results as current.
4. Change context, exclude a result, reopen the app, and verify persistence.
   Attempt an invalid second board/context and confirm the first project's
   result and write targets remain intact.
5. Preview a return via in a disposable board with a matching CLI. Verify saved
   project-setting behavior, no new DRC violations, correct net/span/clearance,
   one Undo/Redo step, and rejection of concurrent board or rule changes.
6. Test absent CLI, version mismatch, missing project, custom rules, keepouts,
   isolation boundaries, native DRC errors, locked items, and an interrupted IPC
   session. Each must produce clear guidance without an unvalidated mutation.
7. Repeat on Windows, macOS, and Linux, including large boards and high-DPI displays.

## Simulation acceptance procedure

1. On Windows x64, use a populated disposable board and click Analyze EMI.
   Confirm layout findings remain available while engine preparation and solver
   progress appear in Simulations. Check the downloaded version, archive checksum
   and private cache paths against `simulation_runtime.py`.
2. Repeat with the network disconnected after setup. Installed engines and
   unchanged numerical results should be reused. Changing board geometry,
   simulation settings, saved schematic/model inputs or solver identity must
   invalidate corresponding results. Missing/error/cancelled execution must
   never appear as a successful simulation.
3. Compare generated ngspice decks with manually reviewed passive networks.
   Check simple RC cutoff, capacitor impedance, LC resonance and transient DC
   settling against analytical expectations, then compare baseline and candidate
   responses. Inspect every assumed ESR/ESL, source/load and synthetic edge value.
4. Compare the generated openEMS XML/mesh/ports with a known microstrip model.
   Verify current direction, incident/reflected wave normalization, spectrum
   support, convergence checks, termination comparison and timeout behavior.
   Port-transfer curves must not be labeled as whole-board radiation or a
   compliance result. Model-missing cases must explain exactly what is absent.
5. Save and load reports with real completed solver evidence. Verify units,
   assumptions, cached status and input fingerprints. Change boards while a job
   runs; discard stale results and preserve the newly selected board. Cancel a
   job or close the window and confirm solver children stop.
6. Use custom symbols, hierarchical sheets and stale schematic/PCB pairs. Verify
   native pin matching and missing model reports. General IC/subcircuit behavior
   remains unsupported even when an assigned model is recorded.

Native Windows GUI/KiCad execution is not available in the build environment.
Mocked process/API tests establish control flow and failure handling, not that
matrix. Any real solver numerical validation performed for the release is
recorded separately; a generated model or mocked subprocess is not a solver run.

## EMC evidence plan

Select representative digital, switching-power, and mixed-signal boards with
known issues. Have independent engineers review top-ranked findings without
being told which defects were inserted. Track precision and actionable top-three
findings, user time to first understood recommendation, and missed known issues.

For selected A/B changes, record supply/load/firmware mode, cabling/enclosure,
probe position/orientation/height, detector/bandwidth, frequency, and units.
Compare near-field, cable-current, conducted and radiated results as appropriate.
Separate observed measurements from simulation and geometry heuristics. Never
advertise a predicted dB reduction or regulatory pass from finding count.
