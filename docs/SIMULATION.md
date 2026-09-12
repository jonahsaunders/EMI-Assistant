# Simulation models and runtime setup in 0.2.0

Press **Analyze EMI** once. Layout findings appear first; the app runs supported
circuit and field jobs in the background. The Simulations tab shows progress,
cancel, baseline/candidate curves, units, assumptions and retained solver files.
Missing inputs and failed engines are separate states from completed results.

## Circuit models: ngspice

The backend constructs a limited set of numeric passive-circuit models. It
compares a capacitor network against an additional capacitor, an eligible RC/LC
stage against added series resistance, and an eligible LC stage's step response
against damping resistance. It invokes ngspice in batch mode or uses a detected
ngspice shared library in an isolated worker process.

Resistor/capacitor/inductor values and connections come from supported PCB and
matched saved schematic information. The generated decks list the subset being
modeled. Capacitor ESR/ESL, source/load impedance, omitted IC/regulator behavior,
estimated connection inductance and synthetic step assumptions are disclosed.
The result is a local impedance or transfer comparison. A response improvement
in dB is not a prediction of radiated-emissions improvement.

Assigned custom-symbol simulation metadata is retained for diagnosis. General
SPICE subcircuits, transistor models, vendor libraries and arbitrary control
statements are not executed. Recognizing a pin or component name never implies
that its internal electrical behavior has been modeled.

## Field model: openEMS

The backend selects a supported straight outer-layer signal section with a
single adjacent homogeneous dielectric and a continuous local reference. It
builds a bounded microstrip coupon using the trace dimensions and saved stackup.
The reference is an ideal broad plane; local filled-copper continuity is checked
before accepting that simplification. The baseline and comparison vary the
resistive load while keeping the stated excitation and model geometry.

The real openEMS executable solves the generated XML. Port voltages and currents
are processed into input reflection **S11** and load/input voltage transfer over
supported frequencies. Time/mesh bounds and numerical validity checks reject
incomplete or unusable data. Bends, other route sections, IC packages, full-board
coupling, enclosures, cables and radiated far fields are outside this model.
Missing dielectric loss data is disclosed as a lossless-dielectric assumption.

## Schematic inputs and source preservation

The matching saved `.kicad_sch` is selected automatically or chosen once in
Simulation settings. Project-local hierarchical sheets and relevant saved
inputs are fingerprinted. `kicad-cli sch export netlist --format kicadxml` runs
against staged schematic copies. Matched component identities, pin numbers and
net memberships are checked against the PCB before circuit enrichment is used.
Input changes during export are reported rather than cached as a coherent model.

Analysis and simulation do not save, refill, reroute or otherwise modify the
original PCB or schematic. Generated decks/XML, exported connectivity, logs,
model assumptions and numerical outputs are stored in the application cache.
Reports include solver results and their evidence paths. The saved schematic
can lag an unsaved PCB; save edits and resolve reported mismatches before relying
on circuit comparisons.

## Private solver preparation

Runtime discovery checks configured paths, cached solvers, PATH and relevant
KiCad locations. On Windows x64 only, missing engines are fetched from these
pinned official release archives using HTTPS and checked against exact archive
size and SHA-256 before installation:

| Engine | Portable release | Official source |
|---|---|---|
| ngspice | 47, `ngspice-47_64.7z` | <https://sourceforge.net/projects/ngspice/files/ng-spice-rework/47/> |
| openEMS | 0.0.36, `openEMS_v0.0.36.zip` | <https://github.com/thliebig/openEMS-Project/releases/tag/v0.0.36> |

`emi_assistant/simulation_runtime.py` is the source of the version, size and
checksum pins. Archive extraction rejects unsafe paths and excessive content.
Setup retains the upstream distribution, probes the executable, then marks the
private installation ready. A failed or cancelled download never counts as an
installed working engine. Retrying Analyze EMI can resume the setup workflow.

On Windows, solvers normally live below `%LOCALAPPDATA%\EMI-Assistant\solvers`.
The app does not change registry entries, system PATH, other plugins or system
packages. Linux/macOS require working installed engines and show actionable
per-engine unavailable states. The plugin ZIP contains its own source, not the
third-party solver binaries; their accompanying licenses stay with each runtime.

Advanced environments can set `EMI_NGSPICE`, `EMI_OPENEMS` or `EMI_KICAD_CLI` to an
existing executable/library, and `EMI_ASSISTANT_CACHE_DIR` or `EMI_SOLVER_CACHE`
to a cache directory. The ordinary Windows workflow needs none of these.

## Reuse and limitations

Simulation caches include board/settings/schematic inputs and solver identity.
Unchanged completed results can be reused offline. Changed inputs invalidate
the corresponding result, and cancelled, error or unavailable jobs are not
presented as cached successes. Solver files are retained for review.

These models help choose which hypothesis or component change to investigate.
They do not establish product compliance. Validate a recommended change against
the real driver, load, component limits and measurements. Native Windows/KiCad
GUI acceptance and hardware validation remain pending; see `VALIDATION.md`.
