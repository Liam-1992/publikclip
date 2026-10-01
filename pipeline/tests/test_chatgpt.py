"""OAuth/security and Responses protocol tests. No account or paid inference."""
import json
import os
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from publikclip_pipeline import chatgpt, cli
from publikclip_pipeline.scoring import llm
from publikclip_pipeline.scoring.chatgpt_client import ChatGPTClient, ChatGPTStopError, read_stream

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    # A missed mock must fail instead of accidentally calling OpenAI.
    def blocked(*args, **kwargs):
        raise AssertionError("unexpected network call")
    monkeypatch.setattr(httpx, "post", blocked)
    monkeypatch.setattr(httpx, "get", blocked)
    monkeypatch.setattr(httpx, "stream", blocked)
    monkeypatch.setattr(chatgpt.webbrowser, "open", blocked)


def account(client="oaiapp_a", subject="user-a", **extra):
    return {"client_id": client, "subject": subject, "email": "same@example.test",
            "access_token": "secret-access", "refresh_token": "secret-refresh", "id_token": "secret-id",
            "expires_at": time.time() + 3600, "scopes": [chatgpt.PLAN_SCOPE], **extra}


def store(*accounts):
    chatgpt._write({"accounts": {a["client_id"]: a for a in accounts}, "active": accounts[0]["client_id"]})


def tokens(**extra):
    return {"access_token": "new-access", "refresh_token": "new-refresh", "id_token": "new-id",
            "scope": chatgpt.SCOPES, "expires_in": 3600, "token_type": "Bearer", **extra}


def response(payload, code=200):
    return httpx.Response(code, json=payload, request=httpx.Request("POST", "https://example.test"))


def sse(kind, **extra):
    return ["data: " + json.dumps({"type": kind, **extra}), ""]


def completed(text='{"ok":true}'):
    return sse("response.completed", response={"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": text}]}]})


def test_storage_atomic_permissions_and_redacted_status():
    store(account())
    text = json.dumps(chatgpt.status())
    assert "secret-" not in text
    assert "access_token" not in text
    assert chatgpt.status()["connected"]
    if os.name != "nt":
        assert chatgpt._path().stat().st_mode & 0o777 == 0o600
        assert chatgpt._root().stat().st_mode & 0o777 == 0o700
    else:
        assert b"secret-access" not in chatgpt._path().read_bytes()
    assert chatgpt._host_id() == chatgpt._host_id()


def test_account_switch_and_pinned_credential():
    store(account(), account("oaiapp_b", "user-b"))
    chatgpt.select_account("oaiapp_b")
    assert chatgpt.credential("oaiapp_a")["subject"] == "user-a"
    assert chatgpt.status()["active"] == "oaiapp_b"
    assert len(set(a["label"] for a in chatgpt.status()["accounts"])) == 2


def test_refresh_rotates_atomically_and_keeps_active_selection(monkeypatch):
    store(account(expires_at=0), account("oaiapp_b", "user-b"))
    chatgpt.select_account("oaiapp_b")
    calls = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: calls.append(kw["data"]) or response(tokens()))
    monkeypatch.setattr(chatgpt, "_validated_identity", lambda *args: {"sub": "user-a"})
    assert chatgpt.credential("oaiapp_a")["refresh_token"] == "new-refresh"
    assert chatgpt.status()["active"] == "oaiapp_b"
    assert calls[0]["client_id"] == "oaiapp_a"
    assert calls[0]["resource"] == chatgpt.RESOURCE
    assert "scope" not in calls[0]
    chatgpt.credential("oaiapp_a")
    assert len(calls) == 1


