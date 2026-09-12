import json
import math
import re
import xml.etree.ElementTree as ET

from emi_assistant.models import AnalysisResult, BoardSnapshot, Finding
from emi_assistant.report import export_report


def result_with(*simulations):
    board = BoardSnapshot('Example PCB', '/project/board.kicad_pcb', 'abc123')
    finding = Finding('return1', 'return_path', 'Return path needs attention', 'High', 'Medium',
                      'A signal crosses a reference gap.', 'Restore reference copper.', ['t1'], (1, 2))
    return AnalysisResult(board, [finding], simulations=list(simulations))


def completed(**extra):
    result = dict(id='spice-1', engine='ngspice', engine_version='45.2', title='Isolated decoupling model',
                  status='complete', summary='The estimated small-signal impedance changes under these assumptions.',
                  assumptions=['Capacitor ESR is assumed; this is not a complete power-distribution model.'],
                  missing=[], warnings=['Component vendor models were not provided.'],
                  recommendations=['Compare the candidate capacitor with a bench measurement.'],
                  metrics=[{'name': 'Baseline peak impedance', 'value': 12.3456789, 'unit': 'ohm'}],
                  series=[{'name': 'Baseline (estimated model)', 'x': [.1, 1, 10, 100], 'y': [1, 4, 10, 3], 'x_unit': 'MHz', 'y_unit': 'ohm'},
                          {'name': 'Candidate (estimated model)', 'x': [.1, 1, 10, 100], 'y': [1, 2, 5, 1.5], 'x_unit': 'MHz', 'y_unit': 'ohm'}],
                  artifacts=['/project/jobs/safe-model.cir', '/project/jobs/ngspice.log'], cached=True)
    result.update(extra)
    return result


def svg_elements(html):
    return [ET.fromstring(x) for x in re.findall(r'<svg\b.*?</svg>', html, re.S)]


def test_complete_simulation_reports_keep_layout_and_model_evidence(tmp_path):
    path = tmp_path / 'report.html'
    export_report(result_with(completed()), str(path))
    html = path.read_text()
    assert 'Return path needs attention' in html
    assert 'Simulation results' in html
    assert 'ngspice 45.2' in html
    assert 'Reused matching cached result' in html
    assert 'Baseline peak impedance' in html and '12.3457' in html
    assert 'Baseline (estimated model)' in html and 'Candidate (estimated model)' in html
    assert 'Capacitor ESR is assumed' in html and 'bench measurement' in html
    assert '/project/jobs/ngspice.log' in html
    assert 'Not a field simulation' not in html
    assert 'do not predict whole-board emissions' in html
    svgs = svg_elements(html)
    assert len(svgs) == 1
    assert len(svgs[0].findall('{http://www.w3.org/2000/svg}polyline')) == 2
    assert 'MHz; log scale' in html
    assert '<script' not in html
    assert 'https://cdn' not in html


def test_json_preserves_both_engines_and_missing_information(tmp_path):
    path = tmp_path / 'report.json'
    field = completed(engine='openems', title='Field model', status='needs_information', metrics=[], series=[],
                      missing=['A continuous reference plane and substrate thickness are needed.'])
    export_report(result_with(completed(), field), str(path))
    data = json.loads(path.read_text())
    assert [s['engine'] for s in data['simulations']] == ['ngspice', 'openems']
    assert data['simulations'][1]['status'] == 'needs_information'
    assert 'substrate thickness' in data['simulations'][1]['missing'][0]
    assert data['simulations'][0]['series'][0]['x'] == [.1, 1, 10, 100]
    assert 'whole-board emissions' in data['interpretation']
    assert data['findings'][0]['rule'] == 'return_path'


def test_missing_and_unavailable_are_visible_without_false_numerical_results(tmp_path):
    path = tmp_path / 'report.html'
    export_report(result_with(completed(engine='openems', status='needs_information', metrics=[], series=[],
                                        missing=['Dielectric thickness is missing.']),
                              completed(status='unavailable', metrics=[], series=[], summary='ngspice was not found.')), str(path))
    html = path.read_text()
    assert 'Needs information' in html and 'Dielectric thickness is missing.' in html
    assert 'Unavailable' in html and 'ngspice was not found.' in html
    assert not svg_elements(html)
    assert 'Numerical model results' not in html


