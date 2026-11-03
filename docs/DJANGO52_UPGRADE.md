# Django 5.2 security and compatibility record (#119, #121)

Source assessment: 2026-11-03 (UTC); #121 patch assessment supersedes the
November 1 selection from #119. Linked umbrella: #55. This is a source upgrade
candidate, not production clearance or authorization to deploy.

## Selected versions and upstream evidence

[Django's support table](https://www.djangoproject.com/download/) lists 5.2.19
as the supported 5.2 LTS patch (extended support through April 2028).
[PyPI version metadata](https://pypi.org/pypi/Django/5.2.19/json) confirms a
non-yanked stable wheel uploaded 2026-11-03 at 15:22:03 UTC, requiring
Python >=3.10 and asgiref >=3.8.1. Python 3.12 remains supported by
[Django 5.2](https://docs.djangoproject.com/en/5.2/faq/install/).
[Python 3.12.16](https://www.python.org/downloads/release/python-31216/) is the
October 25 security release; the
[official image manifest source](https://github.com/docker-library/official-images/blob/master/library/python)
lists `3.12.16-slim-bookworm`. Jenkins independently pulled the official image
and recorded its immutable index digest:
`sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337`.
The Dockerfile pins that digest. The installer is explicitly patched to pip
26.2.2 before installing dependencies; CI uses the same installer patch.

Exact selected versions remain in the authoritative requirements files.
Version-specific PyPI JSON endpoints follow
`https://pypi.org/pypi/<package>/<version>/json`.

| Boundary | Selected inputs and reason |
| --- | --- |
| Django/ASGI | Django 5.2.19, asgiref 3.11.0 |
| REST/GIS/filter | DRF 3.18.2, DRF-GIS 1.3.0, django-filter 25.2; GIS metadata permits DRF <3.19 and filter <27. Older GIS 1.2.0 caps DRF below 3.17. |
| GraphQL | graphene-django 3.2.3, graphql-core 3.2.12; retain Graphene 3 and graphql-core 3.2 semantics |
| JWT | PyJWT 2.15.1 with cryptography explicitly pinned to 50.0.2; SimpleJWT 5.5.1 retained as a dependency, without activating its authentication backend |
| Storage/HTTP | boto3 and botocore 1.43.100, s3transfer 0.19.0, urllib3 2.8.0, idna 3.20; old botocore 1.34.50 required urllib3 <2.1 on Python 3.12 and prevented the patched selection |
| Media/database | Pillow 13.0.0 and psycopg2-binary 2.9.13; keep the existing driver family and image validation policy |
| SQL parsing | sqlparse 0.6.1, published November 1; upstream declares this its final release. Its maintenance status remains a follow-up risk. |
| Development utilities | Fiona 1.10.1 and pandas 2.3.3; retain the tracked shapefile generator and avoid the old pandas 2.2.1 wheel's NumPy 2 ABI mismatch |

Gunicorn 23.0.0 and its WSGI arguments, UID/GID 10001, health routes and strict
staging configuration stay in place. No optional dependencies are removed.
Dependabot #117 (DRF 3.18.2) and #118 (graphql-core 3.2.12) overlap this branch;
neither PR is modified by this task.

## Incremental compatibility review

- [5.0 release notes](https://docs.djangoproject.com/en/5.0/releases/5.0/):
  review removed legacy timezone/storage APIs and OSMGeoAdmin, GIS minimums and
  `update_or_create(create_defaults=...)`. No application import of the removed
  timezone aliases or legacy storage setting was found. The app uses Django's
  built-in PostGIS backend, not a custom database backend.
- [5.1 release notes](https://docs.djangoproject.com/en/5.1/releases/5.1/):
  review PostgreSQL/GIS minimums, thread isolation and constraint deprecations.
  Concurrency tests use `TransactionTestCase`. Existing `CheckConstraint(check=)`
  declarations are deprecated but still supported in 5.2; warnings are not
  suppressed and applied migrations are not rewritten.
- [5.2 release notes](https://docs.djangoproject.com/en/5.2/releases/5.2/):
  PostgreSQL >=14, PostGIS >3.0 and GDAL >3.0 are required; CI retains
  PostGIS 16-3.4 and Bookworm's GDAL 3.6. Review ordered `values_list()` output,
  constraints, and content negotiation against the existing search/API suite.
  No composite keys, schema redesign or endpoint changes are introduced.
- [5.2.18 patch notes](https://docs.djangoproject.com/en/5.2/releases/5.2.18/):
  include spatial lookup and geometry-depth fixes. Viewport input remains
  validated numeric bounds converted into geometry; no untrusted raster bytes
  are added. Existing viewport/search tests remain unchanged.

PyJWT now validates subject types. The added HTTP/GraphQL regression requires
numeric/list/object subjects to map to the existing stable `INVALID_TOKEN` or
REST 401 response; issued subjects already use strings. Existing valid-token,
refresh rotation/reuse/revocation, role and media/race assertions are retained.
GeoDjango admin now uses the supported `GISModelAdmin` (default OSM widget);
the removed `OSMGeoAdmin` import caused the first CI system check to fail.
No application models, API schema or applied migrations are changed. Migration
drift and fresh migration execution remain required; no schema rollback is
claimed necessary based solely on source inspection. If framework-generated
drift appears, inspect SQL and forward/backward compatibility before proceeding.

## November 3 security patch and application applicability (#121)

[5.2.19 release notes](https://docs.djangoproject.com/en/5.2/releases/5.2.19/)
report four low-severity fixes and one moderate fix over 5.2.18:

| CVE (severity) | Upstream change | Current application boundary |
| --- | --- | --- |
| CVE-2026-108147 (low) | Bounds memory use for base64 multipart uploads. | DRF accepts JSON only (`smokemap/settings.py`); media bytes upload directly to object storage. Django admin forms remain a multipart caller when admin is enabled locally. |
| CVE-2026-111211 (low) | Escapes invalid geometry form values in logs. | `backend/admin.py` registers Address and GIS Place admin; admin is disabled in strict staging. Public serializers are distinct from Django GIS forms. |
| CVE-2026-111362 (moderate) | Rejects external GeoJSON CRS in geometry construction, forms and spatial lookups. | Submissions use validated numeric coordinates and `Point(..., srid=4326)` (`backend/submissions.py`). Viewport bounds become a numeric polygon (`backend/views.py`); legacy bbox filters take numeric bounds. GeoJSON output does not imply arbitrary GeoJSON input. Local admin geometry forms remain relevant. |
| CVE-2026-113593 (low) | Separates missing and empty Vary header cache keys. | No `UpdateCacheMiddleware` or `cache_page()` callers are configured. Public managed media explicitly returns `Cache-Control: no-store`. |
| CVE-2026-114582 (low) | Restricts SpatialReference string parsing; rejects DICT references on all GDAL versions. | No direct application `SpatialReference` or geometry/raster transform callers were found. Models and submission points use integer SRIDs. |

Full string-parsing protection requires GDAL >=3.9. The retained Bookworm
GDAL 3.6 is below that threshold: expect new `gis.W001`, keep it visible, and
do not claim full remediation. Prefer integer SRIDs and validated points;
any future string CRS input on older GDAL needs an explicit allowlist.
`DataSource`/`LayerMapping` still allow external CRS for trusted imports:
`load_shp.py` uses `LayerMapping` with a fixed local shapefile path and
`transform=False`; it must not become an untrusted upload ingestion path.

The wheel SHA-256 from current PyPI metadata is
`5965d974a5218a2b68a11dd1276434707f23886525c23f0598eccedaf4f4ca00`;
the sdist SHA-256 is
`ad03d8abc51dbff95ebf34b27123cf0c26a3b62db7ba0cd520783686362533d1`.
Both artifacts are non-yanked. These metadata digests are provenance references,
not a new lockfile or proof of an installed graph. Only the Django runtime pin
changes in #121; all other pins, the Python image digest, configuration,
application code, tests and migrations remain unchanged. Existing boundary
regressions remain the acceptance suite; no new input boundary is introduced.

## Achieved evidence and remaining gates

The source sandbox passes 19 stdlib manifest/staging configuration tests,
Python AST parsing, workflow YAML/shell syntax and `git diff --check`.
These tests do **not** import the installed Django runtime. Docker socket access
is denied, host dependency installation is prohibited, and direct PyPI access
for pip resolution is blocked. Browser metadata checks are not a resolved graph
or a substitute for installed compatibility tests.

CI now builds development and production targets sequentially, checks both
installed paths, exercises the utility imports, captures `pip inspect` and
`pip freeze --all` artifacts with timestamps, runs strict-staging Gunicorn
liveness and unavailable-DB readiness, shows the migration plan, applies it to
the disposable PostGIS database, and runs the full suite plus natural search
and viewport plans. These are configured gates, not claimed local passes.
`check --deploy` warnings must be evaluated, not silently ignored as clearance.

PyPI advisory metadata was used to avoid selecting older urllib3, PyJWT, idna
and sqlparse patches with known findings. Absence of advisories in a queried
metadata response is not a reachability assessment or a complete security scan.
Full production closure (including cryptography's native libraries, psycopg2
wheel libraries, OS packages and packaging tools) and its current advisory
report remain pending on the exact built image. No finding is waived and no
critical/high closure count is claimed. Prior October 31 aggregate scan
summaries cover different installed images/graphs and cannot clear this build.

Root owns CI/secrets results, independent exact-head review (this leaf may not
spawn reviewers), exact-image scans, cross-application E2E, frozen development
integration/rollout and recovery evidence. Rollback selects the previously
verified image/source plus matching requirements for API and cleanup together;
it never reverses migrations implicitly. No live DB, storage, configuration,
staging or production access is part of these checks. The legacy Vercel build
script still selects Python 3.9 and is not a supported path for this baseline;
use the documented Python 3.12/container paths.

## Jenkins follow-through before final-head acceptance

The initial source head failed CI because local-mode admin autodiscovery
imported removed OSMGeoAdmin. A separate isolated source copy with the supported
GISModelAdmin replacement passed all 367 backend tests, fresh PostGIS migration,
migration drift, search plans and viewport benchmarking on 1 November. This is
provisional compatibility evidence; final-head CI and builds remain required.

An installed-graph OSV query on the initial candidate found pip 25.0.1 and
python-dotenv 1.0.1 advisories. Both are patched here (pip 26.2.2 and
python-dotenv 1.2.4). A separate patched probe returned zero OSV package matches
across its 39 installed Python distributions. Final exact-image evidence is
still required. No source metadata response substitutes for that scan.

The Bookworm candidate image scan reported native/OS findings, including
4 critical and 109 high matches with no listed fixed version. An isolated
Trixie/GDAL36 probe still reported 5 critical and 103 high matches; it is not
a security closure and was not selected. Native/GDAL/OS hardening and
applicability assessment remain explicit production launch blockers. These
raw counts are not exploit counts, and no risk exception is granted. This
bounded Django/Python upgrade does not close the overall image-security gate.
