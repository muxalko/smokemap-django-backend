# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libgdal32 \
    && rm -rf /var/lib/apt/lists/*

FROM runtime AS dependencies

COPY requirements.txt ./
RUN python -m pip install --requirement requirements.txt

FROM dependencies AS development

RUN apt-get update \
    && apt-get install --yes --no-install-recommends binutils gdal-bin libproj-dev \
    && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

FROM dependencies AS production

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
