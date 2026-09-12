"""Capture the native UI using the checked-in solver evidence (no solver run).

Run from the repository root after installing the development dependencies:
    QT_QPA_PLATFORM=offscreen python tools/capture_simulation_screenshot.py
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea

from emi_assistant.controller import Controller
from emi_assistant.ui import MainWindow
from emi_assistant.ui_simulation import ResponseChart


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    app = QApplication([])
    controller = Controller()
    # Create the window before loading the board so its startup callback does
    # not launch new solvers and replace the recorded validation results.
    window = MainWindow(controller)
    result = controller.open_board(str(ROOT / "examples/clean.kicad_pcb"))
    results = json.loads((ROOT / "validation/simulation-summary.json").read_text())["results"]
    window.show_result(result)
    window._simulation_completed(results, window._simulation_generation, result)
    window.result_tabs.setCurrentIndex(1)
    window.resize(1420, 1020)
    window.timer.stop()
    window.show()

    output = ROOT / "docs/screenshots/simulations.png"
    previous_geometry = None
    attempts = 0

    def capture_when_ready():
        nonlocal previous_geometry, attempts
        attempts += 1
        try:
            pane = window.simulations
            scroll = pane.findChild(QScrollArea)
            cards = [pane.cards.itemAt(i).widget() for i in range(pane.cards.count() - 1)]
            geometry = [(card.width(), card.height()) for card in cards]
            ready = len(cards) == len(results) and geometry == previous_geometry
            ready = ready and scroll.verticalScrollBar().maximum() > 0
            for card in cards:
                ready = ready and card.height() >= card.minimumSizeHint().height()
                for child in card.findChildren(QLabel) + card.findChildren(ResponseChart):
                    ready = ready and card.rect().contains(child.geometry())
                    if isinstance(child, QLabel):
                        ready = ready and child.height() >= child.heightForWidth(child.width())
                    else:
                        ready = ready and child.height() >= child.minimumHeight()
            previous_geometry = geometry
            if not ready:
                if attempts >= 20:
                    raise RuntimeError("Simulation cards did not settle into an unclipped layout")
                QTimer.singleShot(50, capture_when_ready)
                return
            if not window.grab().save(str(output)):
                raise RuntimeError(f"Could not save {output}")
            print(f"Saved {output}")
            window.close()
            app.exit(0)
        except Exception as error:
            print(f"Screenshot capture failed: {error}")
            window.close()
            app.exit(1)

    # Let show, resize, layout, and paint events run before inspecting geometry.
    QTimer.singleShot(50, capture_when_ready)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
