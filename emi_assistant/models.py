"""Shared analysis model. Distances are millimetres; frequencies are MHz."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any

Point = tuple[float, float]

@dataclass
class Track:
    id: str
    start: Point
    end: Point
    width: float
    layer: str
    net: str

@dataclass
class Via:
    id: str
    position: Point
    diameter: float
    drill: float
    layers: list[str]
    net: str

@dataclass
class Pad:
    id: str
    number: str
    position: Point
    size: Point
    layers: list[str]
    net: str
    footprint_id: str = ""

@dataclass
class Footprint:
    id: str
    reference: str
    value: str
    position: Point
    pads: list[Pad] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)

@dataclass
class CopperPolygon:
    id: str
    net: str
    layer: str
    points: list[Point]
    holes: list[list[Point]] = field(default_factory=list)
    is_filled: bool = True

@dataclass
class BoardSnapshot:
    name: str
    path: str
    fingerprint: str
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    footprints: list[Footprint] = field(default_factory=list)
    copper: list[CopperPolygon] = field(default_factory=list)
    layers: list[str] = field(default_factory=lambda: ["F.Cu", "B.Cu"])
    stackup: list[dict[str, Any]] = field(default_factory=list)
    outline: list[tuple[Point, Point]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def pads(self) -> list[Pad]:
        return [pad for fp in self.footprints for pad in fp.pads]

    @property
    def nets(self) -> list[str]:
        return sorted({x.net for x in [*self.tracks, *self.vias, *self.pads, *self.copper] if x.net})

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        points = [p for t in self.tracks for p in (t.start, t.end)]
        points += [p.position for p in self.pads] + [v.position for v in self.vias]
        points += [p for c in self.copper for p in c.points]
        points += [p for line in self.outline for p in line]
        if not points:
            return (0.0, 0.0, 100.0, 70.0)
        return (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))

@dataclass
class Finding:
    id: str
    rule: str
    title: str
    priority: str
    confidence: str
    summary: str
    recommendation: str
    item_ids: list[str]
    location: Point
    layer: str = ""
    evidence: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # Preview is advisory geometry. kind: route|via|region; coordinates in mm.
    preview: dict[str, Any] = field(default_factory=dict)

@dataclass
class AnalysisResult:
    board: BoardSnapshot
    findings: list[Finding]
    coverage: list[dict[str, str]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    suppressed_count: int = 0
    simulations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

def default_settings() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "fast_nets": {},
        "reference_nets": ["GND"],
        "reference_layers": {},
        "external_connectors": [],
        "regulators": [],
        "ignored": {},
        "thresholds": {"return_via_mm": 2.0, "decoupling_path_mm": 5.0, "switch_copper_mm2": 20.0},
        "simulation": {
            "enabled": True,
            "engines": {"ngspice": True, "openems": True},
            "max_seconds": 90, "max_jobs": 3,
            "frequency_start_mhz": .1, "frequency_stop_mhz": 500, "points": 121,
            "capacitor_esr_ohm": .03, "capacitor_esl_nh": .8,
            "source_ohm": 50, "termination_ohm": 50,
            "candidate_cap_nf": 100, "candidate_series_ohm": 22,
            "schematic_path": "", "external_model_paths": [],
        },
    }
