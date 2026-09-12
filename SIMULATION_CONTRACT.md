# Simulation integration contract (0.2.0)

Root owns controller/models/storage/CLI/docs/packaging and simulation orchestration.
Agents own assigned modules and tests. Coordinates remain millimetres.

Backends `sim_ngspice.py` and `sim_openems.py` expose:
`run(board, settings, workdir: Path, runtime: dict, circuit: dict | None = None,
cancel_event=None, progress=None) -> list[dict]`.
No GUI or engine imports at module import time. `progress(message: str)` optional.
Runtime is the selected engine info, not the complete runtime inventory.

Every simulation result is a JSON-safe dict:
- id, engine (ngspice|openems), title, status (complete|needs_information|unavailable|error|cancelled)
- summary (plain language), assumptions [str], missing [str], warnings [str]
- metrics [{name, value: finite number, unit}], series [{name, x: [finite number], y: [finite number], x_unit, y_unit}]
- recommendations [str], item_ids [str], location [x,y] optional
- artifacts [str] (absolute local generated files), cached bool optional
Only actual successful solver execution may yield complete. Check convergence,
finite output, excitation quality and other numerical validity where appropriate.
Bound time/memory; preserve all generated evidence/logs. Never modify PCB/schematic.
Measurements, estimated responses, unit-excitation field results and relative
predictions must remain distinctly labeled. No fabricated emissions/pass scores.

`simulation_runtime.py`: `discover() -> dict` with engine keys ngspice/openems and
kicad_cli. Engine dict has available bool, path str, kind executable|shared,
version str, message str. `ensure(progress=None,cancel_event=None) -> dict` may
automatically install pinned verified official portable solvers into app-owned
cache (Windows primary); must return readable availability/errors, never throw
for normal missing runtime. No admin/system setting edits/TLS bypass.

`sim_schematic.py`: `prepare(board, settings, workdir: Path, kicad_cli: dict,
cancel_event=None) -> dict` with status, message, source, fingerprint,
components list {reference,value,properties,pins:{pin:net}}, nets, warnings,
missing, artifacts, spice_path optional. Use native KiCad netlist export for
resolved circuit connectivity. Cross-check against actual board; custom symbols
work through actual pins/model metadata. No arbitrary .control execution from
unvalidated input. Explicitly report absent models/unsupported simulation nets.

Root `simulation.py`: `SimulationManager.run(board, settings, progress=None,
cancel_event=None) -> list[dict]`; cache by complete board geometry/materials,
settings, schematic/dependency content and engine/version identity. Call engines
independently so one failure never prevents other/layout results. Run from GUI
background thread automatically after each explicit Analyze/Open/Demo; auto-poll
must clear stale results without starting solver runs every 3 seconds.

UI worker calls `controller.run_simulations(progress=None,cancel_event=None)` after
showing layout result. `controller.result.simulations` holds returned list;
`controller.simulation_progress` unnecessary: QThread signal can pass progress.
`controller.cancel_simulations()` may set event provided by UI. UI implements
progress/cancel and a simulations pane with statuses, plain recommendations,
charts and expandable evidence. Keep Analyze EMI as the single primary action.

Settings `simulation`: enabled true, max_seconds 90, max_jobs 3,
frequency_start_mhz .1, frequency_stop_mhz 500, points 121,
capacitor_esr_ohm .03, capacitor_esl_nh .8, source_ohm 50,
termination_ohm 50, candidate_cap_nf 100, candidate_series_ohm 22,
schematic_path '' (optional explicit root selection), engines {ngspice:true,openems:true}.
Default component parasitics/port impedances are clearly labeled assumptions;
never imply datasheet-characterized values. Backend-specific additions notify root.
