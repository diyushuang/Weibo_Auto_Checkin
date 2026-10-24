# -*- coding: utf-8 -*-
"""手机 App 同步 API（v1.3.7）：手机是登录态的根，面板只消费手机推来的新 Cookie。

设计要点：
- 鉴权用独立同步令牌（X-Panel-Token 头），不占用管理员会话 —— 手机端无人值守，
  会话会过期，令牌不会；令牌权限极小：只能把一份 Cookie upsert 成账号。
- 令牌比较用 secrets.compare_digest，错误尝试按 IP 限流，防在线爆破。
- 探活失败（login=false）时对「面板上还健康的账号」拒绝覆盖：手机端轮换失败
  很常见（被风控/换了 IP），拿一份探不活的 Cookie 覆盖掉面板正在正常续期的
  登录态是净损失；账号本来就 invalid 则放行（没有更差的结局）。
"""
from __future__ import annotations

import logging
import secrets
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import accounts_service, auth, database
from ..security import safe_exc
from ..weibo_client import (
    NetworkError, RateLimitedError, new_account_session, normalize_cookie,
    verify_cookie,
)
from .accounts import _cache_weibo_avatar, _fetch_weibo_profile

log = logging.getLogger("weibo.mobile")

router = APIRouter(prefix="/api/mobile", tags=["mobile"])

# 同步令牌错误限流：15 分钟窗口内最多 10 次失败，触发后锁 15 分钟。
_FAIL_WINDOW_SECONDS = 900
_FAIL_MAX = 10
_LOCK_SECONDS = 900
# 成功同步限流：每 IP 每小时最多 30 次（正常节奏是每 6 小时一轮）。
_OK_WINDOW_SECONDS = 3600
_OK_MAX = 30

_rate_guard = threading.Lock()
_rate_stats: dict[str, dict] = {}


def _prune(hits: list[float], window: float) -> list[float]:
    now = time.time()
    return [t for t in hits if now - t < window]


def _check_and_count(ip: str, *, ok: bool) -> float:
    """记录一次同步尝试；返回剩余锁定秒数（0 = 未锁定）。调用方持锁语义由内部保证。"""
    with _rate_guard:
        stat = _rate_stats.setdefault(ip, {"fails": [], "ok": [], "locked_until": 0.0})
        now = time.time()
        if stat["locked_until"] > now:
            return stat["locked_until"] - now
        if ok:
            stat["ok"] = _prune(stat["ok"], _OK_WINDOW_SECONDS)
            if len(stat["ok"]) >= _OK_MAX:
                # 不锁，只拒绝：成功的同步不该被惩罚，只是别刷太勤
                stat["ok"].append(now)
                return -1.0  # -1 表示"频率超限"而非"锁定"
            stat["ok"].append(now)
            stat["fails"] = _prune(stat["fails"], _FAIL_WINDOW_SECONDS)
            return 0.0
        stat["fails"].append(now)
        stat["fails"] = _prune(stat["fails"], _FAIL_WINDOW_SECONDS)
        if len(stat["fails"]) >= _FAIL_MAX:
            stat["locked_until"] = now + _LOCK_SECONDS
            stat["fails"] = []
            return _LOCK_SECONDS
        return 0.0


class MobileSync(BaseModel):
    cookie: str
    name: str = ""
    uid: str = ""   # App 侧保存的微博 uid：探活失败拿不到资料时靠它匹配既有账号


def _mark_last(result: str) -> None:
    database.set_settings({
        "mobile_sync_last_at": database._now(),
        "mobile_sync_last_result": result[:200],
    })


