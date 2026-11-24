# ServicePilot

ServicePilot is a multi-tenant service advisor backend for automotive dealerships. Its FastAPI API uses a PostgreSQL-backed mock DMS to look up customers and vehicles, find service availability, and manage appointments. An independent PostgreSQL-backed worker processes incoming SMS and sends replies through the simulator or Twilio. The advisor can propose appointment changes and act on a subsequent customer confirmation.

## Start with Docker Compose

Requires Docker with Compose. Copy `.env.example` to `.env` and set `POSTGRES_PASSWORD`. Compose uses this password for PostgreSQL, the API, and the test runner. The URLs in `.env.example` are for commands run on the host. Set `COMPOSE_DATABASE_URL` only if the containerized API should use a different database connection.

```sh
cp .env.example .env
# Edit .env before running the remaining commands.
docker compose up -d --build
docker compose exec api python -m servicepilot.seed
```

The API is available at `http://localhost:8000`, with interactive OpenAPI documentation at `/docs`. A one-shot Compose service applies migrations before the API and worker start. The optional seed command can be rerun and creates two fictional dealerships with three customers, vehicles, and appointments each. Use it in development databases.

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
# In a separate terminal with the same settings:
uv run python -m servicepilot.worker
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

## Simulate an SMS conversation

Set `ENABLE_SMS_SIMULATION=true`, `SMS_TRANSPORT=simulation`, `OPENAI_API_KEY` and a Cedar Ridge API key in `.env`. Use seeded fictional customers when calling the OpenAI API. Start or apply configuration changes to both the API and worker.

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Edit .env with POSTGRES_PASSWORD, OPENAI_API_KEY and DEALERSHIP_API_KEYS.
docker compose up -d --build
docker compose exec api python -m servicepilot.seed
Invoke-RestMethod http://localhost:8000/ready
$env:SERVICEPILOT_API_KEY = '<your Cedar Ridge API key>'
.\scripts\Simulate-Sms.ps1
```

Enter one customer message per prompt. Start with “When is my next service appointment?” or “Can you cancel my oil change?” If the advisor proposes a change, answer in ordinary language, such as “Yeah, go ahead.” The script waits for simulated delivery on each turn. Use `-Sender` to select another development number or `-Text @('What vehicles do I have?', 'When is my next appointment?')` to supply a sequence.

| Endpoint | Purpose |
| --- | --- |
| `POST /simulations/sms` | Submit `sender_phone`, `body`, and an optional `external_message_id`. The receipt gives the conversation and inbound message IDs plus the processing disposition. Reuse the external ID only when retrying the same inbound request. |
| `GET /simulations/sms/{conversation_id}/messages/{inbound_id}/agent` | Poll the execution, including attempts, next retry, error code, `confirmation_attempts` and the saved `reply`. A failed run with a retry time is still queued for another attempt. |
| `GET /simulations/sms/{conversation_id}/messages/{reply_id}/delivery` | Inspect the delivery record using the outbound reply ID. |
| `GET /simulations/sms/{conversation_id}/messages` | Inspect conversation history. |
| `GET /simulations/sms/{conversation_id}/appointment-actions` | Inspect the active proposal and durable appointment results. |
| `POST /simulations/sms/{conversation_id}/messages/{inbound_id}/agent` | Optional diagnostic execution for a simulated message. It can advance a scheduled retry within the attempt cap and uses the same locks and delivery path as the worker. |

All simulation endpoints require `X-API-Key`. Simulated messages always reply through the simulation transport, including when the worker is also configured for Twilio. `STOP` suppresses AI replies and invalidates the pending proposal for that sender and line. `START` re-enables new turns. Consent commands are persisted and skipped by the agent. SMS requests are capped at 16 KiB and message text at 1,600 characters.

## Connect a Twilio number

Use a Twilio SMS-capable number and an HTTPS reverse proxy or development tunnel. Switch on real SMS only for numbers and customer data you have authorization to use. Forward only the two webhook routes publicly and leave `ENABLE_SMS_SIMULATION=false` for deployed environments.

```dotenv
SMS_TRANSPORT=twilio
TWILIO_ACCOUNT_SID=AC<account SID suffix>
TWILIO_AUTH_TOKEN=<primary Twilio Auth Token>
TWILIO_NUMBER_ROUTES='{"+15555550123":"cedar-ridge-motors","+15555550124":"harbor-point-auto"}'
SMS_PUBLIC_WEBHOOK_BASE_URL=https://sms.example.com
```

1. Map each receiving number to an existing dealership slug. A dealership can have multiple lines. The worker sends the reply using the original receiving number and checks that the mapping still belongs to the same dealership.
2. Set the number’s incoming message webhook to HTTP `POST https://sms.example.com/webhooks/twilio/sms` in Twilio. Keep the exact public URL, including any port or path prefix. Omit the query string and trailing slash. If a prefix is configured, the proxy must strip it before forwarding to FastAPI.
3. Forward HTTP `POST /webhooks/twilio/status/{delivery_id}` from the same public base. ServicePilot attaches the per-message callback URL to each Twilio send, so there is no fixed status callback URL to enter in the Console.
4. Restart the API and worker with `docker compose up -d --build`. Text the Twilio number from a test handset and check the worker logs and Twilio Message logs for acceptance and carrier delivery.

