"""Read and display the license documents shipped with EMI Assistant.

Only fixed bundled filenames are read. License text is displayed as plain text;
opening this window never fetches a webpage or installs a dependency.
"""
from __future__ import annotations

from pathlib import Path


NOTICE = (
    "EMI Assistant's original code is provided under the MIT license. "
    "Third-party components retain their own copyrights and licenses.\n\n"
    "This application uses PySide6, Shiboken6, and the Qt Core, GUI and Widgets "
    "libraries under their GNU Lesser General Public License version 3 "
    "(LGPLv3) option. You may replace these libraries with compatible modified "
    "versions. EMI Assistant places no restriction on reverse engineering "
    "to debug those modifications.\n\n"
    "Select a document below to read its full text, dependency notices, or "
    "source and replacement instructions."
)

# The same fixed names are used in wheel package data, where all files are flat.
DOCUMENTS = (
    ("THIRD_PARTY_NOTICES.md", "Third-party notices and source information"),
    ("LICENSE", "MIT — EMI Assistant"),
    ("LGPL-3.0.txt", "GNU Lesser General Public License v3"),
    ("GPL-3.0.txt", "GNU General Public License v3"),
    ("LGPL-2.1.txt", "GNU Lesser General Public License v2.1"),
    ("KiCad-LICENSE-README.txt", "KiCad package metadata schemas"),
)


def read_license_document(name: str, *, root: Path | None = None,
                          data_directory: Path | None = None) -> str:
    """Load a known document from a source/PCM root, then wheel package data."""
    if name not in {item[0] for item in DOCUMENTS}:
        raise ValueError("Unknown bundled license document")
    package = Path(__file__).resolve().parent
    root = Path(root) if root is not None else package.parent
    data_directory = (Path(data_directory) if data_directory is not None
                      else package / "data" / "licenses")
    source = root / name if name in {"LICENSE", "THIRD_PARTY_NOTICES.md"} else root / "LICENSES" / name
    for path in (source, data_directory / name):
        try:
            if path.is_file() and path.stat().st_size <= 2_000_000:
                text = path.read_text(encoding="utf-8")
                if text.strip():
                    return text
        except (OSError, UnicodeError):
            continue
    return (f"The bundled document {name} is missing or unreadable.\n\n"
            "Reinstall EMI Assistant from its complete release package to "
            "restore the license documents.")


def show_licenses(parent=None) -> None:
    """Open the native, read-only licenses window without an acceptance gate."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QComboBox, QDialog, QDialogButtonBox, QLabel, QTextBrowser, QVBoxLayout,
    )

    dialog = QDialog(parent)
    dialog.setWindowTitle("EMI Assistant — Licenses")
    dialog.resize(780, 700)
    layout = QVBoxLayout(dialog)
    notice = QLabel(NOTICE)
    notice.setObjectName("licenseNotice")
    notice.setTextFormat(Qt.TextFormat.PlainText)
    notice.setWordWrap(True)
    notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    layout.addWidget(notice)
    selector = QComboBox()
    selector.setObjectName("licenseSelector")
    selector.setAccessibleName("License document")
    for filename, title in DOCUMENTS:
        selector.addItem(title, filename)
    layout.addWidget(selector)
    text = QTextBrowser()
    text.setObjectName("licenseText")
    text.setOpenExternalLinks(False)
    text.setOpenLinks(False)
    layout.addWidget(text, 1)
    selector.currentIndexChanged.connect(
        lambda _: text.setPlainText(read_license_document(selector.currentData())))
    text.setPlainText(read_license_document(selector.currentData()))
    close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    close.rejected.connect(dialog.reject)
    layout.addWidget(close)
    dialog.exec()
