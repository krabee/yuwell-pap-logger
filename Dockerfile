FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /srv

# Dependencies first so that editing application code does not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY app ./app
RUN uv sync --frozen --no-dev

# SQLite lives here; mounted as a volume in compose so accounts survive a rebuild.
RUN mkdir -p /srv/data

EXPOSE 8000

# One worker on purpose: imports write to SQLite, and a second process would only
# add write contention for a workload that is one upload a night.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
