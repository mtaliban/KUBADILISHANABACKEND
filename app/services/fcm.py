"""Firebase Cloud Messaging push notification service.

Initialises the Firebase Admin SDK from a local service-account JSON
and exposes helpers to send push notifications to individual users or
broadcast to all users with stored FCM tokens.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_firebase_app = None
_initialized = False


def _get_firebase_app():
    """Lazy-initialise the Firebase Admin SDK."""
    global _firebase_app, _initialized
    if _initialized:
        return _firebase_app

    try:
        import firebase_admin
        from firebase_admin import credentials

        # Look for service account JSON in several locations
        candidates = [
            os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH", ""),
            str(Path(__file__).resolve().parent.parent.parent / "config" / "firebase-service-account.json"),
            str(Path.home() / "Documents" / "KUBADILISHANA_VITUO" / "backend" / "config" / "firebase-service-account.json"),
        ]

        cred_path = None
        for c in candidates:
            if c and os.path.isfile(c):
                cred_path = c
                break

        # If no file found, try environment variable with JSON content
        if not cred_path:
            svc_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
            if svc_json:
                info = json.loads(svc_json)
                cred = credentials.Certificate(info)
                _firebase_app = firebase_admin.initialize_app(cred)
                _initialized = True
                logger.info("[FCM] Firebase Admin SDK initialised from env var")
                return _firebase_app

        if cred_path:
            cred = credentials.Certificate(cred_path)
            _firebase_app = firebase_admin.initialize_app(cred)
            _initialized = True
            logger.info("[FCM] Firebase Admin SDK initialised from %s", cred_path)
            return _firebase_app

        logger.warning("[FCM] No Firebase service account found — push notifications disabled")
        _initialized = True  # Don't retry
        return None

    except Exception as exc:
        logger.error("[FCM] Failed to initialise Firebase Admin SDK: %s", exc)
        _initialized = True
        return None


async def send_push_to_user(
    user_id: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    image: Optional[str] = None,
) -> dict:
    """Send a push notification to a single user via FCM.

    Looks up the user's stored FCM tokens from the database and sends
    to all registered devices.
    """
    from ..db import get_db

    app = _get_firebase_app()
    if not app:
        logger.debug("[FCM] Firebase not initialised — skipping push for user %s", user_id)
        return {"sent": 0, "error": "firebase_not_initialised"}

    try:
        from firebase_admin import messaging

        db = get_db()
        user = await db.users.find_one({"_id": user_id})
        if not user:
            return {"sent": 0, "error": "user_not_found"}

        tokens = user.get("fcm_tokens", [])
        if not tokens:
            return {"sent": 0, "error": "no_tokens"}

        # Build notification
        notification = messaging.Notification(
            title=title,
            body=body,
            image=image,
        )

        # Android config
        android_config = messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                title=title,
                body=body,
                image=image,
                click_action="FLUTTER_NOTIFICATION_CLICK",
                channel_id="kubadilishana",
            ),
        )

        # APNs config (iOS)
        apns_config = messaging.APNSConfig(
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(title=title, body=body),
                    badge=1,
                    sound="default",
                )
            )
        )

        # Send to multiple tokens (batch)
        sent_count = 0
        failed_tokens = []
        batch_size = 500  # FCM limit per batch

        for i in range(0, len(tokens), batch_size):
            batch_tokens = tokens[i : i + batch_size]
            messages = [
                messaging.Message(
                    notification=notification,
                    android=android_config,
                    apns=apns_config,
                    token=token,
                    data=data or {},
                )
                for token in batch_tokens
            ]

            response = await messaging.send_each_async(messages)
            sent_count += response.success_count

            # Collect failed tokens for cleanup
            for idx, resp in enumerate(response.responses):
                if not resp.success:
                    failed_tokens.append(batch_tokens[idx])
                    logger.warning("[FCM] Failed to send to %s: %s", batch_tokens[idx], resp.exception)

        # Remove invalid tokens from DB
        if failed_tokens:
            await db.users.update_one(
                {"_id": user_id},
                {"$pull": {"fcm_tokens": {"$in": failed_tokens}}},
            )
            logger.info("[FCM] Removed %d invalid tokens for user %s", len(failed_tokens), user_id)

        return {"sent": sent_count, "failed": len(failed_tokens), "total": len(tokens)}

    except Exception as exc:
        logger.error("[FCM] Push failed for user %s: %s", user_id, exc)
        return {"sent": 0, "error": str(exc)}


async def send_push_to_admins(
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> dict:
    """Send push notification to all admin users."""
    from ..db import get_db

    db = get_db()
    admins = await db.users.find({"is_admin": True}).to_list(100)
    total_sent = 0

    for admin in admins:
        result = await send_push_to_user(
            user_id=str(admin["_id"]),
            title=title,
            body=body,
            data=data,
        )
        total_sent += result.get("sent", 0)

    return {"admins_notified": len(admins), "total_sent": total_sent}
