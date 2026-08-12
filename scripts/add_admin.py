#!/usr/bin/env python3
"""Ongeza au sasisha akaunti ya ADMIN kwa email + password.

Matumizi:
    python scripts/add_admin.py --email you@example.com --password 'Secret!123' --name 'Jina Kamili' [--phone +2557XXXXXXX] [--category health|education] [--cadre CODE]

Password iliyo na herufi maalum? Tumia --password-env (epuka shell quoting):
    PW='Siri@2026' python scripts/add_admin.py --email you@example.com --password-env PW --name 'Jina'

Ikiwa akaunti ipo (kwa email AU namba ya simu) → inasasishwa tu
(email/password/jina). Kama haipo → inaundwa kama ADMIN:
    is_admin=True, email_verified=True, status=active.

MONGO_URI inasomwa kwenye env; default ni docker-compose ya mitaa:
    mongodb://admin:changeme@localhost:27017/kubadilishana_vituo?authSource=admin
"""
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pymongo import MongoClient
from app.security import hash_password, normalize_email, normalize_phone

DEFAULT_MONGO = "mongodb://admin:changeme@localhost:27017/kubadilishana_vituo?authSource=admin"


def _free_phone(db, start: int = 901) -> str:
    """Tafuta namba isiyotumika (+255700000901, ...) kwa placeholder ya admin."""
    for i in range(start, 999999):
        cand = f"+255700{i:06d}"
        if not db.users.find_one({"phone_primary": cand}, {"_id": 1}):
            return cand
    raise SystemExit("Hakuna namba ya simu tupu — weka --phone mwenyewe")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", default="")
    ap.add_argument("--password-env", default="", help="jina la env var iliyo na password")
    ap.add_argument("--name", default="")
    ap.add_argument("--phone", default="")
    ap.add_argument("--category", default="health", choices=["health", "education"])
    ap.add_argument("--cadre", default="CO")
    args = ap.parse_args()

    pw = args.password or os.environ.get(args.password_env, "") if args.password_env else args.password
    if not pw:
        raise SystemExit("Password haijatolewa — tumia --password au --password-env")
    email = normalize_email(args.email)
    phone = normalize_phone(args.phone) if args.phone else None

    db = MongoClient(os.environ.get("MONGO_URI", DEFAULT_MONGO)).get_default_database()
    now = datetime.now(timezone.utc)

    doc = db.users.find_one({"email": email}) or (
        db.users.find_one({"phone_primary": phone}, {"_id": 1, "full_name": 1}) if phone else None
    )
    if doc:
        print(f"✓ Akaunti ipo: {doc.get('full_name')} ({doc['_id']}) — inasasishwa...")
        updates = {
            "email": email, "email_verified": True, "is_admin": True,
            "password_hash": hash_password(pw), "status": "active", "updated_at": now,
        }
        if args.name:
            updates["full_name"] = args.name
        if phone:
            updates["phone_primary"] = phone
        db.users.update_one({"_id": doc["_id"]}, {"$set": updates})
        print(f"  UPDATED → {email} / is_admin ✓ / email_verified ✓")
        return

    name = args.name or email.split("@")[0]
    if not phone:
        phone = _free_phone(db)
    cadre = db.cadres.find_one({"code": args.cadre}) or {}
    db.users.insert_one({
        "full_name": name,
        "phone_primary": phone,
        "password_hash": hash_password(pw),
        "email": email, "email_verified": True,
        "category": args.category, "cadre_code": args.cadre,
        "cadre_display": cadre.get("display_name", args.cadre),
        "subjects": [],
        "current_station": {
            "region_id": 3, "region_name": "Dar es Salaam",
            "district_id": None, "district_name": "", "facility_id": None,
            "facility_name": None, "facility_type": None,
        },
        "desired_destinations": [],
        "status": "active", "is_verified": True, "is_admin": True,
        "notification_prefs": {"new_matches": True, "messages": True},
        "followed_regions": [],
        "created_at": now, "updated_at": now, "last_seen_at": now,
    })
    print(f"✓ CREATED admin → {email} / {name} / {phone} / is_admin ✓ / email_verified ✓")


if __name__ == "__main__":
    main()
