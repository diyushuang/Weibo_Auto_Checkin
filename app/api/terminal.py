"""网页终端 API（v1.3.5）—— 浏览器里获得一个交互式 Shell，直接运行 mini_client 等命令。

安全设计（终端等同管理员权限的 Shell，多层收敛）：
- 默认关闭：设置项 terminal_enabled=1 才允许连接，前端设置页可开关
- WS 自认证：HTTP 中间件不覆盖 WebSocket，握手时手动校验会话 Cookie；
  登录保护未启用时同样拒绝（终端不做「匿名放行」降级，与 require_admin 一致）
- 全局单会话：同一时刻仅允许一个终端，后来者拿到 4409
- 空闲超时：30 分钟无输入自动断开并回收进程
- 进程组回收：断开/退出时 SIGTERM 整个进程组，3 秒后 SIGKILL 兜底
- 审计：连接/退出均写日志（用户、时长、退出码）
"""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import signal
import time
from pathlib import Path

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from .. import auth, database, security
from ..security import safe_exc

if os.name == "posix":  # pty 相关模块 Windows 上不存在，按平台导入
    import fcntl
    import pty
    import struct
    import termios

log = logging.getLogger("weibo.terminal")

router = APIRouter(prefix="/api/terminal")

POSIX = os.name == "posix"
IDLE_TIMEOUT_SECONDS = 30 * 60   # 空闲自动断开
IDLE_TICK_SECONDS = 60           # 空闲检查周期
ROWS, COLS = 30, 100             # 初始终端尺寸

# 全局单会话登记表；None 表示空闲
_ACTIVE_SESSION: "_Session | None" = None


class _Session:
    """一次终端会话的全部资源。"""

    def __init__(self, ws: WebSocket, user: dict) -> None:
        self.ws = ws
        self.user = user
        self.proc: asyncio.subprocess.Process | None = None
        self.master_fd: int | None = None   # POSIX pty master；Windows 管道模式为 None
        self.started = time.monotonic()
        self.last_input = self.started
        self.finished = False               # 进程已退出且 exit 帧已发送
        self.close_note = ""                # 附带在 exit 帧里的断开原因


# ========================= 状态查询 =========================

@router.get("/status")
def terminal_status(user: dict = Depends(auth.require_admin)):
    return {
        "enabled": database.get_setting("terminal_enabled", "0") == "1",
        "busy": _ACTIVE_SESSION is not None,
        "pty": POSIX,
    }


# ========================= WebSocket 终端 =========================

@router.websocket("/ws")
async def terminal_ws(websocket: WebSocket):
    # HTTP 中间件不会拦 WebSocket 握手，这里自行完成会话认证
    user = _authenticate(websocket)
    if user is None:
        await websocket.accept()
        await websocket.close(code=4401, reason="未登录或登录保护未启用")
        return
    if database.get_setting("terminal_enabled", "0") != "1":
        await websocket.accept()
        await websocket.close(code=4403, reason="网页终端未启用，请在「设置 → 网页终端」开启")
        return

    global _ACTIVE_SESSION
    if _ACTIVE_SESSION is not None:
        await websocket.accept()
        await websocket.close(code=4409, reason="已有终端会话在运行，请先在其它页面断开")
        return

    session = _Session(websocket, user)
    _ACTIVE_SESSION = session  # 先占位，避免并发连接同时通过检查
    await websocket.accept()
    peer = websocket.client.host if websocket.client else "unknown"
    log.info("网页终端连接：user=%s peer=%s", user.get("username"), peer)
    try:
        await _spawn(session)
    except Exception as exc:
        log.warning("网页终端启动失败：user=%s err=%s", user.get("username"), safe_exc(exc))
        _ACTIVE_SESSION = None
        # 此处连接已在上方 accept 过，再 accept 会抛 RuntimeError；直接关闭即可
        await websocket.close(code=1011, reason=f"终端启动失败：{safe_exc(exc)}")
        return

    try:
        await _serve(session)
    finally:
        if _ACTIVE_SESSION is session:
            _ACTIVE_SESSION = None
        await _shutdown(session)
        log.info("网页终端退出：user=%s 时长=%.0fs note=%s",
                 user.get("username"), time.monotonic() - session.started,
                 session.close_note or "normal")


