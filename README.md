# hackmit_2026
# Remindr 
 
A backend-first **iMessage care companion** for people who need help with memory, routines, and daily reminders and for the caregivers who look after them.
 
The patient texts a calm, concise companion. It reminds them of medications and routines, tells them who the people in their life are, and answers questions about their schedule. A linked caregiver texts the same companion to teach it facts about the patient and gets alerted when the patient's messages suggest confusion or distress.
 
Messages travel over [Linq](https://linqapp.com) (iMessage), memory lives in MongoDB, and an OpenAI model is used **only to choose which tool to call** — never to store, recall, or invent personal facts on its own.
 
> **Disclaimer:** this is a hackathon prototype. It is not a medical device, gives no medical advice, and is not intended for clinical decisions.
 
---
 
## Highlights
 
- **Signed, idempotent webhook.** `POST /webhooks/linq` verifies Standard Webhooks HMAC-SHA256 signatures (5-minute replay window) and deduplicates on Linq's event ID, so retries never produce duplicate replies.
- **The LLM proposes, the backend disposes.** The model receives strict function schemas and can only suggest one of seven tools. Every argument is validated, every query is scoped to the sender's memory, and only the backend touches the database.
- **Deterministic fast paths.** `DONE`, simple reminder requests, "Who is…?" questions, and "X is my daughter" statements are handled by code, not the model, so they work even without an API key or when a model skips a tool call.
- **Two-channel caregiver mode.** Caregiver messages are the source of truth for patient memory; the patient chat can read but not overwrite personal facts.
- **Real scheduler.** Reminders are delivered by an APScheduler job using an atomic MongoDB lease, so multiple instances never double-send. The model doesn't "remember" future work — the scheduler owns delivery.
- **Confusion alerts.** Patient messages are scored, and high-scoring ones notify the caregiver's chat automatically.
- **Safe development mode.** With no Linq token, outbound messages are printed to the console instead of being sent.
- **Privacy-conscious model calls.** OpenAI requests use `store=False`, and the system prompt forbids medical advice.
---
 
## Architecture
 
```
   Patient iMessage                    Caregiver iMessage
          │                                    │
          └──────────────┬─────────────────────┘
                         ▼
                       Linq
                         │  message.received webhook
                         ▼
        ┌─────────────────────────────────────────┐
        │              FastAPI (main.py)          │
        │  verify signature → parse → dedupe      │
        │  resolve chat → role + patient memory   │
        └───────┬───────────────────────┬─────────┘
                │                       │
                ▼                       ▼
     MessageEvaluator            CareAssistant
     (patient msgs only)         deterministic paths
     score confusion             + OpenAI tool selection
                │                       │
                ▼                       ▼
        alert caregiver         Repository (MongoDB)
                                        ▲
                                        │
                          ReminderScheduler (APScheduler)
                                        │
                                        ▼
                                Linq → iMessage reminder
```
 
**Design principles**
 
1. Personal facts only ever come from the database, never from model knowledge.
2. The model never has credentials, never sends messages, and never writes arbitrary documents.
3. All database I/O lives in one class (`Repository`), which makes scoping and validation easy to audit.
4. Anything that must always work (persisting a stated fact, acknowledging `DONE`) doesn't depend on the model behaving.
---

### Module responsibilities
 
| Module | Responsibility |
|---|---|
| `main.py` | HTTP surface. Verifies webhook signatures, parses Linq envelopes, claims message IDs, resolves who is talking, runs the evaluator and assistant, sends replies, and starts the scheduler in the app lifespan. Exposes `create_app(settings, repo)` so tests can inject a mock database. |
| `assistant.py` | Turns a text message into a reply. Contains the system prompt, tool schemas, regex parsers, permission logic, and the two-step OpenAI tool-calling loop. |
| `evaluator.py` | Rule-based classification of patient messages into an intent and a 0–1 confusion score, with a validated output schema. |
| `repository.py` | The only place that talks to MongoDB. Handles people, events, reminders, context files, conversation history, chat links, evaluations, message dedupe, and reminder leases. |
| `scheduler.py` | Polls for due reminders, sends them, marks them sent, and releases the claim on failure. |
| `linq.py` | Sends messages to `POST /chats/{chatId}/messages` with bounded retries and `Retry-After` support. |
| `models.py` | Pydantic models that bound every input field (e.g. task ≤ 500 chars, message ≤ 4000). |
| `config.py` | Typed settings via `pydantic-settings`, cached with `lru_cache`. |
 
---
 
## How a message is handled
 
1. **Receive.** Linq calls `POST /webhooks/linq`. The body is read raw so the signature can be verified.
2. **Verify.** If a `LINQ_WEBHOOK_SECRET` is set, the `webhook-id`, `webhook-timestamp`, and `webhook-signature` headers are required. Timestamps more than 300 seconds off are rejected, and the HMAC-SHA256 over `id.timestamp.body` is compared in constant time against each `v1,` signature (supports secrets prefixed with `whsec_`).
3. **Parse.** Only `message.received` events are processed; delivery/status events return `{"status": "ignored"}`. Text parts are joined, the chat ID is normalized to its last path segment (Linq may send a raw UUID or a resource path such as `chats/<id>`), and the event ID becomes the message ID. A simple `{message_id, sender_id, text}` body (no `event_type`) is also accepted as a lightweight local test contract.
4. **Deduplicate.** The message ID is inserted into `inbound_messages` (unique index). Duplicates return `{"status": "duplicate"}`.
5. **Resolve.** `resolve_chat` maps the chat to a patient memory ID and a role (`patient` or `caregiver`) using a database link, a configured default pair, or — if neither applies — treats the chat as its own unlinked patient.
6. **Evaluate (patient chats only).** The evaluator scores the message, the result is stored, and a caregiver alert is sent if the score crosses the threshold.
7. **Respond.** `CareAssistant.respond` produces the reply (see below).
8. **Send and persist.** The reply is sent into the same Linq chat, then both sides of the exchange are stored as conversation history.
9. **Failure handling.** If anything raises, the message claim is released so Linq's retry can be processed again.
---
 
## The assistant
 
`CareAssistant.respond` works through these steps in order and returns at the first match:
 
| # | Step | Handled by | Notes |
|---|---|---|---|
| 1 | `DONE` | Code | Marks the most recently *sent* reminder for the chat as acknowledged. |
| 2 | Reminder shorthand | Code (regex) | e.g. `remind me to take a walk at 5pm`. |
| 3 | Relationship lookup | Code (regex + DB) | e.g. `Who is my daughter?`, `What is her doctor's name?`, `Who is Maggie's doctor?` |
| 4 | Person lookup | Code (regex + DB) | e.g. `Who is Susan?`, `Tell me about Susan`, `Do you remember Susan?` |
| 5 | Personal-fact statement | Code (regex + DB) | e.g. `Susan is my daughter.` Only if the sender is allowed to write. |
| 6 | Everything else | OpenAI + tools | Strict function calling, then a final reply generated from tool results. |
 
### Reminder shorthand
 
Format: `remind me [to|about|for] <task> [today|tomorrow] at <h>[:mm] [am|pm]`.
 
- If no AM/PM is given, the time is treated as **PM**. Say `8am` explicitly for morning reminders.
- If the resulting time has already passed today, the reminder rolls to tomorrow.
- Times are interpreted in `DEFAULT_TIMEZONE` and stored in UTC.
- Reply example: `Okay — I'll remind you about take a walk at 5:00 PM.`
### Model tools
 
The model may propose only these functions (all with `strict: true` schemas and `additionalProperties: false`):
 
| Tool | Purpose | Arguments |
|---|---|---|
| `add_person` | Save a named person and relationship | `name`, `relationship` |
| `add_event` | Save an event with an exact date/time | `name`, `datetime` (ISO 8601) |
| `create_reminder` | Create a reminder | `task`, `datetime`, `recurrence` (`none`/`daily`/`weekly`) |
| `get_today_schedule` | Today's events and pending reminders | — |
| `get_next_event` | The next upcoming event | — |
| `get_person` | Look up a person (falls back to relationship lookup) | `name` |
| `get_event` | Events on a given date | `date` (`YYYY-MM-DD`) |
 
### Execution and validation
 
Tool calls are executed by `_execute_tool`, not by the model:
 
- `add_person` / `add_event` are refused unless the sender may write personal facts (they are also removed from the tool list entirely for patient chats).
- Events and reminders must be in the **future**; timestamps without an offset are assumed to be in the default timezone.
- Names ≤ 120 characters, relationships ≤ 200, event names ≤ 250, tasks ≤ 500.
- Question words (`who`, `what`, `when`, `where`, `why`, `how`) can never be saved as a person's name, which prevents the memory from being "poisoned" by misparsed questions.
- Validation errors are returned to the model as a structured error so it can respond gracefully.
After executing, the original model output and the tool results are sent back together (preserving `call_id` pairing, statelessly with `store=False`) to produce the final short reply.
 
### Safety behavior in the system prompt
 
- Must call the save tools for stated facts/events/reminders and the retrieval tools for questions, and state only what tools return.
- Must never claim to remember something unless a save tool succeeded.
- Must never give medical advice, interpret symptoms, or change medication instructions — it suggests contacting a clinician or caregiver.
- Keeps replies short and simple; asks one short question if information is missing.
- Understands third-person caregiver phrasing such as `Maggie's doctor is Dr. Chen` and saves the person with the relationship `doctor`.
### Conversation context
 
The last 12 messages for the chat (user and assistant turns) are included in each model call, along with the current local time, timezone, and the stored role-specific context document.
 
### Without an OpenAI key
 
Deterministic paths still work. Anything that would need the model replies: *"Messaging is connected, but the assistant needs an OpenAI API key to answer requests."*
 
---
 
## Two-channel caregiver mode
 
Create two Linq chats: one with the patient, one with the caregiver and link them. Both then share **one patient memory**, keyed by the patient chat ID.
 
| Behavior | Patient chat | Caregiver chat |
|---|---|---|
| Add/update people and events | Not allowed (asked to contact caregiver) | Allowed |
| Ask "Who is Susan?" | Answered as "…is **your** daughter" | Answered as "…is **the patient's** daughter" |
| Create reminders | Allowed, delivered to the patient chat | Allowed, delivered to the patient chat |
| Third-person facts (`her doctor is Sarah`) | n/a | Saved to the patient's memory |
| Confusion evaluation | Yes | No |
| Receives confusion alerts | No | Yes |
 
**Chat resolution order** (`Repository.resolve_chat`):
 
1. A link stored in `chat_links` (database).
2. A configured default pair (`DEFAULT_PATIENT_CHAT_ID` / `DEFAULT_CAREGIVER_CHAT_ID`).
3. Otherwise the chat is an **unlinked** patient with its own memory, and — since no caregiver exists — it is allowed to write its own personal facts.
**Linking details**
 
- Re-linking replaces any stale link for either chat.
- Linking migrates any people that were accidentally saved under the caregiver's chat ID into the patient's memory.
- Chat IDs are always normalized to the last path segment. Legacy keys (`chats/<id>`, raw ID) are still readable, while all new writes use the raw ID.
- The caregiver's name and relationship are also available to the patient (e.g. *Who is my caregiver?*).
> For production, protect the caregiver endpoints with real authentication. The MVP assumes a trusted local setup.
 
---
 
## Reminders and the scheduler
 
A reminder document moves through these states:
 
```
pending ──claimed──▶ sending ──delivered──▶ sent ──patient replies DONE──▶ acknowledged
    ▲                   │
    └──── delivery fails ┘  (claim released, retried on the next poll)
```
 
- **Polling.** An `AsyncIOScheduler` runs `deliver_due` every `SCHEDULER_POLL_SECONDS` (default 30) with `max_instances=1` and `coalesce=True`.
- **Atomic lease.** `claim_due_reminders` uses `find_one_and_update` to flip `pending → sending`, so two app instances cannot claim the same reminder.
- **Delivery message.** `⏰ It's time for {task}. Reply DONE when you've done it.`
- **Recurrence.** After a successful send, `daily` and `weekly` reminders enqueue their next occurrence (`due_at` + 1 or 7 days).
- **Failures.** If sending raises, the claim is released and the reminder returns to `pending`; the error is re-raised for the scheduler's logs.
- **Acknowledgement.** Replying `DONE` marks the latest `sent` reminder as `acknowledged` with a timestamp.
Reminders can be created by the patient (natural language), by the model tool path, by a caregiver's text, via `POST /caregiver/reminders`, or from intake routines.
 
---
 
## Confusion detection and caregiver alerts
 
For every **patient** message, `MessageEvaluator` produces an `AgentEvaluation`:
 
```json
{
  "reply_text": "You're not alone. Please stay where you are, and I'll let Susan know.",
  "confusion_score": 0.85,
  "confusion_flag": true,
  "routine_intent": "DISTRESS_CALL",
  "caregiver_alert_message": "Maggie may need a check-in. Message: \"where am I\""
}
```
 
| Intent | Example phrases | Score |
|---|---|---|
| `MEDICATION_CONFIRMATION` | "done", "took my medicine/meds" | 0.10 |
| `SCHEDULE_QUERY` | "today", "schedule", "appointment", "what am I doing" | 0.20 |
| `IDENTITY_QUERY` | "who am I", "what is my name" | 0.65 |
| `IDENTITY_QUERY` | "who is my …?", "who is …?" | 0.45 |
| `DISTRESS_CALL` | "where am I", "I'm lost", "help me", "scared", "afraid", "I want to go home" | 0.85 |
| `CASUAL_CHAT` | "hi", "hello", "thanks", "thank you" | 0.05 |
| `OTHER` | anything else | 0.10 |
 
**Rules enforced by validation**
 
- `confusion_flag` is true **exactly when** `confusion_score >= 0.60`.
- A flagged evaluation must include `caregiver_alert_message`; an unflagged one must have it `null`.
**Alert flow.** When flagged, and a linked caregiver chat exists (and differs from the sender), the alert is sent to the caregiver via Linq. Alert failures are logged and never block the patient's reply. Every evaluation is stored in `message_evaluations` for later review. The patient's name and primary caregiver name are pulled from the stored signup context.
 
---
 
## Data model
 
MongoDB database (default `care_companion`), created lazily. Indexes are ensured on startup.
 
| Collection | Purpose | Key fields | Indexes |
|---|---|---|---|
| `inbound_messages` | Webhook idempotency | `message_id`, `received_at` | unique `message_id` |
| `conversation_messages` | Recent chat history for model context | `recipient_id`, `role`, `text`, `created_at` | `(recipient_id, created_at)` |
| `message_evaluations` | Stored confusion evaluations | `patient_id`, `sender_id`, `text`, `evaluation`, `created_at` | `(patient_id, created_at desc)` |
| `chat_links` | Patient ↔ caregiver mapping | `patient_chat_id`, `caregiver_chat_id`, `created_at` | unique on each chat ID |
| `context_files` | Role-scoped patient/caregiver context | `patient_id`, `role`, `title`, `facts`, `notes`, timestamps | unique `(patient_id, role)` |
| `people` | Named people and relationships | `recipient_id`, `name`, `name_key`, `relationship`, timestamps | unique `(recipient_id, name_key)`; `(recipient_id, relationship)` |
| `events` | Dated events | `recipient_id`, `name`, `starts_at`, `created_at` | `(recipient_id, starts_at)` |
| `reminders` | Reminders and delivery state | `recipient_id`, `task`, `due_at`, `recurrence`, `status`, `sent_at`, `acknowledged_at` | `(recipient_id, due_at, status)` |
 
**Context files.** Each patient has two: a `patient` document (patient name, primary caregiver, caregiver relationship, safety notes, communication preferences, patient notes) and a `caregiver` document that is the source of truth. The role-appropriate document is injected into the model's instructions.
 
---
 
## Quick start
 
**Requirements:** Python 3.10+ (3.11 recommended) and Docker for MongoDB.
 
```bash
cd remindr
cp .env.example .env
docker compose up -d mongodb
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```
 
Check it's alive:
 
```bash
curl http://localhost:8000/health
# {"ok": true, "mongodb": true, "version": "..."}
```
 
With no `LINQ_API_TOKEN`, replies and reminders are printed to the console as `[LINQ DRY RUN] to=<chat>: <text>`.
 
Try the webhook locally using the lightweight test contract (no signature secret set):
 
```bash
curl -X POST http://localhost:8000/webhooks/linq -H 'content-type: application/json' \
  -d '{"message_id":"m1","sender_id":"demo-chat","text":"Susan is my daughter."}'
 
curl -X POST http://localhost:8000/webhooks/linq -H 'content-type: application/json' \
  -d '{"message_id":"m2","sender_id":"demo-chat","text":"Who is Susan?"}'
```
 
Local MongoDB defaults: image `mongo:8`, port `27017`, persistent volume `mongodb_data`.
 
---
 
## Configuration
 
Settings are read from environment variables or `.env`.
 
| Variable | Description | Default |
|---|---|---|
| `OPENAI_API_KEY` | Required for natural-language requests beyond the deterministic paths | none |
| `MONGODB_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGODB_DATABASE` | Database name | `care_companion` |
| `LINQ_API_BASE_URL` | Must be exactly `https://api.linqapp.com/api/partner/v3` — do **not** append `/chats` | as shown |
| `LINQ_API_TOKEN` | Linq bearer token; empty enables dry-run mode | none |
| `LINQ_WEBHOOK_SECRET` | Webhook signing secret; empty disables signature checks | none (`replace-me` in the example file) |
| `LINQ_SEND_MAX_ATTEMPTS` | Max attempts for sending a message | `3` |
| `DEFAULT_TIMEZONE` | IANA timezone for parsing times | `America/New_York` |
| `SCHEDULER_POLL_SECONDS` | How often to check for due reminders | `30` |
| `DEFAULT_PATIENT_CHAT_ID` | Optional preconfigured patient chat | see `config.py` |
| `DEFAULT_CAREGIVER_CHAT_ID` | Optional preconfigured caregiver chat | see `config.py` |
 
The default chat IDs let a demo pair work without any database link. Clear or override them for real deployments.
 
### Linq client behavior
 
- Sends `POST {LINQ_API_BASE_URL}/chats/{chatId}/messages` with `Authorization: Bearer <token>` and a body of `{"message": {"parts": [{"type": "text", "value": "..."}]}}`.
- Accepts a raw UUID or a resource path and uses only the final segment.
- Retries 5xx, 429, and transport errors up to `LINQ_SEND_MAX_ATTEMPTS`. For 429 it honors `Retry-After` (capped at 60 seconds); otherwise it backs off exponentially (`2^attempt` seconds). Other 4xx responses fail immediately.
---
 
## Connecting Linq
 
1. Get your Linq API token and set `LINQ_API_TOKEN`.
2. Create a `message.received` webhook subscription pointing to:
   `https://YOUR_HOST/webhooks/linq?version=2026-02-03`
3. Copy its Standard Webhooks signing secret into `LINQ_WEBHOOK_SECRET`.
4. Create two chats (patient and caregiver) and link them:
```bash
curl -X POST http://localhost:8000/caregiver/link -H 'content-type: application/json' \
  -d '{"patient_chat_id":"PATIENT_CHAT_ID","caregiver_chat_id":"CAREGIVER_CHAT_ID"}'
```
 
For local development, expose your port with a tunnel (e.g. ngrok or Cloudflare Tunnel) so Linq can reach the webhook.
 
---
 
## Onboarding a patient
 
### One call: `POST /signup/intake`
 
Links the two chats, creates both context documents, and seeds people, doctors, events, and daily routines. Example (`examples/intake.example.json`):
 
```json
{
  "patient_chat_id": "c742f2a9-0e8e-4ed9-a6cb-bcc39fdc1001",
  "caregiver_chat_id": "576d31b7-7599-42b0-9868-66c7124efad0",
  "patient_name": "Maggie",
  "caregiver_name": "Susan",
  "caregiver_relationship": "daughter",
  "patient_notes": "Use short, simple replies.",
  "caregiver_notes": "Caregiver-provided facts are the source of truth for patient memory.",
  "doctors": [{"name": "Sarah", "role": "doctor"}, {"name": "Dr. Chen", "role": "cardiologist"}],
  "important_people": [{"name": "Susan", "relationship": "daughter"}],
  "daily_routines": [{"task": "morning medication", "time": "08:00", "recurrence": "daily"}],
  "upcoming_events": [],
  "safety_notes": ["Do not provide medical advice.", "Ask caregiver for medication changes."],
  "communication_preferences": ["Use short, simple replies."]
}
```
 
```bash
curl -X POST http://localhost:8000/signup/intake -H 'content-type: application/json' \
  -d @examples/intake.example.json
```
 
What it does:
 
- Links the chats and upserts the `patient` and `caregiver` context documents.
- Saves the caregiver, every important person, and every doctor into `people` (doctors are stored with their `role` as the relationship).
- Saves each upcoming event.
- Creates a reminder for each routine — the next occurrence of its `HH:MM` (see the timezone note under [Known limitations](#known-limitations)).
- Returns the resulting context, people, events, and reminders.
### Lighter option: `POST /signup/context`
 
Same as intake but only creates the link and the two context documents (names, relationship, and notes).
 
### Ongoing updates
 
The caregiver can simply text the companion (`Dr. Chen is the patient's cardiologist.`), or use the `/caregiver/*` endpoints.
 
---
 
## API reference
 
Interactive docs are available at `http://localhost:8000/docs` when running.
 
| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | MongoDB ping and version string; `503` if the database is unreachable |
| `POST` | `/webhooks/linq` | Linq inbound entry point. Returns `sent`, `duplicate`, or `ignored` (HTTP 202) |
| `POST` | `/caregiver/link` | Link `patient_chat_id` and `caregiver_chat_id` |
| `POST` | `/signup/context` | Create link + patient/caregiver context documents |
| `POST` | `/signup/intake` | Full onboarding (context, people, doctors, events, routines) |
| `GET` | `/caregiver/context/{recipient_id}` | View stored context documents |
| `POST` | `/caregiver/people` | Add a person (`recipient_id`, `name`, `relationship`) |
| `GET` | `/caregiver/people/{recipient_id}` | List saved people |
| `POST` | `/caregiver/events` | Add an event (`recipient_id`, `name`, `starts_at`) |
| `GET` | `/caregiver/events/{recipient_id}` | List events |
| `POST` | `/caregiver/reminders` | Add a reminder (`recipient_id`, `task`, `due_at`, `recurrence`) |
| `GET` | `/caregiver/reminders/{recipient_id}` | List pending reminders (±10-year window) |
| `GET` | `/caregiver/memory-check/{recipient_id}` | Diagnostic: resolved memory ID, role, link status, people, contexts |
| `POST` | `/internal/send` | Send a message through Linq (`recipient_id`, `text`) for testing |
 
Notes:
 
- `recipient_id` may be either the patient or caregiver chat ID; it's resolved to the patient's memory.
- `recurrence` is one of `none`, `daily`, `weekly`.
- Datetimes should be ISO 8601 with an offset, e.g. `2026-09-19T20:00:00-04:00`.
Examples:
 
```bash
curl -X POST http://localhost:8000/caregiver/people -H 'content-type: application/json' \
  -d '{"recipient_id":"imessage-user-1","name":"Susan","relationship":"daughter"}'
 
curl -X POST http://localhost:8000/caregiver/reminders -H 'content-type: application/json' \
  -d '{"recipient_id":"imessage-user-1","task":"Evening medication","due_at":"2026-09-19T20:00:00-04:00","recurrence":"daily"}'
 
curl http://localhost:8000/caregiver/memory-check/imessage-user-1
```
 
---
 
## Example conversations
 
**Caregiver teaches, patient recalls**
 
| Chat | Message | Reply |
|---|---|---|
| Caregiver | `Susan is my daughter.` | `I'll remember that Susan is the patient's daughter.` |
| Patient | `Who is Susan?` | `Susan is your daughter.` |
| Patient | `Susan is my sister.` | `I can't change personal facts from this chat. Please ask your caregiver to update that information.` |
| Caregiver | `Maggie's doctor is Dr. Chen.` | Saved as Dr. Chen → doctor |
| Patient | `Who is my doctor?` | `Your doctor is Dr. Chen.` |
| Patient | `Who is Bob?` | `I don't have saved information about Bob.` |
 
**Reminders**
 
| Chat | Message | Reply |
|---|---|---|
| Patient | `remind me to call Susan at 5pm` | `Okay — I'll remind you about call Susan at 5:00 PM.` |
| Companion | *(at 5:00 PM)* | `⏰ It's time for call Susan. Reply DONE when you've done it.` |
| Patient | `DONE` | `Thank you — I marked it done.` |
| Patient | `DONE` *(nothing pending)* | `I don't see a reminder waiting for confirmation.` |
 
**Distress**
 
| Chat | Message | Result |
|---|---|---|
| Patient | `I'm lost` | Reply: `You're not alone. Please stay where you are, and I'll let <caregiver> know.` Caregiver chat receives an alert. |
 
---
 
## Testing
 
```bash
cd remindr
pytest
```
 
`tests/test_mvp.py` contains **34 tests** using `mongomock` (no real database required), grouped as:
 
- **Memory scoping:** per-recipient isolation, legacy `chats/<id>` keys, conversation history persistence and scoping.
- **Webhook and Linq:** V3 envelope handling, idempotency, resource-path chat ID normalization, `Retry-After` handling.
- **Assistant behavior:** deterministic `Who is…?` lookups, saving facts without depending on tool selection, tool results continued with the original function call, validated tool saves, question words never saved as people, natural-language reminders.
- **Caregiver mode:** linking, re-linking, migration of misfiled people, default chat pair resolution, patient cannot overwrite authoritative facts, third-person / pronoun / inverse phrasing, relationship lookups in patient vs caregiver wording.
- **Signup:** context creation, primary caregiver readable by the patient, structured intake seeding.
- **Reminders and alerts:** recurring reminders create the next occurrence, high confusion alerts the caregiver, caregiver messages are never evaluated for confusion.
---
 
## Security and privacy
 
**Built in**
 
- The model has no database credentials and can only propose whitelisted, strictly typed function calls.
- Every query is scoped to the resolved patient memory; arguments are validated and length-bounded.
- Webhook HMAC verification with replay protection, plus event-ID idempotency.
- Patients cannot alter authoritative caregiver-provided facts.
- Medical-advice guardrails in the system prompt; requests about medication are redirected to a clinician or caregiver.
- `store=False` on OpenAI calls to reduce retained API state.
- Dry-run Linq mode when no token is set.
**You must add before real use**
 
- **Authentication** on `/caregiver/*`, `/signup/*`, and `/internal/send`. They are currently open.
- A real `LINQ_WEBHOOK_SECRET`. With no secret, signature verification is skipped entirely.
- Removal or override of the default chat IDs in `config.py`.
- Consent, retention, and access-control policies appropriate to health-related data (and applicable regulation such as HIPAA) before handling real patient information.
Note that message text is logged to the console by the webhook handler (`linq_message`); disable or redact that in production.
 


## Tech stack
 
Python · FastAPI 0.115 · Uvicorn · APScheduler 3.11 · MongoDB 8 (pymongo 4.12) · OpenAI Responses API (`gpt-5.6-luna`) · Linq Partner API v3 · Pydantic / pydantic-settings · httpx · python-dateutil · pytest + mongomock · Docker Compose
