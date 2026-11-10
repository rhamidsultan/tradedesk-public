"""Telegram and ntfy senders. Each returns (ok, short detail). Detail never includes tokens or targets."""
from __future__ import annotations

import httpx

from ..models import Alert

LEVEL_ICON = {"info": "ℹ️", "important": "⚠️", "critical": "🚨"}
NTFY_PRIORITY = {"info": 2, "important": 4, "critical": 5}


def _text(a: Alert, repeat_no: int) -> str:
    head = f"{LEVEL_ICON.get(a.level, '')} {a.title}"
    if repeat_no > 0:
        head += f"  (repeat {repeat_no})"
    return head + (f"\n{a.body}" if a.body else "")


class TelegramSender:
    name = "telegram"

    def __init__(self, http: httpx.Client, bot_token: str, api_base: str = "https://api.telegram.org"):
        self.http, self.token, self.base = http, bot_token, api_base

    def configured(self) -> bool:
        return bool(self.token)

    def send(self, chat_id: str, a: Alert, repeat_no: int, ack_url: str, open_url: str) -> tuple[bool, str]:
        buttons = [[{"text": "Open in TradeDesk", "url": open_url}]]
        if a.level != "info":
            buttons[0].insert(0, {"text": "✅ Acknowledge", "url": ack_url})
        payload = {"chat_id": chat_id, "text": _text(a, repeat_no),
                   "disable_notification": a.level == "info", "disable_web_page_preview": True,
                   "reply_markup": {"inline_keyboard": buttons}}
        try:
            r = self.http.post(f"{self.base}/bot{self.token}/sendMessage", json=payload)
        except Exception as e:  # noqa: BLE001
            return False, f"network: {type(e).__name__}"
        return (r.status_code == 200), f"http {r.status_code}"

    def send_text(self, chat_id: str, text: str) -> tuple[bool, str]:
        try:
            r = self.http.post(f"{self.base}/bot{self.token}/sendMessage",
                               json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
        except Exception as e:  # noqa: BLE001
            return False, f"network: {type(e).__name__}"
        return (r.status_code == 200), f"http {r.status_code}"


class NtfySender:
    name = "ntfy"

    def __init__(self, http: httpx.Client, server: str):
        self.http, self.server = http, server.rstrip("/")

    def configured(self) -> bool:
        return bool(self.server)

    def send(self, topic: str, a: Alert, repeat_no: int, ack_url: str, open_url: str) -> tuple[bool, str]:
        msg = {
            "topic": topic,
            "title": a.title + (f" (repeat {repeat_no})" if repeat_no else ""),
            "message": a.body or a.title,
            "priority": NTFY_PRIORITY.get(a.level, 3),
            "tags": [{"info": "information_source", "important": "warning", "critical": "rotating_light"}[a.level]],
            "click": open_url,
        }
        if a.level != "info":
            # one tap on the notification's button acknowledges without opening anything
            msg["actions"] = [
                {"action": "http", "label": "Acknowledge", "url": ack_url, "method": "POST", "clear": True},
                {"action": "view", "label": "Open", "url": open_url},
            ]
        return self._post(msg)

    def send_text(self, topic: str, text: str, click: str = "") -> tuple[bool, str]:
        msg = {"topic": topic, "title": "TradeDesk", "message": text, "priority": 3}
        if click:
            msg["click"] = click
        return self._post(msg)

    def _post(self, msg: dict) -> tuple[bool, str]:
        # JSON publishing (POST to the server root) keeps titles in UTF-8 and actions structured
        try:
            r = self.http.post(f"{self.server}/", json=msg)
        except Exception as e:  # noqa: BLE001
            return False, f"network: {type(e).__name__}"
        return (r.status_code == 200), f"http {r.status_code}"