def _authenticate(websocket: WebSocket) -> dict | None:
    """与 HTTP 侧 require_admin 同等强度：登录保护未启用或会话无效一律拒绝。

    终端等同管理员权限的 Shell，不能套用普通 API「未启用登录即匿名放行」
    的降级 —— 那样任何未认证访客都能拿到 root shell。
    """
    if not auth.auth_enabled():
        return None
    token = websocket.cookies.get(auth.COOKIE_NAME, "")
    if not token:
        return None
    return database.get_session_user(token)


# ========================= 进程生成 =========================

def _build_env() -> dict:
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["HISTFILE"] = "/dev/null"  # 网页终端不留 shell 历史
    if POSIX:
        home = "/tmp/wcm-home"
        try:
            Path(home).mkdir(parents=True, exist_ok=True)
            env["HOME"] = home
        except OSError:
            pass
    else:
        # Windows 管道模式：Python 子进程统一输出 UTF-8（配合 chcp 65001，见 _spawn）
        env["PYTHONUTF8"] = "1"
    return env


def _working_dir() -> Path:
    # terminal.py 位于 app/api/ 下，仓库根即 mini_client.py 所在目录
    return Path(__file__).resolve().parents[2]


if POSIX:

    def _make_controlling_terminal():
        """子进程内执行：把继承来的 pty slave 设为控制终端，让 Ctrl-C 等信号生效。
        由 subprocess 在 dup2 之后、exec 之前调用（配合 start_new_session=True）。"""
        try:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        except Exception:
            pass

    def _resize_fd(fd: int, rows: int, cols: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def _spawn(session: _Session) -> None:
    cwd = _working_dir()
    env = _build_env()
    if POSIX:
        shell = "/bin/bash" if Path("/bin/bash").exists() else (os.environ.get("SHELL") or "/bin/sh")
        master_fd, slave_fd = pty.openpty()
        try:
            _resize_fd(slave_fd, ROWS, COLS)
            session.proc = await asyncio.create_subprocess_exec(
                shell, "-i",
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                cwd=str(cwd), env=env,
                start_new_session=True,
                preexec_fn=_make_controlling_terminal,
            )
        finally:
            os.close(slave_fd)
        session.master_fd = master_fd
    else:
        # Windows 本地开发降级：无 pty，用管道 cmd.exe。
        # chcp 65001 把 cmd 自身输出也切到 UTF-8（实验验证管道下生效，且不再打 banner），
        # 避免与 Python 子进程的 UTF-8 输出混流成两种编码。
        session.proc = await asyncio.create_subprocess_exec(
            "cmd.exe", "/k", "chcp", "65001>nul",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd), env=env,
        )
    await _emit(session, "\r\n微博签到面板 · 命令会话已建立\r\n\r\n")


# ========================= 数据泵 =========================

async def _emit(session: _Session, text: str) -> None:
    try:
        await session.ws.send_json({"type": "output", "data": text})
    except Exception:
        pass  # 发送失败由主循环的 disconnect 分支收尾


async def _pump_pty(session: _Session) -> None:
    """POSIX：从 pty master 读子进程输出。阻塞读放进线程池，避免占死事件循环。"""
    loop = asyncio.get_running_loop()
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    fd = session.master_fd
    assert fd is not None
    try:
        while True:
            data = await loop.run_in_executor(None, os.read, fd, 4096)
            if not data:
                break
            text = dec.decode(data)
            if text:
                await _emit(session, text)
    except OSError:
        pass  # EIO/EOF：子进程退出
    await _finish(session)


async def _pump_stream(session: _Session, stream: asyncio.StreamReader) -> None:
    # 两种 shell 模式的输出均已统一为 UTF-8（POSIX pty / Windows chcp 65001+PYTHONUTF8）
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while True:
            data = await stream.read(4096)
            if not data:
                break
            text = dec.decode(data)
            if text:
                await _emit(session, text)
    except Exception:
        pass


