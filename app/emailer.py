"""Email sending — OTP codes (2FA) and admin email verification.

Backends:
  1. SMTP (any provider: Gmail app-password, MailerSend SMTP, Resend SMTP, Zoho...)
     via SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD / SMTP_FROM.
  2. MailerSend REST API (MAILERSEND_API_KEY) — no SMTP creds needed.

Settings zinaweza kuwekwa kwa njia mbili:
  A. Env vars kwenye server (SMTP_HOST, ...) — classic deployment.
  B. KUJISAJILI KWA UI (admin settings page): admin anaweka SMTP yake mwenyewe
     kwenye page ya /admin/settings; inahifadhiwa kwenye MongoDB (collection
     `settings`, doc key="email"). Hii inamaanisha hakuna haja ya SSH/git kwa
     kusanidi email — weka kwenye panel na uthibitishe kwa \"Tuma Code ya
     Majaribio\".

Ikiwa hakuna config yoyote → email haitumwi; code ina-log kwenye stdout
(dev mode) ili flow iendelee kujaribiwa.
"""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

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


async def get_email_config() -> dict[str, Any]:
    """Return email settings: DB (admin-set via UI) first, env as fallback.

    The DB doc has the same field names as the env settings, so downstream
    helpers can read them uniformly. Password/key are stored as-is (the admin
    panel shows them masked).
    """
    try:
        from .db import get_db  # local import — avoid circular import at module load
        doc = await get_db().settings.find_one({"key": "email"})
        if doc:
            try:
                port = int(doc.get("smtp_port") or settings.smtp_port)
            except (TypeError, ValueError):
                port = settings.smtp_port
            cfg = {
                "smtp_host": doc.get("smtp_host") or settings.smtp_host,
                "smtp_port": port,
                "smtp_username": doc.get("smtp_username") or settings.smtp_username,
                "smtp_password": doc.get("smtp_password") or settings.smtp_password,
                "smtp_from": doc.get("smtp_from") or settings.smtp_from,
                "smtp_use_tls": bool(doc.get("smtp_use_tls", settings.smtp_use_tls)),
                "mailersend_api_key": doc.get("mailersend_api_key") or settings.mailersend_api_key,
                "mailersend_from": doc.get("mailersend_from") or settings.mailersend_from,
                "enabled": bool(doc.get("enabled", True)),
            }
            # DB config ipo? Tumia hiyo (ikiwa imewasha).
            if cfg["enabled"] and (cfg["smtp_host"] or cfg["mailersend_api_key"]):
                return cfg
    except Exception:
        # warning (sio exception) — usifanye log-flood na tracebacks kila login
        logger.warning("get_email_config DB read failed — kutumia env fallback")
    return {
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_username": settings.smtp_username,
        "smtp_password": settings.smtp_password,
        "smtp_from": settings.smtp_from,
        "smtp_use_tls": settings.smtp_use_tls,
        "mailersend_api_key": settings.mailersend_api_key,
        "mailersend_from": settings.mailersend_from,
        "enabled": True,
    }


def _is_configured(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("smtp_host") or cfg.get("mailersend_api_key"))


def _render(heading: str, body: str, code: str) -> str:
    """Substitution ya salama (sio str.format) — majina yenye braces { }
    hayatavunja template kamwe."""
    return (TEMPLATE
            .replace("__HEADING__", str(heading))
            .replace("__BODY__", str(body))
            .replace("__CODE__", str(code)))


def _send_smtp(cfg: dict[str, Any], to: str, subject: str, html: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = cfg.get("smtp_from")
        msg["To"] = to
        msg.attach(MIMEText(html, "html", "utf-8"))
        # timeout fupi (10s): SMTP isiwe ikifungia login kwa sekunde nyingi.
        with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port") or 587), timeout=10) as srv:
            if cfg.get("smtp_use_tls"):
                srv.starttls()
            if cfg.get("smtp_username"):
                srv.login(cfg["smtp_username"], cfg.get("smtp_password") or "")
            srv.sendmail(cfg.get("smtp_from"), [to], msg.as_string())
        logger.info(f"Email sent via SMTP -> {to} ({subject})")
        return True
    except Exception:
        logger.exception("SMTP send failed — email NOT delivered")
        return False


def _send_mailersend(cfg: dict[str, Any], to: str, subject: str, html: str) -> bool:
    try:
        mailersend_from = cfg.get("mailersend_from") or settings.mailersend_from
        r = httpx.post(
            "https://api.mailersend.com/v1/email",
            headers={
                "Authorization": f"Bearer {cfg.get('mailersend_api_key')}",
                "Content-Type": "application/json",
            },
            json={
                "from": {"email": mailersend_from.split("<")[-1].rstrip(">").strip(),
                         "name": mailersend_from.split("<")[0].strip()},
                "to": [{"email": to}],
                "subject": subject,
                "html": html,
            },
            timeout=15,
        )
        if r.status_code in (200, 201, 202):
            logger.info(f"Email sent via MailerSend -> {to} ({subject})")
            return True
        logger.error(f"MailerSend API {r.status_code}: {r.text[:300]}")
        return False
    except Exception:
        logger.exception("MailerSend send failed")
        return False


async def send_email(cfg: dict[str, Any] | None, to: str, subject: str,
                     heading: str, body: str, code: str) -> bool:
    """Send an OTP-style email. Returns True if delivered, False if not
    configured (code was only logged for dev). `cfg` inaweza kutoka DB
    (get_email_config) au env — ikiwa None, env pekee ndiyo inatumika."""
    if cfg is None:
        cfg = {
            "smtp_host": settings.smtp_host,
            "smtp_port": settings.smtp_port,
            "smtp_username": settings.smtp_username,
            "smtp_password": settings.smtp_password,
            "smtp_from": settings.smtp_from,
            "smtp_use_tls": settings.smtp_use_tls,
            "mailersend_api_key": settings.mailersend_api_key,
            "mailersend_from": settings.mailersend_from,
            "enabled": True,
        }
    html = _render(heading, body, code)
    if not _is_configured(cfg):
        logger.warning(f"✉️  [EMAIL NOT CONFIGURED] Code for {to} ({subject}): {code}")
        return False
    if cfg.get("smtp_host"):
        return _send_smtp(cfg, to, subject, html)
    return _send_mailersend(cfg, to, subject, html)
