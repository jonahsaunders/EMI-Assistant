"""Measurement-assisted hypotheses, not a claim of identified emission sources."""
from __future__ import annotations
import csv
import math
import re
from pathlib import Path
from .models import BoardSnapshot

def positive_number(value, label: str) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Enter a number for {label}.") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label.capitalize()} must be a finite number greater than zero.")
    return number

def diagnose(board: BoardSnapshot, settings: dict, frequency_mhz: float) -> list[dict]:
    frequency_mhz = positive_number(frequency_mhz, "frequency in MHz")
    sources: list[tuple[str, float, bool]] = []
    for net, metadata in settings.get("fast_nets", {}).items():
        if metadata.get("enabled", True) and metadata.get("frequency_mhz"):
            sources.append((net, positive_number(metadata["frequency_mhz"], "source frequency"), True))
    for fp in board.footprints:
        if fp.properties.get('__schematic_dnp') == 'yes':
            continue
        schematic_frequency = fp.properties.get('__schematic_frequency_mhz')
        if schematic_frequency:
            sources.append((f"{fp.reference} (saved schematic)", positive_number(schematic_frequency, "saved source frequency"), False))
            continue
        if fp.reference.upper().startswith(("Y", "X")) or "oscillator" in str(fp.properties).lower():
            match = re.search(r"(\d+(?:\.\d+)?)\s*(MHz|kHz|Hz)\b", fp.value, re.I)
            if match:
                factor = {"mhz": 1, "khz": .001, "hz": .000001}[match[2].lower()]
                sources.append((f"{fp.reference} ({fp.value})", float(match[1]) * factor, False))
    candidates = []
    for name, fundamental, confirmed in sources:
        if fundamental <= 0:
            continue
        harmonic = max(1, int(frequency_mhz / fundamental + .5))
        expected = harmonic * fundamental
        error = abs(expected - frequency_mhz) / frequency_mhz
        if harmonic <= 200 and error <= .02:
            candidates.append({
                "candidate": name,
                "relationship": f"Near harmonic {harmonic} of {fundamental:g} MHz ({expected:g} MHz).",
                "evidence": ("Frequency supplied in board context. " if confirmed else "Frequency inferred from component value. ") +
                    "A harmonic match is a hypothesis. Edge rate, coupling, cabling, and operating mode still matter.",
                "experiment": f"If the circuit permits, change the {fundamental:g} MHz source slightly or disable its block. Check whether this peak follows; probe the source and connected cable paths.",
                "_order": (error, not confirmed, harmonic),
            })
    candidates.sort(key=lambda c: c["_order"])
    for c in candidates:
        del c["_order"]
    if not candidates:
        candidates.append({
            "candidate": "No matching known clock",
            "relationship": "The available design context does not identify this peak.",
            "evidence": "Switching edges, ringing, an unknown clock, or external interference can contribute. An absent harmonic match does not clear the board.",
            "experiment": "Add known clock or switching frequencies in Board context. Compare operating modes, then probe switching circuits and cable connections at the measured frequency.",
        })
    return candidates[:8]

def import_spectrum(path: str) -> list[dict]:
    """Read CSV with explicit frequency units; reject ambiguous instrument exports."""
    file_path = Path(path)
    if file_path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("This CSV is larger than 20 MB. Export a smaller frequency range.")
    with file_path.open(encoding="utf-8-sig", newline="") as stream:
        sample = stream.read(4096)
        stream.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(stream, dialect=dialect)
        keys = {re.sub(r"[^a-z0-9]", "", key.lower()): key for key in (reader.fieldnames or []) if key}
        freq_key, scale = None, 1.0
        for key, factor in [("frequencymhz", 1), ("frequencykhz", .001), ("frequencyhz", .000001)]:
            if key in keys:
                freq_key, scale = keys[key], factor
                break
        amp_key = next((keys[k] for k in ("amplitude", "amplitudedbuv", "amplitudedbm", "level", "dbuv", "dbm") if k in keys), None)
        if not freq_key or not amp_key:
            raise ValueError("Use CSV headers frequency_mhz (or frequency_hz) and amplitude. Include frequency units in the header.")
        rows = []
        for line_number, row in enumerate(reader, 2):
            if line_number > 200002:
                raise ValueError("This CSV contains over 200,000 rows. Export a smaller range.")
            if not any(str(v or "").strip() for v in row.values()):
                continue
            try:
                freq = positive_number(row.get(freq_key), "frequency") * scale
                amplitude = float(row.get(amp_key, ""))
                if not math.isfinite(amplitude):
                    raise ValueError("Non-finite amplitude")
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Invalid frequency or amplitude on CSV row {line_number}.") from exc
            rows.append({"frequency_mhz": freq, "amplitude": amplitude})
    if not rows:
        raise ValueError("The CSV contains no measurements.")
    # Preserve every sample; the UI may choose the largest level to start inspection.
    return sorted(rows, key=lambda r: r["frequency_mhz"])
