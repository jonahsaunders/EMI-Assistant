"""Bounded, native-XML openEMS simulation of a PCB-derived microstrip coupon.

No openEMS/CSXCAD Python binding is required. The native solver writes voltage
and current probes; numerical Fourier integration gives port response. This is
an explicitly simplified local transmission-line investigation, never a board
radiation or regulatory-compliance prediction.

XML field names and port conventions follow upstream CSXCAD serialization and
openEMS ports documentation: https://docs.openems.de/python/openEMS/ports.html
https://github.com/thliebig/CSXCAD/tree/master/src
https://github.com/thliebig/openEMS/blob/master/openems.cpp
https://github.com/thliebig/openEMS/blob/master/matlab/Tutorials/MSL_NotchFilter.m
"""
from __future__ import annotations

import cmath
from collections import Counter
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from .geometry import CopperIndex

_MAX_CELLS = 180_000
_MAX_STEPS = 350_000
_END_CRITERION = 1e-3


class ModelUnavailable(ValueError):
    pass


class SolverInvalid(ValueError):
    pass


def _number(value, low, high, label):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ModelUnavailable(f'{label} must be a number.') from None
    if not math.isfinite(value) or not low <= value <= high:
        raise ModelUnavailable(f'{label} must be between {low:g} and {high:g}.')
    return value


def _result(status, summary, **extra):
    result = dict(id='openems-local-trace', engine='openems', title='Local trace field simulation',
                  status=status, summary=summary, assumptions=[], missing=[], warnings=[],
                  metrics=[], series=[], recommendations=[], item_ids=[], artifacts=[])
    result.update(extra)
    return result


def _stackup(board, layer):
    """Only a single homogeneous dielectric to the immediately adjacent copper."""
    copper = [i for i, row in enumerate(board.stackup) if row.get('name') in board.layers]
    if len(copper) < 2 or layer not in (board.layers[0], board.layers[-1]):
        raise ModelUnavailable('A saved physical stackup and an outer-layer trace are required.')
    indices = copper[:2] if layer == board.layers[0] else list(reversed(copper[-2:]))
    a, b = indices
    if board.stackup[a].get('name') != layer:
        raise ModelUnavailable('Physical stackup order does not match the copper layers.')
    dielectric = [r for r in board.stackup[min(a, b)+1:max(a, b)]
                  if r.get('type') not in ('copper', 'soldermask', 'silkscreen')]
    if len(dielectric) != 1:
        raise ModelUnavailable('This field model requires one homogeneous dielectric between the trace and its nearest reference layer.')
    row = dielectric[0]
    h = _number(row.get('thickness'), .05, 3, 'Dielectric thickness in mm')
    er = _number(row.get('epsilon_r'), 1, 20, 'Dielectric relative permittivity')
    loss = _number(row.get('loss_tangent', 0), 0, .2, 'Dielectric loss tangent')
    return dict(height=h, epsilon_r=er, loss_tangent=loss, reference_layer=board.stackup[b]['name'],
                material=str(row.get('material', 'unspecified dielectric')),
                loss_known='loss_tangent' in row)


