import asyncio

import httpx

from .config import Settings


class LinqClient:
    """Linq Partner API v3 adapter. Recipient IDs are Linq chat IDs after inbound receipt."""
    def __init__(self, settings: Settings):
        self.base_url = settings.linq_api_base_url
        self.token = settings.linq_api_token
        self.max_attempts = settings.linq_send_max_attempts

    async def send_message(self, recipient_id: str, text: str) -> None:
        if not self.base_url or not self.token:
            # Development-safe: no accidental messages when credentials are absent.
            print(f"[LINQ DRY RUN] to={recipient_id}: {text}")
            return
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            # Linq webhooks may represent a chat as a raw UUID or a resource
            # path (including nested `chats/chats/<id>` forms). The endpoint
            # takes only the final chat identifier.
            chat_id = recipient_id.rstrip("/").rsplit("/", 1)[-1]
            if not chat_id:
                raise ValueError("Linq webhook did not include a valid chat ID")
            for attempt in range(self.max_attempts):
                try:
                    response = await client.post(
                        f"/chats/{chat_id}/messages",
                        headers={"Authorization": f"Bearer {self.token}"},
                        json={"message": {"parts": [{"type": "text", "value": text}]}},
                    )
                    if response.status_code < 500 and response.status_code != 429:
                        response.raise_for_status()
                        return
                    if attempt == self.max_attempts - 1:
                        response.raise_for_status()
                    delay = self._retry_delay(response, attempt)
                except httpx.TransportError:
                    if attempt == self.max_attempts - 1:
                        raise
                    delay = 2**attempt
                await asyncio.sleep(delay)

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        """Honor Linq's Retry-After guidance for 429; cap to keep the worker responsive."""
        if response.status_code == 429:
            try:
                return min(float(response.headers.get("Retry-After", "1")), 60)
            except ValueError:
                pass
        return float(2**attempt)
