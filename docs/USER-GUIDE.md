# EMI Assistant for KiCad

Original application code is MIT licensed. The app uses Qt/PySide under LGPLv3
and other separately licensed components. Use **Licenses** in the app to read
the complete notices and license texts. See `THIRD_PARTY_NOTICES.md` in the
installed plugin directory for source and replacement information.

Open a board. Click **Analyze EMI**. Layout findings appear first; supported
ngspice circuit simulations and openEMS field simulations run in the background.
Select a finding or open **Simulations** to inspect the result and what to try.

Version **0.2.0** adds scoped solver comparisons and automatic first-launch
preparation. The app is a native companion window launched from KiCad, rather
than a docked panel. Solver predictions describe the displayed model and its
assumptions. Native Windows/KiCad operation and physical EMC measurements still
need validation; see `docs/VALIDATION.md` for the tested boundary.

## Install in KiCad

1. Use **KiCad 10.0.x** and open its **Plugin and Content Manager**.
2. Choose **Install from File**, select `EMI-Assistant-0.2.1.zip`, and apply the installation.
3. In KiCad **Preferences → Plugins**, enable **KiCad API** and make sure a
   Python interpreter is detected. Reopen the PCB Editor after installation.
4. Open your board and click the teal **Analyze EMI** toolbar button.

On the first click, a preparation window may download the application libraries
into KiCad's private EMI Assistant environment. It opens the app automatically
when ready. On **Windows x64**, supported analysis then reuses an available
ngspice/openEMS installation or downloads verified official portable engines
into a private cache. The **Simulations** tab displays progress and a cancel
button. Subsequent runs reuse installed engines and unchanged results.

No account or board upload is required. Downloads retrieve software; PCB,
schematic, generated models, and solver results remain on your computer. Once
dependencies and engines are ready, analysis works offline.

If the toolbar button is absent, check **PCB Editor → Plugins** in Preferences
and enable **Show Button**. If first-launch preparation fails, the button remains
available: the error opens a setup log, and another click retries. See
`docs/INSTALLER-FIX.md` for troubleshooting. A working Python 3.10+ installation
and Qt system libraries are needed on Linux. Automatic portable solver download
currently supports Windows x64; Linux/macOS reuse installed solver executables
and explain which engine is missing.

## The everyday workflow

* **Analyze / Recheck board** reads the open, including unsaved, PCB through KiCad.
  While connected, the app checks for changes in the background and refreshes
  the results, keeping your selected finding when it still exists.
* **Analyze EMI** also starts both enabled simulation engines in the background.
  Layout findings stay usable while simulations run. A changed board invalidates
  old simulation results; press Analyze EMI to simulate that revision.
* **Simulations** displays numerical response curves, baseline/candidate
  comparisons, missing information, model assumptions, and saved solver evidence.
  **Cancel simulations** keeps the layout analysis available.
* **Start here** shows the top three findings; **Show all** expands the rest.
* Select a finding to focus the board view. **Show in KiCad** selects the real
  items in the PCB editor, using the supported native API.
* **Highlight path to improve** shows an existing path that needs attention.
  It does not claim to be an automatically generated reroute.
* **Preview suggestion** shows candidate via geometry when available.
* **What was checked?** explains completed, partial, and unavailable checks.
* **Board context** lets you correct net recognition, enter edge rates and clock
  frequencies, identify cable connectors, and describe known buck/boost regulators.
* **Ignore with a reason** records an intentional exception. You can restore
  excluded findings from the coverage view.

The app also opens `.kicad_pcb` files directly and includes **Try the example
board**, which works without a running KiCad session. File mode cannot select
or edit a different board open in KiCad. Launch from the relevant PCB editor for
that connection.

## Included checks

