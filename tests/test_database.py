"""测试数据库层。使用独立临时数据库，避免污染真实数据。"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 让临时数据库生效：先设置环境/路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把数据库指向临时文件，并初始化。"""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    # 重置线程本地连接
    db._local.conn = None
    db.init_db()
    yield db
    db._local.conn = None


# ---------- 设置 ----------

def test_init_seeds_defaults(tmp_db):
    assert tmp_db.get_setting("schedule_cron") == "0 7 * * *"
    assert tmp_db.get_setting("anti_ban_enabled") == "1"
    assert tmp_db.get_setting("tg_enabled") == "0"


def test_set_and_get_settings(tmp_db):
    tmp_db.set_settings({"tg_bot_token": "abc:123", "tg_user_id": "456"})
    assert tmp_db.get_setting("tg_bot_token") == "abc:123"
    assert tmp_db.get_setting("tg_user_id") == "456"
    # 不覆盖已有 key
    all_s = tmp_db.get_settings()
    assert all_s["schedule_cron"] == "0 7 * * *"


# ---------- 账号 ----------

def test_add_and_get_account(tmp_db):
    acc_id = tmp_db.add_account({
        "name": "小号1",
        "cookie": "SUB=abc",
        "cookie_raw": "SUB=abc; SCF=x",
        "enabled": True,
        "proxy_index": 0,
        "remark": "测试",
    })
    acc = tmp_db.get_account(acc_id)
    assert acc["name"] == "小号1"
    assert acc["cookie"] == "SUB=abc"
    assert acc["enabled"] == 1
    assert acc["last_status"] == "unknown"


