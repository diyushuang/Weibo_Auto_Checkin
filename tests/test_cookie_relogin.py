"""v1.3.6 扫码续期闭环测试：needs_relogin 判定、新设置种子、
实验性 PC 端点换发的出站白名单与异常吞并、mini_client 子命令注册。
"""
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db  # noqa: E402
from app import cookie_lifecycle  # noqa: E402
from app import weibo_client as wb  # noqa: E402

STAMP_FMT = "%Y-%m-%d %H:%M:%S"


def _stamp(**delta) -> str:
    return (datetime.now() + timedelta(**delta)).strftime(STAMP_FMT)


def _make_acc(**fields) -> dict:
    """构造一个带一年期 ALF 的账号行，生命周期锚点按需覆盖。

    ALF=4102444800 是 2100 年，远在经验寿命之后，deadline 只由锚点决定。
    """
    acc = {
        "id": 1,
        "name": "测试账号",
        "cookie": "SUB=abc; SUBP=def; ALF=4102444800",
        "cookie_health": "unknown",
        "cookie_issued_at": "",
        "cookie_last_rotated_at": "",
        "cookie_last_seen_valid_at": "",
    }
    acc.update(fields)
    return acc


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_relogin.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    yield db
    db._local.conn = None


# ---------- 新设置种子 ----------

def test_relogin_settings_seeded(tmp_db):
    assert tmp_db.get_setting("cookie_relogin_stale_hours") == "48"
    assert tmp_db.get_setting("panel_url") == ""
    assert tmp_db.get_setting("keepalive_passport_enabled") == "0"
    assert "https://weibo.com/" in tmp_db.get_setting("keepalive_passport_urls")


# ---------- needs_relogin_reason ----------

def test_invalid_needs_relogin(tmp_db):
    reason = cookie_lifecycle.needs_relogin_reason(_make_acc(cookie_health="invalid"))
    assert "扫码" in reason


def test_expired_needs_relogin(tmp_db):
    acc = _make_acc(cookie_issued_at=_stamp(days=-10))
    assert cookie_lifecycle.cookie_expiry_view(acc)["state"] == "expired"
    assert cookie_lifecycle.needs_relogin_reason(acc) != ""


def test_expiring_without_rotation_needs_relogin(tmp_db):
    # 录入 5.5 天前 → 经验寿命还剩 1.5 天，落在预警窗口内
    acc = _make_acc(cookie_issued_at=_stamp(hours=-132))
    assert cookie_lifecycle.cookie_expiry_view(acc)["state"] == "expiring"
    assert "从未" in cookie_lifecycle.needs_relogin_reason(acc)


def test_expiring_with_recent_rotation_is_fine(tmp_db):
    acc = _make_acc(cookie_issued_at=_stamp(hours=-132),
                    cookie_last_rotated_at=_stamp(hours=-1))
    assert cookie_lifecycle.needs_relogin_reason(acc) == ""


def test_expiring_with_stale_rotation_needs_relogin(tmp_db):
    acc = _make_acc(cookie_issued_at=_stamp(hours=-132),
                    cookie_last_rotated_at=_stamp(hours=-49))
    assert "换发" in cookie_lifecycle.needs_relogin_reason(acc)


def test_stale_threshold_is_configurable(tmp_db):
    tmp_db.set_settings({"cookie_relogin_stale_hours": "1"})
    acc = _make_acc(cookie_issued_at=_stamp(hours=-132),
                    cookie_last_rotated_at=_stamp(hours=-2))
    assert cookie_lifecycle.needs_relogin_reason(acc) != ""


def test_healthy_account_does_not_need_relogin(tmp_db):
    acc = _make_acc(cookie_issued_at=_stamp(days=-1))
    assert cookie_lifecycle.cookie_expiry_view(acc)["state"] == "ok"
    assert cookie_lifecycle.needs_relogin_reason(acc) == ""


# ---------- touch_passport_endpoints ----------

class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass


@pytest.fixture()
def fake_dns(monkeypatch):
    """出站校验会真实解析 DNS，测试里固定解析成一个公网地址。"""
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])


def test_touch_passport_skips_non_weibo_hosts(monkeypatch, fake_dns):
    calls = []

    def _fake(session, method, url, **kwargs):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(wb, "request_via_proxy", _fake)
    touched = wb.touch_passport_endpoints(
        None, {"SUB": "abc"},
        ["https://evil.example.com/", "http://192.168.0.10/", "ftp://x/"],
    )
    assert touched == []
    assert calls == []


def test_touch_passport_requests_whitelisted_url(monkeypatch, fake_dns):
    calls = []

    def _fake(session, method, url, **kwargs):
        calls.append((url, kwargs))
        return _Resp()

    monkeypatch.setattr(wb, "request_via_proxy", _fake)
    touched = wb.touch_passport_endpoints(
        None, {"SUB": "abc"},
        ["https://weibo.com/", "https://passport.weibo.com/"])
    assert touched == ["https://weibo.com/", "https://passport.weibo.com/"]
    assert all(kw.get("cookies") == {"SUB": "abc"} for _u, kw in calls)


def test_touch_passport_swallows_request_errors(monkeypatch, fake_dns):
    def _boom(*a, **k):
        raise ConnectionError("network down")

    monkeypatch.setattr(wb, "request_via_proxy", _boom)
    assert wb.touch_passport_endpoints(
        None, {"SUB": "abc"}, ["https://weibo.com/"]) == []


# ---------- mini_client rotate-probe 命令注册 ----------

def test_mini_client_has_rotate_probe_command():
    import mini_client
    parser = mini_client.build_parser()
    args = parser.parse_args(["rotate-probe", "--urls", "https://weibo.com/"])
    assert args.func is mini_client.cmd_rotate_probe
    assert args.urls == "https://weibo.com/"
