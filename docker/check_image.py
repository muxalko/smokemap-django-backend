"""Assert a built image's native/installer contract and emit non-secret inventory."""

import ctypes
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import django
from django.contrib.gis.gdal import gdal_version
from django.contrib.gis.gdal.libgdal import lgdal
from django.contrib.gis.geos import geos_version

from update_ensurepip import PIP_VERSION, WHEEL_NAME, WHEEL_SHA256


def check(target):
    assert target in {"development", "production"}
    assert sys.version_info[:3] == (3, 12, 16)
    assert django.get_version() == "5.2.19"
    assert tuple(map(int, gdal_version().decode().split(".")[:2])) >= (3, 9)
    if target == "production":
        assert os.getuid() == os.getgid() == 10001
        for name in ("pip", "ensurepip", "fiona", "pandas"):
            assert importlib.util.find_spec(name) is None, name
        for name in ("pip", "pip3", "pip3.12", "gdalinfo", "gcc", "g++", "make", "readelf"):
            assert shutil.which(name) is None, name
        for path in (Path("/usr/local/lib/python3.12"), Path("/usr/share/python-wheels")):
            assert not list(path.glob("**/pip-*.whl")), "Unexpected bundled pip"
    else:
        import ensurepip
        import hashlib

        assert importlib.metadata.version("pip") == PIP_VERSION
        assert ensurepip.version() == PIP_VERSION
        bundled = Path(ensurepip.__file__).parent / "_bundled"
        assert sorted(path.name for path in bundled.glob("*.whl")) == [WHEEL_NAME]
        assert hashlib.sha256((bundled / WHEEL_NAME).read_bytes()).hexdigest() == WHEEL_SHA256
        # Exercise ensurepip offline as the non-root production UID. A version
        # constant alone does not prove venv bootstraps the replacement wheel.
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([sys.executable, "-m", "venv", directory], check=True, timeout=60)
            version = subprocess.check_output(
                [f"{directory}/bin/python", "-c", "import pip; print(pip.__version__)"], text=True,
            ).strip()
            assert version == PIP_VERSION
    proj = [ctypes.c_int() for _ in range(3)]
    function = lgdal.OSRGetPROJVersion
    function.argtypes = [ctypes.POINTER(ctypes.c_int)] * 3
    function.restype = None
    function(*(ctypes.byref(part) for part in proj))
    return {
        "target": target,
        "python": sys.version.split()[0],
        "django": django.get_version(),
        "gdal": gdal_version().decode(),
        "geos": geos_version().decode(),
        "proj": ".".join(str(part.value) for part in proj),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "packages": sorted(
            [{"name": dist.metadata["Name"], "version": dist.version}
             for dist in importlib.metadata.distributions()], key=lambda item: item["name"].lower(),
        ),
        "debian_packages": subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Package}=${Version}\\n"], text=True,
        ).splitlines(),
    }


if __name__ == "__main__":
    print(json.dumps(check(sys.argv[1]), indent=2))