@router.post("/sync")
def mobile_sync(data: MobileSync, request: Request):
    """手机 App 推送最新 Cookie：探活 → 覆盖同微博号账号 → 重置生命周期。"""
    ip = auth.client_ip(request) or "unknown"
    if database.get_setting("mobile_sync_enabled", "0") != "1":
        raise HTTPException(403, "手机同步未启用（面板设置里打开「手机保活」）")
    token = request.headers.get("X-Panel-Token", "")
    expected = database.get_setting("mobile_sync_token", "")
    if not expected or not secrets.compare_digest(token, expected):
        remaining = _check_and_count(ip, ok=False)
        if remaining > 0:
            raise HTTPException(429, "尝试次数过多，稍后再试",
                                headers={"Retry-After": str(int(remaining) + 1)})
        raise HTTPException(401, "同步令牌无效")

    cookie = (data.cookie or "").strip()
    cookie_dict = normalize_cookie(cookie)
    if not cookie_dict.get("SUB"):
        raise HTTPException(400, "Cookie 缺少 SUB，请先在 App 内完成微博登录")
    remaining = _check_and_count(ip, ok=True)
    if remaining > 0:  # 之前刷令牌失败被锁，正确令牌也要等锁过期
        raise HTTPException(429, "尝试次数过多，稍后再试",
                            headers={"Retry-After": str(int(remaining) + 1)})
    if remaining < 0:
        raise HTTPException(429, "同步频率超限（每小时最多 30 次）")

    session = new_account_session()
    # 把手机传来的 cookie 种进 jar：_fetch_weibo_profile 依赖 jar 里的登录态
    for key, value in cookie_dict.items():
        session.cookies.set(key, value, domain=".weibo.cn")
    try:
        logged_in, _st = verify_cookie(session, cookie_dict)
    except RateLimitedError as exc:
        _mark_last(f"被微博风控拦截：{safe_exc(exc)}")
        raise HTTPException(429, "微博风控拦截，请稍后在 App 重试")
    except NetworkError as exc:
        _mark_last(f"探活网络失败：{safe_exc(exc)}")
        raise HTTPException(502, f"面板探活网络失败：{safe_exc(exc)}")

    profile = {"name": "", "avatar_url": "", "uid": ""}
    if logged_in:
        profile = _fetch_weibo_profile(session)
    effective_uid = str(profile.get("uid") or data.uid or "").strip()

    existing = accounts_service.find_existing_account(
        cookie=cookie, weibo_uid=effective_uid)
    if not logged_in:
        # 手机这份 Cookie 在面板侧探不活：
        # - 既有账号还健康 → 拒绝覆盖（面板正在正常续期，覆盖是净损失）
        # - 匹配不到账号 → 也拒绝（不凭探不活的 Cookie 新建垃圾账号）
        # - 既有账号已 invalid → 放行，续上手机这条登录脉络
        if existing is None or existing.get("cookie_health") not in ("invalid", ""):
            who = existing.get("name", "?") if existing else "无匹配账号"
            _mark_last(f"拒绝：{who}，手机 Cookie 探活失败")
            return JSONResponse(status_code=409, content={
                "ok": False, "health": "invalid",
                "detail": "该 Cookie 未通过面板探活；请打开 App 确认微博登录状态后再同步",
            })

    # 新账号才下载头像/取名，老账号保留用户在面板上的自定义名与头像
    name = ""
    avatar_local = ""
    if existing is None:
        name = data.name.strip() or profile.get("name", "")
        remote_avatar = profile.get("avatar_url", "")
        if remote_avatar:
            try:
                avatar_local = _cache_weibo_avatar(
                    session, remote_avatar, effective_uid)
            except Exception as exc:  # 头像失败不阻断同步
                log.info("手机同步：头像缓存失败：%s", safe_exc(exc))

    account, action = accounts_service.upsert_account(
        cookie,
        name=name,
        default_name="手机同步账号",
        avatar_url=avatar_local,
        weibo_uid=effective_uid,
        enabled=None if existing is not None else True,
    )
    updated = action == "updated"
    result = f"{'覆盖' if updated else '新建'} {account.get('name', '?')}" + (
        "" if logged_in else "（探活未通过）")
    _mark_last(result)
    log.info("📲 手机同步：%s", result)
    return {
        "ok": True,
        "name": account.get("name", name),
        "updated": updated,
        "health": "ok" if logged_in else "invalid",
    }


@router.post("/token/regenerate")
def regenerate_token(user: dict = Depends(auth.require_admin)):
    """生成新的同步令牌（旧令牌立即作废）。"""
    token = secrets.token_urlsafe(24)
    database.set_settings({
        "mobile_sync_token": token,
        "mobile_sync_enabled": "1",
    })
    log.info("📲 手机同步令牌已重新生成")
    return {"token": token, "enabled": True}
