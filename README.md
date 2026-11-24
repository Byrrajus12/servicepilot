# ServicePilot

ServicePilot is a multi-tenant service advisor backend for automotive dealerships. Its FastAPI API uses a PostgreSQL-backed mock DMS to look up customers and vehicles, find service availability, and manage appointments. An independent PostgreSQL-backed worker processes incoming SMS and sends replies through the simulator or Twilio. The advisor can propose appointment changes and act on a subsequent customer confirmation.

Phase 4 adds a dealership service catalog and policies, semantic confirmation and conditional requests,
staff consultations with asynchronous customer follow-up, and human takeover and handback. The advisor
defaults to `gpt-6-luna` with low reasoning effort. The model interprets and writes the conversation.
The backend authorizes operations and owns appointment, consultation, ownership, and delivery state.

## Start with Docker Compose

Requires Docker with Compose. Copy `.env.example` to `.env` and set `POSTGRES_PASSWORD`. Compose uses this password for PostgreSQL, the API, and the test runner. The URLs in `.env.example` are for commands run on the host. Set `COMPOSE_DATABASE_URL` only if the containerized API should use a different database connection.

```sh
cp .env.example .env
# Edit .env before running the remaining commands.
docker compose up -d --build
docker compose exec api python -m servicepilot.seed
```

The API is available at `http://localhost:8000`, with interactive OpenAPI documentation at `/docs`. A one-shot Compose service applies migrations before the API and worker start. The optional seed command creates fictional dealerships with sample services, customers, vehicles, and appointments. To populate an empty catalog for previously seeded demo data, run `docker compose exec api python -m servicepilot.seed --backfill-service-catalog`. The command checks the synthetic customer identity and preserves configured service settings.

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

In PowerShell 7, enter one customer message per prompt. Start with "When is my next service appointment?" or "Can you cancel my oil change?" If the advisor proposes a change, answer in ordinary language. The script waits for simulated delivery on each turn unless staff owns the conversation. Use `/refresh` to check once, `/watch` to poll for 60 seconds, or `/history` to display past delivered outbound SMS. Use `-WatchSeconds` to adjust the watch window. At startup the simulator records the messages already delivered. It prints subsequent deliveries in order, once per session outside explicit history, and labels human SMS and AI follow-ups. The inbox also shows ownership, staff wait state, and delivery status. Use `-Text @('What vehicles do I have?', 'When is my next appointment?')` for a sequence.

For a fresh manual test, create a fictional customer and vehicle in the seeded demo dealership and use its number with the matching dealership API key. This uses a reserved fictional number on the local simulation transport. The existing demo records and appointments stay intact.

```powershell
$sender = docker compose exec -T api python -m servicepilot.seed --new-simulation-customer cedar-ridge-motors
if ($LASTEXITCODE -eq 0) { .\scripts\Simulate-Sms.ps1 -Sender $sender }
```

| Endpoint | Purpose |
| --- | --- |
| `POST /simulations/sms` | Submit `sender_phone`, `body`, and an optional `external_message_id`. The receipt gives the conversation and inbound message IDs plus the processing disposition. Reuse the external ID only when retrying the same inbound request. |
| `GET /simulations/sms?sender_phone=...` | Read the open simulated thread's ownership, staff wait state, and up to 50 recent outbound delivery records. Delivered rows include SMS text and delivery time. The interactive simulator uses `include_all=true` to follow delivery changes outside the recent window. |
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
| `OPENAI_API_KEY`, `AGENT_MODEL`, `AGENT_TIMEOUT_SECONDS`, `AGENT_MAX_TURNS` | Model access and execution limits. The default is `gpt-6-luna` with a 45-second execution timeout and seven SDK turns. |
| `AGENT_REASONING_EFFORT`, `AGENT_MAX_OUTPUT_TOKENS` | Optional Responses API overrides. Luna defaults to low effort. Leave effort empty for GPT-4.1 mini. The per-response budget is 2,048 tokens with reasoning enabled and 512 otherwise. Reasoning shares the output budget. |
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

