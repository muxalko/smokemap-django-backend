FROM python:3.12.16-slim-trixie@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libgdal36=3.10.3+dfsg-1 \
    && rm -rf /var/lib/apt/lists/*

FROM runtime AS dependencies

COPY docker/requirements-installer.txt docker/update_ensurepip.py ./docker/
RUN python -m pip download --only-binary=:all: --no-deps --require-hashes \
        --requirement docker/requirements-installer.txt --dest /tmp/installer \
    && python -m pip install --no-index --no-deps /tmp/installer/pip-26.2.2-py3-none-any.whl \
    && python docker/update_ensurepip.py /tmp/installer/pip-26.2.2-py3-none-any.whl \
    && rm -rf /tmp/installer

COPY requirements.txt ./
RUN python -m pip install --requirement requirements.txt
RUN python -m pip check

FROM dependencies AS development

RUN apt-get update \
    && apt-get install --yes --no-install-recommends gdal-bin=3.10.3+dfsg-1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-dev.txt ./
RUN python -m pip install --requirement requirements-dev.txt
RUN python -m pip check

ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/muxalko/smokemap-django-backend" \
      org.opencontainers.image.revision="${VCS_REF}"

COPY . .

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

FROM dependencies AS production

# Keep the validated build graph as evidence, then remove both installer copies.
RUN mkdir -p /usr/local/share/smokemap/dependencies \
    && python -m pip inspect > /usr/local/share/smokemap/dependencies/build-inspect.json \
    && python -m pip freeze --all > /usr/local/share/smokemap/dependencies/build-freeze.txt \
    && python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip

ARG VCS_REF=unknown

LABEL org.opencontainers.image.source="https://github.com/muxalko/smokemap-django-backend" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV SETTINGS_MODE=staging

RUN groupadd --system --gid 10001 smokemap \
    && useradd --system --uid 10001 --gid smokemap \
        --home-dir /app --shell /usr/sbin/nologin smokemap

COPY --chown=smokemap:smokemap . .

USER smokemap

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; host=os.environ['DJANGO_ALLOWED_HOSTS'].split(',')[0]; request=urllib.request.Request('http://127.0.0.1:8000/health/live/', headers={'Host': host, 'X-Forwarded-Proto': 'https'}); urllib.request.urlopen(request, timeout=2).read()"]

STOPSIGNAL SIGTERM

CMD ["gunicorn", "--bind=0.0.0.0:8000", "--workers=2", "--threads=4", "--timeout=60", "--access-logfile=-", "--error-logfile=-", "smokemap.wsgi:app"]
