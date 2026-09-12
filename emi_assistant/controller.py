"""Application orchestration, shared by the native desktop app and CLI."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
from . import diagnostics
from .models import AnalysisResult, Finding, default_settings
from .storage import load_settings, validate_settings, write_json

class Controller:
    def __init__(self, adapter=None):
        self.result: AnalysisResult | None = None
        self.settings = default_settings()
        self.mode = "connected"
        self.path: str | None = None
        self._adapter = adapter
        self._lock = threading.RLock()
        self._settings_path: Path | None = None
        self._measurement_path: Path | None = None
        self._identity: str | None = None
        self.measurements: list[dict] = []
        self.changes: dict = {}
        self._simulation_manager = None
        self._simulation_cancel = None

    @property
    def adapter(self):
        if self._adapter is None:
            from .kicad_adapter import KiCadAdapter
            self._adapter = KiCadAdapter()
        return self._adapter

    def _adopt(self, board):
        identity = f"{self.mode}:{board.path or board.name}"
        if identity != self._identity:
            settings_path = Path(board.path).with_suffix(".emi.json") if board.path else None
            measurement_path = Path(board.path).with_suffix(".emi-measurements.json") if board.path else None
            settings = load_settings(settings_path)
            measurements = []
            if measurement_path and measurement_path.exists():
                try:
                    raw = json.loads(measurement_path.read_text(encoding="utf-8"))
                    measurements = raw if isinstance(raw, list) else []
                except (OSError, ValueError):
                    board.warnings.append("Saved measurements could not be read. The original file has not been changed.")
            self.settings = settings
            self.measurements = measurements
            self._settings_path = settings_path
            self._measurement_path = measurement_path
            self._identity = identity
            self.result = None
            self.changes = {}

    def _switch_source(self, board, mode, path):
        # A failed Open must leave both the visible board and every write target
        # on the original project. Keep adoption + analysis one transaction.
        names = ("mode", "path", "result", "settings", "measurements", "changes",
                 "_identity", "_settings_path", "_measurement_path")
        previous = {name: getattr(self, name) for name in names}
        self.mode, self.path = mode, path
        try:
            return self._analyze_snapshot(board)
        except Exception:
            for name, value in previous.items():
                setattr(self, name, value)
            raise

    def _analyze_snapshot(self, board) -> AnalysisResult:
        from .engine import analyze
        try:
            from .schematic import enrich
            board.warnings.extend(enrich(board))
        except ImportError:
            pass
        self._adopt(board)
        old = self.result
        result = analyze(board, self.settings)
        if self._simulation_cancel is not None:
            self._simulation_cancel.set()
        for finding in result.findings:
            if finding.preview:
                finding.preview["can_apply"] = False
                if self.mode == "connected" and finding.preview.get("kind") == "via":
                    try:
                        capable, reason = self.adapter.can_apply(finding)
                        finding.preview["can_apply"] = bool(capable)
                        finding.preview["apply_reason"] = reason
                    except (AttributeError, RuntimeError) as exc:
                        finding.preview["apply_reason"] = str(exc) or "Place the suggested via in KiCad, then run DRC."
        old_ids = {f.id for f in old.findings} if old else set()
        current_ids = {f.id for f in result.findings}
        self.changes = {"new": len(current_ids - old_ids), "resolved_or_excluded": len(old_ids - current_ids), "remaining": len(current_ids & old_ids), "has_baseline": old is not None}
        self.result = result
        return result

    def run_simulations(self, progress=None, cancel_event=None) -> list[dict]:
        """Snapshot inputs under lock; slow solvers never hold the PCB/UI lock."""
        with self._lock:
            if self.result is None:
                raise RuntimeError("Analyze a board before running simulations.")
            if self._simulation_cancel is not None:
                self._simulation_cancel.set()
            token = cancel_event or threading.Event()
            self._simulation_cancel = token
            original = self.result
            board = deepcopy(original.board)
            settings = deepcopy(self.settings)
            if self._simulation_manager is None:
                from .simulation import SimulationManager
                self._simulation_manager = SimulationManager()
            manager = self._simulation_manager
        results = manager.run(board, settings, progress=progress, cancel_event=token)
        with self._lock:
            if self.result is original and self._simulation_cancel is token:
                original.simulations = results
            # A completed older job must never overwrite a newly opened board.
        return results

    def cancel_simulations(self):
        with self._lock:
            if self._simulation_cancel is not None:
                self._simulation_cancel.set()

    def analyze(self) -> AnalysisResult:
        with self._lock:
            if self.mode == "connected":
                board = self.adapter.snapshot()
            else:
                from .parser import load_board
                if not self.path:
                    raise RuntimeError("Open a PCB or choose the demo first.")
                board = load_board(self.path)
            return self._analyze_snapshot(board)

    def connect(self) -> AnalysisResult:
        with self._lock:
            board = self.adapter.snapshot()
            return self._switch_source(board, "connected", board.path or None)

    def poll_and_analyze(self) -> AnalysisResult | None:
        """Refresh unsaved live-board edits; UI calls this in its serialized worker."""
        with self._lock:
            if self.mode != "connected" or self.result is None:
                return None
            board = self.adapter.snapshot()
            if board.fingerprint == self.result.board.fingerprint and board.path == self.result.board.path:
                return None
            return self._switch_source(board, "connected", board.path or None)

    def open_board(self, path: str) -> AnalysisResult:
        with self._lock:
            from .parser import load_board
            path = str(Path(path).expanduser().resolve())
            board = load_board(path)
            return self._switch_source(board, "file", path)

    def load_demo(self) -> AnalysisResult:
        with self._lock:
            from .parser import load_board
            path = Path(__file__).resolve().parent / "data" / "examples" / "demo.kicad_pcb"
            if not path.exists():
                path = Path(__file__).resolve().parent.parent / "examples" / "demo.kicad_pcb"
            board = load_board(str(path))
            return self._switch_source(board, "demo", str(path))

    def locate(self, finding: Finding) -> str:
        with self._lock:
            self._require_finding(finding)
            if self.mode != "connected":
                return "This PCB is open as a file. Open it in KiCad and launch EMI Assistant from its toolbar to select the items there."
            current = self.adapter.snapshot()
            if current.fingerprint != self.result.board.fingerprint:
                raise RuntimeError("The board changed. Click Analyze to refresh this finding before locating it.")
            return self.adapter.locate(finding)

    def _require_finding(self, finding: Finding):
        if self.result is None or not any(f.id == finding.id for f in self.result.findings):
            raise RuntimeError("This finding is no longer current. Analyze the board again.")

    def apply_fix(self, finding: Finding) -> str:
        with self._lock:
            self._require_finding(finding)
            if self.mode != "connected" or finding.preview.get("kind") != "via":
                raise RuntimeError("Follow this suggested change in KiCad, then recheck the board.")
            capable, reason = self.adapter.can_apply(finding)
            if not capable:
                raise RuntimeError(reason)
            return self.adapter.apply_return_via(finding, self.result.board.fingerprint)

    def _persist_settings(self):
        if self.mode == "demo":
            return
        if self._settings_path is None:
            raise RuntimeError("Save the PCB in KiCad first so its board context has a project location.")
        write_json(self._settings_path, self.settings)

    def update_settings(self, settings: dict) -> AnalysisResult:
        with self._lock:
            proposed = validate_settings(settings)
            previous = self.settings
            self.settings = proposed
            try:
                self._persist_settings()
            except Exception:
                self.settings = previous
                raise
            return self.analyze()

    def ignore(self, finding: Finding, reason: str) -> AnalysisResult:
        with self._lock:
            self._require_finding(finding)
            if not reason.strip():
                raise ValueError("Add a short reason for excluding this finding.")
            settings = deepcopy(self.settings)
            settings["ignored"][finding.id] = reason.strip()[:1000]
            return self.update_settings(settings)

    def reset_ignored(self) -> AnalysisResult:
        settings = deepcopy(self.settings)
        settings["ignored"] = {}
        return self.update_settings(settings)

    def export_report(self, path: str) -> None:
        with self._lock:
            if self.result is None:
                raise RuntimeError("Analyze a board before exporting a report.")
            from .report import export_report
            export_report(self.result, path, self.changes, self.measurements)

    def diagnose(self, frequency_mhz: float) -> list[dict]:
        with self._lock:
            if self.result is None:
                raise RuntimeError("Analyze a board before diagnosing a frequency.")
            return diagnostics.diagnose(self.result.board, self.settings, frequency_mhz)

    def import_spectrum(self, path: str) -> list[dict]:
        return diagnostics.import_spectrum(path)

    def save_measurement(self, label: str, frequency_mhz: float, amplitude: float, unit: str, notes: str) -> str:
        with self._lock:
            if not self.result:
                raise RuntimeError("Analyze a board first.")
            frequency_mhz = diagnostics.positive_number(frequency_mhz, "frequency in MHz")
            amplitude = float(amplitude)
            if not math.isfinite(amplitude):
                raise ValueError("Enter a finite measurement level.")
            if not label.strip() or not unit.strip():
                raise ValueError("Add a measurement label and its unit.")
            record = {"label": label.strip(), "frequency_mhz": frequency_mhz, "amplitude": amplitude, "unit": unit.strip(), "notes": notes.strip(), "timestamp": datetime.now(timezone.utc).isoformat(), "board_fingerprint": self.result.board.fingerprint}
            updated = [*self.measurements, record]
            if self.mode != "demo":
                if not self._measurement_path:
                    raise RuntimeError("Save the PCB before recording measurements.")
                write_json(self._measurement_path, updated)
            self.measurements = updated
            return "Measurement saved." if self.mode != "demo" else "Demo measurement recorded for this session."
