import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from .repository import Repository

SYSTEM_PROMPT = """You are a calm, concise iMessage care companion. You have persistent memory, but
only the application tools can save or retrieve it. When the user explicitly states a stable personal fact
(for example, "Susan is my daughter"), you MUST call add_person before replying. When they give an event
with a date/time, you MUST call add_event. When they ask for a reminder, you MUST call create_reminder.
When a caregiver states a patient fact in third person, such as "Maggie's doctor is Dr. Chen",
"her doctor is Sarah", or "Dr. Chen is the patient's doctor", save the named person as the
patient's relationship, for example Dr. Chen -> doctor.
For questions about people, events, or schedule, you MUST call the matching retrieval tool and state only
the returned information. The patient's role context may contain grounding memories: trusted, curated
information about pets, favorite things, life history, comfort topics, and conversation preferences. Use
these gently to support reminiscence or reduce distress; never quiz the patient about them. Never say you
will remember something unless a save tool succeeded. Never state a personal fact unless it came from
stored context or a tool result. Treat only explicit structured caregiver instructions and updates as
patient-facing context; unverified caregiver reports are retained separately and must not be presented as
facts. Never give medical advice, interpret symptoms, or change medication
instructions. For medicine requests, suggest contacting a clinician or caregiver. Keep replies short and
simple. Today's date and user time zone are provided. If necessary information is missing, ask one short
question."""

