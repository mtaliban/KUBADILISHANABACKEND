from typing import Literal, Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, Query
from ...db import get_db
from ...security import current_user
from ...cache import get_redis
from .matching import find_matches_for_user, _station_satisfies_destination, _any_in_region
from ..messaging.ws_manager import manager as ws_manager


async def _board_cache_get(key: str) -> dict | None:
    try:
        r = get_redis()
        v = await r.get(key)
        return json.loads(v) if v is not None else None
    except Exception:
        return None


async def _board_cache_set(key: str, value: dict, ttl: int) -> None:
    try:
        r = get_redis()
        await r.setex(key, ttl, json.dumps(value, default=str))
    except Exception:
        pass

router = APIRouter(prefix="/matches", tags=["matches"])


def _filter(matches, region_id=None, district_id=None, facility_id=None):
    out = []
    for m in matches:
        st = m["candidate"]["current_station"]
        if facility_id and st.get("facility_id") != facility_id: continue
        if district_id and st.get("district_id") != district_id: continue
        if region_id and st.get("region_id") != region_id: continue
        out.append(m)
    return out


def _subjects_overlap(a: list, b: list) -> bool:
    """Walimu wenye masomo: wanatakiwa wawe na ANGALAU somo moja linalofanana.
    (Kama mmoja hana masomo → hakuna kizuizi — sawa na matching.py.)"""
    aa, bb = set(a or []), set(b or [])
    if aa and bb:
        return bool(aa & bb)
    return True


def _subjects_match_strict(my: list, theirs: list) -> bool:
    """STRICT subject match (kichujio kimewashwa): wote wawili WANA masomo na
    wanashiriki angalau somo moja. Mtu asiye na masomo kabisa HAONEKANI
    (vinginevyo kichujio "Masomo yanayofanana" hakifanyi kazi kwa kila mtu)."""
    aa, bb = set(my or []), set(theirs or [])
    return bool(aa and bb and (aa & bb))


def _subjects_match_all(my: list, theirs: list) -> bool:
    """Masomo YOTE mawili (au yote ya mimi) yanapaswa kufanana — hata somo
    moja likikosekana, huyu haonekani. Mtu asiye na masomo haonekani."""
    aa, bb = set(my or []), set(theirs or [])
    return bool(aa and bb and aa.issubset(bb))


def _subjects_no_match(my: list, theirs: list) -> bool:
    """WASIO match: hawana somo lolote linalofanana (mmoja asiye na masomo
    au masomo yao yakiwa tofauti kabisa)."""
    aa, bb = set(my or []), set(theirs or [])
    if not aa or not bb:
        return True
    return not (aa & bb)


@router.get("/me")
async def my_matches(user=Depends(current_user),
                     region_id: Optional[int] = None, district_id: Optional[int] = None,
                     facility_id: Optional[str] = None, limit: int = Query(100, le=500)):
    matches = await find_matches_for_user(get_db(), user)
    f = _filter(matches, region_id, district_id, facility_id)
    return {"total": len(matches), "filtered": len(f), "matches": f[:limit]}


@router.get("/stats")
async def stats(user=Depends(current_user)):
    matches = await find_matches_for_user(get_db(), user)
    per_r, per_d, per_f = {}, {}, {}
    for m in matches:
        st = m["candidate"]["current_station"]
        rk = (st["region_id"], st["region_name"])
        per_r[rk] = per_r.get(rk, 0) + 1
        if st.get("district_id"):
            dk = (st["district_id"], st["district_name"], st["region_name"])
            per_d[dk] = per_d.get(dk, 0) + 1
        if st.get("facility_id"):
            fk = (st["facility_id"], st.get("facility_name"), st.get("district_name"))
            per_f[fk] = per_f.get(fk, 0) + 1
    return {
        "total_matches": len(matches),
        "by_region": [{"region_id": k[0], "region_name": k[1], "count": v}
                      for k, v in sorted(per_r.items(), key=lambda x: -x[1])],
        "by_district": [{"district_id": k[0], "district_name": k[1], "region_name": k[2], "count": v}
                        for k, v in sorted(per_d.items(), key=lambda x: -x[1])],
        "by_facility": [{"facility_id": k[0], "facility_name": k[1], "district_name": k[2], "count": v}
                        for k, v in sorted(per_f.items(), key=lambda x: -x[1])],
    }


