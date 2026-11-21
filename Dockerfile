FROM python:3.12-slim AS base
RUN python -m pip install --no-cache-dir --only-binary=:all: uv==0.11.29

WORKDIR /app
ENV PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_CACHE_DIR=/tmp/uv-cache PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-cache
COPY servicepilot ./servicepilot
COPY alembic.ini ./
COPY alembic ./alembic

RUN useradd --system --uid 10001 servicepilot

FROM base AS test
RUN uv sync --locked --no-cache
COPY tests ./tests
USER servicepilot
CMD ["python", "-m", "pytest", "-o", "cache_dir=/tmp/pytest-cache", "tests"]

FROM base AS production
USER servicepilot
EXPOSE 8000
CMD ["uvicorn", "servicepilot.main:app", "--host", "0.0.0.0", "--port", "8000"]
