"""OAuth endpoints for connecting an AI app to the MCP server.

Discovery (RFC 9728 and RFC 8414 metadata), dynamic client registration
(RFC 7591) at ``/oauth/register``, the approval page at ``/oauth/authorize``,
and the token and revocation endpoints. The flow and
storage live in app/services/oauth.py; this is only the HTTP around it.

Every URL in the metadata is built from the request, so it names the address
Claude actually reached — the public one behind the reverse proxy — with no
setting to keep in step with it.
"""
import base64
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit, parse_qsl

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import Role
from app.services import oauth
from app.templates_config import templates
from app.utils import public_url

router = APIRouter()


def protected_resource_metadata(request: Request) -> dict:
    return {
        "resource": public_url(request, "/mcp"),
        "authorization_servers": [public_url(request, "")],
        "bearer_methods_supported": ["header"],
        "resource_name": "National Armchair League",
    }


def authorization_server_metadata(request: Request) -> dict:
    return {
        "issuer": public_url(request, ""),
        "authorization_endpoint": public_url(request, "/oauth/authorize"),
        "token_endpoint": public_url(request, "/oauth/token"),
        "revocation_endpoint": public_url(request, "/oauth/revoke"),
        "registration_endpoint": public_url(request, "/oauth/register"),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": list(oauth.AUTH_METHODS),
        "revocation_endpoint_auth_methods_supported": list(oauth.AUTH_METHODS),
        "scopes_supported": ["offline_access"],
    }


# RFC 9728 puts the resource's path after the well-known prefix; some clients
# ask for the bare one. Answer both.
@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
async def protected_resource(request: Request):
    return JSONResponse(protected_resource_metadata(request))


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
@router.get("/.well-known/openid-configuration", include_in_schema=False)
async def authorization_server(request: Request):
    return JSONResponse(authorization_server_metadata(request))


# ---------------------------------------------------------------------------
# Registration: any MCP client may register itself
# ---------------------------------------------------------------------------

@router.post("/oauth/register")
async def register(request: Request, db: Session = Depends(get_db)):
    try:
        metadata = await request.json()
    except Exception:
        metadata = None
    try:
        body = oauth.register_client(db, metadata)
    except oauth.RegistrationError as exc:
        return _oauth_error(exc.error, exc.description)
    return JSONResponse(body, status_code=201,
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


# ---------------------------------------------------------------------------
# Authorize: log in, then approve
# ---------------------------------------------------------------------------

def _with_params(uri: str, **params) -> str:
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query += [(k, v) for k, v in params.items() if v is not None]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _refuse(request: Request, db: Session, message: str, status: int = 400):
    """An error that can't safely go back to the client — shown here instead."""
    return templates.TemplateResponse(
        "oauth/authorize.html",
        {"request": request, "user": get_current_user(request, db), "error": message},
        status_code=status,
    )


def _check_request(db: Session, params) -> tuple[oauth.OAuthClient | None, str | None]:
    """Validate an authorization request. Returns (client, error)."""
    client = oauth.get_client(db, params.get("client_id"))
    if not client:
        return None, (
            "Unknown client. Check the client ID in your AI app's connector "
            "settings, or remove the connector and add it again."
        )
    if not oauth.redirect_uri_allowed(client, params.get("redirect_uri")):
        fix = "" if client.self_registered else " Add it on the Account Settings page."
        return None, (
            "This redirect URI is not registered for the client: "
            f"{params.get('redirect_uri') or '(none)'}.{fix}"
        )
    return client, None


def _client_error(params, error: str, description: str):
    return RedirectResponse(
        _with_params(params["redirect_uri"], error=error,
                     error_description=description, state=params.get("state")),
        status_code=303,
    )


@router.get("/oauth/authorize", response_class=HTMLResponse)
async def authorize_page(request: Request, db: Session = Depends(get_db)):
    params = request.query_params
    client, problem = _check_request(db, params)
    if problem:
        return _refuse(request, db, problem)
    if params.get("response_type") != "code":
        return _client_error(params, "unsupported_response_type", "Only response_type=code is supported.")
    if not params.get("code_challenge") or params.get("code_challenge_method") != "S256":
        return _client_error(params, "invalid_request", "PKCE with S256 is required.")

    user = get_current_user(request, db)
    if not user:
        here = request.url.path + "?" + request.url.query
        return RedirectResponse(url="/login?" + urlencode({"next": here}), status_code=303)
    if not oauth.may_use_mcp(user):
        return _refuse(request, db, "Your account can't connect an AI app to the league.", 403)

    return templates.TemplateResponse(
        "oauth/authorize.html",
        {
            "request": request, "user": user, "client": client, "params": dict(params),
            # Where the approval goes. For a self-registered client the name is
            # whatever it chose to call itself, so the host is the real tell.
            "redirect_host": urlsplit(params["redirect_uri"]).hostname,
            "sees_money": user.role == Role.admin,
        },
    )


@router.post("/oauth/authorize")
async def authorize_decision(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    client, problem = _check_request(db, form)
    if problem:
        return _refuse(request, db, problem)
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not oauth.may_use_mcp(user):
        return _refuse(request, db, "Your account can't connect an AI app to the league.", 403)
    if not form.get("code_challenge"):
        return _client_error(form, "invalid_request", "PKCE with S256 is required.")

    if form.get("decision") != "allow":
        return _client_error(form, "access_denied", "The request was declined.")

    code = oauth.issue_code(
        db, client, user, form["redirect_uri"], form["code_challenge"],
    )
    return RedirectResponse(
        _with_params(form["redirect_uri"], code=code, state=form.get("state")),
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Token and revocation
# ---------------------------------------------------------------------------

def _oauth_error(error: str, description: str, status: int = 400):
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def _client_credentials(request: Request, form) -> tuple[str | None, str | None]:
    """client_secret_basic (Authorization header) or client_secret_post."""
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "basic" and value:
        try:
            decoded = base64.b64decode(value.strip()).decode()
            client_id, _, secret = decoded.partition(":")
            return unquote(client_id), unquote(secret)
        except Exception:
            return None, None
    return form.get("client_id"), form.get("client_secret")


@router.post("/oauth/token")
async def token(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    client = oauth.authenticate_client(db, *_client_credentials(request, form))
    if not client:
        return _oauth_error("invalid_client", "Client authentication failed.", 401)

    try:
        grant = form.get("grant_type")
        if grant == "authorization_code":
            body = oauth.exchange_code(
                db, client, form.get("code"), form.get("redirect_uri"),
                form.get("code_verifier"),
            )
        elif grant == "refresh_token":
            body = oauth.exchange_refresh(db, client, form.get("refresh_token"))
        else:
            return _oauth_error("unsupported_grant_type", f"Unsupported grant_type: {grant}")
    except oauth.GrantError as exc:
        return _oauth_error(exc.error, exc.description)
    return JSONResponse(body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.post("/oauth/revoke")
async def revoke(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    client = oauth.authenticate_client(db, *_client_credentials(request, form))
    if not client:
        return _oauth_error("invalid_client", "Client authentication failed.", 401)
    oauth.revoke(db, client, form.get("token"))
    return JSONResponse({})
