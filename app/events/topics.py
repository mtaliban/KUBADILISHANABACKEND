"""Canonical MQTT topics for the whole backend."""

# User lifecycle
TOPIC_USER_REGISTERED = "kv/user/registered"
TOPIC_USER_PROFILE_UPDATED = "kv/user/profile_updated"
TOPIC_USER_DESTINATION_CHANGED = "kv/user/destination_changed"
TOPIC_USER_STATION_CHANGED = "kv/user/station_changed"

# Matching
TOPIC_MATCH_FOUND = "kv/match/found"

# Messaging
TOPIC_MESSAGE_SENT = "kv/message/sent"       # + "/{recipient_user_id}"
TOPIC_CALL_INITIATED = "kv/call/initiated"   # + "/{recipient_user_id}"

# Wildcards
TOPIC_ALL_USER = "kv/user/#"
TOPIC_ALL_MATCH = "kv/match/#"
TOPIC_ALL_MESSAGE = "kv/message/#"
TOPIC_ALL_CALL = "kv/call/#"
TOPIC_ALL = "kv/#"
