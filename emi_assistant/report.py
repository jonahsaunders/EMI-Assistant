"""Portable analysis reports, with evidence and coverage kept together."""
from __future__ import annotations
from datetime import datetime, timezone
from html import escape
import json
import math
from pathlib import Path
from .models import AnalysisResult
from .storage import write_json

INTERPRETATION = (
    "Layout screening, diagnostic hypotheses, and scoped numerical models. "
    "Simulation results depend on the stated geometry, excitation, component models, and assumptions. "
    "They do not predict whole-board emissions or establish regulatory compliance."
)


def _esc(value):
    return escape(str(value), quote=True)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clean_simulations(simulations):
    """Keep malformed numeric results out of both JSON and rendered evidence.

    Solver output is normally validated by SimulationManager. Report files may
    also be made from manually constructed results, so fail visibly there too.
    """
    cleaned = []
    for original in simulations:
        if not isinstance(original, dict):
            continue
        item = dict(original)
        warnings = list(item.get("warnings", []))
        metrics = []
        for metric in item.get("metrics", []):
            if isinstance(metric, dict) and _finite(metric.get("value")):
                metrics.append(metric)
            else:
                warnings.append("A malformed or non-finite simulation metric was omitted from this report.")
        series = []
        for curve in item.get("series", []):
            if not isinstance(curve, dict):
                warnings.append("A malformed simulation curve was omitted from this report.")
                continue
            x, y = curve.get("x", []), curve.get("y", [])
            valid = (isinstance(x, (list, tuple)) and isinstance(y, (list, tuple))
                     and len(x) == len(y) and len(x) >= 2
                     and all(_finite(v) for v in [*x, *y])
                     and all(x[i] < x[i + 1] for i in range(len(x) - 1)))
            if valid:
                series.append(curve)
            else:
                warnings.append(f"Curve {curve.get('name', 'unnamed')!r} was omitted: samples must be finite, paired, and increasing on the x axis.")
        item.update(metrics=metrics, series=series, warnings=list(dict.fromkeys(warnings)))
        if item.get("status") == "complete" and not (metrics or series):
            item.update(status="error", summary="No valid numerical evidence remains in this simulation result. Review its warnings and solver logs.")
        cleaned.append(item)
    return cleaned


def _plot_series(series):
    """Portable native SVG plots; incompatible units always get separate axes."""
    groups = {}
    for curve in series:
        key = (str(curve.get("x_unit", "")), str(curve.get("y_unit", "")))
        groups.setdefault(key, []).append(curve)
    figures = []
    colors = ("#087f8c", "#ae4d13", "#6653a3", "#267449", "#bd3863", "#47627b")
    for (x_unit, y_unit), curves in groups.items():
        xs = [x for c in curves for x in c["x"]]
        ys = [y for c in curves for y in c["y"]]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        logarithmic = (x_unit in {"Hz", "kHz", "MHz", "GHz"} and xmin > 0
                       and math.log10(xmax) - math.log10(xmin) >= 2)
        # Scaling before subtraction avoids overflow for finite extreme values.
        xscale, yscale = max(abs(xmin), abs(xmax), 1e-300), max(abs(ymin), abs(ymax), 1e-300)
        tx = (lambda x: math.log10(x)) if logarithmic else (lambda x: x / xscale)
        left, right = tx(xmin), tx(xmax)
        bottom, top = ymin / yscale, ymax / yscale
        if top == bottom:
            limit = math.nextafter(math.inf, 0) / yscale
            bottom, top = max(bottom - .1, -limit), min(top + .1, limit)
        xp = lambda x: 84 + (tx(x) - left) / (right - left) * 588
        yp = lambda y: 232 - (y / yscale - bottom) / (top - bottom) * 204
        x_label = ("Frequency" if x_unit in {"Hz", "kHz", "MHz", "GHz"} else "Time"
                   if x_unit in {"s", "ms", "us", "µs", "ns"} else "X")
        x_label += f" ({x_unit or 'unit unspecified'}; {'log' if logarithmic else 'linear'} scale)"
        y_label = y_unit or "Y unit unspecified"
        elements = [f'<svg class="curve-plot" viewBox="0 0 720 302" role="img" aria-label="{_esc(y_label)} versus {_esc(x_label)}" xmlns="http://www.w3.org/2000/svg">',
                    f'<title>{_esc(y_label)} versus {_esc(x_label)}</title>',
                    '<rect x="84" y="28" width="588" height="204" fill="#fff" stroke="#b7c5cd"/>']
        for i in range(5):
            t = i / 4
            x = 84 + t * 588
            y = 232 - t * 204
            xv = (xmin if i == 0 else xmax if i == 4 else 10 ** (left + t * (right - left))) if logarithmic else xmin * (1 - t) + xmax * t
            yv = ymin * (1 - t) + ymax * t
            if ymin == ymax:
                yv = (bottom * (1 - t) + top * t) * yscale
            elements.extend([
                f'<path d="M{x:.2f} 28V232 M84 {y:.2f}H672" stroke="#e2e8ec" fill="none"/>',
                f'<text x="{x:.2f}" y="252" text-anchor="middle">{_esc(format(xv, ".4g"))}</text>',
                f'<text x="76" y="{y + 4:.2f}" text-anchor="end">{_esc(format(yv, ".4g"))}</text>'])
        legends = []
        for index, curve in enumerate(curves):
            color = colors[index % len(colors)]
            dash = ' stroke-dasharray="7 4"' if index % 2 else ''
            points = " ".join(f"{xp(x):.2f},{yp(y):.2f}" for x, y in zip(curve["x"], curve["y"]))
            elements.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"{dash}><title>{_esc(curve.get("name", "Curve"))}</title></polyline>')
            style = "dashed" if index % 2 else "solid"
            legends.append(f'<span class="curve-key"><span style="border-top:3px {style} {color}"></span>{_esc(curve.get("name", "Curve"))}</span>')
        elements.extend([f'<text x="378" y="284" text-anchor="middle">{_esc(x_label)}</text>',
                         f'<text transform="translate(18 130) rotate(-90)" text-anchor="middle">{_esc(y_label)}</text>', '</svg>'])
        figures.append('<figure>' + ''.join(elements) + '<figcaption>' + ''.join(legends) + '</figcaption></figure>')
    return ''.join(figures)


