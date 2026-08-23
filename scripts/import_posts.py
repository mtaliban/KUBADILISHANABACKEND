"""
Import WhatsApp exchange posts as real users.

- Registers each via HTTP POST /auth/register on auth-service.
- This naturally triggers `kv/user/registered` MQTT events, which the
  match-service subscribes to and re-computes matches, which then publishes
  `kv/match/found` events.
- Default password for every seeded user: "changeme123"

Run:
    python backend/scripts/import_posts.py
"""
from __future__ import annotations
import os
import re
import sys
import json
import time
import httpx
from pymongo import MongoClient
from typing import Optional

AUTH_URL = os.getenv("AUTH_URL", "http://localhost:8001")
MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb://admin:changeme@localhost:27017/kubadilishana_vituo?authSource=admin",
)
DEFAULT_PASSWORD = "changeme123"

# ─────────────── cadre aliases (text → cadre_code) ───────────────
CADRE_ALIASES = {
    # Health
    "clinical officer": "CO", "co": "CO",
    "assistant clinical officer": "ACO", "a.c.o": "ACO", "aco": "ACO",
    "clinical assistant": "CA", "c.a": "CA", "ca": "CA",
    "assistant medical officer": "AMO", "amo": "AMO",
    "medical doctor": "MD", "md": "MD",
    "assistant nursing officer": "ANO", "ano": "ANO",
    "nursing officer": "NO", "n.o": "NO", "no daraja": "NO",
    "enrolled nurse": "EN", "en": "EN",
    "registered nurse": "RN", "rn": "RN",
    "nurse en": "EN", "nurse rn": "RN",
    "laboratory technologist ii": "LAB_TECH_2",
    "laboratory technologist i": "LAB_TECH_1",
    "laboratory technologist": "LAB_TECH_2",
    "health laboratory technologist": "LAB_TECH_2",
    "laboratory scientist ii": "LAB_SCI_2",
    "senior laboratory assistant i": "SR_LAB_ASST",
    "senior laboratory assistant": "SR_LAB_ASST",
    "laboratory assistant": "LAB_ASST",
    "medical laboratory technologist": "MALT", "malt": "MALT",
    "pharmacist ii": "PHARM_2", "pharmacist": "PHARM_2",
    "health assistant": "HA", "h/a": "HA", "ha": "HA",
    "medical attendant": "MA", "m/a": "MA", "ma": "MA",
    # Education
    "elimu msingi": "TEACHER_PRIMARY", "idara msingi": "TEACHER_PRIMARY",
    "idara elimu msingi": "TEACHER_PRIMARY", "elimu ya msingi": "TEACHER_PRIMARY",
    "elimu sekondari": "TEACHER_SECONDARY", "idara sekondari": "TEACHER_SECONDARY",
    "idara elimu sekondari": "TEACHER_SECONDARY", "idara secondary": "TEACHER_SECONDARY",
    "idara sec": "TEACHER_SECONDARY", "secondary": "TEACHER_SECONDARY",
    "mwalimu": "TEACHER_PRIMARY",  # fallback
}

