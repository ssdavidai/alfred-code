from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from .config import SlackConfig, env_secret
from .db import Database
from .errors import ConfigurationError


class Notifier(Protocol):
    channel: str

    def send(self, message: str, detail: dict[str, Any]) -> None: ...


class ConsoleNotifier:
    channel = "console"

    def send(self, message: str, detail: dict[str, Any]) -> None:
        print(message)


class SlackNotifier:
    channel = "slack"

    def __init__(self, config: SlackConfig):
        webhook = env_secret(config.webhook_env)
        if not webhook:
            raise ConfigurationError(f"Slack is enabled but ${config.webhook_env} is empty")
        self.webhook = webhook
        self.destination = config.channel

    def send(self, message: str, detail: dict[str, Any]) -> None:
        payload: dict[str, Any] = {"text": message}
        if self.destination:
            payload["channel"] = self.destination
        request = urllib.request.Request(
            self.webhook,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status >= 300:
                    raise RuntimeError(f"Slack returned HTTP {response.status}")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Slack notification failed: {exc}") from exc


class DurableNotifier:
    def __init__(self, database: Database, notifier: Notifier):
        self.database = database
        self.notifier = notifier

    def send(self, dedupe_key: str, message: str, detail: dict[str, Any] | None = None) -> bool:
        payload = {"message": message, "detail": detail or {}}
        if not self.database.claim_notification(dedupe_key, self.notifier.channel, payload):
            return False
        try:
            self.notifier.send(message, detail or {})
        except Exception as exc:
            self.database.finish_notification(dedupe_key, str(exc))
            self.database.event("notification.failed", {"key": dedupe_key, "error": str(exc)})
            return False
        self.database.finish_notification(dedupe_key)
        return True
