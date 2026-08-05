"""Reverse-matching algorithm.

Definition of a match between user A (me) and user B (them):
  1. Same category (health/education)
  2. Same cadre_code
  3. If teacher_secondary: subjects must intersect (>=1 common subject)
  4. Reverse geography match:
       - B's current_station must satisfy at least one of A's desired_destinations
       - A's current_station must satisfy at least one of B's desired_destinations

Geographic satisfaction rules (destination is satisfied by a station if):
  - destination has facility_id AND station.facility_id == destination.facility_id, OR
  - destination has district_id AND station.district_id == destination.district_id
       (and no facility_id filter), OR
  - destination has only region_id AND station.region_id == destination.region_id
       (and no district/facility filter)
"""
from typing import Iterable


def _station_satisfies_destination(station: dict, dest: dict) -> bool:
    if dest.get("facility_id"):
        return station.get("facility_id") == dest["facility_id"]
    if dest.get("district_id"):
        return station.get("district_id") == dest["district_id"]
    return station.get("region_id") == dest["region_id"]


def _any_destination_satisfied(station: dict, dests: Iterable[dict]) -> bool:
    return any(_station_satisfies_destination(station, d) for d in dests)


def match_score(user_a: dict, user_b: dict) -> float:
    """Return a score 0..1. 0 = no match."""
    if user_a["category"] != user_b["category"]:
        return 0.0
    if user_a["cadre_code"] != user_b["cadre_code"]:
        return 0.0

    if user_a.get("subjects") or user_b.get("subjects"):
        a_subs = set(user_a.get("subjects") or [])
        b_subs = set(user_b.get("subjects") or [])
        if a_subs and b_subs and not (a_subs & b_subs):
            return 0.0

    a_station = user_a["current_station"]
    b_station = user_b["current_station"]
    a_dests = user_a.get("desired_destinations", [])
    b_dests = user_b.get("desired_destinations", [])

    a_wants_b = _any_destination_satisfied(b_station, a_dests)
    b_wants_a = _any_destination_satisfied(a_station, b_dests)

    if not (a_wants_b and b_wants_a):
        return 0.0

    # tighter geographic match = higher score
    score = 0.5
    for d in a_dests:
        if _station_satisfies_destination(b_station, d):
            if d.get("facility_id"):
                score = max(score, 1.0)
            elif d.get("district_id"):
                score = max(score, 0.85)
            else:
                score = max(score, 0.65)
            break
    return score


async def find_matches_for_user(db, user: dict) -> list[dict]:
    """Query candidates then filter in Python. For MVP; acceptable up to ~10k active users."""
    query = {
        "_id": {"$ne": user["_id"]},
        "category": user["category"],
        "cadre_code": user["cadre_code"],
        "status": "active",
    }
    matches = []
    cursor = db.users.find(query)
    async for candidate in cursor:
        score = match_score(user, candidate)
        if score > 0:
            matches.append({
                "user_a_id": str(user["_id"]),
                "user_b_id": str(candidate["_id"]),
                "score": score,
                "candidate": {
                    "user_id": str(candidate["_id"]),
                    "full_name": candidate["full_name"],
                    "phone_primary": candidate["phone_primary"],
                    "cadre_display": candidate.get("cadre_display"),
                    "current_station": candidate["current_station"],
                    "desired_destinations": candidate.get("desired_destinations", []),
                },
            })
    matches.sort(key=lambda m: m["score"], reverse=True)
    return matches
