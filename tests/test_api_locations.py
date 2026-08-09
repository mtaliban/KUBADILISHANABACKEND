"""Locations API tests — Redis cache is bypassed via the `cached` identity fixture."""


async def test_list_regions(client, db):
    await db.regions.insert_many([
        {"id": 3, "name": "Dar Es Salaam"},
        {"id": 1, "name": "Arusha"},
        {"id": 17, "name": "Mwanza"},
    ])

    res = await client.get("/locations/regions")
    assert res.status_code == 200
    names = [r["name"] for r in res.json()]
    assert names == ["Arusha", "Dar Es Salaam", "Mwanza"]  # sorted by name


async def test_list_districts_for_region(client, db):
    await db.districts.insert_many([
        {"id": 1, "name": "Arusha Cc", "region_id": 1, "region_name": "Arusha"},
        {"id": 2, "name": "Arusha Dc", "region_id": 1, "region_name": "Arusha"},
        {"id": 3, "name": "Karatu Dc", "region_id": 1, "region_name": "Arusha"},
        {"id": 1701, "name": "Nyamagana Dc", "region_id": 17, "region_name": "Mwanza"},
    ])

    res = await client.get("/locations/regions/1/districts")
    assert res.status_code == 200
    assert [d["name"] for d in res.json()] == ["Arusha Cc", "Arusha Dc", "Karatu Dc"]

    # Unknown region → 404 with no districts.
    missing = await client.get("/locations/regions/99/districts")
    assert missing.status_code == 404


async def test_list_cadres_filtered_and_sorted(client, db):
    await db.cadres.insert_many([
        {"code": "NO", "category": "health", "display_name": "Nursing Officer (NO)"},
        {"code": "CO", "category": "health", "display_name": "Clinical Officer"},
        {"code": "TEACHER_PRIMARY", "category": "education",
         "display_name": "Mwalimu wa Elimu ya Msingi"},
    ])

    health = await client.get("/cadres?category=health")
    assert health.status_code == 200
    codes = [c["code"] for c in health.json()]
    # Sorted by display_name → Clinical Officer before Nursing Officer.
    assert codes == ["CO", "NO"]

    all_res = await client.get("/cadres")
    assert len(all_res.json()) == 3


async def test_list_subjects_by_level(client, db):
    await db.subjects.insert_many([
        {"code": "MATH", "name": "Mathematics", "level": "Secondary"},
        {"code": "KISW", "name": "Kiswahili", "level": "Secondary"},
        {"code": "PRIM_ENG", "name": "English", "level": "Primary"},
    ])

    secondary = await client.get("/cadres/subjects?level=Secondary")
    assert secondary.status_code == 200
    codes = [s["code"] for s in secondary.json()]
    assert codes == ["KISW", "MATH"]  # sorted by name (Kiswahili < Mathematics)

    primary = await client.get("/cadres/subjects?level=Primary")
    assert [s["code"] for s in primary.json()] == ["PRIM_ENG"]