Direct informal assent can authorize an eligible proposal without an explicit reference. An explicit
reference names the exact proposed change in the current SMS and is required after an unrelated exchange.
An explicit `clarify` lets the advisor ask a focused question or acknowledge hesitation. The canonical
question is the fallback. Subsequent consent must follow delivery of the clarification, which keeps
the same proposal and expiration. Hesitation leaves the proposal pending. Missing,
invalid, duplicate, or failed tool decisions use the configured retry limit and publish no reply on failure.
Model refusals end the execution. The diagnostic entries omit SMS bodies and model output.

Conditional agreement and substantive questions retire the original proposal. A conditional request keeps
its intended change and requirement independently of recent SMS history. Configured policy or a linked staff
answer can resolve it. A later appointment change requires a new, delivered proposal and fresh confirmation.
An unrelated answer leaves the proposal active until it expires. A subsequent affirmation must refer clearly
to the proposed change. The advisor writes ordinary replies and can include a verified informational answer
alongside the worker's exact confirmation question. The worker publishes authoritative appointment receipts.

The worker's heartbeat is updated as it successfully polls or processes database work. For production operations, an alert on old pending work, prolonged `accepted` or `sent` states and `uncertain` outcomes helps identify stalled delivery or missing callbacks. Apply your carrier registration, retention and proxy rate-limit policies before opening the service to customers.

## Dealership staff and consultations

Apply the database migrations and provision a manager from a trusted shell with database access. The command
prompts for the password and leaves an existing account intact.

```powershell
docker compose up -d --build
docker compose exec api python -m servicepilot.staff.bootstrap cedar-ridge-motors manager "Service Manager"
./scripts/Simulate-Staff.ps1 -Dealership cedar-ridge-motors -Username manager
```

Staff sign in with a dealership slug, username and password. Use their bearer token over HTTPS when
accessing the API remotely. Tokens expire after 12 hours. Signing out revokes the current token and disabling
an account revokes all its sessions. Managers can create or disable accounts, assign anyone in their
dealership, and configure service information. Advisors can read their dealership's inbox and work on
unassigned or self-assigned conversations. Neither role derives from a dealership API key.

| Endpoint under `/staff` | Purpose |
| --- | --- |
| `POST /sessions`, `DELETE /sessions/current`, `GET /me` | Authenticate, sign out and check identity. |
| `GET /employees`, `POST /employees`, `PATCH /employees/{id}` | Discover employees, create an account and set its disabled state. |
| `GET /conversations` | Filter the inbox by `owner`, `assigned_employee_id` or `waiting`. |
| `GET /conversations/{id}`, `GET /conversations/{id}/messages` | View thread state, customer and appointment context, and SMS history. |
| `PUT /conversations/{id}/assignment` | Set `employee_id` independently of ownership. |
| `PUT /conversations/{id}/ownership/{human\|ai}` | Take over or hand back an open thread. Submit its current `expected_version`. A competing update can return 409. |
| `GET /conversations/{id}/ownership-events` | View audited ownership and assignment changes. |
| `GET/POST /conversations/{id}/notes` | Read or add private notes. Supply an optional `reply_to_id` for an internal reply. |
| `GET /consultations` | Filter staff consultations by `conversation_id` and `status`. |
| `PUT /conversations/{id}/consultations/{consultation_id}/answer` | Submit a `public_fact` explicitly approved for customer use. A retry with the same author and answer returns the existing result. |
| `PUT /conversations/{id}/consultations/{consultation_id}/cancel` | Cancel an open request or a staff answer the advisor has yet to use. |
| `POST /conversations/{id}/replies` | Send a customer SMS while staff owns the thread. Supply `body` and a UUID `request_id`. Reuse the UUID when retrying the same SMS. |
| `PUT /service-settings` | Replace configured services, policies, and local closure dates. Manager only. |

