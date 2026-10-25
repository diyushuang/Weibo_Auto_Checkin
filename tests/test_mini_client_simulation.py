# -*- coding: utf-8 -*-
"""mini_client（DB-backed）持续保活 + 稳定签到的长周期仿真测试。

本地起一个"仿真微博"：SUB 每 4 次认证请求换发一次（旧值 24h 宽限）、
服务端会话 7 天不活跃死亡、匿名 -100、签到成功响应。CLI 指向仿真服务器：

1. 持续保活：20 个模拟日（每 12h 保活 + 每日签到），登录态零死亡、
   换发持续回收进数据库；
2. 失效检测与恢复：8 天沉寂 → 会话死亡 → 正确判死退出码 1 →
   重导入新 cookie 自动恢复；
3. 真实冒烟：假 cookie 打真实 m.weibo.cn（无外网自动跳过）。
"""
import json
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mini_client as mc
import app.database as database
from app import weibo_client as wc

DEATH_AFTER = timedelta(days=7)
GRACE = timedelta(hours=24)
ROT_EVERY = 4


class SimState:
    def __init__(self):
        self.clock = datetime(2026, 1, 1, 8, 0, 0)
        self.sessions = {}
        self.aliases = {}
        self.authed = 0
        self.rotations = 0
        self.checkins = 0

    def register(self, sub):
        self.sessions[sub] = {"last_seen": self.clock}

    def resolve(self, sub):
        rec = self.sessions.get(sub)
        if rec is not None:
            if self.clock - rec["last_seen"] > DEATH_AFTER:
                del self.sessions[sub]
                return None
            rec["last_seen"] = self.clock
            return sub
        alias = self.aliases.get(sub)
        if alias and self.clock <= alias["until"] and alias["target"] in self.sessions:
            self.sessions[alias["target"]]["last_seen"] = self.clock
            return alias["target"]
        return None

    def touch(self, sub):
        cur = self.resolve(sub)
        if cur is None:
            return None
        self.authed += 1
        if self.authed % ROT_EVERY == 0:
            new = f"r{self.rotations + 1}_n{self.authed}"
            self.sessions[new] = {"last_seen": self.clock}
            self.aliases[cur] = {"target": new, "until": self.clock + GRACE}
            del self.sessions[cur]
            self.rotations += 1
            return new
        return cur

    def advance(self, hours):
        self.clock += timedelta(hours=hours)
        for s in [s for s, r in self.sessions.items()
                  if self.clock - r["last_seen"] > DEATH_AFTER]:
            del self.sessions[s]
        for a in [a for a, info in self.aliases.items() if self.clock > info["until"]]:
            del self.aliases[a]

    def stats(self):
        return {"clock": self.clock.strftime("%Y-%m-%d %H:%M:%S"),
                "authed": self.authed, "rotations": self.rotations,
                "checkins": self.checkins, "live_sessions": len(self.sessions)}


def _handler_factory(sim):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _sub(self):
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "SUB":
                    return v
            return ""

        def _json(self, payload, cookies=()):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            for c in cookies:
                self.send_header("Set-Cookie", c)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)

            if path == "/sim/advance":
                sim.advance(float(query.get("hours", ["0"])[0]))
                self._json(sim.stats())
                return
            if path == "/sim/register":
                sim.register(query["sub"][0])
                self._json({"ok": 1})
                return
            if path == "/sim/stats":
                self._json(sim.stats())
                return

            raw_sub = self._sub()

            if path == "/api/config":
                cur = sim.touch(raw_sub)
                if cur is None:
                    self._json({"ok": 1, "data": {"login": False, "st": ""}},
                               cookies=["MLOGIN=0; Path=/"])
                    return
                alf = int((sim.clock + timedelta(days=365)).timestamp())
                self._json({"ok": 1, "data": {"login": True, "st": f"st{sim.authed}"}},
                           cookies=[f"SUB={cur}; Path=/",
                                    f"ALF={alf}; Path=/",
                                    "MLOGIN=1; Path=/"])
                return

            cur = sim.resolve(raw_sub)
            if cur is None:
                self._json({"ok": -100, "msg": "登录态失效"})
                return

            if path == "/api/container/getIndex":
                sim.touch(raw_sub)
                cards = []
                for name, done in (("超话A", False), ("超话B", True)):
                    item = {"card_type": "8", "title_sub": name,
                            "desc": "", "member_count": 100,
                            "buttons": [{"name": "已签到" if done else "签到"}]}
                    if not done:
                        item["scheme"] = "https://m.weibo.cn/p/index"
                        item["buttons"][0]["scheme"] = "/super/checkin?topic=100808A"
                    cards.append({"card_group": [item]})
                self._json({"ok": 1, "data": {"cards": cards}})
                return

            if path == "/super/checkin":
                sim.touch(raw_sub)
                sim.checkins += 1
                self._json({"ok": 1, "msg": "签到成功", "rank": sim.checkins})
                return

            self._json({"ok": 0, "msg": "unknown endpoint"})
    return Handler


