import itertools

from bson import ObjectId

from app.modules.matches.matching import match_score, find_matches_for_user

# Users swap stations: a is in Mwanza and wants Arusha; b is in Arusha and wants Mwanza.
MWANZA_DEST = {"region_id": 17, "region_name": "Mwanza", "district_id": None,
               "district_name": None, "facility_id": None, "facility_name": None}
ARUSHA_DEST = {"region_id": 1, "region_name": "Arusha", "district_id": None,
               "district_name": None, "facility_id": None, "facility_name": None}

_PHONE_COUNTER = itertools.count(1)


def make_user(**overrides) -> dict:
    """Build a user document with sensible defaults (Mwanza CO wanting Arusha)."""
    user = {
        "_id": ObjectId(),
        "phone_primary": f"+2557{next(_PHONE_COUNTER):09d}",
        "full_name": "Test Mtumishi",
        "category": "health",
        "cadre_code": "CO",
        "cadre_display": "Clinical Officer",
        "subjects": [],
        "current_station": {
            "region_id": 17, "region_name": "Mwanza",
            "district_id": 1701, "district_name": "Nyamagana Dc",
            "facility_id": None, "facility_name": None,
        },
        "desired_destinations": [ARUSHA_DEST],
        "status": "active",
    }
    user.update(overrides)
    return user


def station(region_id=1, region_name="Arusha", district_id=None,
            district_name=None, facility_id=None, facility_name=None) -> dict:
    return {
        "region_id": region_id, "region_name": region_name,
        "district_id": district_id, "district_name": district_name,
        "facility_id": facility_id, "facility_name": facility_name,
    }


# ─── hard no-match rules ────────────────────────────────────────────

def test_category_mismatch_returns_zero():
    a = make_user()
    b = make_user(category="education")
    assert match_score(a, b) == 0.0


def test_cadre_mismatch_returns_zero():
    a = make_user()
    b = make_user(cadre_code="NO")
    assert match_score(a, b) == 0.0


def test_no_subject_overlap_returns_zero():
    a = make_user(subjects=["MATH", "PHYS"])
    b = make_user(subjects=["BIO", "CHEM"])
    assert match_score(a, b) == 0.0


def test_subject_overlap_matches():
    a = make_user(subjects=["MATH", "BIO"])
    b = make_user(subjects=["BIO", "CHEM"],
                  current_station=station(1, "Arusha"),
                  desired_destinations=[MWANZA_DEST])
    assert match_score(a, b) > 0


def test_subjects_only_required_when_both_have_them():
    # One side lists subjects, the other doesn't → not a hard fail.
    a = make_user(subjects=["MATH"])
    b = make_user(subjects=[],
                  current_station=station(1, "Arusha"),
                  desired_destinations=[MWANZA_DEST])
    assert match_score(a, b) > 0


def test_one_way_destination_satisfaction_returns_zero():
    # b's station (Mwanza) is NOT in a's destinations (Arusha) → no swap possible.
    a = make_user()
    b = make_user(
        current_station=station(17, "Mwanza", 1702, "Sengerema Dc"),
        desired_destinations=[{"region_id": 12, "region_name": "Tabora"}],
    )
    assert match_score(a, b) == 0.0


# ─── score tiers ────────────────────────────────────────────────────

def test_region_level_match_scores_0_65():
    a = make_user()
    b = make_user(current_station=station(1, "Arusha"),
                  desired_destinations=[MWANZA_DEST])
    assert match_score(a, b) == 0.65


def test_district_level_match_scores_0_85():
    a = make_user(desired_destinations=[
        {"region_id": 1, "region_name": "Arusha",
         "district_id": 101, "district_name": "Arusha Dc"},
    ])
    b = make_user(current_station=station(1, "Arusha", 101, "Arusha Dc"),
                  desired_destinations=[MWANZA_DEST])
    assert match_score(a, b) == 0.85


def test_facility_level_match_scores_1_0():
    a = make_user(desired_destinations=[
        {"region_id": 1, "region_name": "Arusha",
         "district_id": 101, "district_name": "Arusha Dc",
         "facility_id": "F123", "facility_name": "KCMC"},
    ])
    b = make_user(current_station=station(1, "Arusha", 101, "Arusha Dc",
                                          "F123", "KCMC"),
                  desired_destinations=[MWANZA_DEST])
    assert match_score(a, b) == 1.0


def test_score_prefers_most_specific_destination():
    # Two destinations for the same user: one region-level, one facility-level.
    a = make_user(desired_destinations=[
        {"region_id": 1, "region_name": "Arusha", "district_id": None,
         "district_name": None, "facility_id": None, "facility_name": None},
        {"region_id": 1, "region_name": "Arusha", "district_id": 101,
         "district_name": "Arusha Dc", "facility_id": "F123", "facility_name": "KCMC"},
    ])
    b = make_user(current_station=station(1, "Arusha", 101, "Arusha Dc",
                                          "F123", "KCMC"),
                  desired_destinations=[MWANZA_DEST])
    assert match_score(a, b) == 1.0