def _candidate_out(u: dict, score: float | None = None) -> dict:
    """Grid card payload for the dashboard board."""
    return {
        "user_id": str(u["_id"]),
        "full_name": u["full_name"],
        "phone_primary": u.get("phone_primary"),
        "phone_alt": u.get("phone_alt"),
        "cadre_display": u.get("cadre_display"),
        "cadre_code": u.get("cadre_code"),
        "category": u.get("category"),
        "subjects": u.get("subjects", []),
        "score": score,
        "current_station": u.get("current_station"),
        "desired_destinations": u.get("desired_destinations", []),
        "online": ws_manager.is_online(str(u["_id"])),
        "created_at": u.get("created_at"),
    }


@router.get("/board")
async def board(
    user=Depends(current_user),
    scope: Literal["incoming", "all"] = Query("incoming", description="incoming = wanaokuja mkoa wako; all = watumiaji wote"),
    region_id: Optional[int] = None,
    region_ids: Optional[str] = Query(None, description="comma-separated source region ids (multi-region default)"),
    district_id: Optional[int] = None,
    facility_id: Optional[str] = None,
    subject_match: bool = Query(False, description="[backward-compat] true = waone tu wenye masomo yanayofanana (sawa na subject_filter=any)"),
    subject_filter: Literal["off", "any", "all", "none"] = Query("off", description="off = wote; any = somo moja linalofanana; all = masomo yote mawili yanafanana; none = wasio na somo linalofanana"),
    subject_q: Optional[str] = Query(None, description="comma-separated subject codes — search walimu wenye masomo haya (yoyote kati yao)"),
    cadre_code: Optional[str] = Query(None, description="kada code — kichujio cha afya (k.m. CO, HA, ANO, EN)"),
    limit: int = Query(200, le=1000),
    bypass_cache: bool = Query(False, description="skip Redis board cache — tumia kwenye live reloads (WS events)"),
):
    """Dashboard stats board (ad-board juu ya dashboard).

    - `scope=incoming` (default): watu wanaokuja mkoa wako — matches halisi
      (wanaotaka kuja kwako, kutoka mikoa unayotaka kwenda).
    - `scope=all`: watumiaji wote wa mfumo (stats za mikoa yao).

    Filters (region_ids / region_id / district_id / facility_id) zinachuja
    kwenye **kituo cha sasa** cha mtumiaji (yaani wapi yupo sasa).
    `region_ids` inaunga mkono MIKOA MINGI kwa wakati mmoja (default ya
    mtumiaji aliyejiandikisha destinations nyingi — anapata zote).
    """
    db = get_db()
    # Redis cache (5s) — board ni query nzito (full candidates scan); TTL fupi
    # inalinda `online` isiwe stale sana. WS events zinabust frontend cache
    # papo hapo, hivyo board inaonekana LIVE hata ikiwa Redis cache ipo.
    cache_key = f"board:{user['_id']}:{scope}:{region_ids or ''}:{region_id or ''}:{district_id or ''}:{facility_id or ''}:{subject_match}:{subject_filter}:{subject_q or ''}:{cadre_code or ''}:{limit}"
    cached_res = await _board_cache_get(cache_key)
    if cached_res is not None and not bypass_cache:
        return cached_res
    my_station = user.get("current_station") or {}
    my_category = user.get("category") or "health"
    is_admin = user.get("is_admin", False)

    # ── Gather candidates (full set for stats; limited for grid) ──
    # Admin anaona WATU WOTE (health + education) — si category yake pekee.
    # mtu wa kawaida anaona idara yake tu (walimu → elimu, afya → afya).
    q: dict = {"status": "active", "is_admin": {"$ne": True},
               "_id": {"$ne": user["_id"]}}
    if not is_admin:
        q["category"] = my_category
    # Kichujio cha LEVEL kwa elimu: mwalimu wa msingi aone walimu wa msingi tu,
    # sio wa sekondari (na kinyume chake). Afya haina level filter.
    my_cadre = user.get("cadre_code")
    if my_cadre and my_category == "education":
        my_cadre_doc = await db.cadres.find_one({"code": my_cadre})
        my_level = (my_cadre_doc or {}).get("level")
        if my_level:
            level_codes = [c["code"] async for c in db.cadres.find({"category": my_category, "level": my_level})]
            if level_codes:
                q["cadre_code"] = {"$in": level_codes}
    region_filter: list[int] = []
    if region_ids:
        region_filter = [int(x) for x in region_ids.split(",") if x.strip().lstrip("-").isdigit()]
    elif region_id is not None:
        region_filter = [region_id]
    if region_filter:
        q["current_station.region_id"] = {"$in": region_filter}
    if district_id is not None:
        q["current_station.district_id"] = district_id
    if facility_id is not None:
        q["current_station.facility_id"] = facility_id
    # Kichujio cha kada (afya): mtu aweze kuchuja kwa kada maalum
    # (k.m. CO, HA, ANO, EN) — kama subject_filter kwa walimu.
    if cadre_code:
        q["cadre_code"] = cadre_code.upper()
    cursor = db.users.find(q, {"password_hash": 0}).sort("created_at", -1)
    raw = [u async for u in cursor]

    # Masomo (OPTIONAL — default HAZICHUJI): mtumiaji aweze kuwaona wote
    # wa idara yake hata kama masomo hayakufanana. Akipenda, anaweza kuchuja:
    # `any` = wenye angalau somo moja linalofanana, `all` = wenye masomo YOTE
    # mawili yanayofanana, `none` = wasio na somo linalofanana kabisa, au
    # `subject_q` = search kwa masomo maalum.
    # NOTE: subject filter inafanya kazi mtu yeyote aliye na subjects —
    # sio tu pale cadre ina `level` (kama ilivyokuwa awali).
    my_subjects = user.get("subjects") or []
    sf = subject_filter
    if subject_match and sf == "off":
        sf = "any"  # backward-compat
    if sf == "any" and my_subjects:
        raw = [u for u in raw if _subjects_match_strict(my_subjects, u.get("subjects") or [])]
    elif sf == "all" and my_subjects:
        raw = [u for u in raw if _subjects_match_all(my_subjects, u.get("subjects") or [])]
    elif sf == "none":
        raw = [u for u in raw if _subjects_no_match(my_subjects, u.get("subjects") or [])]
    if subject_q:
        # Search inaweza kutumia CODES au MAJINA (k.m. "Kiswahili, English" →
        # KISW_MSINGI + ENGLISH_MSINGI) — hata masomo mawili kwa pamoja.
        terms = [c.strip() for c in subject_q.split(",") if c.strip()]
        if terms:
            subj_rows = [d async for d in db.subjects.find({}, {"_id": 0, "code": 1, "name": 1})]
            code_of_name = {s["name"].strip().lower(): s["code"] for s in subj_rows}
            wanted: set[str] = set()
            for term in terms:
                t = term.strip().upper()
                exact_name = code_of_name.get(term.strip().lower())
                if exact_name:
                    wanted.add(exact_name)
                elif t in {s["code"] for s in subj_rows}:
                    wanted.add(t)
                else:
                    # Sehemu ya jina/code (k.m. "KISW" au "Kiswah")
                    for s in subj_rows:
                        if t in s["code"].upper() or t in s["name"].upper():
                            wanted.add(s["code"])
            if wanted:
                raw = [u for u in raw if any(w in (u.get("subjects") or []) for w in wanted)]

    if scope == "incoming":
        # Wanaokuja mkoa wako (same idara, kada yoyote) — wanataka kuja kwako.
        # INCLUSION ni kwa MKOA: mtu anayechagua wilaya (k.m. Kigamboni/Dar)
        # bado anaonekana kwa mwenyeji wa mkoa ule ule (k.m. Ilala/Dar) —
        # wilaya/kituo vinapunguza SCORE tu, siyo kuonekana kabisa.
        def _wants_to_come(u: dict) -> bool:
            return _any_in_region(my_station, u.get("desired_destinations") or [])
        raw = [u for u in raw if _wants_to_come(u)]
        stat_rows = raw
        stat_total = len(raw)
        candidates = []
        for u in raw[:limit]:
            # Score: 0.5 = mkoa tu; 0.65 = region-level dest; 0.85 = wilaya sawa;
            # 1.0 = kituo sawa. (Sawa na matching.match_score.)
            score = 0.5
            for d in (u.get("desired_destinations") or []):
                if _station_satisfies_destination(my_station, d):
                    if d.get("facility_id"): score = max(score, 1.0)
                    elif d.get("district_id"): score = max(score, 0.85)
                    else: score = max(score, 0.65)
            c = _candidate_out(u, score)
            candidates.append(c)
    else:
        # Wote wa idara yangu (stats kamili — kada zote, sio kuchanganywa na idara nyingine)
        stat_rows = raw
        stat_total = len(raw)
        candidates = [_candidate_out(u) for u in raw[:limit]]

    # ── Stats by region / district / facility (kituo cha sasa cha candidate) ──
    per_r, per_d, per_f = {}, {}, {}
    for c in stat_rows:
        st = c.get("current_station") or {}
        rk = (st.get("region_id"), st.get("region_name"))
        per_r[rk] = per_r.get(rk, 0) + 1
        if st.get("district_id"):
            dk = (st.get("district_id"), st.get("district_name"), st.get("region_name"))
            per_d[dk] = per_d.get(dk, 0) + 1
        if st.get("facility_id"):
            fk = (st.get("facility_id"), st.get("facility_name"), st.get("district_name"), st.get("district_id"))
            per_f[fk] = per_f.get(fk, 0) + 1

    result = {
        "scope": scope,
        "total": stat_total,
        "candidates": candidates,
        "by_region": [{"region_id": k[0], "region_name": k[1], "count": v}
                       for k, v in sorted(per_r.items(), key=lambda x: -x[1])],
        "by_district": [{"district_id": k[0], "district_name": k[1], "region_name": k[2], "count": v}
                         for k, v in sorted(per_d.items(), key=lambda x: -x[1])],
        "by_facility": [{"facility_id": k[0], "facility_name": k[1], "district_name": k[2], "district_id": k[3], "count": v}
                         for k, v in sorted(per_f.items(), key=lambda x: -x[1])],
    }
    await _board_cache_set(cache_key, result, 5)
    return result


@router.get("/me/cached")
async def cached_matches(user=Depends(current_user), limit: int = Query(50, le=200)):
    db = get_db()
    uid = str(user["_id"])
    cur = db.matches.find({"$or": [{"user_a_id": uid}, {"user_b_id": uid}]}).sort("matched_at", -1).limit(limit)
    out = []
    async for m in cur:
        other_id = m["user_b_id"] if m["user_a_id"] == uid else m["user_a_id"]
        other = await db.users.find_one({"_id": ObjectId(other_id)})
        if not other: continue
        out.append({
            "score": m["score"], "matched_at": m["matched_at"], "status": m.get("status", "new"),
            "candidate": {
                "user_id": str(other["_id"]), "full_name": other["full_name"],
                "phone_primary": other["phone_primary"], "phone_alt": other.get("phone_alt"),
                "cadre_display": other.get("cadre_display"),
                "current_station": other["current_station"],
                "desired_destinations": other.get("desired_destinations", []),
            },
        })
    return {"count": len(out), "matches": out}
