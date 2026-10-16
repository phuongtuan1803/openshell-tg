"""Minimal Telegram Bot API client (standard library only)."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, timeout: float = 35):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._timeout = timeout

    def call(self, method: str, **params) -> object:
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(
            self._base + method, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                data = json.load(exc)
            except ValueError:
                raise TelegramError(f"{method}: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
            raise TelegramError(f"{method}: {exc}") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', 'unknown error')}")
        return data["result"]

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        return self.call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        )

    def send(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> int:
        result = self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_keyboard(buttons),
        )
        return result["message_id"]

    def edit(self, chat_id: int, message_id: int, text: str,
             buttons: list[list[tuple[str, str]]] | None = None) -> None:
        try:
            self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_keyboard(buttons) or {"inline_keyboard": []},
            )
        except TelegramError as exc:
            if "message is not modified" not in str(exc):
                raise

    def answer(self, callback_id: str, text: str | None = None) -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])


def _keyboard(buttons: list[list[tuple[str, str]]] | None) -> dict | None:
    if not buttons:
        return None
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in buttons
        ]
    }
