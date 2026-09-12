"""IPC boundary tests use real kipy wrappers with a fake editor transport.

These verify API shapes and refusal/rollback behavior. They do not claim that
KiCad or its DRC executable ran in this development environment.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

pytest.importorskip("kipy")
from kipy.board_types import Net, Via
from kipy.geometry import Vector2
from kipy.proto.common.types import DocumentSpecifier, DocumentType, KIID
from kipy.proto.board.board_types_pb2 import BoardLayer

from emi_assistant.kicad_adapter import (KiCadAdapter, NativeSnapshotValidator, ExactValidation,
    ViaCandidate, _append_via, _drc_counts, _rules_digest, _run_native_drc)
from emi_assistant.models import Finding
from emi_assistant.parser import parse_board


BOARD = '''(kicad_pcb (version 20240108) (generator pcbnew)
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "") (net 1 "GND") (net 2 "CLK")
  (segment (start 10 10) (end 20 10) (width 0.25) (layer "F.Cu") (net 2)
    (uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (uuid "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    (polygon (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20)))
    (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20))))
  (zone (net 1) (net_name "GND") (layer "B.Cu")
    (uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
    (polygon (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20)))
    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20))))
  (gr_rect (start 0 0) (end 30 20) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
)'''


def finding(**preview):
    data = {"kind": "via", "position": [14, 12], "net": "GND", "layers": ["F.Cu", "B.Cu"],
            "diameter": 0.6, "drill": 0.3, "reference_layers": ["F.Cu", "B.Cu"]}
    data.update(preview)
    return Finding("return-via-a", "return-via", "Return connection", "high", "medium", "", "",
                   ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"], (15, 10), "B.Cu", preview=data)


class FakeBoard:
    def __init__(self, project_dir):
        self.document = DocumentSpecifier(type=DocumentType.DOCTYPE_PCB, board_filename="board.kicad_pcb")
        self.document.project.path = str(project_dir)
        self.document.project.name = "board"
        self.name = "board.kicad_pcb"
        self.text = BOARD
        self.items = {}
        self.selection = []
        self.visible = [BoardLayer.BL_F_Cu]
        self.active = BoardLayer.BL_F_Cu
        self.begin_calls = self.push_calls = self.drop_calls = self.create_calls = 0
        self.save_calls = self.copy_calls = 0
        self.fail_create = False
        self.clamp_via = False
        self.before = ""
        self.project_json = '{"meta":{"filename":"snapshot.kicad_pro"},"board":{"rules":{}}}'

    def get_as_string(self): return self.text
    def get_selection(self): return self.selection.copy()
    def clear_selection(self): self.selection.clear()
    def add_to_selection(self, items): self.selection.extend(items)
    def get_visible_layers(self): return self.visible.copy()
    def set_visible_layers(self, layers): self.visible = list(layers)
    def set_active_layer(self, layer): self.active = layer
    def get_items_by_id(self, ids):
        assert all(isinstance(i, KIID) for i in ids)
        return [self.items[i.value] for i in ids if i.value in self.items]
    def get_tracks(self): return list(self.items.values())
    def get_pads(self): return []
    def get_nets(self): return [Net(name="GND"), Net(name="CLK")]
    def begin_commit(self):
        self.begin_calls += 1
        self.before = self.text
        return object()
    def create_items(self, vias):
        self.create_calls += 1
        if self.fail_create:
            raise RuntimeError("creation rejected")
        item = Via(vias[0].proto)
        if not item.id.value:
            item.id.value = str(uuid.uuid4())
        if self.clamp_via:
            item.drill_diameter += 1000
        candidate = ViaCandidate((item.position.x / 1e6, item.position.y / 1e6), item.net.name,
                                 ("F.Cu", "B.Cu"), item.diameter / 1e6, item.drill_diameter / 1e6)
        self.text = _append_via(self.text, candidate, item.id.value)
        return [item]
    def push_commit(self, commit, message=""):
        self.push_calls += 1
        assert "return via" in message
    def drop_commit(self, commit):
        self.drop_calls += 1
        self.text = self.before
    def save(self):
        self.save_calls += 1
        raise AssertionError("must never save the live PCB")
    def save_as(self, path, overwrite=False, include_project=True):
        self.copy_calls += 1
        assert not overwrite and include_project
        Path(path).write_text(self.text)
        Path(path).with_suffix(".kicad_pro").write_text(self.project_json)


class FakeClient:
    def __init__(self, board):
        self.board = board
        self.docs = [board.document]
        self.cli = "/nonexistent/kicad-cli"
    def get_version(self): return SimpleNamespace(major=10, minor=0, patch=5, full_version="10.0.5")
    def get_open_documents(self, kind):
        assert kind == DocumentType.DOCTYPE_PCB
        return self.docs
    def get_board(self): return self.board
    def get_kicad_binary_path(self, binary):
        assert binary == "kicad-cli"
        return self.cli


class ApprovedValidator:
    def available(self, adapter): return True, "ready"
    def validate(self, adapter, snapshot, candidate):
        return ExactValidation(snapshot.fingerprint, candidate.digest, "rules-1")
    def still_valid(self, adapter, receipt): return receipt.rules_token == "rules-1"


@pytest.fixture
def bridge(tmp_path):
    board = FakeBoard(tmp_path)
    (tmp_path / "board.kicad_pro").write_text('{"board":{"rules":{}}}')
    (tmp_path / "board.kicad_pcb").write_text(BOARD.replace("20 10", "25 10"))
    client = FakeClient(board)
    adapter = KiCadAdapter(client=client, validator=ApprovedValidator())
    return adapter, board, client


def test_live_snapshot_uses_unsaved_ipc_not_disk(bridge):
    adapter, board, _ = bridge
    result = adapter.snapshot()
    assert result.tracks[0].end == (20, 10)
    assert result.fingerprint == parse_board(BOARD).fingerprint
    assert board.save_calls == board.copy_calls == 0


def test_missing_target_does_not_guess_another_editors_socket(monkeypatch):
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    with pytest.raises(RuntimeError, match="toolbar"):
        KiCadAdapter().snapshot()


def test_multiple_documents_fail_closed(bridge):
    adapter, board, client = bridge
    client.docs = [board.document, board.document]
    with pytest.raises(RuntimeError, match="one PCB"):
        adapter.snapshot()


def test_switching_board_does_not_retarget_existing_findings(bridge):
    adapter, board, client = bridge
    adapter.snapshot()
    replacement = deepcopy(board.document)
    replacement.project.path = "/different-project"
    client.docs = [replacement]
    with pytest.raises(RuntimeError, match="different PCB"):
        adapter.locate(finding())
    assert not board.selection


def test_locate_sends_kiid_and_uses_ipc_layer_enum(bridge):
    adapter, board, _ = bridge
    item = SimpleNamespace(id=KIID(value=finding().item_ids[0]))
    old = SimpleNamespace(id=KIID(value=str(uuid.uuid4())))
    board.items = {item.id.value: item, old.id.value: old}
    board.selection = [old]
    assert "Highlighted" in adapter.locate(finding())
    assert board.selection == [item]
    assert board.active == BoardLayer.BL_B_Cu
    assert BoardLayer.BL_F_Cu in board.visible and BoardLayer.BL_B_Cu in board.visible
    adapter.restore_selection()
    assert board.selection == [old]
    assert board.save_calls == board.copy_calls == 0


def test_restore_preserves_users_new_selection(bridge):
    adapter, board, _ = bridge
    item = SimpleNamespace(id=KIID(value=finding().item_ids[0]))
    board.items[item.id.value] = item
    adapter.locate(finding())
    board.selection = []
    adapter.restore_selection()
    assert board.selection == []


def test_locate_falls_back_to_enumerating_pads(bridge):
    adapter, board, _ = bridge
    pad = SimpleNamespace(id=KIID(value=finding().item_ids[0]))
    board.get_items_by_id = lambda ids: (_ for _ in ()).throw(RuntimeError("not supported"))
    board.get_pads = lambda: [pad]
    adapter.locate(finding())
    assert board.selection == [pad]


def test_success_uses_one_native_commit(bridge):
    adapter, board, _ = bridge
    fingerprint = adapter.snapshot().fingerprint
    assert "one step" in adapter.apply_return_via(finding(), fingerprint)
    assert (board.begin_calls, board.create_calls, board.push_calls, board.drop_calls) == (1, 1, 1, 0)
    assert board.save_calls == 0
    via = parse_board(board.text).vias[0]
    assert via.net == "GND" and via.position == (14, 12)


def test_noncardinal_candidate_is_quantized_once_to_native_nanometres(bridge):
    adapter, board, _ = bridge
    adapter.apply_return_via(finding(position=[14.123456789, 12.234567891]), adapter.snapshot().fingerprint)
    assert parse_board(board.text).vias[0].position == (14.123457, 12.234568)


def test_native_staged_creation_is_not_visible_until_push(bridge):
    adapter, board, _ = bridge
    create = board.create_items
    pending = []
    def staged(vias):
        created = create(vias)
        pending.append(board.text)
        board.text = board.before
        return created
    def push(commit, message):
        board.push_calls += 1
        board.text = pending[0]
    board.create_items = staged
    board.push_commit = push
    adapter.apply_return_via(finding(), adapter.snapshot().fingerprint)
    assert board.push_calls == 1 and len(parse_board(board.text).vias) == 1


@pytest.mark.parametrize("preview", [{"position": [float("nan"), 2]}, {"drill": .8},
    {"net": "NOT_A_NET"}, {"layers": ["F.Cu", "In1.Cu"]}, {"position": [29.9, 12]},
    {"reference_layers": ["In1.Cu", "B.Cu"]}])
def test_invalid_candidate_never_begins_a_commit(bridge, preview):
    adapter, board, _ = bridge
    with pytest.raises(RuntimeError):
        adapter.apply_return_via(finding(**preview), adapter.snapshot().fingerprint)
    assert board.begin_calls == board.create_calls == board.push_calls == 0


def test_bridged_copper_contour_keeps_hole_and_accepts_real_plane_area():
    snapshot = parse_board(BOARD)
    bridged = [(0, 0), (30, 0), (30, 20), (0, 20), (0, 0),
               (5, 5), (5, 8), (8, 8), (8, 5), (5, 5), (0, 0)]
    for copper in snapshot.copper:
        copper.points = bridged
    assert KiCadAdapter._candidate(finding(), snapshot).position == (14, 12)
    with pytest.raises(RuntimeError, match="same-net copper"):
        KiCadAdapter._candidate(finding(position=[6.5, 6.5]), snapshot)


def test_stale_analysis_never_begins_a_commit(bridge):
    adapter, board, _ = bridge
    fingerprint = adapter.snapshot().fingerprint
    board.text = board.text.replace("20 10", "21 10")
    with pytest.raises(RuntimeError, match="board changed"):
        adapter.apply_return_via(finding(), fingerprint)
    assert board.begin_calls == 0


def test_stale_rules_never_begins_a_commit(bridge):
    adapter, board, _ = bridge
    adapter._validator.still_valid = lambda *_: False
    with pytest.raises(RuntimeError, match="rules changed"):
        adapter.apply_return_via(finding(), adapter.snapshot().fingerprint)
    assert board.begin_calls == board.create_calls == 0


@pytest.mark.parametrize("failure", ["fail_create", "clamp_via"])
def test_server_failure_or_changed_via_rolls_back(bridge, failure):
    adapter, board, _ = bridge
    setattr(board, failure, True)
    with pytest.raises(RuntimeError, match="cancelled"):
        adapter.apply_return_via(finding(), adapter.snapshot().fingerprint)
    assert (board.begin_calls, board.push_calls, board.drop_calls) == (1, 0, 1)
    assert board.text == BOARD


def test_forged_validation_does_not_write(bridge):
    adapter, board, _ = bridge
    adapter._validator.validate = lambda a, s, c: ExactValidation(s.fingerprint, "different-candidate", "rules-1")
    with pytest.raises(RuntimeError, match="exact suggestion"):
        adapter.apply_return_via(finding(), adapter.snapshot().fingerprint)
    assert board.begin_calls == 0


def test_missing_cli_disables_apply_without_exporting_or_saving(bridge):
    adapter, board, _ = bridge
    adapter._validator = NativeSnapshotValidator()
    adapter.snapshot()
    ok, reason = adapter.can_apply(finding())
    assert not ok and "DRC" in reason
    assert board.save_calls == board.copy_calls == board.begin_calls == 0


def test_lossless_append_ignores_parentheses_in_trailing_comments():
    original = BOARD + '\n; trailing ) () comment\n'
    candidate = ViaCandidate((14, 12), "GND", ("F.Cu", "B.Cu"), .6, .3)
    changed = _append_via(original, candidate, str(uuid.uuid4()))
    assert changed.endswith('\n; trailing ) () comment\n')
    assert len(parse_board(changed).vias) == 1


def test_drc_comparison_distinguishes_positions_and_duplicate_count():
    issue = {"type": "clearance", "description": "Too close", "items": [{"uuid": "a", "pos": {"x": 1, "y": 1}}]}
    old = {"violations": [issue], "unconnected_items": []}
    changed = deepcopy(old)
    changed["violations"][0]["items"][0]["pos"]["x"] = 2
    assert _drc_counts(changed) - _drc_counts(old)
    assert _drc_counts({"violations": [issue, issue], "unconnected_items": []}) - _drc_counts(old)


def test_rules_fingerprint_keeps_design_rules_but_ignores_copy_filename():
    a = b'{"meta":{"filename":"a"},"board":{"rules":{"min_clearance":0.2}}}'
    b = a.replace(b'"a"', b'"b"')
    assert _rules_digest(a, None) == _rules_digest(b, None)
    assert _rules_digest(a, None) != _rules_digest(a.replace(b'0.2', b'0.3'), None)
    assert _rules_digest(a, None) != _rules_digest(a, b'(version 1)')


def test_native_capture_preserves_unsaved_board_and_rejects_missing_rules_copy(bridge, tmp_path):
    adapter, board, _ = bridge
    snapshot = adapter.snapshot()
    (tmp_path / "board.kicad_dru").write_text('(version 1)')
    target = tmp_path / "copy"
    target.mkdir()
    with pytest.raises(RuntimeError, match="not copied"):
        NativeSnapshotValidator()._capture(adapter, target, snapshot.fingerprint)
    assert board.save_calls == 0
    assert board.text == BOARD


def test_native_preflight_checks_before_and_after_then_commits(bridge, tmp_path, monkeypatch):
    import emi_assistant.kicad_adapter as module
    adapter, board, client = bridge
    cli = tmp_path / "kicad-cli"
    cli.write_text("mock only")
    client.cli = str(cli)
    adapter._validator = NativeSnapshotValidator()
    calls = []
    def drc(executable, pcb, *, save_refill):
        assert pcb.with_suffix(".kicad_pro").is_file()
        assert board.text == BOARD  # No live via exists during preflight.
        calls.append((save_refill, len(parse_board(pcb.read_text()).vias)))
        return {"violations": [], "unconnected_items": []}
    monkeypatch.setattr(module, "_run_native_drc", drc)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="10.0.5", stderr=""))
    adapter.apply_return_via(finding(), adapter.snapshot().fingerprint)
    assert calls == [(False, 0), (True, 1)]
    assert board.copy_calls == 3  # initial capture, rule check, check under commit
    assert board.push_calls == 1 and board.save_calls == 0


def test_native_preflight_new_violation_cannot_commit(bridge, tmp_path, monkeypatch):
    import emi_assistant.kicad_adapter as module
    adapter, board, client = bridge
    cli = tmp_path / "kicad-cli"
    cli.write_text("mock only")
    client.cli = str(cli)
    adapter._validator = NativeSnapshotValidator()
    def drc(executable, pcb, *, save_refill):
        return {"violations": [{"type": "clearance", "items": [{"uuid": "new-via"}]}] if save_refill else [],
                "unconnected_items": []}
    monkeypatch.setattr(module, "_run_native_drc", drc)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="10.0.5", stderr=""))
    with pytest.raises(RuntimeError, match="new design-rule"):
        adapter.apply_return_via(finding(), adapter.snapshot().fingerprint)
    assert board.begin_calls == board.create_calls == board.push_calls == 0
    assert board.text == BOARD


def test_native_rule_changes_after_drc_cannot_commit(bridge, tmp_path, monkeypatch):
    import emi_assistant.kicad_adapter as module
    adapter, board, client = bridge
    cli = tmp_path / "kicad-cli"
    cli.write_text("mock only")
    client.cli = str(cli)
    adapter._validator = NativeSnapshotValidator()
    def drc(executable, pcb, *, save_refill):
        if save_refill:
            board.project_json = '{"board":{"rules":{"min_clearance":0.8}}}'
        return {"violations": [], "unconnected_items": []}
    monkeypatch.setattr(module, "_run_native_drc", drc)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="10.0.5", stderr=""))
    with pytest.raises(RuntimeError, match="rules changed"):
        adapter.apply_return_via(finding(), adapter.snapshot().fingerprint)
    assert board.begin_calls == board.create_calls == board.push_calls == 0


def test_concurrent_geometry_edit_is_detected_before_push(bridge):
    adapter, board, _ = bridge
    create = board.create_items
    def changed(vias):
        result = create(vias)
        board.text = board.text.replace("20 10", "21 10")
        return result
    board.create_items = changed
    with pytest.raises(RuntimeError, match="PCB changed"):
        adapter.apply_return_via(finding(), adapter.snapshot().fingerprint)
    assert board.push_calls == 0 and board.drop_calls == 1


def test_native_drc_invocation_uses_argument_list_and_temporary_refill(tmp_path, monkeypatch):
    import emi_assistant.kicad_adapter as module
    board = tmp_path / "snapshot.kicad_pcb"
    board.write_text(BOARD)
    def run(args, **kwargs):
        assert isinstance(args, list)
        assert kwargs.get("shell", False) is False
        assert args[-1] == str(board)
        assert "--refill-zones" in args and "--save-board" in args
        assert "--severity-all" in args and "--all-track-errors" in args
        Path(args[args.index("--output") + 1]).write_text('{"violations": [], "unconnected_items": []}')
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(module.subprocess, "run", run)
    assert _run_native_drc(Path("/trusted/kicad-cli"), board, save_refill=True)["violations"] == []


def test_native_drc_rejects_incomplete_report(tmp_path, monkeypatch):
    import emi_assistant.kicad_adapter as module
    board = tmp_path / "snapshot.kicad_pcb"
    board.write_text(BOARD)
    def run(args, **kwargs):
        Path(args[args.index("--output") + 1]).write_text('{"violations": []}')
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="incomplete DRC"):
        _run_native_drc(Path("/trusted/kicad-cli"), board, save_refill=True)
