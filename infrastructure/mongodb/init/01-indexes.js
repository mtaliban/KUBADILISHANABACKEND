// Runs once on first container start
db = db.getSiblingDB('kubadilishana_vituo');

db.users.createIndex({ phone_primary: 1 }, { unique: true });
db.users.createIndex({ email: 1 }, { unique: true, sparse: true });
db.email_verifications.createIndex({ user_id: 1 });
db.email_verifications.createIndex({ expires_at: 1 }, { expireAfterSeconds: 0 });
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

// Performance: pages zinaload haraka (queries za kila siku)
db.users.createIndex({ created_at: -1 });
db.users.createIndex({ last_seen_at: -1 });
db.users.createIndex({ status: 1, category: 1, is_admin: 1, created_at: -1 });
db.users.createIndex({ "desired_destinations.region_id": 1 });

db.notifications.createIndex({ user_id: 1, created_at: -1 });
db.notifications.createIndex({ user_id: 1, read: 1 });

db.messages.createIndex({ to_user_id: 1, read: 1 });
db.messages.createIndex({ conversation_id: 1, created_at: -1 });

db.page_views.createIndex({ visited_at: -1 });
db.page_views.createIndex({ user_id: 1, visited_at: -1 });

db.login_otps.createIndex({ user_id: 1, purpose: 1 });
db.login_otps.createIndex({ expires_at: 1 }, { expireAfterSeconds: 0 });

db.call_logs.createIndex({ created_at: -1 });
db.call_logs.createIndex({ from_user_id: 1, created_at: -1 });

db.payments.createIndex({ status: 1, created_at: -1 });

print('Indexes created.');
