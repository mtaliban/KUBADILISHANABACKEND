"""End-to-end auth API tests against the in-memory Mongo + stubbed MQTT."""

# ─── helpers ────────────────────────────────────────────────────────

def register_payload(**overrides) -> dict:
    payload = {
        "full_name": "Kieffer Madyedye",
        "phone_primary": "0712345678",
        "password": "siri-kali",
        "category": "health",
        "cadre_code": "CO",
        "subjects": [],
        "current_station": {
            "region_id": 17, "region_name": "Mwanza",
            "district_id": 1701, "district_name": "Nyamagana Dc",
            "facility_id": None, "facility_name": None, "facility_type": None,
        },
        "desired_destinations": [
            {"region_id": 1, "region_name": "Arusha", "district_id": None,
             "district_name": None, "facility_id": None, "facility_name": None,
             "notes": None},
        ],
    }
    payload.update(overrides)
    return payload


def seed_cadres(db):
    """Insert the cadres the register endpoint validates against."""
    return [
        {"code": "CO", "category": "health", "display_name": "Clinical Officer",
         "requires_subjects": False},
        {"code": "TEACHER_SECONDARY", "category": "education",
         "display_name": "Mwalimu wa Elimu ya Sekondari",
         "requires_subjects": True, "level": "Secondary"},
    ]


# ─── register ───────────────────────────────────────────────────────

async def test_register_success(client, db, published_events):
    await db.cadres.insert_many(seed_cadres(db))

    res = await client.post("/auth/register", json=register_payload())

    assert res.status_code == 201
    body = res.json()
    assert body["full_name"] == "Kieffer Madyedye"
    assert body["access_token"]
    assert body["token_type"] == "bearer"

    # User persisted with normalized phone.
    user = await db.users.find_one({"phone_primary": "+255712345678"})
    assert user is not None
    assert user["category"] == "health"
    assert user["cadre_display"] == "Clinical Officer"
    assert user["status"] == "active"
    assert user["is_admin"] is False
    assert user["password_hash"] != "siri-kali"

    # Registration event published on the canonical topic.
    assert len(published_events) == 1
    topic, payload = published_events[0]
    assert topic == "kv/user/registered"
    assert payload["event"] == "user.registered"
    assert payload["user_id"] == body["user_id"]
    assert payload["category"] == "health"


async def test_register_duplicate_phone_returns_409(client, db):
    await db.cadres.insert_many(seed_cadres(db))
    r1 = await client.post("/auth/register", json=register_payload())
    assert r1.status_code == 201

    r2 = await client.post("/auth/register", json=register_payload(phone_primary="0712345678"))
    assert r2.status_code == 409
    assert "already registered" in r2.json()["detail"]


async def test_register_unknown_cadre_returns_422(client, db):
    await db.cadres.insert_many(seed_cadres(db))
    res = await client.post("/auth/register", json=register_payload(cadre_code="NINJA"))
    assert res.status_code == 422


async def test_register_cadre_category_mismatch_returns_422(client, db):
    await db.cadres.insert_many(seed_cadres(db))
    # CO is health, but claim education.
    res = await client.post(
        "/auth/register",
        json=register_payload(category="education", cadre_code="CO"),
    )
    assert res.status_code == 422


async def test_register_cadre_requiring_subjects_rejects_empty(client, db):
    await db.cadres.insert_many(seed_cadres(db))
    res = await client.post(
        "/auth/register",
        json=register_payload(
            category="education", cadre_code="TEACHER_SECONDARY", subjects=[],
        ),
    )
    assert res.status_code == 422


async def test_register_invalid_phone_returns_422(client, db):
    await db.cadres.insert_many(seed_cadres(db))
    res = await client.post("/auth/register", json=register_payload(phone_primary="123"))
    assert res.status_code == 422


async def test_register_requires_destinations(client, db):
    await db.cadres.insert_many(seed_cadres(db))
    res = await client.post(
        "/auth/register", json=register_payload(desired_destinations=[])
    )
    assert res.status_code == 422


# ─── login ──────────────────────────────────────────────────────────

async def test_login_success_and_failure(client, db):
    await db.cadres.insert_many(seed_cadres(db))
    await client.post("/auth/register", json=register_payload())

    ok = await client.post("/auth/login",
                           json={"phone": "0712345678", "password": "siri-kali"})
    assert ok.status_code == 200
    assert ok.json()["access_token"]
    assert ok.json()["full_name"] == "Kieffer Madyedye"

    wrong = await client.post("/auth/login",
                              json={"phone": "0712345678", "password": "nope"})
    assert wrong.status_code == 401

    unknown = await client.post("/auth/login",
                                json={"phone": "0755123456", "password": "x"})
    assert unknown.status_code == 401


async def test_login_normalizes_international_phone(client, db):
    await db.cadres.insert_many(seed_cadres(db))
    await client.post("/auth/register", json=register_payload(phone_primary="+255712345678"))

    res = await client.post("/auth/login",
                            json={"phone": "+255712345678", "password": "siri-kali"})
    assert res.status_code == 200


# ─── check-phone ────────────────────────────────────────────────────

async def test_check_phone_availability(client, db):
    await db.cadres.insert_many(seed_cadres(db))
    free = await client.get("/auth/check-phone/0712345678")
    assert free.status_code == 200
    assert free.json()["available"] is True

    await client.post("/auth/register", json=register_payload())

    taken = await client.get("/auth/check-phone/0712345678")
    assert taken.json()["available"] is False


async def test_check_phone_invalid_format(client):
    res = await client.get("/auth/check-phone/abc")
    assert res.status_code == 200
    assert res.json()["available"] is False
    assert res.json()["reason"] == "invalid_format"


# ─── forgot / reset password ────────────────────────────────────────

async def test_forgot_password_does_not_reveal_account(client, db, monkeypatch):
    res = await client.post("/auth/forgot-password", json={"phone": "0755123456"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    # No reset record created for unknown phones.
    assert await db.password_resets.count_documents({}) == 0


async def test_full_password_reset_flow(client, db, monkeypatch):
    await db.cadres.insert_many(seed_cadres(db))
    await client.post("/auth/register", json=register_payload())

    # Force a deterministic 6-digit code.
    import app.modules.auth.routes as auth_routes
    monkeypatch.setattr(auth_routes.secrets, "randbelow", lambda n: 123456)

    sent = await client.post("/auth/forgot-password", json={"phone": "0712345678"})
    assert sent.status_code == 200

    # Wrong code → rejected.
    bad = await client.post("/auth/reset-password", json={
        "phone": "0712345678", "code": "999999", "new_password": "new-siri",
    })
    assert bad.status_code == 400

    # Correct code → password changed.
    good = await client.post("/auth/reset-password", json={
        "phone": "0712345678", "code": "123456", "new_password": "new-siri",
    })
    assert good.status_code == 200

    # Old password no longer works, new one does.
    old_login = await client.post("/auth/login",
                                  json={"phone": "0712345678", "password": "siri-kali"})
    assert old_login.status_code == 401
    new_login = await client.post("/auth/login",
                                  json={"phone": "0712345678", "password": "new-siri"})
    assert new_login.status_code == 200

    # Code is single-use.
    reuse = await client.post("/auth/reset-password", json={
        "phone": "0712345678", "code": "123456", "new_password": "another-pass",
    })
    assert reuse.status_code == 400
