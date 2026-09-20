from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import asyncio

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app, parse_linq_message
from app.repository import Repository
from app.linq import LinqClient
from app.assistant import CareAssistant


@pytest.fixture
def repo():
    instance = Repository(mongomock.MongoClient().care_companion)
    instance.ensure_indexes()
    return instance


def test_person_memory_is_scoped_to_recipient(repo):
    repo.add_person("alice", "Susan", "daughter")
    assert repo.get_person("alice", "susan")["relationship"] == "daughter"
    assert repo.list_people("alice")[0]["name"] == "Susan"
    assert repo.get_person("other", "Susan") is None


def test_person_memory_reads_legacy_linq_chat_key(repo):
    repo.add_person("chats/c-123", "Susan", "daughter")
    assert repo.get_person("c-123", "Susan")["relationship"] == "daughter"


def test_duplicate_inbound_messages_are_idempotent(repo):
    assert repo.claim_message("linq-123") is True
    assert repo.claim_message("linq-123") is False
    repo.release_message_claim("linq-123")
    assert repo.claim_message("linq-123") is True


def test_recurring_reminder_creates_next_occurrence_after_delivery(repo):
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    reminder = repo.add_reminder("alice", "medication", due, "daily")
    claimed = repo.claim_due_reminders(datetime.now(timezone.utc))
    assert len(claimed) == 1
    repo.mark_sent(reminder["_id"])
    reminders = list(repo.db.reminders.find({"recipient_id": "alice"}))
    assert {x["status"] for x in reminders} == {"sent", "pending"}
    expected = due + timedelta(days=1)
    expected = expected.replace(microsecond=(expected.microsecond // 1000) * 1000)
    assert max(x["due_at"] for x in reminders).replace(tzinfo=timezone.utc) == expected


def test_linq_v3_inbound_event_is_handled_and_deduplicated(repo):
    app = create_app(Settings(mongodb_uri="mongodb://unused", linq_webhook_secret=None, openai_api_key="", linq_api_token=""), repo)
    payload = {
        "event_id": "event-1",
        "event_type": "message.received",
        "created_at": "2026-09-19T12:00:00Z",
        "data": {"chat": {"id": "chat-1"}, "parts": [{"type": "text", "value": "Who is Susan?"}]},
    }
    with TestClient(app) as client:
        assert client.post("/webhooks/linq", json=payload).json() == {"status": "sent"}
        assert client.post("/webhooks/linq", json=payload).json() == {"status": "duplicate"}


def test_patient_message_is_evaluated_and_high_confusion_alerts_caregiver(repo):
    app = create_app(Settings(mongodb_uri="mongodb://unused", linq_webhook_secret=None, openai_api_key="", linq_api_token=""), repo)
    repo.create_initial_context("patient-1", "caregiver-1", "Maggie", "Susan", "daughter")
    payload = {"message_id": "event-1", "sender_id": "patient-1", "text": "Who am I?"}
    with TestClient(app) as client:
        assert client.post("/webhooks/linq", json=payload).json() == {"status": "sent"}
    evaluation = repo.db.message_evaluations.find_one({"patient_id": "patient-1"})
    assert evaluation["evaluation"]["confusion_flag"] is True
    assert evaluation["evaluation"]["routine_intent"] == "IDENTITY_QUERY"


def test_caregiver_messages_are_not_patient_confusion_evaluated(repo):
    app = create_app(Settings(mongodb_uri="mongodb://unused", linq_webhook_secret=None, openai_api_key="", linq_api_token=""), repo)
    repo.link_caregiver("patient-1", "caregiver-1")
    payload = {"message_id": "event-1", "sender_id": "caregiver-1", "text": "Who am I?"}
    with TestClient(app) as client:
        assert client.post("/webhooks/linq", json=payload).json() == {"status": "sent"}
    assert repo.db.message_evaluations.count_documents({}) == 0


def test_linq_resource_path_chat_id_is_normalized_for_memory_scope():
    message = parse_linq_message(b'{"event_id":"event-2","event_type":"message.received","data":{"chat":{"id":"chats/c-123"},"parts":[{"type":"text","value":"hello"}]}}')
    assert message.sender_id == "c-123"


def test_linq_retry_delay_honors_retry_after():
    import httpx

    response = httpx.Response(429, headers={"Retry-After": "12"})
    assert LinqClient._retry_delay(response, 0) == 12


def test_linq_resource_style_chat_id_is_normalized():
    assert "chats/c-123".removeprefix("chats/") == "c-123"
    assert "chats/chats/c-123".rsplit("/", 1)[-1] == "c-123"


def test_tool_result_is_continued_with_its_original_function_call(repo):
    repo.add_person("chat-1", "Susan", "daughter")

    class FakeResponses:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return SimpleNamespace(output=[SimpleNamespace(type="function_call", name="get_person", arguments='{"name":"Susan"}', call_id="call-1")], output_text="")
            return SimpleNamespace(output=[], output_text="Susan is your daughter.")

    responses = FakeResponses()
    assistant = CareAssistant(repo, None, "America/New_York")
    assistant.client = SimpleNamespace(responses=responses)
    assert asyncio.run(assistant.respond("chat-1", "What is Susan's relationship?")) == "Susan is your daughter."
    assert responses.calls[1]["input"][1].call_id == "call-1"
    assert responses.calls[1]["input"][2]["call_id"] == "call-1"


def test_person_and_event_can_be_saved_by_validated_tools(repo):
    assistant = CareAssistant(repo, None, "America/New_York")
    person = assistant._execute_tool("chat-1", "add_person", {"name": "Susan", "relationship": "daughter"})
    assert person["saved"] is True
    assert repo.get_person("chat-1", "Susan")["relationship"] == "daughter"
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    event = assistant._execute_tool("chat-1", "add_event", {"name": "Doctor", "datetime": future})
    assert event["saved"] is True


def test_who_is_is_a_deterministic_memory_lookup(repo):
    repo.add_person("chat-1", "Susan", "daughter")
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond("chat-1", "Who is Susan?")) == "Susan is your daughter."
    assert asyncio.run(assistant.respond("chat-1", "Tell me about Susan")) == "Susan is your daughter."


@pytest.mark.parametrize("question", ["Who am I?", "What's my name?", "What’s my nam"])
def test_self_name_question_reads_patient_context(repo, question):
    repo.create_initial_context("chat-1", "caregiver-1", "Maggie", "Susan", "daughter")
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond("chat-1", question)) == "Your name is Maggie."


