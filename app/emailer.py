"""Email sending — OTP codes (2FA) and admin email verification.

Supports two backends:
  1. SMTP (any provider: Gmail app-password, MailerSend SMTP, Resend SMTP, Zoho...)
     configured via SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD / SMTP_FROM.
  2. MailerSend REST API (MAILERSEND_API_KEY) — no SMTP creds needed.

If neither is configured, emails are NOT sent — the code is logged to backend
stdout (dev mode) so the flow can still be tested locally.
"""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from .config import settings

logger = logging.getLogger(__name__)

TEMPLATE = """\
<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:0 auto;padding:24px;border:1px solid #e5e7eb;border-radius:12px">
  <div style="font-size:13px;font-weight:700;color:#1d4ed8;margin-bottom:16px">Kubadilishana Vituo</div>
  <h2 style="font-size:18px;color:#111827;margin:0 0 8px">__HEADING__</h2>
  <p style="font-size:14px;color:#374151;line-height:1.6;margin:0 0 16px">__BODY__</p>
  <div style="background:#f3f4f6;border-radius:10px;padding:14px 18px;text-align:center;font-size:26px;font-weight:800;letter-spacing:8px;color:#111827">__CODE__</div>
  <p style="font-size:12px;color:#9ca3af;margin:16px 0 0">Code hii ni halali kwa dakika 10. Usimshirikie mtu yeyote.</p>
</div>
"""


def _is_configured() -> bool:
    return bool(settings.smtp_host or settings.mailersend_api_key)


def _render(heading: str, body: str, code: str) -> str:
    """Substitution ya salama (sio str.format) — majina yenye braces { }
    hayatavunja template kamwe."""
    return (TEMPLATE
            .replace("__HEADING__", str(heading))
            .replace("__BODY__", str(body))
            .replace("__CODE__", str(code)))


def _send_smtp(to: str, subject: str, html: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from
        msg["To"] = to
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as srv:
            if settings.smtp_use_tls:
                srv.starttls()
            if settings.smtp_username:
                srv.login(settings.smtp_username, settings.smtp_password)
            srv.sendmail(settings.smtp_from, [to], msg.as_string())
        logger.info(f"Email sent via SMTP -> {to} ({subject})")
        return True
    except Exception:
        logger.exception("SMTP send failed — email NOT delivered")
        return False


def _send_mailersend(to: str, subject: str, html: str) -> bool:
    try:
        r = httpx.post(
            "https://api.mailersend.com/v1/email",
            headers={
                "Authorization": f"Bearer {settings.mailersend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": {"email": settings.mailersend_from.split("<")[-1].rstrip(">").strip(),
                         "name": settings.mailersend_from.split("<")[0].strip()},
                "to": [{"email": to}],
                "subject": subject,
                "html": html,
            },
            timeout=20,
        )
        if r.status_code in (200, 201, 202):
            logger.info(f"Email sent via MailerSend -> {to} ({subject})")
            return True
        logger.error(f"MailerSend API {r.status_code}: {r.text[:300]}")
        return False
    except Exception:
        logger.exception("MailerSend send failed")
        return False


def send_email(to: str, subject: str, heading: str, body: str, code: str) -> bool:
    """Send an OTP-style email. Returns True if delivered, False if not
    configured (code was only logged for dev)."""
    html = _render(heading, body, code)
    if not _is_configured():
        logger.warning(f"✉️  [EMAIL NOT CONFIGURED] Code for {to} ({subject}): {code}")
        return False
    if settings.smtp_host:
        return _send_smtp(to, subject, html)
    return _send_mailersend(to, subject, html)
