# -*- coding: utf-8 -*-
"""Cookie 生命周期域层：健康度、自适应保活间隔、观测回写（面板与 CLI 共用）。

本模块是 database（存储层）之上的唯一 Cookie 状态读写实现：
- scheduler 的保活 job / 签到流程
- topics_service 的列表刷新回写
- 未来的其它调用方
都从这里取函数，禁止再各自实现一份。
"""
from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, timedelta

from . import database
from .weibo_client import (
    cookie_diff_gate,
    cookie_to_string,
    describe_cookie_lifespan,
    harvest_cookie_rotation,
    normalize_cookie,
    reject_cookie_shrink,
)

log = logging.getLogger("weibo.cookie")


# 同一账号的"读 cookie → 发请求 → 回写"必须串行：签到线程按代理分组并行，
# 保活 job 又独立触发，两边同时续期会互相覆盖出更旧的 SUB。
_account_locks: dict[int, threading.Lock] = {}
_account_locks_guard = threading.Lock()


def get_account_lock(account_id: int) -> threading.Lock:
    """按账号 id 取（或建）进程内互斥锁。"""
    with _account_locks_guard:
        lock = _account_locks.get(account_id)
        if lock is None:
            lock = threading.Lock()
            _account_locks[account_id] = lock
        return lock


