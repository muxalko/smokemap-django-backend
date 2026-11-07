"""Replace CPython 3.12's offline pip bundle with the verified installer wheel."""

import hashlib
import re
import shutil
import sys
from pathlib import Path


PIP_VERSION = "26.2.2"
WHEEL_NAME = f"pip-{PIP_VERSION}-py3-none-any.whl"
WHEEL_SHA256 = "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"


def update_bundle(wheel, package):
    # Verify before changing the bundle. Never let an unexpected CPython layout
    # produce a successful image with an old or unusable bootstrap installer.
    if wheel.name != WHEEL_NAME or hashlib.sha256(wheel.read_bytes()).hexdigest() != WHEEL_SHA256:
        raise ValueError("Installer wheel identity mismatch")
    initializer = package / "__init__.py"
    source = initializer.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'^_PIP_VERSION = "[0-9.]+"$',
        f'_PIP_VERSION = "{PIP_VERSION}"',
        source,
        flags=re.MULTILINE,
    )
    bundled = package / "_bundled"
    if count != 1 or not bundled.is_dir():
        raise ValueError("Unsupported ensurepip layout")
    for old_wheel in bundled.glob("pip-*.whl"):
        old_wheel.unlink()
    shutil.copyfile(wheel, bundled / WHEEL_NAME)
    initializer.write_text(updated, encoding="utf-8")
    # The base may contain bytecode for the old constant.
    shutil.rmtree(package / "__pycache__", ignore_errors=True)


if __name__ == "__main__":
    import ensurepip

    update_bundle(Path(sys.argv[1]), Path(ensurepip.__file__).parent)
