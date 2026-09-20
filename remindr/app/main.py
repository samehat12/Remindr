import base64
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from .assistant import CareAssistant
from .config import Settings, get_settings
from .evaluator import MessageEvaluator
from .linq import LinqClient
from .models import CaregiverLinkInput, CreateReminder, EventInput, InboundMessage, OutboundMessage, PersonInput, SignupContextInput, SignupIntakeInput
from .repository import Repository
from .scheduler import ReminderScheduler


def verify_signature(body: bytes, webhook_id: str | None, timestamp: str | None, signature: str | None, secret: str | None) -> None:
    if not secret:  # local development only; production should always configure a secret
        return
    if not webhook_id or not timestamp or not signature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing webhook signature")
    try:
        if abs(time.time() - int(timestamp)) > 300:
            raise ValueError
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Expired webhook signature")
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret")
    signed = webhook_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    if not any(part.startswith("v1,") and hmac.compare_digest(expected, part[3:]) for part in signature.split(" ")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")


def parse_linq_message(body: bytes) -> InboundMessage | None:
    """Accept Linq's V3 message.received envelope; return None for delivery/status webhooks."""
    payload = json.loads(body)
    if "event_type" not in payload:  # lightweight local test contract
        return InboundMessage.model_validate(payload)
    if payload.get("event_type") != "message.received":
        return None
    data = payload["data"]
    text = "".join(part.get("value", "") for part in data.get("parts", []) if part.get("type") == "text").strip()
    chat_id = data.get("chat", {}).get("id")
    if chat_id:
        # Persist one stable key regardless of Linq's raw UUID vs resource-path form.
        chat_id = chat_id.rstrip("/").rsplit("/", 1)[-1]
    if not text or not chat_id:
        return None
    return InboundMessage(message_id=payload["event_id"], sender_id=chat_id, text=text, received_at=payload.get("created_at"))


def create_app(settings: Settings | None = None, repo: Repository | None = None) -> FastAPI:
    settings = settings or get_settings()
    repo = repo or Repository.from_settings(settings)
    linq = LinqClient(settings)
    assistant = CareAssistant(repo, settings.openai_api_key, settings.default_timezone)
    evaluator = MessageEvaluator()
    scheduler = ReminderScheduler(repo, linq)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repo.ensure_indexes()
        jobber = AsyncIOScheduler(timezone="UTC")
        jobber.add_job(scheduler.deliver_due, "interval", seconds=settings.scheduler_poll_seconds, id="deliver-reminders", max_instances=1, coalesce=True)
        jobber.start()
        yield
        jobber.shutdown(wait=False)

    app = FastAPI(title="Care Companion MVP", lifespan=lifespan)
    app.state.repo, app.state.linq = repo, linq

    @app.get("/health")
    async def health():
        try:
            return {"ok": True, "mongodb": repo.healthcheck(), "version": "memory-sync-2026-09-19-2"}
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MongoDB is unavailable") from exc

    @app.post("/webhooks/linq", status_code=status.HTTP_202_ACCEPTED)
    async def linq_webhook(
        request: Request,
        webhook_id: str | None = Header(default=None),
        webhook_timestamp: str | None = Header(default=None),
        webhook_signature: str | None = Header(default=None),
    ):
        body = await request.body()
        verify_signature(body, webhook_id, webhook_timestamp, webhook_signature, settings.linq_webhook_secret)
        message = parse_linq_message(body)
        if message is None:
            return {"status": "ignored"}
        if not repo.claim_message(message.message_id):
            return {"status": "duplicate"}
        try:
            session = repo.resolve_chat(message.sender_id)
            print(
                "linq_message",
                {
                    "sender_id": message.sender_id,
                    "memory_id": session["patient_id"],
                    "role": session["role"],
                    "linked": session["linked"],
                    "source": session["source"],
                    "text": message.text,
                },
            )
            if session["role"] == "patient":
                evaluation = evaluator.evaluate(message.text, repo.patient_profile(session["patient_id"]))
                repo.add_message_evaluation(session["patient_id"], message.sender_id, message.text, evaluation.model_dump())
                if evaluation.confusion_flag:
                    caregiver_id = repo.get_caregiver_chat_id(session["patient_id"])
                    if caregiver_id and caregiver_id != message.sender_id:
                        try:
                            await linq.send_message(caregiver_id, evaluation.caregiver_alert_message or "Patient may need a check-in.")
                        except Exception as exc:
                            print("caregiver_alert_failed", {"caregiver_id": caregiver_id, "error": str(exc)})
            reply = await assistant.respond(
                message.sender_id,
                message.text,
                memory_id=session["patient_id"],
                can_write_personal_facts=session["role"] == "caregiver" or not session["linked"],
            )
            await linq.send_message(message.sender_id, reply)
            repo.add_conversation_message(message.sender_id, "user", message.text)
            repo.add_conversation_message(message.sender_id, "assistant", reply)
        except Exception:
            repo.release_message_claim(message.message_id)
            raise
        return {"status": "sent"}

    @app.post("/caregiver/link")
    async def link_caregiver(payload: CaregiverLinkInput):
        return repo.link_caregiver(payload.patient_chat_id, payload.caregiver_chat_id)

    @app.post("/signup/context")
    async def signup_context(payload: SignupContextInput):
        return repo.create_initial_context(
            payload.patient_chat_id,
            payload.caregiver_chat_id,
            payload.patient_name,
            payload.caregiver_name,
            payload.caregiver_relationship,
            payload.patient_notes,
            payload.caregiver_notes,
        )

    @app.post("/signup/intake")
    async def signup_intake(payload: SignupIntakeInput):
        return repo.create_intake(
            payload.patient_chat_id,
            payload.caregiver_chat_id,
            payload.patient_name,
            payload.caregiver_name,
            payload.caregiver_relationship,
            payload.patient_notes,
            payload.caregiver_notes,
            [item.model_dump() for item in payload.doctors],
            [item.model_dump() for item in payload.important_people],
            [item.model_dump() for item in payload.daily_routines],
            [item.model_dump() for item in payload.upcoming_events],
            payload.safety_notes,
            payload.communication_preferences,
        )

    @app.get("/caregiver/context/{recipient_id}")
    async def get_context(recipient_id: str):
        memory_id = repo.resolve_chat(recipient_id)["patient_id"]
        return {"recipient_id": memory_id, "contexts": repo.get_context_files(memory_id)}

    @app.post("/caregiver/people")
    async def add_person(payload: PersonInput):
        memory_id = repo.resolve_chat(payload.recipient_id)["patient_id"]
        person = repo.add_person(memory_id, payload.name, payload.relationship)
        return {"name": person["name"], "relationship": person["relationship"]}

    @app.get("/caregiver/people/{recipient_id}")
    async def list_people(recipient_id: str):
        memory_id = repo.resolve_chat(recipient_id)["patient_id"]
        return {"recipient_id": memory_id, "people": repo.list_people(memory_id)}

    @app.get("/caregiver/memory-check/{recipient_id}")
    async def memory_check(recipient_id: str):
        """Scoped diagnostic: confirms Mongo is reachable and shows saved people only for this chat."""
        try:
            session = repo.resolve_chat(recipient_id)
            return {
                "ok": True,
                "recipient_id": recipient_id,
                "memory_id": session["patient_id"],
                "role": session["role"],
                "linked": session["linked"],
                "source": session["source"],
                "people": repo.list_people(session["patient_id"]),
                "contexts": repo.get_context_files(session["patient_id"]),
            }
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MongoDB is unavailable") from exc

    @app.post("/caregiver/events")
    async def add_event(payload: EventInput):
        memory_id = repo.resolve_chat(payload.recipient_id)["patient_id"]
        event = repo.add_event(memory_id, payload.name, payload.starts_at)
        return {"name": event["name"], "starts_at": event["starts_at"]}

    @app.get("/caregiver/events/{recipient_id}")
    async def list_events(recipient_id: str):
        memory_id = repo.resolve_chat(recipient_id)["patient_id"]
        return {"recipient_id": memory_id, "events": repo.list_events(memory_id)}

    @app.post("/caregiver/reminders")
    async def add_reminder(payload: CreateReminder):
        memory_id = repo.resolve_chat(payload.recipient_id)["patient_id"]
        reminder = repo.add_reminder(memory_id, payload.task, payload.due_at, payload.recurrence)
        return {"task": reminder["task"], "due_at": reminder["due_at"], "recurrence": reminder["recurrence"]}

    @app.get("/caregiver/reminders/{recipient_id}")
    async def list_reminders(recipient_id: str):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        memory_id = repo.resolve_chat(recipient_id)["patient_id"]
        return {"recipient_id": memory_id, "reminders": repo.get_reminders(memory_id, now - timedelta(days=3650), now + timedelta(days=3650))}

    @app.post("/internal/send")
    async def internal_send(payload: OutboundMessage):
        await linq.send_message(payload.recipient_id, payload.text)
        return {"status": "sent"}

    return app


app = create_app()
