# Native GIS and installer image contract

This bounded source candidate retains Python 3.12.16, Django 5.2.19, both
application requirements manifests, application code, API/schema, applied
migrations and PostGIS versions. It changes the backend images and their
verification. A source change or image build does not authorize rollout.

## Selected upstream inputs

- Official `python:3.12.16-slim-trixie`, index
  `sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07`.
  This identity comes from the existing November Task 7 registry pull and
  manifest evidence (`/tmp/task7-trixie-pull.log` and
  `/tmp/task7-trixie-manifest.json`); it is not a new successful registry query.
  That index includes amd64 manifest
  `sha256:1148dbb014615300fa7e713b2f94f550c0d1c69780c0805f84f093ceee34dd0b`
  and arm64 manifest
  `sha256:b224e2fe306f9d303251ada22961635e5b52a69c0414a0050611be31c59f5f5b`.
  Revalidate registry/provenance identity before promotion; CI currently
  executes amd64 only. The Dockerfile uses the builder's built-in frontend
  rather than fetching a floating Dockerfile frontend image.
- Debian stable Trixie
  [libgdal36 3.10.3+dfsg-1](https://packages.debian.org/trixie/libgdal36),
  with same-suite [GEOS 3.13.1-1](https://packages.debian.org/trixie/libgeos-c1t64)
  and [PROJ 9.6.0-1](https://packages.debian.org/trixie/libproj25).
  Use Debian's compatible dependency closure, not Bookworm/Trixie repository
  mixing or copied binary libraries. GDAL's direct package version is exact;
  removal from the active archive makes the build fail until deliberately
  repinned. APT still verifies Debian's signed repository metadata.
- [pip 26.2.2 metadata](https://pypi.org/pypi/pip/26.2.2/json): non-yanked,
  Python >=3.10, wheel SHA-256
  `fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70`.
  The download accepts only this wheel, without dependencies or source-build
  hooks. The bundle replacement checks that identity again before editing.

These inputs were assessed on 2026-11-03. Debian dependency/security packages
and undeclared Python transitives still resolve from active repositories;
there is no new complete application lock or bit-for-bit reproducibility
claim. Record final package inventories and image IDs for every build. Promote
the recorded registry digest, never rebuild an environment-specific tag.

## Security and compatibility boundaries

[Django 5.2.19](https://docs.djangoproject.com/en/5.2/releases/5.2.19/)
disables filesystem/network resolution of `SpatialReference` strings only
with GDAL >=3.9. Both targets must load the actual GDAL/GEOS/PROJ libraries,
round-trip a point between EPSG 4326 and 3857, accept normal CRS strings and
integer SRIDs, and reject valid local-WKT paths and HTTP/VSI references.
The network regression uses a reachable loopback WKT server and records both
GET and HEAD attempts. No external service or application data is involved.
The GIS registry check runs without warning filters or silenced check IDs.

Development retains the GDAL CLI and development manifest. Unneeded binutils
and PROJ development headers are no longer explicitly installed. CPython's
bundled pip is replaced with the exact verified wheel and matching bootstrap
version; a fresh offline venv must actually install pip 26.2.2. Production
retains only GDAL's required shared-library closure, the runtime manifest,
Gunicorn and UID/GID 10001. Its build runs `pip check`, records the validated
build graph, then removes installed pip and the entire ensurepip bundle.
The final-image check asserts no installers, development Python packages,
GDAL CLI or compiler tools and exports actual native/Python/OS identity.

This is not a clean advisory report. The earlier Trixie probe reported native
critical/high findings. A current exact-image assessment, including native
and wheel-vendored libraries, remains required; no prior findings are waived.

## Isolated verification

CI's `test` job runs the complete existing backend suite, migration drift,
fresh migration/plan, search plans and viewport benchmark inside the candidate
development image, so an older Ubuntu-host GDAL cannot stand in for the image.
It uses `docker/compose.ci.yml` with a unique project, labelled services,
private network, synthetic credentials and a tmpfs test database. It has no
host ports, host-source mounts, external volumes or media service. Existing
media tests retain their controlled fixtures; no live storage is contacted.

From this isolated backend clone, inspect before starting:

```sh
export COMPOSE_FILE=docker/compose.ci.yml
export COMPOSE_PROJECT_NAME=smokemap-gdal-candidate-unique-id
export CANDIDATE_SHA="$(git rev-parse HEAD)"
docker compose config --quiet
docker compose config --format json
docker compose build backend
docker compose up --detach --wait db
docker compose run --rm backend python -m unittest backend.test_native_runtime -v
docker compose run --rm backend python -m unittest discover -s docker -p 'test_*.py' -v
docker compose run --rm backend python manage.py check
docker compose run --rm backend python manage.py makemigrations --check --dry-run
docker compose run --rm backend python manage.py migrate --plan
docker compose run --rm backend python manage.py migrate --noinput
docker compose run --rm backend python manage.py test
docker compose run --rm backend python manage.py inspect_place_search_plans
docker compose run --rm backend python manage.py benchmark_viewport_places
docker compose down
```

The `production-image` job builds both targets, checks installed-package
compatibility during build, tests native security in both images, tests the
development venv as UID/GID 10001 with network disabled, verifies production
installer absence and non-root identity, and retains strict-staging Gunicorn
liveness and unavailable-database readiness. Evidence includes source SHA,
build-input hashes, image ID/size, build-time pip graphs and final native and
package inventories. PR checks check out the actual head SHA and label the
image with that SHA rather than GitHub's synthetic merge SHA. The `secrets`
job retains its full-history redacted scan.

## Current execution limits

The supplied base ends in an extra `b` and has 41 characters. The clean clone,
fetched remote development and PR #122 squash commit agree on the actual base
`7bb168b4e04f366ae3bf25ef7e981444e2499227`.
`TASK7_BACKEND_STATUS.md` and `M5_EXECUTION_PLAN.md` are absent; actual shared
AGENTS guidance, running-root `docs/DEVELOPMENT_VERIFICATION.md`, Task 7 reports
and backend #119/#121 upgrade evidence were used instead.

Local source checks are separate from configured CI. The authoring sandbox
allows direct Docker builds and Compose configuration rendering, but blocks
container execution and Compose builds at the daemon socket. The first build
also failed on registry auth for the floating Dockerfile frontend; using the
built-in frontend removes that dependency. GitHub App API access and direct
host PyPI downloads are blocked. Installed native tests, the full PostGIS suite
and remote CI cannot be claimed as passes until executed. The App helper must be used
to create exactly one new bounded backend issue and PR when access is restored;
do not reuse or duplicate #119/#120/#121/#122. No root integration, merge,
independent review or deployment is performed by this implementation stage.

Authoring checks passed: 19 existing stdlib manifest/staging-configuration
tests, one installer-tampering regression, Python AST parsing, workflow
YAML/shell syntax, resolved Compose isolation and `git diff --check`. Both
Docker targets built and their build-time `pip check` passed. The redacted
history scan passed before committing; the final-head scan and image IDs are
recorded in the external stage evidence, not inferred from these source checks.
