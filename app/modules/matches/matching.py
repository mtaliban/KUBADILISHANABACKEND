"""Reverse-matching: A ↔ B iff same category+cadre, subjects overlap (if any),
and both stations satisfy at least one of the other's desired destinations."""
from typing import Iterable


def _station_satisfies_destination(st: dict, d: dict) -> bool:
    """SCORE precision: je destination d inatimizwa NA KWELI na station st
    (kituo sawa > wilaya sawa > mkoa sawa)?"""
    if d.get("facility_id"):
        return st.get("facility_id") == d["facility_id"]
    if d.get("district_id"):
        return st.get("district_id") == d["district_id"]
    return st.get("region_id") == d["region_id"]


def _station_in_region(st: dict, d: dict) -> bool:
    """INCLUSION check: Mkoa tu unaamua kuonekana.

    Hii ndiyo fix ya tatizo halisi: watumiaji wanaochagua WILAYA (siyo mkoa tu)
    kwenye destinations walikuwa HAWAONEKANI kwa wanaotoka wilaya nyingine ya
    mkoa ule ule — k.m. mtu wa Dodoma anataka kuja "Kigamboni" (Dar) lakini
    mwenyeji wa Dar (Ilala) hakumuona, ingawa anataka kuja MKOA WAKE. Wilaya/
    kituo vinaathiri SCORE tu (0.85/1.0), siyo kuonekana kabisa."""
    return st.get("region_id") is not None and st.get("region_id") == d.get("region_id")


def _any_in_region(st: dict, dests: Iterable[dict]) -> bool:
    return any(_station_in_region(st, d) for d in dests)


def match_score(a: dict, b: dict) -> float:
    if a["category"] != b["category"]:
        return 0.0
    if a["cadre_code"] != b["cadre_code"]:
        return 0.0
    if a.get("subjects") or b.get("subjects"):
        aa = set(a.get("subjects") or [])
        bb = set(b.get("subjects") or [])
        if aa and bb and not (aa & bb):
            return 0.0
    a_st = a["current_station"]; b_st = b["current_station"]
    a_d = a.get("desired_destinations", []); b_d = b.get("desired_destinations", [])
    # Inclusion kwa MKOA (kubadilishana: nataka kwenda mkoa wako, unataka kuja
    # mkoa wangu) — wilaya/kituo visivyolingana HAVIFICHI mtu, vinapunguza score tu.
    if not (_any_in_region(b_st, a_d) and _any_in_region(a_st, b_d)):
        return 0.0
    score = 0.5
    for d in a_d:
        # Consider EVERY matching destination and keep the best (most specific)
        # score — no early break, so a facility-level match beats a region-level one
        # even when the region destination is listed first.
        if _station_satisfies_destination(b_st, d):
            if d.get("facility_id"): score = max(score, 1.0)
            elif d.get("district_id"): score = max(score, 0.85)
            else: score = max(score, 0.65)
    return score


async def find_matches_for_user(db, user: dict) -> list[dict]:
    q = {"_id": {"$ne": user["_id"]}, "category": user["category"],
         "cadre_code": user["cadre_code"], "status": "active"}
    out = []
    async for c in db.users.find(q):
        s = match_score(user, c)
        if s > 0:
            out.append({
                "user_a_id": str(user["_id"]), "user_b_id": str(c["_id"]), "score": s,
                "candidate": {
                    "user_id": str(c["_id"]), "full_name": c["full_name"],
                    "phone_primary": c["phone_primary"], "phone_alt": c.get("phone_alt"),
                    "cadre_display": c.get("cadre_display"),
                    "current_station": c["current_station"],
                    "desired_destinations": c.get("desired_destinations", []),
                },
            })
    out.sort(key=lambda m: m["score"], reverse=True)
    return out
