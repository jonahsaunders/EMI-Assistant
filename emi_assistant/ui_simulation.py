"""Native Qt simulation results, with no plotting or web-view dependency."""
from __future__ import annotations

import html
import math

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QGroupBox, QTextBrowser, QProgressBar,
)


def _label(text, parent=None):
    item = QLabel(str(text), parent)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(True)
    item.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return item


class ResponseChart(QWidget):
    """Display finite, like-unit solver responses, preserving baseline labels."""
    COLORS = ("#137c67", "#c26831", "#497cad", "#9873b0", "#b44865", "#608336")

    def __init__(self, series, parent=None):
        super().__init__(parent)
        self.series = []
        self.units = ("", "")
        for line in series:
            points = []
            for x, y in zip(line.get("x", []), line.get("y", [])):
                try:
                    x, y = float(x), float(y)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(x) and math.isfinite(y):
                    points.append((x, y))
            if points:
                units = (str(line.get("x_unit", "")), str(line.get("y_unit", "")))
                if self.series and units != self.units:
                    continue
                self.units = units
                self.series.append((str(line.get("name", "Response")), points))
        xs = [x for _, points in self.series for x, _ in points]
        self.log_x = bool(xs) and min(xs) > 0 and max(xs) / min(xs) >= 100
        self.setMinimumHeight(300 + 20 * max(len(self.series) - 2, 0))
        self.setAccessibleName("Simulated response chart")
        self.setAccessibleDescription("; ".join(name for name, _ in self.series))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        if not self.series:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No response samples")
            return
        top = 18 + 20 * len(self.series)
        plot = QRectF(78, top, max(40, self.width() - 106), max(70, self.height() - top - 55))
        transform = math.log10 if self.log_x else lambda x: x
        xs = [transform(x) for _, points in self.series for x, _ in points]
        ys = [y for _, points in self.series for _, y in points]
        xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
        if xmin == xmax:
            xmin, xmax = xmin - .5, xmax + .5
        if ymin == ymax:
            pad = max(abs(ymin) * .1, 1)
            ymin, ymax = ymin - pad, ymax + pad
        else:
            pad = (ymax - ymin) * .06
            ymin, ymax = ymin - pad, ymax + pad
        for tick in range(5):
            fraction = tick / 4
            xx, yy = plot.left() + fraction * plot.width(), plot.bottom() - fraction * plot.height()
            painter.setPen(QPen(QColor("#e4eaee"), 1))
            painter.drawLine(QPointF(xx, plot.top()), QPointF(xx, plot.bottom()))
            painter.drawLine(QPointF(plot.left(), yy), QPointF(plot.right(), yy))
            painter.setPen(QColor("#617480"))
            xvalue = xmin + fraction * (xmax - xmin)
            if self.log_x:
                xvalue = 10 ** xvalue
            painter.drawText(QRectF(xx - 35, plot.bottom() + 5, 70, 18), Qt.AlignmentFlag.AlignCenter, f"{xvalue:.3g}")
            painter.drawText(QRectF(0, yy - 10, 70, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{ymin + fraction * (ymax - ymin):.3g}")
        painter.setPen(QColor("#213344"))
        xunit, yunit = self.units
        painter.drawText(QRectF(plot.left(), plot.bottom() + 27, plot.width(), 20), Qt.AlignmentFlag.AlignCenter, f"{xunit or 'x'}{' · logarithmic axis' if self.log_x else ''}")
        painter.drawText(QRectF(0, plot.top() - 25, 74, 20), Qt.AlignmentFlag.AlignCenter, yunit or "y")
        for index, (name, points) in enumerate(self.series):
            color = QColor(self.COLORS[index % len(self.COLORS)])
            painter.setPen(QPen(color, 2))
            painter.drawLine(80, 15 + 20 * index, 100, 15 + 20 * index)
            painter.drawText(108, 20 + 20 * index, name[:100])
            path = QPainterPath()
            for number, (x, y) in enumerate(points):
                point = QPointF(plot.left() + (transform(x)-xmin)/(xmax-xmin)*plot.width(),
                                plot.bottom() - (y-ymin)/(ymax-ymin)*plot.height())
                if number:
                    path.lineTo(point)
                else:
                    path.moveTo(point)
            painter.save()
            painter.setClipRect(plot.adjusted(-1, -1, 1, 1))
            painter.drawPath(path)
            painter.restore()


class SimulationPane(QWidget):
    cancel_requested = Signal()
    context_requested = Signal()

    STATUS = {"complete": "Simulated", "needs_information": "Needs information",
              "unavailable": "Engine unavailable", "error": "Could not finish",
              "cancelled": "Cancelled"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.results = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 12, 0, 0)
        row = QHBoxLayout()
        self.status = _label("Click Analyze EMI to run supported circuit and field simulations.")
        self.status.setObjectName("summary")
        row.addWidget(self.status, 1)
        self.cancel_button = QPushButton("Cancel simulations")
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.cancel_button.hide()
        row.addWidget(self.cancel_button)
        self.context_button = QPushButton("Simulation settings…")
        self.context_button.clicked.connect(self.context_requested)
        row.addWidget(self.context_button)
        outer.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumHeight(5)
        self.progress.setTextVisible(False)
        self.progress.hide()
        outer.addWidget(self.progress)
        outer.addWidget(_label("Results describe the stated model and excitation. Review its assumptions before choosing a change."))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.cards = QVBoxLayout(content)
        self.cards.setContentsMargins(0, 0, 0, 0)
        self.cards.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

    def set_progress(self, message):
        self.status.setText(str(message))

    def set_running(self, running):
        self.progress.setVisible(running)
        self.cancel_button.setVisible(running)
        self.cancel_button.setEnabled(running)

    def set_results(self, results, message=None):
        self.results = list(results or [])
        while self.cards.count() > 1:
            item = self.cards.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for result in self.results:
            self.cards.insertWidget(self.cards.count()-1, self._card(result))
        if message is not None:
            self.status.setText(message)
        elif self.results:
            complete = sum(x.get("status") == "complete" for x in self.results)
            pending = sum(x.get("status") == "needs_information" for x in self.results)
            counts = [f"{complete} simulation{'s' if complete != 1 else ''} completed"]
            if pending:
                counts.append(f"{pending} need{'s' if pending == 1 else ''} more information")
            for state, phrase in [("unavailable", "unavailable"), ("error", "could not finish"), ("cancelled", "cancelled")]:
                count = sum(x.get("status") == state for x in self.results)
                if count:
                    counts.append(f"{count} {phrase}")
            self.status.setText(" · ".join(counts) + ". Layout findings remain available.")
        else:
            self.status.setText("No simulation results yet. Click Analyze EMI to check supported models.")

    def _card(self, result):
        state = self.STATUS.get(result.get("status"), "Not run")
        engine = str(result.get("engine", "Simulation"))
        card = QGroupBox(f"{engine} · {state}{' · reused result' if result.get('cached') else ''}")
        layout = QVBoxLayout(card)
        title = _label(result.get("title", "Simulation"))
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(_label(result.get("summary", "")))
        if result.get("missing"):
            text = "Needed: " + "; ".join(map(str, result["missing"]))
            missing = _label(text)
            missing.setObjectName("error")
            layout.addWidget(missing)
        if result.get("recommendations"):
            layout.addWidget(_label("What to try: " + " ".join(map(str, result["recommendations"]))))
        metrics = []
        for metric in result.get("metrics", []):
            value = metric.get("value")
            if isinstance(value, (int, float)) and math.isfinite(value):
                metrics.append(f"{metric.get('name', 'Value')}: {value:.4g} {metric.get('unit', '')}")
        if metrics:
            layout.addWidget(_label("\n".join(metrics)))
        groups = {}
        for series in result.get("series", []):
            groups.setdefault((series.get("x_unit", ""), series.get("y_unit", "")), []).append(series)
        for series in groups.values():
            layout.addWidget(ResponseChart(series))
        evidence = QTextBrowser()
        evidence.setOpenExternalLinks(False)
        evidence.setOpenLinks(False)
        evidence.setMaximumHeight(220)
        sections = []
        for key, heading in [("assumptions", "Model assumptions"), ("warnings", "Model limits"), ("artifacts", "Saved simulation evidence")]:
            if result.get(key):
                sections.append(f"<h3>{heading}</h3><ul>" + "".join(f"<li>{html.escape(str(x))}</li>" for x in result[key]) + "</ul>")
        evidence.setHtml("".join(sections) or "No additional model evidence was supplied.")
        evidence.hide()
        toggle = QPushButton("Show model assumptions and evidence")
        toggle.setCheckable(True)
        toggle.toggled.connect(evidence.setVisible)
        toggle.toggled.connect(lambda checked: toggle.setText("Hide model assumptions and evidence" if checked else "Show model assumptions and evidence"))
        layout.addWidget(toggle)
        layout.addWidget(evidence)
        return card
