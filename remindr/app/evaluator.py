from typing import Literal

from pydantic import BaseModel, Field, model_validator


RoutineIntent = Literal[
    "MEDICATION_CONFIRMATION",
    "SCHEDULE_QUERY",
    "IDENTITY_QUERY",
    "CASUAL_CHAT",
    "DISTRESS_CALL",
    "OTHER",
]


class AgentEvaluation(BaseModel):
    reply_text: str = Field(min_length=1, max_length=500)
    confusion_score: float = Field(ge=0.0, le=1.0)
    confusion_flag: bool
    routine_intent: RoutineIntent
    caregiver_alert_message: str | None = None

    @model_validator(mode="after")
    def validate_alert_invariants(self) -> "AgentEvaluation":
        if self.confusion_flag != (self.confusion_score >= 0.60):
            raise ValueError("confusion_flag must match confusion_score >= 0.60")
        if self.confusion_flag and not self.caregiver_alert_message:
            raise ValueError("caregiver_alert_message is required when confusion_flag is true")
        if not self.confusion_flag and self.caregiver_alert_message is not None:
            raise ValueError("caregiver_alert_message must be null when confusion_flag is false")
        return self


class MessageEvaluator:
    """Classifies patient messages before/alongside normal assistant handling."""

    def evaluate(self, text: str, patient_profile: dict) -> AgentEvaluation:
        normalized = text.strip().casefold()
        patient_name = (patient_profile.get("patient_name") or "the patient").strip()
        caregiver_name = (patient_profile.get("primary_caregiver") or "the caregiver").strip()

        intent: RoutineIntent = "OTHER"
        score = 0.10
        reply = "I hear you."

        if any(token in normalized for token in ("done", "took my medicine", "took medication", "took my meds")):
            intent = "MEDICATION_CONFIRMATION"
            score = 0.10
            reply = "Thank you. I marked that down."
        elif any(token in normalized for token in ("today", "schedule", "appointment", "what am i doing")):
            intent = "SCHEDULE_QUERY"
            score = 0.20
            reply = "I can help check your schedule."
        elif any(token in normalized for token in ("who am i", "what is my name", "whats my name", "what’s my name", "what’s my nam", "who is me")):
            intent = "IDENTITY_QUERY"
            score = 0.65
            reply = f"You're {patient_name}. You're safe, and I'm here with you."
        elif any(token in normalized for token in ("who is my", "who's my", "what is my name", "who is ")) and "?" in text:
            intent = "IDENTITY_QUERY"
            score = 0.45
            reply = "I can help with that."
        elif any(token in normalized for token in ("i want to go home", "where am i", "i'm lost", "im lost", "help me", "scared", "afraid")):
            intent = "DISTRESS_CALL"
            score = 0.85
            reply = f"You're not alone. Please stay where you are, and I'll let {caregiver_name} know."
        elif any(token in normalized for token in ("hi", "hello", "thanks", "thank you")):
            intent = "CASUAL_CHAT"
            score = 0.05
            reply = "Hi. I'm here with you."

        flag = score >= 0.60
        alert = None
        if flag:
            alert = f"{patient_name} may need a check-in. Message: \"{text.strip()}\""
        return AgentEvaluation(
            reply_text=reply,
            confusion_score=score,
            confusion_flag=flag,
            routine_intent=intent,
            caregiver_alert_message=alert,
        )
