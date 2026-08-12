#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# SAFISHA DATA ZA MAJARIBIO (users za fake) — weka ADMIN pekee.
# Endesha KWENYE SERVER (EC2) ambapo docker-compose inaendesha:
#
#     cd <backend dir kwenye server> && bash scripts/cleanup_fake_data.sh
#
# Inafuta: users wote isipokuwa admin + matches/messages/notifications/
# call_logs/event_log/password_resets/email_verifications/page_views/
# payments/announcements.
# HAIyafuti reference data (mikoa, wilaya, vituo, kada, masomo) — hayo
# yanahitajika kwa usajili!
#
# Vigezo vya hiari (env): MONGO_CONTAINER, MONGO_DB, MONGO_ROOT_USER,
# MONGO_ROOT_PASSWORD, ADMIN_EMAIL.
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

CONTAINER="${MONGO_CONTAINER:-kv_mongodb}"
DB="${MONGO_DB:-kubadilishana_vituo}"
MONGO_USER="${MONGO_ROOT_USER:-admin}"
MONGO_PASS="${MONGO_ROOT_PASSWORD:-changeme}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@kubadilishana.go.tz}"

echo "→ Inasafisha data za majaribio kwenye: ${CONTAINER}/${DB} (admin email: ${ADMIN_EMAIL})"
docker exec "${CONTAINER}" mongo --quiet --username "${MONGO_USER}" --password "${MONGO_PASS}" \
  --authenticationDatabase admin "${DB}" <<'EOF'
var admin = db.users.findOne({ email: 'admin@kubadilishana.go.tz' });
if (!admin) { print('ABORT: admin email haijapatikana — angalia ADMIN_EMAIL'); quit(1); }
print('KEEP ADMIN:', admin.full_name, '|', admin.phone_primary, '| id:', admin._id);
db.users.updateOne({ _id: admin._id }, { $set: { email_verified: true, is_admin: true, status: 'active' } });
var del = db.users.deleteMany({ _id: { $ne: admin._id } });
print('Users deleted:', del.deletedCount);
['matches', 'messages', 'notifications', 'call_logs', 'event_log', 'password_resets',
 'email_verifications', 'page_views', 'payments', 'donations', 'announcements', 'follows'].forEach(function (c) {
  try { var r = db[c].deleteMany({}); print('cleared ' + c + ': ' + r.deletedCount); }
  catch (e) { print(c + ': ' + e.message); }
});
print('DONE — users waliobaki: ' + db.users.countDocuments({}));
EOF
echo "→ Imekamilika ✅"