# ─────────────── region aliases (text hint → region name in DB) ───────────────
REGION_ALIASES = {
    "dar": "Dar Es Salaam", "dsm": "Dar Es Salaam", "dar es salaam": "Dar Es Salaam",
    "pwani": "Coast", "coast": "Coast",
    "arusha": "Arusha", "dodoma": "Dodoma", "geita": "Geita", "iringa": "Iringa",
    "kagera": "Kagera", "katavi": "Katavi", "kigoma": "Kigoma",
    "kilimanjaro": "Kilimanjaro", "moshi": "Kilimanjaro",
    "lindi": "Lindi", "manyara": "Manyara", "babati": "Manyara", "mbulu": "Manyara",
    "mara": "Mara", "musoma": "Mara", "bunda": "Mara", "serengeti": "Mara",
    "mbeya": "Mbeya", "kyela": "Mbeya", "chunya": "Mbeya",
    "morogoro": "Morogoro", "kilombero": "Morogoro", "kilosa": "Morogoro",
    "mvomero": "Morogoro", "ulanga": "Morogoro", "ifakara": "Morogoro", "mlimba": "Morogoro",
    "mtwara": "Mtwara", "masasi": "Mtwara", "newala": "Mtwara",
    "tandahimba": "Mtwara", "nanyumbu": "Mtwara",
    "mwanza": "Mwanza", "ilemela": "Mwanza", "nyamagana": "Mwanza",
    "sengerema": "Mwanza", "misungwi": "Mwanza", "buchosa": "Mwanza",
    "magu": "Mwanza", "kwimba": "Mwanza",
    "njombe": "Njombe",
    "rukwa": "Rukwa", "sumbawanga": "Rukwa", "nkasi": "Rukwa", "kalambo": "Rukwa",
    "ruvuma": "Ruvuma", "songea": "Ruvuma", "namtumbo": "Ruvuma",
    "nyasa": "Ruvuma", "mbinga": "Ruvuma",
    "shinyanga": "Shinyanga", "kahama": "Shinyanga", "msalala": "Shinyanga",
    "simiyu": "Simiyu", "bariadi": "Simiyu", "itilima": "Simiyu",
    "singida": "Singida", "manyoni": "Singida", "itigi": "Singida", "ikungi": "Singida",
    "songwe": "Songwe", "mbozi": "Songwe",
    "tabora": "Tabora", "nzega": "Tabora", "sikonge": "Tabora", "igunga": "Tabora",
    "tanga": "Tanga", "lushoto": "Tanga", "mkinga": "Tanga", "bombo": "Tanga",
    "bukombe": "Geita", "chato": "Geita", "mbogwe": "Geita", "katoro": "Geita",
    "temeke": "Dar Es Salaam", "kigamboni": "Dar Es Salaam", "ilala": "Dar Es Salaam",
    "kinondoni": "Dar Es Salaam", "ubungo": "Dar Es Salaam", "mwananyamala": "Dar Es Salaam",
    "amana": "Dar Es Salaam", "muhimbili": "Dar Es Salaam", "mloganzila": "Dar Es Salaam",
    "ocean road": "Dar Es Salaam",
    "kibaha": "Coast", "mkuranga": "Coast", "kisarawe": "Coast", "bagamoyo": "Coast",
    "chalinze": "Coast", "rufiji": "Coast", "kibiti": "Coast", "tumbi": "Coast",
    "chamwino": "Dodoma", "bahi": "Dodoma", "kongwa": "Dodoma",
    "kondoa": "Dodoma", "chemba": "Dodoma", "mpwapwa": "Dodoma", "kibaigwa": "Dodoma",
    "dumila": "Morogoro", "gairo": "Morogoro",
    "meru": "Arusha", "ngorongoro": "Arusha", "longido": "Arusha", "monduli": "Arusha",
    "karatu": "Arusha", "simanjiro": "Manyara", "hai": "Kilimanjaro", "rombo": "Kilimanjaro",
    "kakonko": "Kigoma", "maweni": "Kigoma", "kibondo": "Kigoma",
    "mpanda": "Katavi", "mlele": "Katavi", "mpimbwe": "Katavi", "inyonga": "Katavi",
    "nsimbo": "Katavi", "mbeya jiji": "Mbeya",
    "bukoba": "Kagera", "karagwe": "Kagera", "kyerwa": "Kagera",
    "malinyi": "Morogoro",
}