def test_self_profile_question_reads_patient_context(repo):
    repo.create_initial_context("chat-1", "caregiver-1", "Maggie", "Susan", "daughter", patient_notes="Use short, simple replies.")
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond("chat-1", "What do you know about me")) == (
        "Your name is Maggie. Your caregiver is Susan. Use short, simple replies."
    )


def test_pet_location_question_uses_gentle_grounding_response(repo):
    repo.create_intake(
        "chat-1", "caregiver-1", "Maggie", "Susan", "daughter",
        grounding_memories={"pets": [{"name": "Milo", "type": "dog"}]},
    )
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond("chat-1", "Where is Milo now?")) == (
        "Milo was very loved. I know you shared many special moments together. "
        "Would you like to tell me a favorite memory of Milo?"
    )


def test_grounding_pet_type_is_found_on_first_question(repo):
    repo.create_intake(
        "chat-1", "caregiver-1", "Maggie", "Susan", "daughter",
        grounding_memories={"pets": [{"name": "Whiskers", "type": "cat", "details": "Family cat during childhood."}]},
    )
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond("chat-1", "Tell me about my cat")) == "Whiskers was your family cat during childhood."


def test_caregiver_loss_update_is_saved_for_patient_access(repo):
    repo.create_initial_context("patient-1", "caregiver-1", "Maggie", "Sameha", "daughter")
    assistant = CareAssistant(repo, None, "America/New_York")
    reply = asyncio.run(assistant.respond(
        "caregiver-1", "Maggie's parrot passed away recently.",
        memory_id="patient-1", can_write_personal_facts=True,
    ))
    assert "remember" in reply
    assert asyncio.run(assistant.respond(
        "patient-1", "Where is my parrot?", memory_id="patient-1", can_write_personal_facts=False,
    )) == "I'm so sorry. Your parrot passed away recently. They were very loved. Would you like to share a favorite memory?"