def _simulation_cards(simulations):
    cards = []
    names = {"complete": "Complete", "needs_information": "Needs information", "unavailable": "Unavailable",
             "error": "Error", "cancelled": "Cancelled"}
    for result in simulations:
        state = names.get(result.get("status"), "Unknown status")
        identity = str(result.get("engine", "Solver"))
        if result.get("engine_version"):
            identity += " " + str(result["engine_version"])
        if "cached" in result:
            identity += " · " + ("Reused matching cached result" if result["cached"] else "New result")
        evidence = []
        for key, title in (("assumptions", "Model scope and assumptions"), ("missing", "Needs information"),
                           ("warnings", "Warnings"), ("recommendations", "Suggested next steps")):
            if result.get(key):
                evidence.append(f'<h3>{title}</h3><ul>' + ''.join(f'<li>{_esc(x)}</li>' for x in result[key]) + '</ul>')
        if result.get("artifacts"):
            evidence.append('<h3>Local solver evidence files</h3><ul>' + ''.join(
                f'<li><code>{_esc(x)}</code></li>' for x in result["artifacts"]) + '</ul>')
        metrics = ''.join(f'<tr><th scope="row">{_esc(m.get("name", "Metric"))}</th><td>{_esc(format(m["value"], ".6g"))}</td><td>{_esc(m.get("unit", ""))}</td></tr>'
                          for m in result.get("metrics", []))
        table = '<table><caption>Numerical model results</caption><tr><th>Metric</th><th>Value</th><th>Unit</th></tr>' + metrics + '</table>' if metrics else ''
        extra = ''
        if result.get("schematic_status"):
            extra += f'<p class="meta">Saved schematic status: {_esc(result["schematic_status"])}</p>'
        if result.get("scope"):
            extra += f'<p><strong>Scope:</strong> {_esc(result["scope"])}</p>'
        cards.append(f'<article class="simulation"><p class="meta">{_esc(identity)} · <strong>{_esc(state)}</strong></p>'
                     f'<h3>{_esc(result.get("title", "Simulation"))}</h3><p>{_esc(result.get("summary", ""))}</p>'
                     + extra + table + _plot_series(result.get("series", [])) + ''.join(evidence) + '</article>')
    return ''.join(cards) or '<p>No simulation results are available for this analysis. Layout findings do not imply simulation coverage.</p>'

