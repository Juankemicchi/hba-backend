"""Pluggable VIP access for HBA.

Temporary gate: a request is VIP if it presents a token listed in
VIP_ACCESS_CODES (comma-separated), sent as header X-VIP-Token or
Authorization: Bearer.

When Supabase env vars are set, a valid user JWT is also accepted and
profiles.is_vip is checked. Missing Supabase config does not break the
public site.

Env vars (set in Render when ready; do not commit secrets):
  VIP_ACCESS_CODES
  SUPABASE_URL
  SUPABASE_ANON_KEY
  SUPABASE_JWT_SECRET
  SUPABASE_SERVICE_ROLE_KEY
  ADMIN_TOKEN

Payment webhook hooks (later):
  MERCADOPAGO_WEBHOOK_SECRET or MP_WEBHOOK_SECRET
  WOMPI_EVENTS_SECRET or WOMPI_WEBHOOK_SECRET
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Optional

import requests
from fastapi import Header, HTTPException

try:
    import jwt as pyjwt
except Exception:  # pragma: no cover
    pyjwt = None


@dataclass
class VipUser:
    source: str
    subject: Optional[str] = None
    email: Optional[str] = None
    is_vip: bool = True


def _access_codes() -> set[str]:
    raw = os.getenv("VIP_ACCESS_CODES") or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def supabase_frontend_configured() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_ANON_KEY"))


def supabase_jwt_configured() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_JWT_SECRET"))


def supabase_admin_ready() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))


def public_config() -> dict:
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    anon = os.getenv("SUPABASE_ANON_KEY") or ""
    ready = bool(url and anon)
    return {
        "supabase": ready,
        "supabase_url": url if ready else None,
        "supabase_anon_key": anon if ready else None,
        "access_codes_enabled": bool(_access_codes()),
        "google": ready,
    }


def extract_token(x_vip_token: Optional[str], authorization: Optional[str]) -> Optional[str]:
    if x_vip_token and x_vip_token.strip():
        return x_vip_token.strip()
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        return token or None
    return None


def _profile_is_vip(user_id: str, user_token: Optional[str] = None) -> Optional[bool]:
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    if not url or not user_id:
        return None
    service = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    anon = os.getenv("SUPABASE_ANON_KEY")
    token = service or user_token or anon
    key = service or anon
    if not token or not key:
        return None
    try:
        resp = requests.get(
            f"{url}/rest/v1/profiles",
            params={"id": f"eq.{user_id}", "select": "is_vip"},
            headers={
                "apikey": key,
                "Authorization": f"Bearer {token}",
            },
            timeout=10,
        )
        if resp.status_code >= 400:
            return None
        rows = resp.json() or []
        if not rows:
            return False
        return bool(rows[0].get("is_vip"))
    except Exception:
        return None


def verify_supabase_token(token: str) -> Optional[VipUser]:
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    if not url or not token:
        return None
    payload = None
    anon = os.getenv("SUPABASE_ANON_KEY")
    # Prefer Auth API so Render does not need SUPABASE_JWT_SECRET.
    if anon:
        try:
            resp = requests.get(
                f"{url}/auth/v1/user",
                headers={"apikey": anon, "Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if resp.status_code < 400:
                payload = resp.json() or {}
        except Exception:
            payload = None
    if payload is None:
        secret = os.getenv("SUPABASE_JWT_SECRET")
        if secret and pyjwt is not None:
            try:
                payload = pyjwt.decode(
                    token,
                    secret,
                    algorithms=["HS256"],
                    audience="authenticated",
                    options={"require": ["sub"]},
                )
            except Exception:
                payload = None
    if not payload:
        return None
    uid = payload.get("sub") or payload.get("id")
    email = payload.get("email")
    if not uid:
        return None
    flag = _profile_is_vip(str(uid), user_token=token)
    return VipUser(source="supabase", subject=str(uid), email=email, is_vip=bool(flag))


def require_vip(
    x_vip_token: Optional[str] = Header(None, alias="X-VIP-Token"),
    authorization: Optional[str] = Header(None),
) -> VipUser:
    token = extract_token(x_vip_token, authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Se requiere acceso VIP")
    if token in _access_codes():
        return VipUser(source="code", subject="code", is_vip=True)
    if supabase_frontend_configured() or supabase_jwt_configured():
        user = verify_supabase_token(token)
        if user and user.is_vip:
            return user
        if user and not user.is_vip:
            raise HTTPException(status_code=403, detail="Tu cuenta aún no tiene acceso VIP")
    raise HTTPException(status_code=401, detail="Token VIP inválido")


def require_admin(x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token")) -> bool:
    expected = os.getenv("ADMIN_TOKEN") or ""
    provided = (x_admin_token or "").strip()
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Admin no autorizado")
    return True


def set_user_vip(email: Optional[str] = None, user_id: Optional[str] = None, is_vip: bool = True) -> dict:
    if not supabase_admin_ready():
        raise HTTPException(status_code=503, detail="Supabase no está configurado")
    url = os.getenv("SUPABASE_URL").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    target_id = (user_id or "").strip() or None
    if not target_id and email:
        try:
            resp = requests.get(
                f"{url}/rest/v1/profiles",
                params={"email": f"eq.{email.strip()}", "select": "id,email,is_vip"},
                headers=headers,
                timeout=10,
            )
            rows = resp.json() if resp.status_code < 400 else []
            if rows:
                target_id = rows[0].get("id")
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"No se pudo buscar el perfil: {exc}")
    if not target_id:
        raise HTTPException(status_code=404, detail="Usuario no encontrado en profiles")
    try:
        resp = requests.patch(
            f"{url}/rest/v1/profiles",
            params={"id": f"eq.{target_id}"},
            json={"is_vip": bool(is_vip)},
            headers=headers,
            timeout=10,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail="No se pudo actualizar is_vip")
    rows = resp.json() if resp.content else [{"id": target_id, "is_vip": is_vip}]
    return {"ok": True, "profile": rows[0] if rows else {"id": target_id, "is_vip": is_vip}}


def payment_secret(provider: str) -> Optional[str]:
    if provider == "mercadopago":
        return os.getenv("MERCADOPAGO_WEBHOOK_SECRET") or os.getenv("MP_WEBHOOK_SECRET")
    if provider == "wompi":
        return os.getenv("WOMPI_EVENTS_SECRET") or os.getenv("WOMPI_WEBHOOK_SECRET")
    return None


def apply_vip_from_payment(email: Optional[str] = None, user_id: Optional[str] = None) -> dict:
    """Hook for future Mercado Pago / Wompi webhooks. Requires Supabase admin."""
    if not supabase_admin_ready():
        return {"ok": False, "detail": "Supabase no está configurado para activar VIP"}
    return set_user_vip(email=email, user_id=user_id, is_vip=True)
