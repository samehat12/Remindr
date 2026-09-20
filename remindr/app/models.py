from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class InboundMessage(BaseModel):
    message_id: str = Field(min_length=1, max_length=200)
    sender_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=4000)
    received_at: datetime | None = None


class CreateReminder(BaseModel):
    task: str = Field(min_length=1, max_length=500)
    due_at: datetime
    recurrence: Literal["none", "daily", "weekly"] = "none"
    recipient_id: str = Field(min_length=1, max_length=200)


class PersonInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    relationship: str = Field(min_length=1, max_length=200)
    recipient_id: str = Field(min_length=1, max_length=200)


class EventInput(BaseModel):
    name: str = Field(min_length=1, max_length=250)
    starts_at: datetime
    recipient_id: str = Field(min_length=1, max_length=200)


class OutboundMessage(BaseModel):
    recipient_id: str
    text: str


class CaregiverLinkInput(BaseModel):
    patient_chat_id: str = Field(min_length=1, max_length=200)
    caregiver_chat_id: str = Field(min_length=1, max_length=200)


class SignupContextInput(BaseModel):
    patient_chat_id: str = Field(min_length=1, max_length=200)
    caregiver_chat_id: str = Field(min_length=1, max_length=200)
    patient_name: str = Field(min_length=1, max_length=120)
    caregiver_name: str = Field(min_length=1, max_length=120)
    caregiver_relationship: str = Field(min_length=1, max_length=120)
    patient_notes: str = Field(default="", max_length=2000)
    caregiver_notes: str = Field(default="", max_length=4000)


class IntakePerson(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    relationship: str = Field(min_length=1, max_length=200)


class IntakeDoctor(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="doctor", min_length=1, max_length=120)


class IntakeRoutine(BaseModel):
    task: str = Field(min_length=1, max_length=500)
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    recurrence: Literal["daily", "weekly"] = "daily"


class IntakeEvent(BaseModel):
    name: str = Field(min_length=1, max_length=250)
    starts_at: datetime


class GroundingPet(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: str = Field(min_length=1, max_length=80)
    details: str = Field(default="", max_length=500)
    emotional_context: str = Field(default="", max_length=500)


class GroundingRelationship(BaseModel):
    person: str = Field(min_length=1, max_length=120)
    relationship: str = Field(min_length=1, max_length=120)
    details: str = Field(default="", max_length=500)


class GroundingMemories(BaseModel):
    """Curated positive life context for supportive reminiscence, not clinical advice."""
    pets: list[GroundingPet] = Field(default_factory=list, max_length=30)
    family_and_relationships: list[GroundingRelationship] = Field(default_factory=list, max_length=50)
    favorite_things: list[str] = Field(default_factory=list, max_length=50)
    life_history: list[str] = Field(default_factory=list, max_length=50)
    comfort_topics: list[str] = Field(default_factory=list, max_length=50)
    conversation_notes: list[str] = Field(default_factory=list, max_length=50)


class SignupIntakeInput(SignupContextInput):
    doctors: list[IntakeDoctor] = Field(default_factory=list, max_length=20)
    important_people: list[IntakePerson] = Field(default_factory=list, max_length=50)
    daily_routines: list[IntakeRoutine] = Field(default_factory=list, max_length=50)
    upcoming_events: list[IntakeEvent] = Field(default_factory=list, max_length=50)
    safety_notes: list[str] = Field(default_factory=list, max_length=50)
    communication_preferences: list[str] = Field(default_factory=list, max_length=50)
    grounding_memories: GroundingMemories = Field(default_factory=GroundingMemories)
