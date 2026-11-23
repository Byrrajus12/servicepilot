# ServicePilot

ServicePilot is a multi-tenant service advisor backend for automotive dealerships. Its FastAPI API uses a PostgreSQL-backed mock DMS to look up customers and vehicles, find service availability, and manage appointments. The development SMS simulator stores conversation history and lets an AI service advisor propose and confirm appointment changes.

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

## Simulate SMS with the Service Advisor

Set `ENABLE_SMS_SIMULATION=true` and `OPENAI_API_KEY` in the development `.env` and apply the latest migration. SMS storage and history work without an OpenAI key. Processing a reply uses the OpenAI Responses API and sends a bounded conversation snapshot, so use fictional customer data for simulation. With Compose, run `docker compose up -d --build api` after editing `.env`. When running Uvicorn on the host, restart it to load the new settings.

| Setting | Default | Purpose |
| --- | --- | --- |
| `AGENT_MODEL` | `gpt-4.1-mini` | OpenAI model with Responses API and function tool support |
| `AGENT_TIMEOUT_SECONDS` | `30` | Total time budget in seconds for preparing context and running the advisor |
| `AGENT_MAX_TURNS` | `5` | Maximum model turns per run, including tool rounds |

Use the dealership API key configured above in all simulation requests.

```sh
curl http://localhost:8000/simulations/sms \
  -H "X-API-Key: $SERVICEPILOT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"sender_phone":"+12065550101","body":"When is my next service appointment?","external_message_id":"demo-sms-001"}'
```

A successful request returns HTTP 200 with `conversation_id`, `message_id`, and `status: "persisted"`. It queues the inbound SMS for explicit processing. Reusing the optional `external_message_id` with the same dealership returns the original IDs and `status: "duplicate"`. Use a fresh ID for each new message. Every request without an external ID represents a new message.

Set `CONVERSATION_ID` and `MESSAGE_ID` to the IDs from the response, then run the following from a shell. The same requests are available in the interactive API docs at `http://localhost:8000/docs`.

```sh
THREAD="http://localhost:8000/simulations/sms/$CONVERSATION_ID/messages"
RUN="$THREAD/$MESSAGE_ID/agent"
curl -X POST -H "X-API-Key: $SERVICEPILOT_API_KEY" "$RUN"
curl -H "X-API-Key: $SERVICEPILOT_API_KEY" "$RUN"
curl -H "X-API-Key: $SERVICEPILOT_API_KEY" "$THREAD?limit=20"
```

The processing request returns the execution status and a completed reply under `reply.body`. The status response includes the model, attempts, lease expiration, duration, token usage when available, tool names and outcomes, and a redacted error code. Run the processing request again to retry a failed or expired execution. A completed execution returns its saved reply on subsequent requests.

Each conversation processes its queued messages in arrival order. If processing a later message returns HTTP 409 with `previous_message_pending`, process the earlier message identified in that response first. Conversations created before agent support retain their history. Only newly received simulation messages are queued.

The route uses a fixed development recipient. Within a dealership, messages between the same sender and recipient reuse the open conversation. A single customer phone match links the customer, while zero or multiple matches keep the conversation unassigned. If a previously linked customer's match changes, the next inbound message opens a fresh conversation. Use this route with development data and leave the setting disabled in deployed environments.

### Confirm a simulated appointment change

The simulation trusts the authenticated dealership integration to supply the sender number. Appointment changes require that number to match exactly one current customer in that dealership and are limited to that customer's records. Names, VINs, and customer IDs in SMS text do not establish identity. This trust policy is for development. Production deployments require stronger sender verification and abuse prevention.

The advisor can propose a booking, reschedule, or cancellation. Each proposal specifies the appointment and the exact dealership-local time, and expires after 15 minutes. Reply in ordinary language to confirm or decline. A changed request invalidates the prior proposal and requires its own confirmation. The backend checks availability again before booking or rescheduling.

> **Customer** · Can you move my oil change to Friday at 2?
>
> **Advisor** · Should I move the oil change for your 2022 Ford Escape at Cedar Ridge Motors from Thu, Dec 3, 2026 at 10:00 AM EST to Fri, Dec 4, 2026 at 2:00 PM EST?
>
> **Customer** · Yeah, go ahead.
>
> **Advisor** · Your oil change at Cedar Ridge Motors is moved to Fri, Dec 4, 2026 at 2:00 PM EST.

Use `GET /simulations/sms/{conversation_id}/appointment-actions` alongside the execution and message history endpoints to inspect the active proposal and durable results. Ordinary SMS replies are unthreaded. The advisor interprets them against the latest prompt and asks for clarification when a response appears to refer to an older proposal.

