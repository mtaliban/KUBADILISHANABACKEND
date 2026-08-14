"""Canonical MQTT topics for the whole backend."""

# User lifecycle
TOPIC_USER_REGISTERED = "kv/user/registered"
TOPIC_USER_PROFILE_UPDATED = "kv/user/profile_updated"
TOPIC_USER_DESTINATION_CHANGED = "kv/user/destination_changed"
TOPIC_USER_STATION_CHANGED = "kv/user/station_changed"
TOPIC_USER_PREFS_UPDATED = "kv/user/prefs_updated"
TOPIC_USER_UPDATED_BY_ADMIN = "kv/user/updated_by_admin"
TOPIC_USER_DELETED = "kv/user/deleted"
TOPIC_USER_ADMIN_CHANGED = "kv/user/admin_changed"
TOPIC_USER_PRESENCE = "kv/user/presence"
TOPIC_USER_PASSWORD_RESET_REQUESTED = "kv/user/password_reset_requested"
TOPIC_USER_PASSWORD_RESET_COMPLETED = "kv/user/password_reset_completed"
TOPIC_EMAIL_VERIFICATION_REQUESTED = "kv/user/email_verification_requested"
TOPIC_EMAIL_VERIFIED = "kv/user/email_verified"

# Matching
TOPIC_MATCH_FOUND = "kv/match/found"

# Messaging
TOPIC_MESSAGE_SENT = "kv/message/sent"       # + "/{recipient_user_id}"
TOPIC_CALL_INITIATED = "kv/call/initiated"   # + "/{recipient_user_id}"

# Payments (manual donation verification)
TOPIC_PAYMENT_SUBMITTED = "kv/payment/submitted"  # + "/{user_id}"
TOPIC_PAYMENT_APPROVED = "kv/payment/approved"    # + "/{user_id}"
TOPIC_PAYMENT_REJECTED = "kv/payment/rejected"    # + "/{user_id}"

# Admin announcements (broadcast to users)
TOPIC_ANNOUNCEMENT = "kv/announcement"  # + "/{recipient_user_id}"

# Reference data management (admin CRUD — idara/masomo/kada/mikoa/wilaya/vituo)
TOPIC_DATA_DEPARTMENTS_CHANGED = "kv/data/departments_changed"
TOPIC_DATA_SUBJECTS_CHANGED = "kv/data/subjects_changed"
TOPIC_DATA_CADRES_CHANGED = "kv/data/cadres_changed"
TOPIC_DATA_REGIONS_CHANGED = "kv/data/regions_changed"
TOPIC_DATA_DISTRICTS_CHANGED = "kv/data/districts_changed"
TOPIC_DATA_FACILITIES_CHANGED = "kv/data/facilities_changed"

# Analytics
TOPIC_PAGE_VIEWED = "kv/page/viewed"

# Wildcards
TOPIC_ALL_USER = "kv/user/#"
TOPIC_ALL_MATCH = "kv/match/#"
TOPIC_ALL_MESSAGE = "kv/message/#"
TOPIC_ALL_CALL = "kv/call/#"
TOPIC_ALL = "kv/#"