def test_concurrent_refresh_happens_once(monkeypatch):
    store(account(expires_at=0))
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *args, **kw: calls.append(1) or response(tokens()))
    monkeypatch.setattr(chatgpt, "_validated_identity", lambda *args: {"sub": "user-a"})
    results = []
    threads = [threading.Thread(target=lambda: results.append(chatgpt.credential())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4 and len(calls) == 1


def test_refresh_does_not_replace_other_identity(monkeypatch):
    store(account(expires_at=0))
    monkeypatch.setattr(httpx, "post", lambda *args, **kw: response(tokens()))
    monkeypatch.setattr(chatgpt, "_validated_identity", lambda *args: {"sub": "other-user"})
    with pytest.raises(chatgpt.ChatGPTError):
        chatgpt.credential()
    assert chatgpt._read()["accounts"]["oaiapp_a"]["refresh_token"] == "secret-refresh"


@pytest.mark.parametrize("field,value", [("iss", "https://evil.test"), ("aud", "other-app"),
    ("nonce", "wrong"), ("exp", 1), ("sub", "")])
def test_id_token_claims_are_verified(monkeypatch, field, value):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    class JWKS:
        def __init__(self, *args, **kw):
            pass
        def get_signing_key_from_jwt(self, token):
            class SigningKey:
                pass
            result = SigningKey()
            result.key = key.public_key()
            return result
    monkeypatch.setattr(jwt, "PyJWKClient", JWKS)
    claims = {"iss": chatgpt.ISSUER, "aud": "oaiapp_a", "nonce": "expected",
              "exp": time.time() + 300, "iat": time.time(), "sub": "user-a"}
    good = jwt.encode(claims, key, algorithm="RS256")
    assert chatgpt._validated_identity(good, "oaiapp_a", "expected")["sub"] == "user-a"
    claims[field] = value
    bad = jwt.encode(claims, key, algorithm="RS256")
    with pytest.raises(chatgpt.ChatGPTError):
        chatgpt._validated_identity(bad, "oaiapp_a", "expected")
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(claims, wrong_key, algorithm="RS256")
    with pytest.raises(chatgpt.ChatGPTError):
        chatgpt._validated_identity(forged, "oaiapp_a", "expected")


@pytest.mark.parametrize("mode", ["success", "denied", "missing-client", "no-plan", "wrong-identity"])
def test_loopback_sign_in(monkeypatch, mode):
    if mode == "wrong-identity":
        store(account())
    captured, exchange, callbacks, threads = {}, [], [], []
    def open_browser(url):
        captured.update({k: v[0] for k, v in parse_qs(urlsplit(url).query).items()})
        def callback():
            redirect = captured["redirect_uri"]
            # Mismatched state is rejected; it must not consume the attempt.
            try:
                urlopen(redirect + "?" + urlencode({"state": "wrong", "code": "bad"}), timeout=5)
            except Exception:
                pass
            query = {"state": captured["state"], "code": "one-time-code"}
            if mode == "denied":
                query = {"state": captured["state"], "error": "access_denied"}
            elif mode != "missing-client":
                query["client_id"] = "oaiapp_a"
            with urlopen(redirect + "?" + urlencode(query), timeout=5) as res:
                callbacks.append(res.status)
        thread = threading.Thread(target=callback)
        threads.append(thread)
        thread.start()
        return True
    monkeypatch.setattr(chatgpt.webbrowser, "open", open_browser)
    def post(url, **kw):
        exchange.append(kw["data"])
        return response(tokens(scope="openid" if mode == "no-plan" else chatgpt.SCOPES))
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(chatgpt, "_validated_identity", lambda *args: {"sub": "other-user" if mode == "wrong-identity" else "user-a"})
    if mode == "success":
        assert chatgpt.sign_in(timeout=5)["connected"]
        assert exchange[0]["client_id"] == "oaiapp_a"
        assert exchange[0]["redirect_uri"] == captured["redirect_uri"]
        assert captured["client_id"] == "dynamic_agent_client"
        assert captured["code_challenge_method"] == "S256"
        assert captured["ext_agent_host_id"].startswith("urn:uuid:")
    else:
        with pytest.raises(chatgpt.ChatGPTError):
            chatgpt.sign_in("oaiapp_a" if mode == "wrong-identity" else None, timeout=5)
        if mode in ("denied", "missing-client"):
            assert not exchange
        if mode == "wrong-identity":
            assert chatgpt.credential()["subject"] == "user-a"
    for thread in threads:
        thread.join(timeout=5)
    assert callbacks == [200]


def test_returning_sign_in_reuses_registration(monkeypatch):
    store(account())
    def browser(url):
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert params["client_id"] == "oaiapp_a"
        assert "agent_name_hint" not in params
        assert params["id_token_hint"] == "secret-id"
        threading.Thread(target=lambda: urlopen(params["redirect_uri"] + "?" + urlencode(
            {"state": params["state"], "code": "new-code"}), timeout=5).close()).start()
        return True
    monkeypatch.setattr(chatgpt.webbrowser, "open", browser)
    monkeypatch.setattr(httpx, "post", lambda *args, **kw: response(tokens()))
    monkeypatch.setattr(chatgpt, "_validated_identity", lambda *args: {"sub": "user-a"})
    assert chatgpt.sign_in("oaiapp_a", timeout=5)["connected"]
    assert len(chatgpt.status()["accounts"]) == 1


def test_signout_revokes_and_preserves_account_mapping(monkeypatch):
    store(account())
    monkeypatch.setattr(httpx, "get", lambda *args, **kw: response({"revocation_endpoint": chatgpt.ISSUER + "/revoke"}))
    calls = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: calls.append(kw["data"]) or response({}))
    result = chatgpt.sign_out()
    assert result["revocation_confirmed"] and not result["connected"]
    assert calls[0]["token"] == "secret-refresh"
    assert "secret-" not in json.dumps(chatgpt._read())
    assert result["accounts"][0]["id"] == "oaiapp_a"


def test_signout_reports_unconfirmed_revocation(monkeypatch):
    store(account())
    def offline(*args, **kw):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "get", offline)
    result = chatgpt.sign_out()
    assert not result["connected"] and not result["revocation_confirmed"]