Inbound requests and callbacks use Twilio's form-signing algorithm and the configured HTTPS URL. Forwarded host headers cannot select the signing URL. A signed request's account and receiving-number mapping determine its scope. Duplicate inbound SIDs reuse the original message within the account and dealership. MMS and messages from configured dealership numbers receive an empty acknowledgment. Outside an active appointment proposal, a rapid exact repeat of recently answered text is stored without another reply to reduce autoresponder loops. With Twilio Advanced Opt-Out, signed `OptOutType` values control local suppression and Twilio sends its configured keyword reply. The local fallback recognizes common full-message keywords. A lone `YES` remains an appointment reply unless the sender is suppressed or Twilio explicitly identifies the message as opt-in. [Twilio webhook security](https://www.twilio.com/docs/usage/security)

## Worker and delivery behavior

The API commits the inbound message and its execution together. Worker instances claim the oldest unfinished turn in each conversation by locking the conversation row and skipping rows another worker holds. The agent runs outside the claim transaction. An attempt owns a time-limited lease, and stale attempts cannot commit an appointment decision or publish a reply. Transient agent failures back off up to the attempt limit. A terminal failure releases later turns. When the model succeeds, the reply and its outbox entry commit together.

Outbox claims are leased separately. Before claiming a reply, the worker rechecks the phone number's customer association and fails the delivery if it changed. The worker sends messages for a thread in submission order and releases the database transaction before contacting Twilio. Safe retries include connection establishment failures and HTTP 429. A Twilio acceptance, a carrier submission and a carrier delivery are recorded separately. Signed callbacks advance progress and the first terminal status is retained. A carrier may deliver messages out of order.

Twilio provides no de-duplication guarantee for this create request. If an in-flight send times out, loses its response, or its worker exits before persisting the receipt, the delivery becomes `uncertain` and is held without another send. A later signed callback can reconcile it. If no callback arrives, inspect Twilio's Message logs before contacting the customer again. An expired simulation lease can be retried because simulation sends are local and deterministic.

Appointment confirmations require a delivery receipt for the exact prompt recorded before the inbound confirmation arrived. A missing or delayed receipt retires the proposal and requires a fresh request. This also applies to prompts created before the delivery migration. Proposals expire after 15 minutes. An unresolved or out-of-order older Twilio prompt can block confirmation until its delivery is resolved. Appointment results commit transactionally with the underlying change, so the same confirmation cannot execute the appointment change twice after a retry or crash.

The development identity policy associates an SMS sender with a customer only when the tenant has a unique match for the phone number. SMS addresses and delivery receipts do not establish who currently controls the phone or who read a prompt. Twilio inbound webhooks also cannot establish the original order of a carrier-delayed SMS and a normal SMS reply is unthreaded. Use appropriately verified pilot contacts and add customer verification before using the system with general customer data. The current Twilio integration uses a single account across configured lines. Local consent is scoped to the tenant, account, customer number and dealership line. Configure any wider provider consent policy in Twilio. A STOP cannot recall a message already submitted to Twilio.

### Configuration and diagnostics

| Setting | Purpose |
| --- | --- |
| `OPENAI_API_KEY`, `AGENT_MODEL`, `AGENT_TIMEOUT_SECONDS`, `AGENT_MAX_TURNS` | Model access and execution limits. The default model is `gpt-4.1-mini`. |
| `AGENT_MAX_ATTEMPTS`, `SMS_DELIVERY_MAX_ATTEMPTS` | Agent and safe outbound attempt caps. Defaults are 3 and 5. |
| `WORKER_POLL_SECONDS`, `WORKER_CONCURRENCY`, `WORKER_RETRY_BASE_SECONDS` | Idle poll interval, concurrent agent slots per process, and initial retry delay. Defaults are 1 second, 4 slots and 2 seconds. The worker also runs up to two delivery slots so slow model calls leave capacity for sends. |
| `SMS_MESSAGES_PER_MINUTE` | Agent work per conversation per minute. The default is 12. Over-limit messages are stored without scheduling work. |
| `ENABLE_SMS_SIMULATION`, `DEALERSHIP_API_KEYS` | Enables the development routes and gives each tenant a distinct key. |
| `SMS_TRANSPORT`, `TWILIO_NUMBER_ROUTES`, `SMS_PUBLIC_WEBHOOK_BASE_URL`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Enables signed Twilio ingress, outbound sender routing and callbacks. |

```powershell
docker compose ps -a
docker compose logs --tail 100 migrate worker api
docker compose exec worker python -m servicepilot.worker --health
docker compose exec db psql -U servicepilot -d servicepilot -c "SELECT provider, status, count(*) FROM sms_deliveries GROUP BY provider, status;"
docker compose exec db psql -U servicepilot -d servicepilot -c "SELECT status, error_code, count(*) FROM agent_executions GROUP BY status, error_code;"
```

For appointment confirmation, `agent_executions.confirmation_attempts` records each attempt against the inbound
message (`agent_executions.id`), proposal and original prompt. It records the model decision or interpretation
failure, interpreter invocation count, and the backend authorization and action outcome. A `pending` entry can
remain if the worker stops during the model call. A recovery attempt can reuse a stored action result or resume
preparing a revision after its original proposal was retired.

An explicit `clarify` repeats the confirmation question when the original proposal is still eligible. Missing,
invalid, duplicate, or failed tool decisions use the configured retry limit and publish no reply on failure.
Model refusals end the execution. The diagnostic entries omit SMS bodies and model output.

The worker's heartbeat is updated as it successfully polls or processes database work. For production operations, an alert on old pending work, prolonged `accepted` or `sent` states and `uncertain` outcomes helps identify stalled delivery or missing callbacks. Apply your carrier registration, retention and proxy rate-limit policies before opening the service to customers.

## Run tests

Set `POSTGRES_PASSWORD` in `.env`, then run the test image. It creates `servicepilot_test` on the bundled PostgreSQL server if needed.

```sh
docker compose run --rm --build tests
```

To run pytest on the host, create a dedicated PostgreSQL test database, point `TEST_DATABASE_URL` at it, and run `uv run --locked pytest`. The test role needs permission to create schemas.

The suite applies Alembic migrations in a temporary schema and removes it at the end of the run. It uses independent committed transactions to verify concurrent bookings, inbound SMS delivery, and agent execution. Agent tests stub the model and require no OpenAI key.

Run the optional semantic smoke test with `uv run --locked python -m scripts.evaluate_confirmation`. It uses
`OPENAI_API_KEY` from the environment or `.env` to send synthetic cases to `gpt-4.1-mini` and needs no database.

## Code layout

| Path | Contents |
| --- | --- |
| `servicepilot/` | FastAPI entry point, database setup, domain models, and development seed command |
| `servicepilot/appointments/` | Scheduling rules, appointment services, request schemas, and API routes |
| `servicepilot/conversations/` | Inbound SMS persistence, simulation endpoint, and typed conversation context |
| `servicepilot/agent/` | Service Advisor instructions, read and proposal tools, and PostgreSQL-backed execution |
| `servicepilot/sms/` | Consent, Twilio webhooks, delivery outbox and SMS transports |
| `servicepilot/worker.py` | Independent worker for agent execution and outbound delivery |
| `servicepilot/integrations/` | DMS adapter interface and PostgreSQL-backed mock implementation |
| `alembic/` | PostgreSQL schema migrations |
| `tests/` | Health, appointment, conversation, concurrency, schema, and seed tests backed by PostgreSQL |
