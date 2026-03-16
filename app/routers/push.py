"""
Web Push subscription endpoints.
  GET  /push/vapid-public-key   — returns the VAPID public key for the browser
  POST /push/subscribe          — registers a browser push subscription
  POST /push/unsubscribe        — removes a browser push subscription
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, JSONResponse

from sqlalchemy.orm import Session
from app.database import get_db
from app.models import PushSubscription
from app.auth import get_current_user
from app.services.notifications import get_vapid_public_key

router = APIRouter(prefix="/push")


@router.get("/vapid-public-key", response_class=PlainTextResponse)
async def vapid_public_key():
    return get_vapid_public_key()


@router.post("/subscribe")
async def subscribe(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    endpoint = body.get("endpoint")
    keys = body.get("keys", {})
    p256dh = keys.get("p256dh")
    auth_key = keys.get("auth")

    if not endpoint or not p256dh or not auth_key:
        return JSONResponse({"error": "Invalid subscription data"}, status_code=400)

    existing = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()
    if existing:
        # Re-associate with current user if needed (e.g. shared device)
        existing.user_id = user.id
        existing.p256dh = p256dh
        existing.auth_key = auth_key
    else:
        db.add(PushSubscription(
            user_id=user.id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth_key=auth_key,
        ))
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/unsubscribe")
async def unsubscribe(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    endpoint = body.get("endpoint")
    if endpoint:
        db.query(PushSubscription).filter(
            PushSubscription.endpoint == endpoint,
            PushSubscription.user_id == user.id,
        ).delete()
        db.commit()
    return JSONResponse({"ok": True})