def now_str(moment: datetime | None = None) -> str:
    return (moment or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _int_setting(key: str, default: int) -> int:
    try:
        return int(database.get_setting(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _float_setting(key: str, default: float) -> float:
    try:
        return float(database.get_setting(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


# cookie_health 取值优先级：越靠前越严重。观测结果与到期推算取更严重的那个。
_HEALTH_PRIORITY = ("invalid", "expired", "expiring", "rate_limited",
                    "network_error", "stale", "ok", "unknown")


def resolve_cookie_health(observed: str, estimated: str) -> str:
    """观测结果与到期推算取更严重的那个。

    例外：微博刚确认登录有效时，不能因为"已超过经验寿命"就报已过期 —— 经验寿命
    只是统计值，探活成功才是硬证据；这时报"即将失效"，既不隐瞒也不误杀。
    """
    if observed == "ok" and estimated == "expired":
        return "expiring"

    def rank(state: str) -> int:
        return _HEALTH_PRIORITY.index(state) if state in _HEALTH_PRIORITY else 99
    candidates = [state for state in (observed, estimated) if state]
    if not candidates:
        return "unknown"
    return min(candidates, key=rank)


def cookie_expiry_view(acc: dict) -> dict:
    """按当前设置把一行账号的 cookie 生命周期算成展示用 dict。"""
    return describe_cookie_lifespan(
        acc,
        ttl_days=_int_setting("cookie_session_ttl_days", 7),
        warn_days=_float_setting("cookie_expiry_warn_days", 2),
    )


def keepalive_interval_hours(acc: dict) -> float:
    """自适应续期间隔：保证一个实测 session 寿命内至少成功换发 5 次，
    临近预计失效时自动加密抢救，连续失败则指数退避，别在代理抖动时猛撞接口。"""
    base = _float_setting("keepalive_interval_hours", 12)
    lifetime = int(acc.get("cookie_lifetime_observed_days") or 0)
    interval = base
    if lifetime > 0:
        interval = min(base, max(lifetime * 24 / 5, 2.0))
    if cookie_expiry_view(acc).get("state") in ("expiring", "expired"):
        # 最后的抢救窗口：加密探测，多给微博几次换发 SUB 的机会。
        # 换发一旦成功，cookie_last_rotated_at 推进，预计寿命也随之延长。
        urgent = max(_float_setting("keepalive_urgent_interval_hours", 6), 0.5)
        interval = min(interval, urgent)
    streak = int(acc.get("cookie_fail_streak") or 0)
    if streak > 0:
        interval = min(24.0, interval * (2 ** streak))
    return max(min(interval, 72.0), 1.0)


def _quiet_window() -> tuple[int, int] | None:
    """解析深夜静默时段设置（如 "0-6"）；off/非法值返回 None 不启用。"""
    raw = str(database.get_setting("keepalive_quiet_hours", "0-6") or "").strip().lower()
    if not raw or raw in ("off", "none", "disabled", "0"):
        return None
    try:
        start_s, end_s = raw.split("-", 1)
        start, end = int(start_s.strip()), int(end_s.strip())
    except ValueError:
        return None
    if not (0 <= start < end <= 24):
        return None
    return start, end


def _shift_out_of_quiet_hours(moment: datetime) -> datetime:
    """真人不会凌晨整点稳定在线：命中深夜静默窗的排期挪到清晨并加随机分钟。

    抢救期（urgent）不挪 —— 眼看要过期了，续命优先于作息伪装。
    """
    window = _quiet_window()
    if window is None:
        return moment
    start, end = window
    if not (start <= moment.hour < end):
        return moment
    if end >= 24:
        target = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        target += timedelta(days=1)
    else:
        target = moment.replace(hour=end, minute=0, second=0, microsecond=0)
    return target + timedelta(minutes=random.uniform(0, 120))


def _jitter_hours(hours: float, *, low: float = 0.85, high: float = 1.2) -> float:
    """给续期间隔加随机抖动：永远踩着整周期出现的请求本身就是机器人指纹。"""
    return hours * random.uniform(low, high)


def _next_keepalive_at(hours: float, now: datetime | None = None,
                       *, urgent: bool = False) -> str:
    """计算带随机抖动（非抢救期避开深夜）的下次保活时间。"""
    moment = now or datetime.now()
    scheduled = moment + timedelta(hours=_jitter_hours(hours))
    if not urgent:
        scheduled = _shift_out_of_quiet_hours(scheduled)
    return scheduled.strftime("%Y-%m-%d %H:%M:%S")


def _cookie_state_meta(acc: dict, *, observed: str = "",
                       rotated_keys: list[str] | None = None,
                       rotate_source: str = "",
                       now: datetime | None = None) -> dict:
    """汇总一次认证请求的观测结果，得到要写进 cookie_* 列的元数据。"""
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    view = cookie_expiry_view(acc)
    health = resolve_cookie_health(observed, view["state"])
    urgent = view["state"] in ("expiring", "expired")
    meta = {
        "cookie_health": health,
        "cookie_expires_at": view["alf_expires_at"],
        "cookie_next_keepalive_at": _next_keepalive_at(
            keepalive_interval_hours(acc), now, urgent=urgent),
    }
    if rotated_keys is not None:
        meta["cookie_last_rotated_at"] = stamp
        meta["cookie_last_rotate_keys"] = ",".join(rotated_keys)
        meta["cookie_last_rotate_source"] = rotate_source or "unknown"
    if observed == "ok":
        # 只有微博明确回了 login=True 才算"确认有效"；列表接口抖动或流程内部
        # 异常都不能刷新这个时钟，否则真死了也报不出来。
        meta["cookie_last_seen_valid_at"] = stamp
        meta["cookie_fail_streak"] = 0
    return meta


def _record_failure_streak(account_id: int, observed: str) -> None:
    """网络/风控类失败：退避重试，但绝不改 cookie，也不把账号判死（代理掉线≠登录过期）。"""
    acc = database.get_account(account_id) or {}
    streak = int(acc.get("cookie_fail_streak") or 0) + 1
    fresh = {**acc, "cookie_fail_streak": streak}
    interval = keepalive_interval_hours(fresh)
    urgent = cookie_expiry_view(fresh).get("state") in ("expiring", "expired")
    database.set_cookie_state(
        account_id,
        cookie_health=observed,
        cookie_fail_streak=streak,
        cookie_next_keepalive_at=_next_keepalive_at(interval, urgent=urgent),
    )


# 签到结果里的 failure_type → cookie 观测健康度（未列出的按"登录态有效"处理）
_OBSERVED_BY_FAILURE = {
    "network": "network_error",
    "rate_limited": "rate_limited",
    "topics_list_error": "",
    "internal_error": "",
}


def persist_cookie_observation(acc: dict, raw_cookie: str,
                               cookie_dict: dict, result: dict,
                               source: str = "") -> None:
    """把一次认证请求观测到的 cookie 换发与生命周期状态写回库。

    回写门用 cookie_diff_gate：只认同名 key 的变化会漏掉微博补发的登录凭据，
    库里那份一旦缺了 SCF 就再也补不回来，session 便只能等死。
    """
    account_id = acc["id"]
    name = acc.get("name", "未命名账号")
    failure_type = result.get("failure_type")
    changed = list(result.get("cookie_changed") or [])
    added = list(result.get("cookie_added") or [])
    refreshed = result.get("cookie") or {}

    if failure_type == "cookie_invalid":
        lifetime = database.mark_cookie_dead(account_id)
        if lifetime:
            log.info("  %s 实测 session 寿命约 %d 天，后续保活按此自适应加密", name, lifetime)
        return

    observed = _OBSERVED_BY_FAILURE.get(failure_type or "", "ok")
    if observed in ("network_error", "rate_limited"):
        _record_failure_streak(account_id, observed)
        return
    if observed == "":
        # 列表接口抖动之类不能证明登录态好坏，只刷新到期视图，不动 valid 标记
        database.set_cookie_state(account_id, **_cookie_state_meta(acc, observed=""))
        return

    if refreshed and cookie_diff_gate(changed, added):
        new_cookie_str = cookie_to_string(refreshed)
        reason = reject_cookie_shrink(cookie_dict, normalize_cookie(new_cookie_str))
        if reason:
            log.warning("  %s 放弃 cookie 回写：%s", name, reason)
            database.set_cookie_state(account_id, **_cookie_state_meta(acc, observed="ok"))
            return
        rotated = list(dict.fromkeys(changed + added))
        view = {**acc, "cookie": new_cookie_str, "cookie_raw": new_cookie_str,
                "cookie_last_rotated_at": now_str()}
        wrote = database.merge_account_cookie(
            account_id, new_cookie_str,
            meta=_cookie_state_meta(view, observed="ok", rotated_keys=rotated,
                                    rotate_source=source),
            expected_old=raw_cookie,
        )
        if wrote:
            log.info("  🔄 %s 已续期 cookie（%s，来自 %s）",
                     name, ",".join(rotated), source or "未知来源")
        else:
            log.info("  %s 的 cookie 已被其它流程更新，本次回写作废", name)
        return

    database.set_cookie_state(account_id, **_cookie_state_meta(acc, observed="ok"))


def persist_refreshed_cookie(account_id: int, session, base_cookie: dict,
                             *, source: str = "") -> bool:
    """回收一次认证请求换发到的 Set-Cookie 并落库（调用方需持有该账号锁）。"""
    merged, changed, added = harvest_cookie_rotation(session, base_cookie)
    if not cookie_diff_gate(changed, added):
        return False
    new_cookie_str = cookie_to_string(merged)
    reason = reject_cookie_shrink(base_cookie, normalize_cookie(new_cookie_str))
    if reason:
        log.warning("  放弃 cookie 回写：%s", reason)
        return False
    rotated = list(dict.fromkeys(changed + added))
    acc = database.get_account(account_id) or {}
    view = {**acc, "cookie": new_cookie_str, "cookie_raw": new_cookie_str,
            "cookie_last_rotated_at": now_str()}
    wrote = database.merge_account_cookie(
        account_id, new_cookie_str,
        meta=_cookie_state_meta(view, observed="ok", rotated_keys=rotated,
                                rotate_source=source),
    )
    if wrote:
        log.info("  🔄 %s 已续期 cookie（%s，来自 %s）",
                 acc.get("name", account_id), ",".join(rotated), source or "未知来源")
    return wrote