# ─── fix: destination ya WILAYA haifichi mtu wa mkoa ule ule ────────

def test_district_destination_same_region_still_matches():
    """TATIZO HALISI: mtumiaji wa Dar (Ilala) anataka kwenda Dodoma (Chamwino),
    na mtumiaji wa Dodoma (Chamwino) anataka kuja Dar (Kigamboni). Wilaya za
    destinations ni tofauti na stations — lakini MKOA unalingana, basi ni match.
    (Hapo awali mtu wa Kigamboni hakuonekana kwa mtu wa Ilala — no data.)"""
    a = make_user(
        current_station=station(3, "Dar Es Salaam", 17, "Ilala Mc"),
        desired_destinations=[{"region_id": 4, "region_name": "Dodoma",
                               "district_id": 23, "district_name": "Chamwino Dc",
                               "facility_id": None, "facility_name": None}],
    )
    b = make_user(
        current_station=station(4, "Dodoma", 23, "Chamwino Dc"),
        desired_destinations=[{"region_id": 3, "region_name": "Dar Es Salaam",
                               "district_id": 18, "district_name": "Kigamboni Mc",
                               "facility_id": None, "facility_name": None}],
    )
    s = match_score(a, b)
    assert s > 0
    # a's destination (Chamwino) inatimizwa NA KWELI na station ya b (Chamwino)
    # → district-level score 0.85. Kinyume chake ni mkoa tu → base 0.5.
    assert s == 0.85


def test_district_destination_different_district_same_region_min_score():
    """Wilaya zote mbili hazilingani (Chamwino vs Dodoma Cc) — bado match kwa
    mkoa, score = 0.5 (base region-level), siyo 0.0."""
    a = make_user(
        current_station=station(3, "Dar Es Salaam", 17, "Ilala Mc"),
        desired_destinations=[{"region_id": 4, "region_name": "Dodoma",
                               "district_id": 23, "district_name": "Chamwino Dc",
                               "facility_id": None, "facility_name": None}],
    )
    b = make_user(
        current_station=station(4, "Dodoma", 24, "Dodoma Cc"),
        desired_destinations=[{"region_id": 3, "region_name": "Dar Es Salaam",
                               "district_id": 18, "district_name": "Kigamboni Mc",
                               "facility_id": None, "facility_name": None}],
    )
    assert match_score(a, b) == 0.5


# ─── find_matches_for_user (async, in-memory Mongo) ─────────────────

async def test_find_matches_returns_only_valid_pairs_sorted_by_score(db):
    # me lists Arusha region AND the specific KCMC facility → perfect (facility)
    # should outscore the region-only candidate.
    me = make_user(desired_destinations=[
        ARUSHA_DEST,
        {"region_id": 1, "region_name": "Arusha",
         "district_id": 101, "district_name": "Arusha Dc",
         "facility_id": "F123", "facility_name": "KCMC"},
    ])
    await db.users.insert_one(me)

    perfect = make_user(current_station=station(1, "Arusha", 101, "Arusha Dc",
                                                "F123", "KCMC"),
                        desired_destinations=[MWANZA_DEST])
    await db.users.insert_one(perfect)

    region_only = make_user(current_station=station(1, "Arusha"),
                            desired_destinations=[MWANZA_DEST])
    await db.users.insert_one(region_only)

    wrong_cadre = make_user(cadre_code="NO", current_station=station(1, "Arusha"),
                            desired_destinations=[MWANZA_DEST])
    await db.users.insert_one(wrong_cadre)

    one_way = make_user(
        current_station=station(17, "Mwanza", 1702, "Sengerema Dc"),
        desired_destinations=[{"region_id": 12, "region_name": "Tabora"}],
    )
    await db.users.insert_one(one_way)

    results = await find_matches_for_user(db, me)

    assert len(results) == 2
    # Sorted best-first.
    assert results[0]["score"] > results[1]["score"]
    candidates = {r["candidate"]["user_id"] for r in results}
    assert str(perfect["_id"]) in candidates
    assert str(region_only["_id"]) in candidates
    # Non-matching users are excluded.
    assert str(wrong_cadre["_id"]) not in candidates
    assert str(one_way["_id"]) not in candidates
    # Candidate payload exposes everything the dashboard needs.
    first = results[0]
    assert first["candidate"]["full_name"] == perfect["full_name"]
    assert first["candidate"]["phone_primary"] == perfect["phone_primary"]
    assert first["candidate"]["current_station"]["facility_name"] == "KCMC"


async def test_find_matches_excludes_inactive_users(db):
    me = make_user()
    await db.users.insert_one(me)

    active = make_user(current_station=station(1, "Arusha"),
                       desired_destinations=[MWANZA_DEST])
    await db.users.insert_one(active)

    inactive = make_user(status="inactive", current_station=station(1, "Arusha"),
                         desired_destinations=[MWANZA_DEST])
    await db.users.insert_one(inactive)

    results = await find_matches_for_user(db, me)
    assert len(results) == 1
    assert results[0]["candidate"]["user_id"] == str(active["_id"])
