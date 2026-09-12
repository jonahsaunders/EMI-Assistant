"""Small native desktop companion for KiCad's EMI Assistant toolbar action.

The interface imports only PySide6 Essentials. All analysis and IPC operations run
on one background worker; a board edit is never implied by an advisory preview.
"""
from __future__ import annotations

import copy
import html
import math
import os
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QBrush
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGraphicsScene, QGraphicsView, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QSplitter, QStackedWidget, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QTextBrowser,
    QVBoxLayout, QWidget, QTabWidget,
)

from .models import AnalysisResult, BoardSnapshot, Finding
from .licensing import show_licenses
from .ui_simulation import SimulationPane


STYLE = """
QWidget { font-family: 'Inter', 'Segoe UI', sans-serif; font-size: 13px; color: #213344; }
QMainWindow, QDialog { background: #f3f6f8; }
QLabel#brand { color: #102938; font-size: 22px; font-weight: 700; }
QLabel#muted { color: #617480; }
QLabel#hero { font-size: 31px; font-weight: 700; color: #122f40; }
QLabel#heading { font-size: 19px; font-weight: 700; }
QLabel#section { font-size: 14px; font-weight: 700; }
QLabel#summary { background: #e2f2ed; border-radius: 7px; padding: 12px; color: #175d4b; }
QLabel#error { background: #fff0e8; border: 1px solid #ecbca7; border-radius: 6px; padding: 10px; color: #853c22; }
QPushButton { background: white; border: 1px solid #ccd7de; padding: 8px 13px; border-radius: 6px; }
QPushButton:hover { background: #e9f1f5; border-color: #9daeb8; }
QPushButton:pressed { background: #dce8ee; }
QPushButton:disabled { color: #8c9ca5; background: #eef2f4; }
QPushButton#primary { background: #137c67; border: 1px solid #137c67; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #0a6855; }
QPushButton#primary:disabled { background: #aac7bf; border-color: #aac7bf; }
QPushButton:checked { background: #dcefe9; border-color: #137c67; }
QListWidget { background: white; border: 1px solid #dce3e8; border-radius: 8px; outline: none; }
QListWidget::item { border-bottom: 1px solid #e9eef1; border-left: 3px solid transparent; padding: 14px 11px; }
QListWidget::item:selected { background: #e3f2ed; color: #174637; border-left: 3px solid #137c67; }
QListWidget::item:hover { background: #f0f7f4; }
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox { background: white; padding: 7px; border: 1px solid #c8d5dd; border-radius: 5px; }
QTextBrowser { background: white; border: 1px solid #dce3e8; border-radius: 7px; padding: 8px; }
QScrollArea { border: none; background: transparent; }
QGroupBox { background: white; border: 1px solid #dce3e8; border-radius: 7px; margin-top: 13px; padding: 14px; }
QGroupBox::title { subcontrol-origin: margin; left: 13px; padding: 0 5px; font-weight: 600; }
QTableWidget { background: white; border: 1px solid #dce3e8; gridline-color: #e3e9ed; }
QHeaderView::section { background: #e9eff3; border: none; padding: 9px; }
QSplitter::handle { background: transparent; width: 10px; }
QToolTip { background: #183341; color: white; border: none; padding: 6px; }
"""


def esc(value) -> str:
    return html.escape(str(value))


