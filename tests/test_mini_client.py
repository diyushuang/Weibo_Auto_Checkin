# -*- coding: utf-8 -*-
"""mini_client.py（DB-backed CLI）回归测试：全部 mock 出网，SQLite 用临时库。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mini_client as mc
import app.database as database
import app.scheduler as sched
import app.topics_service as topics_service
from app import accounts_service
from app.weibo_client import NetworkError, _bundle


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """每个测试独占一个临时库；CLI 与测试进程看到同一份。"""
    db = tmp_path / "mini.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database._local.conn = None
    database.init_db()
    database.set_settings({"anti_ban_enabled": "0", "tg_enabled": "0",
                           "auth_enabled": "0",
                           "checkin_delay_min": "0", "checkin_delay_max": "0",
                           "keepalive_gap_seconds": "0"})
    yield db
    database._local.conn = None


def _quiet_settings_off():
    database.set_settings({"keepalive_quiet_hours": "off"})


def _db_args(db):
    return ["--db", str(db)]


# ========================= 服务层：解析与去重 =========================

def test_parse_account_line_formats():
    assert accounts_service.parse_account_line("# 注释") is None
    assert accounts_service.parse_account_line("") is None
    a = accounts_service.parse_account_line("SUB=a; SCF=1", 3)
    assert a == {"name": "账号3", "proxy": "", "cookie": "SUB=a; SCF=1"}
    b = accounts_service.parse_account_line("小号A|SUB=b; SCF=2")
    assert b["name"] == "小号A" and b["proxy"] == ""
    c = accounts_service.parse_account_line(
        "小号B|socks5://h:1080|SUB=c; SCF=3")
    assert c["proxy"] == "socks5://h:1080" and c["cookie"] == "SUB=c; SCF=3"
    with pytest.raises(ValueError):
        accounts_service.parse_account_line("不是cookie")


def test_upsert_account_dedup():
    acc1, action1 = accounts_service.upsert_account("SUB=s1; SCF=c", name="A")
    assert action1 == "created"
    # 同 SUB：更新而非新增（不传名，避免覆盖原名称）
    acc2, action2 = accounts_service.upsert_account("SUB=s1; SCF=c2", name="")
    assert action2 == "updated" and acc2["id"] == acc1["id"]
    assert "SCF=c2" in acc2["cookie"]
    # 名称匹配（无 SUB 命中）：更新
    acc3, action3 = accounts_service.upsert_account(
        "SUB=s3; SCF=c", name="A", match_name="A")
    assert action3 == "updated" and acc3["id"] == acc1["id"]
    # 全新：创建
    acc4, action4 = accounts_service.upsert_account("SUB=s4; SCF=c", name="B")
    assert action4 == "created" and acc4["id"] != acc1["id"]


# ========================= import 命令 =========================

def test_import_command_and_idempotent(tmp_path, capsys):
    f = tmp_path / "acc.txt"
    f.write_text(
        "SUB=a; SCF=1\n小号A|SUB=b; SCF=2\n"
        "小号B|socks5://h:1080|SUB=c; SCF=3\n", encoding="utf-8")
    code = mc.main(["import", "--file", str(f)] + _db_args(_isolated_db_impl(tmp_path)))
    assert code == 0
    assert len(database.get_accounts()) == 3
    out = capsys.readouterr().out
    assert "新增 3" in out
    # 重复导入：全部走更新，不产生重复账号
    code2 = mc.main(["import", "--file", str(f)] + _db_args(_isolated_db_impl(tmp_path)))
    assert code2 == 0
    assert len(database.get_accounts()) == 3
    assert "更新 3" in capsys.readouterr().out


def _isolated_db_impl(tmp_path):
    # fixture 里设置的同一份数据库路径
    return database.DB_PATH


def test_import_inline_cookie():
    code = mc.main(["import", "--cookie", "SUB=inline; SCF=1",
                    "--name", "临时号"] + _db_args(_isolated_db_impl(None)))
    assert code == 0
    names = [a["name"] for a in database.get_accounts()]
    assert "临时号" in names


# ========================= legacy 迁移 =========================

def test_legacy_state_auto_migration(tmp_path, capsys):
    legacy = tmp_path / "mini_state.json"
    legacy.write_text(json.dumps({"version": 1, "accounts": {
        "老号A": {"file_cookie": "SUB=old1; SCF=c", "cookie": "SUB=old1; SCF=c",
                  "proxy": ""},
        "老号B": {"cookie": "SUB=old2; SCF=c",
                  "proxy": "socks5://h:1080"},
    }}), encoding="utf-8")
    # 迁移挂在零出网的 topics list 上验证（迁移本身与业务命令解耦）
    code = mc.main(["topics", "list", "--legacy-state", str(legacy)]
                   + _db_args(_isolated_db_impl(tmp_path)))
    assert code == 0
    names = {a["name"]: a for a in database.get_accounts()}
    assert "老号A" in names and "老号B" in names
    assert names["老号B"]["proxy"] == "socks5://h:1080"
    assert not legacy.exists()
    assert legacy.with_name("mini_state.json.migrated").exists()
    assert "已迁移 2 个账号" in capsys.readouterr().out


# ========================= checkin / keepalive 委托 =========================

def test_checkin_delegates_and_persists_rotation(monkeypatch):
    accounts_service.upsert_account("SUB=s0; SCF=c", name="号1")

    def fake_checkin(cookie_dict, opts, proxy_url=None, proxy_index=0):
        merged = dict(cookie_dict)
        merged["SUBP"] = "fresh"
        return _bundle("success", "签到完成", 1, 1, 0, [], merged, ["SUBP"], "direct")

    monkeypatch.setattr(sched, "run_account_checkin", fake_checkin)
    code = mc.main(["checkin"] + _db_args(_isolated_db_impl(None)))
    assert code == 0
    acc = database.get_accounts()[0]
    assert "SUBP=fresh" in acc["cookie"]
    assert acc["last_status"] == "success"


def test_keepalive_delegates_rotation_and_invalid(monkeypatch):
    accounts_service.upsert_account("SUB=s0; SCF=c", name="号1")

    class _Jar:
        def __init__(self, name, value):
            self.name, self.value = name, value

    class _FakeSession:
        def __init__(self):
            self.cookies = []

    def fake_verify(session, cookies, **kw):
        session.cookies.append(_Jar("SUB", "rotated"))
        return True, "st"

    monkeypatch.setattr(sched, "verify_cookie", fake_verify)
    monkeypatch.setattr(sched, "new_account_session", lambda: _FakeSession())
    monkeypatch.setattr(sched, "get_followed_topics", lambda *a, **kw: [])
    code = mc.main(["keepalive", "--force"] + _db_args(_isolated_db_impl(None)))
    assert code == 0
    acc = database.get_accounts()[0]
    assert "SUB=rotated" in acc["cookie"]
    assert acc["cookie_next_keepalive_at"]

    # 登录态失效：退出码 1，账号标 invalid
    monkeypatch.setattr(sched, "verify_cookie", lambda *a, **kw: (False, None))
    code2 = mc.main(["keepalive", "--force", "--json"]
                    + _db_args(_isolated_db_impl(None)))
    assert code2 == 1
    assert database.get_accounts()[0]["cookie_health"] == "invalid"


def test_keepalive_network_failure_not_fatal(monkeypatch):
    accounts_service.upsert_account("SUB=s0; SCF=c", name="号1")

    def boom(*a, **kw):
        raise NetworkError("代理不通")

    monkeypatch.setattr(sched, "verify_cookie", boom)
    monkeypatch.setattr(sched, "new_account_session", lambda: None)
    code = mc.main(["keepalive", "--force"] + _db_args(_isolated_db_impl(None)))
    assert code == 0
    acc = database.get_accounts()[0]
    assert acc["cookie_fail_streak"] == 1
    assert acc["cookie_health"] == "network_error"
    assert not acc["cookie_health"] == "invalid"


# ========================= topics 命令 =========================

def test_topics_list_and_refresh(monkeypatch, capsys):
    acc_id = accounts_service.upsert_account("SUB=s0; SCF=c", name="号1")[0]["id"]
    # list：空缓存
    code = mc.main(["topics", "list"] + _db_args(_isolated_db_impl(None)))
    assert code == 0
    # refresh：mock 列表接口
    fake_topics = [
        {"name": "超话A", "id": "100808a", "scheme": "/checkin?a=1", "done": False},
        {"name": "超话B", "id": "100808b", "scheme": None, "done": True},
    ]
    monkeypatch.setattr(topics_service, "get_followed_topics",
                        lambda *a, **kw: fake_topics)
    code = mc.main(["topics", "refresh"] + _db_args(_isolated_db_impl(None)))
    assert code == 0
    cache = database.get_topic_cache(acc_id)
    assert cache and len(cache["topics"]) == 2
    assert database.get_all_topic("100808a") is not None
    out = capsys.readouterr().out
    assert "[OK] 号1：2 个超话" in out
    # list：有缓存
    code = mc.main(["topics", "list"] + _db_args(_isolated_db_impl(None)))
    assert code == 0
    assert "超话A" in capsys.readouterr().out