| Check | Evidence used | Important limit |
|---|---|---|
| Reference copper gaps | Fast-net centerlines and actual filled polygons/holes | Reference selection can be inferred; current distribution is not solved |
| Return connections at layer changes | Signal transitions, same-net copper contact and through-layer connections | A nearby via on another net or disconnected island does not count |
| Decoupling connections | Routed paths between IC/capacitor pads, including return connection when known | Plane-mediated path impedance and component parasitics are not extracted |
| Buck/boost layout | Explicit topology, relevant capacitor paths, switch-node copper extent | Missing circuit roles produce partial coverage |
| Sensitive and cable-net coupling | Proximity and parallel routing to recognized/marked nets | Geometric screening cannot establish coupling amplitude |

Priority and confidence are separate. Unknown edge rates, ambiguous reference
planes, incomplete saved schematic context, and unfilled/unsupported geometry
remain visible. A quiet result means no findings in the checked areas; it does
not establish product compliance.

## Included simulations

| Engine and check | What is compared | Scope and required information |
|---|---|---|
| ngspice capacitor-network impedance | Baseline shunt-capacitor network and an added capacitor across frequency | Recognized two-terminal capacitors, numeric values, reference nets and modeled connection paths; ESR/ESL and geometry-derived parasitics are estimates |
| ngspice RC/LC transfer | Baseline isolated passive stage and added series resistance | An unambiguous supported stage; source/load impedances and component parasitics are shown assumptions |
| ngspice LC ringing | Step response, overshoot and settling with and without damping resistance | A supported LC stage and a stated synthetic input step; this is not a transistor-level switching-regulator waveform |
| openEMS local transmission-line model | Reflection **S11** and load/input voltage transfer for two resistive loads | A suitable straight outer-layer trace section, a continuous adjacent reference, and saved dielectric thickness/permittivity |

The openEMS model is a **local microstrip coupon derived from the PCB**. It uses
an ideal broad reference plane after checking local filled-copper continuity.
It does not solve the complete route, whole PCB, enclosure, cables, component
packages, or far-field emissions. Loss assumptions and omitted geometry remain
visible in each result. A missing stackup, unsupported geometry, or absent input
produces **Needs information** rather than a passing result.

The circuit backend generates bounded passive models and runs the actual
ngspice solver. It does not automatically execute arbitrary SPICE model text,
simulate a custom IC's internal behavior, or establish regulatory compliance.
Predicted dB changes are specific circuit or port-response comparisons, not
predicted reductions in radiated emissions. No simulation changes the PCB.

You can reuse the defaults or open **Simulation settings…** in the Simulations
tab to save source/load assumptions, capacitor ESR/ESL, the frequency range,
time limit, and enabled engines. Settings are remembered with the board.

## Saved schematics and custom symbols

The app finds the matching saved `.kicad_sch` beside the board, follows bounded
project-local hierarchical sheets, and asks `kicad-cli` to export native
connectivity from a staged copy. You can choose a different root schematic once
in **Simulation settings…**. Save schematic changes before analysis; this uses
saved schematic content even when the PCB snapshot contains unsaved edits.

Component references, pin numbers, symbol paths and connections are checked
against the board before matched circuit information is used. Custom symbols
are read through their saved definitions and pins. Assigned simulation-model
metadata is retained and missing or unsupported model inputs are reported;
arbitrary IC/subcircuit models are not executed by this release. A custom symbol
can therefore still participate in layout checks without a behavioral model.

Missing KiCad CLI, mismatched connections or unavailable schematic information
remain visible. Independently supported PCB-derived checks can still run.
Solver inputs, exported connectivity, logs and numerical outputs are retained
in the application cache and listed with each result. See `docs/SIMULATION.md`
for the model and installation details.

For an example that supports both solvers, open the included
`examples/clean.kicad_pcb`. `docs/example-simulation-report.html` contains an
actual completed run and comparison curves. The default layout example has an
intentional reference-plane gap, so its local field model correctly needs
different geometry.

## Applying a suggested return via

The Apply action is available only in a connected KiCad session when supported
native DRC validation is available. Most recommendations are guided placement
or routing actions.