def test_incompatible_axis_units_always_use_separate_plots(tmp_path):
    curves = completed()['series']
    curves += [dict(name='Reflection', x=[.1, 1, 10], y=[-20, -12, -5], x_unit='MHz', y_unit='dB'),
               dict(name='Step response', x=[0, 1, 2], y=[0, 1.2, 1], x_unit='us', y_unit='V')]
    path = tmp_path / 'report.html'
    export_report(result_with(completed(series=curves)), str(path))
    html = path.read_text()
    plots = svg_elements(html)
    assert len(plots) == 3
    assert [len(p.findall('{http://www.w3.org/2000/svg}polyline')) for p in plots] == [2, 1, 1]
    assert 'Time (us; linear scale)' in html


def test_escape_all_simulation_text_including_svg_and_artifact_paths(tmp_path):
    attack = '<img src=x onerror="alert(1)">'
    curve = dict(name=attack, x=[1, 2], y=[3, 4], x_unit=attack, y_unit=attack)
    sim = completed(title=attack, summary=attack, engine=attack, engine_version=attack,
                    assumptions=[attack], missing=[attack], warnings=[attack], recommendations=[attack],
                    artifacts=['javascript:' + attack], series=[curve],
                    metrics=[dict(name=attack, value=1, unit=attack)], scope=attack, schematic_status=attack)
    path = tmp_path / 'report.html'
    export_report(result_with(sim), str(path))
    html = path.read_text()
    assert attack not in html
    assert '&lt;img' in html
    assert 'href="javascript:' not in html
    svg_elements(html)  # Text escaping also keeps native SVG XML well-formed.


def test_nonfinite_or_unpaired_curves_are_omitted_with_visible_warning_in_html_and_json(tmp_path):
    sim = completed(metrics=[dict(name='bad', value=float('nan'), unit='V')], series=[
        dict(name='NaN curve', x=[1, 2], y=[1, float('nan')], x_unit='MHz', y_unit='V'),
        dict(name='Infinity curve', x=[1, 2], y=[1, float('inf')], x_unit='MHz', y_unit='V'),
        dict(name='Mismatched curve', x=[1, 2, 3], y=[1, 2], x_unit='MHz', y_unit='V'),
        dict(name='Nonmonotonic curve', x=[1, 3, 2], y=[1, 2, 3], x_unit='MHz', y_unit='V')])
    analysis = result_with(sim)
    for suffix in ['html', 'json']:
        path = tmp_path / ('report.' + suffix)
        export_report(analysis, str(path))
        text = path.read_text()
        assert 'omitted' in text
        assert 'No valid numerical evidence remains' in text
        if suffix == 'html':
            assert not svg_elements(text)
        else:
            payload = json.loads(text)
            assert payload['simulations'][0]['status'] == 'error'
            assert payload['simulations'][0]['series'] == []
            assert payload['simulations'][0]['metrics'] == []
    assert math.isnan(analysis.simulations[0]['metrics'][0]['value'])  # No mutation.


def test_finite_extreme_and_constant_curves_produce_finite_svg_coordinates(tmp_path):
    for ys in [[-1e308, 1e308], [1, 1], [0, 0], [math.nextafter(math.inf, 0)] * 2]:
        sim = completed(series=[dict(name='Finite input', x=[-1e308, 1e308], y=ys, x_unit='s', y_unit='V')])
        path = tmp_path / 'report.html'
        export_report(result_with(sim), str(path))
        plot = svg_elements(path.read_text())[0]
        assert '>inf<' not in path.read_text()
        coordinates = plot.find('{http://www.w3.org/2000/svg}polyline').get('points')
        assert all(math.isfinite(float(v)) for v in coordinates.replace(',', ' ').split())


def test_no_simulation_results_does_not_imply_simulated_pass(tmp_path):
    path = tmp_path / 'report.html'
    export_report(result_with(), str(path))
    assert 'No simulation results are available' in path.read_text()
