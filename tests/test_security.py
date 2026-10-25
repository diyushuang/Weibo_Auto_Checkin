"""安全加固（v1.3.1）的专项测试：脱敏、SSRF 守卫、限流持久化、数据目录收窄。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.database as db  # noqa: E402
from app.main import app  # noqa: E402
from app.security import (  # noqa: E402
    UnsafeURLError,
    safe_exc,
    sanitize_message,
    validate_outbound_url,
    validate_weibo_url,
)

GOOD_PW = "secret" + "123"


# ---------------- 凭据脱敏 ----------------

def test_sanitize_message_strips_proxy_userinfo():
    text = "socks 代理请求失败：HTTPSConnectionPool(socks5://alice:secret123@1.2.3.4:1080)"
    out = sanitize_message(text)
    assert "alice" not in out
    assert "secret123" not in out
    assert "***@" in out


def test_sanitize_message_strips_bot_token():
    text = "HTTPSConnectionPool(host='api.telegram.org'): url: /bot123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-7s/sendMessage"
    out = sanitize_message(text)
    assert "AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-7s" not in out
    assert "/bot***" in out


def test_sanitize_message_strips_bearer_key():
    out = sanitize_message("调用失败：401 {'error': 'invalid key sk-abcdefghijklmnopqrstuvwx'}")
    assert "sk-abcdefghijklmnopqrstuvwx" not in out
    assert "sk-***" in out


def test_sanitize_message_collapses_newlines_and_caps_length():
    out = sanitize_message("line1\nline2\r\nline3\ttab" + "x" * 2000)
    assert "\n" not in out and "\t" not in out
    assert len(out) <= 500


def test_safe_exc_keeps_exception_type():
    try:
        raise ConnectionError("socks5://u:p@1.2.3.4 失败")
    except Exception as exc:
        out = safe_exc(exc)
    assert out.startswith("ConnectionError:")
    assert "u:p@" not in out


# ---------------- SSRF 守卫 ----------------

def test_validate_rejects_non_http_schemes():
    for url in ("ftp://example.com/x", "file:///etc/passwd", "gopher://x", "javascript:alert(1)"):
        with pytest.raises(UnsafeURLError):
            validate_outbound_url(url)


def test_validate_rejects_local_and_private_hosts():
    for url in (
        "http://localhost/api",
        "http://127.0.0.1:8000/api",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0/",
    ):
        with pytest.raises(UnsafeURLError):
            validate_outbound_url(url)


def test_validate_rejects_dns_rebinding_to_private(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.9", 0))])
    with pytest.raises(UnsafeURLError):
        validate_outbound_url("https://evil.example.com/")


def test_validate_allows_public_https():
    url = validate_outbound_url("https://m.weibo.cn/api/container/getIndex")
    assert url.startswith("https://m.weibo.cn")


def test_validate_weibo_allowlist():
    assert validate_weibo_url("https://weibo.com/ajax/page").startswith("https://")
    with pytest.raises(UnsafeURLError):
        validate_weibo_url("https://evil.example.com/steal")
    with pytest.raises(UnsafeURLError):
        validate_weibo_url("http://127.0.0.1/x")  # 白名单外 + 本地地址


def test_validate_weibo_login_allowlist(monkeypatch):
    """扫码登录链路放行新浪通行域（login.sina.com.cn），域伪造仍拒绝。"""
    import socket
    from app.security import validate_weibo_login_url
    # DNS 解析 mock 成公网地址，测试不依赖外网
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert validate_weibo_login_url("https://passport.weibo.com/sso/v2/login").startswith("https://")
    assert validate_weibo_login_url("https://login.sina.com.cn/sso/alt").startswith("https://")
    # 后缀 / 前缀伪造不落入白名单
    with pytest.raises(UnsafeURLError):
        validate_weibo_login_url("https://sina.com.cn.evil.example/steal")
    with pytest.raises(UnsafeURLError):
        validate_weibo_login_url("https://evil-sina.com.cn/steal")
    with pytest.raises(UnsafeURLError):
        validate_weibo_login_url("http://192.168.0.10/internal")


def test_checkin_topic_rejects_foreign_scheme():
    """微博返回的绝对 URL scheme 不得带 Cookie 访问外部主机。"""
    from app.weibo_client import checkin_topic

    with pytest.raises(UnsafeURLError):
        checkin_topic(None, {}, "http://evil.example.com/collect", "st")


def test_checkin_topic_accepts_relative_scheme(monkeypatch):
    """相对 scheme 拼接 BASE 后正常放行（mock 请求本身）。"""
    import app.weibo_client as wc

    captured = {}

    def fake_request_json(session, method, url, **kwargs):
        captured["url"] = url
        return {"ok": 1}

    monkeypatch.setattr(wc, "request_json", fake_request_json)
    out = wc.checkin_topic(None, {}, "/aj/t/checkin?tid=1", "st")
    assert out["ok"] == 1
    assert captured["url"].startswith(wc.BASE)


# ---------------- 登录限流持久化 ----------------

@pytest.fixture()
def db_client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_sec.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    yield db
    db._local.conn = None


def test_login_lock_survives_connection_reset(db_client):
    """限流状态落库：重连数据库（模拟重启）后锁定仍然生效。"""
    import time as _time

    for _ in range(4):
        assert db_client.record_login_failure("1.2.3.4", 5, 300, 300) == 0
    locked = db_client.record_login_failure("1.2.3.4", 5, 300, 300)
    assert locked == 300
    # 模拟进程重启：丢弃线程本地连接
    db_client._local.conn = None
    assert db_client.login_lock_remaining("1.2.3.4", 300) > 0


def test_login_lock_resets_on_success(db_client):
    db_client.record_login_failure("2.3.4.5", 5, 300, 300)
    db_client.reset_login_failures("2.3.4.5")
    assert db_client.login_lock_remaining("2.3.4.5", 300) == 0


# ---------------- 数据目录收窄 / 导出接口 ----------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_sec_api.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    from app import auth as auth_mod
    db.create_user("admin", auth_mod.hash_password(GOOD_PW))
    db.set_settings({"auth_enabled": "1"})
    auth_mod.reset_login_limits()
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PW})
        assert r.status_code == 200
        token = r.cookies.get(auth_mod.COOKIE_NAME)
        c.headers.update({"Cookie": f"{auth_mod.COOKIE_NAME}={token}"})
        yield c
    db._local.conn = None


def test_database_file_not_served(client):
    """/data 挂载收窄后，数据库文件不可通过 HTTP 下载。"""
    db_filename = db.DB_PATH.name
    r = client.get(f"/data/{db_filename}")
    assert r.status_code == 404
    r = client.get(f"/data/{db_filename}-wal")
    assert r.status_code == 404


def test_security_headers_present(client):
    r = client.get("/api/health")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert "default-src 'self'" in r.headers.get("content-security-policy", "")


def test_export_requires_post_and_confirm(client):
    """/api/accounts/export 改为 POST + 显式确认；GET 不可用（405/422）。"""
    assert client.get("/api/accounts/export").status_code in (405, 422)
    r = client.post("/api/accounts/export", json={"confirm": False})
    assert r.status_code == 400


def test_public_paths_exact_match(client):
    """/api/auth/login 前缀相似路径不得被公开白名单误放行。"""
    r = client.get("/api/auth/login-bogus")
    # 未登录（带有效 cookie 也无此路由）→ 404/405/401 之一，但绝不能放行出业务数据
    assert r.status_code in (401, 404, 405)


def test_health_anonymous_returns_slim_payload(tmp_path, monkeypatch):
    """v1.3.8：匿名 /api/health 只回存活标记，不暴露版本/鉴权开关/账号统计。"""
    from fastapi.testclient import TestClient
    from app import auth as auth_mod

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_health_anon.db")
    db._local.conn = None
    db.init_db()
    db.create_user("admin", auth_mod.hash_password("anon-test-password"))
    db.set_settings({"auth_enabled": "1"})
    try:
        with TestClient(app) as anon:
            body = anon.get("/api/health").json()
        assert body == {"ok": True}
    finally:
        db._local.conn = None


def test_health_authenticated_returns_full(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert "version" in body and "cookie" in body


# ---------------- 逐跳受控请求（v1.3.8，R-1 重定向 SSRF） ----------------

class _FakeResponse:
    def __init__(self, status_code=200, location=""):
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}
        self.is_redirect = status_code in (301, 302, 303, 307, 308) and bool(location)
        self.content = b"data"

    def close(self):
        pass


class _RecordingSession:
    """按预设脚本回放响应，并记录每一跳实际请求的 URL。"""

    def __init__(self, script):
        self.script = list(script)   # [(status, location), ...]
        self.requested = []

    def request(self, method, url, **kwargs):
        assert kwargs.get("allow_redirects") is False, "必须关闭自动重定向"
        self.requested.append(url)
        status, location = self.script.pop(0)
        return _FakeResponse(status, location)


def _no_dns(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])


def test_guarded_request_follows_validated_redirects(monkeypatch):
    _no_dns(monkeypatch)
    from app.security import guarded_request

    session = _RecordingSession([(302, "https://m.weibo.cn/next"), (200, "")])
    resp = guarded_request(session, "GET", "https://weibo.cn/start",
                           allowed_hosts=("weibo.cn", "weibo.com"))
    assert resp.status_code == 200
    assert session.requested == ["https://weibo.cn/start", "https://m.weibo.cn/next"]


def test_guarded_request_blocks_private_redirect(monkeypatch):
    """白名单域上的 30x 指向云元数据地址：必须拒绝而不是跟随（R-1 核心）。"""
    _no_dns(monkeypatch)
    from app.security import guarded_request

    session = _RecordingSession([(302, "http://169.254.169.254/latest/meta-data")])
    with pytest.raises(UnsafeURLError):
        guarded_request(session, "GET", "https://weibo.cn/start")
    assert len(session.requested) == 1   # 第二跳根本没发出去


def test_guarded_request_blocks_whitelist_escape_via_redirect(monkeypatch):
    """白名单内的入口 + 白名单外的重定向目标：同样拒绝。"""
    _no_dns(monkeypatch)
    from app.security import guarded_request

    session = _RecordingSession([(302, "https://evil.example.com/steal")])
    with pytest.raises(UnsafeURLError):
        guarded_request(session, "GET", "https://weibo.cn/start",
                        allowed_hosts=("weibo.cn",))


def test_guarded_request_relative_location_and_loop_cap(monkeypatch):
    _no_dns(monkeypatch)
    from app.security import guarded_request

    # 相对 Location 以当前 URL 为基准解析
    session = _RecordingSession([(302, "/jump"), (200, "")])
    resp = guarded_request(session, "GET", "https://weibo.cn/start",
                           allowed_hosts=("weibo.cn",))
    assert resp.status_code == 200
    assert session.requested[-1] == "https://weibo.cn/jump"

    # 恒 302 视为循环：达到跳数上限后中止
    loop = _RecordingSession([(302, "/again")] * 20)
    with pytest.raises(UnsafeURLError):
        guarded_request(loop, "GET", "https://weibo.cn/start",
                        allowed_hosts=("weibo.cn",))
    assert len(loop.requested) == 8


# ---------------- 手机同步限流持久化（v1.3.8，R-4） ----------------

def _mobile_rate(ip, ok):
    return db.mobile_rate_check_and_count(
        ip, ok,
        fail_window=900, fail_max=10, lock_seconds=900,
        ok_window=3600, ok_max=30,
    )


def test_mobile_rate_state_survives_connection_reset(db_client):
    """限流状态落库：重连数据库（模拟重启）后锁定与频控仍然生效。"""
    remaining = 0
    for _ in range(10):
        remaining = _mobile_rate("9.9.9.9", False)
    assert remaining > 0                      # 第 10 次失败触发锁定
    db._local.conn = None                     # 模拟进程重启
    assert _mobile_rate("9.9.9.9", True) > 0  # 锁定未丢：正确令牌也要等

    # 成功频控（-1）同样持久化
    result = 0
    for _ in range(31):
        result = _mobile_rate("8.8.8.8", True)
    assert result == -1


def test_mobile_rate_row_cap(db_client, monkeypatch):
    """条目封顶：伪造海量 IP 也撑不破限流表（按最旧淘汰）。"""
    monkeypatch.setattr(db, "MOBILE_RATE_MAX_IPS", 50)
    for i in range(60):
        _mobile_rate(f"198.51.100.{i}", False)
    count = db._get_conn().execute(
        "SELECT COUNT(*) FROM mobile_sync_rate").fetchone()[0]
    assert count <= 50