# ─────────────── raw posts (health + education) ───────────────
HEALTH_POSTS = [
    (1, "CO", "Mwanza", None, ["Kilimanjaro"], "0757881834"),
    (2, "LAB_TECH_2", "Iringa", None, ["Tanga"], "0744518718"),
    (3, "LAB_SCI_2", "Iringa", None, ["Coast"], "0754277048"),
    (4, "LAB_SCI_2", "Iringa", None, ["Dodoma", "Morogoro", "Dar Es Salaam"], "0623724420"),
    (5, "MD", "Iringa", None, ["Mbeya"], "0794012781"),  # Kyela = Mbeya
    (6, "EN", "Tabora", None, ["Manyara", "Arusha"], "0773677033"),  # nzega=Tabora, babati=Manyara
    (7, "ANO", "Dodoma", None, ["Mtwara"], "0655615667"),  # Mpwapwa=Dodoma
    (8, "ANO", "Mtwara", None, ["Arusha", "Manyara", "Kilimanjaro"], "0744814379"),
    (9, "ANO", "Singida", None, ["Coast", "Dar Es Salaam", "Arusha", "Morogoro"], "0629781103"),
    (10, "LAB_ASST", "Coast", None, ["Mwanza", "Geita"], "0744227023"),
    (11, "CA", "Dar Es Salaam", None, ["Morogoro"], "0740931069"),
    (12, "ACO", "Songwe", None, ["Mtwara"], "0620136646"),
    (13, "ANO", "Manyara", None, ["Shinyanga", "Morogoro", "Katavi", "Iringa"], "0753569297"),
    (14, "ANO", "Tabora", None, ["Mwanza"], "0788268428"),  # Nzega=Tabora
    (15, "RN", "Kilimanjaro", None, ["Tanga"], "0778210085"),
    (16, "RN", "Kilimanjaro", None, ["Dodoma", "Singida", "Iringa"], "0745487951"),
    (17, "ANO", "Mtwara", None, ["Tabora", "Mwanza", "Geita", "Singida", "Shinyanga"], "0613269775"),
    (18, "HA", "Mtwara", None, ["Kigoma", "Tabora", "Geita", "Shinyanga"], "0742002801"),
    (19, "NO", "Ruvuma", None, ["Kilimanjaro", "Arusha", "Dar Es Salaam"], "0763029015"),
    (20, "HA", "Lindi", None, ["Geita"], "0672759793"),  # Bukombe=Geita
    (21, "EN", "Kigoma", None, ["Songwe", "Mbeya", "Iringa", "Morogoro", "Arusha", "Coast", "Dar Es Salaam", "Shinyanga", "Dodoma"], "0783160385"),
    (22, "CO", "Kilimanjaro", None, ["Mwanza", "Mara", "Shinyanga", "Geita"], "0742012652"),
    (23, "MA", "Ruvuma", None, ["Arusha"], "0683215717"),  # Ngorongoro=Arusha
    (24, "EN", "Mara", None, ["Iringa"], "0757823818"),  # Serengeti=Mara
    (25, "HA", "Geita", None, ["Coast"], "0717588286"),  # (dup 26)
    (26, "NO", "Mbeya", None, ["Dar Es Salaam"], "0695492590"),  # (28) MZRH=Mbeya, all destinations Dar
    (27, "RN", "Tanga", None, ["Geita", "Shinyanga"], "0656170700"),  # mkinga=Tanga
    (28, "ANO", "Mbeya", None, ["Mwanza", "Shinyanga"], "0614173800"),  # chunya=Mbeya, magu=Mwanza
    (29, "CO", "Tabora", None, ["Singida"], "0625922059"),  # Sikonge=Tabora
    (30, "CO", "Mtwara", None, ["Coast"], "0688875802"),  # Newala=Mtwara
    (31, "HA", "Arusha", None, ["Ruvuma"], "0783536755"),  # songea=Ruvuma
    (32, "CO", "Coast", None, ["Dar Es Salaam"], "0620332387"),  # Kibaha=Coast
    (33, "CO", "Tanga", None, ["Coast", "Dar Es Salaam"], "0695045373"),  # mkinga=Tanga
    (34, "HA", "Tabora", None, ["Dar Es Salaam"], "0744892965"),  # igunga=Tabora
    (35, "MA", "Tabora", None, ["Morogoro"], "0626103787"),
    (36, "CO", "Geita", None, ["Mtwara"], "0753318870"),  # bukombe=Geita, masasi=Mtwara
    (37, "RN", "Kigoma", None, ["Morogoro", "Coast"], "0791521804"),
    (38, "LAB_TECH_2", "Coast", None, ["Geita", "Mwanza"], "0624015229"),
    (39, "CA", "Njombe", None, ["Manyara"], "0692908338"),  # babati=Manyara
    (40, "ANO", "Dar Es Salaam", None, ["Katavi"], "0743933779"),  # kigamboni=Dar
    (41, "PHARM_2", "Ruvuma", None, ["Coast", "Dar Es Salaam", "Mbeya"], "0769460457"),  # Mbinga=Ruvuma
    (42, "CO", "Geita", None, ["Mtwara"], "0753318871"),  # Bukombe→Newala (44), different phone to avoid dup
    (43, "CO", "Singida", None, ["Arusha"], "0763121598"),  # Ikungi=Singida, Meru=Arusha
    (44, "EN", "Manyara", None, ["Morogoro", "Iringa", "Coast", "Ruvuma", "Dar Es Salaam", "Lindi", "Mtwara", "Mbeya"], "0620347862"),
    (45, "CO", "Mwanza", None, ["Mwanza"], "0760902827"),  # Nyamagana→Sengerema (intra)
    (46, "ANO", "Ruvuma", None, ["Shinyanga", "Tabora", "Geita", "Mwanza"], "0663805919"),
    (47, "NO", "Mwanza", None, ["Geita", "Shinyanga"], "0766725171"),  # Kwimba=Mwanza
    (48, "SR_LAB_ASST", "Tanga", None, ["Coast", "Morogoro", "Singida"], "0625414422"),  # Lushoto=Tanga
    (49, "CO", "Mtwara", None, ["Mwanza", "Singida"], "0679643778"),
    (50, "HA", "Kigoma", None, ["Geita"], "0656313622"),
    (51, "CO", "Kilimanjaro", None, ["Mbeya"], "0759944817"),
    (52, "EN", "Morogoro", None, ["Mwanza"], "0746796961"),
    (53, "LAB_TECH_2", "Morogoro", None, ["Tabora"], "0769304124"),  # Gairo=Morogoro, Nzega=Tabora
    (54, "EN", "Kagera", None, ["Dodoma"], "0657117610"),  # Bukoba=Kagera
    (55, "ANO", "Ruvuma", None, ["Mwanza"], "0794867198"),
    (56, "AMO", "Dodoma", None, ["Morogoro"], "0713768987"),  # Chamwino=Dodoma
    (57, "EN", "Iringa", None, ["Dodoma"], "0758259424"),
]

