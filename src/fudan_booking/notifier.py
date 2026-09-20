from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass(frozen=True, slots=True)
class QQSMTPSettings:
    username: str
    auth_code: str
    recipient: str
    host: str = "smtp.qq.com"
    port: int = 465

    @classmethod
    def from_env(cls) -> QQSMTPSettings:
        values = {
            "username": os.getenv("QQ_SMTP_USERNAME", ""),
            "auth_code": os.getenv("QQ_SMTP_AUTH_CODE", ""),
            "recipient": os.getenv("NOTIFICATION_EMAIL", ""),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"missing QQ SMTP settings: {', '.join(missing)}")
        return cls(**values)


class QQSMTPNotifier:
    def __init__(self, settings: QQSMTPSettings) -> None:
        self.settings = settings

    def send(self, subject: str, text: str) -> None:
        message = EmailMessage()
        message["From"] = self.settings.username
        message["To"] = self.settings.recipient
        message["Subject"] = subject
        message.set_content(text)

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            self.settings.host,
            self.settings.port,
            context=context,
            timeout=20,
        ) as client:
            client.login(self.settings.username, self.settings.auth_code)
            client.send_message(message)
