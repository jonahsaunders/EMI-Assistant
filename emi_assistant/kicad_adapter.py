"""KiCad IPC boundary: live snapshots, selection, and conservative edit preflight.

Verified against the official kicad-python 0.8.0 package (KiCad 10 API).
No KiCad/GUI import or connection occurs until a live operation is requested.

Automatic edits require KiCad 10's native SaveCopyOfDocument plus matching
kicad-cli DRC. SaveCopyOfDocument calls SaveProjectLocalSettings -> SaveProject:
the UI must disclose that explicit Apply saves current project settings.
Analyze, locate, and advisory saved-context DRC never invoke that command.
The PCB itself remains unsaved and the via edit is one native Undo operation.

Sources:
https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/
https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/files.cpp
https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/pcbnew_config.cpp
https://docs.kicad.org/10.0/en/cli/cli.html
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Protocol
import uuid

from .models import BoardSnapshot, Finding
from .parser import parse_board, _sexpr


@dataclass(frozen=True)
class ViaCandidate:
    position: tuple[float, float]
    net: str
    layers: tuple[str, str]
    diameter: float
    drill: float
    reference_layers: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return sha256(repr(self).encode()).hexdigest()


@dataclass(frozen=True)
class ExactValidation:
    """Receipt from a complete, native, exact-live-state validation backend.

