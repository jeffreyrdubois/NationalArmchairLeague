"""
Web Push notification service.

VAPID keys are auto-generated on first startup and stored in the app_settings
table so they persist across container restarts without any manual configuration.

pywebpush expects `vapid_private_key` to be the raw 32-byte P-256 private key
scalar encoded as base64url (no padding) — NOT a PEM string.
The matching public key for the browser applicationServerKey is the 65-byte
uncompressed EC point also encoded as base64url.
"""
import json
import logging
import base64
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import AppSetting, PushSubscription, User

logger = logging.getLogger(__name__)


def _generate_vapid_keys(db: Session) -> tuple[str, str]:
    """Generate a fresh VAPID key pair, store it, return (private_b64, public_b64)."""
    from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, SECP256R1
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private_key = generate_private_key(SECP256R1())

    # Raw 32-byte scalar — the format pywebpush/py_vapid expects
    d = private_key.private_numbers().private_value
    private_b64 = base64.urlsafe_b64encode(d.to_bytes(32, "big")).rstrip(b"=").decode()

    # 65-byte uncompressed point — what the browser needs for applicationServerKey
    pub_bytes = private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    public_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

    db.merge(AppSetting(key="vapid_private_key", value=private_b64))
    db.merge(AppSetting(key="vapid_public_key", value=public_b64))
    db.commit()
    logger.info("Generated new VAPID key pair and stored in app_settings")
    return private_b64, public_b64


def _get_or_create_vapid_keys(db: Session) -> tuple[str, str]:
    """Return (private_b64, public_b64), generating them if missing or invalid."""
    priv_row = db.query(AppSetting).filter(AppSetting.key == "vapid_private_key").first()
    pub_row = db.query(AppSetting).filter(AppSetting.key == "vapid_public_key").first()

    if priv_row and pub_row:
        # Validate: raw private key must decode to exactly 32 bytes
        try:
            padding = "=" * ((4 - len(priv_row.value) % 4) % 4)
            raw = base64.urlsafe_b64decode(priv_row.value + padding)
            if len(raw) == 32:
                return priv_row.value, pub_row.value
            logger.warning("Stored VAPID private key is wrong length (%d bytes) — regenerating", len(raw))
        except Exception:
            logger.warning("Stored VAPID private key is not valid base64 — regenerating")
        # Bad key: wipe and regenerate
        db.delete(priv_row)
        db.delete(pub_row)
        db.commit()

    return _generate_vapid_keys(db)


def get_vapid_public_key() -> str:
    db = SessionLocal()
    try:
        _, pub = _get_or_create_vapid_keys(db)
        return pub
    finally:
        db.close()


def init_vapid_keys():
    """Called at startup to ensure valid VAPID keys exist."""
    db = SessionLocal()
    try:
        _get_or_create_vapid_keys(db)
    finally:
        db.close()


def _send_to_subscription(sub: PushSubscription, title: str, body: str, url: str = "/",
                           private_b64: str = None, db: Session = None) -> bool:
    """Send a push to one subscription. Returns False if the subscription is gone."""
    from pywebpush import webpush, WebPushException

    try:
        webpush(
            subscription_info={
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth_key},
            },
            data=json.dumps({"title": title, "body": body, "url": url}),
            vapid_private_key=private_b64,
            vapid_claims={"sub": "mailto:noreply@nal.local"},
        )
        return True
    except WebPushException as e:
        status = e.response.status_code if e.response is not None else None
        if status in (404, 410, 403):
            # 404/410 = endpoint gone; 403 = VAPID key mismatch (stale subscription)
            if db:
                db.delete(sub)
            return False
        logger.warning("Push failed for sub %d (status %s): %s", sub.id, status, e)
        return True  # transient error — keep subscription


def send_to_user(user: User, title: str, body: str, url: str = "/", db: Session = None) -> int:
    """Send a push to all active subscriptions for a user. Returns sent count."""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        private_b64, _ = _get_or_create_vapid_keys(db)
        subs = db.query(PushSubscription).filter(PushSubscription.user_id == user.id).all()
        sent = 0
        for sub in subs:
            if _send_to_subscription(sub, title, body, url, private_b64, db):
                sent += 1
        if own_db:
            db.commit()
        return sent
    except Exception as e:
        logger.error("send_to_user error for user %d: %s", user.id, e)
        return 0
    finally:
        if own_db:
            db.close()


def send_to_all(title: str, body: str, url: str = "/", notif_filter: str = None) -> int:
    """
    Broadcast a push notification.
    notif_filter: 'notif_picks_reminder' or 'notif_week_results' to respect user prefs.
    Returns total subscriptions reached.
    """
    db = SessionLocal()
    try:
        private_b64, _ = _get_or_create_vapid_keys(db)

        query = db.query(User).filter(User.is_active == True)
        if notif_filter == "notif_picks_reminder":
            query = query.filter(User.notif_picks_reminder == True)
        elif notif_filter == "notif_week_results":
            query = query.filter(User.notif_week_results == True)

        total = 0
        for user in query.all():
            subs = db.query(PushSubscription).filter(PushSubscription.user_id == user.id).all()
            for sub in subs:
                if _send_to_subscription(sub, title, body, url, private_b64, db):
                    total += 1
        db.commit()
        return total
    except Exception as e:
        logger.error("send_to_all error: %s", e)
        return 0
    finally:
        db.close()
