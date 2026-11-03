# Backend dependency authority

The declared M5 backend baseline is Python 3.12.16 and Django 5.2.19 LTS.
Issue #121 advances the security patch from #119, which replaces the unsupported
Django 4.2 bridge. Runtime verification and deployment are separate gates; see [upgrade evidence](docs/DJANGO52_UPGRADE.md).

## Authoritative manifests

- `requirements.txt` is the only production/runtime dependency authority.
  The Dockerfile and legacy Vercel build script install it directly.
  Gunicorn is an exact-pinned runtime dependency because the Dockerfile's
  `production` target uses it as the WSGI process manager; development still
  uses Django's development server.
- `requirements-dev.txt` includes `requirements.txt` with pip's `-r` directive.
  Add development-only tools below that directive, with exact `==` pins. A
  package already declared by the runtime manifest must never be repeated or
  overridden there. CI installs this development manifest and runs `pip check`,
  exercising both supported requirements files together.
- Pipenv is not a supported install path. The former `Pipfile` and
  `Pipfile.lock` had no tracked caller, declared Python 3.9, and locked a
  dependency graph that disagreed with the runtime manifest, so they were
  retired instead of being regenerated or presented as a second authority.
  The exact locked `fiona` and `pandas` versions remain development
  dependencies because the tracked `generate_shp.py` utility imports them;
  unused Pipenv-only packages were not carried forward.

Use one of these commands from a clean Python 3.12 virtual environment:

```sh
# Runtime, deployment, or CI dependencies
python -m pip install --requirement requirements.txt

# Development and test dependencies (including the exact runtime set)
python -m pip install --requirement requirements-dev.txt
```

## Pin and lock strategy

Every package line committed to either requirements file uses an exact `==`
pin. `requirements.txt` is an input manifest, not a complete hash-verified
lock: pip may still resolve undeclared transitive dependencies required by its
entries. There is deliberately no second lockfile. Introducing a generated,
hash-verified lock is separate M5 work and must replace this strategy in one
reviewed change across Docker, CI, and documented install commands.

Dependency updates must be focused and intentional. Change the canonical
runtime manifest first, keep development-only additions separate, and verify
the result from a clean Python 3.12 environment with:

```sh
python -m pip install --requirement requirements-dev.txt
python -m pip check
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

Do not copy `pip freeze` output from an unrelated environment into either
manifest. Further framework/runtime migrations follow the staged M5 upgrade plan.
The container installer is separately hash-pinned in
`docker/requirements-installer.txt`. It does not replace either application
manifest. Both image builds use pip 26.2.2 and run `pip check`. Development also
replaces CPython's bundled pip wheel, so offline `venv` creation cannot restore
pip 25.0.1. Production captures the validated build graph, then removes pip and
ensurepip entirely; install dependencies at build time, never in a running
production container.

CI exports the build graph using `pip inspect` and `pip freeze --all`, plus
separate inventories of the actual final production/development distributions
and Debian packages. The production build graph includes the subsequently
removed installer; `production-runtime.json` records the final set. These
artifacts describe that build, not a hash lock. See
[the native and installer contract](docs/NATIVE_RUNTIME.md) for verification.