def test_update_account(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": ""})
    assert tmp_db.update_account(acc_id, {"name": "B", "enabled": False})
    acc = tmp_db.get_account(acc_id)
    assert acc["name"] == "B"
    assert acc["enabled"] == 0


def test_delete_account(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": ""})
    assert tmp_db.delete_account(acc_id)
    assert tmp_db.get_account(acc_id) is None


def test_enabled_accounts(tmp_db):
    tmp_db.add_account({"name": "A", "cookie": "", "enabled": True})
    tmp_db.add_account({"name": "B", "cookie": "", "enabled": False})
    enabled = tmp_db.get_enabled_accounts()
    assert [a["name"] for a in enabled] == ["A"]


def test_touch_account_result(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": ""})
    tmp_db.touch_account_result(acc_id, "success", "全部成功")
    acc = tmp_db.get_account(acc_id)
    assert acc["last_status"] == "success"
    assert acc["last_message"] == "全部成功"
    assert acc["last_checkin"] is not None


def test_account_profile_fields_are_persisted_and_updated(tmp_db):
    account_id = tmp_db.add_account({
        "name": "资料账号",
        "cookie": "SUB=test",
        "avatar_url": "https://example.com/old-avatar.jpg",
        "weibo_uid": "10001",
    })
    account = tmp_db.get_account(account_id)
    assert account["avatar_url"] == "https://example.com/old-avatar.jpg"
    assert account["weibo_uid"] == "10001"

    assert tmp_db.update_account(account_id, {
        "avatar_url": "https://example.com/new-avatar.jpg",
        "weibo_uid": "10002",
    })
    account = tmp_db.get_account(account_id)
    assert account["avatar_url"] == "https://example.com/new-avatar.jpg"
    assert account["weibo_uid"] == "10002"


# ---------- 日志 ----------

def test_logs_and_stats(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": ""})
    tmp_db.add_log({
        "account_id": acc_id, "account_name": "A", "task_id": "t1",
        "status": "success", "channel": "direct",
        "total": 2, "success": 2, "fail": 0,
        "detail": [{"name": "超话1", "success": True}], "message": "ok",
    })
    tmp_db.add_log({
        "account_id": acc_id, "account_name": "A", "task_id": "t2",
        "status": "failed", "channel": "socks",
        "total": 1, "success": 0, "fail": 1,
        "detail": [], "message": "Cookie 失效",
    })
    logs = tmp_db.get_logs()
    assert len(logs) == 2
    # 最新(id 大)在前：logs[0] 是 t2(空 detail)，logs[1] 是 t1
    assert logs[0]["task_id"] == "t2"
    assert logs[0]["detail"] == []
    # detail 被正确 JSON 解析（t1 的 detail 有数据）
    assert logs[1]["task_id"] == "t1"
    assert logs[1]["detail"][0]["name"] == "超话1"
    # 按账号过滤
    assert len(tmp_db.get_logs(account_id=acc_id)) == 2

    stats = tmp_db.get_log_stats()
    assert stats["total"] == 2
    assert stats["success"] == 1
    assert stats["fail"] == 1
    assert stats["today"] == 2


# ---------- 任务 & 通知 ----------

def test_task_lifecycle(tmp_db):
    tmp_db.create_task("task-xyz", "manual")
    tasks = tmp_db.get_tasks()
    assert tasks[0]["status"] == "running"
    tmp_db.finish_task("task-xyz", "success", "完成")
    tasks = tmp_db.get_tasks()
    assert tasks[0]["status"] == "success"
    assert tasks[0]["finished_at"] is not None


def test_notification_record(tmp_db):
    tmp_db.add_notification("标题", "内容", True)
    tmp_db.add_notification("标题2", "内容2", False, "网络错误")
    notif = tmp_db.get_notifications()
    assert len(notif) == 2
    assert notif[0]["success"] == 0  # 最新在前
    assert notif[1]["success"] == 1


def test_get_logs_grouped(tmp_db):
    """日志按日期分组 + 同次执行归并。"""
    acc_a = tmp_db.add_account({"name": "A", "cookie": ""})
    acc_b = tmp_db.add_account({"name": "B", "cookie": ""})
    # 同一次任务（同 task_id）两个账号
    tmp_db.add_log({"account_id": acc_a, "account_name": "A", "task_id": "t1",
                    "status": "success", "channel": "direct", "total": 2, "success": 2, "fail": 0,
                    "detail": [{"name": "超话1", "success": True}], "message": ""})
    tmp_db.add_log({"account_id": acc_b, "account_name": "B", "task_id": "t1",
                    "status": "failed", "channel": "socks", "total": 1, "success": 0, "fail": 1,
                    "detail": [], "message": "Cookie 失效"})
    # 另一次任务
    tmp_db.add_log({"account_id": acc_a, "account_name": "A", "task_id": "t2",
                    "status": "success", "channel": "direct", "total": 0, "success": 0, "fail": 0,
                    "detail": [], "message": "成功"})

    grouped = tmp_db.get_logs_grouped()
    assert len(grouped) >= 1
    # 取最新日期组
    day = grouped[0]
    assert day["date"]  # 有日期
    # 该日期下的任务组
    groups = day["groups"]
    # t2 是最新的，可能在前面；但关键是 t1 的两个账号归为一组
    t1_groups = [g for g in groups if g["task_id"] == "t1"]
    assert t1_groups, "t1 任务应存在"
    g = t1_groups[0]
    assert len(g["accounts"]) == 2          # 两个账号归并到同一组
    assert g["total"] == 3                  # 2+1
    assert g["success"] == 2
    assert g["fail"] == 1
    assert g["status"] == "failed"          # 有 failed 账号 → 组失败


# ---------- Cookie 生命周期（保活与到期可见） ----------

def test_cookie_columns_and_settings_migrated(tmp_db):
    cols = {r["name"] for r in tmp_db._get_conn().execute(
        "PRAGMA table_info(accounts)").fetchall()}
    for name in ("cookie_expires_at", "cookie_issued_at", "cookie_last_seen_valid_at",
                 "cookie_last_rotated_at", "cookie_last_rotate_keys",
                 "cookie_next_keepalive_at", "cookie_health", "cookie_fail_streak",
                 "cookie_warned_at", "cookie_lifetime_observed_days"):
        assert name in cols, f"缺少列 {name}"
    assert tmp_db.get_setting("keepalive_enabled") == "1"
    assert tmp_db.get_setting("keepalive_interval_hours") == "12"
    assert tmp_db.get_setting("cookie_session_ttl_days") == "7"
    # 旧的占位配置已删除
    assert "cookie_valid" not in tmp_db.get_settings()


def test_add_account_seeds_cookie_baseline(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie_raw": "SUB=s; SCF=c; ALF=1893456000"})
    acc = tmp_db.get_account(acc_id)
    assert acc["cookie_issued_at"]
    assert acc["cookie_expires_at"].startswith("2030-01-01")
    assert acc["cookie_health"] == "unknown"
    assert acc["cookie_next_keepalive_at"] > acc["cookie_issued_at"]


def test_update_account_replacing_cookie_resets_baseline(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": "SUB=old; SCF=c"})
    first_issued = tmp_db.get_account(acc_id)["cookie_issued_at"]
    tmp_db.set_cookie_state(acc_id, cookie_fail_streak=3, cookie_health="network_error")

    tmp_db.update_account(acc_id, {"cookie": "SUB=new; SCF=c"})
    acc = tmp_db.get_account(acc_id)
    assert acc["cookie_issued_at"] >= first_issued
    assert acc["cookie_fail_streak"] == 0
    assert acc["cookie_health"] == "unknown"


def test_update_account_without_cookie_change_keeps_baseline(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": "SUB=s; SCF=c"})
    tmp_db.set_cookie_state(acc_id, cookie_last_rotated_at="2026-10-01 00:00:00")
    before = tmp_db.get_account(acc_id)

    tmp_db.update_account(acc_id, {"cookie": "SUB=s; SCF=c", "name": "改名"})
    after = tmp_db.get_account(acc_id)
    assert after["name"] == "改名"
    assert after["cookie_last_rotated_at"] == before["cookie_last_rotated_at"] == "2026-10-01 00:00:00"


def test_merge_account_cookie_unions_without_dropping_keys(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": "SUB=old; SCF=c; UOR=x"})
    assert tmp_db.merge_account_cookie(acc_id, "SUB=new") is True
    acc = tmp_db.get_account(acc_id)
    assert acc["cookie"] == acc["cookie_raw"]
    for fragment in ("SUB=new", "SCF=c", "UOR=x"):
        assert fragment in acc["cookie"], acc["cookie"]


def test_merge_account_cookie_cas_rejects_stale_write(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": "SUB=v1; SCF=c"})
    # 模拟别的线程已经先续期到 v2
    tmp_db.merge_account_cookie(acc_id, "SUB=v2; SCF=c")
    assert tmp_db.merge_account_cookie(
        acc_id, "SUB=v1.5; SCF=c", expected_old="SUB=v1; SCF=c") is False
    assert "SUB=v2" in tmp_db.get_account(acc_id)["cookie"]


def test_merge_account_cookie_rejects_empty(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": "SUB=s"})
    assert tmp_db.merge_account_cookie(acc_id, "") is False
    assert tmp_db.merge_account_cookie(9999, "SUB=x") is False


def test_list_keepalive_due(tmp_db):
    a_ok = tmp_db.add_account({"name": "到期", "cookie": "SUB=1"})
    tmp_db.set_cookie_state(a_ok, cookie_next_keepalive_at="2020-01-01 00:00:00")
    a_future = tmp_db.add_account({"name": "未到点", "cookie": "SUB=2"})
    tmp_db.set_cookie_state(a_future, cookie_next_keepalive_at="2099-01-01 00:00:00")
    a_dead = tmp_db.add_account({"name": "已判死", "cookie": "SUB=3"})
    tmp_db.set_cookie_state(a_dead, cookie_health="invalid",
                            cookie_next_keepalive_at="")
    a_blank = tmp_db.add_account({"name": "没排期", "cookie": "SUB=4"})
    tmp_db.set_cookie_state(a_blank, cookie_next_keepalive_at="")
    a_nocookie = tmp_db.add_account({"name": "空 cookie", "cookie": ""})
    a_disabled = tmp_db.add_account({"name": "停用", "cookie": "SUB=5", "enabled": False})

    due = {a["id"] for a in tmp_db.list_keepalive_due()}
    assert due == {a_ok, a_blank}
    assert a_dead not in due and a_future not in due
    assert a_nocookie not in due and a_disabled not in due


def test_mark_cookie_dead_records_observed_lifetime(tmp_db):
    from datetime import datetime, timedelta
    acc_id = tmp_db.add_account({"name": "A", "cookie": "SUB=s"})
    issued = datetime.now() - timedelta(days=4, hours=6)
    tmp_db.set_cookie_state(acc_id, cookie_issued_at=issued.strftime("%Y-%m-%d %H:%M:%S"))

    lifetime = tmp_db.mark_cookie_dead(acc_id)
    acc = tmp_db.get_account(acc_id)
    assert lifetime == 4                      # 实测 session 寿命取整天
    assert acc["cookie_lifetime_observed_days"] == 4
    assert acc["cookie_health"] == "invalid"
    assert acc["cookie_next_keepalive_at"] == ""


def test_count_cookie_health(tmp_db):
    tmp_db.add_account({"name": "A", "cookie": "SUB=1"})
    b = tmp_db.add_account({"name": "B", "cookie": "SUB=2"})
    tmp_db.mark_cookie_dead(b)
    counts = tmp_db.count_cookie_health()
    assert counts.get("invalid") == 1
    assert counts.get("unknown") == 1


def test_set_cookie_state_ignores_unknown_columns(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": "SUB=s"})
    assert tmp_db.set_cookie_state(acc_id, name="被篡改", cookie_health="ok") is True
    acc = tmp_db.get_account(acc_id)
    assert acc["name"] == "A"
    assert acc["cookie_health"] == "ok"