Applying saves the current project settings. The live PCB is not saved by
analysis. The adapter captures temporary native copies, checks filled copper and
candidate connectivity, runs KiCad DRC on the baseline and the proposed board,
and rejects new DRC results. It rechecks live board/rule state before committing
the via as one native Undo operation. The CLI version must match the running
KiCad version. A failed validation leaves the live via unchanged.

This path is implemented and tested with controlled API/CLI doubles, but has not
been exercised against a real KiCad installation in the build environment.
Use a disposable project first and verify native Undo/Redo and DRC before
relying on it. Analysis and manual guidance remain usable when Apply is unavailable.

## Diagnose a measured peak

Open **Diagnose a peak**, enter its frequency in MHz, or import a CSV spectrum.
Known clocks and oscillator values produce ranked harmonic hypotheses and a
next experiment, such as changing a source frequency or comparing operating
modes. A matching harmonic is not proof of the source.

Accepted CSV headers include `frequency_mhz,amplitude` or
`frequency_hz,amplitude`. Record measurement units and setup when saving a
reading. Saved measurements include the board fingerprint, label, frequency,
level, unit, timestamp, and setup notes. Compare only like-for-like setups.

## Project files and reports

* `your-board.emi.json`: classifications, thresholds, and exception reasons.
* `your-board.emi-measurements.json`: measurement notebook.
* **Export report**: portable HTML or structured JSON with findings, evidence,
  assumptions, coverage, revision counts, saved measurements and simulation results.

Sidecars are written atomically. Demo context and measurements remain in memory.
The app reads only the selected board and explicitly related saved schematic
files, with bounded traversal of project-local hierarchical sheets. Saved
schematic context is supplemental; unsaved schematic/netlist synchronization
cannot be established by this version.

Broad filter-topology recognition, differential-pair imbalance analysis, full
automatic rerouting, arbitrary behavioral-circuit simulation and complete-board
field/emissions simulation remain future extensions.

## Run or develop from source

The installation archive contains the complete source under `plugins/`,
including tests, examples, and documentation. Extract that directory and run:

```sh
python -m venv .venv
# Activate the environment using your operating system's normal command.
python -m pip install -r runtime-requirements.txt
python -m emi_assistant --demo
```

Command-line analysis uses the same engine:

```sh
python -m emi_assistant --board board.kicad_pcb --analyze --output review.html
python -m emi_assistant --demo --analyze --output review.json
```

For development, install pytest, then run `python -m pytest -q`. Qt tests use
offscreen rendering. `python tools/build_package.py` rebuilds the PCM archive.

Core dependencies are pinned/bounded in `runtime-requirements.txt`: official
KiCad Python bindings, PySide6 Essentials, Shapely, NumPy and py7zr. The
comment-only `requirements.txt` lets KiCad register the dependency-free launcher
before first-click preparation. Development packaging additionally requires
`jsonschema`. Dependency and solver licenses remain with their projects;
their runtime distributions are downloaded separately from the plugin ZIP.
This project's source is MIT licensed.

## Engineering references

* [KiCad IPC API](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/)
* [KiCad Python Board API](https://docs.kicad.org/kicad-python-main/board.html)
* [KiCad plugin packaging](https://dev-docs.kicad.org/en/addons/)
* [ngspice documentation](https://ngspice.sourceforge.io/docs.html)
* [openEMS documentation](https://docs.openems.de/)
* [TI High-Speed Interface Layout Guidelines](https://www.ti.com/lit/an/spraar7j/spraar7j.pdf)
* [TI High Speed Layout Guidelines](https://www.ti.com/lit/an/scaa082a/scaa082a.pdf)
* [ADI AN-139, Power Supply Layout and EMI](https://www.analog.com/en/resources/app-notes/an-139.html)
* [Tektronix Practical EMI Troubleshooting](https://www.tek.com/en/documents/application-note/practical-emi-troubleshooting)