def test_caregiver_dietary_restriction_blocks_food_recommendations(repo):
    repo.create_initial_context("patient-1", "caregiver-1", "Maggie", "Sameha", "daughter")
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond(
        "caregiver-1", "Maggie cannot eat greasy or oily foods.",
        memory_id="patient-1", can_write_personal_facts=True,
    )) == "Understood. I'll remember to avoid suggesting greasy or oily foods."
    assert asyncio.run(assistant.respond(
        "caregiver-1", "Maggie should have more fiber.",
        memory_id="patient-1", can_write_personal_facts=True,
    )) == "Understood. I'll remember Maggie should have more fiber."
    assert asyncio.run(assistant.respond(
        "patient-1", "What should I have today, McDonalds or Burger King?",
        memory_id="patient-1", can_write_personal_facts=False,
    )) == (
        "I know choosing what to eat can feel frustrating. Sameha asked me to help you avoid greasy or oily foods and include more fiber. "
        "Let's check with Sameha or your clinician about what would feel good today."
    )


def test_caregiver_avoid_instruction_handles_typo_and_blocks_any_food_choice(repo):
    repo.create_initial_context("patient-1", "caregiver-1", "Maggie", "Sameha", "daughter")
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond(
        "caregiver-1", "Maggie shoukd avoid all nuts.",
        memory_id="patient-1", can_write_personal_facts=True,
    )) == "Understood. I'll remember to avoid suggesting nuts."
    assert "avoid nuts" in asyncio.run(assistant.respond(
        "patient-1", "Can I have a PB and J?", memory_id="patient-1", can_write_personal_facts=False,
    ))


def test_caregiver_shellfish_restriction_handles_cant_and_broader_food_questions(repo):
    repo.create_initial_context("patient-1", "caregiver-1", "Maggie", "Sameha", "daughter")
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond(
        "caregiver-1", "Shee cant have shellfish.",
        memory_id="patient-1", can_write_personal_facts=True,
    )) == "Understood. I'll remember to avoid suggesting shellfish."
    response = asyncio.run(assistant.respond(
        "patient-1", "Am I able to eat shrimp?", memory_id="patient-1", can_write_personal_facts=False,
    ))
    assert "avoid shellfish" in response


def test_unverified_caregiver_commentary_is_not_patient_context(repo):
    repo.create_initial_context("patient-1", "caregiver-1", "Maggie", "Sameha", "daughter")
    repo.add_caregiver_note("patient-1", "I think Maggie may not like tomatoes.")
    patient_context = repo.get_context_for_role("patient-1", "patient")
    assert "caregiver_notes" not in patient_context["facts"]
    saved = repo.db.caregiver_notes.find_one({"patient_id": "patient-1"})
    assert saved["source"] == "caregiver_report"
    assert saved["verification_required"] is True


def test_person_statement_is_saved_without_relying_on_model_tool_selection(repo):
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond("chat-1", "Susan is my daughter.")) == "I'll remember that Susan is your daughter."
    assert asyncio.run(assistant.respond("chat-1", "Who is Susan?")) == "Susan is your daughter."


def test_caregiver_third_person_statement_saves_named_person(repo):
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond("caregiver-1", "Maggie's doctor is Dr. Chen.", memory_id="patient-1")) == "I'll remember that Dr. Chen is the patient's doctor."
    assert asyncio.run(assistant.respond("patient-1", "Who is Dr. Chen?")) == "Dr. Chen is your doctor."
    assert asyncio.run(assistant.respond("patient-1", "Who is my doctor?")) == "Your doctor is Dr. Chen."
    assert asyncio.run(assistant.respond("patient-1", "What is the name ofmy doctor")) == "Your doctor is Dr. Chen."


def test_patient_relationship_question_is_never_saved_as_fact(repo):
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond("patient-1", "Who is my doctor?", memory_id="patient-1", can_write_personal_facts=False)) == "I don't have a saved doctor."
    assert repo.get_person_by_relationship("patient-1", "doctor") is None


def test_relationship_lookup_skips_poisoned_question_word_person(repo):
    repo.db.people.insert_one({"recipient_id": "patient-1", "name": "Who", "name_key": "who", "relationship": "doctor", "updated_at": datetime.now(timezone.utc)})
    repo.add_person("patient-1", "Sarah", "doctor")
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond("patient-1", "Who is my doctor?")) == "Your doctor is Sarah."


def test_conversation_history_is_persisted_and_scoped(repo):
    repo.add_conversation_message("chat-1", "user", "Hello")
    repo.add_conversation_message("chat-1", "assistant", "Hi there")
    assert {x["text"] for x in repo.recent_conversation("chat-1")} == {"Hello", "Hi there"}
    assert repo.recent_conversation("other") == []


