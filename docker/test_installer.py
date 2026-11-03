"""Installer replacement must reject tampering before changing Python's bundle."""

import importlib.util
import tempfile
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "update_ensurepip", Path(__file__).with_name("update_ensurepip.py")
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerIntegrityTests(unittest.TestCase):
    def test_tampered_wheel_leaves_existing_installer_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "ensurepip"
            bundled = package / "_bundled"
            bundled.mkdir(parents=True)
            initializer = package / "__init__.py"
            initializer.write_text('_PIP_VERSION = "25.0.1"\n', encoding="utf-8")
            old = bundled / "pip-25.0.1-py3-none-any.whl"
            old.write_bytes(b"original bundle")
            wheel = root / installer.WHEEL_NAME
            wheel.write_bytes(b"tampered wheel")

            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                installer.update_bundle(wheel, package)

            self.assertEqual(old.read_bytes(), b"original bundle")
            self.assertEqual(initializer.read_text(), '_PIP_VERSION = "25.0.1"\n')
            self.assertEqual(list(bundled.iterdir()), [old])
