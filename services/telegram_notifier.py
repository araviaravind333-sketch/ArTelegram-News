"""Telegram delivery and command intake.

Implemented directly against the Bot API over ``requests``. That is a
deliberate choice over ``python-telegram-bot``: v20+ of that library is
async-only and pulls an event loop into every GitHub Actions step, while this
project only needs three endpoints (``sendMessage``, ``sendDocument``,
``getUpdates``). Synchronous calls keep CI logs readable and failures explicit.

Credentials are read from the environment at call time and never logged.
"""

from __future__ import annotations

import html
import logging
import time
from pathlib import Path
from typing import Any, Iterator

import requests

import config

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org/bot{token}/{method}"
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024


class TelegramError(RuntimeError):
    """Raised when the Bot API rejects a request."""


class TelegramNotifier:
    """Thin, synchronous Telegram Bot API client."""

    def __init__(self, token: str | None = None, chat_id: str | None = None):
        if token and chat_id:
            self.token, self.chat_id = token, chat_id
        else:
            self.token, self.chat_id = config.telegram_credentials()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})

    # -- plumbing ----------------------------------------------------------

    def _call(self, method: str, *, timeout: int = 30, **kwargs) -> dict[str, Any]:
        url = API_ROOT.format(token=self.token, method=method)
        try:
            response = self.session.post(url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            raise TelegramError(f"{method} request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramError(
                f"{method} returned non-JSON (HTTP {response.status_code})"
            ) from exc

        if not payload.get("ok"):
            # description never contains the token, so it is safe to surface.
            raise TelegramError(
                f"{method} failed (HTTP {response.status_code}): "
                f"{payload.get('description', 'unknown error')}"
            )
        return payload.get("result", {})

    # -- outbound ----------------------------------------------------------

    @staticmethod
    def escape(text: str) -> str:
        """Escape text for Telegram's HTML parse mode."""
        return html.escape(text, quote=False)

    @staticmethod
    def _chunks(text: str, limit: int = MESSAGE_LIMIT) -> Iterator[str]:
        """Split long messages on paragraph, then line, then word boundaries.

        Messages use HTML parse mode, and tags are always opened and closed
        within a single line, so splitting on a newline keeps every tag
        balanced. The last-resort cut therefore also avoids landing inside a
        ``<...>`` tag, which would make Telegram reject the whole message.
        """
        while len(text) > limit:
            window = text[:limit]
            split_at = window.rfind("\n\n")
            if split_at < limit // 2:
                split_at = window.rfind("\n")
            if split_at < limit // 2:
                # No line boundary: cut on a space that is not inside a tag.
                candidate = window.rfind(" ")
                last_open = window.rfind("<")
                last_close = window.rfind(">")
                if last_open > last_close:          # window ends mid-tag
                    candidate = last_open
                split_at = candidate if candidate > limit // 2 else limit
            yield text[:split_at].rstrip()
            text = text[split_at:].lstrip("\n")
        if text.strip():
            yield text

    def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        parse_mode: str = "HTML",
        disable_preview: bool = True,
    ) -> list[dict[str, Any]]:
        """Send a (possibly long) message, split across the 4096-char limit."""
        target = chat_id or self.chat_id
        results = []
        for part in self._chunks(text):
            results.append(
                self._call(
                    "sendMessage",
                    data={
                        "chat_id": target,
                        "text": part,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": str(disable_preview).lower(),
                    },
                )
            )
            time.sleep(0.35)  # stay inside the per-chat rate limit
        log.info("Sent %d message part(s) to %s", len(results), target)
        return results

    def send_document(
        self,
        path: Path,
        caption: str = "",
        chat_id: str | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        """Upload a file (the generated .docx) to the chat."""
        document = Path(path)
        if not document.is_file():
            raise TelegramError(f"Document not found: {document}")

        target = chat_id or self.chat_id
        with document.open("rb") as handle:
            result = self._call(
                "sendDocument",
                timeout=180,
                data={
                    "chat_id": target,
                    "caption": caption[:CAPTION_LIMIT],
                    "parse_mode": parse_mode,
                },
                files={
                    "document": (
                        document.name,
                        handle,
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document",
                    )
                },
            )
        log.info("Uploaded %s (%.1f KB) to %s",
                 document.name, document.stat().st_size / 1024, target)
        return result

    def send_error(self, context: str, exc: BaseException) -> None:
        """Best-effort failure alert; never raises, so it is safe in ``except``."""
        try:
            self.send_message(
                f"⚠️ <b>{self.escape(context)} failed</b>\n"
                f"<code>{self.escape(f'{type(exc).__name__}: {exc}')}</code>\n\n"
                f"Check the GitHub Actions run log for the full traceback."
            )
        except Exception as nested:  # pragma: no cover - alerting must not cascade
            log.error("Could not deliver failure alert: %s", nested)

    # -- inbound -----------------------------------------------------------

    def get_updates(self, offset: int | None = None, timeout: int | None = None):
        """Long-poll for new messages.

        Includes edited_message/edited_channel_post: Telegram delivers an
        edited message as this separate update type, not "message" again, so
        a user who fixes a typo by editing (rather than sending a fresh
        message) would otherwise have the command silently dropped - it
        would never appear in any getUpdates response at all.
        """
        poll_timeout = timeout if timeout is not None else config.LISTENER_POLL_TIMEOUT
        payload: dict[str, Any] = {
            "timeout": poll_timeout,
            "allowed_updates": (
                '["message","channel_post","edited_message","edited_channel_post"]'
            ),
        }
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", timeout=poll_timeout + 15, data=payload)

    def iter_commands(
        self, runtime_seconds: int | None = None, offset: int | None = None
    ) -> Iterator[tuple[str, str, int]]:
        """Yield ``(text, chat_id, update_id)`` for each incoming message.

        Runs for at most ``runtime_seconds`` so the listener workflow finishes
        well inside a GitHub Actions job and never burns free minutes idling.
        """
        budget = runtime_seconds or config.LISTENER_RUNTIME_SECONDS
        deadline = time.monotonic() + budget
        next_offset = offset

        while time.monotonic() < deadline:
            remaining = int(deadline - time.monotonic())
            poll = max(1, min(config.LISTENER_POLL_TIMEOUT, remaining))
            try:
                updates = self.get_updates(offset=next_offset, timeout=poll)
            except TelegramError as exc:
                log.warning("Polling error: %s", exc)
                time.sleep(3)
                continue

            for update in updates:
                next_offset = update["update_id"] + 1
                message = (
                    update.get("message")
                    or update.get("channel_post")
                    or update.get("edited_message")
                    or update.get("edited_channel_post")
                    or {}
                )
                text = (message.get("text") or "").strip()
                chat = str((message.get("chat") or {}).get("id", ""))
                if text and chat:
                    log.info("Command received from %s: %s", chat, text)
                    yield text, chat, update["update_id"]

    def drain(self) -> int | None:
        """Acknowledge pending updates without acting, returning the next offset.

        Used at listener start-up so a backlog accumulated while the workflow
        was not running does not trigger a burst of duplicate scans.
        """
        try:
            updates = self.get_updates(timeout=0)
        except TelegramError as exc:
            log.warning("Could not drain backlog: %s", exc)
            return None
        if not updates:
            return None
        offset = updates[-1]["update_id"] + 1
        log.info("Drained %d stale update(s); resuming from offset %d",
                 len(updates), offset)
        return offset
