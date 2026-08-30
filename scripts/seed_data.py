"""Seed MongoDB with reference data: regions, districts, schools, facilities, cadres, subjects.

Run:
    docker exec kv_mongodb mongosh --quiet   # or:
    python scripts/seed_data.py
"""
import json
import os
from pathlib import Path
from pymongo import MongoClient, ASCENDING

# seed_data.py lives in backend/scripts/. Data is at the backend repo root
# (tanzania_data/ + tanzania_health_data/ — reference data included in the repo
# so every clone/deploy has it). Override with TZ_EDU_DIR/TZ_HEALTH_DIR env vars.
ROOT = Path(__file__).resolve().parent.parent
TZ_EDU = Path(os.getenv("TZ_EDU_DIR", ROOT / "tanzania_data" / "json"))
TZ_HEALTH = Path(os.getenv("TZ_HEALTH_DIR", ROOT / "tanzania_health_data" / "json"))

MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb://admin:changeme@localhost:27017/kubadilishana_vituo?authSource=admin",
)


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


CADRES = [
    # Afya — Wizara ya Afya (specialist — hospitali za rufaa/taifa)
    {"code": "MED_SPECIALIST", "category": "health", "display_name": "Medical Specialist", "requires_subjects": False, "sector": "wizara_afya"},
    {"code": "DENTAL_SPECIALIST", "category": "health", "display_name": "Dental Specialist", "requires_subjects": False, "sector": "wizara_afya"},
    {"code": "BIOMED_ENG_2", "category": "health", "display_name": "Biomedical Engineer II", "requires_subjects": False, "sector": "wizara_afya"},
    {"code": "CHEM_2", "category": "health", "display_name": "Chemist II", "requires_subjects": False, "sector": "wizara_afya"},
    {"code": "HEALTH_RECORDER_2", "category": "health", "display_name": "Health Recorder II", "requires_subjects": False, "sector": "wizara_afya"},
    {"code": "OPHTHALMIC", "category": "health", "display_name": "Ophthalmic Optician II", "requires_subjects": False, "sector": "wizara_afya"},
    # Afya — Wote (Wizara + TAMISEMI)
    {"code": "CO", "category": "health", "display_name": "Clinical Officer", "requires_subjects": False, "sector": "all"},
    {"code": "ACO", "category": "health", "display_name": "Assistant Clinical Officer", "requires_subjects": False, "sector": "all"},
    {"code": "CA", "category": "health", "display_name": "Clinical Assistant", "requires_subjects": False, "sector": "all"},
    {"code": "AMO", "category": "health", "display_name": "Assistant Medical Officer", "requires_subjects": False, "sector": "all"},
    {"code": "MD", "category": "health", "display_name": "Medical Doctor (MD)", "requires_subjects": False, "sector": "all"},
    {"code": "ANO", "category": "health", "display_name": "Assistant Nursing Officer (ANO)", "requires_subjects": False, "sector": "all"},
    {"code": "NO", "category": "health", "display_name": "Nursing Officer (NO)", "requires_subjects": False, "sector": "all"},
    {"code": "EN", "category": "health", "display_name": "Enrolled Nurse (EN)", "requires_subjects": False, "sector": "all"},
    {"code": "RN", "category": "health", "display_name": "Registered Nurse (RN)", "requires_subjects": False, "sector": "all"},
    {"code": "LAB_TECH_1", "category": "health", "display_name": "Laboratory Technologist I", "requires_subjects": False, "sector": "all"},
    {"code": "LAB_TECH_2", "category": "health", "display_name": "Laboratory Technologist II", "requires_subjects": False, "sector": "all"},
    {"code": "LAB_SCI_2", "category": "health", "display_name": "Laboratory Scientist II", "requires_subjects": False, "sector": "all"},
    {"code": "LAB_ASST", "category": "health", "display_name": "Laboratory Assistant", "requires_subjects": False, "sector": "all"},
    {"code": "SR_LAB_ASST", "category": "health", "display_name": "Senior Laboratory Assistant I", "requires_subjects": False, "sector": "all"},
    {"code": "MALT", "category": "health", "display_name": "Medical Laboratory Technologist", "requires_subjects": False, "sector": "all"},
    {"code": "PHARM_2", "category": "health", "display_name": "Pharmacist II", "requires_subjects": False, "sector": "all"},
    {"code": "HA", "category": "health", "display_name": "Health Assistant (HA)", "requires_subjects": False, "sector": "all"},
    {"code": "MA", "category": "health", "display_name": "Medical Attendant (MA)", "requires_subjects": False, "sector": "all"},

    # Elimu
    {"code": "TEACHER_PRIMARY", "category": "education", "display_name": "Mwalimu wa Elimu ya Msingi", "requires_subjects": False, "level": "Primary"},
    {"code": "TEACHER_SECONDARY", "category": "education", "display_name": "Mwalimu wa Elimu ya Sekondari", "requires_subjects": True, "level": "Secondary"},
    {"code": "TEACHER_SPECIAL", "category": "education", "display_name": "Mwalimu wa Elimu ya Pekee (Special Needs)", "requires_subjects": False, "level": "Primary"},
]