@pytest.fixture
def sim(tmp_path, monkeypatch):
    state = SimState()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setattr(wc, "BASE", base)
    monkeypatch.setattr(wc, "CONFIG_URL", base + "/api/config")
    monkeypatch.setattr(wc, "TOPICS_URL", base + "/api/container/getIndex")
    monkeypatch.setattr(wc, "validate_weibo_url", lambda url, *a, **k: url)
    # 模拟服务器在 127.0.0.1：逐跳出站守卫同样要放行本地回环（仅测试）
    from app import security as sec
    monkeypatch.setattr(sec, "validate_outbound_url",
                        lambda url, allowed_hosts=(): url)

    db = tmp_path / "sim.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database._local.conn = None
    database.init_db()
    yield state, httpd, db
    httpd.shutdown()
    httpd.server_close()
    database._local.conn = None


def _seed_settings():
    database.set_settings({"anti_ban_enabled": "0", "tg_enabled": "0",
                           "auth_enabled": "0",
                           "checkin_delay_min": "0", "checkin_delay_max": "0",
                           "keepalive_gap_seconds": "0",
                           "keepalive_quiet_hours": "off"})


def _import_account(db, cookie, name="小号A"):
    code = mc.main(["import", "--cookie", cookie, "--name", name, "--db", str(db)])
    assert code == 0
    return next(a for a in database.get_accounts() if a["name"] == name)


# ===================== 场景一：20 个模拟日持续保活 =====================

def test_simulation_sustained_keepalive_and_checkin(sim):
    state_obj, httpd, db = sim
    _seed_settings()
    state_obj.register("s0")
    acc = _import_account(db, "SUB=s0; SCF=c; ALF=4102444800")

    keep_codes, check_codes = [], []
    for _day in range(20):
        for _ in range(2):
            keep_codes.append(mc.main(["keepalive", "--force", "--db", str(db)]))
        check_codes.append(mc.main(["checkin", "--db", str(db)]))
        state_obj.advance(24)

    assert all(code == 0 for code in keep_codes), keep_codes
    assert all(code == 0 for code in check_codes), check_codes
    stats = state_obj.stats()
    assert stats["rotations"] >= 3, stats
    assert stats["checkins"] == 20, stats

    acc = database.get_account(acc["id"])
    assert acc["cookie"].startswith("SUB=r")
    assert "SCF=c" in acc["cookie"]
    assert acc["cookie"] != "SUB=s0; SCF=c; ALF=4102444800"
    assert acc["cookie_health"] != "invalid"
    assert acc["last_status"] == "success"
    assert acc["cookie_next_keepalive_at"]


# ============ 场景二：长期不活动 → 死亡检测 → 换新 cookie 自动恢复 ============

def test_simulation_expiry_detection_and_seed_recovery(sim):
    state_obj, httpd, db = sim
    _seed_settings()
    state_obj.register("s0")
    acc_file = None
    acc = _import_account(db, "SUB=s0; SCF=c")

    # 第 1 天签到正常
    assert mc.main(["checkin", "--db", str(db)]) == 0
    state_obj.advance(24)
    # 之后 8 天完全沉寂（服务端会话 7 天即死亡）
    state_obj.advance(24 * 8)
    assert mc.main(["checkin", "--db", str(db)]) == 1
    acc = database.get_account(acc["id"])
    assert acc["cookie_health"] == "invalid"
    assert "Cookie" in (acc.get("last_message") or "")

    # 重导入新 cookie（同名 → 名称匹配更新）→ 自动恢复
    state_obj.register("s1")
    acc = _import_account(db, "SUB=s1; SCF=c2")
    assert mc.main(["checkin", "--db", str(db)]) == 0
    acc = database.get_account(acc["id"])
    assert "SCF=c2" in acc["cookie"]
    assert acc["cookie_health"] != "invalid"
    assert acc["last_status"] == "success"


# ===================== 场景三：真实微博死亡检测冒烟 =====================

def test_live_weibo_detects_garbage_cookie(tmp_path, monkeypatch):
    import requests as _rq
    try:
        _rq.get("https://m.weibo.cn/api/config", timeout=8)
    except Exception:
        pytest.skip("本机无外网，跳过真实冒烟")

    db = tmp_path / "live.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database._local.conn = None
    database.init_db()
    code = mc.main(["import", "--cookie", "SUB=fake; SCF=fake; ALF=1893456000",
                    "--name", "cli", "--db", str(db)])
    assert code == 0
    code = mc.main(["keepalive", "--force", "--db", str(db)])
    acc = next(a for a in database.get_accounts() if a["name"] == "cli")
    if acc.get("cookie_health") == "invalid":
        assert code == 1                     # 真实环境正确识别无效凭据
    else:
        # 网络抖动走退避路径，绝不把网络失败误判成登录死亡
        assert code == 0 and acc.get("cookie_fail_streak") == 1
    database._local.conn = None