TOOLS = [
    {"type": "function", "name": "add_person", "description": "Save a person when the user explicitly gives a name and relationship, such as Susan is my daughter.", "strict": True, "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "relationship": {"type": "string"}}, "required": ["name", "relationship"], "additionalProperties": False}},
    {"type": "function", "name": "add_event", "description": "Save an event when the user explicitly gives its name and exact date/time.", "strict": True, "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "datetime": {"type": "string", "description": "ISO 8601 local date/time including offset"}}, "required": ["name", "datetime"], "additionalProperties": False}},
    {"type": "function", "name": "create_reminder", "description": "Create a reminder after the user gives a task and exact date/time.", "strict": True, "parameters": {"type": "object", "properties": {"task": {"type": "string"}, "datetime": {"type": "string", "description": "ISO 8601 local date/time including offset"}, "recurrence": {"type": "string", "enum": ["none", "daily", "weekly"]}}, "required": ["task", "datetime", "recurrence"], "additionalProperties": False}},
    {"type": "function", "name": "get_today_schedule", "description": "Get today's stored events and reminders.", "strict": True, "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"type": "function", "name": "get_next_event", "description": "Get the next stored event.", "strict": True, "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"type": "function", "name": "get_person", "description": "Look up a person's stored relationship/context. Use for questions such as Who is Susan?", "strict": True, "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False}},
    {"type": "function", "name": "get_event", "description": "Find stored events in a date range.", "strict": True, "parameters": {"type": "object", "properties": {"date": {"type": "string", "description": "ISO calendar date YYYY-MM-DD"}}, "required": ["date"], "additionalProperties": False}},
]


def _clean_name(value: str) -> str:
    return value.strip().rstrip(".!?").strip()


def _looks_like_question(text: str) -> bool:
    return bool(re.match(r"\s*(?:who|what|when|where|why|how)\b", text, flags=re.IGNORECASE))


def _extract_person_lookup(text: str) -> str | None:
    patterns = [
        r"\s*(?:who\s+is|who's)\s+(?P<name>.+?)\s*[?!.]*\s*",
        r"\s*(?:what\s+do\s+you\s+know\s+about|tell\s+me\s+about|do\s+you\s+remember)\s+(?P<name>.+?)\s*[?!.]*\s*",
    ]
    for pattern in patterns:
        match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
        if match:
            name = _clean_name(match.group("name"))
            return name or None
    return None


def _extract_relationship_lookup(text: str) -> str | None:
    patterns = [
        r"\s*(?:who\s+is|who's)\s+(?:my|her|his|their|the\s+patient's|patients?|[A-Za-z][A-Za-z .'-]{0,117}(?:'s|’s))\s+(?P<relationship>[A-Za-z][A-Za-z -]{0,198})\s*[?!.]*\s*",
        r"\s*(?:what\s+is|what's)\s+(?:my|her|his|their|the\s+patient's|patients?)\s+(?P<relationship>[A-Za-z][A-Za-z -]{0,198}?)\s*(?:name)?\s*[?!.]*\s*",
        r"\s*(?:what\s+is|what's)\s+the\s+name\s+of\s*(?:my|her|his|their|the\s+patient's|patients?)\s+(?P<relationship>[A-Za-z][A-Za-z -]{0,198})\s*[?!.]*\s*",
    ]
    for pattern in patterns:
        match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
        if match:
            relationship = match.group("relationship").strip().casefold()
            relationship = re.sub(r"\s+name$", "", relationship).strip()
            return relationship or None
    return None


def _is_self_name_query(text: str) -> bool:
    """Recognize direct identity questions without depending on model tool selection."""
    return bool(re.fullmatch(
        r"\s*(?:who\s+am\s+i|what(?:'s|’s|\s+is)\s+(?:my\s+)?nam(?:e)?)\s*[?!.]*\s*",
        text,
        flags=re.IGNORECASE,
    ))


def _is_self_profile_query(text: str) -> bool:
    return bool(re.fullmatch(
        r"\s*(?:what\s+do\s+you\s+know\s+about|tell\s+me\s+about|do\s+you\s+remember)\s+me\s*[?!.]*\s*",
        text,
        flags=re.IGNORECASE,
    ))


def _self_profile_response(context: dict | None) -> str:
    facts = (context or {}).get("facts") or {}
    patient_name = facts.get("patient_name")
    if not patient_name:
        return "I don't have information about you saved yet."
    details = [f"Your name is {patient_name}."]
    if facts.get("primary_caregiver"):
        details.append(f"Your caregiver is {facts['primary_caregiver']}.")
    if (context or {}).get("notes"):
        details.append(str(context["notes"]).strip())
    return " ".join(details)


def _grounding_pet_response(context: dict | None, text: str) -> str | None:
    """Offer a gentle reminiscence prompt without inventing a pet's current status."""
    facts = (context or {}).get("facts") or {}
    grounding = facts.get("grounding_memories") or {}
    normalized = text.casefold()
    if not any(phrase in normalized for phrase in ("where is", "where's", "wheres")):
        return None
    for pet in grounding.get("pets") or []:
        name = str(pet.get("name") or "").strip()
        if name and re.search(rf"\b{re.escape(name.casefold())}\b", normalized):
            return (
                f"{name} was very loved. I know you shared many special moments together. "
                f"Would you like to tell me a favorite memory of {name}?"
            )
    return None


def _grounding_memory_response(context: dict | None, text: str) -> str | None:
    """Resolve first-try questions about any pet saved in grounding memories."""
    facts = (context or {}).get("facts") or {}
    grounding = facts.get("grounding_memories") or {}
    match = re.fullmatch(
        r"\s*(?:tell\s+me\s+about\s+)?(?:my\s+)?(?P<subject>[A-Za-z][A-Za-z -]{0,80})\s*[?!.]*\s*",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    subject = match.group("subject").strip().casefold()
    # Avoid intercepting ordinary sentences; this is a concise noun-phrase query.
    if subject.startswith(("what ", "who ", "where ", "can ", "should ")):
        return None
    for pet in grounding.get("pets") or []:
        name = str(pet.get("name") or "").strip()
        pet_type = str(pet.get("type") or "").strip()
        if subject not in {name.casefold(), pet_type.casefold(), f"my {pet_type.casefold()}"}:
            continue
        details = str(pet.get("details") or "").strip().rstrip(".")
        if details:
            details = details[:1].lower() + details[1:]
            return f"{name} was your {details}."
        return f"{name} was an important {pet_type} in your life."
    return None


def _extract_caregiver_loss_update(text: str, role: str) -> tuple[str, str] | None:
    if role != "caregiver":
        return None
    match = re.search(
        r"(?:my|(?:[A-Za-z][A-Za-z .'-]{0,117})(?:'s|’s)|the\s+patient(?:'s)?)\s+"
        r"(?P<subject>[A-Za-z][A-Za-z -]{0,80}?)\s+"
        r"(?P<update>passed\s+away(?:\s+recently)?|died(?:\s+recently)?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group("subject").strip(), match.group("update").strip()


def _caregiver_update_response(context: dict | None, text: str) -> str | None:
    facts = (context or {}).get("facts") or {}
    normalized = text.casefold()
    for item in facts.get("caregiver_updates") or []:
        subject = str(item.get("subject") or "").strip()
        update = str(item.get("update") or "").casefold()
        if subject and re.search(rf"\b{re.escape(subject)}\b", normalized) and ("passed away" in update or "died" in update):
            return (
                f"I'm so sorry. Your {subject} passed away recently. "
                "They were very loved. Would you like to share a favorite memory?"
            )
    return None


def _extract_dietary_restriction(text: str, role: str) -> str | None:
    if role != "caregiver":
        return None
    match = re.search(
        r"\b(?:cannot|can't|cant|shouldn't|should not|must not|do not|don't)\s+(?:eat|have)\s+(?P<restriction>[^.!?]{2,180})",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group("restriction").strip()
    # Explicit caregiver "avoid" instructions remain valid even when casual
    # spelling mistakes occur before the word (for example, "shoukd avoid").
    match = re.search(r"\bavoid\s+(?:all\s+)?(?P<restriction>[^.!?]{2,180})", text, flags=re.IGNORECASE)
    return match.group("restriction").strip() if match else None


def _extract_dietary_guidance(text: str, role: str) -> str | None:
    if role != "caregiver":
        return None
    match = re.search(r"\bshould\s+(?:have|eat|include)\s+(?P<guidance>[^.!?]{2,180})", text, flags=re.IGNORECASE)
    return match.group("guidance").strip() if match else None


def _food_safety_response(context: dict | None, text: str) -> str | None:
    facts = (context or {}).get("facts") or {}
    restrictions = facts.get("dietary_restrictions") or []
    guidance = facts.get("dietary_guidance") or []
    if not restrictions and not guidance:
        return None
    normalized = text.casefold()
    food_question = any(token in normalized for token in (
        "what should i have", "what should i eat", "can i have", "can i eat", "am i able to eat",
        "what about", "mcdonald", "burger king", "food", "meal", " eat ", " have ",
    ))
    if not food_question:
        return None
    details = []
    if restrictions:
        details.append(f"avoid {restrictions[-1].get('restriction')}")
    if guidance:
        details.append(f"include {guidance[-1].get('guidance')}")
    instruction = " and ".join(details)
    return (
        "I know choosing what to eat can feel frustrating. "
        f"Sameha asked me to help you {instruction}. "
        "Let's check with Sameha or your clinician about what would feel good today."
    )


def _owner_pattern(role: str) -> str:
    if role == "caregiver":
        return r"(?:my|our|the\s+patient's|patients?|her|his|their|[A-Za-z][A-Za-z .'-]{0,117}(?:'s|’s))"
    return r"(?:my|our)"


def _extract_person_fact(text: str, role: str = "patient") -> tuple[str, str] | None:
    if _looks_like_question(text):
        return None
    owner = _owner_pattern(role)
    owned_person = re.fullmatch(
        rf"\s*(?P<owner>{owner})\s+(?P<relationship>[A-Za-z][A-Za-z -]{{0,198}}?)\s+is\s+(?P<name>[A-Za-z][A-Za-z .'-]{{0,117}})\s*[.!]*\s*",
        text,
        flags=re.IGNORECASE,
    )
    if owned_person:
        name = _clean_name(owned_person.group("name"))
        if name.casefold() in {"who", "what", "when", "where", "why", "how"}:
            return None
        return name, owned_person.group("relationship").strip().casefold()

    person_owned = re.fullmatch(
        rf"\s*(?P<name>[A-Za-z][A-Za-z .'-]{{0,117}})\s+is\s+(?P<owner>{owner})\s+(?P<relationship>[A-Za-z][A-Za-z -]{{0,198}})\s*[.!]*\s*",
        text,
        flags=re.IGNORECASE,
    )
    if person_owned:
        name = _clean_name(person_owned.group("name"))
        if name.casefold() in {"who", "what", "when", "where", "why", "how"}:
            return None
        return name, person_owned.group("relationship").strip().casefold()

    return None


def _context_relationship_answer(context: dict | None, relationship: str, role: str) -> str | None:
    if not context:
        return None
    facts = context.get("facts") or {}
    if relationship == "caregiver" and facts.get("primary_caregiver"):
        owner = "The patient's" if role == "caregiver" else "Your"
        return f"{owner} caregiver is {facts['primary_caregiver']}."
    return None


class CareAssistant:
    def __init__(self, repo: Repository, api_key: str | None, timezone_name: str):
        self.repo, self.client, self.tz = repo, AsyncOpenAI(api_key=api_key) if api_key else None, ZoneInfo(timezone_name)

    async def respond(self, recipient_id: str, text: str, *, memory_id: str | None = None, can_write_personal_facts: bool = True) -> str:
        memory_id = memory_id or recipient_id
        role = "caregiver" if can_write_personal_facts and memory_id != recipient_id else "patient"
        context = self.repo.get_context_for_role(memory_id, role) or self.repo.get_context_for_role(memory_id, "patient")
        if text.strip().casefold() == "done":
            reminder = self.repo.acknowledge_latest(recipient_id)
            return "Thank you — I marked it done." if reminder else "I don't see a reminder waiting for confirmation."
        if _is_self_name_query(text):
            patient_name = (context or {}).get("facts", {}).get("patient_name")
            if patient_name:
                return f"Your name is {patient_name}."
            return "I don't have your name saved yet."
        if _is_self_profile_query(text):
            return _self_profile_response(context)
        caregiver_loss = _extract_caregiver_loss_update(text, role)
        if caregiver_loss and can_write_personal_facts:
            subject, update = caregiver_loss
            self.repo.add_caregiver_update(memory_id, subject, update)
            return "I'm sorry. I'll remember this and respond gently if Maggie asks."
        dietary_restriction = _extract_dietary_restriction(text, role)
        if dietary_restriction and can_write_personal_facts:
            self.repo.add_dietary_restriction(memory_id, dietary_restriction)
            return f"Understood. I'll remember to avoid suggesting {dietary_restriction}."
        dietary_guidance = _extract_dietary_guidance(text, role)
        if dietary_guidance and can_write_personal_facts:
            self.repo.add_dietary_guidance(memory_id, dietary_guidance)
            return f"Understood. I'll remember Maggie should have {dietary_guidance}."
        food_safety = _food_safety_response(context, text)
        if food_safety:
            return food_safety
        caregiver_update = _caregiver_update_response(context, text)
        if caregiver_update:
            return caregiver_update
        grounding_response = _grounding_memory_response(context, text)
        if grounding_response:
            return grounding_response
        pet_response = _grounding_pet_response(context, text)
        if pet_response:
            return pet_response
        # Friendly patient-facing reminder shorthand. If no AM/PM is supplied,
        # interpret a bare afternoon/evening clock time as PM; the full LLM
        # tool path remains available for more complex recurrence/date requests.
        reminder_match = re.fullmatch(
            r"\s*(?:remind\s+me|reminder)\s*(?:to|about|for)?\s*(?P<task>.+?)\s+(?:on\s+)?(?P<day>today|tomorrow)?\s*at\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?\s*[.!?]*\s*",
            text,
            flags=re.IGNORECASE,
        )
        if reminder_match:
            hour = int(reminder_match.group("hour"))
            minute = int(reminder_match.group("minute") or 0)
            ampm = (reminder_match.group("ampm") or "pm").casefold()
            if hour < 1 or hour > 12 or minute > 59:
                return "Please give me a valid time, such as 5 PM."
            hour = hour % 12 + (12 if ampm == "pm" else 0)
            day = (reminder_match.group("day") or "today").casefold()
            now = datetime.now(self.tz)
            due_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if day == "tomorrow" or due_at <= now:
                due_at += timedelta(days=1)
            task = reminder_match.group("task").strip()
            if task.lower().startswith("to "):
                task = task[3:].strip()
            if task:
                self.repo.add_reminder(memory_id, task, due_at.astimezone(timezone.utc), "none")
                return f"Okay — I'll remind you about {task} at {due_at.strftime('%-I:%M %p')}."
        # This high-confidence memory query is deterministic: personal facts
        # are never answered from model knowledge or skipped by tool selection.
        relationship = _extract_relationship_lookup(text)
        if relationship:
            person = self.repo.get_person_by_relationship(memory_id, relationship)
            if person:
                owner = "The patient's" if role == "caregiver" else "Your"
                return f"{owner} {person['relationship']} is {person['name']}."
            context_answer = _context_relationship_answer(context, relationship, role)
            if context_answer:
                return context_answer
            return f"I don't have a saved {relationship}."
        name = _extract_person_lookup(text)
        if name:
            person = self.repo.get_person(memory_id, name)
            if person:
                owner = "the patient's" if role == "caregiver" else "your"
                return f"{person['name']} is {owner} {person['relationship']}."
            return f"I don't have saved information about {name}."
        # Capture the most common personal-memory statement before asking the
        # model. This guarantees persistence even if a model emits prose rather
        # than the required add_person function call.
        person_fact = _extract_person_fact(text, role)
        if person_fact and can_write_personal_facts:
            name, relationship = person_fact
            self.repo.add_person(memory_id, name, relationship)
            owner = "the patient's" if role == "caregiver" else "your"
            return f"I'll remember that {name} is {owner} {relationship}."
        if person_fact and not can_write_personal_facts:
            return "I can't change personal facts from this chat. Please ask your caregiver to update that information."
        if not self.client:
            return "Messaging is connected, but the assistant needs an OpenAI API key to answer requests."

        now = datetime.now(self.tz)
        instructions = f"{SYSTEM_PROMPT}\nCurrent local time: {now.isoformat()}. Time zone: {self.tz.key}."
        if context:
            instructions += f"\nRole context from database: {json.dumps(context, default=str)}"
        history = [
            {"role": item["role"], "content": item["text"]}
            for item in self.repo.recent_conversation(recipient_id)
            if item["role"] in {"user", "assistant"}
        ]
        model_input = [*history, {"role": "user", "content": text}]
        available_tools = TOOLS if can_write_personal_facts else [tool for tool in TOOLS if tool["name"] not in {"add_person", "add_event"}]
        response = await self.client.responses.create(
            model="gpt-5.6-luna", instructions=instructions,
            input=model_input, tools=available_tools, store=False,
        )
        # The model only proposes function arguments. This application executes, validates, and returns outputs.
        tool_outputs = []
        for item in response.output:
            if item.type != "function_call":
                continue
            try:
                result = self._execute_tool(recipient_id, item.name, json.loads(item.arguments), memory_id, can_write_personal_facts)
            except (ValueError, KeyError, TypeError) as exc:
                result = {"error": f"Request could not be completed: {exc}"}
            tool_outputs.append({"type": "function_call_output", "call_id": item.call_id, "output": json.dumps(result, default=str)})
        if not tool_outputs:
            return response.output_text or "Sorry, I couldn't understand that."
        # A function-call output must be sent alongside the model's original
        # function-call item. This preserves the call_id relationship while
        # keeping the API stateless (`store=False`).
        final = await self.client.responses.create(
            model="gpt-5.6-luna",
            instructions=instructions,
            input=[*model_input, *response.output, *tool_outputs],
            tools=available_tools,
            store=False,
        )
        return final.output_text or "Done."

    def _execute_tool(self, recipient_id: str, name: str, args: dict, memory_id: str | None = None, can_write_personal_facts: bool = True) -> dict:
        memory_id = memory_id or recipient_id
        now = datetime.now(self.tz)
        if name == "add_person":
            if not can_write_personal_facts:
                raise ValueError("only a linked caregiver can update personal facts")
            person_name = args["name"].strip()
            relationship = args["relationship"].strip()
            if not person_name or not relationship or len(person_name) > 120 or len(relationship) > 200:
                raise ValueError("invalid person details")
            self.repo.add_person(memory_id, person_name, relationship)
            return {"saved": True, "name": person_name, "relationship": relationship}
        if name == "add_event":
            if not can_write_personal_facts:
                raise ValueError("only a linked caregiver can update events")
            starts_at = datetime.fromisoformat(args["datetime"])
            if starts_at.tzinfo is None:
                starts_at = starts_at.replace(tzinfo=self.tz)
            if starts_at <= now:
                raise ValueError("the event time must be in the future")
            event_name = args["name"].strip()
            if not event_name or len(event_name) > 250:
                raise ValueError("invalid event name")
            self.repo.add_event(memory_id, event_name, starts_at.astimezone(timezone.utc))
            return {"saved": True, "name": event_name, "datetime": starts_at.isoformat()}
        if name == "create_reminder":
            due_at = datetime.fromisoformat(args["datetime"])
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=self.tz)
            if due_at <= now:
                raise ValueError("the reminder time must be in the future")
            task = args["task"].strip()
            if not task or len(task) > 500:
                raise ValueError("invalid reminder task")
            doc = self.repo.add_reminder(memory_id, task, due_at.astimezone(timezone.utc), args["recurrence"])
            return {"created": True, "task": doc["task"], "due_at": due_at.isoformat(), "recurrence": doc["recurrence"]}
        if name == "get_person":
            person = self.repo.get_person(memory_id, args["name"])
            if person:
                return {"person": person}
            by_relationship = self.repo.get_person_by_relationship(memory_id, args["name"])
            return {"person": by_relationship} if by_relationship else {"person": None, "message": "No stored information for that person."}
        if name == "get_today_schedule":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            return {"events": self.repo.get_events(memory_id, start.astimezone(timezone.utc), end.astimezone(timezone.utc)), "reminders": self.repo.get_reminders(memory_id, start.astimezone(timezone.utc), end.astimezone(timezone.utc))}
        if name == "get_next_event":
            return {"event": self.repo.next_event(memory_id, now.astimezone(timezone.utc))}
        if name == "get_event":
            start = datetime.fromisoformat(args["date"]).replace(tzinfo=self.tz)
            end = start + timedelta(days=1)
            return {"events": self.repo.get_events(memory_id, start.astimezone(timezone.utc), end.astimezone(timezone.utc))}
        raise ValueError("unsupported action")
