# ServicePilot

ServicePilot is a multi-tenant service advisor backend for automotive dealerships. It uses FastAPI and PostgreSQL to model dealerships, customers, vehicles, appointments, and service conversations. The API exposes health and readiness endpoints.

## Start with Docker Compose

Requires Docker with Compose. Copy `.env.example` to `.env`, choose a local PostgreSQL password, and replace `<password>` in each URL. URL-encode reserved characters in the URLs.

```sh
cp .env.example .env
# Edit .env before running the remaining commands.
docker compose up -d --wait db
docker compose run --rm api alembic upgrade head
docker compose run --rm api python -m servicepilot.seed
docker compose up -d api
```

The API is available at `http://localhost:8000`.

| Endpoint | Response |
| --- | --- |
| `GET /health` | 200 while the process is serving requests |
| `GET /ready` | 200 when PostgreSQL answers a query, or 503 when it is unavailable |

## Run the API locally

Install [uv](https://docs.astral.sh/uv/) and use Python 3.12 or later. Start PostgreSQL via `docker compose up -d --wait db` or supply a running local database in `DATABASE_URL`.

```sh
uv sync --locked --python 3.12
uv run alembic upgrade head
uv run python -m servicepilot.seed
uv run uvicorn servicepilot.main:app --reload
```

Apply migrations before starting the API in a new environment. The optional seed command can be rerun and creates two fictional dealerships with three customers, vehicles, and appointments each. Use it in development databases.

## Run tests

Set `COMPOSE_TEST_DATABASE_URL` in `.env` to the test database on the Compose `db` service. With the bundled PostgreSQL service, create the test database once, then run the test image.

```sh
docker compose up -d --wait db
docker compose exec db createdb -U servicepilot servicepilot_test
docker compose run --rm --build tests
```

To run pytest on the host, point `TEST_DATABASE_URL` at a dedicated PostgreSQL database and run `uv run --locked pytest`. The test role needs permission to create schemas.

The tests apply Alembic migrations to a temporary schema, roll back test transactions, and remove the schema at the end of the run.

## Code layout

| Path | Contents |
| --- | --- |
| `servicepilot/` | FastAPI entry point, database setup, domain models, and development seed command |
| `alembic/` | PostgreSQL schema migrations |
| `tests/` | API, schema, and seed tests backed by PostgreSQL |
