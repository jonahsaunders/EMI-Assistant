"""Physics eligibility, native solver I/O and process limits for field screening."""
from pathlib import Path
import json
import math
import os
import threading
import xml.etree.ElementTree as ET

import pytest

from emi_assistant.models import BoardSnapshot, Track, CopperPolygon, default_settings
from emi_assistant.sim_openems import (ModelUnavailable, SolverInvalid, choose_model,
    write_model, run, _probe, read_response, _execute)


def coupon():
    return BoardSnapshot(name='coupon',path='',fingerprint='coupon',
        layers=['F.Cu','B.Cu'],
        stackup=[{'name':'F.Cu','type':'copper','thickness':.035},
                 {'name':'dielectric 1','type':'core','thickness':.2,'epsilon_r':4.2,'loss_tangent':.02},
                 {'name':'B.Cu','type':'copper','thickness':.035}],
        tracks=[Track('clock',(5,10),(25,10),.3,'F.Cu','CLK')],
        copper=[CopperPolygon('ground','GND','B.Cu',[(0,0),(30,0),(30,20),(0,20)])])


def settings():
    result=default_settings()
    result['fast_nets']={'CLK':{'enabled':True,'frequency_mhz':100}}
    result['simulation']={'max_seconds':90,'frequency_start_mhz':1,'frequency_stop_mhz':500,'points':41}
    return result


def test_reads_actual_geometry_and_material():
    model=choose_model(coupon(),settings())
    assert model['length']==20
    assert model['height']==.2
    assert model['epsilon_r']==4.2
    assert model['reference_layer']=='B.Cu'
    assert model['item_ids']==['clock']


def test_rejects_unknown_stackup():
    board=coupon();board.stackup=[]
    with pytest.raises(ModelUnavailable,match='stackup'):
        choose_model(board,settings())


def test_does_not_fill_reference_holes():
    board=coupon();board.copper[0].holes=[[(14,9),(16,9),(16,11),(14,11)]]
    with pytest.raises(ModelUnavailable,match='continuous'):
        choose_model(board,settings())


def test_unfilled_zone_is_not_a_plane():
    board=coupon();board.copper[0].is_filled=False
    assert run(board,settings(),Path('.'),{'available':True,'path':'wrong'})[0]['status']=='needs_information'


def test_nearby_trace_is_not_silently_erased():
    board=coupon();board.tracks.append(Track('aggressor',(5,10.3),(25,10.3),.2,'F.Cu','OTHER'))
    with pytest.raises(ModelUnavailable):
        choose_model(board,settings())


def test_bottom_layer_uses_reversed_stackup():
    board=coupon();board.tracks[0].layer='B.Cu';board.copper[0].layer='F.Cu'
    assert choose_model(board,settings())['reference_layer']=='F.Cu'


def test_internal_layer_not_approximated_as_microstrip():
    board=coupon();board.layers=['F.Cu','In1.Cu','B.Cu'];board.tracks[0].layer='In1.Cu'
    with pytest.raises(ModelUnavailable):
        choose_model(board,settings())


def test_xml_contains_ports_grid_and_saved_epsilon(tmp_path):
    model=choose_model(coupon(),settings())
    path,info=write_model(model,settings(),tmp_path,72)
    xml=ET.parse(path).getroot()
    assert xml.tag=='openEMS'
    assert info['cells']<=180000
    csx=xml.find('ContinuousStructure')
    assert csx.find('RectilinearGrid').get('DeltaUnit')=='.001'
    assert csx.find('Properties/Material/Property').get('Epsilon')=='4.2'
    resistors=csx.findall('Properties/LumpedElement')
    assert [r.get('R') for r in resistors]==['50','72']
    assert len(csx.findall('Properties/ProbeBox'))==4
    assert xml.find('FDTD/BoundaryCond').get('zmin')=='PEC'
    assert json.loads((tmp_path/'model-inputs.json').read_text())['length']==20