List endpoints accept `limit` (1–50) and `offset`. The generated OpenAPI spec gives the request schemas.
Staff notes and consultation questions stay in staff-only tables. The advisor receives only the approved
public fact on resumption. In the staff simulator, use `inbox` and select a conversation by number.
The selection is retained for `messages`, `note`, `takeover`, `reply` and `handback`. Use `consultations` to
select an open question and `answer` to submit an approved fact for that selection. `answer` opens the list
when there is no selection. A stale selection is cleared if the API rejects it. The simulator resolves conversation and
consultation IDs internally. Enter `n` or `p` for another page or leave a selection blank to cancel.

Use one terminal for the staff simulator and another for
`Simulate-Sms.ps1`. Ask the advisor whether rescheduling costs extra when that policy is unconfigured.
Use `/watch` in the customer terminal. Submit a customer-ready answer in the staff terminal, then
watch the worker deliver the AI follow-up. To try human ownership, use `takeover`, `reply` and `handback`.

Ownership and assignment are separate from the receiving SMS number. An AI-owned thread can wait for
staff with one or more consultations open. An answered consultation schedules a durable worker turn
and resolves when that turn commits its reply. Pending consultations stay in the background during greetings
and unrelated questions. Explicit status inquiries reuse the relevant consultation. The advisor can supersede obsolete questions when the customer withdraws or changes a
request. Queued customer turns are processed before staff resumption, and takeover fences outstanding
AI work. Customer SMS still persists during takeover. A staff reply marks the current
backlog addressed. Handback resumes eligible work still unanswered. Outbox submission is serialized
with ownership changes. A provider submission made before takeover remains subject to carrier delivery.

Configure each service with `name`, `description` and `minutes`. Policies map topic names to
customer-facing facts. Closure dates use `YYYY-MM-DD` in the dealership timezone. Appointment capacity
and the opening schedule come from the mock DMS. Configured service duration determines the end time,
and the dealership's appointment interval determines allowed start times. With an empty catalog the
mock DMS uses the dealership's default interval and accepts the existing free-form service labels.
The advisor verifies the exact requested time, can offer nearby openings, and checks capacity again
when executing the confirmed change. Lunchtime results use starts from 11 AM up to 2 PM in dealership
time, ordered by distance from noon. The scheduler records one service per appointment and rejects
overlap for the same customer or vehicle. Separate, non-overlapping appointments are supported.
Parts inventory, technicians and pricing require configured facts or a staff answer.

The advisor can stage a private question as part of its turn. A new consultation, its acknowledgment
and its outbound delivery record commit together. An exact repeated question can use existing open or
answered work or reuse an approved resolved fact from the last 24 hours when the original scope still
applies. A resolved fact creates no new follow-up. Canceled questions are eligible for a new request.

Ordinary SMS remains model-authored. In the same structured result, the advisor identifies claims about
staff follow-up, checked availability and appointment changes. The worker checks those declarations
before publishing. A missing supporting operation produces a brief neutral response, or the existing
staff acknowledgment when a real question was staged. A staff resumption with an unsupported claim
uses its existing failed-reply retry policy. This check trusts the advisor to label its prose and does
not prove that every sentence or quoted slot matches the evidence. Review unmarked claims in the live
benchmark. The change adds no model passes. The existing optional review for standalone information
attached to a canonical appointment proposal still applies.

## Run tests

Set `POSTGRES_PASSWORD` in `.env`, then run the test image. It creates `servicepilot_test` on the bundled PostgreSQL server if needed.

```sh
docker compose run --rm --build tests
```

To run pytest on the host, create a dedicated PostgreSQL test database, point `TEST_DATABASE_URL` at it, and run `uv run --locked pytest`. The test role needs permission to create schemas.

The suite applies Alembic migrations in a temporary schema and removes it at the end of the run. It uses independent committed transactions to verify concurrent bookings, inbound SMS delivery, and agent execution. Agent tests stub the model and require no OpenAI key.