def choose_model(board, settings):
    """Choose a straight, isolated local section with a verified broad return."""
    from shapely.geometry import LineString, Polygon, Point
    reference_nets = settings.get('reference_nets') or ['GND']
    fast = settings.get('fast_nets', {})
    selected = {net for net, profile in fast.items()
                if (profile.get('enabled', True) if isinstance(profile, dict) else bool(profile))}
    disabled = set(fast)-selected
    id_counts = Counter(t.id for t in board.tracks)
    tracks = sorted((t for t in board.tracks if t.net and t.net not in reference_nets
                     and id_counts[t.id] == 1 and t.net not in disabled and (not selected or t.net in selected)),
                    key=lambda t: (t.net not in selected,
                                   -math.dist(t.start, t.end), t.id))
    copper = CopperIndex(board)
    reasons = []
    for track in tracks:
        length = math.dist(track.start, track.end)
        if not 3 <= length <= 1000 or not .1 <= track.width <= 3:
            continue
        try:
            data = _stackup(board, track.layer)
        except ModelUnavailable as exc:
            reasons.append(str(exc)); continue
        h = data['height']
        # Keep runtime predictable; only the central, straight section is modeled.
        use_length = min(length, 25.0)
        ux, uy = ((track.end[i]-track.start[i])/length for i in range(2))
        midpoint = tuple((track.start[i]+track.end[i])/2 for i in range(2))
        start = (midpoint[0]-ux*use_length/2, midpoint[1]-uy*use_length/2)
        end = (midpoint[0]+ux*use_length/2, midpoint[1]+uy*use_length/2)
        margin = max(6*h, 1.)
        halfwidth = track.width/2+margin
        def transform(x, y):
            return (start[0]+ux*x-uy*y, start[1]+uy*x+ux*y)
        region = Polygon([transform(-margin, -halfwidth), transform(use_length+margin, -halfwidth),
                          transform(use_length+margin, halfwidth), transform(-margin, halfwidth)])
        reference = next((net for net in reference_nets
                          if copper.get(data['reference_layer'], net).covers(region)), None)
        if not reference:
            reasons.append('No straight outer-layer section has sufficiently broad, continuous filled reference copper.'); continue
        # Do not silently erase coupling from adjacent trace, pad, via or copper.
        corridor = LineString([start, end]).buffer(track.width/2+3*h, cap_style=2)
        blocked = any(t.id != track.id and t.layer == track.layer and
                      LineString([t.start, t.end]).buffer(t.width/2).intersects(corridor)
                      for t in board.tracks)
        blocked |= any(track.layer in p.layers or '*.Cu' in p.layers
                       for p in board.pads if Point(p.position).buffer(max(p.size)/2).intersects(corridor))
        blocked |= any(Point(v.position).buffer(v.diameter/2).intersects(corridor) for v in board.vias)
        blocked |= any(layer == track.layer and shape.intersects(corridor)
                       for (layer, net), shape in copper.geometry.items())
        if blocked:
            # Retain a central section away from pads and connected endpoints.
            trim = max(3*h, track.width, 1.)
            if use_length <= 3+2*trim:
                continue
            trimmed_start = transform(trim, 0); trimmed_end = transform(use_length-trim, 0)
            start, end = trimmed_start, trimmed_end
            use_length -= 2*trim
            corridor = LineString([start, end]).buffer(track.width/2+3*h, cap_style=2)
            blocked = any(t.id != track.id and t.layer == track.layer and
                          LineString([t.start, t.end]).buffer(t.width/2).intersects(corridor)
                          for t in board.tracks)
            blocked |= any((track.layer in p.layers or '*.Cu' in p.layers) and
                           Point(p.position).buffer(max(p.size)/2).intersects(corridor) for p in board.pads)
            blocked |= any(Point(v.position).buffer(v.diameter/2).intersects(corridor) for v in board.vias)
            blocked |= any(layer == track.layer and shape.intersects(corridor)
                           for (layer, net), shape in copper.geometry.items())
            if blocked:
                reasons.append('Nearby copper, pads or vias require a more complex field model.'); continue
        data.update(length=use_length, width=track.width, net=track.net, layer=track.layer,
                    item_ids=[track.id], location=list(midpoint), start=list(start), end=list(end),
                    reference_net=reference, full_segment_length=length)
        return data
    raise ModelUnavailable(reasons[0] if reasons else
                           'No eligible straight outer-layer trace was found (minimum 3 mm long and 0.1 mm wide).')


def _fmt(value):
    return format(value, '.12g')


def _linspace(a, b, count):
    return [a+(b-a)*i/(count-1) for i in range(count)]


def _expand(core, distance, step):
    """Smoothly expand a core and append eight uniform cells for the PML."""
    core = sorted(set(round(float(v), 10) for v in core))
    left, right = [], []
    width, delta = 0., step
    while width < distance:
        width += delta
        left.append(core[0]-width); right.append(core[-1]+width)
        delta = min(delta*1.3, max(distance/3, step))
    for _ in range(8):
        width += delta
        left.append(core[0]-width); right.append(core[-1]+width)
    return list(reversed(left))+core+right


