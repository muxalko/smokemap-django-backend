[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fvercel%2Fexamples%2Ftree%2Fmain%2Fpython%2Fdjango&demo-title=Django%20%2B%20Vercel&demo-description=Use%20Django%204%20on%20Vercel%20with%20Serverless%20Functions%20using%20the%20Python%20Runtime.&demo-url=https%3A%2F%2Fdjango-template.vercel.app%2F&demo-image=https://assets.vercel.com/image/upload/v1669994241/random/django.png)

# Django + Vercel

## Checks and tests

Docker Compose from the workspace root remains the canonical local environment:

```sh
make check
make test
make test-backend-fresh
```

Pull requests and pushes to `development` independently run the Django system
check and complete backend suite against an isolated PostGIS service. CI also
scans the full Git history with Gitleaks and redacts any detected values.

## Private submission media operations

Owner-bound draft media uses a private object-storage bucket configured by
`MEDIA_STORAGE_BUCKET_NAME`. It must be nonempty and different from the legacy
public `AWS_STORAGE_BUCKET_NAME`. `MEDIA_STORAGE_IDENTIFIER` is a stable,
non-secret identifier for that storage binding; changing either value does not
rewrite bindings already recorded on media intents.

Private media supports separate S3 endpoint URLs for backend object operations
and browser uploads. `MEDIA_STORAGE_INTERNAL_ENDPOINT_URL` is used for backend
reads, sealing, metadata checks, and deletion. `MEDIA_UPLOAD_ENDPOINT_URL` is
used only to sign presigned POST uploads, so its hostname must be reachable by
the browser. If either variable is absent, it inherits `AWS_S3_ENDPOINT_URL` for
backwards compatibility. An explicitly empty value selects the AWS SDK's
default endpoint resolution.

For local root Compose, the backend can reach MinIO by its service name while a
browser must use the host-published port. For example, when MinIO port 9000 is
published as host port 9000:

```sh
MEDIA_STORAGE_INTERNAL_ENDPOINT_URL=http://storage:9000
MEDIA_UPLOAD_ENDPOINT_URL=http://localhost:9000
```

If Compose publishes a different host port, use that published port in
`MEDIA_UPLOAD_ENDPOINT_URL`.

Each intent records three separate immutable keys: the compatibility
`object_key` is the client-presigned upload target, `sealed_object_key` is the
backend-only exact verified source, and `rendition_object_key` is a decoded and
re-encoded display copy with embedded metadata removed. None of these keys is
returned publicly. The private preview continues to use the sealed source.

### Private media preview

The `mediaAttachmentPreviewV3` GraphQL query issues a short-lived (at most 10
minutes), exact-object presigned GET for one verified, sealed, attached
managed image. It never returns a bucket, key, credential, checksum, upload
form, or permanent/public URL - only a `url` and its `expiresAt`. It is signed
with the same browser-reachable `MEDIA_UPLOAD_ENDPOINT_URL` client used for
upload presigning, so the local MinIO wiring above covers it without changing
any bucket ACL.

Authorization is fail-closed and state-scoped: the active owner may preview
their own `draft` or `pending` attachment, and a moderator/administrator may
preview a `pending` attachment under the existing M1 review permission
(never another owner's `draft`). Every other case - guest, inactive, another
owner, a terminal submission state, or an attachment that is not a verified
sealed managed image - returns the same stable `NOT_FOUND`/`UNAUTHENTICATED`
outcome so existence is never distinguishable from denial.

Deployments must schedule `python manage.py process_media_cleanup` periodically.
The command first expires drafts after 30 days of relevant inactivity, then
expires abandoned intents, retries exact-key object deletion, removes stale
client-upload objects after verified bytes have been sealed, and redacts expired
upload authorizations from idempotency evidence. This repository does not
prescribe a scheduler; use the deployment platform's periodic-job facility and
monitor command failures.

Draft request media is intentionally absent from generic `Request.imageSet` and
public `Place.imageSet` relations. Authorized clients receive M3 private request
media only through the owner-bound media mutation responses. Approved legacy
place images remain available through the public place image relation.

### Approved managed media

Approval publishes one opaque record per retained managed attachment. Public
place GraphQL/REST metadata exposes only its application URL, opaque public ID,
position, MIME type, byte size and dimensions. Anonymous bytes are served by
`GET /api/v1/media/<public-id>/`; the backend reads the private sanitized
rendition and rechecks the publication, approved submission, place link, intent
and attachment under their canonical locks before returning `Cache-Control:
no-store`. Source and rendition buckets/keys, intent/submission/owner IDs,
checksums and filenames never cross that boundary.

`revokePublicMediaV4` is an administrator-only, idempotent exceptional-removal
mutation. It durably audits and revokes the public record, removes the live
attachment, and transitions the exact upload/source/rendition binding to
`cleanup_pending` in one transaction. Object deletion remains asynchronous and
uses the existing lease, retry and exact-key checks; a deletion failure cannot
make a revoked URL public again.

## Category reference data

Categories are administrator-managed physical-setting reference data. Clients
may read them through the existing GraphQL `categories` query, but no client API
creates categories. Each category has a backend-issued, exact, case-sensitive
`slug` that is separate from its display `name`; issued slugs are unique and
immutable, while administrators may update display names and descriptions.

The initial slug-to-name mappings are `indoors` → “Indoors”, `outdoors` →
“Outdoors”, `rooftop` → “Rooftop”, `underground` → “Underground”,
`on-the-water` → “On the water”, `underwater` → “Underwater”, `in-the-air` →
“In the air”, and `other` → “Other”. Additions or other taxonomy evolution must
use a new schema/data migration and must never edit the applied
`backend/migrations/0001_initial.py` or repurpose a published slug.

`Place` and `Request` continue to hold exactly one protected category foreign
key. Their existing numeric category inputs, and the viewport category-ID
filter, remain compatibility interfaces until the submission-input work changes
them to exact slug lookup.

## Tag proposals and public vocabulary

Submission tags are optional free-form proposals. Each submission accepts at
most 10 distinct strings through the existing list-of-strings GraphQL input and
retains the owner's order. The backend Unicode NFKC-normalizes each value,
trims it, collapses internal whitespace, and then requires 3 through 50 Unicode
characters. Duplicate detection uses the normalized value's Unicode casefold;
null, empty, too-short, too-long, and duplicate values are rejected before any
tag row is written.

All request and place associations use one relational `Tag` vocabulary keyed
by that canonical value. Each request-tag association separately keeps the
normalized display spelling submitted for that request, so request history is
an immutable proposal snapshot even when multiple requests share a canonical
tag. An existing public tag is reused without changing its established public
display spelling. A newly proposed tag remains private and is omitted from the
global GraphQL `tags` reference query until a moderator successfully approves a
submission using it. Approval promotes the shared tag using the approved
request's display spelling unless it is already public, and attaches the same
tag row to the resulting `Place` in one database transaction. A failed approval
therefore cannot publish vocabulary or leave partial place/tag state. Public
tag objects expose only `id` and `name`; canonical keys and moderation
visibility are internal fields.

## Bounded place search API

The public autocomplete contract searches only `Place`, the approved public
projection produced by moderation. Submission `Request` rows in draft, pending,
rejected, withdrawn, or expired states are never part of the query.

```text
GET /api/v1/places/search/?q=alpha%20lounge&limit=10
```

`q` is required. The backend applies Unicode NFKC normalization, collapses and
trims whitespace, and lowercases the query before validating its inclusive
2-to-100-character range; a query containing a NUL character is rejected.
`limit` defaults to 10 and must be an integer from 1
through the hard maximum of 20. Invalid input returns HTTP 400 with the stable
`invalid_search` code. No-match input succeeds with an empty `results` list.

Matching and ranking happen in PostgreSQL. Case-insensitive normalized prefix
matches sort before fuzzy matches. Within each tier, trigram similarity sorts
descending, followed by normalized name and numeric place ID for a completely
deterministic order. Fuzzy inclusion uses a request-local similarity threshold
of `0.3`; it does not depend on mutable pooled-connection state. A query from
which `pg_trgm` extracts no trigrams (for example punctuation-only input) cannot
match fuzzily, so it searches prefixes only and never forces a sequential
scan. The query is
sliced to the requested limit before evaluation and uses `select_related` for
its address and category context.

Each result contains only `id`, `name`, a nullable address label, GeoJSON point
`location`, category `{id, slug, name}`, and `match` (`prefix` or `fuzzy`). The
endpoint is anonymous read-only and returns the same public projection to every
role. It does not expose descriptions, websites, tags, images, submission
owners/reviewers, moderation state, or audit data.

PostgreSQL `pg_trgm` is provisioned by migration. An expression B-tree index on
`lower(name) varchar_pattern_ops` supports normalized prefix candidates and an
expression GIN `gin_trgm_ops` index supports fuzzy candidates. The GIN index is
created with `fastupdate = off`: public places change only through moderation
approval, and entries left in the GIN pending list until autovacuum make the
planner fall back to sequential scans (observed at 20,000 places: roughly
80 ms sequential versus about 1 ms through a bitmap OR of both indexes). The
plan inspector fails if this storage parameter is missing. The legacy GraphQL
autocomplete field `placesNames` retains its existing shape but is
deterministically capped at 20; `placesStartwithName` retains prefix-only
semantics while applying the same normalization and cap. The search and
autocomplete surfaces therefore cannot fetch the complete place-name
collection. This guarantee covers search and autocomplete only. The legacy
public listing surfaces described below are not search surfaces and remain
unbounded.

### Search surfaces versus legacy public listings

The M4 exit criterion "search never retrieves the full place-name collection"
applies to the search and autocomplete surfaces. The legacy public listing
surfaces are a separate contract. They are unbounded but return only approved
`Place` rows, never submission `Request` rows. They return the same result to
guests and to every role, and they are read-only for guests and
non-administrators. Their shape and cardinality stay unchanged until the
frontend search migration
([smokemap-webapp#10](https://github.com/muxalko/smokemap-webapp/issues/10))
lands.

| Surface | Kind | Current behavior | Status |
| --- | --- | --- | --- |
| `GET /api/v1/places/search/` | Search | Ranked, at most 20 results | Supported |
| GraphQL `placesNames` | Autocomplete | First 20 names by lowercased name, then ID | Legacy, capped |
| GraphQL `placesStartwithName(name)` | Autocomplete | First 20 normalized prefix matches | Legacy, capped |
| GraphQL `placesByName(name)` | Lookup | First 20 exact-name matches by ID | Legacy, capped |
| GraphQL `placeById(id)` | Lookup | One `Place` | Supported |
| GraphQL `places` | Listing | Every approved `Place`, unbounded | Deprecated |
| `GET /places/` (optional `in_bbox`) | Listing | Every approved `Place` in the box, or every one without it; unbounded, unpaginated GeoJSON | Legacy |

The GraphQL schema exposes no mutation that writes `Place` directly. `/places/`
accepts writes only from an administrator. Guests receive HTTP 401 and
authenticated non-administrators, including moderators, receive HTTP 403.

The `places` field is deprecated in the schema, and its `deprecationReason`
points to `GET /api/v1/places/search/` for name search and
`GET /api/v1/places/` for viewport reads. The current frontend does not select
it. `places` and `/places/` can be bounded or removed only after the frontend
search migration has shipped and no supported client depends on them.
`placesNames` and `placesStartwithName` are formally deprecated under the same
condition. `backend/test_place_search.py` pins all of this behavior.

Inspect both natural query plans against a deterministic 20,000-place corpus:

```sh
docker compose --project-directory ../smokemap \
  --file ../smokemap/docker-compose.yaml \
  run --rm -T --build --volume "$PWD:/app" backend \
  python manage.py inspect_place_search_plans \
    --output-dir /app/place-search-plan-evidence
```

The DEBUG-only inspector leaves `enable_seqscan` on, never overrides a planner
setting, executes the real limited ORM search with `EXPLAIN (ANALYZE, BUFFERS,
FORMAT JSON)`, and fails unless the prefix plan uses a search index and the
fuzzy plan uses `place_name_lower_trgm_idx`. Its fixture is transactional and
rolled back; it refreshes table statistics after rollback. The optional output
contains the full plans, actual index names, planning/execution timings, and a
canonical text summary.

## Viewport place API

The public, read-only map contract is:

```text
GET /api/v1/places/?bbox=minx,miny,maxx,maxy&zoom=11&categories=1,2
```

`bbox` and `zoom` are required. Longitude is limited to `[-180, 180]`, latitude
to `[-90, 90]`, minimums must be less than maximums, and neither span may
exceed 10 degrees. `zoom` must be an integer from 0 through 22. `categories` is
an optional comma-separated list of at most 20 positive category IDs.

The endpoint queries approved `Place` rows through the indexed authoritative
`Address.location` geometry, includes points on the viewport boundary, and
returns at most 500 deterministically ordered GeoJSON features. A larger result
set returns `viewport_result_limit_exceeded` so clients can zoom in or select
categories instead of silently receiving partial data. Invalid input returns
`invalid_viewport`; both errors use HTTP 400 and a stable non-sensitive detail.

The response exposes only the map properties `place_id`, `name`, `category`,
`description`, `address`, `tags`, and `website`. It never includes submission
owners, reviewers, authentication state, or audit fields. The M2 performance
budget is at most two database queries, 500 features, 512 KiB of encoded
GeoJSON, and 250 ms server processing time for a representative city viewport.
The query-count contract is enforced in backend tests. The deterministic M2
exit harness below enforces the natural GiST plan, response size, and elapsed
time against a representative populated dataset.

### Deterministic viewport benchmark

Run the M2 exit harness from this backend checkout. The command uses the
Smokemap superproject in the sibling `../smokemap` directory for Compose, while
mounting the current backend worktree (rather than the superproject's pinned
backend submodule) into the one-off container:

```sh
docker compose --project-directory ../smokemap \
  --file ../smokemap/docker-compose.yaml \
  run --rm -T --build --volume "$PWD:/app" backend \
  python manage.py benchmark_viewport_places \
    --output-dir /app/viewport-benchmark-evidence
```

The harness exercises the real `GET /api/v1/places/` route in-process, including
middleware, rendering, and its two ORM queries. It creates a namespaced,
row-major 200 by 100 Washington, DC grid (20,000 synthetic places) in a
transaction. The fixed `-77.020,38.830,-76.980,38.870` viewport contains exactly
400 points. A reserved benchmark category is supplied through the public
`categories` query parameter, so unrelated pre-existing places in or around the
bounds remain untouched and cannot alter the measured response. Every warmup
and sample verifies that all 400 returned features belong to the benchmark
namespace. After three warmups, each of five samples must independently stay
within two queries, 500 features, 512 KiB of encoded GeoJSON, and 250 ms of
server processing.

Before planning, the harness analyzes the populated tables. It then runs the
endpoint's actual limited queryset through natural PostgreSQL `EXPLAIN (ANALYZE,
BUFFERS, FORMAT JSON)`, requires `enable_seqscan` to remain `on`, and verifies
that the plan names a real GiST index on `Address.location`. It never changes a
planner setting. The command prints canonical text evidence and, when
`--output-dir` is supplied, writes `viewport-places-benchmark.txt` and
`viewport-places-benchmark.json` with the dataset, full plan, per-sample query,
feature and byte counts, and timings.

Synthetic names, addresses, tags, and category use the reserved
`__sm79_vpbench_v1__` prefix. A pre-run cleanup refuses cross-namespace
references, and the command refuses to run unless Django `DEBUG` is enabled.
The benchmark inserts are rolled back even on failure, and table statistics are
refreshed after rollback so neither rows nor synthetic planner estimates remain.

## Local mock map data

With the workspace Docker Compose stack running, seed three deterministic fictional
places inside the frontend's default Washington, DC viewport:

```sh
docker compose exec -T backend python manage.py seed_mock_data
```

Run the command from the workspace root. It is development-only, performs no
external geocoding, and can be run repeatedly without creating duplicate places.
Refresh `http://localhost:3000` after seeding to see the markers.

## Authentication lifecycle

The GraphQL `tokenAuth`, `verifyToken`, `refreshToken` and `revokeToken`
mutations are the canonical authentication lifecycle. Access tokens are Bearer
JWTs with a five-minute lifetime and only `sub`, `type`, `iat` and `exp`
claims. The same access token authenticates protected GraphQL and REST calls.

Refresh tokens are opaque, live for at most seven days and are stored only as
SHA-256 digests. Every successful refresh atomically rotates the credential
without extending its family lifetime. Reusing a rotated credential marks the
family compromised and revokes every successor. `revokeToken` revokes the
whole family. Deploying this lifecycle deletes legacy third-party refresh
records, deliberately requiring existing sessions to sign in again.

Refresh and revoke accept `refreshToken` explicitly or from the
`JWT-refresh-token` cookie for trusted server callers. Missing, invalid,
expired and reused credentials return stable GraphQL codes:
`INVALID_REFRESH_TOKEN`, `REFRESH_TOKEN_EXPIRED` and
`REFRESH_TOKEN_REUSED`. Invalid access tokens return `INVALID_TOKEN`; generic
login failure returns `AUTHENTICATION_FAILED` without revealing whether an
account exists.

## Local moderation administrator

Moderation writes use the versioned GraphQL lifecycle mutations
`withdrawSubmissionV4`, `approveSubmissionV4`, and `rejectSubmissionV4`.
Each mutation requires an idempotency key. Withdrawal is limited to the active
owner of a draft or pending submission; approval and rejection require an
active moderator or administrator and ordinary review cannot target the
reviewer's own submission.

The services lock the submission aggregate and commit its state, authenticated
actor, reviewer metadata, and append-only lifecycle event together. Approval
also takes a transaction-scoped lock derived from the complete normalized place
name and rechecks nearby public places before materializing exactly one `Place`,
its legacy map-compatibility `Location`, any legacy unmanaged image links, and
opaque publication records for managed display renditions. Approval never
exposes a private bucket, key, storage URL, checksum, or provenance identity
through the public place API.
Rejection and withdrawal remove retained attachment rows and move their exact
owner-bound upload intents to `cleanup_pending`; the existing media cleanup job
deletes object-store data outside the lifecycle transaction. Exceptional hard
deletion remains administrator-only, leaves a standalone durable audit record,
and refuses deletion while any image metadata or managed media still requires
explicit cleanup. Approved requests and the places materialized from them cannot
be hard-deleted through compatibility APIs. Deleting an unreferenced legacy
place atomically removes only its denormalized `Location` row and database
record; moderation never infers authority to delete an object from an image URL.

Moderation reads use `moderationQueueV4(first, after)`. The query is limited to
active moderators and administrators, returns pending submissions oldest-first
with an ID tie-breaker, defaults to 20 rows, and rejects page sizes outside 1
through 50. `after` is an opaque, signed keyset cursor; clients should only pass
back cursors returned as `nextCursor` and continue only while `hasNextPage` is
true. The legacy `requestsToApprove` field remains available for small existing
clients but is capped at the first 50 rows in the same stable order.

Queue objects do not contain private attachment locations or storage metadata.
Their `attachments` field exposes only each retained managed attachment's ID and
display position. Review clients pass the ID to the separately authorized,
short-lived `mediaAttachmentPreviewV3` capability. Buckets, object keys, upload
intent IDs, hashes, credentials, and permanent URLs remain private.

Create or update a local-only administrator with an interactively entered password:

```sh
docker compose exec backend python manage.py create_local_admin \
  --email admin@smokemap.local
```

Run the command from the workspace root without `-T` so the password prompt can
read from the terminal. The password is not accepted as a command-line argument
or printed. The command refuses to run outside debug mode. After provisioning,
sign in at `http://localhost:3000/api/auth/signin` and open
`http://localhost:3000/requests` to review pending submissions.

For a repeatable login, submission, and moderation test cohort, run this
non-interactive development-only command instead:

```sh
docker compose exec -T backend python manage.py provision_local_test_users
```

It creates or updates `admin@smokemap.local`, `user-one@smokemap.local`, and
`user-two@smokemap.local`. All three use the local-only fallback password
`Smokemap-local-test-only-2026!`; override it without putting a password on the
command line by setting `SMOKEMAP_LOCAL_TEST_PASSWORD` in the backend container
environment. The command never prints the password and refuses to run when
`DEBUG` is disabled. Rerunning it restores the documented names, active state,
roles, group membership, and password without creating duplicates.

This example shows how to use Django 4 on Vercel with Serverless Functions using the [Python Runtime](https://vercel.com/docs/concepts/functions/serverless-functions/runtimes/python).

## Demo

https://django-template.vercel.app/

## How it Works

Our Django application, `example` is configured as an installed application in `smokemap/settings.py`:

```python
# smokemap/settings.py
INSTALLED_APPS = [
    # ...
    'backend',
]
```

We allow "\*.vercel.app" subdomains in `ALLOWED_HOSTS`, in addition to 127.0.0.1:

```python
# smokemap/settings.py
ALLOWED_HOSTS = ['127.0.0.1', '.vercel.app']
```

The `wsgi` module must use a public variable named `app` to expose the WSGI application:

```python
# smokemap/wsgi.py
app = get_wsgi_application()
```

The corresponding `WSGI_APPLICATION` setting is configured to use the `app` variable from the `smokemap.wsgi` module:

```python
# smokemap/settings.py
WSGI_APPLICATION = 'smokemap.wsgi.app'
```

There is a single view which renders the current time in `backend/views.py`:

```python
# backend/views.py
from datetime import datetime

from django.http import HttpResponse


def index(request):
    now = datetime.now()
    html = f'''
    <html>
        <body>
            <h1>Hello from Vercel!</h1>
            <p>The current time is { now }.</p>
        </body>
    </html>
    '''
    return HttpResponse(html)
```

This view is exposed a URL through `backend/urls.py`:

```python
# backend/urls.py
from django.urls import path

from backend.views import index


urlpatterns = [
    path('', index),
]
```

Finally, it's made accessible to the Django server inside `smokemap/urls.py`:

```python
# smokemap/urls.py
from django.urls import path, include

urlpatterns = [
    ...
    path('', include('backend.urls')),
]
```

This example uses the Web Server Gateway Interface (WSGI) with Django to enable handling requests on Vercel with Serverless Functions.

## Running Locally

```bash
python manage.py runserver
```

Your Django application is now available at `http://localhost:8000`.

## Running inside VM

```bash
SETTINGS_MODE='local' python manage.py runserver 0.0.0.0:8000

DEVELOPMENT MODE !!! - Hello from 75897
GDAL_LIBRARY_PATH=/usr/lib/libgdal.so.26
GEOS_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/libgeos_c.so.1
DEVELOPMENT MODE !!! - Hello from 75898
GDAL_LIBRARY_PATH=/usr/lib/libgdal.so.26
GEOS_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/libgeos_c.so.1
Watching for file changes with StatReloader
Performing system checks...

System check identified no issues (0 silenced).
January 24, 2024 - 20:58:47
Django version 4.2.8, using settings 'smokemap.settings'
Starting development server at http://0.0.0.0:8000/
Quit the server with CONTROL-C.

```

The environment variable ```SETTINGS_MODE``` is set to "local" to indicate development mode, used in settings.py 

## One-Click Deploy

Deploy the example using [Vercel](https://vercel.com?utm_source=github&utm_medium=readme&utm_campaign=vercel-examples):

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fvercel%2Fexamples%2Ftree%2Fmain%2Fpython%2Fdjango&demo-title=Django%20%2B%20Vercel&demo-description=Use%20Django%204%20on%20Vercel%20with%20Serverless%20Functions%20using%20the%20Python%20Runtime.&demo-url=https%3A%2F%2Fdjango-template.vercel.app%2F&demo-image=https://assets.vercel.com/image/upload/v1669994241/random/django.png)
