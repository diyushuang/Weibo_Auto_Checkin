"""v1.3.7 手机 App 同步端点测试：令牌鉴权、限流、按 uid 覆盖、死 Cookie 保护。"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db  # noqa: E402
from app import auth as auth_mod  # noqa: E402
from app.api import mobile as mobile_api  # noqa: E402
from app.main import app  # noqa: E402

GOOD_PW = "secret" + "321"
TOKEN = "tok-" + "abc123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_mobile.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.create_user("admin", auth_mod.hash_password(GOOD_PW))
    db.set_settings({
        "auth_enabled": "1",
        "mobile_sync_enabled": "1",
        "mobile_sync_token": TOKEN,
    })
    auth_mod.reset_login_limits()
    mobile_api._rate_stats.clear()

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PW})
        assert r.status_code == 200
        c.headers.update({
            "Cookie": f"{auth_mod.COOKIE_NAME}={c.cookies.get(auth_mod.COOKIE_NAME)}",
            "X-Panel-Token": TOKEN,
        })
        yield c
    db._local.conn = None


def _patch_weibo(monkeypatch, login=True, uid="777", name="手机昵称"):
    monkeypatch.setattr(mobile_api, "verify_cookie",
                        lambda session, cookies, **kw: (login, "st"))
    monkeypatch.setattr(mobile_api, "_fetch_weibo_profile",
                        lambda session: {"name": name, "avatar_url":
                                         "https://tvax.example.com/a.jpg", "uid": uid})


def _seed_account(uid="777", health="ok", name="面板名"):
    acc_id = db.add_account({
        "name": name, "cookie": "SUB=old; SUBP=p", "cookie_raw": "SUB=old; SUBP=p",
        "enabled": True, "weibo_uid": uid,
    })
    db.set_cookie_state(acc_id, cookie_health=health, cookie_issued_at=(
        datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"))
    return acc_id


# ---------- 设置种子 ----------

def test_mobile_settings_seeded(client):
    s = client.get("/api/settings").json()
    assert s["mobile_sync_enabled"] == "1"  # fixture 显式打开
    assert s["mobile_sync_last_at"] == ""


# ---------- 鉴权与限流 ----------

def test_sync_disabled_returns_403(client):
    db.set_settings({"mobile_sync_enabled": "0"})
    r = client.post("/api/mobile/sync", json={"cookie": "SUB=x"})
    assert r.status_code == 403


def test_sync_wrong_token_401_then_lockout(client):
    # 前 _FAIL_MAX-1 次失败返回 401，第 _FAIL_MAX 次触发锁定转 429
    for _ in range(mobile_api._FAIL_MAX - 1):
        r = client.post("/api/mobile/sync", json={"cookie": "SUB=x"},
                        headers={"X-Panel-Token": "wrong"})
        assert r.status_code == 401
    r = client.post("/api/mobile/sync", json={"cookie": "SUB=x"},
                    headers={"X-Panel-Token": "wrong"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    # 锁定期内正确令牌也不行（按 IP 锁，防止拿到令牌前先刷爆）
    r = client.post("/api/mobile/sync", json={"cookie": "SUB=x"})
    assert r.status_code == 429


def test_sync_missing_sub_400(client, monkeypatch):
    _patch_weibo(monkeypatch)
    r = client.post("/api/mobile/sync", json={"cookie": "SUBP=only"})
    assert r.status_code == 400


# ---------- 覆盖与保护 ----------

def test_sync_overwrites_same_uid_and_resets_lifecycle(client, monkeypatch):
    _patch_weibo(monkeypatch)
    acc_id = _seed_account(uid="777", health="ok", name="面板名")

    r = client.post("/api/mobile/sync",
                    json={"cookie": "SUB=new123; SUBP=p; SCF=s"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["updated"] is True
    assert body["name"] == "面板名"  # 老账号保留面板自定义名

    acc = db.get_account(acc_id)
    assert "SUB=new123" in (acc["cookie"] or acc["cookie_raw"])
    # on_cookie_replaced 重置了生命周期基准
    assert acc["cookie_issued_at"] >= (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert db.get_setting("mobile_sync_last_at") != ""


def test_sync_creates_new_account_with_profile(client, monkeypatch):
    _patch_weibo(monkeypatch, uid="888", name="新手机号")
    monkeypatch.setattr(mobile_api, "_cache_weibo_avatar",
                        lambda session, url, uid: f"/data/avatars/{uid}.jpg")
    r = client.post("/api/mobile/sync",
                    json={"cookie": "SUB=create1; SCF=s"})
    assert r.status_code == 200
    assert r.json()["updated"] is False
    assert r.json()["name"] == "新手机号"
    accs = db.get_accounts()
    assert len(accs) == 1
    assert accs[0]["weibo_uid"] == "888"
    assert accs[0]["avatar_url"].startswith("/data/avatars/")


def test_sync_dead_cookie_rejected_for_healthy_account(client, monkeypatch):
    _patch_weibo(monkeypatch, login=False, uid="777")
    acc_id = _seed_account(uid="777", health="ok")

    # App 自带 uid 才能在探活失败时匹配到面板账号
    r = client.post("/api/mobile/sync", json={"cookie": "SUB=dead1", "uid": "777"})
    assert r.status_code == 409
    acc = db.get_account(acc_id)
    assert "SUB=old" in acc["cookie"]  # 未被死 cookie 覆盖


def test_sync_dead_cookie_rejected_when_no_match(client, monkeypatch):
    """探不活的 cookie 匹配不到账号时：拒绝新建垃圾账号。"""
    _patch_weibo(monkeypatch, login=False, uid="777")
    r = client.post("/api/mobile/sync", json={"cookie": "SUB=dead9", "uid": "999"})
    assert r.status_code == 409
    assert db.get_accounts() == []


def test_sync_dead_cookie_allowed_for_invalid_account(client, monkeypatch):
    _patch_weibo(monkeypatch, login=False, uid="777")
    acc_id = _seed_account(uid="777", health="invalid")

    r = client.post("/api/mobile/sync", json={"cookie": "SUB=dead2", "uid": "777"})
    assert r.status_code == 200
    acc = db.get_account(acc_id)
    assert "SUB=dead2" in acc["cookie"]
    assert len(db.get_accounts()) == 1  # 覆盖而非新建


# ---------- 令牌再生成 ----------

def test_token_regenerate_invalidates_old(client):
    db.set_settings({"mobile_sync_token": ""})
    r = client.post("/api/mobile/token/regenerate")
    assert r.status_code == 200
    new_token = r.json()["token"]
    assert new_token and new_token != TOKEN
    assert db.get_setting("mobile_sync_token") == new_token
    assert db.get_setting("mobile_sync_enabled") == "1"
