"""Push notification sender — sends push notifications via FCM/Web Push.

This module provides utilities to send push notifications to users
when events occur (new users, donations, announcements, etc.).

Usage:
    from .push_sender import send_push_to_user, send_push_to_users
    
    # Send to single user
    await send_push_to_user(user_id, title="Mpya!", body="Kuna mtu mpya ameingia")
    
    # Send to multiple users
    await send_push_to_users(user_ids, title="Tangazo", body="Kuna tangazo jipya")
"""

import logging
import json
from typing import Optional

logger = logging.getLogger(__name__)

# Firebase Admin SDK config — set via environment variables
# pip install firebase-admin
# then: FIREBASE_CREDENTIALS=/path/to/serviceAccountKey.json


async def send_push_to_user(
    user_id: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    url: Optional[str] = None,
) -> bool:
    """Send push notification to a single user.
    
    Returns True if at least one notification was sent successfully.
    """
    from ..db import get_db
    
    db = get_db()
    
    # Get user's push tokens
    tokens = await db.push_tokens.find({"user_id": user_id}).to_list(10)
    
    if not tokens:
        logger.debug(f"No push tokens for user {user_id}")
        return False
    
    sent = False
    for token_doc in tokens:
        try:
            provider = token_doc.get("provider", "fcm")
            
            if provider == "fcm":
                success = await _send_fcm(
                    token=token_doc["token"],
                    title=title,
                    body=body,
                    data=data,
                    url=url,
                )
            elif provider == "web-push":
                success = await _send_web_push(
                    endpoint=token_doc["endpoint"],
                    keys=token_doc.get("keys", {}),
                    title=title,
                    body=body,
                    data=data,
                    url=url,
                )
            else:
                continue
            
            if success:
                sent = True
            
        except Exception as e:
            logger.warning(f"Push send failed for token: {e}")
            # Remove invalid tokens
            if "invalid" in str(e).lower() or "not found" in str(e).lower():
                await db.push_tokens.delete_one({"_id": token_doc["_id"]})
    
    return sent


async def send_push_to_users(
    user_ids: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    url: Optional[str] = None,
) -> int:
    """Send push notification to multiple users.
    
    Returns the number of users who received the notification.
    """
    sent_count = 0
    
    for uid in user_ids:
        try:
            if await send_push_to_user(uid, title, body, data, url):
                sent_count += 1
        except Exception as e:
            logger.warning(f"Push send failed for user {uid}: {e}")
    
    return sent_count


async def send_push_to_region(
    region: str,
    title: str,
    body: str,
    exclude_user_id: Optional[str] = None,
    data: Optional[dict] = None,
    url: Optional[str] = None,
) -> int:
    """Send push notification to all users in a region."""
    from ..db import get_db
    
    db = get_db()
    
    # Find users whose home_region matches
    query = {"home_region": region}
    if exclude_user_id:
        query["_id"] = {"$ne": exclude_user_id}
    
    users = await db.users.find(query, {"_id": 1}).to_list(1000)
    user_ids = [str(u["_id"]) for u in users]
    
    return await send_push_to_users(user_ids, title, body, data, url)


async def _send_fcm(
    token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    url: Optional[str] = None,
) -> bool:
    """Send via Firebase Cloud Messaging."""
    try:
        import firebase_admin
        from firebase_admin import messaging
        
        if not firebase_admin._apps:
            # Initialize with default credentials
            # Set GOOGLE_APPLICATION_CREDENTIALS env var
            firebase_admin.initialize_app()
        
        notification = messaging.Notification(
            title=title,
            body=body,
        )
        
        android_config = messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                title=title,
                body=body,
                click_action="OPEN_ACTIVITY",
            ),
        )
        
        webpush_config = messaging.WebpushConfig(
            notification=messaging.WebpushNotification(
                title=title,
                body=body,
                icon="/icon-192.png",
            ),
            data={**(data or {}), "url": url or "/dashboard"},
        )
        
        message = messaging.Message(
            notification=notification,
            android=android_config,
            webpush=webpush_config,
            token=token,
        )
        
        response = messaging.send(message)
        logger.debug(f"FCM sent: {response}")
        return True
        
    except ImportError:
        logger.warning("firebase-admin not installed. Run: pip install firebase-admin")
        return False
    except Exception as e:
        logger.warning(f"FCM send failed: {e}")
        return False


async def _send_web_push(
    endpoint: str,
    keys: dict,
    title: str,
    body: str,
    data: Optional[dict] = None,
    url: Optional[str] = None,
) -> bool:
    """Send via Web Push API (without Firebase)."""
    try:
        from pywebpush import webpush, WebPushException
        
        # VAPID keys — set via environment variables
        import os
        vapid_private_key = os.getenv("VAPID_PRIVATE_KEY")
        vapid_claims = {
            "sub": os.getenv("VAPID_CLAIMS_SUB", "mailto:admin@esstranfer.com"),
        }
        
        if not vapid_private_key:
            logger.warning("VAPID_PRIVATE_KEY not set, skipping web push")
            return False
        
        subscription_info = {
            "endpoint": endpoint,
            "keys": keys,
        }
        
        payload = json.dumps({
            "title": title,
            "body": body,
            "data": data or {},
            "url": url or "/dashboard",
        })
        
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=vapid_private_key,
            vapid_claims=vapid_claims,
        )
        
        logger.debug(f"Web Push sent to {endpoint[:50]}...")
        return True
        
    except ImportError:
        logger.warning("pywebpush not installed. Run: pip install pywebpush")
        return False
    except WebPushException as e:
        logger.warning(f"Web Push failed: {e}")
        return False
    except Exception as e:
        logger.warning(f"Web Push error: {e}")
        return False