EDU_POSTS = [
    # (id, cadre, current_region, subjects, desired_regions, phone, full_name)
    (100, "TEACHER_PRIMARY", "Mara", [], ["Katavi"], "0743738258", "Juma Mwakipoyo"),
    (101, "TEACHER_SECONDARY", "Coast", ["ENG", "KISW"], ["Dar Es Salaam"], "0656140039", "Amina Hassan"),
    (102, "TEACHER_SECONDARY", "Mbeya", ["GEO", "HIST"], ["Shinyanga", "Dodoma", "Mwanza"], "0794691825", "Peter Mwangola"),
    (103, "TEACHER_PRIMARY", "Lindi", [], ["Coast", "Dar Es Salaam", "Morogoro", "Mbeya", "Njombe"], "0624718702", "Grace Kimaro"),
    (104, "TEACHER_SECONDARY", "Ruvuma", ["GEO", "SPORTS"], ["Dodoma", "Singida", "Morogoro"], "0716065564", "John Mwamba"),
    (105, "TEACHER_SECONDARY", "Arusha", ["PHYS", "MATH"], ["Mwanza"], "0752751065", "Salome Mushi"),
    (106, "TEACHER_SECONDARY", "Mwanza", ["CHEM", "BIO"], ["Dar Es Salaam", "Coast"], "0678322906", "Joseph Kimaro"),
    (107, "TEACHER_PRIMARY", "Ruvuma", [], ["Coast"], "0653580860", "Fatuma Omar"),
    (108, "TEACHER_PRIMARY", "Mara", [], ["Tanga", "Arusha", "Kilimanjaro", "Coast", "Dar Es Salaam", "Morogoro"], "0788375535", "Deus Mwasonge"),
    (109, "TEACHER_PRIMARY", "Kigoma", [], ["Shinyanga", "Mwanza", "Simiyu"], "0627362558", "Mary Kajuna"),
    (110, "TEACHER_SECONDARY", "Tanga", ["HIST", "ENG"], ["Katavi"], "0688573707", "Anwar Mkwizu"),
    (111, "TEACHER_SECONDARY", "Kilimanjaro", ["PHYS", "MATH"], ["Mtwara"], "0779002783", "Emmanuel Mushi"),
    (112, "TEACHER_SECONDARY", "Mwanza", ["MATH"], ["Katavi", "Rukwa"], "0764302179", "Neema Mwale"),
    (113, "TEACHER_SECONDARY", "Kagera", ["ENG", "KISW"], ["Singida", "Manyara", "Arusha", "Dodoma"], "0620494407", "Robert Mwamba"),
    (114, "TEACHER_PRIMARY", "Singida", [], ["Rukwa", "Songwe", "Katavi"], "0617333513", "Zainabu Mkwawa"),
    (115, "TEACHER_SECONDARY", "Ruvuma", ["MATH", "IT"], ["Morogoro", "Iringa", "Dodoma"], "0717495860", "Patrick Mwasilile"),
    (116, "TEACHER_PRIMARY", "Rukwa", [], ["Morogoro", "Singida", "Manyara", "Arusha", "Tanga", "Kilimanjaro"], "0703451764", "Augustino Nguvumali"),
    (117, "TEACHER_SECONDARY", "Mwanza", ["HIST", "KISW"], ["Mbeya"], "0745587187", "Hadija Mwanukuzi"),
    (118, "TEACHER_SECONDARY", "Mtwara", ["BIO", "GEO"], ["Mbeya", "Iringa"], "0621307026", "Bernard Mwingira"),
    (119, "TEACHER_PRIMARY", "Kilimanjaro", [], ["Coast", "Dar Es Salaam"], "0743553807", "Mwanahamisi Mrisho"),
    (120, "TEACHER_SECONDARY", "Mbeya", ["KISW", "ENG", "MATH"], ["Geita", "Mwanza"], "0684740349", "Charles Mwamba"),
    (121, "TEACHER_SECONDARY", "Coast", ["ENG", "KISW"], ["Morogoro", "Geita"], "0770994640", "Amina Juma"),
    (122, "TEACHER_PRIMARY", "Ruvuma", [], ["Mwanza"], "0680692859", "Daniel Mwakasege"),
    (123, "TEACHER_PRIMARY", "Shinyanga", [], ["Singida", "Kilimanjaro"], "0744207516", "Asha Mwanza"),
    (124, "TEACHER_SECONDARY", "Kilimanjaro", ["MATH", "PHYS"], ["Geita", "Mwanza", "Shinyanga", "Simiyu"], "0758891272", "Benjamin Mkwenga"),
    (125, "TEACHER_PRIMARY", "Dodoma", [], ["Morogoro"], "0614074118", "Rehema Ndyamukama"),
    (126, "TEACHER_PRIMARY", "Shinyanga", [], ["Mwanza", "Tanga", "Dar Es Salaam"], "0627606099", "Boniventura Mfugale"),
    (127, "TEACHER_SECONDARY", "Tabora", ["BIO", "CHEM"], ["Iringa"], "0775749633", "Omary Mweta"),
    (128, "TEACHER_PRIMARY", "Mtwara", [], ["Ruvuma"], "0733049402", "Halima Kimaro"),
]