async def _pump_pipes(session: _Session) -> None:
    proc = session.proc
    assert proc and proc.stdout and proc.stderr
    await asyncio.gather(
        _pump_stream(session, proc.stdout),
        _pump_stream(session, proc.stderr),
        return_exceptions=True,
    )
    await _finish(session)


async def _finish(session: _Session) -> None:
    """进程输出结束：补收退出码并下发 exit 帧，然后关闭 WS。"""
    if session.finished:
        return
    session.finished = True
    code: int | None = None
    proc = session.proc
    if proc is not None:
        if proc.returncode is None:
            _kill(session)
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            code = -1
    try:
        await session.ws.send_json({"type": "exit", "code": code, "reason": session.close_note})
        await session.ws.close()
    except Exception:
        pass


# ========================= 输入 / 尺寸 =========================

async def _write_input(session: _Session, text: str) -> None:
    data = text.encode("utf-8", "replace")
    try:
        if session.master_fd is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, os.write, session.master_fd, data)
        elif session.proc is not None and session.proc.stdin is not None:
            # Windows 管道没有 pty 的 ICRNL 行 discipline：xterm 回车只发 \r，
            # cmd/powershell 按 \n 分行，孤 \r 会让它们一直等下一行——补齐 CRLF
            if b"\n" not in data:
                data = data.replace(b"\r", b"\r\n")
            session.proc.stdin.write(data)
            await session.proc.stdin.drain()
    except (OSError, RuntimeError):
        pass  # 进程已退出/管道已断


def _resize(session: _Session, rows: int, cols: int) -> None:
    if session.master_fd is None:
        return  # Windows 管道模式不支持
    try:
        _resize_fd(session.master_fd, rows, cols)
        if session.proc is not None and session.proc.returncode is None:
            os.kill(session.proc.pid, signal.SIGWINCH)
    except OSError:
        pass


# ========================= 生命周期收尾 =========================

def _kill(session: _Session) -> None:
    proc = session.proc
    if proc is None or proc.returncode is not None:
        return
    try:
        if POSIX:
            # start_new_session 使 proc.pid 即进程组长，可杀整组（含 shell 派生的子进程）
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _shutdown(session: _Session) -> None:
    proc = session.proc
    if proc is not None and proc.returncode is None:
        _kill(session)
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                if POSIX:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            await proc.wait()
    if session.master_fd is not None:
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        session.master_fd = None


async def _idle_watchdog(session: _Session) -> None:
    while not session.finished:
        await asyncio.sleep(IDLE_TICK_SECONDS)
        if session.finished:
            return
        if time.monotonic() - session.last_input > IDLE_TIMEOUT_SECONDS:
            session.close_note = f"空闲超过 {IDLE_TIMEOUT_SECONDS // 60} 分钟，终端已自动断开"
            _kill(session)
            return


# ========================= 会话主循环 =========================

async def _serve(session: _Session) -> None:
    pump = asyncio.ensure_future(
        _pump_pty(session) if POSIX else _pump_pipes(session)
    )
    idle = asyncio.ensure_future(_idle_watchdog(session))
    try:
        while True:
            msg = await session.ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg["type"] != "websocket.receive":
                continue
            session.last_input = time.monotonic()
            raw = msg.get("text")
            if raw is None and msg.get("bytes") is not None:
                raw = msg["bytes"].decode("utf-8", "replace")
            try:
                frame = json.loads(raw or "")
            except ValueError:
                continue
            kind = frame.get("type")
            if kind == "input":
                await _write_input(session, str(frame.get("data") or ""))
            elif kind == "resize":
                try:
                    _resize(session,
                            max(2, int(frame.get("rows") or ROWS)),
                            max(2, int(frame.get("cols") or COLS)))
                except (TypeError, ValueError):
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        for task in (pump, idle):
            task.cancel()
        await asyncio.gather(pump, idle, return_exceptions=True)
