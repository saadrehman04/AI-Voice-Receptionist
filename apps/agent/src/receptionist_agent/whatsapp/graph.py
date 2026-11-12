"""Meta's Graph API, the three calls we need: answer a call, end it, send a text.

Ours rather than Pipecat's `WhatsAppApi` because the base URL must be configurable (the local
Meta simulator stands in for graph.facebook.com in tests and demos) and because one app serves
every business, so the phone number id is per request, not per client.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

AnswerAction = Literal["pre_accept", "accept"]


class GraphError(RuntimeError):
    """Meta refused a request; `code` is Meta's error code when it sent one."""

    def __init__(self, message: str, *, status: int, code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class GraphApi:
    def __init__(self, base_url: str, token: str, *, client: httpx.AsyncClient | None = None):
        self._base = base_url.rstrip("/")
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{self._base}/{path}",
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise GraphError(f"Graph API unreachable: {type(exc).__name__}", status=0) from exc
        data: dict[str, Any] = response.json() if response.content else {}
        if response.status_code >= 400:
            raw_error = data.get("error")
            error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
            raise GraphError(
                str(error.get("message") or f"Graph API answered {response.status_code}"),
                status=response.status_code,
                code=error.get("code"),
            )
        return data

    async def answer_call(
        self, phone_number_id: str, call_id: str, action: AnswerAction, sdp: str
    ) -> None:
        """`pre_accept` sets up media early (no clipped first words), then `accept` connects.
        Both must carry the same SDP answer."""
        data = await self._post(
            f"{phone_number_id}/calls",
            {
                "messaging_product": "whatsapp",
                "call_id": call_id,
                "action": action,
                "session": {"sdp_type": "answer", "sdp": sdp},
            },
        )
        if not data.get("success"):
            raise GraphError(f"{action} was not acknowledged", status=200)

    async def reject_call(self, phone_number_id: str, call_id: str) -> None:
        await self._post(
            f"{phone_number_id}/calls",
            {"messaging_product": "whatsapp", "call_id": call_id, "action": "reject"},
        )

    async def terminate_call(self, phone_number_id: str, call_id: str) -> None:
        """Meta asks for this even when the media already stopped, so billing is exact."""
        await self._post(
            f"{phone_number_id}/calls",
            {"messaging_product": "whatsapp", "call_id": call_id, "action": "terminate"},
        )

    async def send_text(self, phone_number_id: str, to: str, body: str) -> str:
        """A plain text message (allowed: the caller's call opened a 24-hour service window).
        Returns Meta's message id."""
        data = await self._post(
            f"{phone_number_id}/messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": body},
            },
        )
        messages = data.get("messages") or [{}]
        return str(messages[0].get("id", ""))


__all__ = ["AnswerAction", "GraphApi", "GraphError"]
