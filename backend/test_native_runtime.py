"""Exercise the actual GeoDjango C libraries, without a database or remote I/O."""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import django
from django.contrib.gis.gdal import GDALException, SpatialReference, gdal_version
from django.contrib.gis.gdal.error import SRSException
from django.contrib.gis.geos import GEOSGeometry, Point, geos_version


class NativeRuntimeTests(unittest.TestCase):
    def test_supported_runtime_and_native_libraries(self):
        self.assertEqual(sys.version_info[:3], (3, 12, 16))
        self.assertEqual(django.get_version(), "5.2.19")
        version = tuple(int(part) for part in gdal_version().decode().split(".")[:2])
        self.assertGreaterEqual(version, (3, 9))
        self.assertTrue(geos_version())
        # Parse through GEOS and round-trip through GDAL/PROJ's installed EPSG DB.
        point = GEOSGeometry(Point(34.8, 32.1, srid=4326).geojson, srid=4326)
        projected = point.transform(3857, clone=True)
        self.assertAlmostEqual(projected.x, 3873918.2796, places=3)
        projected.transform(4326)
        self.assertAlmostEqual(projected.x, point.x, places=8)
        self.assertAlmostEqual(projected.y, point.y, places=8)
        for value in (4326, "4326", "EPSG:4326", "WGS84"):
            with self.subTest(value=value):
                self.assertEqual(SpatialReference(value).srid, 4326)

    def test_gis_warning_is_resolved_without_silencing(self):
        # Use an independent minimal registry so the check cannot be disabled by
        # application settings, and works in both strict and local-mode images.
        result = subprocess.run(
            [sys.executable, "-c", """
import json
import django
from django.conf import settings
from django.core import checks
settings.configure(INSTALLED_APPS=['django.contrib.gis'])
django.setup()
print(json.dumps([message.id for message in checks.run_checks()]))
"""],
            check=True, capture_output=True, text=True, timeout=20,
        )
        self.assertNotIn("gis.W001", json.loads(result.stdout))

    def test_spatial_reference_cannot_read_valid_local_wkt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference.wkt"
            path.write_text(SpatialReference(4326).wkt, encoding="utf-8")
            for value in (str(path), path.as_uri(), f"/vsicurl/{path.as_uri()}"):
                with self.subTest(kind=value.split(":", 1)[0]):
                    with self.assertRaises((GDALException, SRSException)):
                        SpatialReference(value)

    def test_spatial_reference_does_not_request_http_or_vsi_urls(self):
        requests = []
        wkt = SpatialReference(4326).wkt.encode()

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                requests.append(self.path)
                self.send_response(200)
                self.end_headers()

            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(wkt)

            def log_message(self, *args):
                pass

        # A reachable loopback fixture proves denial without public network or
        # mocking Python HTTP clients (GDAL performs its own native I/O).
        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/reference.wkt"
                with urllib.request.urlopen(url, timeout=2) as response:
                    self.assertEqual(response.read(), wkt)
                requests.clear()
                for value in (url, f"/vsicurl/{url}", f"/vsicurl_streaming/{url}"):
                    with self.subTest(kind=value.split(":", 1)[0]):
                        with self.assertRaises((GDALException, SRSException)):
                            SpatialReference(value)
                self.assertEqual(requests, [])
            finally:
                server.shutdown()
                thread.join(timeout=5)