The backend must check a candidate against all current native rules, isolation,
all copper/holes/keepouts, refill connectivity, and compare full DRC identities.
It must retain an opaque rules token and revalidate it through ``still_valid``.
This receipt is never constructed from the advisory saved-context DRC helper.
"""
    board_fingerprint: str
    candidate_digest: str
    rules_token: Any
    item_id: str = ""


class ExactStateValidator(Protocol):
    def available(self, adapter: "KiCadAdapter") -> tuple[bool, str]: ...
    def validate(self, adapter: "KiCadAdapter", snapshot: BoardSnapshot,
                 candidate: ViaCandidate) -> ExactValidation: ...
    def still_valid(self, adapter: "KiCadAdapter", receipt: ExactValidation) -> bool: ...


def _rules_digest(project: bytes, rules: bytes | None) -> str:
    value = json.loads(project)
    # KiCad changes only this bookkeeping filename when exporting a project copy.
    if isinstance(value.get("meta"), dict):
        value["meta"].pop("filename", None)
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                  + b"\0CUSTOM_RULES\0" + (rules if rules is not None else b"<absent>")).hexdigest()


def _append_via(text: str, candidate: ViaCandidate, item_id: str) -> str:
    """Append only one native via form; leave every original byte unchanged."""
    root = _sexpr(text)
    net_forms = [x for x in root[1:] if isinstance(x, list) and x and x[0] == "net"]
    numeric = [n for n in net_forms if len(n) == 3 and n[2] == candidate.net and n[1].isdigit()]
    named = [n for n in net_forms if len(n) == 2 and n[1] == candidate.net]
    if len(numeric) == 1:
        net_token = numeric[0][1]
    elif len(named) == 1:
        net_token = json.dumps(candidate.net, ensure_ascii=False)
    else:
        raise RuntimeError("The board's net encoding cannot be validated. Place this via manually.")
    # Locate the top-level closing parenthesis while respecting strings/comments.
    depth, quoted, escaped, comment, close = 0, False, False, False, -1
    for index, char in enumerate(text):
        if comment:
            if char == "\n":
                comment = False
            continue
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == ";":
            comment = True
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close = index
                break
    if close < 0:
        raise RuntimeError("The current board could not be prepared for validation.")
    x, y = candidate.position
    form = (f'\n  (via (at {x:.6f} {y:.6f}) (size {candidate.diameter:.6f}) '
            f'(drill {candidate.drill:.6f}) (layers "F.Cu" "B.Cu") '
            f'(net {net_token}) (uuid "{item_id}"))\n')
    result = text[:close] + form + text[close:]
    parsed = parse_board(result)
    matched = [v for v in parsed.vias if v.id == item_id]
    if len(matched) != 1 or matched[0].net != candidate.net:
        raise RuntimeError("The proposed via could not be verified in the temporary board.")
    return result


def _drc_counts(report: dict[str, Any]) -> Counter:
    """Compare full violation identity, including positions and multiplicity."""
    if not isinstance(report, dict):
        raise RuntimeError("KiCad returned an invalid DRC report.")
    counts: Counter = Counter()
    for key in ("violations", "unconnected_items", "schematic_parity"):
        entries = report.get(key, [])
        if key != "schematic_parity" and key not in report:
            raise RuntimeError("KiCad returned an incomplete DRC report.")
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise RuntimeError("KiCad returned an unsupported DRC report.")
        for entry in entries:
            value = dict(entry)
            if isinstance(value.get("items"), list):
                value["items"] = sorted(value["items"], key=lambda i: json.dumps(i, sort_keys=True))
            counts[(key, json.dumps(value, sort_keys=True, separators=(",", ":")))] += 1
    return counts


def _run_native_drc(executable: Path, pcb: Path, *, save_refill: bool) -> dict[str, Any]:
    report_path = pcb.parent / (pcb.stem + "-drc.json")
    args = [str(executable), "pcb", "drc", "--format", "json", "--units", "mm",
            "--severity-all", "--severity-exclusions", "--all-track-errors", "--refill-zones",
            "--exit-code-violations", "--output", str(report_path)]
    if save_refill:
        args.append("--save-board")
    args.append(str(pcb))
    proc = subprocess.run(args, cwd=str(pcb.parent), capture_output=True, text=True,
                          timeout=120, check=False)
    if proc.returncode not in (0, 5) or not report_path.is_file():
        raise RuntimeError("KiCad DRC could not finish. Run Inspect > Design Rules Checker in KiCad.")
    result = json.loads(report_path.read_text(encoding="utf-8"))
    _drc_counts(result)
    # Rule parse errors can be printed with a nominally successful process status.
    if re.search(r"\b(error|failed|unable|cannot)\b", proc.stderr, flags=re.I):
        raise RuntimeError("KiCad reported a problem while checking design rules. Run DRC in KiCad.")
    return result


class NativeSnapshotValidator:
    """KiCad 10 validation via exact native project copies and the shipped CLI."""
    def __init__(self):
        self._executable: Path | None = None

    def available(self, adapter: "KiCadAdapter") -> tuple[bool, str]:
        board = adapter._assert_same_document()
        if not adapter.version.startswith("10."):
            return False, "Automatic changes require the verified KiCad 10 API. Place the via manually."
        if not all(callable(getattr(board, name, None)) for name in (
                "save_as", "get_as_string", "begin_commit", "push_commit", "drop_commit", "get_nets")):
            return False, "This KiCad connection cannot validate an edit. Place the via manually."
        name, directory, project = adapter._document_key or ("", "", "")
        if not directory or not project or Path(name).stem != project:
            return False, "Automatic changes need a PCB and project with the same name. Place the via manually."
        if not (Path(directory) / (project + ".kicad_pro")).is_file():
            return False, "Save the KiCad project once before applying a via."
        try:
            executable = Path(adapter._client.get_kicad_binary_path("kicad-cli"))
            if not executable.is_absolute() or not executable.is_file():
                raise ValueError("missing CLI")
            self._executable = executable
        except Exception:
            return False, "KiCad's DRC program is unavailable. Place this via in KiCad, then run DRC."
        return True, "Applying saves current project settings; the via is one Undo step."

    def _capture(self, adapter: "KiCadAdapter", directory: Path,
                 expected_fingerprint: str) -> tuple[Path, str]:
        board = adapter._assert_same_document()
        source = Path(adapter._path())
        source_rules = source.with_suffix(".kicad_dru")
        original_rules = source_rules.read_bytes() if source_rules.is_file() else None
        if original_rules and b"${" in original_rules:
            raise RuntimeError("Custom rules depend on project paths or variables. Place this via manually and run DRC.")
        snapshot_path = directory / "snapshot.kicad_pcb"
        if snapshot_path.resolve() == source.resolve():
            raise RuntimeError("The validation copy must not replace the original PCB.")
        # This is an explicit Apply action. KiCad also saves current project settings.
        board.save_as(str(snapshot_path), overwrite=False, include_project=True)
        if not snapshot_path.is_file() or not snapshot_path.with_suffix(".kicad_pro").is_file():
            raise RuntimeError("KiCad did not export a complete project copy. Place the via manually.")
        exported_rules_path = snapshot_path.with_suffix(".kicad_dru")
        exported_rules = exported_rules_path.read_bytes() if exported_rules_path.is_file() else None
        current_rules = source_rules.read_bytes() if source_rules.is_file() else None
        if original_rules != exported_rules or current_rules != original_rules:
            raise RuntimeError("Custom rules changed or were not copied completely. Recheck and try again.")
        current = adapter.snapshot()
        if current.fingerprint != expected_fingerprint:
            raise RuntimeError("The board changed while copying project settings. Recheck and try again.")
        # Use the exact analyzed IPC string, preserving unsaved geometry and every
        # native field; the native export supplies its complete current project.
        snapshot_path.write_text(adapter._last_text, encoding="utf-8")
        digest = _rules_digest(snapshot_path.with_suffix(".kicad_pro").read_bytes(), exported_rules)
        return snapshot_path, digest

    def validate(self, adapter: "KiCadAdapter", snapshot: BoardSnapshot,
                 candidate: ViaCandidate) -> ExactValidation:
        available, reason = self.available(adapter)
        if not available or self._executable is None:
            raise RuntimeError(reason)
        version = subprocess.run([str(self._executable), "version"], capture_output=True,
                                 text=True, timeout=10, check=False)
        cli_version = re.search(r"\b(\d+\.\d+\.\d+)\b", version.stdout)
        editor_version = re.search(r"\b(\d+\.\d+\.\d+)\b", adapter.version)
        if version.returncode or not cli_version or not editor_version or cli_version[1] != editor_version[1]:
            raise RuntimeError("The KiCad editor and DRC program versions differ. Place this via manually.")
        with tempfile.TemporaryDirectory(prefix="emi-apply-") as directory:
            pcb, rules_digest = self._capture(adapter, Path(directory), snapshot.fingerprint)
            baseline_text = pcb.read_text(encoding="utf-8")
            baseline = _run_native_drc(self._executable, pcb, save_refill=False)
            candidate_id = str(uuid.uuid4())
            pcb.write_text(_append_via(baseline_text, candidate, candidate_id), encoding="utf-8")
            after = _run_native_drc(self._executable, pcb, save_refill=True)
            added = _drc_counts(after) - _drc_counts(baseline)
            if added:
                raise RuntimeError("This via introduces a new design-rule or connectivity issue. "
                                   "Choose another position in KiCad; no via was added.")
            refilled = parse_board(pcb.read_text(encoding="utf-8"))
            actual = [v for v in refilled.vias if v.id == candidate_id]
            if (len(actual) != 1 or actual[0].net != candidate.net
                    or actual[0].position != candidate.position
                    or actual[0].diameter != candidate.diameter or actual[0].drill != candidate.drill):
                raise RuntimeError("KiCad's validated via differs from the proposal. Place it manually.")
            # Repeat contact checks against actual refilled native copper. Exclude
            # this one proposed via from the pre-existing-via collision test.
            refilled.vias = [v for v in refilled.vias if v.id != candidate_id]
            probe = Finding("validation", "return-via", "", "", "", "", "", [], candidate.position,
                            preview={"kind": "via", "position": candidate.position, "net": candidate.net,
                                     "layers": candidate.layers, "diameter": candidate.diameter, "drill": candidate.drill,
                                     "reference_layers": candidate.reference_layers})
            adapter._candidate(probe, refilled)
        return ExactValidation(snapshot.fingerprint, candidate.digest, rules_digest, candidate_id)

    def still_valid(self, adapter: "KiCadAdapter", receipt: ExactValidation) -> bool:
        with tempfile.TemporaryDirectory(prefix="emi-rules-check-") as directory:
            _, digest = self._capture(adapter, Path(directory), receipt.board_fingerprint)
        return digest == receipt.rules_token


def _id(item: Any) -> str:
    value = getattr(item, "id", None)
    return str(getattr(value, "value", value) or "")


class KiCadAdapter:
    def __init__(self, *, client: Any = None, socket_path: str | None = None,
                 token: str | None = None, validator: ExactStateValidator | None = None):
        self._client = client
        self._board = None
        self._socket_path = socket_path
        self._token = token
        self._validator = validator if validator is not None else NativeSnapshotValidator()
        self._document_key: tuple[str, str, str] | None = None
        self._last_snapshot: BoardSnapshot | None = None
        self._last_text = ""
        self._original_selection: list[Any] | None = None
        self._last_selection_ids: set[str] = set()
        self.version = ""

    def connect(self) -> None:
        if self._board is not None:
            return
        try:
            from kipy import KiCad
            from kipy.proto.common.types import DocumentType
        except ImportError as exc:
            raise RuntimeError("KiCad connection components are missing. Recreate EMI Assistant's "
                               "plugin environment in KiCad Preferences > Plugins.") from exc
        if self._client is None:
            endpoint = self._socket_path or os.environ.get("KICAD_API_SOCKET")
            token = self._token if self._token is not None else os.environ.get("KICAD_API_TOKEN")
            if not endpoint:
                raise RuntimeError("Open your board in KiCad, then click Analyze EMI on its toolbar. "
                                   "Launching there connects to the correct board. You can also use Open PCB here.")
            self._client = KiCad(socket_path=endpoint, kicad_token=token or "",
                                 client_name="emi-assistant-" + uuid.uuid4().hex[:12], timeout_ms=5000)
        try:
            version = self._client.get_version()
            if int(version.major) < 10:
                raise RuntimeError("Live integration requires KiCad 10 or newer. Use Open PCB for this board.")
            self.version = str(getattr(version, "full_version", version))
            docs = list(self._client.get_open_documents(DocumentType.DOCTYPE_PCB))
            if len(docs) != 1:
                raise RuntimeError("Open one PCB in this KiCad editor, then click Analyze EMI again.")
            board = self._client.get_board()
            for method in ("get_as_string", "get_selection", "add_to_selection", "clear_selection"):
                if not callable(getattr(board, method, None)):
                    raise RuntimeError("This KiCad connection lacks " + method + ". Update KiCad and the plugin environment.")
            key = self._key(board.document)
            if key != self._key(docs[0]):
                raise RuntimeError("The active board changed while connecting. Click Analyze EMI again.")
            self._board, self._document_key = board, key
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Could not connect to the PCB editor. Enable KiCad Preferences > Plugins > "
                               "Enable IPC API Server, close any active routing/dialog tool, and try again.") from exc

    @staticmethod
    def _key(document: Any) -> tuple[str, str, str]:
        project = getattr(document, "project", None)
        return (str(getattr(document, "board_filename", "")),
                str(getattr(project, "path", "")), str(getattr(project, "name", "")))

    def _assert_same_document(self) -> Any:
        self.connect()
        from kipy.proto.common.types import DocumentType
        try:
            docs = list(self._client.get_open_documents(DocumentType.DOCTYPE_PCB))
        except Exception as exc:
            raise RuntimeError("KiCad is busy or disconnected. Finish the current tool and recheck.") from exc
        if len(docs) != 1 or self._key(docs[0]) != self._document_key:
            raise RuntimeError("A different PCB is now open. Reopen EMI Assistant from that board's toolbar.")
        return self._board

    def _path(self) -> str:
        filename, project_dir, _ = self._document_key or ("", "", "")
        if not filename:
            return ""
        if Path(filename).is_absolute():
            return filename
        return str(Path(project_dir) / filename) if project_dir else filename

    def snapshot(self) -> BoardSnapshot:
        board = self._assert_same_document()
        try:
            text = board.get_as_string()
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty board snapshot")
            result = parse_board(text, self._path())
        except Exception as exc:
            raise RuntimeError("Could not read the current PCB. Finish the current KiCad tool, then recheck.") from exc
        self._last_text, self._last_snapshot = text, result
        return result

    def _items_for_ids(self, board: Any, ids: set[str]) -> list[Any]:
        found: dict[str, Any] = {}
        # Official KiCad 10 accepts protobuf KIID, not UUID strings.
        direct = getattr(board, "get_items_by_id", None)
        if callable(direct):
            try:
                from kipy.proto.common.types import KIID
                for item in direct([KIID(value=value) for value in sorted(ids)]):
                    if _id(item) in ids:
                        found[_id(item)] = item
            except Exception:
                # Enumeration is supported by older IPC server patch versions.
                pass
        if set(found) != ids:
            for name in ("get_tracks", "get_vias", "get_pads", "get_footprints", "get_zones",
                         "get_shapes", "get_text", "get_groups", "get_dimensions",
                         "get_barcodes", "get_reference_images"):
                getter = getattr(board, name, None)
                if not callable(getter):
                    continue
                try:
                    for item in getter():
                        if _id(item) in ids:
                            found[_id(item)] = item
                except Exception:
                    continue
                if set(found) == ids:
                    break
        return list(found.values())

    def locate(self, finding: Finding) -> str:
        board = self._assert_same_document()
        items = self._items_for_ids(board, set(finding.item_ids))
        if not items:
            raise RuntimeError("These items changed or were removed. Recheck the board to update this finding.")
        previous = list(board.get_selection())
        try:
            # Canonical file IDs differ from IPC enums; never use PCB numeric IDs.
            if finding.layer:
                from kipy.util.board_layer import layer_from_canonical_name
                from kipy.proto.board.board_types_pb2 import BoardLayer
                layer = layer_from_canonical_name(finding.layer)
                if layer != BoardLayer.BL_UNKNOWN:
                    visible = list(board.get_visible_layers())
                    if layer not in visible:
                        board.set_visible_layers([*visible, layer])
                    board.set_active_layer(layer)
            board.clear_selection()
            board.add_to_selection(items)
        except Exception as exc:
            try:
                board.clear_selection()
                if previous:
                    board.add_to_selection(previous)
            except Exception:
                pass
            raise RuntimeError("Could not highlight these items. Finish the current KiCad tool, then try again.") from exc
        if self._original_selection is None:
            self._original_selection = previous
        self._last_selection_ids = {_id(item) for item in items}
        return "Highlighted in KiCad. Use Zoom to Selection in the PCB editor for a closer view."

    def restore_selection(self) -> None:
        """Restore only if the user has not changed the assistant's selection."""
        if self._original_selection is None:
            return
        board = self._assert_same_document()
        if {_id(item) for item in board.get_selection()} == self._last_selection_ids:
            board.clear_selection()
            remaining = self._items_for_ids(board, {_id(x) for x in self._original_selection})
            if remaining:
                board.add_to_selection(remaining)
        self._original_selection = None

    def can_apply(self, finding: Finding) -> tuple[bool, str]:
        if finding.preview.get("kind") != "via":
            return False, "Follow the highlighted routing or placement suggestion in KiCad."
        if self._validator is None:
            return False, "Place this via in KiCad, then run DRC. Automatic validation is unavailable."
        try:
            self._assert_same_document()
            if self._last_snapshot is not None:
                self._candidate(finding, self._last_snapshot)
            return self._validator.available(self)
        except RuntimeError as exc:
            return False, str(exc)
        except Exception:
            return False, "Reconnect to KiCad and recheck this board."

    @staticmethod
    def _candidate(finding: Finding, snapshot: BoardSnapshot) -> ViaCandidate:
        p = finding.preview
        if p.get("kind") != "via":
            raise RuntimeError("This suggestion needs manual routing or placement in KiCad.")
        try:
            position = tuple(round(float(value), 6) for value in p["position"])
            diameter, drill = round(float(p["diameter"]), 6), round(float(p["drill"]), 6)
            layers = tuple(p["layers"])
            references = tuple(p.get("reference_layers", ()))
            net = str(p["net"])
            if (len(position) != 2 or not all(math.isfinite(v) for v in (*position, diameter, drill))
                    or not 0 < drill < diameter or diameter > 10 or layers != ("F.Cu", "B.Cu")
                    or not net or net not in snapshot.nets
                    or any(layer not in snapshot.layers for layer in references)):
                raise ValueError("invalid via")
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("This via suggestion is incomplete. Recheck, or place a return via manually in KiCad.") from exc
        # Conservative geometric connectivity check. Full native DRC remains mandatory.
        from shapely.geometry import Point
        from .geometry import CopperIndex
        annulus = Point(position).buffer(diameter / 2 + 0.001)
        copper = CopperIndex(snapshot)
        connected_layers = []
        for layer in snapshot.layers:
            if copper.get(layer, net).covers(annulus):
                connected_layers.append(layer)
        if len(connected_layers) < 2 or not set(references).issubset(connected_layers):
            raise RuntimeError("The proposed via does not sit fully inside filled same-net copper on two layers. "
                               "Choose its position manually in KiCad.")
        if any(math.dist(v.position, position) < (v.diameter + diameter) / 2 for v in snapshot.vias):
            raise RuntimeError("A via is already too close to this position. Recheck the suggestion.")
        return ViaCandidate(position, net, layers, diameter, drill, references)

    def apply_return_via(self, finding: Finding, expected_fingerprint: str) -> str:
        try:
            return self._apply_return_via(finding, expected_fingerprint)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("KiCad could not complete this operation. Finish the current tool, then recheck the PCB.") from exc

    def _apply_return_via(self, finding: Finding, expected_fingerprint: str) -> str:
        snapshot = self.snapshot()
        original_text = self._last_text
        if snapshot.fingerprint != expected_fingerprint:
            raise RuntimeError("The board changed. Recheck before applying this suggestion.")
        candidate = self._candidate(finding, snapshot)
        available, reason = self.can_apply(finding)
        if not available or self._validator is None:
            raise RuntimeError(reason)
        board = self._assert_same_document()
        for name in ("begin_commit", "create_items", "push_commit", "drop_commit", "get_nets"):
            if not callable(getattr(board, name, None)):
                raise RuntimeError("This KiCad version cannot apply this change safely. Place the via manually.")
        try:
            receipt = self._validator.validate(self, snapshot, candidate)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Native validation could not finish. No via was added. Run DRC in KiCad and try again.") from exc
        if (not isinstance(receipt, ExactValidation) or receipt.board_fingerprint != snapshot.fingerprint
                or receipt.candidate_digest != candidate.digest):
            raise RuntimeError("The design-rule check did not validate this exact suggestion. Nothing was changed.")
        if self.snapshot().fingerprint != snapshot.fingerprint or not self._validator.still_valid(self, receipt):
            raise RuntimeError("The board or its rules changed during validation. Recheck and try again.")
        from kipy.board_types import Via
        from kipy.geometry import Vector2
        from kipy.proto.board.board_types_pb2 import ViaType
        nets = [net for net in board.get_nets() if net.name == candidate.net]
        if len(nets) != 1:
            raise RuntimeError("The return net changed during validation. Recheck the board.")
        via = Via()
        # Match the exact object identity used by native candidate DRC. KiCad
        # wrappers expose a mutable protobuf KIID even though the property has
        # no Python setter. Explicit UUIDs also avoid nil IDs on API servers.
        via.id.value = receipt.item_id or str(uuid.uuid4())
        via.position = Vector2.from_xy_mm(*candidate.position)
        via.type = ViaType.VT_THROUGH
        via.diameter = round(candidate.diameter * 1_000_000)
        via.drill_diameter = round(candidate.drill * 1_000_000)
        via.net = nets[0]
        commit = board.begin_commit()
        try:
            # Recheck after acquiring KiCad's transaction, before the first write.
            if self.snapshot().fingerprint != snapshot.fingerprint or not self._validator.still_valid(self, receipt):
                raise RuntimeError("The board or its rules changed. Recheck before applying.")
            created = list(board.create_items([via]))
            if len(created) != 1 or not _id(created[0]):
                raise RuntimeError("KiCad did not create the via.")
            actual = created[0]
            if (actual.net.name != candidate.net or actual.diameter != via.diameter
                    or actual.drill_diameter != via.drill_diameter or actual.type != via.type
                    or actual.position.x != via.position.x or actual.position.y != via.position.y
                    or actual.padstack.drill.start_layer != via.padstack.drill.start_layer
                    or actual.padstack.drill.end_layer != via.padstack.drill.end_layer
                    or (receipt.item_id and _id(actual) != receipt.item_id)):
                raise RuntimeError("KiCad changed the proposed via dimensions or net.")
            # A native commit groups Undo but does not promise a user-input lock.
            # Detect concurrent board edits before pushing the transaction.
            after = _sexpr(board.get_as_string())
            actual_id = _id(actual)
            removed = []
            for node in after[1:]:
                if isinstance(node, list) and node and node[0] == "via":
                    identifiers = [child[1] for child in node[1:] if isinstance(child, list)
                                   and len(child) == 2 and child[0] in ("uuid", "tstamp")]
                    if actual_id in identifiers:
                        removed.append(node)
            for node in removed:
                after.remove(node)
            # KiCad 10 stages additions until push_commit; some server versions
            # include staged items in serialization. Accept either exact state.
            if len(removed) > 1 or after != _sexpr(original_text):
                raise RuntimeError("The PCB changed during the edit; the proposed via was cancelled.")
            board.push_commit(commit, "EMI Assistant: add validated return via")
        except Exception as exc:
            try:
                board.drop_commit(commit)
            except Exception as rollback_error:
                raise RuntimeError("KiCad disconnected during the edit. Check the PCB and use Undo if the via is present.") from rollback_error
            raise RuntimeError("The change was cancelled: " + str(exc)) from exc
        self._last_snapshot = None
        return "Return via added. Undo in KiCad reverses this change in one step."

    def run_saved_drc(self) -> dict[str, Any]:
        """Native DRC on current PCB geometry with SAVED project settings.

        This read-only diagnostic never certifies an automatic edit. No save,
        save_as, editor action, or live zone refill is invoked. The CLI refills
        zones in its own temporary in-memory board, using its shipped DRC engine.
        """
        snapshot = self.snapshot()
        board_path = Path(snapshot.path)
        if not snapshot.path or not board_path.is_absolute():
            raise RuntimeError("Save this project in KiCad once before running its saved-rule DRC.")
        _, project_dir, project_name = self._document_key or ("", "", "")
        project_path = Path(project_dir) / (project_name + ".kicad_pro")
        if not project_name or not project_path.is_file():
            project_path = board_path.with_suffix(".kicad_pro")
        if not project_path.is_file():
            raise RuntimeError("The saved .kicad_pro file is missing. Run DRC directly in KiCad.")
        rules_path = project_path.with_suffix(".kicad_dru")
        try:
            executable = Path(self._client.get_kicad_binary_path("kicad-cli"))
            if not executable.is_absolute() or not executable.is_file():
                raise ValueError("missing native CLI")
            files = {".kicad_pro": project_path.read_bytes()}
            if rules_path.is_file():
                files[".kicad_dru"] = rules_path.read_bytes()
            # Parse project JSON before spawning KiCad; retain all native values.
            json.loads(files[".kicad_pro"])
            with tempfile.TemporaryDirectory(prefix="emi-drc-") as directory:
                pcb = Path(directory) / "snapshot.kicad_pcb"
                pcb.write_text(self._last_text, encoding="utf-8")
                for suffix, data in files.items():
                    pcb.with_suffix(suffix).write_bytes(data)
                report_path = Path(directory) / "drc.json"
                proc = subprocess.run([str(executable), "pcb", "drc", "--format", "json",
                    "--units", "mm", "--severity-all", "--severity-exclusions", "--all-track-errors",
                    "--refill-zones", "--exit-code-violations", "--output", str(report_path), str(pcb)],
                    cwd=directory, capture_output=True, text=True, timeout=120, check=False)
                if proc.returncode not in (0, 5) or not report_path.is_file():
                    raise RuntimeError("KiCad DRC could not finish. Run Inspect > Design Rules Checker in KiCad.")
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if not isinstance(report, dict) or not isinstance(report.get("violations"), list):
                    raise RuntimeError("KiCad returned an unsupported DRC report. Run DRC in KiCad.")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Saved-rule DRC is unavailable. Run Inspect > Design Rules Checker in KiCad.") from exc
        return {"report": report, "board_fingerprint": snapshot.fingerprint,
                "coverage": "Current PCB geometry with saved project and custom rules; unsaved rule changes and schematic parity are not checked.",
                "can_apply": False}
