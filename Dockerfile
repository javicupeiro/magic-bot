FROM ghcr.io/astral-sh/uv:0.8.2 AS uv
FROM python:3.12-slim-bookworm

COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY backend/ backend/
COPY frontend/ frontend/
COPY README.md ./
RUN uv sync --locked --no-dev --no-editable \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p data/game_rules data/processed data/chroma data/conversations \
    && chown -R app:app data

# Empty named volumes inherit these writable directories on first mount.
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health', timeout=2)"
# Locks are process-local; multiple workers require a different storage/lock design.
CMD ["uvicorn", "backend.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