def test_caregiver_link_maps_two_chats_to_one_authoritative_patient_memory(repo):
    repo.link_caregiver("patient-1", "caregiver-1")
    assert repo.resolve_chat("patient-1") == {"patient_id": "patient-1", "role": "patient", "linked": True, "source": "database"}
    assert repo.resolve_chat("caregiver-1") == {"patient_id": "patient-1", "role": "caregiver", "linked": True, "source": "database"}


def test_relinking_caregiver_replaces_stale_patient_mapping(repo):
    repo.link_caregiver("old-patient", "caregiver-1")
    repo.link_caregiver("patient-1", "caregiver-1")
    assert repo.resolve_chat("caregiver-1") == {"patient_id": "patient-1", "role": "caregiver", "linked": True, "source": "database"}
    assert repo.resolve_chat("old-patient") == {"patient_id": "old-patient", "role": "patient", "linked": False, "source": "unlinked"}


def test_linking_migrates_people_accidentally_saved_under_caregiver(repo):
    repo.add_person("caregiver-1", "Sarah", "doctor")
    repo.link_caregiver("patient-1", "caregiver-1")
    assert repo.get_person_by_relationship("patient-1", "doctor")["name"] == "Sarah"
    assert repo.get_person_by_relationship("caregiver-1", "doctor") is None


def test_configured_default_chat_pair_resolves_without_database_link():
    settings = Settings(mongodb_uri="mongodb://unused")
    repo = Repository(mongomock.MongoClient().care_companion, settings.default_patient_chat_id, settings.default_caregiver_chat_id)
    assert repo.resolve_chat("c742f2a9-0e8e-4ed9-a6cb-bcc39fdc1001") == {
        "patient_id": "c742f2a9-0e8e-4ed9-a6cb-bcc39fdc1001",
        "role": "patient",
        "linked": True,
        "source": "default",
    }
    assert repo.resolve_chat("576d31b7-7599-42b0-9868-66c7124efad0") == {
        "patient_id": "c742f2a9-0e8e-4ed9-a6cb-bcc39fdc1001",
        "role": "caregiver",
        "linked": True,
        "source": "default",
    }


def test_default_id_pair_caregiver_write_patient_relationship_read():
    settings = Settings(mongodb_uri="mongodb://unused", linq_webhook_secret=None, openai_api_key="", linq_api_token="")
    repo = Repository(mongomock.MongoClient().care_companion, settings.default_patient_chat_id, settings.default_caregiver_chat_id)
    repo.ensure_indexes()
    app = create_app(settings, repo)
    with TestClient(app) as client:
        caregiver_payload = {"message_id": "event-1", "sender_id": settings.default_caregiver_chat_id, "text": "Her doctor is Sarah."}
        patient_payload = {"message_id": "event-2", "sender_id": settings.default_patient_chat_id, "text": "Who is my doctor?"}
        assert client.post("/webhooks/linq", json=caregiver_payload).json() == {"status": "sent"}
        assert client.post("/webhooks/linq", json=patient_payload).json() == {"status": "sent"}
        assert repo.get_person_by_relationship(settings.default_patient_chat_id, "doctor")["name"] == "Sarah"


def test_signup_context_creates_patient_and_caregiver_context_files(repo):
    app = create_app(Settings(mongodb_uri="mongodb://unused", linq_webhook_secret=None, openai_api_key="", linq_api_token=""), repo)
    with TestClient(app) as client:
        response = client.post("/signup/context", json={
            "patient_chat_id": "patient-1",
            "caregiver_chat_id": "caregiver-1",
            "patient_name": "Maggie",
            "caregiver_name": "Susan",
            "caregiver_relationship": "daughter",
            "patient_notes": "Use short replies.",
            "caregiver_notes": "Susan is source of truth for appointments.",
        })
        body = response.json()
        assert body["patient_id"] == "patient-1"
        assert {context["role"] for context in body["contexts"]} == {"patient", "caregiver"}
        check = client.get("/caregiver/memory-check/caregiver-1").json()
        assert check["memory_id"] == "patient-1"
        assert len(check["contexts"]) == 2


def test_patient_can_read_primary_caregiver_from_signup_context(repo):
    repo.create_initial_context("patient-1", "caregiver-1", "Maggie", "Susan", "daughter")
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond("patient-1", "Who is my caregiver?")) == "Your caregiver is Susan."


