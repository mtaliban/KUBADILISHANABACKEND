// Runs once on first container start
db = db.getSiblingDB('kubadilishana_vituo');

db.users.createIndex({ phone_primary: 1 }, { unique: true });
db.users.createIndex({ category: 1, cadre_code: 1 });
db.users.createIndex({ "current_station.region_id": 1, cadre_code: 1 });
db.users.createIndex({ "desired_destinations.region_id": 1, cadre_code: 1 });
db.users.createIndex({ "desired_destinations.district_id": 1 });
db.users.createIndex({ "desired_destinations.facility_id": 1 });

db.regions.createIndex({ id: 1 }, { unique: true });
db.regions.createIndex({ name: 1 });

db.districts.createIndex({ id: 1 }, { unique: true });
db.districts.createIndex({ region_id: 1 });
db.districts.createIndex({ name: 1 });

db.schools.createIndex({ id: 1 }, { unique: true });
db.schools.createIndex({ district_id: 1, level: 1 });
db.schools.createIndex({ region_id: 1 });

db.health_facilities.createIndex({ code: 1 }, { unique: true });
db.health_facilities.createIndex({ region: 1, district: 1 });

db.cadres.createIndex({ code: 1 }, { unique: true });
db.cadres.createIndex({ category: 1 });

db.subjects.createIndex({ code: 1 }, { unique: true });

db.matches.createIndex({ user_a_id: 1, user_b_id: 1 }, { unique: true });
db.matches.createIndex({ user_a_id: 1, matched_at: -1 });

db.event_log.createIndex({ event_type: 1, occurred_at: -1 });
db.event_log.createIndex({ actor_user_id: 1, occurred_at: -1 });

print('Indexes created.');