def write_model(model, settings, folder: Path, termination: float):
    folder.mkdir(parents=True, exist_ok=True)
    h, w, length = model['height'], model['width'], model['length']
    sim = settings.get('simulation', {})
    fmax = _number(sim.get('frequency_stop_mhz', 500), 1, 5000, 'Maximum field frequency in MHz')*1e6
    # A broadband impulse shortens the time run while still exciting the entire
    # requested reporting band. Its high-frequency tail is not reported.
    excitation_fc = max(fmax, 2e9)
    _number(sim.get('frequency_start_mhz', .1), .001, fmax/1e6, 'Minimum frequency in MHz')
    source = _number(sim.get('source_ohm', 50), 1, 10000, 'Source resistance in ohms')
    # At least four cells through dielectric, three across trace, and twenty per
    # shortest dielectric wavelength. Port centers and ends are exact grid nodes.
    resolution = min(.5, 299792458/excitation_fc*1000/math.sqrt(model['epsilon_r'])/20)
    nx = max(8, math.ceil(length/resolution))
    if nx % 2:
        nx += 1
    xcore = _linspace(0, length, nx+1)
    port_dx = xcore[2]
    x = _expand(xcore, max(4*h, 1), xcore[1])
    # Resolve metal edges using the upstream microstrip tutorial's one-third
    # inside / two-thirds outside rule instead of placing metal edges directly
    # on electric-field nodes (which systematically widens a thin conductor).
    edge_step = min(w/4, h/2)
    inner = w/2-edge_step/3
    ycore = _linspace(-inner, inner, max(3, math.ceil(2*inner/edge_step)+1))
    ycore += [-w/2-2*edge_step/3, w/2+2*edge_step/3]
    y = _expand(ycore, max(6*h, 1), edge_step)
    z = _linspace(0, h, 5)
    # PEC at the bottom represents a broad reference plane. Other faces absorb.
    z = _expand(z, max(8*h, 2), h/4)
    z = [v for v in z if v >= -1e-9]
    cells = (len(x)-1)*(len(y)-1)*(len(z)-1)
    if cells > _MAX_CELLS:
        raise ModelUnavailable(f'The local mesh needs {cells:,} cells, above the {_MAX_CELLS:,}-cell automatic limit.')
    smallest = [min(b-a for a, b in zip(axis, axis[1:]))*1e-3 for axis in (x,y,z)]
    dt = .9/(299792458*math.sqrt(sum(1/d**2 for d in smallest)))
    # A zero-centred Gaussian covers DC..fmax. At fc it is -20 dB in amplitude.
    needed = math.ceil((8/excitation_fc + 20*length*1e-3*math.sqrt(model['epsilon_r'])/299792458)/dt)
    if needed > _MAX_STEPS:
        raise ModelUnavailable('This frequency range needs too many field timesteps for the automatic limit; use a higher maximum frequency or a shorter/wider trace.')
    root = ET.Element('openEMS')
    fdtd = ET.SubElement(root, 'FDTD', NumberOfTimesteps=str(max(needed, 50000)),
                         endCriteria=str(_END_CRITERION), OverSampling='4', TimeStepFactor='.9')
    ET.SubElement(fdtd, 'Excitation', Type='0', f0='0', fc=_fmt(excitation_fc))
    ET.SubElement(fdtd, 'BoundaryCond', xmin='PML_8', xmax='PML_8', ymin='PML_8', ymax='PML_8', zmin='PEC', zmax='PML_8')
    csx = ET.SubElement(root, 'ContinuousStructure', CoordSystem='0')
    grid = ET.SubElement(csx, 'RectilinearGrid', DeltaUnit='.001', CoordSystem='0')
    for tag, axis in zip(('XLines','YLines','ZLines'), (x,y,z)):
        ET.SubElement(grid, tag, Qty=str(len(axis))).text = ','.join(_fmt(v) for v in axis)
    properties = ET.SubElement(csx, 'Properties')
    def prop(tag, name, **attrs):
        return ET.SubElement(properties, tag, Name=name, **{k:str(v) for k,v in attrs.items()})
    def box(parent, a, b, priority=0):
        primitives = parent.find('Primitives')
        if primitives is None:
            primitives = ET.SubElement(parent,'Primitives')
        node = ET.SubElement(primitives, 'Box', Priority=str(priority))
        ET.SubElement(node,'P1', **{k:_fmt(v) for k,v in zip(('X','Y','Z'),a)})
        ET.SubElement(node,'P2', **{k:_fmt(v) for k,v in zip(('X','Y','Z'),b)})
    dielectric = prop('Material','substrate',Isotropy='1')
    # Conductivity represents loss tangent at the centre of the reported band;
    # this is deliberately recorded as a frequency-dependent approximation.
    conductivity = 2*math.pi*(fmax/2)*8.8541878128e-12*model['epsilon_r']*model['loss_tangent']
    ET.SubElement(dielectric,'Property', Epsilon=_fmt(model['epsilon_r']), Kappa=_fmt(conductivity))
    box(dielectric,(x[0],y[0],0),(x[-1],y[-1],h))
    trace = prop('Metal','signal_trace')
    box(trace,(0,-w/2,h),(length,w/2,h),10)
    for number, a, b, resistance, excite in ((1,0,port_dx,source,True),(2,length-port_dx,length,termination,False)):
        port = prop('LumpedElement',f'port_resist_{number}',Direction='2',Caps='1',R=_fmt(resistance))
        box(port,(a,-w/2,0),(b,w/2,h),20)
        if excite:
            sourceprop = prop('Excitation','port_excite_1',Type='0',Excite='0,0,-1',Enabled='1')
            box(sourceprop,(a,-w/2,0),(b,w/2,h),20)
        voltage = prop('ProbeBox',f'port_ut_{number}',Type='0',Weight='-1')
        box(voltage,((a+b)/2,0,0),((a+b)/2,0,h))
        current = prop('ProbeBox',f'port_it_{number}',Type='1',Weight='1',NormDir='2')
        box(current,(a,-w/2,h/2),(b,w/2,h/2))
    path = folder/'model.xml'
    ET.indent(root)
    ET.ElementTree(root).write(path,encoding='utf-8',xml_declaration=True)
    evidence = dict(model, source_ohm=source, termination_ohm=termination,
                    cells=cells, fmax_hz=fmax, excitation_cutoff_hz=excitation_fc, conductivity_s_m=conductivity,
                    domain_mm=[[x[0],x[-1]],[y[0],y[-1]],[0,z[-1]]], grid_lines=[len(x),len(y),len(z)])
    (folder/'model-inputs.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    return path, evidence


def _execute(runtime, folder, max_seconds, cancel_event):
    path = Path(runtime.get('path',''))
    if not path.is_file():
        raise SolverInvalid('The openEMS executable is unavailable.')
    log = folder/'solver.log'
    env = os.environ.copy()
    env.update({str(k):str(v) for k,v in runtime.get('env',{}).items()})
    kwargs = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    started = time.monotonic()
    with log.open('w',encoding='utf-8') as output:
        process = subprocess.Popen([str(path.resolve()),'model.xml','--numThreads=2','--dump-statistics'],
                                   cwd=folder,stdout=output,stderr=subprocess.STDOUT,env=env,**kwargs)
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError('Field simulation was cancelled.')
                if time.monotonic()-started > max_seconds:
                    raise TimeoutError('Field simulation reached its time limit. Its partial output is not a valid result.')
                time.sleep(.05)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=2)
    text = log.read_text(encoding='utf-8',errors='replace')
    if process.returncode != 0:
        raise SolverInvalid(f'openEMS exited with code {process.returncode}; see solver.log.')
    if re.search(r'\b(?:nan|inf)\b|Error:|before the end-criteria|un-used|unused|Unknown Property|invalid Property|No primitives',text,re.I):
        raise SolverInvalid('openEMS reported an error, unused geometry, non-finite output, or incomplete energy decay; see solver.log.')
    energies = re.findall(r'Energy:.*?\(-\s*([\d.]+)\s*dB\)',text)
    if not energies or float(energies[-1]) < 29.5:
        raise SolverInvalid('openEMS did not report at least 30 dB of energy decay; no trustworthy frequency result is shown.')
    return text


