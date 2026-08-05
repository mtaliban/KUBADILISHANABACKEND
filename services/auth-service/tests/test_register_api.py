"""Integration tests for /auth/register — mocks Mongo and MQTT so no infra needed."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from bson import ObjectId


@pytest.fixture
def valid_body():
    return {
        "full_name": "Kieffer Madyedye",
        "phone_primary": "0710703705",
        "password": "test1234",
        "category": "health",
        "cadre_code": "LAB_TECH_2",
        "current_station": {
            "region_id": 6, "region_name": "Iringa",
            "district_id": 55, "district_name": "Iringa Mc",
            "facility_id": None, "facility_name": "Iringa RRH",
            "facility_type": "Regional Referral Hospital",
        },
        "desired_destinations": [
            {"region_id": 26, "region_name": "Tanga",
             "district_id": None, "facility_name": "Bombo RRH"},
        ],
    }


@pytest.fixture
def client(monkeypatch):
    # mock DB
    fake_db = MagicMock()
    fake_db.cadres.find_one = AsyncMock(return_value={
        "code": "LAB_TECH_2", "category": "health",
        "display_name": "Laboratory Technologist II", "requires_subjects": False,
    })
    fake_db.users.insert_one = AsyncMock(return_value=MagicMock(inserted_id=ObjectId()))
    fake_db.users.find_one = AsyncMock(return_value=None)
    fake_db.users.update_one = AsyncMock()

    monkeypatch.setattr("app.core.db.get_db", lambda: fake_db)
    monkeypatch.setattr("app.routes.auth.get_db", lambda: fake_db)

    # mock MQTT publisher (no-op)
    monkeypatch.setattr("app.routes.auth.publish", lambda *a, **kw: None)

    from app.main import app
    return TestClient(app)


def test_register_success(client, valid_body):
    r = client.post("/auth/register", json=valid_body)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["full_name"] == "Kieffer Madyedye"
    assert data["phone_primary"] == "+255710703705"
    assert data["category"] == "health"
    assert data["cadre_code"] == "LAB_TECH_2"
    assert "access_token" in data


def test_register_invalid_phone(client, valid_body):
    valid_body["phone_primary"] = "12345"
    r = client.post("/auth/register", json=valid_body)
    assert r.status_code == 422


def test_register_short_password(client, valid_body):
    valid_body["password"] = "abc"
    r = client.post("/auth/register", json=valid_body)
    assert r.status_code == 422


def test_register_missing_destinations(client, valid_body):
    valid_body["desired_destinations"] = []
    r = client.post("/auth/register", json=valid_body)
    assert r.status_code == 422


def test_register_teacher_needs_subjects(client, monkeypatch, valid_body):
    # override cadre lookup to return teacher_secondary
    from unittest.mock import AsyncMock
    fake_db = MagicMock()
    fake_db.cadres.find_one = AsyncMock(return_value={
        "code": "TEACHER_SECONDARY", "category": "education",
        "display_name": "Mwalimu Sekondari", "requires_subjects": True,
    })
    monkeypatch.setattr("app.routes.auth.get_db", lambda: fake_db)

    valid_body["category"] = "education"
    valid_body["cadre_code"] = "TEACHER_SECONDARY"
    valid_body["subjects"] = []
    r = client.post("/auth/register", json=valid_body)
    assert r.status_code == 422
    assert "subject" in r.json()["detail"].lower()