def test_model_catalog_order_filter_and_selection(monkeypatch):
    store(account())
    monkeypatch.setattr(httpx, "get", lambda *args, **kw: response({"models": [
        {"slug": "model-b", "display_name": "B", "visibility": "list"},
        {"slug": "hidden", "visibility": "hide"}, {"slug": "model-a", "visibility": "list"}]}))
    assert [m["slug"] for m in chatgpt.models()] == ["model-b", "model-a"]
    assert chatgpt.select_model("model-a")["model"] == "model-a"
    with pytest.raises(chatgpt.ChatGPTError):
        chatgpt.select_model("hidden")


@pytest.mark.parametrize("ending", ["interrupt", "response.incomplete", "response.failed", "error"])
def test_partial_text_never_counts_as_completed(ending):
    lines = sse("response.output_text.delta", delta='{"ok":true}')
    if ending != "interrupt":
        lines += sse(ending, response={"error": {"code": "subscription_sharing_usage_limit_exceeded"}})
    with pytest.raises(ChatGPTStopError):
        read_stream(lines)


def test_full_completion_and_multiline_sse():
    assert read_stream(completed()) == '{"ok":true}'
    assert read_stream(['data: {"type": "response.output_text.delta",', 'data: "delta": "abc"}', ""]
                       + sse("response.completed", response={"status": "completed"})) == "abc"


def fake_client(monkeypatch, events):
    store(account())
    monkeypatch.setattr(chatgpt, "models", lambda *args: [{"slug": "account-model"}])
    calls = []
    class Stream:
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def iter_lines(self):
            return iter(events)
    monkeypatch.setattr(httpx, "stream", lambda method, url, **kw: calls.append((method, url, kw)) or Stream())
    return ChatGPTClient(), calls


def test_client_contract_vision_schema_validation_and_cache(monkeypatch):
    client, calls = fake_client(monkeypatch, completed())
    assert client.generate_json("prompt", SCHEMA, [b"jpeg"]) == {"ok": True}
    assert client.generate_json("prompt", SCHEMA, [b"jpeg"]) == {"ok": True}
    assert len(calls) == 1
    method, url, kw = calls[0]
    assert url == chatgpt.RESOURCE + "/responses"
    body = kw["json"]
    assert body["stream"] is True and body["store"] is False
    assert body["model"] == "account-model"
    assert body["input"][0]["content"][1]["type"] == "input_image"
    assert not ({"temperature", "max_output_tokens", "previous_response_id"} & body.keys())
    assert kw["headers"]["Authorization"] == "Bearer secret-access"
    assert isinstance(llm.make_client("chatgpt"), ChatGPTClient)


def test_invalid_schema_response_does_not_enter_cache(monkeypatch):
    client, calls = fake_client(monkeypatch, completed('{"wrong":true}'))
    with pytest.raises(llm.LlmError):
        client.generate_json("prompt", SCHEMA)
    assert not list(llm._cache_dir().glob("*.json"))


def test_limit_after_delta_never_cached_or_retried(monkeypatch):
    events = sse("response.output_text.delta", delta='{"ok":true}') + sse("response.failed",
        response={"error": {"code": "subscription_sharing_usage_limit_exceeded"}})
    client, calls = fake_client(monkeypatch, events)
    with pytest.raises(ChatGPTStopError, match="limit"):
        client.generate_json("prompt", SCHEMA)
    assert len(calls) == 1 and not list(llm._cache_dir().glob("*.json"))


def test_chatgpt_never_falls_back_to_paid_image_generation(monkeypatch, tmp_path):
    from publikclip_pipeline.edits import visuals
    assert visuals.fetch_gemini("cat", tmp_path, "chatgpt") is None


def test_cli_accepts_chatgpt_provider(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_run", lambda args: seen.append(args.llm) or 0)
    assert cli.main(["run", "video.mp4", "--llm", "chatgpt"]) == 0
    assert seen == ["chatgpt"]


def test_dependency_marker_from_another_runtime_is_not_trusted(monkeypatch):
    import importlib.util
    chatgpt._root()
    marker = chatgpt.config.home_dir() / ".chatgpt_deps_synced"
    marker.write_text("ok")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    calls = []
    monkeypatch.setattr(cli, "_ensure_group_deps", lambda *args: calls.append(args) or (True, None))
    assert cli._ensure_chatgpt_deps(lambda *args: None) == (True, None)
    assert not marker.exists() and calls[0][0] == "chatgpt"