def _probe(path):
    times, values = [], []
    if not path.exists() or path.stat().st_size > 64*1024*1024:
        raise SolverInvalid(f'Missing or oversized probe output: {path.name}.')
    for line in path.read_text(encoding='utf-8',errors='replace').splitlines():
        if not line.strip() or line.lstrip().startswith(('%','#')):
            continue
        columns = line.split()
        if len(columns)<2:
            raise SolverInvalid('Malformed field probe output.')
        try:
            t, value = float(columns[0]),float(columns[1])
        except ValueError:
            raise SolverInvalid('Malformed field probe numbers.') from None
        if not math.isfinite(t) or not math.isfinite(value) or (times and t <= times[-1]):
            raise SolverInvalid('Non-finite or non-monotonic field probe output.')
        times.append(t);values.append(value)
    if len(times)<32 or max(abs(v) for v in values)<1e-18:
        raise SolverInvalid('The field probe is empty or has no excitation.')
    peak = max(v*v for v in values)
    tail = sum(v*v for v in values[-max(8,len(values)//20):])/max(8,len(values)//20)
    if tail/peak > 1e-3:
        raise SolverInvalid('Probe voltage/current has not decayed sufficiently.')
    return times,values


def _spectrum(times, values, frequencies):
    # Direct quadrature accounts for half-step time offsets of the H/current
    # samples. It avoids silently treating separately sampled currents as E data.
    try:
        import numpy as np
        t=np.asarray(times);v=np.asarray(values);dt=np.diff(t)
        return [complex(np.sum((v[:-1]*np.exp(-2j*np.pi*f*t[:-1])+v[1:]*np.exp(-2j*np.pi*f*t[1:]))*dt/2)) for f in frequencies]
    except ImportError:
        return [sum((values[i]*cmath.exp(-2j*math.pi*f*times[i])+values[i+1]*cmath.exp(-2j*math.pi*f*times[i+1]))*(times[i+1]-times[i])/2 for i in range(len(times)-1)) for f in frequencies]


def read_response(folder, settings, source, fmax):
    sim=settings.get('simulation',{})
    start=_number(sim.get('frequency_start_mhz',.1),.001,fmax/1e6,'Minimum frequency in MHz')*1e6
    points=int(_number(sim.get('points',121),16,501,'Frequency points'))
    frequencies=_linspace(start,fmax,points)
    spectra={name:_spectrum(*_probe(folder/name),frequencies) for name in ('port_ut_1','port_it_1','port_ut_2')}
    voltage,current,load=(spectra[name] for name in ('port_ut_1','port_it_1','port_ut_2'))
    incident=[(u+source*i)/2 for u,i in zip(voltage,current)]
    peak=max(abs(v) for v in incident)
    if peak<1e-20:
        raise SolverInvalid('The incident source spectrum is absent.')
    supported=[i for i,v in enumerate(incident) if abs(v)>=peak*.03 and abs(voltage[i])>=peak*.005]
    if len(supported)<max(8,points//2):
        raise SolverInvalid('Too little incident excitation supports the requested frequency range.')
    reflected=[abs((voltage[i]-incident[i])/incident[i]) for i in supported]
    if any(not math.isfinite(v) or v>1.05 for v in reflected):
        raise SolverInvalid('The simulated passive-port reflection failed its energy/passivity check.')
    transfer=[abs(load[i]/voltage[i]) for i in supported]
    if any(not math.isfinite(v) for v in transfer):
        raise SolverInvalid('Non-finite port transfer.')
    return dict(frequency=[frequencies[i]/1e6 for i in supported],
                reflection_db=[20*math.log10(max(v,1e-9)) for v in reflected],
                transfer_db=[20*math.log10(max(v,1e-9)) for v in transfer],
                masked=points-len(supported))


def run(board, settings, workdir:Path, runtime:dict, circuit=None, cancel_event=None, progress=None):
    """Run a PCB coupon and a resistive-termination what-if, independently gated."""
    if cancel_event is not None and cancel_event.is_set():
        return [_result('cancelled','Field simulation was cancelled.')]
    try:
        model=choose_model(board,settings)
    except ModelUnavailable as exc:
        return [_result('needs_information','A suitable local field model could not be built.',missing=[str(exc)],
                        recommendations=['Save the board with filled reference zones and a physical stackup containing dielectric thickness and permittivity.'])]
    if not runtime.get('available'):
        return [_result('unavailable',runtime.get('message') or 'openEMS is not installed.',item_ids=model['item_ids'],location=model['location'])]
    workdir=Path(workdir).resolve();workdir.mkdir(parents=True,exist_ok=True)
    sim=settings.get('simulation',{})
    assumptions=[f'Local {model["length"]:.2f} mm section of {model["net"]} on {model["layer"]}; bends, remaining route and component models are excluded.',
                 f'{model["width"]:.3f} mm trace over {model["height"]:.3f} mm {model["material"]}, relative permittivity {model["epsilon_r"]:g}.',
                 f'Broad ideal reference plane at {model["reference_layer"]}/{model["reference_net"]}; filled copper continuity was checked within at least six dielectric heights.',
                 'Zero-thickness perfect conductors; solder mask, conductor loss, roughness, connector/package parasitics and distant conductors are omitted.',
                 'Dielectric loss is approximated by constant conductivity matching the saved loss tangent at half the maximum frequency.',
                 'Ports are synthetic resistive terminations, not verified on-board driver/receiver circuits. This is a field-solver screening result, not absolute radiated emissions or a compliance result.',
                 'Mesh size is bounded; mesh-refinement convergence has not been established for this board.',
                 'At least 30 dB of energy decay is required. Very small reflections and small differences may be numerical error and need a refined model before quantitative interpretation.']
    result=_result('error','Field simulation did not finish.',assumptions=assumptions,item_ids=model['item_ids'],location=model['location'])
    if not model['loss_known']:
        result['warnings'].append('No dielectric loss tangent was saved; the dielectric is modeled as lossless.')
    try:
        termination=_number(sim.get('termination_ohm',50),1,10000,'Load resistance in ohms')
        extra=_number(sim.get('candidate_series_ohm',22),.01,1000,'Candidate resistance in ohms')
        limit=_number(sim.get('max_seconds',90),5,600,'Maximum simulation time in seconds')
        started=time.monotonic()
        responses=[]
        for name,resistance in [('baseline',termination),('termination-comparison',termination+extra)]:
            if progress:
                progress(f'openEMS: simulating {model["net"]} ({name.replace("-"," ")})…')
            folder=workdir/name
            _,inputs=write_model(model,settings,folder,resistance)
            remaining=limit-(time.monotonic()-started)
            if remaining<2:
                raise TimeoutError('Field comparison reached its shared time limit.')
            _execute(runtime,folder,remaining,cancel_event)
            response=read_response(folder,settings,inputs['source_ohm'],inputs['fmax_hz'])
            responses.append(response)
            (folder/'port-response.json').write_text(json.dumps(response,indent=2),encoding='utf-8')
            label=f'{resistance:g} Ω load'
            result['series'] += [dict(name=f'Reflection S11 · {label}',x=response['frequency'],y=response['reflection_db'],x_unit='MHz',y_unit='dB'),
                                 dict(name=f'Load/input voltage · {label}',x=response['frequency'],y=response['transfer_db'],x_unit='MHz',y_unit='dB')]
            result['metrics'].append(dict(name=f'Worst reflection ({label})',value=max(response['reflection_db']),unit='dB'))
            if response['masked']:
                result['warnings'].append(f'{response["masked"]} frequency samples excluded for insufficient excitation in {name}.')
        difference=max(responses[0]['reflection_db'])-max(responses[1]['reflection_db'])
        result['status']='complete'
        result['title']=f'{model["net"]}: local trace field simulation'
        result['summary']=f'openEMS simulated a {model["length"]:.2f} mm straight section and compared {termination:g} Ω and {termination+extra:g} Ω loads. Lower S11 means less reflection in this simplified model.'
        result['metrics'].append(dict(name='Compared load worst-reflection reduction',value=difference,unit='dB'))
        result['assumptions'].append(f'Synthetic source resistance {inputs["source_ohm"]:g} Ω; baseline load {termination:g} Ω; compared load {termination+extra:g} Ω.')
        if difference>3 and max(responses[0]['reflection_db'])>-30:
            result['recommendations']=[f'Review a {termination+extra:g} Ω effective load: the local model predicts {difference:.1f} dB less worst-case reflection. Check driver current, logic levels and the complete route before changing termination.']
        else:
            result['recommendations']=['The compared resistance does not clearly reduce worst-case reflection. Review the complete route and actual driver/receiver impedances before changing termination.']
        result['recommendations'].append('Confirm promising changes with circuit simulation and a measurement; these port curves do not predict an EMI compliance margin.')
    except InterruptedError as exc:
        result.update(status='cancelled',summary=str(exc),series=[],metrics=[])
    except ModelUnavailable as exc:
        result.update(status='needs_information',summary='The automatic field model needs different inputs.',missing=[str(exc)],series=[],metrics=[])
    except (OSError,ValueError,TimeoutError,subprocess.SubprocessError) as exc:
        result.update(status='error',summary=str(exc),series=[],metrics=[])
        result['recommendations']=['Inspect the saved solver log. A timed-out or unconverged field run is not a passing result.']
    result['artifacts']=[str(p) for p in sorted(workdir.rglob('*')) if p.is_file() and
                         (p.suffix in ('.xml','.json','.log','.txt') or p.name.startswith('port_'))]
    return [result]