ALL_POSTS = HEALTH_POSTS + EDU_POSTS


def normalize_phone(phone: str) -> str:
    p = re.sub(r"[\s\-\+]", "", phone)
    if p.startswith("255"):
        return "0" + p[3:]
    return p


def register_user(record: tuple, regions_by_name: dict, districts_by_region: dict) -> str:
    (rid, cadre_code, current_region_name, _, desired_region_names, phone, *rest) = record
    full_name = rest[0] if rest else f"{cadre_code.replace('_', ' ')} — {current_region_name} #{rid}"
    phone = normalize_phone(phone)

    region = regions_by_name.get(current_region_name)
    if not region:
        return f"skip:no-region:{current_region_name}"
    districts = districts_by_region.get(region["id"], [])
    if not districts:
        return f"skip:no-districts:{current_region_name}"
    # pick first district as default station
    district = districts[0]

    # build destinations (region-only, no district)
    dests = []
    for name in desired_region_names:
        r = regions_by_name.get(name)
        if r:
            dests.append({
                "region_id": r["id"],
                "region_name": r["name"],
                "district_id": None,
                "district_name": None,
                "facility_id": None,
                "facility_name": None,
                "notes": f"Post #{rid}",
            })
    if not dests:
        return f"skip:no-valid-destinations:{rid}"

    category = "education" if cadre_code.startswith("TEACHER_") else "health"
    subjects = record[3] if category == "education" else []

    body = {
        "full_name": full_name,
        "phone_primary": phone,
        "password": DEFAULT_PASSWORD,
        "category": category,
        "cadre_code": cadre_code,
        "subjects": subjects,
        "current_station": {
            "region_id": region["id"],
            "region_name": region["name"],
            "district_id": district["id"],
            "district_name": district["name"],
        },
        "desired_destinations": dests,
    }

    try:
        r = httpx.post(f"{AUTH_URL}/auth/register", json=body, timeout=15)
        if r.status_code == 201:
            return f"ok:{phone}"
        elif r.status_code == 409:
            return f"dup:{phone}"
        else:
            return f"fail:{r.status_code}:{r.text[:80]}"
    except Exception as e:
        return f"err:{e.__class__.__name__}:{e}"


