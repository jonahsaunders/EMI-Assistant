# Module contract

All geometry is millimetres. Models are in `emi_assistant.models`. No module
imports GUI or kipy at import time except `ui.py` itself.

* `parser.parse_board(text: str, path: str = "") -> BoardSnapshot` and
  `parser.load_board(path: str) -> BoardSnapshot` parse KiCad s expressions.
* `engine.analyze(board: BoardSnapshot, settings: dict | None = None) -> AnalysisResult`.
* `kicad_adapter.KiCadAdapter`: `snapshot() -> BoardSnapshot`,
  `locate(finding: Finding) -> str`, `apply_return_via(finding, expected_fingerprint: str) -> str`.
  Adapter imports kipy lazily. Failure is actionable RuntimeError.
* `controller.Controller`: `result: AnalysisResult | None`, `settings: dict`,
  `mode: str` (connected|file|demo), `analyze() -> AnalysisResult`,
  `open_board(path: str) -> AnalysisResult`, `load_demo() -> AnalysisResult`,
  `locate(finding) -> str`, `apply_fix(finding) -> str`,
  `ignore(finding, reason: str) -> AnalysisResult`, `reset_ignored() -> AnalysisResult`,
  `update_settings(settings: dict) -> AnalysisResult`, `export_report(path: str) -> None`,
  `diagnose(frequency_mhz: float) -> list[dict]` (candidate, relationship, evidence, experiment),
  `import_spectrum(path: str) -> list[dict]` (frequency_mhz, amplitude),
  `save_measurement(label: str, frequency_mhz: float, amplitude: float, unit: str, notes: str) -> str`.
  Long calls are serialized by the controller and called by GUI worker.
* `ui.run(controller, auto_connect: bool = False) -> int`: native PySide6 Essentials
  GUI. No WebEngine. Welcome offers Analyze open KiCad board, Open PCB, Try demo.
* `diagnostics.diagnose(board, settings, frequency_mhz) -> list[dict]`.

Settings `fast_nets`: net name -> {frequency_mhz: float optional, rise_ns: float
optional, enabled: bool optional}. `reference_layers`: signal layer -> list of
reference layer names. `regulators`: [{reference, topology: buck|boost,
input_cap, output_cap, switch_net, ground_net (optional)}]. Unknown topology
must remain a coverage limitation. Auto inference should have lower confidence.

`Finding.preview` uses `kind` (via|route|region), `position` [x,y] for via,
`points` [[x,y],...] for route/region, `net`, `layers`, `diameter`, `drill` for via.
It must never imply a DRC-checked fix unless actually validated. Automatic write
must fail closed without full checks. Stable IDs should derive from rule + item
IDs, not title/distance. Ignore reasons stay in settings keyed by finding ID.

Each agent owns assigned modules/tests only; root owns models/controller/
diagnostics/packaging/docs. Notify root about contract changes before editing it.