SUBJECTS = [
    # Shule ya MSINGI (Elimu ya Msingi) — Taaluma zote za mtaala wa TZ
    {"code": "KISW_MSINGI", "name": "Kiswahili (Msingi)", "level": "Primary"},
    {"code": "ENGLISH_MSINGI", "name": "English (Primary)", "level": "Primary"},
    {"code": "HISABATI_MSINGI", "name": "Hisabati (Msingi)", "level": "Primary"},
    {"code": "SAYANSI_MSINGI", "name": "Sayansi na Teknolojia (Msingi)", "level": "Primary"},
    {"code": "JAMII_MSINGI", "name": "Jamiituni (Msingi)", "level": "Primary"},
    {"code": "MAZINGIRA_MSINGI", "name": "Mazingira na Ujuzi wa Kuishi (Msingi)", "level": "Primary"},
    {"code": "URAYA_MSINGI", "name": "Uraia na Maadili (Msingi)", "level": "Primary"},
    {"code": "MICHEZO_MSINGI", "name": "Michezo na Sanaa (Msingi)", "level": "Primary"},
    {"code": "DINI_MSINGI", "name": "Elimu ya Dini (Msingi)", "level": "Primary"},
    # Shule ya SEKONDARI
    {"code": "MATH", "name": "Mathematics", "level": "Secondary"},
    {"code": "PHYS", "name": "Physics", "level": "Secondary"},
    {"code": "CHEM", "name": "Chemistry", "level": "Secondary"},
    {"code": "BIO", "name": "Biology", "level": "Secondary"},
    {"code": "ENG", "name": "English", "level": "Secondary"},
    {"code": "KISW", "name": "Kiswahili", "level": "Secondary"},
    {"code": "HIST", "name": "History", "level": "Secondary"},
    {"code": "GEO", "name": "Geography", "level": "Secondary"},
    {"code": "IT", "name": "Information & Computer Studies", "level": "Secondary"},
    {"code": "SPORTS", "name": "Sports / Physical Education", "level": "Secondary"},
    {"code": "CIVICS", "name": "Civics", "level": "Secondary"},
    {"code": "COMM", "name": "Commerce", "level": "Secondary"},
    {"code": "BOOK", "name": "Book Keeping", "level": "Secondary"},
    {"code": "AGRIC", "name": "Agriculture", "level": "Secondary"},
]


def main():
    client = MongoClient(MONGO_URI)
    db = client.get_default_database()
    print(f"Connected: {db.name}")

    regions = load_json(TZ_EDU / "regions.json")
    districts = load_json(TZ_EDU / "districts.json")
    schools = load_json(TZ_EDU / "schools.json")
    facilities = load_json(TZ_HEALTH / "facilities.json")

    print(f"Loading {len(regions)} regions...")
    db.regions.delete_many({})
    db.regions.insert_many(regions)

    print(f"Loading {len(districts)} districts...")
    db.districts.delete_many({})
    db.districts.insert_many(districts)

    print(f"Loading {len(schools)} schools...")
    db.schools.delete_many({})
    # bulk insert in chunks of 5000
    for i in range(0, len(schools), 5000):
        db.schools.insert_many(schools[i:i+5000])

    print(f"Loading {len(facilities)} health facilities...")
    db.health_facilities.delete_many({})
    for i in range(0, len(facilities), 5000):
        db.health_facilities.insert_many(facilities[i:i+5000])

    print(f"Loading {len(CADRES)} cadres...")
    db.cadres.delete_many({})
    db.cadres.insert_many(CADRES)

    print(f"Loading {len(SUBJECTS)} subjects...")
    db.subjects.delete_many({})
    db.subjects.insert_many(SUBJECTS)

    print("\nDone. Counts:")
    for coll in ["regions", "districts", "schools", "health_facilities", "cadres", "subjects"]:
        print(f"  {coll}: {db[coll].count_documents({})}")


if __name__ == "__main__":
    main()
