"""Authorization at the real HTTP boundary, including persistent ownership."""

from fastapi.testclient import TestClient

from ui.auth_store import AuthStore, COOKIE_NAME
from ui.web_server import create_app

ORIGIN = "http://testserver"
TASK = "从平顶山港到军李船闸，2026-09-18 09:00出发，SOC85%，半载，6小时内到达"


def _post(client, path, *, csrf=None, **kwargs):
    headers = {"Origin": ORIGIN}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return client.post(path, headers=headers, **kwargs)


def _login(client, username, password):
    response = _post(client, "/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    return response.json()["csrf_token"]


def test_login_session_logout_and_roles(tmp_path):
    store = AuthStore(backend="sqlite", sqlite_path=tmp_path / "auth.sqlite3")
    store.ensure_schema()
    store.create_user("crew", "船员", "user", "test-password-crew")
    store.create_user("chief", "管理员", "admin", "test-password-chief")
    app = create_app(checkpoint_backend="memory", auth_store=store)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok", "service": "ship-agent-web"}
        assert client.get("/api/auth/me").json()["error"] == "auth_required"
        assert _post(client, "/api/auth/login", json={"username": "crew", "password": "wrong"}).json()["error"] == "invalid_credentials"
        csrf = _login(client, "crew", "test-password-crew")
        cookie = client.cookies.get(COOKIE_NAME)
        assert cookie
        assert client.get("/api/auth/me").json()["user"]["role"] == "user"
        assert client.get("/api/admin/system-status").status_code == 403
        assert _post(client, "/api/run", json={"task_text": TASK}).status_code == 403
        assert _post(client, "/api/auth/logout", csrf=csrf).status_code == 204
        client.cookies.set(COOKIE_NAME, cookie)
        assert client.get("/api/auth/me").status_code == 401
        client.cookies.clear()
        admin_csrf = _login(client, "chief", "test-password-chief")
        assert admin_csrf
        assert "release" in client.get("/api/admin/system-status").json()


def test_origin_rate_limit_and_cross_user_workflow(tmp_path):
    store = AuthStore(backend="sqlite", sqlite_path=tmp_path / "auth.sqlite3")
    store.ensure_schema()
    store.create_user("alice", "甲", "user", "test-password-alice")
    store.create_user("bob", "乙", "user", "test-password-bob")
    app = create_app(checkpoint_backend="memory", auth_store=store)
    with TestClient(app) as alice, TestClient(app) as bob:
        assert alice.post("/api/auth/login", json={"username": "alice", "password": "x"}).status_code == 403
        csrf_a = _login(alice, "alice", "test-password-alice")
        csrf_b = _login(bob, "bob", "test-password-bob")
        started = _post(alice, "/api/run", csrf=csrf_a, json={"task_text": TASK})
        assert started.status_code == 200
        result = started.json()
        thread_id = result["thread_id"]
        assert "llm_call_status" not in str(result)
        assert alice.get(f"/api/runs/{thread_id}").status_code == 200
        assert bob.get(f"/api/runs/{thread_id}").json()["error"] == "workflow_not_found"
        assert _post(bob, "/api/resume", csrf=csrf_b, json={
            "thread_id": thread_id, "decision_id": "anything", "option_id": "anything",
        }).json()["error"] == "workflow_not_found"
        for _ in range(5):
            _post(bob, "/api/auth/login", json={"username": "unknown", "password": "wrong"})
        assert _post(bob, "/api/auth/login", json={
            "username": "unknown", "password": "wrong",
        }).status_code == 429


def test_session_and_ownership_survive_store_recreation(tmp_path):
    path = tmp_path / "auth.sqlite3"
    first = AuthStore(backend="sqlite", sqlite_path=path)
    first.ensure_schema()
    user_id = first.create_user("crew", "船员", "user", "test-password-crew")
    token, csrf, _ = first.create_session(user_id)
    first.bind_workflow("a" * 36, user_id)
    second = AuthStore(backend="sqlite", sqlite_path=path)
    assert second.get_session(token)["csrf_token"] == csrf
    assert second.owns_workflow("a" * 36, user_id)


def test_voice_session_is_limited_to_creator(tmp_path):
    class Speech:
        available = True

        def start(self):
            return "a" * 32

        def chunk(self, _session_id, _pcm):
            return "录音中"

        def finish(self, _session_id):
            return "完成"

    store = AuthStore(backend="sqlite", sqlite_path=tmp_path / "auth.sqlite3")
    store.ensure_schema()
    store.create_user("alice", "甲", "user", "test-password-alice")
    store.create_user("bob", "乙", "admin", "test-password-bob")
    app = create_app(checkpoint_backend="memory", auth_store=store, speech_manager=Speech())
    with TestClient(app) as alice, TestClient(app) as bob:
        csrf_a = _login(alice, "alice", "test-password-alice")
        csrf_b = _login(bob, "bob", "test-password-bob")
        session_id = _post(alice, "/api/voice/start", csrf=csrf_a).json()["session_id"]
        assert _post(bob, "/api/voice/chunk", csrf=csrf_b, json={
            "session_id": session_id, "audio": "AAEC",
        }).status_code == 404
        assert _post(alice, "/api/voice/chunk", csrf=csrf_a, json={
            "session_id": session_id, "audio": "AAEC",
        }).status_code == 200
        assert _post(alice, "/api/voice/finish", csrf=csrf_a, json={
            "session_id": session_id,
        }).status_code == 200
        assert _post(alice, "/api/voice/finish", csrf=csrf_a, json={
            "session_id": session_id,
        }).status_code == 404


def test_public_origin_sets_secure_cookie(tmp_path):
    store = AuthStore(backend="sqlite", sqlite_path=tmp_path / "auth.sqlite3")
    store.ensure_schema()
    store.create_user("crew", "船员", "user", "test-password-crew")
    with TestClient(create_app(checkpoint_backend="memory", auth_store=store),
                    base_url="https://ship.example") as client:
        response = client.post("/api/auth/login", headers={"Origin": "https://ship.example"},
                               json={"username": "crew", "password": "test-password-crew"})
        assert response.status_code == 200
        assert "secure" in response.headers["set-cookie"].lower()
        assert client.get("/api/auth/me").status_code == 200