Run the optional semantic smoke test with `uv run --locked python -m scripts.evaluate_confirmation`. It uses
`OPENAI_API_KEY` from the environment or `.env`, uses `AGENT_MODEL`, and needs no database.

### Live Phase 4 acceptance

After running the Compose suite to create the test database, run this from the repository root
with the development PostgreSQL port published and `OPENAI_API_KEY` configured in `.env` or the shell.

```powershell
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://servicepilot@127.0.0.1:5432/servicepilot_test'
$env:PGPASSWORD = Read-Host -MaskInput 'Development PostgreSQL password'
uv run --locked python -m scripts.evaluate_staff_workflow
```

The default runs all scenarios once with GPT-6 Luna at low reasoning effort, using one
execution attempt per customer turn. Each scenario starts with synthetic fixtures in its own
temporary PostgreSQL schema. The terminal shows messages, operational results and elapsed time.
Detailed results are written as each scenario finishes to a JSONL report under `.tmp/`.
Ctrl+C prints a partial summary and preserves completed results.

Use `--scenarios F_resume_after_ack G_supersession` to replay selected cases. The optional
`--models`, `--reasoning-effort`, `--repeats`, `--report` and `--extended` flags support other runs.
For the consultation relevance check alone, run `uv run --locked python -m scripts.evaluate_staff_workflow --scenarios K_consultation_relevance`.
It exercises a greeting and unrelated service question during an open consultation, an explicit status request,
and the proactive follow-up after an employee answers.

In the deferred-confirmation scenario, asking about service duration can retire the original
proposal. A later explicit request for the same slot can then publish a fresh prompt, adding
one confirmation turn. A future UX improvement could preserve the original exact proposal
through an informational exchange while retaining delivery, expiry and explicit-reference checks.

Existing application `.env` model settings still override application defaults. Update an old
`AGENT_MODEL` entry to `gpt-6-luna` and remove a stale `AGENT_REASONING_EFFORT` override or set it to
`low` before recreating the worker.

Review the report for unsupported facts, false commitments, concise wording and correct slot
descriptions alongside the persisted consultation and appointment outcomes. Booking cases
identify proposal failures separately from confirmation failures. The estimated short-context
Standard inference cost uses returned input, cached input and output tokens. Reasoning tokens
are included in output tokens. The estimate excludes cache writes and usage the SDK never
receives. See the [GPT-6 Luna API model documentation](https://developers.openai.com/api/docs/models/gpt-6-luna).

For a failed run, `agent_executions.advisor_attempts` records the prompt version, tool outcomes, redacted
scheduling inputs and results, and the source and ID of the saved reply. The diagnostic list excludes
SMS bodies, staff text, and full tool payloads.

Phase 4 live-model testing uses simulated SMS with the real advisor and worker code. Live Twilio carrier
delivery and registration remain unverified. The scheduling integration is a mock DMS, staff use the API
or console simulator, and login should be protected by deployment-level rate limits if exposed remotely.

## Code layout

| Path | Contents |
| --- | --- |
| `servicepilot/` | FastAPI entry point, database setup, domain models, and development seed command |
| `servicepilot/appointments/` | Scheduling rules, appointment services, request schemas, and API routes |
| `servicepilot/conversations/` | Inbound SMS persistence, simulation endpoint, and typed conversation context |
| `servicepilot/agent/` | Service Advisor instructions, read and proposal tools, and PostgreSQL-backed execution |
| `servicepilot/sms/` | Consent, Twilio webhooks, delivery outbox and SMS transports |
| `servicepilot/staff/` | Employee authentication, ownership and assignments, private notes, and consultations |
| `servicepilot/worker.py` | Independent worker for agent execution and outbound delivery |
| `servicepilot/integrations/` | DMS adapter interface and PostgreSQL-backed mock implementation |
| `alembic/` | PostgreSQL schema migrations |
| `tests/` | Health, appointment, conversation, concurrency, schema, and seed tests backed by PostgreSQL |
