from datetime import datetime, timedelta, timezone
from datetime import time as datetime_time
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument

from .config import Settings


INVALID_PERSON_NAMES = {"who", "what", "when", "where", "why", "how"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Repository:
    """Only this class performs database I/O; LLM output is validated first."""

    def __init__(self, db: Any, default_patient_chat_id: str | None = None, default_caregiver_chat_id: str | None = None):
        self.db = db
        self.default_patient_chat_id = self._normalize_chat_id(default_patient_chat_id)
        self.default_caregiver_chat_id = self._normalize_chat_id(default_caregiver_chat_id)

    def ensure_indexes(self) -> None:
        self.db.inbound_messages.create_index("message_id", unique=True)
        self.db.conversation_messages.create_index([("recipient_id", ASCENDING), ("created_at", ASCENDING)])
        self.db.message_evaluations.create_index([("patient_id", ASCENDING), ("created_at", DESCENDING)])
        self.db.chat_links.create_index("patient_chat_id", unique=True)
        self.db.chat_links.create_index("caregiver_chat_id", unique=True)
        self.db.context_files.create_index([("patient_id", ASCENDING), ("role", ASCENDING)], unique=True)
        self.db.people.create_index([("recipient_id", ASCENDING), ("name_key", ASCENDING)], unique=True)
        self.db.people.create_index([("recipient_id", ASCENDING), ("relationship", ASCENDING)])
        self.db.events.create_index([("recipient_id", ASCENDING), ("starts_at", ASCENDING)])
        self.db.reminders.create_index([("recipient_id", ASCENDING), ("due_at", ASCENDING), ("status", ASCENDING)])

    @classmethod
    def from_settings(cls, settings: Settings) -> "Repository":
        return cls(
            MongoClient(settings.mongodb_uri)[settings.mongodb_database],
            settings.default_patient_chat_id,
            settings.default_caregiver_chat_id,
        )

    def claim_message(self, message_id: str) -> bool:
        try:
            self.db.inbound_messages.insert_one({"message_id": message_id, "received_at": utcnow()})
            return True
        except Exception as exc:
            if exc.__class__.__name__ == "DuplicateKeyError":
                return False
            raise

    def release_message_claim(self, message_id: str) -> None:
        """Allow a webhook retry when reply generation or delivery did not finish."""
        self.db.inbound_messages.delete_one({"message_id": message_id})

    def add_conversation_message(self, recipient_id: str, role: str, text: str) -> None:
        self.db.conversation_messages.insert_one({
            "recipient_id": recipient_id,
            "role": role,
            "text": text,
            "created_at": utcnow(),
        })

    def recent_conversation(self, recipient_id: str, limit: int = 12) -> list[dict]:
        rows = list(self.db.conversation_messages.find(
            {"recipient_id": {"$in": self._recipient_variants(recipient_id)}},
            {"_id": 0, "role": 1, "text": 1},
        ).sort("created_at", ASCENDING))
        return rows[-limit:]

    def link_caregiver(self, patient_chat_id: str, caregiver_chat_id: str) -> dict:
        doc = {"patient_chat_id": self._normalize_chat_id(patient_chat_id), "caregiver_chat_id": self._normalize_chat_id(caregiver_chat_id), "created_at": utcnow()}
        self._merge_people(doc["caregiver_chat_id"], doc["patient_chat_id"])
        self.db.chat_links.delete_many({"$or": [{"patient_chat_id": doc["patient_chat_id"]}, {"caregiver_chat_id": doc["caregiver_chat_id"]}]})
        self.db.chat_links.insert_one(doc)
        return {key: value for key, value in doc.items() if key != "_id"}

    def _merge_people(self, source_recipient_id: str, target_recipient_id: str) -> None:
        if source_recipient_id == target_recipient_id:
            return
        for person in self.db.people.find({"recipient_id": source_recipient_id}):
            self.add_person(target_recipient_id, person["name"], person["relationship"])
            self.db.people.delete_one({"_id": person["_id"]})

    def resolve_chat(self, chat_id: str) -> dict:
        raw = self._normalize_chat_id(chat_id)
        link = self.db.chat_links.find_one({"$or": [{"patient_chat_id": raw}, {"caregiver_chat_id": raw}]})
        if not link:
            if self.default_patient_chat_id and self.default_caregiver_chat_id:
                if raw == self.default_caregiver_chat_id:
                    return {"patient_id": self.default_patient_chat_id, "role": "caregiver", "linked": True, "source": "default"}
                if raw == self.default_patient_chat_id:
                    return {"patient_id": self.default_patient_chat_id, "role": "patient", "linked": True, "source": "default"}
            return {"patient_id": raw, "role": "patient", "linked": False, "source": "unlinked"}
        if link["caregiver_chat_id"] == raw:
            return {"patient_id": link["patient_chat_id"], "role": "caregiver", "linked": True, "source": "database"}
        return {"patient_id": link["patient_chat_id"], "role": "patient", "linked": True, "source": "database"}

    def get_caregiver_chat_id(self, patient_id: str) -> str | None:
        raw = self._normalize_chat_id(patient_id)
        link = self.db.chat_links.find_one({"patient_chat_id": raw}, {"_id": 0, "caregiver_chat_id": 1})
        if link:
            return link["caregiver_chat_id"]
        if raw == self.default_patient_chat_id:
            return self.default_caregiver_chat_id
        return None

    def patient_profile(self, patient_id: str) -> dict:
        context = self.get_context_for_role(patient_id, "patient") or {}
        facts = context.get("facts") or {}
        return {
            **facts,
            "people": self.list_people(patient_id),
            "notes": context.get("notes", ""),
        }

    def add_message_evaluation(self, patient_id: str, sender_id: str, text: str, evaluation: dict) -> None:
        self.db.message_evaluations.insert_one({
            "patient_id": self._normalize_chat_id(patient_id),
            "sender_id": self._normalize_chat_id(sender_id),
            "text": text,
            "evaluation": evaluation,
            "created_at": utcnow(),
        })

    def create_initial_context(
        self,
        patient_chat_id: str,
        caregiver_chat_id: str,
        patient_name: str,
        caregiver_name: str,
        caregiver_relationship: str,
        patient_notes: str = "",
        caregiver_notes: str = "",
        extra_facts: dict | None = None,
    ) -> dict:
        link = self.link_caregiver(patient_chat_id, caregiver_chat_id)
        patient_id = link["patient_chat_id"]
        now = utcnow()
        patient_doc = {
            "patient_id": patient_id,
            "role": "patient",
            "title": f"{patient_name} patient context",
            "facts": {
                "patient_name": patient_name,
                "primary_caregiver": caregiver_name,
                "caregiver_relationship": caregiver_relationship,
                **(extra_facts or {}),
            },
            "notes": patient_notes,
            "created_at": now,
            "updated_at": now,
        }
        caregiver_doc = {
            "patient_id": patient_id,
            "role": "caregiver",
            "title": f"{patient_name} caregiver source of truth",
            "facts": {
                "patient_name": patient_name,
                "caregiver_name": caregiver_name,
                "caregiver_relationship": caregiver_relationship,
                **(extra_facts or {}),
            },
            "notes": caregiver_notes,
            "created_at": now,
            "updated_at": now,
        }
        # Intake synchronization must not erase sensitive updates supplied later
        # by a caregiver (for example, a pet's passing).
        for doc in (patient_doc, caregiver_doc):
            existing = self.get_context_for_role(patient_id, doc["role"]) or {}
            existing_facts = existing.get("facts") or {}
            for key in ("caregiver_updates", "dietary_restrictions", "dietary_guidance"):
                if existing_facts.get(key):
                    doc["facts"][key] = existing_facts[key]
        for doc in (patient_doc, caregiver_doc):
            self.db.context_files.update_one(
                {"patient_id": patient_id, "role": doc["role"]},
                {"$set": {k: v for k, v in doc.items() if k != "created_at"}, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        return {"patient_id": patient_id, "contexts": self.get_context_files(patient_id)}

    def add_caregiver_update(self, patient_id: str, subject: str, update: str) -> None:
        """Save sensitive, caregiver-authoritative context for gentle patient responses."""
        patient_id = self._normalize_chat_id(patient_id)
        item = {"subject": subject.strip().casefold(), "update": update.strip(), "created_at": utcnow()}
        for role in ("patient", "caregiver"):
            self.db.context_files.update_one(
                {"patient_id": patient_id, "role": role},
                {"$push": {"facts.caregiver_updates": item}, "$set": {"updated_at": utcnow()}},
            )

    def add_dietary_restriction(self, patient_id: str, restriction: str) -> None:
        patient_id = self._normalize_chat_id(patient_id)
        item = {"restriction": restriction.strip(), "created_at": utcnow()}
        for role in ("patient", "caregiver"):
            self.db.context_files.update_one(
                {"patient_id": patient_id, "role": role},
                {"$addToSet": {"facts.dietary_restrictions": item}, "$set": {"updated_at": utcnow()}},
            )

    def add_dietary_guidance(self, patient_id: str, guidance: str) -> None:
        patient_id = self._normalize_chat_id(patient_id)
        item = {"guidance": guidance.strip(), "created_at": utcnow()}
        for role in ("patient", "caregiver"):
            self.db.context_files.update_one(
                {"patient_id": patient_id, "role": role},
                {"$addToSet": {"facts.dietary_guidance": item}, "$set": {"updated_at": utcnow()}},
            )

    def add_caregiver_note(self, patient_id: str, text: str) -> None:
        """Retain unverified caregiver commentary without exposing it as patient fact."""
        patient_id = self._normalize_chat_id(patient_id)
        self.db.caregiver_notes.insert_one({
            "patient_id": patient_id,
            "text": text.strip(),
            "source": "caregiver_report",
            "verification_required": True,
            "created_at": utcnow(),
        })

    def create_intake(
        self,
        patient_chat_id: str,
        caregiver_chat_id: str,
        patient_name: str,
        caregiver_name: str,
        caregiver_relationship: str,
        patient_notes: str = "",
        caregiver_notes: str = "",
        doctors: list[dict] | None = None,
        important_people: list[dict] | None = None,
        daily_routines: list[dict] | None = None,
        upcoming_events: list[dict] | None = None,
        safety_notes: list[str] | None = None,
        communication_preferences: list[str] | None = None,
        grounding_memories: dict | None = None,
        intake_source_digest: str | None = None,
    ) -> dict:
        extra_facts = {
            "safety_notes": safety_notes or [],
            "communication_preferences": communication_preferences or [],
            "grounding_memories": grounding_memories or {},
        }
        if intake_source_digest:
            extra_facts["intake_source_digest"] = intake_source_digest
        result = self.create_initial_context(
            patient_chat_id,
            caregiver_chat_id,
            patient_name,
            caregiver_name,
            caregiver_relationship,
            patient_notes,
            caregiver_notes,
            extra_facts,
        )
        patient_id = result["patient_id"]
        self.add_person(patient_id, caregiver_name, caregiver_relationship)
        for person in important_people or []:
            self.add_person(patient_id, person["name"], person["relationship"])
        for doctor in doctors or []:
            self.add_person(patient_id, doctor["name"], doctor["role"])
        for event in upcoming_events or []:
            self.add_event(patient_id, event["name"], event["starts_at"])
        for routine in daily_routines or []:
            hour, minute = (int(part) for part in routine["time"].split(":", 1))
            due_at = datetime.combine(utcnow().date(), datetime_time(hour=hour, minute=minute), tzinfo=timezone.utc)
            if due_at <= utcnow():
                due_at += timedelta(days=1)
            self.add_reminder(patient_id, routine["task"], due_at, routine["recurrence"])
        return {
            "patient_id": patient_id,
            "contexts": self.get_context_files(patient_id),
            "people": self.list_people(patient_id),
            "events": self.list_events(patient_id),
            "reminders": self.get_reminders(patient_id, utcnow() - timedelta(days=1), utcnow() + timedelta(days=3650)),
        }

    def get_context_files(self, patient_id: str) -> list[dict]:
        return list(self.db.context_files.find(
            {"patient_id": self._normalize_chat_id(patient_id)},
            {"_id": 0},
        ).sort("role", ASCENDING))

    def get_context_for_role(self, patient_id: str, role: str) -> dict | None:
        return self.db.context_files.find_one(
            {"patient_id": self._normalize_chat_id(patient_id), "role": role},
            {"_id": 0},
        )

    def add_person(self, recipient_id: str, name: str, relationship: str) -> dict:
        now = utcnow()
        if name.strip().casefold() in INVALID_PERSON_NAMES:
            raise ValueError("invalid person name")
        return self.db.people.find_one_and_update(
            {"recipient_id": recipient_id, "name_key": name.casefold()},
            {"$set": {"name": name, "relationship": relationship, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True, return_document=ReturnDocument.AFTER,
        )

    @staticmethod
    def _normalize_chat_id(chat_id: str | None) -> str | None:
        return chat_id.rstrip("/").rsplit("/", 1)[-1] if chat_id else None

    @classmethod
    def _recipient_variants(cls, recipient_id: str) -> list[str]:
        """Read legacy resource-path keys while all new writes use a raw chat ID."""
        raw = cls._normalize_chat_id(recipient_id)
        return list(dict.fromkeys([recipient_id, raw, f"chats/{raw}"]))

    def get_person(self, recipient_id: str, name: str) -> dict | None:
        return self.db.people.find_one(
            {"recipient_id": {"$in": self._recipient_variants(recipient_id)}, "name_key": name.casefold()},
            {"_id": 0},
        )

    def get_person_by_relationship(self, recipient_id: str, relationship: str) -> dict | None:
        return self.db.people.find_one(
            {
                "recipient_id": {"$in": self._recipient_variants(recipient_id)},
                "relationship": relationship.strip().casefold(),
                "name_key": {"$nin": list(INVALID_PERSON_NAMES)},
            },
            {"_id": 0},
            sort=[("updated_at", DESCENDING)],
        )

    def list_people(self, recipient_id: str) -> list[dict]:
        return list(self.db.people.find({"recipient_id": {"$in": self._recipient_variants(recipient_id)}}, {"_id": 0}).sort("name_key", ASCENDING))

    def add_event(self, recipient_id: str, name: str, starts_at: datetime) -> dict:
        doc = {"recipient_id": recipient_id, "name": name, "starts_at": starts_at, "created_at": utcnow()}
        self.db.events.insert_one(doc)
        return doc

    def get_events(self, recipient_id: str, start: datetime, end: datetime) -> list[dict]:
        return list(self.db.events.find({"recipient_id": recipient_id, "starts_at": {"$gte": start, "$lt": end}}, {"_id": 0}).sort("starts_at", ASCENDING))

    def list_events(self, recipient_id: str) -> list[dict]:
        return list(self.db.events.find({"recipient_id": recipient_id}, {"_id": 0}).sort("starts_at", ASCENDING))

    def healthcheck(self) -> bool:
        return bool(self.db.command("ping").get("ok"))

    def add_reminder(self, recipient_id: str, task: str, due_at: datetime, recurrence: str = "none") -> dict:
        doc = {"recipient_id": recipient_id, "task": task, "due_at": due_at, "recurrence": recurrence, "status": "pending", "sent_at": None, "acknowledged_at": None, "created_at": utcnow()}
        self.db.reminders.insert_one(doc)
        return doc

    def get_reminders(self, recipient_id: str, start: datetime, end: datetime) -> list[dict]:
        return list(self.db.reminders.find({"recipient_id": recipient_id, "due_at": {"$gte": start, "$lt": end}, "status": "pending"}, {"_id": 0}).sort("due_at", ASCENDING))

    def next_event(self, recipient_id: str, after: datetime) -> dict | None:
        return self.db.events.find_one({"recipient_id": recipient_id, "starts_at": {"$gte": after}}, {"_id": 0}, sort=[("starts_at", ASCENDING)])

    def claim_due_reminders(self, now: datetime) -> list[dict]:
        # Atomic lease prevents two scheduler instances from double-sending.
        claimed = []
        while doc := self.db.reminders.find_one_and_update(
            {"status": "pending", "due_at": {"$lte": now}},
            {"$set": {"status": "sending", "sending_at": now}},
            sort=[("due_at", ASCENDING)], return_document=ReturnDocument.AFTER,
        ):
            claimed.append(doc)
        return claimed

    def mark_sent(self, reminder_id: Any) -> None:
        reminder = self.db.reminders.find_one_and_update(
            {"_id": reminder_id, "status": "sending"},
            {"$set": {"status": "sent", "sent_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        if reminder and reminder["recurrence"] != "none":
            delta = timedelta(days=1 if reminder["recurrence"] == "daily" else 7)
            self.add_reminder(reminder["recipient_id"], reminder["task"], reminder["due_at"] + delta, reminder["recurrence"])

    def release_claim(self, reminder_id: Any) -> None:
        self.db.reminders.update_one({"_id": reminder_id, "status": "sending"}, {"$set": {"status": "pending"}, "$unset": {"sending_at": ""}})

    def acknowledge_latest(self, recipient_id: str) -> dict | None:
        return self.db.reminders.find_one_and_update(
            {"recipient_id": recipient_id, "status": "sent"},
            {"$set": {"status": "acknowledged", "acknowledged_at": utcnow()}},
            sort=[("sent_at", DESCENDING)], return_document=ReturnDocument.AFTER,
        )
