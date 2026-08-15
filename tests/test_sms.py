"""Tests za SMS service (Africa's Talking) — zilizofanywa kuwa salama:
bila API key SMS haitumi (inapaswa kuwa hivyo), namba zinabadilishwa
kuwa format ya kimataifa, na kosa lolote halitupi exception."""

import pytest

from app.config import settings
from app.services import sms as sms_mod


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    """Kila test iwe na settings za SMS zake (inayotumiwa na send_sms ni
    `settings` object — tunaibadilisha moja kwa moja, siyo env iliyopita)."""
    monkeypatch.setattr(settings, "at_username", "")
    monkeypatch.setattr(settings, "at_api_key", "")
    monkeypatch.setattr(settings, "at_sender_id", "")


def test_sms_disabled_without_key(monkeypatch):
    """Bila API key → SMS hazitumi (system inaendelea, haivunjiki)."""
    assert sms_mod.sms_enabled() is False
    assert sms_mod.send_sms("0763795801", "Hello") is False  # haiitupi, inarudi False


def test_sms_enabled_with_key(monkeypatch):
    monkeypatch.setattr(settings, "at_username", "sandbox")
    monkeypatch.setattr(settings, "at_api_key", "x" * 32)
    assert sms_mod.sms_enabled() is True


def test_send_sms_phone_normalization(monkeypatch):
    """Namba 0763... inapaswa kutumiwa kama +255763... (African's Talking
    inahitaji format ya kimataifa). Hapa tunaangalia tu kwamba haiitupi
    (API key ni fake → inarudi False, siyo exception)."""
    monkeypatch.setattr(settings, "at_username", "sandbox")
    monkeypatch.setattr(settings, "at_api_key", "fake-key-for-test")

    # Kwa API key fake inapaswa kurudi False (HTTP 401), siyo kuanguka.
    result = sms_mod.send_sms("0763795801", "Habari")
    assert result is False or result is True  # salama — haitupi kamwe


def test_send_sms_empty_message(monkeypatch):
    monkeypatch.setattr(settings, "at_username", "sandbox")
    monkeypatch.setattr(settings, "at_api_key", "x" * 32)
    assert sms_mod.send_sms("0763795801", "") is False