def export_report(result: AnalysisResult, path: str, changes: dict | None = None, measurements: list | None = None) -> None:
    destination = Path(path)
    payload = result.to_dict()
    payload.update({"generated_at": datetime.now(timezone.utc).isoformat(), "revision_changes": changes or {}, "measurements": measurements or [],
                    "interpretation": INTERPRETATION})
    payload["simulations"] = _clean_simulations(payload.get("simulations", []))
    if destination.suffix.lower() != ".html":
        write_json(destination, payload)
        return
    esc = lambda v: escape(str(v), quote=True)
    cards = []
    for f in result.findings:
        evidence = "".join(f"<li>{esc(x)}</li>" for x in f.evidence)
        missing = "".join(f"<li>{esc(x)}</li>" for x in f.missing)
        links = " ".join(f'<a href="{esc(u)}">Engineering reference</a>' for u in f.sources if u.startswith("https://"))
        cards.append(f'<article><p class="meta">{esc(f.priority)} priority · {esc(f.confidence)} confidence · {esc(f.rule)}</p><h2>{esc(f.title)}</h2><p>{esc(f.summary)}</p><p><strong>Suggested change:</strong> {esc(f.recommendation)}</p><p class="meta">{esc(f.layer)} · {f.location[0]:.3f}, {f.location[1]:.3f} mm</p><details><summary>Evidence and assumptions</summary><ul>{evidence}</ul><ul>{missing}</ul>{links}</details></article>')
    coverage = "".join(f"<li>{esc(c.get('rule', c.get('check', 'Check')))}: {esc(c.get('status', ''))} — {esc(c.get('detail', c.get('reason', '')))}</li>" for c in result.coverage)
    assumptions = "".join(f"<li>{esc(s)}</li>" for s in [*result.board.warnings, *result.assumptions])
    measured = "".join(f'<tr><td>{esc(m.get("label", ""))}</td><td>{esc(m.get("frequency_mhz", ""))}</td><td>{esc(m.get("amplitude", ""))} {esc(m.get("unit", ""))}</td><td>{esc(m.get("notes", ""))}</td></tr>' for m in (measurements or []))
    body = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EMI Assistant — {esc(result.board.name)}</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:940px;margin:40px auto;padding:0 24px;color:#22313b;background:#f4f7f8}}h1{{font-size:30px;letter-spacing:-.6px}}h2{{font-size:20px}}article,section{{background:white;border:1px solid #dbe3e8;border-radius:12px;padding:22px;margin:18px 0}}figure{{margin:18px 0}}.curve-plot{{width:100%;height:auto;font:12px system-ui,sans-serif}}.curve-key{{display:inline-flex;align-items:center;margin:4px 18px 4px 0;font-size:13px}}.curve-key>span{{display:inline-block;width:26px;margin-right:7px}}code{{overflow-wrap:anywhere}}caption{{text-align:left;font-weight:600}}h3{{font-size:17px}}.meta{{color:#566a78;font-size:14px}}a{{color:#006b70}}summary{{cursor:pointer}}table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:8px;border-bottom:1px solid #ddd}}@media print{{body{{background:white;margin:0}}article{{break-inside:avoid}}details>summary{{display:none}}details>ul{{display:block}}}}</style>
<p class="meta">EMI ASSISTANT · LOCAL ANALYSIS</p><h1>{esc(result.board.name)}</h1><p>{len(result.findings)} findings · {result.suppressed_count} excluded · {result.elapsed_ms:.0f} ms</p><p>{esc(payload['interpretation'])}</p>
{''.join(cards) or '<article><h2>No findings in the checked areas</h2><p>Review coverage and assumptions below before interpreting this result.</p></article>'}
<section><h2>Simulation results</h2>{_simulation_cards(payload["simulations"])}</section>
<section><h2>Analysis coverage</h2><ul>{coverage}</ul><ul>{assumptions}</ul></section>
<section><h2>Changes since the previous analysis</h2><pre>{esc(json.dumps(changes or {}, indent=2))}</pre></section>
<section><h2>Saved measurements</h2><table><tr><th>Label</th><th>MHz</th><th>Level</th><th>Setup / notes</th></tr>{measured}</table><p>Compare readings only with matching units, detector/settings, operating mode, cabling, and probe placement.</p></section>
<p class="meta">Generated {esc(payload['generated_at'])}. Board fingerprint {esc(result.board.fingerprint)}.</p></html>'''
    destination.write_text(body, encoding="utf-8")
