# -*- coding: utf-8 -*-
"""网页终端 API（v1.3.5）测试：认证 / 开关 / 单会话 / 真实进程回环。

真实进程测试在 POSIX 走 pty + bash，Windows 走管道 cmd.exe（降级模式），
两条路径都会验证「输入命令 → 收到输出 → 退出帧」的完整回环。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.database as db  # noqa: E402
from app.main import app  # noqa: E402

# 测试口令（拼接写法避免被日志脱敏工具改写）
GOOD_PW = "secret" + "123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_terminal.db"
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

    # 会话登记表是模块级全局，测试结束后必须清干净
    from app.api import terminal as terminal_api
    terminal_api._ACTIVE_SESSION = None
    db._local.conn = None


def _unauth_client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_terminal_unauth.db"
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


def test_status_requires_login(tmp_path, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    for c in _unauth_client(tmp_path, monkeypatch):
        r = c.get("/api/terminal/status")
        assert r.status_code == 401

        # 未登录 WS 握手应被拒（4401）
        with pytest.raises(WebSocketDisconnect) as ei:
            with c.websocket_connect("/api/terminal/ws") as ws:
                ws.receive_text()
        assert ei.value.code == 4401


def test_ws_rejected_when_disabled(client):
    from starlette.websockets import WebSocketDisconnect

    # 默认 terminal_enabled=0
    assert client.get("/api/terminal/status").json()["enabled"] is False

    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/api/terminal/ws") as ws:
            ws.receive_text()
    assert ei.value.code == 4403


def test_ws_rejected_when_auth_disabled(tmp_path, monkeypatch):
    """登录保护关闭时终端也不放行匿名连接（与 require_admin 一致，杜绝免认证 root shell）。"""
    from starlette.websockets import WebSocketDisconnect

    db_path = tmp_path / "test_terminal_noauth.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    from app import auth as auth_mod
    db.create_user("admin", auth_mod.hash_password(GOOD_PW))
    db.set_settings({"auth_enabled": "0", "terminal_enabled": "1"})
    auth_mod.reset_login_limits()

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with pytest.raises(WebSocketDisconnect) as ei:
            with c.websocket_connect("/api/terminal/ws") as ws:
                ws.receive_text()
        assert ei.value.code == 4401
    db._local.conn = None


def test_settings_whitelist_terminal_enabled(client):
    r = client.post("/api/settings", json={"terminal_enabled": "1"})
    assert r.status_code == 200
    assert r.json()["terminal_enabled"] == "1"
    assert client.get("/api/terminal/status").json()["enabled"] is True


def test_ws_single_session_lock(client, monkeypatch):
    from app.api import terminal as terminal_api

    monkeypatch.setattr(terminal_api, "_ACTIVE_SESSION", object())
    from starlette.websockets import WebSocketDisconnect

    # 即使开关打开，已有会话占用也应拒绝
    db.set_settings({"terminal_enabled": "1"})
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/api/terminal/ws") as ws:
            ws.receive_text()
    assert ei.value.code == 4409


def test_terminal_echo_roundtrip(client):
    """真实进程回环：输入 echo → 收到输出 → exit → 收到退出帧。"""
    from starlette.websockets import WebSocketDisconnect

    db.set_settings({"terminal_enabled": "1"})
    with client.websocket_connect("/api/terminal/ws") as ws:
        ws.send_text(json.dumps({"type": "input", "data": "echo wcm-ok\r\n"}))
        buf = ""
        got_exit = False
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                frame = json.loads(ws.receive_text())
            except WebSocketDisconnect:
                break
            if frame.get("type") == "output":
                buf += frame.get("data", "")
                if "wcm-ok" in buf:
                    break
            elif frame.get("type") == "exit":
                got_exit = True
                break
        assert "wcm-ok" in buf, f"未收到回显，实际输出：{buf[:200]!r}"

        ws.send_text(json.dumps({"type": "input", "data": "exit\r\n"}))
        deadline = time.time() + 20
        while not got_exit and time.time() < deadline:
            try:
                frame = json.loads(ws.receive_text())
            except WebSocketDisconnect:
                break
            if frame.get("type") == "exit":
                got_exit = True
        assert got_exit, "未收到 exit 帧"

    # 会话结束后登记表应清空
    from app.api import terminal as terminal_api
    assert terminal_api._ACTIVE_SESSION is None
