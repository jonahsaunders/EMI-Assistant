"""Bounded, generated passive-circuit simulations using the actual ngspice solver.

These are small-signal comparison models, not whole-board emission predictions.
Only numeric R/L/C elements generated here reach the solver; arbitrary schematic
SPICE directives, models and control blocks are never executed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .geometry import RouteGraph


class SolverFailure(RuntimeError):
    pass


class SolverCancelled(SolverFailure):
    pass


def _number(value, default, low, high):
    try:
        value = float(value)
        return value if math.isfinite(value) and low <= value <= high else default
    except (TypeError, ValueError):
        return default


def component_value(text, kind):
    """Conservative KiCad engineering values; rejects SPICE/code expressions."""
    text = str(text).strip().replace("µ", "u").replace("μ", "u").replace("Ω", "ohm")
    # IEC middle multiplier, e.g. 4k7, 2R2, 4n7. Trailing units are optional.
    match = re.fullmatch(r"(\d+)([RrKkMmunp])(\d+)([FfHh]|[oO][hH][mM])?", text)
    if match:
        if (match[4] and match[4].lower() != {"C": "f", "L": "h", "R": "ohm"}[kind]) or (match[2] in "Rr" and kind != "R"):
            return None
        scale = {"r": 1, "R": 1, "k": 1e3, "K": 1e3, "M": 1e6,
                 "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12}[match[2]]
        value = float(match[1] + "." + match[3]) * scale
    else:
        match = re.fullmatch(r"([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(meg|[TGMKkmunpfRr]?)([FfHh]|[oO][hH][mM])?", text)
        if not match:
            return None
        suffix = match[2]
        # A bare 'F' can only be a capacitance unit, not a prefix here.
        scale = {"": 1, "R": 1, "r": 1, "T": 1e12, "G": 1e9,
                 "M": 1e6, "meg": 1e6, "K": 1e3, "k": 1e3,
                 "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15}[suffix]
        units = (match[3] or "").lower()
        if units and units != {"C": "f", "L": "h", "R": "ohm"}[kind]:
            return None
        value = float(match[1]) * scale
    limits = {"C": (1e-15, 1.), "L": (1e-12, 100.), "R": (0., 1e12)}
    return value if math.isfinite(value) and limits[kind][0] <= value <= limits[kind][1] else None


def _base(identity, title):
    return {"id": "ngspice-" + hashlib.sha256(identity.encode()).hexdigest()[:16],
            "engine": "ngspice", "title": title, "status": "needs_information", "summary": "",
            "assumptions": [], "missing": [], "warnings": [], "metrics": [], "series": [],
            "recommendations": [], "item_ids": [], "artifacts": []}


def _components(board, circuit):
    metadata = {str(c.get("reference")): c for c in (circuit or {}).get("components", []) if c.get("board_match") is True}
    for fp in board.footprints:
        props = dict(fp.properties)
        matched = metadata.get(fp.reference, {})
        props.update(matched.get("properties") or {})
        if any(str(props.get(k, "")).lower() in {"yes", "true", "1"}
               for k in ("DNP", "dnp", "exclude_from_sim")):
            continue
        # Pad topology on the board remains the source of physical identities.
        pads = {p.number: p for p in fp.pads if p.net}
        if len(pads) != 2:
            continue
        declared_kind = str(props.get("Sim.Device", "")).upper()
        kind = declared_kind if declared_kind in {"C", "L", "R"} else fp.reference[:1].upper()
        if kind not in {"C", "L", "R"}:
            continue
        value = component_value(matched.get("value") or fp.value, kind)
        if value is not None:
            yield {"fp": fp, "kind": kind, "value": value, "pads": list(pads.values())}


def _settings(settings):
    s = settings.get("simulation", {})
    low = _number(s.get("frequency_start_mhz"), .1, .000001, 1e5) * 1e6
    high = _number(s.get("frequency_stop_mhz"), 500., .000001, 1e5) * 1e6
    if high <= low:
        raise SolverFailure("The simulation stop frequency must exceed the start frequency.")
    return {"low": low, "high": high,
            "points": int(_number(s.get("points"), 121, 21, 2001)),
            "esr": _number(s.get("capacitor_esr_ohm"), .03, 0, 1000),
            "esl": _number(s.get("capacitor_esl_nh"), .8, 0, 10000) * 1e-9,
            "cap": _number(s.get("candidate_cap_nf"), 100, .001, 1e6) * 1e-9,
            "series_r": _number(s.get("candidate_series_ohm"), 22, 0, 1e6),
            "source": _number(s.get("source_ohm"), 50, .000001, 1e6),
            "load": _number(s.get("termination_ohm"), 50, .000001, 1e6),
            "seconds": _number(s.get("max_seconds"), 90, 5, 3600),
            "jobs": int(_number(s.get("max_jobs"), 3, 1, 8))}


def _deck(elements, config):
    density = max(3, math.ceil((config["points"] - 1) / math.log10(config["high"] / config["low"])))
    sweep = f"lin {config['points']}" if config["high"] / config["low"] < 1.1 else f"dec {density}"
    return "\n".join(["EMI Assistant generated passive comparison", *elements,
                       ".options numdgt=15", ".save v(base) v(candidate)",
                       f'.ac {sweep} {config["low"]:.12g} {config["high"]:.12g}',
                       ".end", ""])


def _branch(name, node, capacitance, resistance, inductance):
    # Zero ESR/ESL means an ideal connection: omit the element rather than
    # inserting a hidden floor, a singular zero inductor or a zero resistor.
    elements = []
    current = node
    if resistance > 0:
        elements.append(f"R{name} {current} n{name} {resistance:.12g}")
        current = f"n{name}"
    if inductance > 0:
        elements.append(f"L{name} {current} m{name} {inductance:.12g}")
        current = f"m{name}"
    elements.append(f"C{name} {current} 0 {capacitance:.12g}")
    return elements


def _cap_jobs(board, settings, components, config):
    references = set(settings.get("reference_nets") or ["GND"])
    groups = {}
    for c in components:
        a, b = c["pads"]
        if c["kind"] == "C" and ((a.net in references) != (b.net in references)):
            signal, ground = (b, a) if a.net in references else (a, b)
            groups.setdefault((signal.net, ground.net), []).append((c, signal, ground))
    graphs = {}
    # Power-like names get priority without declaring unrecognised nets power rails.
    ordered = sorted(groups, key=lambda k: (not bool(re.search(r"(^[+]|VCC|VDD|VIN|VOUT|POWER)", k[0], re.I)), -len(groups[k]), k))
    for net, ground in ordered[:config["jobs"]]:
        caps = groups[net, ground][:32]
        ports = [(fp, p) for fp in board.footprints if fp.reference.upper().startswith("U")
                 for p in fp.pads if p.net == net]
        target_fp, target = min(ports, key=lambda x: min(math.dist(x[1].position, c[1].position) for c in caps)) if ports else (caps[0][0]["fp"], caps[0][1])
        result = _base("cap:" + net + ":" + ground, f"Shunt-capacitor impedance: {net}")
        result["location"] = list(target.position)
        result["item_ids"] = [target.id] + [p.id for c, a, b in caps for p in (a, b)]
        result["assumptions"] = [f"Small-signal 1 A AC current at {target_fp.reference} pad {target.number}; voltage magnitude equals impedance in ohms.",
            f"Nominal capacitor values; each capacitor has assumed ESR {config['esr']:g} ohm and ESL {config['esl']*1e9:g} nH.",
            "Connection inductance uses a rough 0.7 nH/mm rule of thumb; it is not a field-extracted parasitic model.",
            "Only the listed capacitor branches are modeled; regulator, IC, plane spreading, shared-path coupling and cable impedances are omitted.",
            f"Comparison adds {config['cap']*1e9:g} nF at the port with an assumed 1 mm connection and the same ESR/ESL."]
        result["warnings"] = ["Estimated passive-network impedance, not radiated emissions or a compliance result.",
                              "DC bias, capacitor tolerances and frequency-dependent losses are not characterized."]
        elems = ["Ibase 0 base DC 0 AC 1", "Icandidate 0 candidate DC 0 AC 1", "Rdcbase base 0 1e12", "Rdccandidate candidate 0 1e12"]
        lengths = []
        for index, (c, pad, return_pad) in enumerate(caps):
            if net not in graphs:
                graphs[net] = RouteGraph(board, net) if len(board.tracks) + len(board.pads) < 20000 else None
            path = graphs[net].shortest(target, pad) if graphs[net] is not None else None
            if path is not None:
                length = path.length
                result["item_ids"].extend(path.item_ids)
                detail = f"{c['fp'].reference}: measured signal route {length:.3g} mm"
            else:
                length = math.dist(target.position, pad.position)
                detail = f"{c['fp'].reference}: estimated straight-line signal distance {length:.3g} mm; explicit route unresolved"
                result["warnings"].append("Unresolved routes may use copper pours or may be unrouted; physical connectivity must be checked.")
            return_targets = [p for p in target_fp.pads if p.net == ground]
            if return_targets:
                if ground not in graphs:
                    graphs[ground] = RouteGraph(board, ground) if len(board.tracks) + len(board.pads) < 20000 else None
                back = graphs[ground].shortest_many([return_pad], return_targets) if graphs[ground] is not None else None
                if back is not None:
                    length += back.length
                    detail += f"; measured return route {back.length:.3g} mm."
                    result["item_ids"].extend(back.item_ids)
                else:
                    return_length = min(math.dist(return_pad.position, p.position) for p in return_targets)
                    length += return_length
                    detail += f"; return distance estimated at {return_length:.3g} mm (plane/current path unresolved)."
            else:
                length += 1.
                detail += "; assumed 1 mm local ground connection; port return location is unknown."
            lengths.append(length)
            result["assumptions"].append(detail)
            inductance = config["esl"] + max(length, .01) * .7e-9
            for tag, node in (("b", "base"), ("c", "candidate")):
                elems += _branch(f"{tag}{index}", node, c["value"], config["esr"], inductance)
        elems += _branch("extra", "candidate", config["cap"], config["esr"], config["esl"] + .7e-9)
        result["warnings"] = list(dict.fromkeys(result["warnings"]))
        result["item_ids"] = sorted(set(result["item_ids"]))
        result["metrics"] = [{"name": "Modeled capacitor branches", "value": len(caps), "unit": "count"},
                             {"name": "Longest modeled connection", "value": max(lengths), "unit": "mm"}]
        yield {"result": result, "deck": _deck(elems, config), "quantity": "impedance", "net": net}


def _filter_jobs(board, settings, components, config):
    refs = set(settings.get("reference_nets") or ["GND"])
    for series in components:
        if series["kind"] not in {"R", "L"}:
            continue
        p, q = series["pads"]
        if p.net == q.net or p.net in refs or q.net in refs:
            continue
        # Each output direction is explicit; select the side with shunt caps.
        options = []
        for source, output in ((p, q), (q, p)):
            caps = [c for c in components if c["kind"] == "C" and
                    any(x.net == output.net for x in c["pads"]) and any(x.net in refs for x in c["pads"])]
            grounds = {x.net for c in caps for x in c["pads"] if x.net in refs}
            if caps and len(grounds) == 1:
                options.append((source, output, caps))
        if len(options) != 1:
            continue  # Ambiguous pi network/ports require information, never guess direction.
        source, output, caps = options[0]
        caps = caps[:32]
        result = _base("filter:" + series["fp"].reference, f"Passive {series['kind']}C stage: {series['fp'].reference}")
        result["location"] = list(series["fp"].position)
        result["item_ids"] = [x.id for c in [series, *caps] for x in c["pads"]]
        result["assumptions"] = [f"Isolated small-signal stage from {source.net} through {series['fp'].reference} to {output.net}; source {config['source']:g} ohm and load {config['load']:g} ohm are assumptions.",
            f"Nominal component values, capacitor ESR {config['esr']:g} ohm and ESL {config['esl']*1e9:g} nH; connections treated as ideal.",
            f"Comparison adds {config['series_r']:g} ohm in series with {series['fp'].reference}.",
            "All other connected circuitry is omitted; this is a local topology experiment, not full-circuit simulation."]
        if series["kind"] == "L":
            result["assumptions"].append("Inductor uses an ideal inductance and assumed 0.1 ohm winding resistance; self-resonance and saturation are unknown.")
        result["warnings"] = ["Compare attenuation only within the frequency range where these lumped component assumptions are valid.",
            "Added resistance can cause DC voltage drop, heating and slower signals; load current and timing must be checked before applying.",
            "This does not predict regulator stability, switching waveforms or radiated emissions."]
        elems = ["Vinput vin 0 DC 0 AC 1"]
        for tag, out in (("b", "base"), ("c", "candidate")):
            elems.append(f"Rsource{tag} vin n{tag} {config['source']:.12g}")
            stage_in = f"m{tag}" if tag == "c" and config["series_r"] > 0 else f"n{tag}"
            if tag == "c" and config["series_r"] > 0:
                elems.append(f"Rextra{tag} n{tag} m{tag} {config['series_r']:.12g}")
            if series["kind"] == "L":
                elems += [f"Rdcr{tag} {stage_in} l{tag} .1", f"Lstage{tag} l{tag} {out} {series['value']:.12g}"]
            elif series["value"]:
                elems.append(f"Rstage{tag} {stage_in} {out} {series['value']:.12g}")
            else:
                elems.append(f"Vwire{tag} {stage_in} {out} DC 0 AC 0")
            elems.append(f"Rload{tag} {out} 0 {config['load']:.12g}")
            for index, cap in enumerate(caps):
                elems += _branch(f"{tag}{index}", out, cap["value"], config["esr"], config["esl"])
        yield {"result": result, "deck": _deck(elems, config), "quantity": "gain", "net": output.net}
        if series["kind"] == "L":
            total_cap = sum(c["value"] for c in caps)
            rates = []
            for added in (0., config["series_r"]):
                resistance = config["source"] + .1 + added
                alpha = .5 * (resistance / series["value"] + 1 / (config["load"] * total_cap))
                omega2 = (1 + resistance / config["load"]) / (series["value"] * total_cap)
                rate = omega2 / (alpha + math.sqrt(max(0., alpha * alpha - omega2))) if alpha * alpha > omega2 else alpha
                rates.append(rate)
            duration = max(1e-9, 18 / min(rates))
            # 1 V step is an experiment, never an inferred switching waveform.
            delay, rise = duration * .02, duration / 10000
            step_elems = list(elems)
            step_elems[0] = f"Vinput vin 0 PULSE(0 1 {delay:.12g} {rise:.12g} {rise:.12g} {duration*2:.12g} {duration*3:.12g})"
            transient = copy.deepcopy(result)
            transient["id"] += "-step"
            transient["title"] = f"LC ringing and settling: {series['fp'].reference}"
            transient["assumptions"].append(f"Assumed 1 V input step with {rise*1e9:.3g} ns rise time; simulated {duration*1e6:.3g} us. This is not the actual switching edge.")
            deck = "\n".join(["EMI Assistant generated passive step comparison", *step_elems,
                ".options numdgt=15 reltol=0.00001", ".save v(base) v(candidate)",
                f".tran {duration/1600:.12g} {duration:.12g} 0 {duration/1600:.12g}", ".end", ""])
            final = [config["load"] / (config["load"] + config["source"] + .1 + extra) for extra in (0., config["series_r"])]
            yield {"result": transient, "deck": deck, "quantity": "transient", "net": output.net,
                   "duration": duration, "delay": delay, "rise": rise, "final": final}


def parse_raw(path, transient=False):
    """Read ngspice ASCII AC raw output, rejecting partial/nonfinite results."""
    path = Path(path)
    if not path.exists() or path.stat().st_size > 16 * 1024 * 1024:
        raise SolverFailure("ngspice did not produce a bounded result file.")
    text = path.read_text(encoding="utf-8", errors="strict")
    flags = "Flags: real" if transient else "Flags: complex"
    axis = "time" if transient else "frequency"
    if flags not in text or "Values:" not in text:
        raise SolverFailure("ngspice output is missing the requested analysis result.")
    head, values = text.split("Values:", 1)
    nv = re.search(r"No\. Variables:\s*(\d+)", head)
    np = re.search(r"No\. Points:\s*(\d+)", head)
    if not nv or not np:
        raise SolverFailure("ngspice output is missing sample counts.")
    nv, np = int(nv[1]), int(np[1])
    if not 3 <= nv <= 2048 or not 3 <= np <= 100000:
        raise SolverFailure("ngspice returned an unexpected sample count.")
    variables = re.findall(r"^[ \t]*\d+[ \t]+(\S+)[ \t]+\S+(?:[ \t]+[^\n]*)?$", head.split("Variables:")[-1], re.M)
    if len(variables) != nv or not all(n in variables for n in (axis, "v(base)", "v(candidate)")):
        raise SolverFailure("ngspice output is missing the requested voltages.")
    lines = [line.strip() for line in values.splitlines() if line.strip()]
    if len(lines) != nv * np:
        raise SolverFailure("ngspice result is truncated.")
    result = {name: [] for name in variables}
    for point in range(np):
        for index, name in enumerate(variables):
            fields = lines[point * nv + index].split()
            if index == 0:
                if not fields or fields[0] != str(point):
                    raise SolverFailure("ngspice result has invalid sample indices.")
                fields = fields[1:]
            try:
                pair = "".join(fields).split(",")
                real, imag = (float(pair[0]), float(pair[1])) if len(pair) == 2 else (float(pair[0]), 0.)
                if not math.isfinite(real) or not math.isfinite(imag):
                    raise ValueError("nonfinite")
            except (ValueError, IndexError):
                raise SolverFailure("ngspice returned a nonfinite or malformed sample.") from None
            result[name].append(complex(real, imag))
    samples = [x.real for x in result[axis]]
    if samples[0] < 0 or (not transient and samples[0] == 0) or any(a >= b for a, b in zip(samples, samples[1:])):
        raise SolverFailure("ngspice sample coordinates are invalid.")
    transform = (lambda x: x.real) if transient else abs
    return samples, [transform(x) for x in result["v(base)"]], [transform(x) for x in result["v(candidate)"]]


def _execute(deck, directory, runtime, seconds, cancel_event=None, transient=False):
    directory.mkdir(parents=True, exist_ok=True)
    source, raw, log = directory / "comparison.cir", directory / "result.raw", directory / "solver.log"
    source.write_text(deck, encoding="ascii")
    raw.unlink(missing_ok=True)
    if runtime.get("kind") == "shared":
        command = [sys.executable, str(Path(__file__).with_name("sim_ngspice_worker.py")), str(Path(runtime["path"]).resolve()), str(source), str(raw)]
    else:
        command = [runtime["path"], "-n", "-b", "-r", str(raw), str(source)]
    env = os.environ.copy()
    env["SPICE_ASCIIRAWFILE"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    for key in ("TMPDIR", "TEMP", "TMP"):
        env[key] = str(directory)
    start = time.monotonic()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with log.open("wb") as output:
        try:
            process = subprocess.Popen(command, cwd=directory, stdout=output, stderr=subprocess.STDOUT,
                                       env=env, creationflags=flags)
        except OSError as exc:
            raise SolverFailure(f"ngspice could not start: {exc}") from exc
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                process.kill()
                process.wait()
                raise SolverCancelled("Simulation was cancelled.")
            if time.monotonic() - start > seconds:
                process.kill()
                process.wait()
                raise SolverFailure(f"ngspice exceeded its {seconds:g} second time limit.")
            if log.stat().st_size > 8 * 1024 * 1024:
                process.kill()
                process.wait()
                raise SolverFailure("ngspice stopped because its diagnostic output exceeded the size limit.")
            time.sleep(.05)
    diagnostic = log.read_text(encoding="utf-8", errors="replace")
    fatal = re.search(r"(?:^|\n)\s*(?:stderr\s+)?(?:fatal|error)\b|singular matrix|timestep too small|simulation interrupted|doanalyses:|no circuit loaded|no simulations run", diagnostic, re.I)
    if process.returncode or fatal:
        detail = fatal.group(0).strip() if fatal else f"exit {process.returncode}"
        raise SolverFailure(f"ngspice could not complete the circuit ({detail}); see solver.log.")
    return parse_raw(raw, transient=transient)


def _finish(job, values, config):
    result = job["result"]
    frequencies, baseline, candidate = values
    if not (frequencies[0] <= config["low"] * 1.001 and frequencies[-1] >= config["high"] * .88):
        raise SolverFailure("ngspice did not cover the requested frequency band.")
    if min(baseline) <= 0 or min(candidate) <= 0 or max(baseline + candidate) > 1e15:
        raise SolverFailure("ngspice returned an unusable comparison response.")
    peak = max(baseline)
    at = baseline.index(peak)
    improvement = 20 * math.log10(baseline[at] / candidate[at])
    worsening = max(20 * math.log10(c / b) for b, c in zip(baseline, candidate))
    result["metrics"].extend([
        {"name": "Baseline peak frequency", "value": frequencies[at] / 1e6, "unit": "MHz"},
        {"name": "Reduction at baseline peak (positive is lower)", "value": improvement, "unit": "dB"},
        {"name": "Worst increase anywhere in band", "value": max(0., worsening), "unit": "dB"}])
    change_text = f"{abs(improvement):.1f} dB {'lower' if improvement >= 0 else 'higher'}"
    if job["quantity"] == "impedance":
        ybase, ycand, unit = baseline, candidate, "ohm"
        result["metrics"] += [{"name": "Baseline peak impedance", "value": peak, "unit": "ohm"},
                              {"name": "Candidate peak impedance", "value": max(candidate), "unit": "ohm"}]
        result["summary"] = f"Actual ngspice AC solution of an estimated capacitor network. Adding {config['cap']*1e9:g} nF changes impedance by {change_text} at the baseline peak ({frequencies[at]/1e6:.3g} MHz)."
        result["recommendations"] = ["Inspect both curves before adding a capacitor: a new anti-resonance can worsen another part of the band.",
                                     "Confirm capacitor ESR/ESL and DC-bias capacitance, then measure the rail impedance or ripple near the flagged frequency."]
    else:
        ybase, ycand, unit = [20 * math.log10(x) for x in baseline], [20 * math.log10(x) for x in candidate], "dB"
        result["summary"] = f"Actual ngspice AC solution of an isolated passive stage. Adding {config['series_r']:g} ohm changes output by {change_text} at the baseline peak, under the stated source/load assumptions."
        result["recommendations"] = ["Review the source/load impedances and component frequency limits before choosing damping resistance.",
                                     "Check DC voltage drop, resistor power and required signal bandwidth; compare the response on hardware."]
    result["series"] = [{"name": "Baseline (estimated model)", "x": [f / 1e6 for f in frequencies], "y": ybase, "x_unit": "MHz", "y_unit": unit},
                        {"name": "With proposed change (estimated model)", "x": [f / 1e6 for f in frequencies], "y": ycand, "x_unit": "MHz", "y_unit": unit}]
    if worsening > 1.:
        result["warnings"].append(f"The proposed change worsens the response by up to {worsening:.1f} dB elsewhere in this band.")
    result["status"] = "complete"


def _finish_transient(job, values, config):
    result = job["result"]
    times, base, candidate = values
    if times[-1] < job["duration"] * .999 or times[0] > job["delay"]:
        raise SolverFailure("ngspice did not cover the complete unit-step response.")
    overshoots, settling = [], []
    for label, waveform, final in zip(("Baseline", "Candidate"), (base, candidate), job["final"]):
        if abs(waveform[-1] - final) > .01 * final or abs(waveform[0]) > .01:
            raise SolverFailure("The LC step response did not reach the expected DC value; extend/inspect the model before interpreting ringing.")
        overshoot = max(0., (max(waveform) / final - 1) * 100)
        overshoots.append(overshoot)
        outside = [i for i, (t, y) in enumerate(zip(times, waveform)) if t >= job["delay"] and abs(y - final) > .02 * final]
        index = min(len(times) - 1, outside[-1] + 1) if outside else next(i for i, t in enumerate(times) if t >= job["delay"])
        settle = max(0., times[index] - job["delay"])
        settling.append(settle)
        result["metrics"] += [{"name": label + " overshoot above its DC level", "value": overshoot, "unit": "%"},
            {"name": label + " 2% settling time after input step", "value": settle * 1e6, "unit": "us"},
            {"name": label + " final output for 1 V input", "value": final, "unit": "V"}]
    result["summary"] = f"Actual ngspice transient solution: predicted overshoot changes from {overshoots[0]:.1f}% to {overshoots[1]:.1f}% with {config['series_r']:g} ohm added, for the assumed 1 V step and passive stage."
    result["recommendations"] = ["Compare overshoot, settling and the lower DC output together before selecting damping resistance.",
        "Confirm the real source impedance, load and switching rise time, then measure ringing at this stage."]
    result["series"] = [{"name": label + " (assumed 1 V step)", "x": [t * 1e6 for t in times], "y": wave,
                         "x_unit": "us", "y_unit": "V"} for label, wave in (("Baseline", base), ("With proposed resistance", candidate))]
    result["status"] = "complete"


def run(board, settings, workdir: Path, runtime: dict, circuit=None, cancel_event=None, progress=None):
    """Automatically compare eligible local passive networks in bounded jobs."""
    workdir = Path(workdir).resolve()
    if cancel_event is not None and cancel_event.is_set():
        r = _base("cancelled", "Circuit simulations")
        r.update(status="cancelled", summary="Simulation was cancelled.")
        return [r]
    if not runtime.get("available") or not runtime.get("path"):
        r = _base("runtime", "Circuit simulations")
        r.update(status="unavailable", summary=runtime.get("message") or "ngspice is not installed.", missing=["ngspice runtime"])
        return [r]
    try:
        config = _settings(settings)
        components = list(_components(board, circuit))
        filters = list(_filter_jobs(board, settings, components, config))[:2]
        caps = list(_cap_jobs(board, settings, components, config))
        jobs = (caps[:max(0, config["jobs"] - len(filters))] + filters)[:config["jobs"]]
    except Exception as exc:
        r = _base("prepare", "Circuit simulations")
        r.update(status="error", summary=f"Could not prepare the passive circuit model: {exc}")
        return [r]
    if not jobs:
        r = _base("inputs", "Circuit simulations")
        r["summary"] = "No supported passive network was found. Layout analysis still applies."
        r["missing"] = ["A two-terminal capacitor with a numeric value between a signal/power net and a configured reference net, or an unambiguous RC/LC stage."]
        r["warnings"] = ["Custom IC and behavioral models are not executed automatically; unsupported circuits are not treated as passing."]
        return [r]
    deadline = time.monotonic() + config["seconds"]
    results = []
    for job in jobs:
        result = job["result"]
        result["warnings"].append("Full IC and custom behavioral models are not executed. A completed passive comparison does not validate the whole circuit or any custom IC model.")
        if config["esr"] == 0 or config["esl"] == 0:
            result["assumptions"].append("Zero-valued capacitor ESR/ESL settings are modeled as ideal connections by omitting those elements; separate estimated connection inductance may still be included.")
        directory = workdir / result["id"]
        directory.mkdir(parents=True, exist_ok=True)
        result["artifacts"] = [str(directory / name) for name in ("comparison.cir", "solver.log", "result.raw", "model.json")]
        (directory / "comparison.cir").write_text(job["deck"], encoding="ascii")
        (directory / "model.json").write_text(json.dumps({k: v for k, v in result.items() if k != "artifacts"}, indent=2), encoding="utf-8")
        if progress:
            progress("ngspice: " + result["title"])
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise SolverCancelled("Simulation was cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SolverFailure("Circuit simulation time budget was exhausted.")
            values = _execute(job["deck"], directory, runtime, remaining, cancel_event, transient=job["quantity"] == "transient")
            (_finish_transient if job["quantity"] == "transient" else _finish)(job, values, config)
        except SolverCancelled as exc:
            result.update(status="cancelled", summary=str(exc))
        except (SolverFailure, OSError, ValueError) as exc:
            result.update(status="error", summary=str(exc))
        result["artifacts"] = [p for p in result["artifacts"] if Path(p).exists()]
        results.append(result)
    return results
