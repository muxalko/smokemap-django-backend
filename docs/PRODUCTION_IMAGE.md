# Production image and staging runtime contract

The `production` Docker target is the backend's minimal deployment candidate.
It retains the supported Python 3.12 baseline, installs only the canonical
exact-pinned runtime manifest, runs as UID/GID 10001, and serves
`smokemap.wsgi:app` with Gunicorn. The existing `development` target and Django
development server remain unchanged.

This target is a release building block, not authorization to deploy. It makes
configuration failure deterministic and supplies an immutable artifact path;
the unresolved blockers below still apply.

## Build and provenance

Build from a clean commit, pass that exact commit as the OCI revision, and do
not pass credentials as build arguments or copy a dotenv file into the context:

```sh
source_sha="$(git rev-parse HEAD)"
docker build \
  --target production \
  --build-arg VCS_REF="$source_sha" \
  --tag "smokemap-backend:$source_sha" \
  .
```

For a promotable multi-platform registry artifact, enable BuildKit provenance
and SBOM output and push it once:

```sh
source_sha="$(git rev-parse HEAD)"
docker buildx build \
  --target production \
  --build-arg VCS_REF="$source_sha" \
  --provenance=mode=max \
  --sbom=true \
  --platform linux/amd64,linux/arm64 \
  --tag "registry.example.com/smokemap/backend:$source_sha" \
  --push \
  .
docker buildx imagetools inspect \
  "registry.example.com/smokemap/backend:$source_sha"
```

Record all of the following in release evidence:

- the full source commit SHA and a clean-tree assertion;
- the `Dockerfile` and `requirements.txt` SHA-256 values;
- the resolved `python:3.12-slim-bookworm` base digest from BuildKit
  provenance;
- the registry index digest and each platform manifest digest;
- the CI run URL, migration/test result, Gitleaks result, builder identity, and
  build timestamp.

Promotion must refer to
`registry.example.com/smokemap/backend@sha256:<index-digest>`, never rebuild a
mutable tag for each environment. Verify the OCI revision label and registry
attestations before promotion. Rollback likewise selects a previously verified
image digest; it never reverses a schema migration implicitly.

## Required staging environment

The production image defaults to `SETTINGS_MODE=staging`. Startup raises a
configuration error when any variable below is absent, blank, duplicated where
applicable, or malformed. Errors identify variable names only and never echo
values.

| Variable | Contract |
| --- | --- |
| `DJANGO_SECRET_KEY` | Secret-manager value of at least 50 characters; never an image layer or manifest value |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated exact public hostnames; no wildcard, URL, port, or development host |
| `CORS_ALLOWED_ORIGINS` | Non-empty comma-separated HTTPS origins without paths, credentials, query strings, or fragments |
| `CSRF_TRUSTED_ORIGINS` | Same strict HTTPS-origin contract as CORS |
| `DJANGO_TRUST_X_FORWARDED_PROTO` | Must be exactly `true`, acknowledging the trusted proxy contract below |
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Explicit PostGIS connection values |
| `POSTGRES_OPTIONS` | Explicit libpq options, normally `-c search_path=smokemap` |
| `POSTGRES_SSLMODE` | `require`, `verify-ca`, or `verify-full`; `disable` is rejected |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` | Injected S3-compatible credentials and region |
| `AWS_STORAGE_BUCKET_NAME` | Legacy/public bucket; valid S3 bucket name |
| `AWS_S3_ENDPOINT_URL` | Explicit HTTPS endpoint for the legacy S3-compatible binding |
| `AWS_S3_ADDRESSING_STYLE` | Explicit `path` or `virtual` choice |
| `MEDIA_STORAGE_BUCKET_NAME` | Private source/rendition bucket, different from the public bucket |
| `MEDIA_STORAGE_IDENTIFIER` | Stable non-secret binding identifier; changing it does not rewrite stored records |
| `MEDIA_STORAGE_INTERNAL_ENDPOINT_URL` | Explicit backend-reachable HTTP(S) endpoint; plain HTTP is only acceptable on an isolated private network |
| `MEDIA_UPLOAD_ENDPOINT_URL` | Explicit browser-reachable HTTPS endpoint used for presigned upload and preview URLs |

`GDAL_LIBRARY_PATH` and `GEOS_LIBRARY_PATH` remain optional overrides. The
image discovers its installed libraries when those overrides are absent.

Staging forces `DEBUG=False`, disables Django admin, redirects HTTP to HTTPS,
marks session/CSRF/JWT cookies secure, and trusts
`X-Forwarded-Proto: https`. The edge proxy is therefore a hard prerequisite:
it must terminate TLS, remove any client-supplied forwarding header, set the
header itself, preserve the exact request host, and be the only public route to
Gunicorn. Direct public access to port 8000 is unsupported.

## Startup and health contract

Run migrations as a separate, single-writer release job using the same image
digest and staging environment:

```sh
python manage.py check --deploy
python manage.py migrate --noinput
```

Do not run migrations concurrently in every web replica. Start the web
replicas only after the migration job succeeds. Configure the platform to use
`/health/live/` for liveness and `/health/ready/` for readiness; readiness
requires a successful PostGIS query. The image-level health check deliberately
uses liveness so a database outage does not cause a destructive restart loop.

Deployments must also schedule and monitor
`python manage.py process_media_cleanup` as already described in the README.
PostGIS and both object-store buckets must exist before the migration and web
jobs start. The private bucket must deny anonymous access.

## Unresolved release blockers

- Django 4.2 reached end of extended support on 2026-04-07, and this repository
  is pinned to 4.2.22 rather than even the final 4.2.30 patch. The planned
  framework/dependency upgrade remains a blocker for an internet-facing
  production release; it is intentionally not mixed into this container slice.
- `requirements.txt` is an exact-pin input manifest but is not a complete,
  hash-verified transitive lock. The Python base tag is also mutable. Builds are
  repeatable from the declared inputs, but not promised to be bit-for-bit
  reproducible; the pushed registry digest and attestations are the promotion
  authority.
- No registry, TLS proxy, secret manager, PostGIS service, S3/MinIO service,
  cleanup scheduler, monitoring/error-reporting sink, or deployment platform is
  provisioned by this repository.
- Backup/restore drills, schema-aware rollback procedures, error reporting,
  and production observability remain tracked by umbrella issue #55. A passing
  image build does not satisfy those release gates.
- Docker daemon access is required to build and execute the image. When a
  sandbox denies daemon access, CI must supply the image-build, startup, and
  digest evidence before promotion.

The current Python/Django compatibility and support status should be
revalidated during the framework upgrade against the official
[Django downloads and support table](https://www.djangoproject.com/download/).