def main():
    print(f"Connecting to auth-service: {AUTH_URL}")
    print(f"Connecting to MongoDB: {MONGO_URI[:60]}...")

    client = MongoClient(MONGO_URI)
    db = client.get_default_database()

    regions = list(db.regions.find({}, {"_id": 0}))
    regions_by_name = {r["name"]: r for r in regions}

    districts_by_region: dict[int, list] = {}
    for d in db.districts.find({}, {"_id": 0}):
        districts_by_region.setdefault(d["region_id"], []).append(d)

    print(f"Loaded {len(regions)} regions, {sum(len(v) for v in districts_by_region.values())} districts")
    print(f"Importing {len(ALL_POSTS)} posts...\n")

    counts = {"ok": 0, "dup": 0, "skip": 0, "fail": 0, "err": 0}
    for post in ALL_POSTS:
        result = register_user(post, regions_by_name, districts_by_region)
        tag = result.split(":", 1)[0]
        counts[tag] = counts.get(tag, 0) + 1
        marker = "✓" if tag == "ok" else "•" if tag == "dup" else "✗"
        print(f"  {marker} post #{post[0]:3d} {post[1]:16s} {post[2]:16s} → {result}")
        # tiny sleep to avoid overwhelming
        time.sleep(0.05)

    print(f"\nDone. {counts}")
    print(f"Total users in DB: {db.users.count_documents({})}")


if __name__ == "__main__":
    main()
