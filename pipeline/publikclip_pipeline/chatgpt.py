"""Official Sign in with ChatGPT for an open-source desktop client.

No API keys, Codex credential reuse, or private ChatGPT endpoints. Tokens stay
in the sidecar; every UI/CLI response is an explicit, non-secret projection.
See https://developers.openai.com/siwc/token-sharing-open-source/sign-in.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from filelock import FileLock, Timeout

from . import config

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
USAGE_URL = "https://chatgpt.com/#settings/Usage"


class ChatGPTError(Exception):
    """Safe to show to the user: never include response bodies or credentials."""


def _root() -> Path:
    root = config.home_dir() / "chatgpt"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        root.chmod(0o700)
    return root


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    """Protect Windows credentials for the current Windows user via DPAPI."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    fn = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN: a background sidecar must not prompt.
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ChatGPTError("Windows could not protect or read the ChatGPT credentials.")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)


def _path() -> Path:
    return _root() / ("accounts.dpapi" if os.name == "nt" else "accounts.json")


def _read() -> dict:
    path = _path()
    if not path.exists():
        return {"accounts": {}, "active": None}
    try:
        raw = path.read_bytes()
        data = json.loads(_dpapi(raw, True) if os.name == "nt" else raw)
        if not isinstance(data.get("accounts"), dict):
            raise ValueError
        return data
    except (OSError, ValueError, TypeError, AttributeError) as err:
        raise ChatGPTError("ChatGPT account storage could not be read. Restore it or sign in again.") from err


def _write(data: dict) -> None:
    path = _path()
    raw = json.dumps(data).encode()
    if os.name == "nt":
        raw = _dpapi(raw)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        if os.name != "nt":
            os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _lock() -> FileLock:
    # OS-backed locks release even when the sidecar crashes. Serialize token
    # rotation and every store mutation across desktop/CLI processes.
    return FileLock(str(_root() / "accounts.lock"), timeout=45)


def _host_id() -> str:
    with _lock():
        path = _root() / "host-id"
        if path.exists():
            return path.read_text().strip()
        value = "urn:uuid:" + str(uuid.uuid4())
        path.write_text(value)
        return value


def _active(store: dict) -> dict:
    account = store["accounts"].get(store.get("active"))
    if not account or not account.get("access_token"):
        raise ChatGPTError("Continue with ChatGPT in Brain & keys before starting this run.")
    if PLAN_SCOPE not in account.get("scopes", []):
        raise ChatGPTError("ChatGPT plan usage was not enabled. Sign in again and grant plan access.")
    return account


def status() -> dict:
    with _lock():
        store = _read()
    active = store["accounts"].get(store.get("active"), {})
    return {
        "ok": True,
        "connected": bool(active.get("access_token") and PLAN_SCOPE in active.get("scopes", [])),
        "active": store.get("active"),
        "model": active.get("model"),
        "accounts": [
            {"id": key, "label": f"{a.get('email') or 'ChatGPT account'} · {key[-8:]}",
             "connected": bool(a.get("access_token"))}
            for key, a in store["accounts"].items()
        ],
    }


def _token_post(form: dict) -> dict:
    try:
        response = httpx.post(TOKEN, data=form, timeout=30)
        if response.status_code != 200:
            raise ChatGPTError("ChatGPT authorization expired or was declined. Sign in again.")
        payload = response.json()
        if (not isinstance(payload, dict) or not payload.get("access_token")
                or not payload.get("refresh_token") or payload.get("token_type", "").lower() != "bearer"):
            raise ChatGPTError("ChatGPT returned an incomplete credential set. Sign in again.")
        return payload
    except (httpx.HTTPError, ValueError) as err:
        raise ChatGPTError("Could not complete ChatGPT authorization. Check your connection and retry.") from err


def _validated_identity(token: str, client_id: str, nonce: str | None = None) -> dict:
    try:
        key = jwt.PyJWKClient(ISSUER + "/.well-known/jwks.json", timeout=15).get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=client_id,
                            issuer=ISSUER, leeway=5, options={"require": ["sub", "exp", "iat"]})
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ValueError
        if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ValueError
        return claims
    except (jwt.PyJWTError, ValueError, KeyError) as err:
        raise ChatGPTError("ChatGPT identity validation failed. Start a new sign-in.") from err


def _updated(account: dict, tokens: dict) -> dict:
    result = {**account, **{k: tokens[k] for k in ("access_token", "refresh_token", "id_token") if k in tokens}}
    result["scopes"] = tokens.get("scope", " ".join(account.get("scopes", []))).split()
    result["expires_at"] = time.time() + float(tokens.get("expires_in", 3600))
    if PLAN_SCOPE not in result["scopes"]:
        raise ChatGPTError("ChatGPT plan usage was not enabled. Grant plan access during sign-in.")
    return result


