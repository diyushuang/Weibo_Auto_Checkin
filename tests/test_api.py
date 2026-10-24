"""API 集成测试：通过 FastAPI TestClient 测试全部接口。
使用临时数据库，不触碰真实网络（验证 Cookie 接口会用 mock 替换 requests）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db  # noqa: E402
from app.main import app  # noqa: E402

# 测试口令（拼接写法避免被日志脱敏工具改写）
GOOD_PW = "secret" + "123"
BAD_PW = "definitely" + "-wrong"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_api.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    # 创建默认管理员并启用登录
    from app import auth as auth_mod
    db.create_user("admin", auth_mod.hash_password(GOOD_PW))
    db.set_settings({"auth_enabled": "1"})
    # v7.2 起登录有失败限流，清掉上一个测试留下的锁定状态
    auth_mod.reset_login_limits()

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        # 登录获取 token
        r = c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PW})
        assert r.status_code == 200
        token = r.cookies.get(auth_mod.COOKIE_NAME)
        c.headers.update({"Cookie": f"{auth_mod.COOKIE_NAME}={token}"})
        yield c

    db._local.conn = None


def _unauth_client(tmp_path, monkeypatch):
    """未登录客户端（用于测 401）。"""
    db_path = tmp_path / "test_api_unauth.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    from app import auth as auth_mod
    db.create_user("admin", auth_mod.hash_password(GOOD_PW))
    db.set_settings({"auth_enabled": "1"})
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c
    db._local.conn = None


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_account_crud(client):
    # 创建
    r = client.post("/api/accounts", json={
        "name": "小号A", "cookie_raw": "SUB=abc; SCF=x", "enabled": True,
    })
    assert r.status_code == 200
    acc_id = r.json()["id"]

    # 列表
    r = client.get("/api/accounts")
    assert len(r.json()) == 1
    assert r.json()[0]["name"] == "小号A"

    # 详情
    r = client.get(f"/api/accounts/{acc_id}")
    assert r.status_code == 200
    assert r.json()["cookie_length"] > 0

    # 更新
    r = client.put(f"/api/accounts/{acc_id}", json={"name": "小号B",
                                                    "enabled": False})
    assert r.json()["name"] == "小号B"
    assert r.json()["enabled"] == 0

    # 404
    assert client.get("/api/accounts/9999").status_code == 404

    # 删除
    assert client.delete(f"/api/accounts/{acc_id}").status_code == 200
    assert client.get("/api/accounts").json() == []


def test_settings_api(client):
    # 默认值
    r = client.get("/api/settings")
    assert r.json()["schedule_cron"] == "0 7 * * *"

    # 更新
    r = client.post("/api/settings", json={
        "tg_bot_token": "tok", "tg_enabled": "1", "schedule_cron": "0 8 * * *",
    })
    assert r.json()["tg_bot_token"] == "tok"
    assert r.json()["schedule_cron"] == "0 8 * * *"

    # 未知 key 应被忽略
    r = client.post("/api/settings", json={"hacker_key": "x"})
    assert "hacker_key" not in r.json()


def test_logs_stats_api(client):
    # 先建账号和手动插日志
    acc_id = client.post("/api/accounts", json={"name": "A", "cookie": ""}).json()["id"]
    db.add_log({
        "account_id": acc_id, "account_name": "A", "task_id": "t",
        "status": "success", "channel": "direct",
        "total": 1, "success": 1, "fail": 0, "detail": [], "message": "",
    })
    stats = client.get("/api/logs/stats").json()
    assert stats["total"] == 1
    assert stats["success"] == 1


def test_verify_endpoint_invalid_cookie(client):
    """校验接口应返回 valid=False 并给出提示（真实调用会被 mock 拦截）。"""
    acc_id = client.post("/api/accounts", json={
        "name": "A", "cookie_raw": "SUB=x",
    }).json()["id"]

    # mock 微博验证接口：返回未登录
    import app.api.accounts as acc_mod
    import requests
    from unittest.mock import patch

    class FakeResp:
        status_code = 200
        def json(self):
            return {"data": {"login": False, "st": None}}
        def raise_for_status(self):
            pass

    with patch.object(requests.Session, "request",
                      return_value=FakeResp()):
        r = client.post(f"/api/accounts/{acc_id}/verify")
        assert r.json()["valid"] is False
        assert "无效" in r.json()["message"]


def test_checkin_flow_with_mock(client):
    """用 mock 微博接口测试完整签到流程（成功路径）。"""
    acc_id = client.post("/api/accounts", json={
        "name": "成功号", "cookie_raw": "SUB=valid; SCF=x",
    }).json()["id"]

    from unittest.mock import patch
    from app import scheduler
    from app.weibo_client import _bundle

    fake_result = _bundle(
        "success", "签到完成", 2, 2, 0,
        [{"name": "超话1", "success": True, "message": "已签到"},
         {"name": "超话2", "success": True, "message": "今日已签到"}],
        {"SUB": "refreshed", "SCF": "x"}, ["SUB"], "direct",
    )

    with patch.object(scheduler, "run_account_checkin", return_value=fake_result):
        summary = scheduler.run_checkin("manual")

    assert summary["status"] == "success"
    assert summary["total"] == 2
    assert summary["success"] == 2

    # 账号状态已更新
    acc = db.get_account(acc_id)
    assert acc["last_status"] == "success"
    # Cookie 已回写为字符串
    assert "refreshed" in (acc["cookie"] or "")


def test_static_files(client):
    assert client.get("/").status_code == 200
    assert "text/html" in client.get("/").headers["content-type"]
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200


def test_run_checkin_groups_by_proxy(tmp_path, monkeypatch):
    """不同 socks 的账号应分组（不同线程），同 socks 顺序。用 mock 验证。"""
    import app.database as db
    import app.scheduler as sched
    from app import auth as auth_mod
    from app.weibo_client import _bundle

    db_path = tmp_path / "test_group.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.create_user("admin", auth_mod.hash_password("sec123456"))
    db.set_settings({"auth_enabled": "0", "anti_ban_enabled": "0"})

    # 三个账号：两个用 soapA，一个用 soapB
    a1 = db.add_account({"name": "A1", "cookie_raw": "SUB=a", "proxy": "socks5://p1:1080"})
    a2 = db.add_account({"name": "A2", "cookie_raw": "SUB=b", "proxy": "socks5://p1:1080"})
    a3 = db.add_account({"name": "A3", "cookie_raw": "SUB=c", "proxy": "socks5://p2:1080"})

    called = []
    def fake_checkin(cookie, opts, proxy_url=None, proxy_index=0):
        called.append(proxy_url)
        return _bundle("success", "ok", 1, 1, 0,
                       [{"name": "超话", "success": True, "message": "已签到"}],
                       dict(cookie), [], proxy_url or "direct")

    from unittest.mock import patch
    with patch.object(sched, "run_account_checkin", side_effect=fake_checkin):
        summary = sched.run_checkin("manual")

    assert summary["status"] == "success"
    assert summary["accounts"] == 3
    # A1/A2 用 p1 代理，A3 用 p2 代理
    assert "socks5://p1:1080" in called, f"p1 代理应被调用, got {called}"
    assert "socks5://p2:1080" in called
    db._local.conn = None


def test_run_checkin_selected_accounts(tmp_path, monkeypatch):
    """手动签到指定账号（多选）：只签到选中且启用的账号。"""
    import app.database as db
    import app.scheduler as sched
    from app import auth as auth_mod
    from app.weibo_client import _bundle

    db_path = tmp_path / "test_sel.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.create_user("admin", auth_mod.hash_password("sec123456"))
    db.set_settings({"auth_enabled": "0", "anti_ban_enabled": "0"})

    a1 = db.add_account({"name": "A1", "cookie_raw": "SUB=a", "enabled": 1})
    a2 = db.add_account({"name": "A2", "cookie_raw": "SUB=b", "enabled": 1})
    a3 = db.add_account({"name": "A3", "cookie_raw": "SUB=c", "enabled": 1})

    called = []
    def fake_checkin(cookie, opts, proxy_url=None, proxy_index=0):
        name = cookie.get("SUB", "?")
        called.append(name)
        return _bundle("success", "ok", 1, 1, 0,
                       [{"name": "超话", "success": True, "message": "已签到"}],
                       dict(cookie), [], proxy_url or "direct")

    from unittest.mock import patch
    with patch.object(sched, "run_account_checkin", side_effect=fake_checkin):
        summary = sched.run_checkin("manual", account_ids=[a1, a3])

    assert summary["accounts"] == 2, f"应只签到 2 个账号, got {summary['accounts']}"
    assert set(called) == {"a", "c"}, f"应只含 A1/A3, got {called}"
    db._local.conn = None


def test_run_checkin_disabled_excluded(tmp_path, monkeypatch):
    """禁用的账号不应进入自动签到队列。"""
    import app.database as db
    import app.scheduler as sched
    from app import auth as auth_mod
    from app.weibo_client import _bundle

    db_path = tmp_path / "test_dis.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.create_user("admin", auth_mod.hash_password("sec123456"))
    db.set_settings({"auth_enabled": "0", "anti_ban_enabled": "0"})

    db.add_account({"name": "ON", "cookie_raw": "SUB=on", "enabled": 1})
    db.add_account({"name": "OFF", "cookie_raw": "SUB=off", "enabled": 0})

    called = []
    def fake_checkin(cookie, opts, proxy_url=None, proxy_index=0):
        called.append(cookie.get("SUB"))
        return _bundle("success", "ok", 1, 1, 0, [], dict(cookie), [], "direct")

    from unittest.mock import patch
    with patch.object(sched, "run_account_checkin", side_effect=fake_checkin):
        summary = sched.run_checkin("manual")

    assert called == ["on"], f"禁用账号不应进入队列, got {called}"
    assert summary["accounts"] == 1
    db._local.conn = None


def test_checkin_run_accounts_api(client):
    """POST /api/checkin/run-accounts 应启动后台线程并返回 ok。"""
    db = client.app.state  # noqa
    import app.database as db_mod
    a1 = db_mod.add_account({"name": "T1", "cookie_raw": "SUB=t1", "enabled": 1})
    r = client.post("/api/checkin/run-accounts", json={"account_ids": [a1]})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["count"] == 1

    # 空列表应 400
    r2 = client.post("/api/checkin/run-accounts", json={"account_ids": []})
    assert r2.status_code == 400


# ========================= v7.0 =========================

def test_hitokoto_fallback_offline(monkeypatch):
    """无外网时每日一言应回退内置句库，且不抛异常。"""
    from app import hitokoto

    hitokoto.clear_cache()

    def boom(*a, **kw):
        raise OSError("no network")

    monkeypatch.setattr(hitokoto.requests, "get", boom)
    text, source = hitokoto.fetch_quote(use_cache=False)
    assert text, "回退句库应返回非空文本"
    assert isinstance(source, str)
    line = hitokoto.format_quote(text, source)
    assert "每日一言" in line and text in line


def test_hitokoto_uses_api_when_available(monkeypatch):
    """接口可用时应使用接口返回内容。"""
    from app import hitokoto

    hitokoto.clear_cache()

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"hitokoto": "测试句子", "from": "出处", "from_who": "作者"}

    monkeypatch.setattr(hitokoto.requests, "get", lambda *a, **kw: FakeResp())
    text, source = hitokoto.fetch_quote(use_cache=False)
    assert text == "测试句子"
    assert "作者" in source and "出处" in source
    hitokoto.clear_cache()


def test_quote_api(client):
    """GET /api/quote 应返回每日一言。"""
    r = client.get("/api/quote")
    assert r.status_code == 200
    data = r.json()
    assert data["text"]


def test_log_trend_api(client):
    """GET /api/logs/trend 应返回按天的固定长度序列。"""
    r = client.get("/api/logs/trend?days=7")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 7
    assert {"day", "label", "runs", "success", "fail"} <= set(data[0])


def test_log_stats_extra_fields(client):
    """日志统计应新增累计超话数、成功率、最近时间。"""
    import app.database as db_mod
    db_mod.add_log({
        "account_name": "T", "task_id": "t1", "status": "success",
        "total": 3, "success": 3, "fail": 0, "detail": [], "message": "ok",
    })
    r = client.get("/api/logs/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["topics_signed"] >= 3
    assert data["success_rate"] > 0
    assert data["last_log_at"]


def test_clear_and_purge_logs(client):
    """清空日志与按天清理接口应可用。"""
    import app.database as db_mod
    db_mod.add_log({
        "account_name": "T", "task_id": "t2", "status": "success",
        "total": 1, "success": 1, "fail": 0, "detail": [], "message": "ok",
    })
    r = client.post("/api/logs/purge?days=0")
    assert r.status_code == 200
    assert r.json()["removed"] == 0

    r2 = client.delete("/api/logs")
    assert r2.status_code == 200
    assert r2.json()["removed"] >= 1
    assert client.get("/api/logs").json() == []


def test_purge_old_logs_removes_only_old(tmp_path, monkeypatch):
    """purge_old_logs 只删除超过保留期的日志。"""
    import app.database as db
    from datetime import datetime, timedelta

    db_path = tmp_path / "test_purge.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()

    new_id = db.add_log({"account_name": "new", "task_id": "n", "status": "success",
                         "total": 1, "success": 1, "fail": 0, "detail": [], "message": ""})
    old_id = db.add_log({"account_name": "old", "task_id": "o", "status": "success",
                         "total": 1, "success": 1, "fail": 0, "detail": [], "message": ""})
    old_time = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    conn = db._get_conn()
    conn.execute("UPDATE checkin_logs SET created_at = ? WHERE id = ?", (old_time, old_id))
    conn.commit()

    removed = db.purge_old_logs(30)
    assert removed == 1
    remaining = [l["id"] for l in db.get_logs(10)]
    assert new_id in remaining and old_id not in remaining
    db._local.conn = None


def test_report_includes_quote_and_respects_only_on_change(tmp_path, monkeypatch):
    """报告应可附带每日一言；仅异常推送模式下全成功时跳过。"""
    import app.database as db
    from app import notifier

    db_path = tmp_path / "test_notify.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.set_settings({"tg_enabled": "1", "tg_bot_token": "x", "tg_user_id": "1",
                     "tg_quote_enabled": "1", "tg_only_on_change": "0"})

    sent = {}

    def fake_send(text, title="微博签到"):
        sent["text"] = text
        sent["title"] = title
        return True

    monkeypatch.setattr(notifier, "send_telegram", fake_send)
    summary = {"time": "2026-08-17 07:00:00", "accounts": 1, "total": 2,
               "success": 2, "fail": 0, "trigger_type": "schedule",
               "detail": [{"name": "A", "status": "success",
                           "results": [{"name": "超话1", "success": True, "message": "签到成功"}]}]}
    assert notifier.send_checkin_report(summary) is True
    assert "每日一言" in sent["text"]
    assert "成功率" in sent["text"]

    # 仅异常推送：全成功应跳过
    db.set_settings({"tg_only_on_change": "1"})
    sent.clear()
    assert notifier.send_checkin_report(summary) is False
    assert not sent

    # 有失败时仍会推送
    bad = dict(summary, fail=1)
    assert notifier.send_checkin_report(bad) is True
    assert sent["title"].startswith("⚠️")
    db._local.conn = None


def test_reload_schedule_keeps_maintenance_jobs(tmp_path, monkeypatch):
    """reload_schedule 不能误删 browser_sweep / log_purge 等维护任务。"""
    import app.database as db
    import app.scheduler as sched

    db_path = tmp_path / "test_sched.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.set_settings({"schedule_enabled": "1", "schedule_cron": "0 7 * * *"})

    for jid in ("weibo_checkin", "browser_sweep", "log_purge"):
        job = sched.scheduler.get_job(jid)
        if job:
            job.remove()
    sched.scheduler.add_job(lambda: None, trigger="interval", minutes=3,
                            id="browser_sweep", max_instances=1, coalesce=True)
    sched.scheduler.add_job(lambda: None, trigger="interval", hours=24,
                            id="log_purge", max_instances=1, coalesce=True)

    sched.reload_schedule()

    assert sched.scheduler.get_job("browser_sweep") is not None
    assert sched.scheduler.get_job("log_purge") is not None
    assert sched.scheduler.get_job("weibo_checkin") is not None
    db._local.conn = None


def test_old_logs_channel_is_masked(tmp_path, monkeypatch):
    """历史日志里存的完整代理 URL 不能在读取时泄漏凭据。"""
    import app.database as db

    db_path = tmp_path / "test_mask.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()

    db.add_log({
        "account_name": "old", "task_id": "old1", "status": "success",
        "channel": "socks[socks5://user:secretpass@1.2.3.4:1080]",
        "total": 1, "success": 1, "fail": 0, "detail": [], "message": "ok",
    })
    db.add_log({
        "account_name": "old2", "task_id": "old2", "status": "success",
        "channel": "direct[__direct__]",
        "total": 1, "success": 1, "fail": 0, "detail": [], "message": "ok",
    })

    logs = db.get_logs(10)
    channels = [l["channel"] for l in logs]
    assert "SOCKS5 代理" in channels and "直连" in channels
    joined = " ".join(channels)
    assert "secretpass" not in joined and "@" not in joined

    grouped = db.get_logs_grouped(10)
    flat = [a["channel"] for day in grouped for g in day["groups"] for a in g["accounts"]]
    assert all("secretpass" not in c for c in flat)
    db._local.conn = None


# ===================== v7.1：代理凭据不出网 + 定时倒计时 =====================

@pytest.fixture()
def no_geo(monkeypatch):
    """屏蔽归属地识别的真实网络请求（沙箱无外网时会超时拖慢测试）。"""
    import app.proxy_geo as proxy_geo
    monkeypatch.setattr(proxy_geo, "detect",
                        lambda url, use_cache=True: {"ok": False, "message": "skipped"})
    yield


def test_proxy_api_never_leaks_credentials(client, no_geo):
    """代理列表/详情接口不得返回明文密码或完整含认证的链接。"""
    r = client.post("/api/proxies", json={
        "url": "socks5://puser:supersecret@9.9.9.9:1080", "label": "测试节点",
    })
    assert r.status_code == 200
    body = r.json()
    assert "supersecret" not in str(body)
    assert body["password"] == "***"
    assert body["url"] == "socks5://***@9.9.9.9:1080"
    assert body["has_auth"] is True

    lst = client.get("/api/proxies").json()
    assert "supersecret" not in str(lst)


def test_proxy_update_keeps_password_when_link_has_no_auth(client, no_geo):
    """只改 label 或提交不带认证的链接时，不能把已有密码洗掉。"""
    pid = client.post("/api/proxies", json={
        "url": "socks5://puser:supersecret@9.9.9.9:1080",
    }).json()["id"]

    # 前端回填的是打码密码，不应写入库
    client.put(f"/api/proxies/{pid}", json={"password": "***", "label": "改名"})
    assert db.get_proxy(pid)["password"] == "supersecret"

    # 提交不带认证的链接，认证信息保留
    client.put(f"/api/proxies/{pid}", json={"url": "socks5://9.9.9.9:1080"})
    p = db.get_proxy(pid)
    assert p["password"] == "supersecret" and p["username"] == "puser"


def test_account_binds_proxy_by_id_without_exposing_url(client, no_geo):
    """账号按 proxy_id 绑定代理；对外只给打码链接与可读名称。"""
    pid = client.post("/api/proxies", json={
        "url": "socks5://auser:apass@8.8.8.8:1080", "label": "东京节点",
    }).json()["id"]

    acc = client.post("/api/accounts", json={
        "name": "A", "cookie_raw": "SUB=x", "proxy_id": pid,
    }).json()
    assert acc["proxy_id"] == pid
    assert acc["proxy_label"] == "东京节点"
    assert "apass" not in str(acc)
    assert acc["proxy"] == "socks5://***@8.8.8.8:1080"

    # 服务端仍存真实链接，签到才能用
    assert db.get_account(acc["id"])["proxy"] == "socks5://auser:apass@8.8.8.8:1080"

    # 打码值回传不应破坏已存链接
    client.put(f"/api/accounts/{acc['id']}", json={"proxy": acc["proxy"]})
    assert db.get_account(acc["id"])["proxy"] == "socks5://auser:apass@8.8.8.8:1080"

    # proxy_id=0 → 改为直连
    client.put(f"/api/accounts/{acc['id']}", json={"proxy_id": 0})
    assert db.get_account(acc["id"])["proxy"] == ""

    # 不存在的代理 id → 400
    assert client.put(f"/api/accounts/{acc['id']}",
                      json={"proxy_id": 99999}).status_code == 400


def test_mask_proxy_url_helper():
    assert db.mask_proxy_url("") == ""
    assert db.mask_proxy_url("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert db.mask_proxy_url("socks5://u:p@1.2.3.4:1080") == "socks5://***@1.2.3.4:1080"
    assert "p@" not in db.mask_proxy_url("socks5://u:p@1.2.3.4:1080")


def test_schedule_next_api(client):
    """下次定时签到接口：开启时给 cron，关闭时 enabled=False。"""
    client.post("/api/settings", json={"schedule_enabled": "1",
                                       "schedule_cron": "0 7 * * *"})
    client.post("/api/settings/reload-schedule")
    info = client.get("/api/schedule/next").json()
    assert info["enabled"] is True
    assert info["cron"] == "0 7 * * *"

    client.post("/api/settings", json={"schedule_enabled": "0"})
    info = client.get("/api/schedule/next").json()
    assert info["enabled"] is False
    assert info["next_run"] is None


# ===================== v7.2：登录限流 + Secure Cookie + 归属地 =====================

def test_login_rate_limit_locks_after_repeated_failures(client, monkeypatch):
    """连续失败达到阈值后应返回 429，而不是无限允许爆破。"""
    import app.auth as auth_mod
    auth_mod.reset_login_limits()

    codes = []
    for _ in range(auth_mod.LOGIN_MAX_FAILS + 2):
        r = client.post("/api/auth/login",
                        json={"username": "admin", "password": BAD_PW})
        codes.append(r.status_code)

    assert 401 in codes, codes
    assert 429 in codes, codes
    # 锁定后即使密码正确也要被拦住
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": GOOD_PW})
    assert r.status_code == 429
    assert r.headers.get("Retry-After")

    # 清理，避免影响其他测试
    auth_mod.reset_login_limits()


def test_login_success_resets_failure_counter(tmp_path, monkeypatch):
    """成功登录后失败计数清零，不会因为之前手滑几次就被锁。"""
    import app.auth as auth_mod
    db_path = tmp_path / "test_rl.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.create_user("admin", auth_mod.hash_password(GOOD_PW))
    db.set_settings({"auth_enabled": "1"})
    auth_mod.reset_login_limits()

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        # 失败 2 次后成功一次
        for _ in range(2):
            assert c.post("/api/auth/login",
                          json={"username": "admin", "password": BAD_PW}).status_code == 401
        assert c.post("/api/auth/login",
                      json={"username": "admin", "password": GOOD_PW}).status_code == 200
        # 计数已清零：再失败到阈值-1 次仍应是 401 而非 429
        for _ in range(auth_mod.LOGIN_MAX_FAILS - 1):
            assert c.post("/api/auth/login",
                          json={"username": "admin", "password": BAD_PW}).status_code == 401
    auth_mod.reset_login_limits()
    db._local.conn = None


def test_cookie_secure_flag_follows_forwarded_proto(tmp_path, monkeypatch):
    """可信反代（WCM_TRUST_PROXY=1）给出 X-Forwarded-Proto: https 时
    Cookie 必须带 Secure；未声明可信时不得采信该头；纯 HTTP 不能带。"""
    import app.auth as auth_mod
    monkeypatch.setenv("WCM_TRUST_PROXY", "1")
    db_path = tmp_path / "test_secure.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.create_user("admin", auth_mod.hash_password(GOOD_PW))
    db.set_settings({"auth_enabled": "1"})
    auth_mod.reset_login_limits()

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PW},
                   headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 200
        assert "secure" in r.headers.get("set-cookie", "").lower()
        assert "httponly" in r.headers.get("set-cookie", "").lower()

        r = c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PW})
        assert r.status_code == 200
        assert "secure" not in r.headers.get("set-cookie", "").lower()
    auth_mod.reset_login_limits()
    db._local.conn = None


def test_forwarded_headers_distrusted_by_default(tmp_path, monkeypatch):
    """未设置 WCM_TRUST_PROXY 时（默认直连），伪造 XFF/X-Forwarded-Proto 不生效：
    Secure 不会被误加，登录限流也不因伪造 XFF 而分流。"""
    import app.auth as auth_mod
    monkeypatch.delenv("WCM_TRUST_PROXY", raising=False)
    db_path = tmp_path / "test_distrust.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.create_user("admin", auth_mod.hash_password(GOOD_PW))
    db.set_settings({"auth_enabled": "1"})
    auth_mod.reset_login_limits()

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PW},
                   headers={"X-Forwarded-Proto": "https",
                            "X-Forwarded-For": "1.2.3.4"})
        assert r.status_code == 200
        assert "secure" not in r.headers.get("set-cookie", "").lower()
    auth_mod.reset_login_limits()
    db._local.conn = None


def test_is_secure_request_helper(monkeypatch):
    import app.auth as auth_mod

    class _Req:
        def __init__(self, headers, scheme="http"):
            self.headers = headers
            self.url = type("U", (), {"scheme": scheme})()

    monkeypatch.setenv("WCM_TRUST_PROXY", "1")
    assert auth_mod.is_secure_request(_Req({"x-forwarded-proto": "https"})) is True
    assert auth_mod.is_secure_request(_Req({"x-forwarded-proto": "https, http"})) is True
    assert auth_mod.is_secure_request(_Req({"x-forwarded-proto": "http"})) is False
    assert auth_mod.is_secure_request(_Req({"x-forwarded-ssl": "on"})) is True
    monkeypatch.delenv("WCM_TRUST_PROXY")
    assert auth_mod.is_secure_request(_Req({"x-forwarded-proto": "https"})) is False
    assert auth_mod.is_secure_request(_Req({}, scheme="https")) is True
    assert auth_mod.is_secure_request(_Req({})) is False


def test_geo_region_and_country_normalized():
    """不同接口的地名要归一成同一种中文写法。"""
    from app import proxy_geo

    assert proxy_geo._clean_region("Jiangsu Sheng") == "江苏"
    assert proxy_geo._clean_region("Jiangsu") == "江苏"
    assert proxy_geo._clean_region("江苏省") == "江苏"
    assert proxy_geo._zh_country("China") == "中国"
    assert proxy_geo._zh_country("", "CN") == "中国"
    assert proxy_geo._zh_country("", "HK") == "中国香港"

    # ipwho 与 ipinfo 两种原始返回应产出一致的国家/地区
    svc = {s["name"]: s for s in proxy_geo.GEO_SERVICES}
    a = svc["ipwho"]["parse"]({"success": True, "ip": "1.2.3.4", "country": "China",
                               "country_code": "CN", "region": "Jiangsu Sheng",
                               "city": "Nanjing"})
    b = svc["ipinfo"]["parse"]({"ip": "1.2.3.4", "country": "CN",
                                "region": "Jiangsu", "city": "Nanjing"})
    assert a["country"] == b["country"] == "中国"
    assert a["region"] == b["region"] == "江苏"


def test_geo_https_services_come_first():
    """HTTPS 接口必须排在明文 HTTP 之前（出口会劫持明文 HTTP）。"""
    from app import proxy_geo
    urls = [s["url"] for s in proxy_geo.GEO_SERVICES]
    assert urls[0].startswith("https://")
    http_idx = [i for i, u in enumerate(urls) if u.startswith("http://")]
    assert all(i == len(urls) - 1 for i in http_idx), urls


def test_geo_error_message_never_leaks_proxy_credentials():
    """底层异常里带完整代理 URL，翻译后的提示不能把密码带出来。"""
    from app import proxy_geo
    import requests

    exc = requests.exceptions.ProxyError(
        "SOCKSHTTPSConnectionPool(host='ipwho.is', port=443): "
        "Max retries exceeded with url: / (Caused by ProxyError('Cannot connect to proxy.', "
        "GeneralProxyError('Socket error: 0x01: General SOCKS server failure')))"
    )
    msg = proxy_geo._friendly_error(exc)
    assert "SOCKS" in msg or "代理" in msg
    assert "supersecret" not in msg

    msg2 = proxy_geo._friendly_error(Exception("socks5://user:supersecret@1.2.3.4:1080 timed out"))
    assert "supersecret" not in msg2
    assert "超时" in msg2


def test_geo_failure_cached_briefly(monkeypatch):
    """失败结果只短暂缓存，不能把一次抖动锁死一小时。"""
    from app import proxy_geo
    proxy_geo.clear_cache()
    calls = []

    def fake_detect(url):
        calls.append(url)
        return {"ok": False, "message": "连接失败（代理不可用）"}

    monkeypatch.setattr(proxy_geo, "_detect", fake_detect)
    u = "socks5://x:y@1.2.3.4:1080"
    proxy_geo.detect(u)
    proxy_geo.detect(u)
    assert len(calls) == 1                      # 30s 内命中缓存

    # 把时间戳往前拨过 _FAIL_CACHE_TTL，应重新探测
    proxy_geo._cache[u]["_t"] -= proxy_geo._FAIL_CACHE_TTL + 1
    proxy_geo.detect(u)
    assert len(calls) == 2
    proxy_geo.clear_cache()


def test_detect_endpoint_accepts_proxy_id(client, no_geo, monkeypatch):
    """编辑已有代理时前端拿不到密码，应能用 proxy_id 让服务端拿真实链接探测。"""
    from app import proxy_geo

    pid = client.post("/api/proxies", json={
        "url": "socks5://guser:gpass@7.7.7.7:1080",
    }).json()["id"]

    seen = {}

    def fake_detect(url, use_cache=True, force=False):
        seen["url"] = url
        return {"ok": True, "ip": "7.7.7.7", "country": "中国", "country_code": "CN",
                "region": "江苏", "city": "南京", "message": "success",
                "latency_ms": 12, "_t": 1.0}

    monkeypatch.setattr(proxy_geo, "detect", fake_detect)

    r = client.post("/api/proxies/detect", json={"proxy_id": pid})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["country"] == "中国"
    assert "_t" not in body                     # 内部缓存戳不外泄
    assert seen["url"] == "socks5://guser:gpass@7.7.7.7:1080"

    # 打码链接不应被当成真链接去拨号
    r = client.post("/api/proxies/detect", json={"url": "socks5://***@7.7.7.7:1080"})
    assert r.status_code == 400


# ================== Cookie 保活与换发回写 ==================

def _keepalive_env(tmp_path, monkeypatch, cookie="SUB=old; UOR=x"):
    """独立临时库 + 一个账号，返回 (scheduler 模块, 账号 id)。"""
    import app.database as local_db
    import app.scheduler as sched

    monkeypatch.setattr(local_db, "DB_PATH", tmp_path / "keepalive.db")
    local_db._local.conn = None
    local_db.init_db()
    local_db.set_settings({"auth_enabled": "0", "anti_ban_enabled": "0",
                           "keepalive_gap_seconds": "0", "tg_enabled": "0"})
    acc_id = local_db.add_account({"name": "保活号", "cookie": cookie})
    return sched, local_db, acc_id


def _jar_session(pairs):
    class _C:
        def __init__(self, name, value):
            self.name, self.value = name, value

    class _S:
        def __init__(self):
            self.cookies = [_C(n, v) for n, v in pairs]

    return _S()


def test_writeback_persists_newly_issued_login_key(tmp_path, monkeypatch):
    """回归根因：微博补发 SCF（同名 key 都没变）也必须落库，否则缺的凭据永远补不回。"""
    from unittest.mock import patch
    from app.weibo_client import _bundle

    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch)

    def fake_checkin(cookie, opts, proxy_url=None, proxy_index=0):
        merged = dict(cookie)
        merged["SCF"] = "issued"          # 只新增，SUB 值没变
        return _bundle("success", "ok", 1, 1, 0,
                       [{"name": "超话", "success": True, "message": "已签到"}],
                       merged, [], "direct", cookie_added=["SCF"])

    with patch.object(sched, "run_account_checkin", side_effect=fake_checkin):
        summary = sched.run_checkin("manual")

    assert summary["status"] == "success"
    stored = local_db.get_account(acc_id)["cookie"]
    assert "SCF=issued" in stored, stored
    acc = local_db.get_account(acc_id)
    assert "SCF" in acc["cookie_last_rotate_keys"]
    assert acc["cookie_last_rotated_at"]
    assert acc["cookie_last_rotate_source"] == "checkin"
    assert acc["cookie_health"] in ("ok", "unknown", "expiring", "expired")
    local_db._local.conn = None


def test_writeback_refuses_losing_login_keys(tmp_path, monkeypatch):
    """脏响应把 SUB 洗掉了，绝不能覆盖库里的完整凭据。"""
    from unittest.mock import patch
    from app.weibo_client import _bundle

    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch,
                                             cookie="SUB=old; SCF=c")

    def fake_checkin(cookie, opts, proxy_url=None, proxy_index=0):
        return _bundle("success", "ok", 1, 1, 0,
                       [{"name": "超话", "success": True, "message": "已签到"}],
                       {"SUB": "", "SCF": "c", "junk": "y"}, ["SCF"], "direct")

    with patch.object(sched, "run_account_checkin", side_effect=fake_checkin):
        sched.run_checkin("manual")

    stored = local_db.get_account(acc_id)["cookie"]
    assert "SUB=old" in stored, stored
    assert "junk=y" not in stored
    local_db._local.conn = None


def test_cookie_invalid_stops_keepalive_without_overwriting(tmp_path, monkeypatch):
    from unittest.mock import patch
    from app.weibo_client import _bundle

    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch,
                                             cookie="SUB=old; SCF=c")

    def fake_checkin(cookie, opts, proxy_url=None, proxy_index=0):
        return _bundle("failed", "Cookie 无效或已过期", 0, 0, 0, [],
                       dict(cookie), [], "direct", failure_type="cookie_invalid")

    with patch.object(sched, "run_account_checkin", side_effect=fake_checkin):
        summary = sched.run_checkin("manual")

    acc = local_db.get_account(acc_id)
    assert acc["cookie_health"] == "invalid"
    assert acc["cookie_next_keepalive_at"] == ""
    assert acc["cookie"] == "SUB=old; SCF=c"
    assert summary["detail"][0]["failure_type"] == "cookie_invalid"
    local_db._local.conn = None


def test_keepalive_rotates_cookie_and_reschedules(tmp_path, monkeypatch):
    from unittest.mock import patch

    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch)
    session = _jar_session([("SUB", "rotated"), ("SUBP", "p1")])

    with patch.object(sched, "verify_cookie", return_value=(True, "st1")), \
         patch.object(sched, "new_account_session", return_value=session), \
         patch.object(sched, "get_followed_topics", return_value=[]):
        stats = sched.run_keepalive_once([acc_id], force=True)

    assert stats["checked"] == 1 and stats["rotated"] == 1
    acc = local_db.get_account(acc_id)
    assert acc["cookie"].startswith("SUB=rotated")
    assert "SUBP=p1" in acc["cookie"]
    assert acc["cookie_health"] in ("ok", "expiring", "expired")
    assert acc["cookie_next_keepalive_at"]
    assert acc["cookie_fail_streak"] == 0
    assert acc["cookie_last_rotate_source"].startswith("keepalive")
    local_db._local.conn = None


def test_keepalive_interval_tightens_and_jitters_near_expiry(tmp_path, monkeypatch):
    """临近预计失效：间隔自动加密到 urgent 以内，且排期带随机抖动。"""
    sched, local_db, acc_id = _keepalive_env(
        tmp_path, monkeypatch, cookie="SUB=old; SCF=c; ALF=4102444800")  # 2100 年
    local_db.set_cookie_state(acc_id, cookie_issued_at="2020-01-01 00:00:00")
    acc = local_db.get_account(acc_id)

    assert sched.cookie_expiry_view(acc)["state"] in ("expiring", "expired")
    hours = sched.keepalive_interval_hours(acc)
    assert 1.0 <= hours <= 6.0
    jittered = sched._jitter_hours(hours)
    assert 0.85 * hours <= jittered <= 1.2 * hours

    scheduled = sched._next_keepalive_at(hours, urgent=True)
    parsed = __import__("datetime").datetime.strptime(scheduled, "%Y-%m-%d %H:%M:%S")
    delta_hours = (parsed - __import__("datetime").datetime.now()).total_seconds() / 3600
    assert 0.8 * hours <= delta_hours <= 1.25 * hours + 0.01
    local_db._local.conn = None


def test_quiet_hours_shift_scheduled_keepalive(tmp_path, monkeypatch):
    """深夜静默窗：命中的排期挪到清晨并加随机分钟；白天不动；off 关闭。"""
    from datetime import datetime

    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch)
    assert sched._quiet_window() == (0, 6)

    night = datetime(2026, 1, 1, 2, 30)
    moved = sched._shift_out_of_quiet_hours(night)
    assert datetime(2026, 1, 1, 6, 0) <= moved <= datetime(2026, 1, 1, 8, 0)

    day = datetime(2026, 1, 1, 12, 0)
    assert sched._shift_out_of_quiet_hours(day) == day

    edge = datetime(2026, 1, 1, 6, 0)          # 窗口边界外，不动
    assert sched._shift_out_of_quiet_hours(edge) == edge

    local_db.set_settings({"keepalive_quiet_hours": "off"})
    assert sched._quiet_window() is None
    local_db._local.conn = None


def test_urgent_keepalive_ignores_quiet_hours(tmp_path, monkeypatch):
    """抢救期续命优先于作息伪装：urgent=True 不挪出静默窗。"""
    from datetime import datetime

    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch)
    local_db.set_settings({"keepalive_quiet_hours": "0-23"})  # 极端全天静默
    base = datetime(2026, 1, 1, 3, 0)
    urgent_at = datetime.strptime(
        sched._next_keepalive_at(6, base, urgent=True), "%Y-%m-%d %H:%M:%S")
    # 6h × [0.85, 1.2] 抖动 → 最晚 10:12，绝不能等到 23 点窗口结束后
    assert urgent_at <= datetime(2026, 1, 1, 10, 15)
    local_db._local.conn = None


def test_rotate_source_resets_on_new_cookie(tmp_path, monkeypatch):
    """手工换新 cookie 后，上一份的换发来源必须清零。"""
    from unittest.mock import patch

    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch)
    session = _jar_session([("SUB", "rotated"), ("SUBP", "p1")])
    with patch.object(sched, "verify_cookie", return_value=(True, "st1")), \
         patch.object(sched, "new_account_session", return_value=session), \
         patch.object(sched, "get_followed_topics", return_value=[]):
        sched.run_keepalive_once([acc_id], force=True)
    assert local_db.get_account(acc_id)["cookie_last_rotate_source"]

    local_db.on_cookie_replaced(acc_id, "SUB=new; SCF=c")
    acc = local_db.get_account(acc_id)
    assert acc["cookie_last_rotate_source"] == ""
    assert acc["cookie_last_rotated_at"] == ""
    local_db._local.conn = None


def test_keepalive_business_endpoint_forced_after_gap(tmp_path, monkeypatch):
    """随机不中也没关系：隔满 N 轮必须强制掺一次真实业务端点。"""
    from unittest.mock import patch

    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch)
    sched._keepalive_business_gap[acc_id] = sched._KEEPALIVE_LIST_EVERY

    with patch.object(sched, "verify_cookie", return_value=(True, "st")), \
         patch.object(sched, "new_account_session", return_value=_jar_session([])), \
         patch.object(sched, "get_followed_topics", return_value=[]) as spy, \
         patch.object(sched.random, "random", return_value=0.99):
        sched.run_keepalive_once([acc_id], force=True)

    assert spy.called
    assert sched._keepalive_business_gap[acc_id] == 0
    local_db._local.conn = None


def test_keepalive_business_endpoint_forced_when_expiring(tmp_path, monkeypatch):
    """临期账号每轮都掺业务端点：SUB 换发多发生在业务接口。"""
    from unittest.mock import patch

    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch,
                                             cookie="SUB=old; SCF=c; ALF=4102444800")
    local_db.set_cookie_state(acc_id, cookie_issued_at="2020-01-01 00:00:00")
    sched._keepalive_business_gap[acc_id] = 0

    with patch.object(sched, "verify_cookie", return_value=(True, "st")), \
         patch.object(sched, "new_account_session", return_value=_jar_session([])), \
         patch.object(sched, "get_followed_topics", return_value=[]) as spy, \
         patch.object(sched.random, "random", return_value=0.99):
        sched.run_keepalive_once([acc_id], force=True)

    assert spy.called
    local_db._local.conn = None


def test_keepalive_business_endpoint_gap_advances_on_miss(tmp_path, monkeypatch):
    """随机未中时记一笔轮次，保证最多 N 轮内一定补上业务请求。"""
    from unittest.mock import patch

    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch)
    sched._keepalive_business_gap[acc_id] = 0

    with patch.object(sched, "verify_cookie", return_value=(True, "st")), \
         patch.object(sched, "new_account_session", return_value=_jar_session([])), \
         patch.object(sched, "get_followed_topics", return_value=[]) as spy, \
         patch.object(sched.random, "random", return_value=0.99):
        sched.run_keepalive_once([acc_id], force=True)

    assert spy.called is False
    assert sched._keepalive_business_gap[acc_id] == 1
    local_db._local.conn = None


def test_keepalive_network_failure_backs_off_without_killing_account(tmp_path, monkeypatch):
    from unittest.mock import patch

    from app.weibo_client import NetworkError
    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch)

    def boom(*a, **kw):
        raise NetworkError("代理不通")

    with patch.object(sched, "verify_cookie", side_effect=boom), \
         patch.object(sched, "new_account_session", return_value=_jar_session([])):
        stats = sched.run_keepalive_once([acc_id], force=True)

    acc = local_db.get_account(acc_id)
    assert stats["network"] == 1
    assert acc["cookie_health"] == "network_error"
    assert acc["cookie_fail_streak"] == 1
    assert acc["cookie"] == "SUB=old; UOR=x"       # 网络故障不能改 cookie
    assert acc["cookie_next_keepalive_at"]          # 仍在退避后重试
    local_db._local.conn = None


def test_keepalive_marks_dead_and_reports(tmp_path, monkeypatch):
    from unittest.mock import patch

    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch)
    local_db.set_cookie_state(acc_id, cookie_issued_at="2020-01-01 00:00:00")
    local_db.set_settings({"tg_enabled": "1"})
    sent = []
    with patch.object(sched, "verify_cookie", return_value=(False, None)), \
         patch.object(sched, "new_account_session", return_value=_jar_session([])), \
         patch.object(sched.notifier, "send_cookie_warning",
                      side_effect=lambda items: sent.append(items)):
        stats = sched.run_keepalive_once([acc_id])

    assert stats["invalid"] == 1
    assert local_db.get_account(acc_id)["cookie_health"] == "invalid"
    assert sent and sent[0][0]["health"] == "invalid"
    local_db._local.conn = None


def test_keepalive_yields_to_running_checkin(tmp_path, monkeypatch):
    """签到进行中时保活必须让路 —— 两边同 IP 叠认证请求最容易触发风控。"""
    import threading
    from unittest.mock import patch

    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch)
    local_db.set_cookie_state(acc_id, cookie_next_keepalive_at="2020-01-01 00:00:00")
    held = threading.Lock()
    held.acquire()
    with patch.object(sched, "_running_lock", held), \
         patch.object(sched, "run_keepalive_once") as spy:
        sched._keepalive_job()
    assert spy.called is False
    local_db._local.conn = None


def test_keepalive_disabled_by_setting(tmp_path, monkeypatch):
    from unittest.mock import patch

    sched, local_db, acc_id = _keepalive_env(tmp_path, monkeypatch)
    local_db.set_settings({"keepalive_enabled": "0"})
    local_db.set_cookie_state(acc_id, cookie_next_keepalive_at="2020-01-01 00:00:00")
    with patch.object(sched, "run_keepalive_once") as spy:
        sched._keepalive_job()
    assert spy.called is False
    local_db._local.conn = None


def test_account_exception_does_not_abort_batch(tmp_path, monkeypatch):
    """L0 回归：单个账号抛异常，其余账号照常签到。"""
    from unittest.mock import patch

    from app.weibo_client import _bundle

    import app.database as local_db
    import app.scheduler as sched_module
    monkeypatch.setattr(local_db, "DB_PATH", tmp_path / "isolate.db")
    local_db._local.conn = None
    local_db.init_db()
    local_db.set_settings({"auth_enabled": "0", "anti_ban_enabled": "0", "tg_enabled": "0"})
    [local_db.add_account({"name": f"号{i}", "cookie": "SUB=s%d; SCF=c" % i})
     for i in range(3)]

    def flaky(cookie, opts, proxy_url=None, proxy_index=0):
        if "s1" in str(cookie):
            raise RuntimeError("模拟微博接口炸了")
        return _bundle("success", "ok", 1, 1, 0,
                       [{"name": "超话", "success": True, "message": "已签到"}],
                       dict(cookie), [], "direct")

    with patch.object(sched_module, "run_account_checkin", side_effect=flaky):
        summary = sched_module.run_checkin("manual")

    assert summary["accounts"] == 3, summary
    statuses = {e["name"]: e["status"] for e in summary["detail"]}
    assert len(statuses) == 3
    assert any(e.get("failure_type") == "internal_error" for e in summary["detail"])
    assert summary["success"] == 2
    local_db._local.conn = None


def test_topics_list_error_is_not_cookie_invalid(tmp_path, monkeypatch):
    """列表接口抖动应归到 topics_list_error，不能误判成 cookie 失效催重登。"""
    from unittest.mock import patch

    import app.weibo_client as client
    import app.database as local_db
    from app.weibo_client import CheckinOptions

    monkeypatch.setattr(local_db, "DB_PATH", tmp_path / "topics_err.db")
    local_db._local.conn = None
    local_db.init_db()

    opts = CheckinOptions(checkin_delay_min=0, checkin_delay_max=0)
    with patch.object(client, "verify_cookie", return_value=(True, "st")), \
         patch.object(client, "get_followed_topics",
                      side_effect=RuntimeError("微博未能返回关注超话列表：500")):
        result = client.run_account_checkin({"SUB": "s", "SCF": "c"}, opts)

    assert result["failure_type"] == "topics_list_error"
    assert result["status"] == "failed"
    local_db._local.conn = None


def test_topics_list_expired_maps_to_cookie_invalid(tmp_path, monkeypatch):
    from unittest.mock import patch

    import app.weibo_client as client
    from app.weibo_client import CheckinOptions, CookieExpiredError

    opts = CheckinOptions(checkin_delay_min=0, checkin_delay_max=0)
    with patch.object(client, "verify_cookie", return_value=(True, "st")), \
         patch.object(client, "get_followed_topics",
                      side_effect=CookieExpiredError("ok=-100")):
        result = client.run_account_checkin({"SUB": "s", "SCF": "c"}, opts)

    assert result["failure_type"] == "cookie_invalid"


def test_keepalive_all_and_single_endpoints(client, monkeypatch):
    """新端点可用且不会真的出网（verify_cookie 被 mock）。"""
    from unittest.mock import patch

    import app.scheduler as sched

    acc_id = client.post("/api/accounts", json={
        "name": "保活号", "cookie_raw": "SUB=s; SCF=c",
    }).json()["id"]
    with patch.object(sched, "verify_cookie", return_value=(True, "st")), \
         patch.object(sched, "new_account_session",
                      return_value=_jar_session([("SUB", "s2")])), \
         patch.object(sched, "get_followed_topics", return_value=[]):
        r = client.post(f"/api/accounts/{acc_id}/keepalive")
        assert r.status_code == 200
        assert r.json()["checked"] == 1
        assert r.json()["rotated"] == 1
        r2 = client.post("/api/accounts/keepalive-all", json={"account_ids": [acc_id]})
        assert r2.status_code == 200
        # 库里已是 s2，同样的凭证不再产生换发，但账号仍判有效
        assert r2.json()["checked"] == 1 and r2.json()["rotated"] == 0
        assert r2.json()["alive"] == 1


def test_verify_endpoint_returns_expiry_and_health(client, monkeypatch):
    from unittest.mock import patch

    import app.weibo_client as client_mod

    acc_id = client.post("/api/accounts", json={
        "name": "校验号", "cookie_raw": "SUB=s; SCF=c; ALF=1893456000",
    }).json()["id"]
    with patch.object(client_mod, "verify_cookie", return_value=(True, "st")), \
         patch.object(client_mod, "new_account_session",
                      return_value=_jar_session([("SUB", "s")])):
        body = client.post(f"/api/accounts/{acc_id}/verify").json()

    assert body["valid"] is True, body
    assert body["cookie_health"] in ("ok", "expiring", "expired", "unknown")
    listed = client.get("/api/accounts").json()[0]
    assert listed["cookie_expiry"]["alf_expires_at"].startswith("2030-")
    assert listed["cookie_health"]


def test_settings_keepalive_keys_are_writable(client):
    """新设置 key 必须同时进白名单，否则"改了没生效"。"""
    r = client.post("/api/settings", json={
        "keepalive_enabled": "0", "keepalive_interval_hours": "6",
        "keepalive_urgent_interval_hours": "4",
        "keepalive_quiet_hours": "1-7",
        "keepalive_gap_seconds": "15", "keepalive_max_per_tick": "5",
        "cookie_session_ttl_days": "5", "cookie_expiry_warn_days": "1",
        "cookie_warn_cooldown_hours": "3", "not_a_real_key": "x",
    })
    assert r.status_code == 200
    saved = r.json()
    assert saved["keepalive_enabled"] == "0"
    assert saved["keepalive_interval_hours"] == "6"
    assert saved["keepalive_urgent_interval_hours"] == "4"
    assert saved["keepalive_quiet_hours"] == "1-7"
    assert saved["cookie_session_ttl_days"] == "5"
    assert saved["cookie_warn_cooldown_hours"] == "3"
    assert "not_a_real_key" not in saved


def test_health_exposes_cookie_stats_without_failing(client):
    """cookie 全死也只代表业务故障：/api/health 的 ok 必须仍为 True，
    否则 Docker HEALTHCHECK + restart policy 会引发重启风暴。"""
    acc_id = client.post("/api/accounts", json={
        "name": "死号", "cookie_raw": "SUB=dead",
    }).json()["id"]
    db.mark_cookie_dead(acc_id)
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["cookie"].get("invalid") == 1


def test_relogin_settings_keys_via_api(client):
    """v1.3.6 扫码续期新键必须在设置白名单里，可写可读。"""
    r = client.post("/api/settings", json={
        "cookie_relogin_stale_hours": "24",
        "panel_url": "https://panel.example.com",
        "keepalive_passport_enabled": "1",
        "keepalive_passport_urls": "https://weibo.com/",
    })
    assert r.status_code == 200
    saved = r.json()
    assert saved["cookie_relogin_stale_hours"] == "24"
    assert saved["panel_url"] == "https://panel.example.com"
    assert saved["keepalive_passport_enabled"] == "1"
    assert saved["keepalive_passport_urls"] == "https://weibo.com/"


def test_account_public_flags_needs_relogin(client):
    """_public 需给出 needs_relogin / relogin_reason。"""
    from datetime import datetime, timedelta
    acc_id = client.post("/api/accounts", json={
        "name": "临期号", "cookie": "SUB=abc; ALF=4102444800",
    }).json()["id"]

    # 刚录入的账号没有到期压力 → 不需要续期
    row = next(r for r in client.get("/api/accounts").json() if r["id"] == acc_id)
    assert row["needs_relogin"] is False
    assert row["relogin_reason"] == ""

    # 录入时间拨到 10 天前 → 预计失效已过 → 提示扫码续期
    db.set_cookie_state(acc_id, cookie_issued_at=(
        datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"))
    row = next(r for r in client.get("/api/accounts").json() if r["id"] == acc_id)
    assert row["needs_relogin"] is True
    assert row["relogin_reason"]
