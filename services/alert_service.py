#!/usr/bin/env python3
"""
Alert service for critical operational notifications.

Supports Telegram and SendGrid (if configured).
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False

from services.correlation_context import inject_correlation

if _structlog_available:
    logger = structlog.get_logger(__name__)
else:
    import logging

    logging.basicConfig(level=logging.INFO)
    class _FallbackLogger:
        def __init__(self, name: str):
            self._logger = logging.getLogger(name)

        def _log(self, level, event: str, **kwargs):
            exc_info = kwargs.pop("exc_info", None)
            kwargs = inject_correlation(kwargs)
            message = f"{event} | {kwargs}" if kwargs else event
            self._logger.log(level, message, exc_info=exc_info)

        def debug(self, event: str, **kwargs):
            self._log(logging.DEBUG, event, **kwargs)

        def info(self, event: str, **kwargs):
            self._log(logging.INFO, event, **kwargs)

        def warning(self, event: str, **kwargs):
            self._log(logging.WARNING, event, **kwargs)

        def error(self, event: str, **kwargs):
            self._log(logging.ERROR, event, **kwargs)

        def critical(self, event: str, **kwargs):
            self._log(logging.CRITICAL, event, **kwargs)

    logger = _FallbackLogger(__name__)


class AlertService:
    """Multi-channel alerting for critical events."""

    def __init__(self):
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
        self.sendgrid_from = os.getenv("SENDGRID_FROM_EMAIL")
        self.sendgrid_to = os.getenv("SENDGRID_TO_EMAIL")

    async def send_critical_alert(self, title: str, message: str) -> None:
        """Send alert via all configured channels."""
        await self._send_telegram(title, message)
        await self._send_sendgrid(title, message)

    async def _send_telegram(self, title: str, message: str) -> None:
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return

        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.telegram_chat_id,
            "text": f"🚨 {title}\n\n{message}",
            "parse_mode": "Markdown",
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
            logger.info("telegram_alert_sent")
        except Exception as exc:
            logger.error("telegram_alert_failed", error=str(exc))

    async def _send_sendgrid(self, title: str, message: str) -> None:
        if not self.sendgrid_api_key or not self.sendgrid_from or not self.sendgrid_to:
            return

        url = "https://api.sendgrid.com/v3/mail/send"
        payload = {
            "personalizations": [{"to": [{"email": self.sendgrid_to}]}],
            "from": {"email": self.sendgrid_from},
            "subject": f"{title}",
            "content": [{"type": "text/plain", "value": message}],
        }
        headers = {"Authorization": f"Bearer {self.sendgrid_api_key}"}

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
            logger.info("sendgrid_alert_sent")
        except Exception as exc:
            logger.error("sendgrid_alert_failed", error=str(exc))