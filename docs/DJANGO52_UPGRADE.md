# Django 5.2 security and compatibility record (#119)

Source assessment: 2026-11-02. Linked umbrella: #55. This is a source upgrade
candidate, not production clearance or authorization to deploy.

## Selected versions and upstream evidence

[Django's support table](https://www.djangoproject.com/download/) lists 5.2.18
as the supported 5.2 LTS patch (extended support through April 2028).
[PyPI version metadata](https://pypi.org/pypi/Django/5.2.18/json) confirms a
non-yanked stable wheel uploaded 2026-10-06, requiring Python >=3.10 and
asgiref >=3.8.1. Python 3.12 remains supported by
[Django 5.2](https://docs.djangoproject.com/en/5.2/faq/install/).
[Python 3.12.16](https://www.python.org/downloads/release/python-31216/) is the
October 25 security release; the
[official image manifest source](https://github.com/docker-library/official-images/blob/master/library/python)
lists `3.12.16-slim-bookworm`. The source sandbox could not query the registry
digest or build it. The tag is micro-pinned, still mutable, and needs immutable
base-digest evidence before promotion.

Exact selected versions remain in the authoritative requirements files.
Version-specific PyPI JSON endpoints follow
`https://pypi.org/pypi/<package>/<version>/json`.

| Boundary | Selected inputs and reason |
| --- | --- |
| Django/ASGI | Django 5.2.18, asgiref 3.11.0 |
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
  review removed legacy timezone/storage APIs, GIS minimums and
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
No application models, API schema or applied migrations are changed. Migration
drift and fresh migration execution remain required; no schema rollback is
claimed necessary based solely on source inspection. If framework-generated
drift appears, inspect SQL and forward/backward compatibility before proceeding.

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
