# Backend dependency authority

The backend currently runs on the M5 bridge baseline of Python 3.12 and
Django 4.2.22. This document defines how dependency declarations work on that
baseline; it does not authorize a package, framework, or runtime upgrade.

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
manifest. Framework/runtime migrations follow the staged M5 upgrade plan and
must not be folded into routine manifest maintenance.
