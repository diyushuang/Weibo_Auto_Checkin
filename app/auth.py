"""
认证模块：密码哈希、会话 token、登录/登出逻辑、FastAPI 认证中间件。
默认管理员账号在首次启动时创建（不设密码则生成随机密码并打印）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import database

COOKIE_NAME = "wcm_token"
SESSION_TTL_HOURS = 168  # 7 天


# ========================= 密码哈希（PBKDF2，v1.3.8 升 600k 迭代） =========================

# OWASP 2023 建议 PBKDF2-SHA256 ≥600,000 迭代；老库存量哈希是 120k，
# 校验兼容旧格式（salt$hash），下次改密自然升级到新格式。
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_LEGACY_ITERATIONS = 120_000


def hash_password(password: str, salt: str | None = None) -> str:
    """返回 'pbkdf2$iterations$salt$hash' 格式（可自描述迭代次数）。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), iterations=_PBKDF2_ITERATIONS
    )
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if len(parts) == 4 and parts[0] == "pbkdf2":
            _, iterations_raw, salt, expected = parts
            iterations = int(iterations_raw)
            if iterations <= 0:
                return False
        elif len(parts) == 2:
            # 旧格式 salt$hash（120k 迭代），校验通过即兼容
            salt, expected = parts
            iterations = _PBKDF2_LEGACY_ITERATIONS
        else:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), iterations=iterations
        )
        return hmac.compare_digest(dk.hex(), expected)
    except (ValueError, TypeError):
        return False


# ========================= 默认管理员 =========================

def ensure_default_admin() -> None:
    """首次启动若无用户且未显式配置，则创建默认管理员。
    若设置 WCM_ADMIN_PASSWORD 用其创建；否则不建（由初始化页设置）。
    """
    if database.count_users() > 0:
        return
    password = os.environ.get("WCM_ADMIN_PASSWORD", "").strip()
    if not password:
        # 未提供密码：留待初始化页设置，不自动创建
        return
    username = os.environ.get("WCM_ADMIN_USER", "admin").strip() or "admin"
    database.create_user(username, hash_password(password))
    database.set_settings({"admin_initialized": "1"})
    print(f"  👤 默认管理员已创建: {username}（密码来自 WCM_ADMIN_PASSWORD）")


# ========================= Token =========================

def generate_token() -> str:
    return secrets.token_urlsafe(32)


def auth_enabled() -> bool:
    return database.get_setting("auth_enabled", "1") == "1"


# ========================= Cookie 安全（v7.2） =========================

def is_secure_request(request: Request) -> bool:
    """判断当前请求是否走 HTTPS。

    仅在 WCM_TRUST_PROXY=1（可信反代）时采信 `X-Forwarded-Proto`，
    否则以应用自身看到的 scheme 为准——直连场景下该头可被伪造。
    反代部署请设置 WCM_TRUST_PROXY=1，否则 Secure 标记不会生效。
    """
    if _trust_proxy_headers():
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if proto:
            return proto == "https"
        if (request.headers.get("x-forwarded-ssl") or "").lower() == "on":
            return True
    return request.url.scheme == "https"


def set_session_cookie(response, token: str, request: Request | None = None) -> None:
    """统一设置会话 Cookie：HttpOnly + SameSite=Lax，HTTPS 下追加 Secure。

    Secure 只在确认是 HTTPS 时加 —— 若在纯 HTTP 部署下无条件加上，
    浏览器会直接丢弃 Cookie，导致谁都登不进去。
    """
    secure = is_secure_request(request) if request is not None else False
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_TTL_HOURS * 3600,
        httponly=True, samesite="lax", path="/",
        secure=secure,
    )


# ========================= 登录失败限流（v7.2，状态落库见 database.py） =========================

LOGIN_MAX_FAILS = 5           # 窗口内允许的失败次数
LOGIN_WINDOW_SECONDS = 300    # 统计窗口
LOGIN_LOCK_SECONDS = 300      # 触发后锁定时长


def _trust_proxy_headers() -> bool:
    """仅在显式配置 WCM_TRUST_PROXY=1 时信任反代头（X-Forwarded-For 等）。

    面板直连暴露时，XFF 可被任意伪造——默认信任等于给爆破者免费换 IP。
    部署在 nginx 等可信反代后的用户请设置该环境变量。
    """
    return os.environ.get("WCM_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes")


def client_ip(request: Request) -> str:
    """取真实客户端 IP：可信反代场景取 X-Forwarded-For 第一段，否则取直连地址。"""
    if _trust_proxy_headers():
        xff = request.headers.get("x-forwarded-for") or ""
        if xff:
            return xff.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"


def login_lock_remaining(ip: str) -> int:
    """该 IP 剩余锁定秒数；0 表示未锁定。状态存 SQLite，重启/多进程共享。"""
    return database.login_lock_remaining(ip, LOGIN_LOCK_SECONDS)


def record_login_failure(ip: str) -> int:
    """记录一次登录失败；触发锁定则返回锁定秒数，否则 0。"""
    return database.record_login_failure(
        ip, LOGIN_MAX_FAILS, LOGIN_WINDOW_SECONDS, LOGIN_LOCK_SECONDS
    )


def reset_login_failures(ip: str) -> None:
    """登录成功后清掉该 IP 的失败计数。"""
    database.reset_login_failures(ip)


def reset_login_limits() -> None:
    """清空全部登录限流状态（测试/运维用）。"""
    database.reset_all_login_locks()


# ========================= FastAPI 依赖 =========================

def get_current_user(request: Request) -> dict:
    """依赖注入：从 cookie 取 token 校验用户。未启用登录时放行。"""
    if not auth_enabled():
        return {"id": 0, "username": "local", "anonymous": True}
    token = request.cookies.get(COOKIE_NAME, "")
    user = database.get_session_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def login_required(user: dict = Depends(get_current_user)) -> dict:
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """管理操作需要登录（登录用户即管理员）。"""
    if user.get("anonymous"):
        raise HTTPException(status_code=401, detail="需要登录")
    return user
