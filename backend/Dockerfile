# QueueForge backend image: one image, three roles (api, worker, scheduler) chosen by command.
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml ./
RUN mkdir queueforge && touch queueforge/__init__.py \
    && pip install . \
    && rm -rf queueforge

COPY alembic.ini ./
COPY alembic ./alembic
COPY queueforge ./queueforge
# Editable install keeps the package at /app so `queueforge migrate` finds alembic.ini.
RUN pip install --no-deps -e . \
    && useradd --system --uid 10001 --home /app queueforge \
    && chown -R queueforge /app

USER queueforge
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)" || exit 1

# Default role: API. Override with `queueforge worker` or `queueforge scheduler`.
CMD ["queueforge", "api", "--host", "0.0.0.0", "--port", "8000"]
