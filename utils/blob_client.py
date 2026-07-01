"""Cliente HTTP mínimo para Vercel Blob (sem SDK pesado)."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any
from urllib.parse import quote

import httpx

BLOB_API_URL = "https://vercel.com/api/blob"
BLOB_API_VERSION = "12"
LEGACY_PATHNAME = "vo-tradutor/database.json"
USERS_PATHNAME = "vo-tradutor/users.json"
MESSAGES_PREFIX = "vo-tradutor/messages/"
DEFAULT_PATHNAME = USERS_PATHNAME


def _clean_env(value: str) -> str:
    """Remove BOM e espaços (comum quando env é criada via PowerShell/echo)."""
    return value.strip().lstrip("\ufeff").strip()


def _read_env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = _clean_env(str(raw))
    return value or None


def blob_enabled() -> bool:
    return bool(_read_env("BLOB_READ_WRITE_TOKEN"))


def _store_id() -> str:
    explicit = _read_env("BLOB_STORE_ID")
    if explicit:
        return explicit.removeprefix("store_")

    token = _read_env("BLOB_READ_WRITE_TOKEN")
    if not token:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN não configurado.")
    parts = token.split("_")
    if len(parts) < 4:
        raise RuntimeError("Token Blob inválido.")
    return parts[3]


def _auth_headers() -> dict[str, str]:
    token = _read_env("BLOB_READ_WRITE_TOKEN")
    if not token:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN não configurado.")
    return {
        "authorization": f"Bearer {token}",
        "x-api-version": BLOB_API_VERSION,
        "x-vercel-blob-store-id": _store_id(),
    }


def _blob_content_url(pathname: str, access: str = "private") -> str:
    return f"https://{_store_id()}.{access}.blob.vercel-storage.com/{quote(pathname, safe='/')}"


def put_json(pathname: str, payload: dict[str, Any], *, if_match: str | None = None, access: str = "public") -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        **_auth_headers(),
        "x-vercel-blob-access": access,
        "x-content-type": "application/json",
        "x-add-random-suffix": "0",
        "x-allow-overwrite": "1",
        "x-content-length": str(len(body)),
    }
    if if_match:
        headers["x-if-match"] = _clean_env(str(if_match)).strip('"')

    params = {"pathname": pathname}
    with httpx.Client(timeout=30.0) as client:
        resp = client.put(f"{BLOB_API_URL}/", params=params, content=body, headers=headers)
        if resp.status_code == 412:
            raise RuntimeError(f"Blob put falhou (412): {resp.text}")
        if resp.status_code >= 400:
            raise RuntimeError(f"Blob put falhou ({resp.status_code}): {resp.text}")
        result = resp.json()
        if result.get("etag"):
            result["etag"] = _clean_env(str(result["etag"])).strip('"')
        return result


def get_json(pathname: str = DEFAULT_PATHNAME, *, access: str = "public") -> tuple[dict[str, Any] | None, str | None]:
    """Retorna (dados, etag) ou (None, None) se não existir."""
    url = _blob_content_url(pathname, access=access)
    headers = _auth_headers()
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code == 404:
            return None, None
        if resp.status_code >= 400:
            raise RuntimeError(f"Blob get falhou ({resp.status_code}): {resp.text}")
        etag = resp.headers.get("etag")
        if etag:
            etag = _clean_env(etag).strip('"')
        return json.loads(resp.content.decode("utf-8")), etag


def head_blob(pathname: str = DEFAULT_PATHNAME) -> dict[str, Any] | None:
    params = {"url": pathname}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{BLOB_API_URL}/", params=params, headers=_auth_headers())
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(f"Blob head falhou ({resp.status_code}): {resp.text}")
        return resp.json()


def put_message_json(payload: dict[str, Any], *, access: str = "public") -> dict[str, Any]:
    """Grava mensagem em blob único (append-only, sem conflito de ETag)."""
    pathname = f"{MESSAGES_PREFIX}{uuid.uuid4().hex}.json"
    return put_json(pathname, payload, if_match=None, access=access)
