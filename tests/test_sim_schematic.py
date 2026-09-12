from pathlib import Path
import threading
from unittest.mock import patch

import pytest

from emi_assistant.models import BoardSnapshot, Footprint, Pad, default_settings
from emi_assistant.sim_schematic import fingerprint_inputs, parse_netlist, prepare


XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<export version="E"><components>
<comp ref="U1"><value>My Custom IC</value><footprint>Mine:Custom</footprint>
<fields><field name="Sim.Library">models/custom.lib</field><field name="Sim.Name">MYCHIP</field>
<field name="Sim.Pins">1=VIN 2=GND</field><field name="Frequency">100 MHz</field></fields>
<libsource lib="MyPersonalLibrary" part="HandMadeSymbol"/>
<sheetpath names="/" tstamps="/root/"/><tstamps>symbol-u1</tstamps></comp>
<comp ref="C1"><value>100n</value><sheetpath names="/" tstamps="/root/"/><tstamp>symbol-c1</tstamp></comp>
</components><nets>
<net code="1" name="/VCC"><node ref="U1" pin="1" pinfunction="VIN" pintype="power_in"/><node ref="C1" pin="1"/></net>
<net code="2" name="GND"><node ref="U1" pin="2"/><node ref="C1" pin="2"/></net>
</nets></export>'''


@pytest.fixture
def board(tmp_path):
    source = tmp_path / 'board.kicad_sch'
    source.write_text('(kicad_sch (version 20250114) (uuid root))')
    def fp(ref, value, stamp):
        return Footprint(ref.lower(), ref, value, (0, 0), [
            Pad(ref + '-1', '1', (0, 0), (1, 1), ['F.Cu'], '/VCC'),
            Pad(ref + '-2', '2', (1, 0), (1, 1), ['F.Cu'], 'GND')],
            {'__kicad_path': '/root/' + stamp})
    return BoardSnapshot('board', str(tmp_path / 'board.kicad_pcb'), 'pcb',
                         footprints=[fp('U1', 'My Custom IC', 'symbol-u1'), fp('C1', '100n', 'symbol-c1')])


def export_ok(command, cwd, log_path, seconds, cancel_event):
    assert command[1:5] == ['sch', 'export', 'netlist', '--format']
    assert command[5] == 'kicadxml'
    assert Path(command[-1]).is_relative_to(cwd)
    Path(command[7]).write_bytes(XML)
    log_path.write_text('Native XML export finished.')
    return 0


def test_custom_symbol_pin_metadata_and_native_connectivity(board):
    components, nets, warnings, missing, aliases = parse_netlist(XML, board, 'root')
    assert not missing
    custom = components[0]
    assert custom['lib_id'] == 'MyPersonalLibrary:HandMadeSymbol'
    assert custom['pins'] == {'1': '/VCC', '2': 'GND'}
    assert custom['properties']['Sim.Pins'] == '1=VIN 2=GND'
    assert custom['pin_metadata']['1']['type'] == 'power_in'
    assert custom['board_match'] is True
    assert len(nets) == 2
    assert aliases == {}


def test_native_export_stages_copy_preserves_sources_and_retains_artifacts(board, tmp_path):
    source = Path(board.path).with_suffix('.kicad_sch')
    before = source.read_bytes()
    with patch('emi_assistant.sim_schematic._export', side_effect=export_ok):
        result = prepare(board, default_settings(), tmp_path / 'job', {'available': True, 'path': '/bin/kicad-cli'})
    assert result['status'] == 'complete'
    assert source.read_bytes() == before
    assert result['source'] == str(source)
    assert len(result['fingerprint']) == 64
    assert Path(result['artifacts'][0]).is_file()
    assert Path(result['artifacts'][1]).is_file()
    assert (tmp_path / 'job/schematic-input/board.kicad_sch').read_bytes() == before


@pytest.mark.parametrize('change', ['value', 'uuid', 'pin', 'missing', 'duplicate'])
def test_stale_saved_schematic_is_not_used_as_matching_circuit(board, change):
    if change == 'value':
        board.footprints[0].value = 'Other IC'
    elif change == 'uuid':
        board.footprints[0].properties['__kicad_path'] = '/root/replaced-symbol'
    elif change == 'pin':
        board.footprints[0].pads[0].net = 'OTHER'
    elif change == 'missing':
        board.footprints.pop(0)
    else:
        board.footprints.append(board.footprints[0])
    components, _, _, missing, _ = parse_netlist(XML, board, 'root')
    assert missing
    assert components[0]['board_match'] is False


def test_net_alias_requires_identical_full_pin_membership(board):
    alias_xml = XML.replace(b'name="/VCC"', b'name="/VCC_alias"')
    components, _, warnings, missing, aliases = parse_netlist(alias_xml, board, 'root')
    assert not missing
    assert aliases == {'/VCC_alias': '/VCC'}
    assert components[0]['pins']['1'] == '/VCC'
    assert components[0]['schematic_pins']['1'] == '/VCC_alias'
    board.footprints[1].pads[0].net = 'SPLIT'
    components, _, _, missing, aliases = parse_netlist(alias_xml, board, 'root')
    assert missing and not aliases
    assert not components[0]['board_match']


def test_hierarchy_and_recursive_model_content_change_fingerprint(board, tmp_path):
    source = Path(board.path).with_suffix('.kicad_sch')
    source.write_text('''(kicad_sch (uuid root)
      (sheet (uuid child) (property "Sheetfile" "child.kicad_sch")))''')
    child = tmp_path / 'child.kicad_sch'
    child.write_text('''(kicad_sch (uuid childroot)
      (symbol (property "Reference" "U1") (property "Sim.Library" "models/main.lib")))''')
    models = tmp_path / 'models'
    models.mkdir()
    (models / 'main.lib').write_text('.include "nested.lib"\n.subckt X a b\n.ends X\n')
    nested = models / 'nested.lib'
    nested.write_text('* first model revision\n')
    first = fingerprint_inputs(board, default_settings())
    nested.write_text('* second model revision\n')
    assert fingerprint_inputs(board, default_settings()) != first
    second = fingerprint_inputs(board, default_settings())
    child.write_text(child.read_text().replace('childroot', 'newchildroot'))
    assert fingerprint_inputs(board, default_settings()) != second


def test_explicit_root_selection_and_project_configuration_fingerprint(board, tmp_path):
    other = tmp_path / 'control.kicad_sch'
    other.write_text('(kicad_sch (uuid root))')
    settings = default_settings()
    settings['simulation'] = {'schematic_path': 'control.kicad_sch'}
    config = other.with_suffix('.kicad_pro')
    config.write_text('{"text_variables":{"VALUE":"1k"}}')
    first = fingerprint_inputs(board, settings)
    config.write_text('{"text_variables":{"VALUE":"2k"}}')
    assert fingerprint_inputs(board, settings) != first
    result = prepare(board, settings, tmp_path / 'job', {})
    assert result['source'] == str(other)
    assert result['status'] == 'needs_information'


def test_missing_cli_does_not_guess_schematic_connectivity(board, tmp_path):
    result = prepare(board, default_settings(), tmp_path / 'job', {})
    assert result['status'] == 'needs_information'
    assert not result['components']
    assert 'CLI' in result['message']


def test_missing_schematic_keeps_layout_available(board, tmp_path):
    Path(board.path).with_suffix('.kicad_sch').unlink()
    result = prepare(board, default_settings(), tmp_path / 'job', {})
    assert result['status'] == 'needs_information'
    assert 'Layout-based' in result['message']


@pytest.mark.parametrize('child', ['../outside.kicad_sch', '/tmp/external.kicad_sch', '${UNRESOLVED}/child.kicad_sch'])
def test_unsafe_or_missing_sheet_fails_before_native_export(board, tmp_path, child):
    source = Path(board.path).with_suffix('.kicad_sch')
    source.write_text('(kicad_sch (uuid root) (sheet (property "Sheetfile" "' + child + '")))')
    with patch('emi_assistant.sim_schematic._export') as export:
        result = prepare(board, default_settings(), tmp_path / 'job', {'available': True, 'path': 'kicad-cli'})
    assert result['status'] == 'needs_information'
    export.assert_not_called()


def test_external_model_requires_exact_explicit_selection(board, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    board.path = str(project / 'board.kicad_pcb')
    source = Path(board.path).with_suffix('.kicad_sch')
    model = tmp_path / 'outside.lib'
    model.write_text('* external model revision 1')
    source.write_text('(kicad_sch (uuid root) (symbol (property "Sim.Library" "../outside.lib")))')
    settings = default_settings()
    denied = prepare(board, settings, tmp_path / 'job', {})
    assert any('outside' in m for m in denied['missing'])
    settings['simulation'] = {'external_model_paths': [str(model)]}
    allowed = prepare(board, settings, tmp_path / 'job', {})
    assert not any('outside' in m for m in allowed['missing'])
    first = allowed['fingerprint']
    model.write_text('* external model revision 2')
    assert fingerprint_inputs(board, settings) != first


def test_no_arbitrary_spice_control_execution(board, tmp_path):
    source = Path(board.path).with_suffix('.kicad_sch')
    source.write_text('(kicad_sch (uuid root) (symbol (property "Sim.Library" "model.lib")))')
    (tmp_path / 'model.lib').write_text('.control\nshell touch DO_NOT_CREATE\n.endc\n')
    with patch('emi_assistant.sim_schematic._export', side_effect=export_ok):
        result = prepare(board, default_settings(), tmp_path / 'job', {'available': True, 'path': 'kicad-cli'})
    assert any('control commands' in warning for warning in result['warnings'])
    assert 'spice_path' not in result
    assert not (tmp_path / 'DO_NOT_CREATE').exists()


@pytest.mark.parametrize('failure', [TimeoutError('timed out'), OSError('missing executable'), ValueError('bad netlist')])
def test_export_failures_produce_actionable_partial_result(board, tmp_path, failure):
    with patch('emi_assistant.sim_schematic._export', side_effect=failure):
        result = prepare(board, default_settings(), tmp_path / 'job', {'available': True, 'path': 'kicad-cli'})
    assert result['status'] == 'needs_information'
    assert str(failure) in result['message']
    assert not result['components']


def test_cancellation_before_files_or_processes(board, tmp_path):
    event = threading.Event()
    event.set()
    with patch('emi_assistant.sim_schematic._export') as export:
        result = prepare(board, default_settings(), tmp_path / 'job', {'available': True, 'path': 'kicad-cli'}, event)
    assert result['status'] == 'cancelled'
    export.assert_not_called()


def test_changed_schematic_during_export_invalidates_result(board, tmp_path):
    source = Path(board.path).with_suffix('.kicad_sch')
    def changed(*args):
        result = export_ok(*args)
        source.write_text('(kicad_sch (uuid changed-root))')
        return result
    with patch('emi_assistant.sim_schematic._export', side_effect=changed):
        result = prepare(board, default_settings(), tmp_path / 'job', {'available': True, 'path': 'kicad-cli'})
    assert result['status'] == 'needs_information'
    assert all(not c['board_match'] for c in result['components'])
    assert any('changed during export' in m for m in result['missing'])


def test_xml_entities_and_conflicting_pin_nets_rejected(board):
    with pytest.raises(ValueError, match='entity'):
        parse_netlist(b'<!DOCTYPE export [<!ENTITY x SYSTEM "file:///private">]><export/>', board)
    with pytest.raises(ValueError, match='multiple schematic nets'):
        parse_netlist(XML.replace(b'<net code="2" name="GND">', b'<net code="2" name="GND"><node ref="U1" pin="1"/>'), board)
