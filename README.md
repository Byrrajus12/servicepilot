# ServicePilot

ServicePilot is a multi-tenant service advisor backend for automotive dealerships. Its FastAPI API uses a PostgreSQL-backed mock DMS to look up customers and vehicles, find service availability, and manage appointments. A development endpoint can receive simulated inbound SMS and store conversation history.

## Start with Docker Compose

Requires Docker with Compose. Copy `.env.example` to `.env` and set `POSTGRES_PASSWORD`. Compose uses this password for PostgreSQL, the API, and the test runner. The URLs in `.env.example` are for commands run on the host. Set `COMPOSE_DATABASE_URL` only if the containerized API should use a different database connection.

```sh
cp .env.example .env
# Edit .env before running the remaining commands.
docker compose up -d --wait db
docker compose run --rm --build api alembic upgrade head
docker compose run --rm api python -m servicepilot.seed
docker compose up -d api
```

The API is available at `http://localhost:8000`, with interactive OpenAPI documentation at `/docs`. The optional seed command can be rerun and creates two fictional dealerships with three customers, vehicles, and appointments each. Use it in development databases.

| Endpoint | Response |
| --- | --- |
| `GET /health` | 200 while the process is serving requests |
| `GET /ready` | 200 when PostgreSQL answers a query, or 503 when it is unavailable |

## Run the API locally

Install [uv](https://docs.astral.sh/uv/) and use Python 3.12 or later. Start PostgreSQL via `docker compose up -d --wait db` or supply a running local database in `DATABASE_URL`. For the bundled database, replace `<password>` in `DATABASE_URL` with the URL-encoded PostgreSQL password.

```sh
uv sync --locked --python 3.12
uv run alembic upgrade head
uv run python -m servicepilot.seed
uv run uvicorn servicepilot.main:app --reload
```

## Use the appointment API

Generate a different development API key for each dealership with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Set `DEALERSHIP_API_KEYS` in your local `.env` as a JSON object keyed by dealership slug. The development seed includes `cedar-ridge-motors` and `harbor-point-auto`. Replace the placeholder with your generated key before starting the API.

```dotenv
DEALERSHIP_API_KEYS='{"cedar-ridge-motors":"<your-generated-key>"}'
```

Send the key in the `X-API-Key` header on appointment and customer requests. The API derives the dealership from the key. The following command looks up a seeded customer using the Cedar Ridge key from `SERVICEPILOT_API_KEY` in your shell.

```sh
curl -G -H "X-API-Key: $SERVICEPILOT_API_KEY" \
  --data-urlencode "phone_number=+12065550101" http://localhost:8000/customers
```

| Method and path | Input and behavior |
| --- | --- |
| `GET /customers` | Exact `phone_number` in international format, including `+` |
| `GET /customers/{customer_id}/vehicles` | Vehicles owned by the customer |
| `GET /customers/{customer_id}/appointments` | Upcoming scheduled appointments |
| `GET /availability` | Dealership-local `date` (YYYY-MM-DD) and optional `vehicle_id` |
| `GET /appointments/{appointment_id}` | An appointment, including cancelled and completed records |
| `POST /appointments` | JSON with `customer_id`, `vehicle_id`, `scheduled_start`, and `service_type` |
| `PATCH /appointments/{appointment_id}` | JSON with the new `scheduled_start` |
| `POST /appointments/{appointment_id}/cancel` | Cancel an upcoming scheduled appointment |

Copy a slot's `start` into `scheduled_start` when booking or rescheduling. Availability includes the local UTC offset. Appointment responses use UTC. A missing or invalid key returns 401, an inaccessible record returns 404, an unavailable slot or invalid state transition returns 409, and an invalid time or request returns 422.

Configure a dealership's schedule in its `tenants` row. The defaults are `America/Detroit`, Monday through Friday, 08:00–17:00 local time, 60-minute appointments, and one concurrent appointment. Use an IANA timezone, `service_days` values from 0 (Monday) through 6 (Sunday), and one same-day window defined by `opens_at` and `closes_at`. Set `appointment_minutes` and `service_capacity` for the slot length and maximum simultaneous appointments. The same vehicle can have only one appointment at a time.

Slots follow the dealership's wall clock. If a clock change interrupts a slot's duration, that slot is unavailable.

Internal callers can use `AppointmentService(MockDMSAdapter(session, tenant_id))` directly with a trusted tenant ID and a transaction from `build_session_factory(engine).begin()`. Booking, rescheduling, and cancellation hold the dealership row lock until the transaction ends, so capacity checks and writes run in order for each dealership.

## Simulate inbound SMS

Set `ENABLE_SMS_SIMULATION=true` in the development `.env` and apply the latest migration. With Compose, run `docker compose up -d --build api` to recreate the API with the new setting. Use the same dealership API key configured above.

```sh
curl http://localhost:8000/simulations/sms \
  -H "X-API-Key: $SERVICEPILOT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"sender_phone":"+12065550101","body":"Can I book an oil change?","external_message_id":"demo-sms-001"}'
```

A successful request returns HTTP 200 with `conversation_id`, `message_id`, and `status: "persisted"`. This is a storage acknowledgement. Reusing the optional `external_message_id` with the same dealership returns the original IDs and `status: "duplicate"`. Use a fresh ID for each new message. Every request without an external ID represents a new message.

The route uses a fixed development recipient. Within a dealership, messages between the same sender and recipient reuse the open conversation. A single customer phone match links the customer, while zero or multiple matches keep the conversation unassigned. If a previously linked customer's match changes, the next inbound message opens a fresh conversation. Use this route with development data and leave the setting disabled in deployed environments.

## Run tests

Set `POSTGRES_PASSWORD` in `.env`, then run the test image. It creates `servicepilot_test` on the bundled PostgreSQL server if needed.

```sh
docker compose run --rm --build tests
```

To run pytest on the host, create a dedicated PostgreSQL test database, point `TEST_DATABASE_URL` at it, and run `uv run --locked pytest`. The test role needs permission to create schemas.

The suite applies Alembic migrations in a temporary schema and removes it at the end of the run. It uses independent committed transactions to verify concurrent bookings and inbound SMS delivery.

## Code layout

| Path | Contents |
| --- | --- |
| `servicepilot/` | FastAPI entry point, database setup, domain models, and development seed command |
| `servicepilot/appointments/` | Scheduling rules, appointment services, request schemas, and API routes |
| `servicepilot/conversations/` | Inbound SMS persistence, simulation endpoint, and typed conversation context |
| `servicepilot/integrations/` | DMS adapter interface and PostgreSQL-backed mock implementation |
| `alembic/` | PostgreSQL schema migrations |
| `tests/` | Health, appointment, conversation, concurrency, schema, and seed tests backed by PostgreSQL |
