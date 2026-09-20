from datetime import datetime, timezone

from .linq import LinqClient
from .repository import Repository


class ReminderScheduler:
    def __init__(self, repo: Repository, linq: LinqClient):
        self.repo, self.linq = repo, linq

    async def deliver_due(self) -> None:
        for reminder in self.repo.claim_due_reminders(datetime.now(timezone.utc)):
            try:
                await self.linq.send_message(reminder["recipient_id"], f"⏰ It's time for {reminder['task']}. Reply DONE when you've done it.")
                self.repo.mark_sent(reminder["_id"])
            except Exception:
                self.repo.release_claim(reminder["_id"])
                raise