def test_signup_intake_seeds_structured_memory(repo):
    app = create_app(Settings(mongodb_uri="mongodb://unused", linq_webhook_secret=None, openai_api_key="", linq_api_token=""), repo)
    with TestClient(app) as client:
        response = client.post("/signup/intake", json={
            "patient_chat_id": "patient-1",
            "caregiver_chat_id": "caregiver-1",
            "patient_name": "Maggie",
            "caregiver_name": "Susan",
            "caregiver_relationship": "daughter",
            "patient_notes": "Use short replies.",
            "caregiver_notes": "Susan is source of truth for appointments.",
            "doctors": [{"name": "Sarah", "role": "doctor"}],
            "important_people": [{"name": "Dr. Chen", "relationship": "cardiologist"}],
            "daily_routines": [{"task": "morning medication", "time": "08:00", "recurrence": "daily"}],
            "upcoming_events": [{"name": "Checkup", "starts_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()}],
            "safety_notes": ["Do not provide medical advice."],
            "communication_preferences": ["Use short, simple replies."],
        })
        body = response.json()
        assert body["patient_id"] == "patient-1"
        assert repo.get_person_by_relationship("patient-1", "doctor")["name"] == "Sarah"
        assert repo.get_person_by_relationship("patient-1", "daughter")["name"] == "Susan"
        assert repo.get_reminders("patient-1", datetime.now(timezone.utc) - timedelta(days=1), datetime.now(timezone.utc) + timedelta(days=2))
    assistant = CareAssistant(repo, None, "America/New_York")
    assert asyncio.run(assistant.respond("patient-1", "Who is my doctor?")) == "Your doctor is Sarah."


def test_example_intake_is_loaded_once_at_startup(repo):
    settings = Settings(mongodb_uri="mongodb://unused", linq_webhook_secret=None, openai_api_key="", linq_api_token="")
    app = create_app(settings, repo)
    with TestClient(app):
        patient_id = settings.default_patient_chat_id
        assert {context["role"] for context in repo.get_context_files(patient_id)} == {"patient", "caregiver"}
        assert repo.get_person_by_relationship(patient_id, "doctor")["name"] == "Sarah"
        assert repo.get_context_for_role(patient_id, "patient")["facts"]["intake_source_digest"]
    before = repo.db.reminders.count_documents({"recipient_id": settings.default_patient_chat_id})
    with TestClient(app):
        assert repo.db.reminders.count_documents({"recipient_id": settings.default_patient_chat_id}) == before


def test_patient_cannot_change_authoritative_person_fact(repo):
    repo.add_person("patient-1", "Susan", "daughter")
    assistant = CareAssistant(repo, None, "America/New_York")
    reply = asyncio.run(assistant.respond("patient-1", "Susan is my neighbor.", memory_id="patient-1", can_write_personal_facts=False))
    assert "caregiver" in reply
    assert repo.get_person("patient-1", "Susan")["relationship"] == "daughter"


def test_patient_can_create_natural_language_reminder(repo):
    assistant = CareAssistant(repo, None, "America/New_York")
    reply = asyncio.run(assistant.respond("patient-1", "reminder for appointment at 5"))
    assert "appointment" in reply.lower()
    assert repo.get_reminders("patient-1", datetime.now(timezone.utc) - timedelta(days=1), datetime.now(timezone.utc) + timedelta(days=2))


def test_caregiver_people_endpoint_saves_to_linked_patient_memory(repo):
    app = create_app(Settings(mongodb_uri="mongodb://unused", linq_webhook_secret=None, openai_api_key="", linq_api_token=""), repo)
    with TestClient(app) as client:
        client.post("/caregiver/link", json={"patient_chat_id": "patient-1", "caregiver_chat_id": "caregiver-1"})
        response = client.post("/caregiver/people", json={"recipient_id": "caregiver-1", "name": "Susan", "relationship": "daughter"})
        assert response.json() == {"name": "Susan", "relationship": "daughter"}
        assert repo.get_person("patient-1", "Susan")["relationship"] == "daughter"
        assert client.get("/caregiver/people/caregiver-1").json()["people"][0]["relationship"] == "daughter"
        check = client.get("/caregiver/memory-check/caregiver-1").json()
        assert check["memory_id"] == "patient-1"
        assert check["people"][0]["name"] == "Susan"


def test_linked_caregiver_text_saves_and_patient_text_recalls_same_memory(repo):
    app = create_app(Settings(mongodb_uri="mongodb://unused", linq_webhook_secret=None, openai_api_key="", linq_api_token=""), repo)
    with TestClient(app) as client:
        client.post("/caregiver/link", json={"patient_chat_id": "patient-1", "caregiver_chat_id": "caregiver-1"})
        caregiver_payload = {"message_id": "event-1", "sender_id": "caregiver-1", "text": "Susan is my daughter."}
        patient_payload = {"message_id": "event-2", "sender_id": "patient-1", "text": "Who is Susan?"}
        assert client.post("/webhooks/linq", json=caregiver_payload).json() == {"status": "sent"}
        assert client.post("/webhooks/linq", json=patient_payload).json() == {"status": "sent"}
        assert repo.get_person("patient-1", "Susan")["relationship"] == "daughter"


def test_linked_caregiver_can_save_patient_fact_in_third_person(repo):
    app = create_app(Settings(mongodb_uri="mongodb://unused", linq_webhook_secret=None, openai_api_key="", linq_api_token=""), repo)
    with TestClient(app) as client:
        client.post("/caregiver/link", json={"patient_chat_id": "patient-1", "caregiver_chat_id": "caregiver-1"})
        caregiver_payload = {"message_id": "event-1", "sender_id": "caregiver-1", "text": "Maggie's doctor is Dr. Chen."}
        patient_payload = {"message_id": "event-2", "sender_id": "patient-1", "text": "Who is my doctor?"}
        assert client.post("/webhooks/linq", json=caregiver_payload).json() == {"status": "sent"}
        assert client.post("/webhooks/linq", json=patient_payload).json() == {"status": "sent"}
        assert repo.get_person("patient-1", "Dr. Chen")["relationship"] == "doctor"
        assert repo.get_person_by_relationship("patient-1", "doctor")["name"] == "Dr. Chen"
        replies = [item["text"] for item in repo.recent_conversation("caregiver-1") if item["role"] == "assistant"]
        assert replies == ["I'll remember that Dr. Chen is the patient's doctor."]


def test_linked_caregiver_pronoun_fact_saves_to_patient_memory(repo):
    app = create_app(Settings(mongodb_uri="mongodb://unused", linq_webhook_secret=None, openai_api_key="", linq_api_token=""), repo)
    with TestClient(app) as client:
        client.post("/caregiver/link", json={"patient_chat_id": "patient-1", "caregiver_chat_id": "caregiver-1"})
        caregiver_payload = {"message_id": "event-1", "sender_id": "caregiver-1", "text": "Her doctor is Sarah."}
        patient_payload = {"message_id": "event-2", "sender_id": "patient-1", "text": "Who is my doctor?"}
        assert client.post("/webhooks/linq", json=caregiver_payload).json() == {"status": "sent"}
        assert client.post("/webhooks/linq", json=patient_payload).json() == {"status": "sent"}
        assert repo.get_person_by_relationship("patient-1", "doctor")["name"] == "Sarah"
        assert repo.get_person_by_relationship("caregiver-1", "doctor") is None


def test_linked_caregiver_inverse_patient_fact_saves_to_patient_memory(repo):
    app = create_app(Settings(mongodb_uri="mongodb://unused", linq_webhook_secret=None, openai_api_key="", linq_api_token=""), repo)
    with TestClient(app) as client:
        client.post("/caregiver/link", json={"patient_chat_id": "patient-1", "caregiver_chat_id": "caregiver-1"})
        caregiver_payload = {"message_id": "event-1", "sender_id": "caregiver-1", "text": "Sarah is patients doctor."}
        patient_payload = {"message_id": "event-2", "sender_id": "patient-1", "text": "Who is my doctor?"}
        assert client.post("/webhooks/linq", json=caregiver_payload).json() == {"status": "sent"}
        assert client.post("/webhooks/linq", json=patient_payload).json() == {"status": "sent"}
        assert repo.get_person_by_relationship("patient-1", "doctor")["name"] == "Sarah"
        assert repo.get_person_by_relationship("caregiver-1", "doctor") is None


def test_linked_caregiver_relationship_lookup_uses_patient_wording(repo):
    assistant = CareAssistant(repo, None, "America/New_York")
    asyncio.run(assistant.respond("caregiver-1", "Maggie's doctor is Dr. Chen.", memory_id="patient-1"))
    assert asyncio.run(assistant.respond("caregiver-1", "Who is Maggie's doctor?", memory_id="patient-1")) == "The patient's doctor is Dr. Chen."
