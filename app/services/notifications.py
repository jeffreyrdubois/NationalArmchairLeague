"""
Web Push notification service.

VAPID keys are auto-generated on first startup and stored in the app_settings
table so they persist across container restarts without any manual configuration.
"""
import json
import logging
import base64
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import AppSetting, PushSubscription, User

logger = logging.getLogger(__name__)


def _get_or_create_vapid_keys(db: Session) -> tuple[str, str]:
    """Return (private_key_pem, public_key_base64url), generating them if needed."""
    priv_row = db.query(AppSetting).filter(AppSetting.key == "vapid_private_key").first()
    pub_row = db.query(AppSetting).filter(AppSetting.key == "vapid_public_key").first()

    if priv_row and pub_row:
        return priv_row.value, pub_row.value

    # Generate a new VAPID key pair
    from py_vapid import Vapid
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    vapid = Vapid()
    vapid.generate_keys()
    private_pem = vapid.private_pem().decode("utf-8")
    pub_bytes = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    public_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode("utf-8")

    db.merge(AppSetting(key="vapid_private_key", value=private_pem))
    db.merge(AppSetting(key="vapid_public_key", value=public_b64))
    db.commit()
    logger.info("Generated new VAPID key pair and stored in app_settings")
    return private_pem, public_b64


def get_vapid_public_key() -> str:
    db = SessionLocal()
    try:
        _, pub = _get_or_create_vapid_keys(db)
        return pub
    finally:
        db.close()


def init_vapid_keys():
    """Called at startup to ensure VAPID keys exist."""
    db = SessionLocal()
    try:
        _get_or_create_vapid_keys(db)
    finally:
        db.close()


def _send_to_subscription(sub: PushSubscription, title: str, body: str, url: str = "/",
                           private_pem: str = None, db: Session = None) -> bool:
    """Send a push to one subscription. Returns False if the subscription is gone."""
    from pywebpush import webpush, WebPushException

    try:
        webpush(
            subscription_info={
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth_key},
            },
            data=json.dumps({"title": title, "body": body, "url": url}),
            vapid_private_key=private_pem,
            vapid_claims={"sub": "mailto:noreply@nal.local"},
        )
        return True
    except WebPushException as e:
        status = e.response.status_code if e.response is not None else None
        if status in (404, 410):
            # Subscription expired/unregistered — remove it
            if db:
                db.delete(sub)
            return False
        logger.warning(f"Push failed for sub {sub.id} (status {status}): {e}")
        return True  # keep the subscription; transient error


def send_to_user(user: User, title: str, body: str, url: str = "/", db: Session = None) -> int:
    """Send a push notification to all active subscriptions for a user. Returns sent count."""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        private_pem, _ = _get_or_create_vapid_keys(db)
        subs = db.query(PushSubscription).filter(PushSubscription.user_id == user.id).all()
        sent = 0
        for sub in subs:
            ok = _send_to_subscription(sub, title, body, url, private_pem, db)
            if ok:
                sent += 1
        if own_db:
            db.commit()
        return sent
    except Exception as e:
        logger.error(f"send_to_user error for user {user.id}: {e}")
        return 0
    finally:
        if own_db:
            db.close()


def send_to_all(title: str, body: str, url: str = "/",
                notif_filter: str = None) -> int:
    """
    Broadcast a push notification.
    notif_filter: if set, only send to users where that column is True
                  (e.g. 'notif_picks_reminder', 'notif_week_results')
    Returns total sent count.
    """
    db = SessionLocal()
    try:
        private_pem, _ = _get_or_create_vapid_keys(db)

        query = db.query(User).filter(User.is_active == True)
        if notif_filter == "notif_picks_reminder":
            query = query.filter(User.notif_picks_reminder == True)
        elif notif_filter == "notif_week_results":
            query = query.filter(User.notif_week_results == True)

        users = query.all()
        total = 0
        for user in users:
            subs = db.query(PushSubscription).filter(PushSubscription.user_id == user.id).all()
            for sub in subs:
                ok = _send_to_subscription(sub, title, body, url, private_pem, db)
                if ok:
                    total += 1
        db.commit()
        return total
    except Exception as e:
        logger.error(f"send_to_all error: {e}")
        return 0
    finally:
        db.close()
