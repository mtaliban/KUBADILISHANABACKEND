"""SMS ya Africa's Talking — kuwaarifu watumiaji kwa SMS halisi kwenye simu zao.

Inatumia REST API ya Africa's Talking (`https://api.africastalking.com/version1/messaging`)
kupitia httpx (hakuna SDK mpya inayohitajika). Ikiwa AT_API_KEY haijasanidiwa
kwenye environment → `send_sms` inarudi tu (False) na mfumo unaendelea na
notifications za mfumo (toast/kengele) — SMS haivunji chochote.

Mfano wa .env:
    AT_USERNAME=myapp
    AT_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    AT_SENDER_ID=KUBADILI      # optional — Sender ID/shortcode iliyosajiliwa
"""
import logging

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

_SMS_ENDPOINT = "https://api.africastalking.com/version1/messaging"
_TIMEOUT = 10.0


def sms_enabled() -> bool:
    """SMS zinafanya kazi ikiwa username + api_key zimewekwa kwenye env."""
    return bool(settings.at_username.strip() and settings.at_api_key.strip())


def send_sms(phone: str, message: str) -> bool:
    """Tuma SMS moja kwa namba ya Tanzania (format +255...).

    Inarudi True kama SMS ilikubaliwa na Africa's Talking (HTTP 201).
    Ikiwa SMS haijasanidiwa au kuna kosa → log + False (kamwe haitupi).
    """
    if not sms_enabled():
        logger.info("SMS disabled (AT_API_KEY haipo) — skip SMS kwa %s", phone)
        return False
    if not message or not phone:
        return False
    # Hakikisha format ni +255XXXXXXXXX (Africa's Talking inahitaji hiyo).
    digits = phone.strip().replace(" ", "").replace("-", "")
    if digits.startswith("255"):
        digits = "+" + digits
    elif digits.startswith("0") and len(digits) == 10:
        digits = "+255" + digits[1:]
    if not digits.startswith("+"):
        logger.warning("SMS skip — namba si ya kimataifa: %s", phone)
        return False

    data = {
        "username": settings.at_username.strip(),
        "to": digits,
        "message": message[:160],
    }
    if settings.at_sender_id.strip():
        data["from"] = settings.at_sender_id.strip()
    headers = {
        "apiKey": settings.at_api_key.strip(),
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(_SMS_ENDPOINT, data=data, headers=headers)
        if resp.status_code in (200, 201):
            logger.info("SMS sent ✓ → %s (%s)", digits, resp.json().get("SMSMessageData", {}).get("Recipients", [{}])[0].get("status", "ok"))
            return True
        logger.warning("SMS fail HTTP %s → %s (to %s)", resp.status_code, resp.text[:200], digits)
        return False
    except Exception as e:
        logger.exception("SMS error → %s: %s", digits, e)
        return False


def send_bulk_sms(phones: list[str], message: str) -> int:
    """Tuma SMS kwa watu wengi (kila moja tofauti — African's Talking bulk
    ina limit kwenye recipients; tunaweza kutumia send moja kwa moja kwa
    kila namba ili kuepuka kushindwa kwa zote kwa moja mbovu)."""
    sent = 0
    for p in phones:
        try:
            if send_sms(p, message):
                sent += 1
        except Exception:
            continue
    return sent