With a seeded development database and the Cedar Ridge key, the following PowerShell session books, moves, and cancels a tire rotation for the seeded Escape. It selects actual open times and reads back the corresponding appointment after each write.

```powershell
$api = 'http://localhost:8000'
$headers = @{ 'X-API-Key' = $env:SERVICEPILOT_API_KEY }
$phone = '+12065550101'
$encodedPhone = [uri]::EscapeDataString($phone)
$customer = @(Invoke-RestMethod "$api/customers?phone_number=$encodedPhone" -Headers $headers)[0]
$vehicle = @(Invoke-RestMethod "$api/customers/$($customer.id)/vehicles" -Headers $headers)[0]

function Send-SimSms([string]$Text) {
    $body = @{sender_phone=$phone; body=$Text; external_message_id=[guid]::NewGuid().ToString()} | ConvertTo-Json
    $receipt = Invoke-RestMethod "$api/simulations/sms" -Method Post -Headers $headers -ContentType 'application/json' -Body $body
    $run = Invoke-RestMethod "$api/simulations/sms/$($receipt.conversation_id)/messages/$($receipt.message_id)/agent" -Method Post -Headers $headers
    if ($run.status -ne 'completed') { throw ($run | ConvertTo-Json -Depth 5) }
    Write-Host "Customer: $Text"
    Write-Host "Advisor: $($run.reply.body)"
    $run
}

$slots = @()
for ($days = 7; $days -le 21 -and $slots.Count -lt 2; $days++) {
    $day = (Get-Date).AddDays($days).ToString('yyyy-MM-dd')
    $slots += Invoke-RestMethod "$api/availability?date=$day&vehicle_id=$($vehicle.id)" -Headers $headers
}
if ($slots.Count -lt 2) { throw 'Two available slots are required for this demo.' }
$first = ([DateTimeOffset]$slots[0].start).ToString('yyyy-MM-ddTHH:mm:sszzz')
$second = ([DateTimeOffset]$slots[1].start).ToString('yyyy-MM-ddTHH:mm:sszzz')

$book = Send-SimSms "Please book a tire rotation for my Ford Escape at Cedar Ridge Motors at $first."
$thread = "$api/simulations/sms/$($book.conversation_id)"
$booked = Send-SimSms 'Book it.'
$state = Invoke-RestMethod "$thread/appointment-actions" -Headers $headers
$appointmentId = ($state.results | Where-Object decision_message_id -eq $booked.id).appointment_id
if (-not $appointmentId) { throw 'Booking did not succeed.' }
Invoke-RestMethod "$api/appointments/$appointmentId" -Headers $headers

$move = Send-SimSms "Move the tire rotation we just booked to $second."
$moved = Send-SimSms 'Yeah, go ahead.'
Invoke-RestMethod "$api/appointments/$appointmentId" -Headers $headers

$cancel = Send-SimSms 'Cancel the tire rotation we just moved.'
$cancelled = Send-SimSms 'Yes, please cancel it.'
Invoke-RestMethod "$api/appointments/$appointmentId" -Headers $headers
Invoke-RestMethod "$thread/appointment-actions" -Headers $headers | ConvertTo-Json -Depth 8
Invoke-RestMethod "$thread/messages" -Headers $headers
@($book, $booked, $move, $moved, $cancel, $cancelled) | Select-Object id, status, tool_calls
```

## Run tests

Set `POSTGRES_PASSWORD` in `.env`, then run the test image. It creates `servicepilot_test` on the bundled PostgreSQL server if needed.

```sh
docker compose run --rm --build tests
```

To run pytest on the host, create a dedicated PostgreSQL test database, point `TEST_DATABASE_URL` at it, and run `uv run --locked pytest`. The test role needs permission to create schemas.

The suite applies Alembic migrations in a temporary schema and removes it at the end of the run. It uses independent committed transactions to verify concurrent bookings, inbound SMS delivery, and agent execution. Agent tests stub the model and require no OpenAI key.

## Code layout

| Path | Contents |
| --- | --- |
| `servicepilot/` | FastAPI entry point, database setup, domain models, and development seed command |
| `servicepilot/appointments/` | Scheduling rules, appointment services, request schemas, and API routes |
| `servicepilot/conversations/` | Inbound SMS persistence, simulation endpoint, and typed conversation context |
| `servicepilot/agent/` | Service Advisor instructions, read and proposal tools, and PostgreSQL-backed execution |
| `servicepilot/integrations/` | DMS adapter interface and PostgreSQL-backed mock implementation |
| `alembic/` | PostgreSQL schema migrations |
| `tests/` | Health, appointment, conversation, concurrency, schema, and seed tests backed by PostgreSQL |