def label(text: str, role: str = "", wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if role:
        widget.setObjectName(role)
    widget.setWordWrap(wrap)
    return widget


def button(text: str, callback: Callable, primary: bool = False) -> QPushButton:
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


class Job(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: Callable, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.succeeded.emit(self.operation())
        except Exception as error:
            self.failed.emit(str(error) or type(error).__name__)


class SimulationJob(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.cancel_event = threading.Event()

    def run(self):
        try:
            self.succeeded.emit(self.controller.run_simulations(
                progress=self.progress.emit, cancel_event=self.cancel_event))
        except Exception as error:
            self.failed.emit(str(error) or type(error).__name__)


class BoardCanvas(QGraphicsView):
    """True board geometry with pan/zoom and separate advisory overlays."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#132735"))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setMinimumSize(300, 340)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.board = None
        self.finding = None
        self.layer = "All copper"
        self.preview = False
        self.first_fit = False

    def set_board(self, board: BoardSnapshot):
        self.board = board
        self.finding = None
        self.preview = False
        self.redraw()
        self.fit_board()
        self.first_fit = True

    def set_layer(self, layer: str):
        self.layer = layer
        self.redraw()

    def set_finding(self, finding: Finding | None, preview=False):
        self.finding = finding
        self.preview = preview
        self.redraw()
        if finding:
            x, y = finding.location
            width = max(18.0, min(self._board_rect().width(), 40.0))
            self.fitInView(QRectF(x-width/2, y-width/2, width, width), Qt.AspectRatioMode.KeepAspectRatio)

    def _board_rect(self):
        if not self.board:
            return QRectF(0, 0, 100, 70)
        x0, y0, x1, y1 = self.board.bounds
        return QRectF(x0-3, y0-3, max(x1-x0, 10)+6, max(y1-y0, 10)+6)

    def fit_board(self):
        self.fitInView(self._board_rect(), Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event):
        factor = 1.2 if event.angleDelta().y() > 0 else 1/1.2
        scale = self.transform().m11() * factor
        if 0.4 < scale < 1600:
            self.scale(factor, factor)
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.first_fit:
            self.fit_board()
            self.first_fit = False

    @staticmethod
    def _path(points, close=True):
        path = QPainterPath()
        if points:
            path.moveTo(*points[0])
            for point in points[1:]:
                path.lineTo(*point)
            if close:
                path.closeSubpath()
        return path

    def redraw(self):
        scene = self.scene()
        scene.clear()
        if not self.board:
            return
        board = self.board
        ids = set(self.finding.item_ids) if self.finding else set()
        front = QColor("#e49570")
        back = QColor("#7cadde")
        for copper in board.copper:
            path = self._path(copper.points)
            path.setFillRule(Qt.FillRule.OddEvenFill)
            for hole in copper.holes:
                path.addPath(self._path(hole))
            color = QColor("#64b5a0" if copper.layer == "F.Cu" else "#6b97b7")
            color.setAlpha(66 if self.layer in ("All copper", copper.layer) else 17)
            item = scene.addPath(path, QPen(Qt.PenStyle.NoPen), QBrush(color))
            item.setZValue(-3)
        for first, last in board.outline:
            scene.addLine(*first, *last, QPen(QColor("#bacbd4"), 0.13))
        for track in board.tracks:
            selected = track.id in ids
            color = QColor("#fff39a") if selected else QColor(front if track.layer == "F.Cu" else back)
            if not selected and self.layer not in ("All copper", track.layer):
                color.setAlpha(37)
            pen = QPen(color, max(track.width, 0.10))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            item = scene.addLine(*track.start, *track.end, pen)
            item.setZValue(3 if selected else 0)
            item.setToolTip(f"{track.net} · {track.layer}")
        for fp in board.footprints:
            for pad in fp.pads:
                selected = pad.id in ids or fp.id in ids
                color = QColor("#fff39a" if selected else "#c5b77f")
                if not selected and self.layer != "All copper" and self.layer not in pad.layers and "*.Cu" not in pad.layers:
                    color.setAlpha(40)
                x, y = pad.position
                w, h = pad.size
                item = scene.addRect(x-w/2, y-h/2, max(w, .12), max(h, .12), QPen(Qt.PenStyle.NoPen), QBrush(color))
                item.setZValue(3 if selected else 1)
                item.setToolTip(f"{fp.reference}.{pad.number} · {pad.net}")
            caption = scene.addSimpleText(fp.reference)
            caption.setBrush(QColor("#c3d3dc"))
            caption.setScale(.13)
            caption.setPos(fp.position[0]+.6, fp.position[1]+.6)
            caption.setZValue(2)
        for via in board.vias:
            x, y = via.position
            color = QColor("#fff39a" if via.id in ids else "#86b3bf")
            diameter = max(via.diameter, .3)
            item = scene.addEllipse(x-diameter/2, y-diameter/2, diameter, diameter, QPen(color, .10), QBrush(QColor("#132735")))
            item.setZValue(4)
            item.setToolTip(f"Via · {via.net}")
        if self.finding:
            x, y = self.finding.location
            pen = QPen(QColor("#fff39a"), .15, Qt.PenStyle.DashLine)
            scene.addEllipse(x-1.6, y-1.6, 3.2, 3.2, pen).setZValue(5)
            if self.preview and self.finding.preview:
                data = self.finding.preview
                pen = QPen(QColor("#54f3c2"), .22, Qt.PenStyle.DashLine)
                if data.get("kind") == "via" and data.get("position"):
                    px, py = data["position"]
                    dia = max(float(data.get("diameter", .6)), .6)
                    scene.addEllipse(px-dia/2, py-dia/2, dia, dia, pen).setZValue(8)
                    scene.addEllipse(px-1.1, py-1.1, 2.2, 2.2, pen).setZValue(8)
                elif data.get("points"):
                    scene.addPath(self._path(data["points"], data.get("kind") == "region"), pen).setZValue(8)
        scene.setSceneRect(self._board_rect().adjusted(-20, -20, 20, 20))


class NetNameDelegate(QStyledItemDelegate):
    def __init__(self, nets, parent=None):
        super().__init__(parent)
        self.nets = nets

    def createEditor(self, parent, option, index):
        editor = QComboBox(parent)
        editor.setEditable(True)
        editor.addItems(self.nets)
        return editor

    def setEditorData(self, editor, index):
        editor.setEditText(index.data(Qt.ItemDataRole.EditRole) or "")

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText().strip(), Qt.ItemDataRole.EditRole)


class ContextDialog(QDialog):
    """Optional context improves confidence without blocking the first analysis."""
    def __init__(self, settings: dict, board: BoardSnapshot, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Board context")
        self.resize(830, 750)
        self.settings = copy.deepcopy(settings)
        self.known_nets = set(board.nets)
        self.known_references = {fp.reference.casefold(): fp.reference for fp in board.footprints}
        outer = QVBoxLayout(self)
        outer.addWidget(label("Make recommendations more specific", "heading"))
        outer.addWidget(label("Everything is optional. Leave values blank when unknown.", "muted"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        body = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        ground_group = QGroupBox("Reference copper")
        ground_form = QFormLayout(ground_group)
        self.grounds = QLineEdit(", ".join(settings.get("reference_nets", ["GND"])))
        self.grounds.setPlaceholderText("GND, DGND")
        ground_form.addRow("Ground net names", self.grounds)
        self.refs = QTableWidget(len(board.layers), 2)
        self.refs.setHorizontalHeaderLabels(["Signal layer", "Reference layers (comma separated)"])
        self.refs.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.refs.setMinimumHeight(140)
        for row, layer_name in enumerate(board.layers):
            name_item = QTableWidgetItem(layer_name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.refs.setItem(row, 0, name_item)
            self.refs.setItem(row, 1, QTableWidgetItem(", ".join(settings.get("reference_layers", {}).get(layer_name, []))))
        ground_form.addRow(self.refs)
        body.addWidget(ground_group)
        connector_group = QGroupBox("External cables")
        connector_form = QFormLayout(connector_group)
        self.connectors = QLineEdit(", ".join(settings.get("external_connectors", [])))
        self.connectors.setPlaceholderText("e.g. J1, J3 — leave blank if unknown")
        connector_form.addRow(label("Which connectors have an external cable?", "muted", True))
        connector_form.addRow("Connector references", self.connectors)
        body.addWidget(connector_group)
        fast_group = QGroupBox("Fast signals")
        fast_layout = QVBoxLayout(fast_group)
        fast_layout.addWidget(label("Add known clocks and fast signals. Uncheck Analyze to exclude a net from automatic signal detection.", "muted", True))
        self.fast = QTableWidget(0, 4)
        self.fast.setHorizontalHeaderLabels(["Net", "Frequency (MHz)", "Rise time (ns)", "Analyze"])
        self.fast.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.fast.setMinimumHeight(150)
        self.fast.setItemDelegateForColumn(0, NetNameDelegate(sorted(self.known_nets), self.fast))
        self.fast.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        for net, data in settings.get("fast_nets", {}).items():
            self._add_fast_signal([net, data.get("frequency_mhz", ""), data.get("rise_ns", "")], data.get("enabled", True))
        fast_layout.addWidget(self.fast)
        fast_controls = QHBoxLayout()
        fast_controls.addWidget(button("Add signal", lambda: self._add_fast_signal(["", "", ""])))
        fast_controls.addWidget(button("Remove selected", lambda: self._remove_row(self.fast)))
        fast_controls.addStretch()
        fast_layout.addLayout(fast_controls)
        body.addWidget(fast_group)
        regulators = QGroupBox("Switching regulators")
        regulator_layout = QVBoxLayout(regulators)
        regulator_layout.addWidget(label("Enter the IC reference and buck or boost topology. Add any capacitor references or switch net you know; blank ground uses the first reference net.", "muted", True))
        self.regs = QTableWidget(0, 6)
        self.regs.setHorizontalHeaderLabels(["IC ref", "Topology", "Input cap", "Output cap", "Switch net", "Ground net"])
        self.regs.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.regs.setMinimumHeight(120)
        keys = ["reference", "topology", "input_cap", "output_cap", "switch_net", "ground_net"]
        for data in settings.get("regulators", []):
            self._add_row(self.regs, [data.get(key, "") for key in keys])
        regulator_layout.addWidget(self.regs)
        row = QHBoxLayout()
        row.addWidget(button("Add regulator", lambda: self._add_row(self.regs, ["", "buck", "", "", "", ""])))
        row.addWidget(button("Remove selected", lambda: self._remove_row(self.regs)))
        row.addStretch()
        regulator_layout.addLayout(row)
        body.addWidget(regulators)
        simulations = QGroupBox("Circuit and field simulations")
        simulation_form = QFormLayout(simulations)
        sim_settings = settings.get("simulation", {})
        self.sim_enabled = QCheckBox("Run supported simulations when I click Analyze EMI")
        self.sim_enabled.setChecked(sim_settings.get("enabled", True))
        simulation_form.addRow(self.sim_enabled)
        self.schematic_path = QLineEdit(sim_settings.get("schematic_path", ""))
        self.schematic_path.setPlaceholderText("Find the matching saved schematic automatically")
        schematic_row = QHBoxLayout()
        schematic_row.addWidget(self.schematic_path, 1)
        schematic_row.addWidget(button("Choose…", self._choose_schematic))
        simulation_form.addRow("Root schematic", schematic_row)
        simulation_form.addRow(label("Custom symbols use their saved pins and assigned models. Missing models are reported in the simulation results.", "muted", True))
        self.sim_advanced_button = button("Show advanced simulation settings", lambda: self.sim_advanced.setVisible(self.sim_advanced_button.isChecked()))
        self.sim_advanced_button.setCheckable(True)
        simulation_form.addRow(self.sim_advanced_button)
        self.sim_advanced = QWidget()
        advanced = QFormLayout(self.sim_advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        self.sim_fields = {}
        specs = [
            ("frequency_start_mhz", "Start frequency", .1, .000001, 100000, " MHz"),
            ("frequency_stop_mhz", "Stop frequency", 500, .000001, 100000, " MHz"),
            ("max_seconds", "Time limit per simulation", 90, 5, 3600, " s"),
            ("capacitor_esr_ohm", "Assumed capacitor ESR", .03, 0, 1000, " Ω"),
            ("capacitor_esl_nh", "Assumed capacitor ESL", .8, 0, 10000, " nH"),
            ("source_ohm", "Assumed source impedance", 50, .000001, 1000000, " Ω"),
            ("termination_ohm", "Assumed termination", 50, .000001, 1000000, " Ω"),
        ]
        for key, title, default, low, high, suffix in specs:
            spin = QDoubleSpinBox()
            spin.setDecimals(6)
            spin.setRange(low, high)
            spin.setValue(sim_settings.get(key, default))
            spin.setSuffix(suffix)
            self.sim_fields[key] = spin
            advanced.addRow(title, spin)
        self.sim_ngspice = QCheckBox("ngspice circuit simulation")
        self.sim_openems = QCheckBox("openEMS field simulation")
        engines = sim_settings.get("engines", {})
        self.sim_ngspice.setChecked(engines.get("ngspice", True))
        self.sim_openems.setChecked(engines.get("openems", True))
        advanced.addRow(self.sim_ngspice)
        advanced.addRow(self.sim_openems)
        advanced.addRow(label("These are model assumptions, not component datasheet values. Each result lists the assumptions it used.", "muted", True))
        self.sim_advanced.hide()
        simulation_form.addRow(self.sim_advanced)
        body.addWidget(simulations)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save and recheck")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _choose_schematic(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose the root schematic", self.schematic_path.text(), "KiCad schematic (*.kicad_sch)")
        if path:
            self.schematic_path.setText(path)

    def _add_fast_signal(self, values, enabled=True):
        self._add_row(self.fast, values)
        item = QTableWidgetItem()
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        self.fast.setItem(self.fast.rowCount()-1, 3, item)

    @staticmethod
    def _add_row(table, values):
        row = table.rowCount()
        table.insertRow(row)
        for column, value in enumerate(values):
            table.setItem(row, column, QTableWidgetItem(str(value)))
        table.setCurrentCell(row, 0)

    @staticmethod
    def _remove_row(table):
        if table.currentRow() >= 0:
            table.removeRow(table.currentRow())

    @staticmethod
    def _value(table, row, column):
        item = table.item(row, column)
        return item.text().strip() if item else ""

    def _validate(self):
        try:
            settings = copy.deepcopy(self.settings)
            settings["reference_nets"] = [x.strip() for x in self.grounds.text().split(",") if x.strip()]
            if not settings["reference_nets"]:
                raise ValueError("Enter at least one ground net name.")
            external = [value.strip() for value in self.connectors.text().split(",") if value.strip()]
            unknown = [value for value in external if value.casefold() not in self.known_references]
            if unknown:
                raise ValueError(f"Connector reference {unknown[0]!r} is not on this board. Use a footprint reference such as J1.")
            settings["external_connectors"] = list(dict.fromkeys(self.known_references[value.casefold()] for value in external))
            settings["reference_layers"] = {}
            layer_names = [self._value(self.refs, row, 0) for row in range(self.refs.rowCount())]
            for row in range(self.refs.rowCount()):
                layers = [x.strip() for x in self._value(self.refs, row, 1).split(",") if x.strip()]
                if any(x not in layer_names for x in layers):
                    raise ValueError("Reference layers must use the layer names shown in the table.")
                if layers:
                    settings["reference_layers"][layer_names[row]] = layers
            settings["fast_nets"] = {}
            for row in range(self.fast.rowCount()):
                name = self._value(self.fast, row, 0)
                if not name:
                    if any(self._value(self.fast, row, col) for col in (1, 2)):
                        raise ValueError("Enter a net name for each fast signal.")
                    continue
                enabled_item = self.fast.item(row, 3)
                data = {"enabled": enabled_item is None or enabled_item.checkState() == Qt.CheckState.Checked}
                old_disabled = self.settings.get("fast_nets", {}).get(name, {}).get("enabled") is False
                if name not in self.known_nets and not (old_disabled and not data["enabled"]):
                    raise ValueError(f"Signal net {name!r} is not on this board. Choose an existing net from the Net cell's list.")
                for column, key in [(1, "frequency_mhz"), (2, "rise_ns")]:
                    raw = self._value(self.fast, row, column)
                    if raw:
                        value = float(raw)
                        if not math.isfinite(value) or value <= 0:
                            raise ValueError("Frequency and rise time must be positive numbers.")
                        data[key] = value
                settings["fast_nets"][name] = data
            settings["regulators"] = []
            keys = ["reference", "topology", "input_cap", "output_cap", "switch_net", "ground_net"]
            for row in range(self.regs.rowCount()):
                values = [self._value(self.regs, row, col) for col in range(6)]
                if not values[0] and not any(values[2:]):
                    continue
                if not all(values[:2]):
                    raise ValueError("Each regulator needs its IC reference and topology; other fields may be left blank.")
                values[1] = values[1].lower()
                if values[1] not in ("buck", "boost"):
                    raise ValueError("Choose buck or boost for regulator topology.")
                regulator = {key: value for key, value in zip(keys, values) if value}
                regulator.setdefault("ground_net", settings["reference_nets"][0])
                settings["regulators"].append(regulator)
            simulation = settings.setdefault("simulation", {})
            simulation.update({key: widget.value() for key, widget in self.sim_fields.items()})
            if simulation["frequency_start_mhz"] >= simulation["frequency_stop_mhz"]:
                raise ValueError("Simulation stop frequency must be higher than the start frequency.")
            simulation["enabled"] = self.sim_enabled.isChecked()
            simulation["schematic_path"] = self.schematic_path.text().strip()
            simulation["engines"] = {"ngspice": self.sim_ngspice.isChecked(), "openems": self.sim_openems.isChecked()}
            self.settings = settings
        except ValueError as error:
            QMessageBox.warning(self, "Check board context", str(error))
            return
        self.accept()


class DiagnosisDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Diagnose a measured peak")
        self.resize(760, 680)
        layout = QVBoxLayout(self)
        layout.addWidget(label("What should I test next?", "heading"))
        layout.addWidget(label("Enter a measured peak or import a spectrum CSV to explore possible causes.", "muted", True))
        row = QHBoxLayout()
        self.frequency = QDoubleSpinBox()
        self.frequency.setRange(.000001, 1000000)
        self.frequency.setDecimals(6)
        self.frequency.setValue(100)
        self.frequency.setSuffix(" MHz")
        row.addWidget(self.frequency)
        self.diagnose_button = button("Find possible causes", self.diagnose, True)
        row.addWidget(self.diagnose_button)
        self.import_button = button("Import spectrum CSV…", self.import_spectrum)
        row.addWidget(self.import_button)
        layout.addLayout(row)
        self.peaks = QComboBox()
        self.peaks.hide()
        self.peaks.currentIndexChanged.connect(self._select_peak)
        layout.addWidget(self.peaks)
        self.results = QTextBrowser()
        self.results.setHtml("<p>Start with a peak you want to investigate. Results are hypotheses with practical confirming experiments.</p>")
        layout.addWidget(self.results, 1)
        measurement = QGroupBox("Save this measurement")
        form = QFormLayout(measurement)
        self.measure_label = QLineEdit()
        self.measure_label.setPlaceholderText("e.g. Baseline, 12 V input, full load")
        self.amplitude = QDoubleSpinBox()
        self.amplitude.setRange(-10000, 10000)
        self.amplitude.setDecimals(3)
        self.unit = QComboBox()
        self.unit.addItems(["dBµV", "dBµV/m", "dBm", "dB", "V", "A"])
        amp_row = QHBoxLayout()
        amp_row.addWidget(self.amplitude)
        amp_row.addWidget(self.unit)
        self.notes = QLineEdit()
        self.notes.setPlaceholderText("Probe position, cables, operating mode, instrument settings")
        form.addRow("Label", self.measure_label)
        form.addRow("Amplitude", amp_row)
        form.addRow("Test setup", self.notes)
        self.save_button = button("Save measurement", self.save_measurement)
        form.addRow(self.save_button)
        layout.addWidget(measurement)
        self.status = label("", "muted", True)
        layout.addWidget(self.status)
        layout.addWidget(button("Close", self.reject))

    def set_busy(self, busy):
        for control in (self.diagnose_button, self.import_button, self.save_button):
            control.setEnabled(not busy)

    def diagnose(self):
        frequency = self.frequency.value()
        self.status.setText("Looking for possible causes…")
        self.window.run_job(lambda: self.window.controller.diagnose(frequency), self._show_causes, "Diagnosing measured peak…", self)

    def _show_causes(self, candidates):
        if not candidates:
            self.results.setHtml("<h3>No matching source identified</h3><p>Add known clock frequencies in Board context, then try again. Check harmonics, switching sources, and cable common-mode current with measurements.</p>")
        else:
            sections = []
            for index, candidate in enumerate(candidates, 1):
                evidence = candidate.get("evidence", "")
                if isinstance(evidence, list):
                    evidence = "; ".join(map(str, evidence))
                sections.append(f"<h3>{index}. {esc(candidate.get('candidate', 'Possible source'))}</h3><p>{esc(candidate.get('relationship', ''))}</p><p><b>Evidence:</b> {esc(evidence)}</p><p style='color:#176e58'><b>Try this:</b> {esc(candidate.get('experiment', ''))}</p>")
            self.results.setHtml("".join(sections))
        self.status.setText("Possible causes require a confirming measurement.")

    def import_spectrum(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import spectrum", "", "Spectrum CSV (*.csv);;All files (*)")
        if path:
            self.window.run_job(lambda: self.window.controller.import_spectrum(path), self._show_peaks, "Reading spectrum…", self)

    def _show_peaks(self, peaks):
        self.peaks.blockSignals(True)
        self.peaks.clear()
        # Prominent peaks first. Importing never silently changes measured units.
        for peak in sorted(peaks, key=lambda p: p.get("amplitude", 0), reverse=True)[:50]:
            self.peaks.addItem(f"{peak['frequency_mhz']:g} MHz · amplitude {peak['amplitude']:g}", peak)
        self.peaks.blockSignals(False)
        self.peaks.setVisible(bool(peaks))
        if peaks:
            self._select_peak(0)
            self.status.setText(f"Imported {len(peaks)} samples. Select a peak, check its amplitude unit, then find possible causes.")
        else:
            self.status.setText("No spectrum samples found. Expected frequency and amplitude columns.")

    def _select_peak(self, index):
        peak = self.peaks.itemData(index)
        if peak:
            self.frequency.setValue(peak["frequency_mhz"])
            self.amplitude.setValue(peak["amplitude"])

    def save_measurement(self):
        title = self.measure_label.text().strip()
        if not title:
            self.status.setText("Add a short measurement label first.")
            self.measure_label.setFocus()
            return
        values = (title, self.frequency.value(), self.amplitude.value(), self.unit.currentText(), self.notes.text().strip())
        self.window.run_job(lambda: self.window.controller.save_measurement(*values), lambda result: self.status.setText(str(result)), "Saving measurement…", self)

    def reject(self):
        if self.window.busy:
            self.status.setText("Please wait for the current operation to finish.")
            return
        super().reject()

    def closeEvent(self, event):
        if self.window.busy:
            event.ignore()
        else:
            event.accept()


class MainWindow(QMainWindow):
    def __init__(self, controller, auto_connect=False):
        super().__init__()
        self.controller = controller
        self.result = None
        self.finding = None
        self.busy = False
        self.worker = None
        self._job_dialog = None
        self._quiet_job = False
        self._pending_job = None
        self._poll_error = ""
        self.show_all = False
        self.details_expanded = False
        self._prior_ids = None
        self._file_mtime = None
        self.simulation_worker = None
        self._simulation_result = None
        self._simulation_generation = 0
        self._simulation_pending = None
        self._close_after_simulation = False
        self.setWindowTitle("EMI Assistant — KiCad")
        self.resize(1420, 900)
        self.setMinimumSize(1020, 680)
        self.setStyleSheet(STYLE)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(14)
        header = QHBoxLayout()
        identity = QVBoxLayout()
        identity.addWidget(label("EMI Assistant", "brand"))
        identity.addWidget(label("Find the next useful improvement to your PCB.", "muted"))
        header.addLayout(identity)
        header.addStretch()
        self.open_button = button("Open PCB…", self.open_board)
        self.analyze_button = button("Analyze EMI", self.analyze, True)
        header.addWidget(self.open_button)
        header.addWidget(self.analyze_button)
        layout.addLayout(header)
        self.error = label("", "error", True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error.hide()
        layout.addWidget(self.error)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self.pages.addWidget(self._welcome())
        self.pages.addWidget(self._results_page())
        footer = QHBoxLayout()
        self.status = label("Ready", "muted", True)
        self.mode_label = label("Local analysis · board data stays on your computer", "muted")
        footer.addWidget(self.status, 1)
        footer.addWidget(self.mode_label)
        self.licenses_button = button("Licenses", lambda: show_licenses(self))
        self.licenses_button.setToolTip("Read application and third-party licenses")
        footer.addWidget(self.licenses_button)
        layout.addLayout(footer)
        self._controls = [self.open_button, self.analyze_button, self.context_button,
                          self.diagnose_button, self.export_button, self.restore_button,
                          self.demo_button, self.welcome_analyze, self.welcome_open,
                          self.connect_button]
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._check_file_changed)
        self.timer.start(3000)
        if auto_connect:
            QTimer.singleShot(0, self.connect_kicad)
        elif getattr(controller, "result", None):
            self._show_analysis(controller.result)
            QTimer.singleShot(0, self._start_pending_simulation)

    def _welcome(self):
        welcome = QWidget()
        outer = QHBoxLayout(welcome)
        outer.addStretch(1)
        column = QVBoxLayout()
        column.addStretch(1)
        column.addWidget(label("A clearer path to better EMI.", "hero"))
        column.addSpacing(12)
        column.addWidget(label("Analyze your board, see where to look,\nand understand what to change first.", "muted", True))
        column.addSpacing(25)
        self.welcome_analyze = button("Analyze the board open in KiCad", self.connect_kicad, True)
        self.welcome_analyze.setMinimumHeight(52)
        column.addWidget(self.welcome_analyze)
        self.welcome_open = button("Open a .kicad_pcb file…", self.open_board)
        self.welcome_open.setMinimumHeight(46)
        column.addWidget(self.welcome_open)
        self.demo_button = button("Try the example board", self.load_demo)
        self.demo_button.setMinimumHeight(46)
        column.addWidget(self.demo_button)
        column.addSpacing(20)
        column.addWidget(label("1  Analyze       2  Inspect a finding       3  Preview & recheck", "muted"))
        column.addSpacing(10)
        column.addWidget(label("Start immediately. Add clock or regulator details later for more specific results.", "muted", True))
        column.addStretch(1)
        outer.addLayout(column, 3)
        outer.addStretch(1)
        return welcome

    def _results_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        summary_row = QHBoxLayout()
        board_info = QVBoxLayout()
        self.board_title = label("", "heading")
        self.summary = label("", "muted", True)
        board_info.addWidget(self.board_title)
        board_info.addWidget(self.summary)
        summary_row.addLayout(board_info, 1)
        self.context_button = button("Board context…", self.edit_context)
        self.diagnose_button = button("Diagnose a peak…", self.diagnose)
        self.export_button = button("Export report…", self.export_report)
        summary_row.addWidget(self.context_button)
        summary_row.addWidget(self.diagnose_button)
        summary_row.addWidget(self.export_button)
        layout.addLayout(summary_row)
        self.revision = label("", "summary", True)
        self.revision.hide()
        layout.addWidget(self.revision)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(12)
        splitter.setChildrenCollapsible(False)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(label("Start here", "section"))
        self.findings_list = QListWidget()
        self.findings_list.setWordWrap(True)
        self.findings_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.findings_list.setSpacing(0)
        self.findings_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.findings_list.currentRowChanged.connect(self._finding_changed)
        self.findings_list.setAccessibleName("Prioritized EMI findings")
        left_layout.addWidget(self.findings_list, 1)
        self.more_button = button("Show all findings", self.toggle_all)
        left_layout.addWidget(self.more_button)
        self.restore_button = button("Restore ignored findings", self.restore_ignored)
        self.restore_button.hide()
        left_layout.addWidget(self.restore_button)
        self.coverage_button = button("What was checked?", self.show_coverage)
        left_layout.addWidget(self.coverage_button)
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        canvas_bar = QHBoxLayout()
        self.layers = QComboBox()
        self.layers.setAccessibleName("Visible copper layer")
        self.layers.currentTextChanged.connect(lambda text: self.canvas.set_layer(text))
        canvas_bar.addWidget(self.layers, 1)
        canvas_bar.addWidget(button("Fit board", lambda: self.canvas.fit_board()))
        center_layout.addLayout(canvas_bar)
        self.canvas = BoardCanvas()
        center_layout.addWidget(self.canvas, 1)
        self.canvas_caption = label("Drag to pan · scroll to zoom · yellow highlights the finding", "muted", True)
        center_layout.addWidget(self.canvas_caption)
        right = QWidget()
        detail_layout = QVBoxLayout(right)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(label("Your next action", "section"))
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        self.detail.setAccessibleName("Finding explanation and recommended action")
        detail_layout.addWidget(self.detail, 1)
        self.details_button = button("Show evidence and assumptions", self.toggle_details)
        self.details_button.setCheckable(True)
        detail_layout.addWidget(self.details_button)
        self.locate_button = button("Show in KiCad", self.locate)
        self.preview_button = button("Preview suggestion", self.toggle_preview)
        self.preview_button.setCheckable(True)
        self.apply_button = button("Apply fix in KiCad", self.apply_fix, True)
        self.apply_button.hide()
        self.ignore_button = button("Ignore with a reason…", self.ignore)
        detail_layout.addWidget(self.locate_button)
        detail_layout.addWidget(self.preview_button)
        detail_layout.addWidget(self.apply_button)
        detail_layout.addWidget(self.ignore_button)
        splitter.addWidget(left)
        splitter.addWidget(center)
        splitter.addWidget(right)
        splitter.setSizes([290, 650, 340])
        splitter.setStretchFactor(1, 1)
        self.result_tabs = QTabWidget()
        self.result_tabs.addTab(splitter, "Layout findings")
        self.simulations = SimulationPane()
        self.simulations.cancel_requested.connect(self.cancel_simulations)
        self.simulations.context_requested.connect(self.edit_context)
        self.result_tabs.addTab(self.simulations, "Simulations")
        layout.addWidget(self.result_tabs, 1)
        self.simulation_summary = label("Simulations run automatically with Analyze EMI.", "muted", True)
        layout.addWidget(self.simulation_summary)
        self.connect_button = button("Connect to open KiCad board", self.connect_kicad)
        self.connect_button.hide()
        layout.addWidget(self.connect_button)
        return page

    def run_job(self, operation, callback, description, dialog=None, quiet=False):
        if self.busy:
            if self._quiet_job and not quiet:
                self._pending_job = (operation, callback, description, dialog)
                self.status.setText(description)
            return
        self.busy = True
        self._job_dialog = dialog
        self._quiet_job = quiet
        if not quiet:
            self.error.hide()
            self.status.setText(description)
            for widget in self._controls:
                widget.setEnabled(False)
            self.simulations.context_button.setEnabled(False)
            self._update_action_state()
        else:
            # Never start a board mutation while its snapshot is being refreshed.
            self.apply_button.setEnabled(False)
        if dialog:
            dialog.set_busy(True)
        worker = Job(operation, self)
        self.worker = worker
        worker.succeeded.connect(callback)
        worker.failed.connect(self._quiet_error if quiet else self._show_error)
        worker.finished.connect(self._job_finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _job_finished(self):
        self.busy = False
        self.worker = None
        for widget in self._controls:
            widget.setEnabled(True)
        self.simulations.context_button.setEnabled(True)
        if self._job_dialog:
            self._job_dialog.set_busy(False)
        self._job_dialog = None
        self._quiet_job = False
        self._update_action_state()
        pending, self._pending_job = self._pending_job, None
        if pending is not None:
            self.run_job(*pending)
        else:
            self._start_pending_simulation()

    def _show_analysis(self, result):
        """Display the quick answer before starting the longer solver work."""
        self.show_result(result)
        self._simulation_pending = result
        if callable(getattr(self.controller, "run_simulations", None)):
            self.simulation_summary.setText("Layout ready · simulations will run automatically in the background.")

    def _invalidate_simulations(self, message):
        self._simulation_generation += 1
        self._simulation_pending = None
        if self.simulation_worker is not None:
            self.simulation_worker.cancel_event.set()
        self.simulations.set_results([], message)
        self.simulation_summary.setText(message)
        self.result_tabs.setTabText(1, "Simulations")

    def _start_pending_simulation(self):
        if self.busy or self.simulation_worker is not None or self._close_after_simulation:
            return
        expected = self._simulation_pending
        if expected is None or expected is not self.result:
            return
        self._simulation_pending = None
        if not callable(getattr(self.controller, "run_simulations", None)):
            return
        if not self.controller.settings.get("simulation", {}).get("enabled", True):
            message = "Simulations are off. Enable them in Board context to include them in Analyze EMI."
            self.simulations.set_results([], message)
            self.simulation_summary.setText(message)
            return
        self._simulation_generation += 1
        generation = self._simulation_generation
        worker = SimulationJob(self.controller, self)
        self.simulation_worker = worker
        self._simulation_result = expected
        self.simulations.set_results([], "Preparing ngspice and openEMS…")
        self.simulations.set_running(True)
        self.result_tabs.setTabText(1, "Simulations · running")
        self.simulation_summary.setText("Layout ready · simulations running. Open the Simulations tab to see progress or cancel.")
        worker.progress.connect(lambda message: self._simulation_progress(message, generation, expected))
        worker.succeeded.connect(lambda results: self._simulation_completed(results, generation, expected))
        worker.failed.connect(lambda error: self._simulation_failed(error, generation, expected))
        worker.finished.connect(lambda: self._simulation_finished(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _simulation_progress(self, message, generation, expected):
        if generation == self._simulation_generation and expected is self.result:
            self.simulations.set_progress(message)
            self.simulation_summary.setText(f"Simulation: {message}")

    def _simulation_completed(self, results, generation, expected):
        if generation != self._simulation_generation or expected is not self.result:
            return
        results = list(results or [])
        self.simulations.set_results(results)
        self.result_tabs.setTabText(1, f"Simulations · {len(results)} results")
        self.simulation_summary.setText(self.simulations.status.text())

    def _simulation_failed(self, error, generation, expected):
        if generation != self._simulation_generation or expected is not self.result:
            return
        self.simulations.set_results([], f"Simulation could not finish: {error}")
        self.simulation_summary.setText("Simulation could not finish. Open Simulations for details; layout findings are ready.")
        self.result_tabs.setTabText(1, "Simulations · needs attention")

    def _simulation_finished(self, worker):
        if self.simulation_worker is worker:
            self.simulation_worker = None
            self._simulation_result = None
            self.simulations.set_running(False)
        if self._close_after_simulation:
            QTimer.singleShot(0, self.close)
        else:
            self._start_pending_simulation()

    def cancel_simulations(self):
        self._simulation_pending = None
        if self.simulation_worker is not None:
            self.simulation_worker.cancel_event.set()
            self.simulations.cancel_button.setEnabled(False)
            self.simulations.set_progress("Stopping simulations; your layout findings are ready.")
            self.simulation_summary.setText("Stopping simulations…")

    def _quiet_error(self, message):
        if message != self._poll_error:
            self._poll_error = message
            self.status.setText(f"Auto-recheck is waiting: {message}")

    def _show_error(self, message):
        self.error.setText(message)
        self.error.show()
        self.status.setText("Action could not finish. See the message above.")
        if self._job_dialog:
            self._job_dialog.status.setText(message)

    def analyze(self):
        if self.result is None:
            self.connect_kicad()
        else:
            self.run_job(self.controller.analyze, self._show_analysis, "Analyzing board geometry and return paths…")

    def connect_kicad(self):
        # Controller exposes connect() where available; mode is part of its public contract.
        operation = getattr(self.controller, "connect", None)
        if operation is None:
            def operation():
                prior_mode = self.controller.mode
                self.controller.mode = "connected"
                try:
                    return self.controller.analyze()
                except Exception:
                    self.controller.mode = prior_mode
                    raise
        self.run_job(operation, self._show_analysis, "Connecting to KiCad and analyzing the open board…")

    def open_board(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open PCB", "", "KiCad PCB (*.kicad_pcb)")
        if path:
            self.run_job(lambda: self.controller.open_board(path), self._show_analysis, "Reading and analyzing PCB…")

    def load_demo(self):
        self.run_job(self.controller.load_demo, self._show_analysis, "Analyzing the example board…")

    def show_result(self, result: AnalysisResult):
        old = self.result
        if old is not result:
            self._invalidate_simulations("Board analysis updated. Click Analyze EMI to simulate this version.")
        selected_id = self.finding.id if self.finding else None
        self.result = result
        board = result.board
        self.pages.setCurrentIndex(1)
        self.board_title.setTextFormat(Qt.TextFormat.PlainText)
        self.board_title.setText(board.name or "Untitled PCB")
        count = len(result.findings)
        high = sum(f.priority.lower() in ("high", "critical") for f in result.findings)
        self.summary.setText(f"{count} finding{'s' if count != 1 else ''} · {high} high priority · {len(board.layers)} copper layers")
        mode = getattr(self.controller, "mode", "file")
        self.mode_label.setText({"connected": "Connected to KiCad · auto-recheck every 3 s", "demo": "Example board · advisory previews", "file": "PCB file · auto-recheck on save"}.get(mode, mode))
        self.analyze_button.setText("Analyze EMI")
        self.connect_button.setVisible(mode != "connected")
        if old and (old.board.path == board.path and old.board.name == board.name):
            previous = {f.id for f in old.findings}
            current = {f.id for f in result.findings}
            resolved = len(previous-current)
            added = len(current-previous)
            self.revision.setText(f"Compared with the previous check: {resolved} no longer reported · {added} newly reported · {len(previous & current)} remain. Changes in context or ignored findings also affect these counts.")
            self.revision.show()
        else:
            self.revision.hide()
        self.layers.blockSignals(True)
        self.layers.clear()
        self.layers.addItems(["All copper", *board.layers])
        self.layers.blockSignals(False)
        self.canvas.layer = "All copper"
        self.canvas.set_board(board)
        self.preview_button.setChecked(False)
        self._fill_findings(selected_id)
        self.restore_button.setVisible(result.suppressed_count > 0)
        self.restore_button.setText(f"Restore {result.suppressed_count} ignored finding{'s' if result.suppressed_count != 1 else ''}")
        self.status.setText(f"Checked in {result.elapsed_ms / 1000:.2f} s. Select a finding to see what to change.")
        if board.path and os.path.isfile(board.path):
            self._file_mtime = os.stat(board.path).st_mtime_ns
        else:
            self._file_mtime = None
        self._update_action_state()
        if getattr(result, "simulations", None):
            self.simulations.set_results(result.simulations)
            self.simulation_summary.setText(self.simulations.status.text())

    def _fill_findings(self, selected_id=None):
        self.findings_list.blockSignals(True)
        self.findings_list.clear()
        findings = self.result.findings if self.show_all else self.result.findings[:3]
        selected_row = 0
        for index, finding in enumerate(findings):
            place = f"{finding.layer} · {finding.location[0]:.1f}, {finding.location[1]:.1f} mm" if finding.layer else f"{finding.location[0]:.1f}, {finding.location[1]:.1f} mm"
            title = f"{index+1}. {finding.title}\n\n{finding.priority.upper()} PRIORITY · {finding.confidence} confidence\n{place}"
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, finding)
            item.setToolTip(finding.summary)
            self.findings_list.addItem(item)
            if finding.id == selected_id:
                selected_row = index
        self.findings_list.blockSignals(False)
        count = len(self.result.findings)
        self.more_button.setVisible(count > 3)
        self.more_button.setText("Show top 3" if self.show_all else f"Show all {count} findings")
        if findings:
            self.findings_list.setCurrentRow(selected_row)
        else:
            self.finding = None
            self.detail.setHtml("<h2>No findings from the enabled checks</h2><p>Review <b>What was checked?</b> to see coverage and missing context. A clean result is not an emissions measurement.</p><p>You can add clock and regulator details in <b>Board context</b>, or investigate a measured peak.</p>")
            self.canvas.set_finding(None)
        self._update_action_state()

    def _finding_changed(self, row):
        item = self.findings_list.item(row)
        if not item:
            return
        self.finding = item.data(Qt.ItemDataRole.UserRole)
        f = self.finding
        self.preview_button.setChecked(False)
        self.preview_button.setText(self._preview_label())
        self.canvas_caption.setText("Drag to pan · scroll to zoom · yellow highlights the finding")
        if f.layer and self.layers.findText(f.layer) >= 0:
            self.layers.setCurrentText(f.layer)
        else:
            self.layers.setCurrentText("All copper")
        self.canvas.set_finding(f)
        self.details_expanded = False
        self.details_button.setChecked(False)
        self.details_button.setText("Show evidence and assumptions")
        self._render_detail()
        self._update_action_state()

    def _render_detail(self):
        f = self.finding
        if f is None:
            return
        color = {"critical": "#b84330", "high": "#ad4929", "medium": "#8c691e", "low": "#366c85"}.get(f.priority.lower(), "#366c85")
        content = f"<h2>{esc(f.title)}</h2><p><b style='color:{color}'>{esc(f.priority.title())} priority</b> &nbsp;·&nbsp; {esc(f.confidence)} confidence</p><h3>Why it matters</h3><p>{esc(f.summary)}</p><h3 style='color:#137c67'>What to change</h3><p>{esc(f.recommendation)}</p>"
        if self.details_expanded and f.evidence:
            content += "<h3>Observed evidence</h3><ul>" + "".join(f"<li>{esc(x)}</li>" for x in f.evidence) + "</ul>"
        if self.details_expanded and f.missing:
            content += "<h3>What would improve confidence</h3><ul>" + "".join(f"<li>{esc(x)}</li>" for x in f.missing) + "</ul>"
        if f.preview.get("description"):
            content += f"<p><b>Highlighted geometry:</b> {esc(f.preview['description'])}</p>"
        if f.preview:
            content += "<p style='color:#617480'>The preview is an advisory suggestion. Check electrical suitability and KiCad DRC before making the change.</p>"
        if f.preview.get("apply_reason"):
            content += f"<p><b>Editing in KiCad:</b> {esc(f.preview['apply_reason'])}</p>"
        if f.preview.get("can_apply") is True:
            content += "<p>Applying saves current project settings; undo the via in KiCad.</p>"
        if self.details_expanded and f.sources:
            content += "<h3>Engineering references</h3><ul>"
            for source in f.sources:
                if isinstance(source, str) and source.startswith(("https://", "http://")):
                    content += f'<li><a href="{esc(source)}">{esc(source)}</a></li>'
                else:
                    content += f"<li>{esc(source)}</li>"
            content += "</ul>"
        self.detail.setHtml(content)
        self._update_action_state()

    def toggle_details(self):
        self.details_expanded = self.details_button.isChecked()
        self.details_button.setText("Hide evidence and assumptions" if self.details_expanded else "Show evidence and assumptions")
        self._render_detail()

    def _update_action_state(self):
        active = self.finding is not None and not self.busy
        connected = getattr(self.controller, "mode", "") == "connected"
        self.locate_button.setEnabled(active and connected)
        self.locate_button.setToolTip("Select the related objects in the open KiCad board." if connected else "Connect to the open KiCad board to locate objects.")
        preview = self.finding.preview if self.finding else {}
        self.preview_button.setEnabled(active and bool(preview))
        self.ignore_button.setEnabled(active)
        self.details_button.setEnabled(self.finding is not None)
        # Automatic writes are only offered when the adapter explicitly validates capability.
        can_apply = preview.get("can_apply") is True and connected
        self.apply_button.setVisible(can_apply)
        self.apply_button.setEnabled(active and can_apply and self.preview_button.isChecked())

    def toggle_all(self):
        selected = self.finding.id if self.finding else None
        self.show_all = not self.show_all
        self._fill_findings(selected)

    def _preview_label(self):
        preview = self.finding.preview if self.finding else {}
        if preview.get("kind") == "route":
            return "Highlight path to improve"
        if preview.get("kind") == "region":
            return "Highlight area to improve"
        return "Preview suggestion"

    def toggle_preview(self):
        enabled = self.preview_button.isChecked()
        self.canvas.set_finding(self.finding, enabled)
        self.preview_button.setText("Hide highlight" if enabled else self._preview_label())
        description = (self.finding.preview.get("description") if self.finding else None) or "Advisory geometry; review in KiCad before editing."
        self.canvas_caption.setText(f"Mint dashed highlight: {description}" if enabled else "Drag to pan · scroll to zoom · yellow highlights the finding")
        self._update_action_state()

    def locate(self):
        if self.finding:
            finding = self.finding
            self.run_job(lambda: self.controller.locate(finding), lambda result: self.status.setText(str(result)), "Selecting related objects in KiCad…")

    def apply_fix(self):
        if not self.finding or not self.apply_button.isEnabled():
            return
        finding = self.finding
        def operation():
            message = self.controller.apply_fix(finding)
            try:
                result = self.controller.analyze()
            except Exception as error:
                return message, None, str(error)
            return message, result, None
        def done(data):
            message, result, error = data
            if result is not None:
                self._show_analysis(result)
                self.status.setText(f"{message} Use Undo in KiCad to revert.")
            else:
                self._show_error(f"The edit was applied, but rechecking failed: {error} Use Undo in KiCad to revert the edit, or recheck the board again.")
        self.run_job(operation, done, "Validating the proposed edit and applying in KiCad…")

    def ignore(self):
        if not self.finding:
            return
        reason, accepted = QInputDialog.getText(self, "Ignore finding", "Why is this intentional or not applicable?")
        if accepted and reason.strip():
            finding = self.finding
            self.run_job(lambda: self.controller.ignore(finding, reason.strip()), self.show_result, "Saving the reason and updating findings…")

    def restore_ignored(self):
        self.run_job(self.controller.reset_ignored, self.show_result, "Restoring ignored findings…")

    def edit_context(self):
        if self.result:
            board_identity = (self.result.board.path, self.result.board.name)
            dialog = ContextDialog(self.controller.settings, self.result.board, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                if (self.result.board.path, self.result.board.name) != board_identity:
                    self._show_error("The open board changed while Board context was open. Review context for the current board before saving.")
                    return
                self.run_job(lambda: self.controller.update_settings(dialog.settings), self._show_analysis, "Rechecking with the updated board context…")

    def show_coverage(self):
        if not self.result:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Analysis coverage")
        dialog.resize(730, 570)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("What was checked?", "heading"))
        browser = QTextBrowser()
        content = "<p>Geometric checks identify possible risks. Emissions also depend on excitation, cables, enclosure, and operating conditions.</p>"
        for entry in self.result.coverage:
            title = entry.get("check", entry.get("rule", entry.get("name", "Check")))
            content += f"<h3>{esc(title)}</h3><p>" + "<br>".join(f"<b>{esc(key.replace('_', ' ').title())}:</b> {esc(value)}" for key, value in entry.items() if key not in ("check", "rule", "name")) + "</p>"
        if self.result.assumptions:
            content += "<h3>Assumptions</h3><ul>" + "".join(f"<li>{esc(x)}</li>" for x in self.result.assumptions) + "</ul>"
        if self.result.board.warnings:
            content += "<h3>Board information</h3><ul>" + "".join(f"<li>{esc(x)}</li>" for x in self.result.board.warnings) + "</ul>"
        browser.setHtml(content)
        layout.addWidget(browser)
        layout.addWidget(button("Close", dialog.accept))
        dialog.exec()

    def diagnose(self):
        if self.result:
            DiagnosisDialog(self).exec()

    def export_report(self):
        if not self.result:
            return
        basename = Path(self.result.board.name or "board").stem + "-emi-report.html"
        path, chosen = QFileDialog.getSaveFileName(self, "Export EMI report", basename, "HTML report (*.html);;JSON report (*.json)")
        if path:
            if not Path(path).suffix:
                path += ".json" if chosen.startswith("JSON") else ".html"
            self.run_job(lambda: self.controller.export_report(path), lambda _: self.status.setText(f"Report saved: {path}"), "Exporting analysis report…")

    def _receive_poll(self, result):
        if self._poll_error:
            self._poll_error = ""
            self.status.setText("Connected again. Automatic rechecking is active.")
        if result is None:
            return
        selected_id = self.finding.id if self.finding else None
        same_board = self.result and self.result.board.path == result.board.path and self.result.board.name == result.board.name
        preserved = same_board and selected_id in {f.id for f in result.findings}
        view_transform = self.canvas.transform()
        view_center = self.canvas.mapToScene(self.canvas.viewport().rect().center())
        previous_layer = self.layers.currentText()
        had_preview = self.preview_button.isChecked()
        had_details = self.details_expanded
        if preserved and selected_id not in {f.id for f in result.findings[:3]}:
            self.show_all = True
        self.show_result(result)
        if preserved:
            if had_details:
                self.details_button.setChecked(True)
                self.toggle_details()
            if self.layers.findText(previous_layer) >= 0:
                self.layers.setCurrentText(previous_layer)
            if had_preview and self.finding and self.finding.preview:
                self.preview_button.setChecked(True)
                self.toggle_preview()
            self.canvas.setTransform(view_transform)
            self.canvas.centerOn(view_center)
        self.status.setText("Board changed — findings automatically updated.")

    def _check_file_changed(self):
        if self.busy or not self.result or QApplication.activeModalWidget() is not None:
            return
        if getattr(self.controller, "mode", "") == "connected":
            poll = getattr(self.controller, "poll_and_analyze", None)
            if poll is not None:
                self.run_job(poll, self._receive_poll, "", quiet=True)
            return
        if self._file_mtime is None:
            return
        try:
            changed = os.stat(self.result.board.path).st_mtime_ns != self._file_mtime
        except OSError:
            return
        if changed:
            self.run_job(self.controller.analyze, self._receive_poll, "", quiet=True)

    def closeEvent(self, event):
        if self.busy:
            self.status.setText("Please wait for the current operation to finish before closing.")
            event.ignore()
        elif self.simulation_worker is not None:
            self._close_after_simulation = True
            self.cancel_simulations()
            self.status.setText("Stopping simulations before closing…")
            event.ignore()
        else:
            event.accept()


def run(controller, auto_connect: bool = False) -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("EMI Assistant")
    app.setOrganizationName("EMI Assistant")
    window = MainWindow(controller, auto_connect=auto_connect)
    window.show()
    return app.exec()
