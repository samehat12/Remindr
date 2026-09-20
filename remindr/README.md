# Care Companion MVP

A backend-first iMessage companion that routes messages through Linq, uses GPT-5.6 Luna for *tool selection only*, stores scoped personal memory in MongoDB, and sends reminders from a real scheduler.

## What is implemented

- `POST /webhooks/linq`: signed, idempotent Linq V3 `message.received` entry point; response is sent into the same Linq chat.
- GPT-5.6 Luna Responses API with strict function schemas for reminder and retrieval intents.
- MongoDB collections for people, events, reminders, and processed inbound messages.
- Reminder polling scheduler with an atomic MongoDB lease to avoid duplicate delivery across instances.
- `DONE` marks the latest delivered reminder as acknowledged.
- Caregiver endpoints for people, events, creating reminders, and listing reminders.
- Two-channel caregiver mode: link a caregiver Linq chat to a patient Linq chat; caregiver updates are authoritative, while the patient chat cannot overwrite people/events.

The LLM never receives database credentials, does not send iMessages, and cannot write arbitrary documents. It can only propose one of the allowed function calls. The backend parses/validates each argument, scopes every query to the sender, then executes it.

## Two Linq conversations

Create two Linq chats (one with the patient, one with the caregiver), then link their chat IDs:

```bash
curl -X POST http://localhost:8000/caregiver/link -H 'content-type: application/json' \
  -d '{"patient_chat_id":"PATIENT_CHAT_ID","caregiver_chat_id":"CAREGIVER_CHAT_ID"}'
```

The caregiver can text `Susan is my daughter.` and that fact is stored under the patient chat's memory. The patient can ask `Who is Susan?` and retrieve it, but a patient message cannot overwrite that fact. Patient reminders still belong to the patient chat and are delivered there. For production, protect the caregiver endpoint with caregiver authentication; this MVP endpoint is intended for a trusted local integration setup.

## Run locally

```bash
cp .env.example .env
docker compose up -d mongodb
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

On its first startup, the app loads `examples/intake.example.json` into MongoDB as the demo patient profile. It only does this when that patient has no existing context, so caregiver updates are never overwritten. Set `AUTO_SEED_EXAMPLE_INTAKE=false` to disable this behavior.

Set `OPENAI_API_KEY` before asking natural-language questions. Set `LINQ_API_BASE_URL` to exactly `https://api.linqapp.com/api/partner/v3` (do not append `/chats`). Without a Linq token the adapter uses a visible dry run and will not deliver a real message. Create a Linq `message.received` webhook subscription pointing at `https://YOUR_HOST/webhooks/linq?version=2026-02-03`; set its Standard Webhooks signing secret as `LINQ_WEBHOOK_SECRET`.

## Caregiver examples

```bash
curl -X POST http://localhost:8000/caregiver/people -H 'content-type: application/json' \
  -d '{"recipient_id":"imessage-user-1","name":"Susan","relationship":"daughter"}'

curl -X POST http://localhost:8000/caregiver/reminders -H 'content-type: application/json' \
  -d '{"recipient_id":"imessage-user-1","task":"Evening medication","due_at":"2026-09-19T20:00:00-04:00","recurrence":"daily"}'
```

For production, put the service behind TLS, set a strong `LINQ_WEBHOOK_SECRET`, use managed MongoDB with backups, and configure a supervisor/container platform so the scheduler process restarts automatically. Linq's webhook event id is used for deduplication and its standard signed headers are replay-checked.

The Linq client sends to `POST /v3/chats/{chatId}/messages`, and retries temporary server/network failures. For a Linq rate-limit response it honors the `Retry-After` header before retrying.

## Notes

The scheduler owns delivery; the LLM does not remember future work. Medical requests are constrained in the system prompt to reminders and escalation—not advice or medication changes. The OpenAI integration uses `store=False` to reduce retained API state.