def test_missing_engine_is_not_completed(tmp_path):
    result=run(coupon(),settings(),tmp_path,{'available':False,'message':'Install openEMS'})[0]
    assert result['status']=='unavailable'
    assert not result['series']


def test_cancelled_does_not_start_engine(tmp_path):
    event=threading.Event();event.set()
    assert run(coupon(),settings(),tmp_path,{},cancel_event=event)[0]['status']=='cancelled'
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('bad', ['NaN 1','1 inf','1 1\n0 2'])
def test_rejects_invalid_probe_numbers(tmp_path,bad):
    path=tmp_path/'probe';path.write_text(bad)
    with pytest.raises(SolverInvalid):
        _probe(path)


def test_probe_tail_must_decay(tmp_path):
    path=tmp_path/'probe';path.write_text('\n'.join(f'{i*1e-12} 1' for i in range(100)))
    with pytest.raises(SolverInvalid,match='decayed'):
        _probe(path)


def test_fourier_current_uses_own_sample_times(tmp_path):
    cfg=settings();cfg['simulation']['frequency_stop_mhz']=100
    # Matched source: U=50 I, delayed half-amplitude output. Current deliberately
    # has a different time origin, requiring its own DFT time vector.
    def pulse(t):
        return math.exp(-((t-30e-9)/5e-9)**2)
    for name,scale,offset,delay in [('port_ut_1',1,0,0),('port_it_1',.02,.05e-9,0),('port_ut_2',.5,0,2e-9)]:
        (tmp_path/name).write_text('\n'.join(f'{i*.1e-9+offset:.14g} {scale*pulse(i*.1e-9+offset-delay):.14g}' for i in range(1001)))
    response=read_response(tmp_path,cfg,50,100e6)
    assert max(response['reflection_db']) < -100
    assert all(abs(y+6.0206)<.001 for y in response['transfer_db'])


def test_declared_runtime_error_retains_generated_evidence(tmp_path,monkeypatch):
    import emi_assistant.sim_openems as backend
    def fail(*args):
        raise SolverInvalid('No convergence')
    monkeypatch.setattr(backend,'_execute',fail)
    result=run(coupon(),settings(),tmp_path,{'available':True,'path':'anything'})[0]
    assert result['status']=='error'
    assert not result['series']
    assert any(p.endswith('model.xml') for p in result['artifacts'])


def test_real_openems_coupon(tmp_path):
    """Opt-in actual FDTD execution, no mock or analytical replacement."""
    path=os.environ.get('EMI_TEST_OPENEMS')
    if not path:
        pytest.skip('Set EMI_TEST_OPENEMS to run the native solver validation.')
    runtime={'available':True,'path':path,'env':{}}
    if os.environ.get('EMI_TEST_OPENEMS_LIBS'):
        runtime['env']['LD_LIBRARY_PATH']=os.environ['EMI_TEST_OPENEMS_LIBS']
    result=run(coupon(),settings(),tmp_path,runtime)[0]
    assert result['status']=='complete',result
    assert len(result['series'])==4
    assert all(math.isfinite(v) for s in result['series'] for v in s['y'])
    assert any(Path(p).name=='solver.log' for p in result['artifacts'])


def test_native_process_is_stopped_at_time_limit(tmp_path):
    import sys
    (tmp_path/'model.xml').write_text('import time\ntime.sleep(10)\n')
    with pytest.raises(TimeoutError,match='time limit'):
        _execute({'path':sys.executable},tmp_path,.1,None)


def test_selected_fast_net_does_not_fall_back_to_power(tmp_path):
    board=coupon();board.tracks[0].net='+3V3'
    assert run(board,settings(),tmp_path,{'available':True})[0]['status']=='needs_information'


def test_arc_chords_are_not_misrepresented_as_straight_trace():
    board=coupon();board.tracks.append(Track('clock',(25,10),(29,12),.3,'F.Cu','CLK'))
    with pytest.raises(ModelUnavailable):
        choose_model(board,settings())