def sign_in(account_id: str | None = None, timeout: float = 300) -> dict:
    host = _host_id()
    with _lock():
        old = _read()["accounts"].get(account_id) if account_id else None
        if account_id and not old:
            raise ChatGPTError("That saved ChatGPT account was not found.")
    verifier, state, nonce = (secrets.token_urlsafe(48) for _ in range(3))
    callback: dict[str, list[str]] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # authorization codes/state must never reach stderr

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            valid = (parsed.path == "/auth/callback" and len(query.get("state", [])) == 1
                     and hmac.compare_digest(query["state"][0], state))
            if not valid:
                self.send_response(400)
                self.end_headers()
                return
            callback.update(query)
            body = b"Return to PublikClip to finish signing in. You can close this window."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
        params = {"client_id": old["client_id"] if old else "dynamic_agent_client",
                  "ext_agent_host_id": host, "response_type": "code", "redirect_uri": redirect,
                  "scope": SCOPES, "resource": RESOURCE, "state": state, "nonce": nonce,
                  "code_challenge_method": "S256",
                  "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
        if old:
            if old.get("id_token"):
                params["id_token_hint"] = old["id_token"]
            if old.get("email"):
                params["login_hint"] = old["email"]
        else:
            params["agent_name_hint"] = "PublikClip ChatGPT"
        if not webbrowser.open(AUTHORIZE + "?" + urlencode(params)):
            raise ChatGPTError("Could not open the system browser. Set a default browser and try again.")
        deadline = time.monotonic() + timeout
        while not callback and time.monotonic() < deadline:
            server.handle_request()
    if not callback:
        raise ChatGPTError("ChatGPT sign-in timed out. Click Continue with ChatGPT to try again.")
    if callback.get("error"):
        raise ChatGPTError("ChatGPT sign-in was declined. No account was changed.")
    if len(callback.get("code", [])) != 1 or len(callback.get("client_id", [])) > 1:
        raise ChatGPTError("ChatGPT registration was incomplete. Try again.")
    client_id = callback.get("client_id", [old["client_id"] if old else ""])[0]
    if not client_id or client_id == "dynamic_agent_client" or (old and client_id != old["client_id"]):
        raise ChatGPTError("ChatGPT returned an unexpected client registration.")
    tokens = _token_post({"grant_type": "authorization_code", "client_id": client_id,
                         "code": callback["code"][0], "code_verifier": verifier,
                         "redirect_uri": redirect, "resource": RESOURCE})
    identity = _validated_identity(tokens.get("id_token", ""), client_id, nonce)
    if old and identity["sub"] != old["subject"]:
        raise ChatGPTError("The signed-in identity did not match the selected ChatGPT account.")
    account = _updated({**(old or {}), "client_id": client_id, "subject": identity["sub"],
                        "email": identity.get("email"), "ext_agent_host_id": host}, tokens)
    with _lock():
        store = _read()
        store["accounts"][client_id] = account
        store["active"] = client_id
        _write(store)
    return status()


def credential(account_id: str | None = None) -> dict:
    with _lock():
        store = _read()
        if account_id:
            store["active"] = account_id  # select only in this read, never switch UI
        account = _active(store)
        if float(account.get("expires_at", 0)) <= time.time() + 60:
            tokens = _token_post({"grant_type": "refresh_token", "client_id": account["client_id"],
                                 "refresh_token": account["refresh_token"], "resource": RESOURCE})
            if tokens.get("id_token"):
                claims = _validated_identity(tokens["id_token"], account["client_id"])
                if claims["sub"] != account["subject"]:
                    raise ChatGPTError("ChatGPT refresh returned a different account.")
            account = _updated(account, tokens)
            # Preserve actual active selection when this is a pinned scoring run.
            saved = _read()
            saved["accounts"][account["client_id"]] = account
            _write(saved)
        return account


def models(account_id: str | None = None) -> list[dict]:
    account = credential(account_id)
    try:
        res = httpx.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + account["access_token"]}, timeout=30)
        if res.status_code != 200:
            raise ChatGPTError("ChatGPT model access is unavailable. Reauthorize or check plan access in ChatGPT settings.")
        return [{"slug": m["slug"], "display_name": m.get("display_name", m["slug"])}
                for m in res.json().get("models", []) if m.get("visibility") == "list" and m.get("slug")]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as err:
        raise ChatGPTError("Could not load the available ChatGPT models. Check your connection.") from err


def select_model(slug: str) -> dict:
    account_id = credential()["client_id"]
    if slug not in {m["slug"] for m in models(account_id)}:
        raise ChatGPTError("That model is unavailable to the selected ChatGPT account.")
    with _lock():
        store = _read()
        store["accounts"][account_id]["model"] = slug
        _write(store)
    return status()


def select_account(account_id: str) -> dict:
    credential(account_id)  # validate/refresh before activation
    with _lock():
        store = _read()
        store["active"] = account_id
        _write(store)
    return status()


def sign_out() -> dict:
    confirmed = True
    with _lock():
        store = _read()
        account = store["accounts"].get(store.get("active"))
        if account:
            if account.get("refresh_token"):
                confirmed = False
                try:
                    discovery = httpx.get(ISSUER + "/.well-known/openid-configuration", timeout=15)
                    discovery.raise_for_status()
                    endpoint = discovery.json()["revocation_endpoint"]
                    if not endpoint.startswith(ISSUER + "/"):
                        raise ValueError
                    for attempt in range(3):
                        res = httpx.post(endpoint, data={"token": account["refresh_token"],
                            "token_type_hint": "refresh_token", "client_id": account["client_id"]}, timeout=15)
                        if res.status_code == 200:
                            confirmed = True
                            break
                        if res.status_code < 500:
                            break
                        time.sleep(0.5 * (attempt + 1))
                except (httpx.HTTPError, ValueError, KeyError):
                    pass
            for key in ("access_token", "refresh_token", "id_token", "expires_at", "scopes"):
                account.pop(key, None)
            _write(store)
    return {**status(), "revocation_confirmed": confirmed}


def command(action: str, value: str | None = None) -> dict:
    try:
        if action == "status":
            return status()
        if action == "login":
            return sign_in(value)
        if action == "logout":
            return sign_out()
        if action == "models":
            return {"ok": True, "models": models()}
        if action == "model" and value:
            return select_model(value)
        if action == "account" and value:
            return select_account(value)
        raise ChatGPTError("Unknown ChatGPT action.")
    except (ChatGPTError, Timeout, OSError) as err:
        message = str(err) if isinstance(err, ChatGPTError) else "ChatGPT account storage is busy or unavailable. Retry shortly."
        return {"ok": False, "error": message}
