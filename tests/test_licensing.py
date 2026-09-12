"""Bundled license lookup and the native read-only disclosure window."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from emi_assistant.licensing import DOCUMENTS, NOTICE, read_license_document, show_licenses

try:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QTextBrowser
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


class BundledLicenseTests(unittest.TestCase):
    def test_source_package_and_flat_wheel_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            data = Path(directory) / "wheel-data"
            (root / "LICENSES").mkdir(parents=True)
            data.mkdir()
            for name, _ in DOCUMENTS:
                source = root / name if name in {"LICENSE", "THIRD_PARTY_NOTICES.md"} else root / "LICENSES" / name
                source.write_text("source " + name, encoding="utf-8")
                (data / name).write_text("wheel " + name, encoding="utf-8")
                with self.subTest(name=name):
                    self.assertEqual(read_license_document(name, root=root, data_directory=data), "source " + name)
                    source.unlink()
                    self.assertEqual(read_license_document(name, root=root, data_directory=data), "wheel " + name)

    def test_invalid_names_are_rejected_and_missing_text_is_explained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("../secret", str(root / "secret"), "other.txt"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    read_license_document(name, root=root, data_directory=root)
            text = read_license_document("LICENSE", root=root, data_directory=root)
            self.assertIn("missing or unreadable", text)

    def test_unreadable_or_empty_source_uses_packaged_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            (data / "LICENSE").write_text("MIT fallback", encoding="utf-8")
            for contents in (b"\xff", b"\n"):
                (root / "LICENSE").write_bytes(contents)
                self.assertEqual(read_license_document("LICENSE", root=root, data_directory=data), "MIT fallback")


@unittest.skipUnless(QT_AVAILABLE, "PySide6 Essentials is not installed")
class LicenseWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_native_dialog_shows_notice_and_selects_literal_text(self):
        observed = {}

        def inspect_and_close():
            dialog = self.app.activeModalWidget()
            try:
                notice = dialog.findChild(QLabel, "licenseNotice")
                selector = dialog.findChild(QComboBox, "licenseSelector")
                browser = dialog.findChild(QTextBrowser, "licenseText")
                observed["notice"] = notice.text()
                observed["initial"] = browser.toPlainText()
                observed["count"] = selector.count()
                selector.setCurrentIndex(1)
                observed["selected"] = browser.toPlainText()
                observed["external_links"] = browser.openExternalLinks()
            finally:
                dialog.reject()

        QTimer.singleShot(0, inspect_and_close)
        with patch("emi_assistant.licensing.read_license_document", side_effect=lambda name: f"<b>{name}</b>"):
            show_licenses()
        self.assertEqual(observed["notice"], NOTICE)
        self.assertIn("LGPLv3", observed["notice"])
        self.assertEqual(observed["count"], len(DOCUMENTS))
        self.assertEqual(observed["initial"], "<b>THIRD_PARTY_NOTICES.md</b>")
        self.assertEqual(observed["selected"], "<b>LICENSE</b>")
        self.assertFalse(observed["external_links"])

    def test_main_window_exposes_licenses_without_starting_analysis(self):
        from emi_assistant.ui import MainWindow
        from emi_assistant.models import default_settings

        class Controller:
            result = None
            settings = default_settings()

        window = MainWindow(Controller())
        try:
            with patch("emi_assistant.ui.show_licenses") as show:
                window.licenses_button.click()
                show.assert_called_once_with(window)
            self.assertFalse(window.busy)
            self.assertIsNone(window.worker)
        finally:
            window.close()
