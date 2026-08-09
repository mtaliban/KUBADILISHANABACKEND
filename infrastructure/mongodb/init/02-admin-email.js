// Ensure the default admin account exists with an email (fresh installs only).
// Runs once on first container start — for existing DBs, run:
//   docker exec kv_mongodb mongosh kubadilishana_vituo --eval 'db.users.updateOne(
//     {phone_primary: "+255763795801"}, {$set: {email: "admin@kubadilishana.go.tz"}})'
db = db.getSiblingDB('kubadilishana_vituo');

const ADMIN_PHONE = '+255763795801'; // 0763795801
const ADMIN_EMAIL = 'admin@kubadilishana.go.tz';

// Attach the email to an existing admin account if one matches the phone.
// (Password is created by the backend seed script / admin route — we never
// hardcode a password hash here; the account's password is set via the
// registration flow or by the operator.)
db.users.updateOne(
  { phone_primary: ADMIN_PHONE },
  { $set: { email: ADMIN_EMAIL }, $setOnInsert: { is_admin: true } }
);

print('Admin email ensured.');
