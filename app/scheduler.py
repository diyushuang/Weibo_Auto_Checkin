"""
签到调度与执行编排。
支持手动触发和定时（APScheduler cron）触发，统一走 run_checkin 流程。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import database, notifier
from .anti_ban import AntiBanPolicy
from .security import safe_exc
from .weibo_client import (
    CheckinOptions, CookieExpiredError, NetworkError, RateLimitedError,
    cookie_diff_gate, cookie_to_string, describe_cookie_lifespan,
    fetch_topic_posts, new_account_session, harvest_cookie_rotation,
    normalize_cookie, reject_cookie_shrink, get_followed_topics,
    run_account_checkin, verify_cookie,
)

log = logging.getLogger("weibo.scheduler")

scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
_running_lock = threading.Lock()
_keepalive_lock = threading.Lock()   # 独立于签到锁：保活绝不能挤掉签到
_state_lock = threading.Lock()   # 保护 _current_run/_last_run_summary 的替换与读取
_current_run = None          # 当前运行状态（供前端轮询）
_last_run_summary = None     # 最近一次任务汇总

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


def cookie_expiry_view(acc: dict) -> dict:
    """按当前设置把一行账号的 cookie 生命周期算成展示用 dict。"""
    return describe_cookie_lifespan(
        acc,
        ttl_days=_int_setting("cookie_session_ttl_days", 7),
        warn_days=_float_setting("cookie_expiry_warn_days", 2),
    )


def keepalive_interval_hours(acc: dict) -> float:
    """自适应续期间隔：保证一个实测 session 寿命内至少成功换发 5 次，
    连续失败则指数退避，别在代理抖动时猛撞接口。"""
    base = _float_setting("keepalive_interval_hours", 12)
    lifetime = int(acc.get("cookie_lifetime_observed_days") or 0)
    interval = base
    if lifetime > 0:
        interval = min(base, max(lifetime * 24 / 5, 2.0))
    streak = int(acc.get("cookie_fail_streak") or 0)
    if streak > 0:
        interval = min(24.0, interval * (2 ** streak))
    return max(min(interval, 72.0), 1.0)


def _cookie_state_meta(acc: dict, *, observed: str = "",
                       rotated_keys: list[str] | None = None,
                       now: datetime | None = None) -> dict:
    """汇总一次认证请求的观测结果，得到要写进 cookie_* 列的元数据。"""
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    view = cookie_expiry_view(acc)
    health = resolve_cookie_health(observed, view["state"])
    meta = {
        "cookie_health": health,
        "cookie_expires_at": view["alf_expires_at"],
        "cookie_next_keepalive_at": (
            now + timedelta(hours=keepalive_interval_hours(acc))).strftime("%Y-%m-%d %H:%M:%S"),
    }
    if rotated_keys is not None:
        meta["cookie_last_rotated_at"] = stamp
        meta["cookie_last_rotate_keys"] = ",".join(rotated_keys)
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
    interval = keepalive_interval_hours({**acc, "cookie_fail_streak": streak})
    database.set_cookie_state(
        account_id,
        cookie_health=observed,
        cookie_fail_streak=streak,
        cookie_next_keepalive_at=(
            datetime.now() + timedelta(hours=interval)).strftime("%Y-%m-%d %H:%M:%S"),
    )


# 签到结果里的 failure_type → cookie 观测健康度（未列出的按"登录态有效"处理）
_OBSERVED_BY_FAILURE = {
    "network": "network_error",
    "rate_limited": "rate_limited",
    "topics_list_error": "",
    "internal_error": "",
}


def persist_cookie_observation(acc: dict, raw_cookie: str,
                               cookie_dict: dict, result: dict) -> None:
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
                "cookie_last_rotated_at": _now_str()}
        wrote = database.merge_account_cookie(
            account_id, new_cookie_str,
            meta=_cookie_state_meta(view, observed="ok", rotated_keys=rotated),
            expected_old=raw_cookie,
        )
        if wrote:
            log.info("  🔄 %s 已续期 cookie（%s）", name, ",".join(rotated))
        else:
            log.info("  %s 的 cookie 已被其它流程更新，本次回写作废", name)
        return

    database.set_cookie_state(account_id, **_cookie_state_meta(acc, observed="ok"))


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_current_run() -> dict | None:
    """返回正在进行/最近一次的运行状态（浅拷贝，避免前端拿到可变内部引用）。"""
    with _state_lock:
        current = _current_run if _current_run is not None else _last_run_summary
        return dict(current) if current is not None else None


# ========================= 核心签到流程 =========================

def run_checkin(trigger_type: str = "manual", account_ids: list[int] | None = None) -> dict:
    """
    执行一次完整签到：遍历启用账号（或指定 account_ids）→ 防封等待 → 签到 → 记录日志 → 汇总。
    返回汇总 dict。线程安全（同一时刻只允许一个运行）。
    """
    global _current_run, _last_run_summary
    if not _running_lock.acquire(blocking=False):
        log.warning("已有签到任务在运行，跳过本次触发")
        return {"status": "skipped", "message": "已有任务在运行"}

    task_id = uuid.uuid4().hex[:12]
    database.create_task(task_id, trigger_type)
    started = _now_str()
    # 签到后是否自动刷新超话帖子缓存（只读一次设置，避免每账号重复查库）
    auto_refresh_enabled = database.get_setting("auto_refresh_topics", "0") == "1"
    with _state_lock:
        _current_run = {"task_id": task_id, "status": "running",
                        "trigger_type": trigger_type, "started_at": started,
                        "progress": 0, "accounts_total": 0, "accounts_done": 0}
    opts = CheckinOptions.from_settings(database.get_setting)
    policy = AntiBanPolicy.from_settings()

    log.info("开始签到任务 %s（%s）：%s", task_id, trigger_type, policy.describe())
    # 提到 try 外：最外层异常分支也要能拿到已完成账号来发告警
    overall: list[dict] = []

    try:
        accounts = database.get_enabled_accounts()
        if account_ids:
            ids = set(account_ids)
            accounts = [a for a in accounts if a["id"] in ids]
        with _state_lock:
            _current_run["accounts_total"] = len(accounts)
        if not accounts:
            summary = _finish(task_id, "success", "没有可用账号", started,
                              {"accounts": 0, "total": 0, "success": 0,
                               "fail": 0, "detail": []})
            return summary

        total = success = fail = 0
        results_lock = threading.Lock()

        # 按账号指定的 socks 分组；不同 socks → 不同组（可并行）
        groups: dict[str, list[dict]] = {}
        group_order: list[str] = []
        for acc in accounts:
            proxy = (acc.get("proxy") or "").strip()
            key = proxy if proxy else "__direct__"   # 无指定归到直连组
            if key not in groups:
                groups[key] = []
                group_order.append(key)
            groups[key].append(acc)

        n_groups = len(group_order)
        parallel = n_groups > 1  # 多个不同 socks 才并行
        log.info("按代理分组：%d 组%s（%s）", n_groups,
                 "，并行签到" if parallel else "，依次签到",
                 "、".join(groups[k][0].get("name", "?") if groups[k] else k.split("@")[-1] for k in group_order))

        def _safe_group_label(group_label: str) -> str:
            """控制台日志里脱敏代理：去掉 协议://user:pass@，只留 host:port。"""
            s = group_label or ""
            if "@" in s:
                # socks5://user:pass@host:port -> host:port
                return s.split("@", 1)[-1]
            return s

        def _safe_channel_label(channel: str) -> str:
            """日志只记录安全的通道摘要，绝不把代理认证信息写入数据库。"""
            return "SOCKS5 代理" if channel == "socks" else "直连"

        def _process_one(acc: dict, idx_in_group: int, group_size: int, group_label: str):
            """处理单个账号签到。返回结果 dict 或 None（并发下由主线程记账）。"""
            log_label = _safe_group_label(group_label)
            # 防封等待（组内账号间）
            if idx_in_group > 0 or policy.should_wait():
                wait = policy.wait_between_accounts(idx_in_group + 1, group_size)
                if wait and group_label:
                    log.info("  [%s] 防封等待 %.0fs 后处理 %s", log_label, wait, acc.get("name"))

            raw_cookie = acc.get("cookie") or acc.get("cookie_raw") or ""
            cookie_dict = normalize_cookie(raw_cookie)
            proxy_url = acc.get("proxy") or None

            log.info("👤 [%s] 账号：%s", log_label, acc.get("name"))
            # "读 cookie → 发请求 → 回写"整段占住账号锁：保活 job 可能在签到期间
            # 想续同一个账号，两边并发会用更旧的 SUB 覆盖更新的那份，自己把登录态作死。
            with get_account_lock(acc["id"]):
                try:
                    result = run_account_checkin(cookie_dict, opts, proxy_url=proxy_url)
                except Exception as exc:
                    # 兜底：单个账号再意外也不能中断整批签到。
                    log.exception("账号 %s 签到流程异常", acc.get("name"))
                    result = {
                        "status": "failed", "message": f"签到流程异常：{safe_exc(exc)}",
                        "total": 0, "success": 0, "fail": 0, "results": [],
                        "cookie": {}, "cookie_changed": [], "cookie_added": [],
                        "channel": "socks" if proxy_url else "direct",
                        "failure_type": "internal_error",
                    }
                channel = "socks" if proxy_url else "direct"
                persist_cookie_observation(acc, raw_cookie, cookie_dict, result)

                # 签到后自动刷新超话帖子缓存（如果启用）
                if auto_refresh_enabled and result.get("status") in ("success", "partial"):
                    _auto_refresh_topics_for_account(acc, cookie_dict, proxy_url, channel)

            # 更新账号状态
            database.touch_account_result(
                acc["id"], result["status"],
                result.get("message") or result.get("status"),
            )

            entry = {
                "name": acc.get("name", "未命名账号"),
                "status": result["status"],
                "failure_type": result.get("failure_type"),
                "message": result.get("message", ""),
                "channel": _safe_channel_label(result.get("channel", "direct")),
                "total": result.get("total", 0),
                "success": result.get("success", 0),
                "fail": result.get("fail", 0),
                "results": result.get("results", []),
                # 报告里顺带告诉用户这份 cookie 最近一次续期是什么时候
                "cookie_rotated_at": (database.get_account(acc["id"]) or {}).get(
                    "cookie_last_rotated_at", ""),
            }

            # 记录日志
            database.add_log({
                "account_id": acc["id"],
                "account_name": acc.get("name", "未命名账号"),
                "task_id": task_id,
                "status": result["status"],
                "channel": _safe_channel_label(result.get("channel", "direct")),
                "total": result.get("total", 0),
                "success": result.get("success", 0),
                "fail": result.get("fail", 0),
                "detail": result.get("results", []),
                "message": result.get("message", ""),
            })

            return entry

        # 用可变字典做并发计数器/合计
        nonlocal_map = {"total": 0, "success": 0, "fail": 0, "done": 0}

        def _account_entry(entry: dict) -> None:
            with results_lock:
                overall.append(entry)
                nonlocal_map["total"] += entry.get("total", 0)
                nonlocal_map["success"] += entry.get("success", 0)
                nonlocal_map["fail"] += entry.get("fail", 0)
                nonlocal_map["done"] += 1
                with _state_lock:
                    if _current_run is not None:
                        _current_run["accounts_done"] = nonlocal_map["done"]
                        _current_run["progress"] = round(
                            nonlocal_map["done"] / len(accounts) * 100)

        def _crash_entry(acc: dict, exc: Exception) -> dict:
            """账号级兜底：写库并计入汇总，让整批继续跑完、告警照常发出。"""
            message = f"处理异常：{safe_exc(exc)}"
            try:
                database.touch_account_result(acc["id"], "failed", message)
                database.add_log({
                    "account_id": acc["id"],
                    "account_name": acc.get("name", "未命名账号"),
                    "task_id": task_id,
                    "status": "failed", "channel": "unknown",
                    "total": 0, "success": 0, "fail": 0, "detail": [],
                    "message": message,
                })
            except Exception:
                log.exception("账号 %s 异常兜底写库也失败", acc.get("name"))
            return {
                "name": acc.get("name", "未命名账号"), "status": "failed",
                "failure_type": "internal_error", "message": message,
                "channel": "unknown", "total": 0, "success": 0, "fail": 0,
                "results": [],
            }

        def _run_group(accounts_in_group: list[dict], label: str):
            """依次处理组内账号；任何账号意外都不能带走整批签到。"""
            for i, acc in enumerate(accounts_in_group, start=0):
                try:
                    entry = _process_one(acc, i, len(accounts_in_group), label)
                except Exception as exc:
                    log.exception("账号 %s 处理异常", acc.get("name"))
                    entry = _crash_entry(acc, exc)
                if entry is not None:
                    _account_entry(entry)

        if parallel:
            # 不同 socks 组并行（每个组一个线程）
            threads = []
            for label in group_order:
                t = threading.Thread(
                    target=_run_group, args=(groups[label], label),
                    daemon=True,
                )
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
        else:
            # 只有一个组：依次签到
            label = group_order[0] if group_order else ""
            _run_group(groups.get(label, []), label)

        total, success, fail = nonlocal_map["total"], nonlocal_map["success"], nonlocal_map["fail"]

        # 汇总
        status = "failed" if fail > 0 and success == 0 else ("partial" if fail > 0 else "success")
        summary = _finish(task_id, status, "完成", started, {
            "accounts": len(overall), "total": total, "success": success,
            "fail": fail, "detail": overall,
            "time": _now_str(), "task_id": task_id, "trigger_type": trigger_type,
        })

        # TG 通知
        if database.get_setting("tg_enabled", "0") == "1":
            notifier.send_checkin_report(summary)
        return summary
    except Exception as exc:
        log.exception("签到任务异常")
        safe_detail = safe_exc(exc)
        summary = _finish(task_id, "failed", f"异常：{safe_detail}", started, {
            # 从已完成账号汇总，而不是引用 nonlocal_map：异常可能发生在它定义之前
            "accounts": len(overall),
            "total": sum(e.get("total", 0) for e in overall),
            "success": sum(e.get("success", 0) for e in overall),
            "fail": sum(e.get("fail", 0) for e in overall),
            "detail": overall,
            "time": _now_str(), "task_id": task_id, "trigger_type": trigger_type,
            "error": safe_detail,
        })
        # 批次级异常同样要发 TG：否则"整批被一个账号打挂"会变成静默失败，
        # 用户只会发现今天没签到，却收不到任何提示。
        if database.get_setting("tg_enabled", "0") == "1" and overall:
            try:
                notifier.send_checkin_report(summary)
            except Exception:
                log.exception("异常路径发送 TG 报告失败")
        return summary
    finally:
        _running_lock.release()


def _finish(task_id: str, status: str, message: str, started: str,
            summary: dict) -> dict:
    global _current_run, _last_run_summary
    finished = _now_str()
    database.finish_task(task_id, status, message)
    summary["status"] = status
    summary["finished_at"] = finished
    summary["message"] = message
    database.set_settings({"last_checkin_time": finished})
    with _state_lock:
        _current_run = None
        _last_run_summary = summary
    return summary


# ========================= 定时调度 =========================

def _fire_scheduled():
    log.info("定时任务触发，开始签到")
    run_checkin(trigger_type="schedule")


def next_run_info() -> dict:
    """返回下一次定时签到的信息（v7.1）。

    {enabled, cron, next_run, seconds_left}；next_run 为本地时间字符串或 None。
    """
    enabled = database.get_setting("schedule_enabled", "1") == "1"
    cron_expr = database.get_setting("schedule_cron", "0 7 * * *")
    info = {"enabled": enabled, "cron": cron_expr,
            "next_run": None, "seconds_left": None}
    if not enabled:
        return info
    job = scheduler.get_job("weibo_checkin")
    nxt = getattr(job, "next_run_time", None) if job else None
    if not nxt:
        return info
    info["next_run"] = nxt.strftime("%Y-%m-%d %H:%M:%S")
    try:
        delta = (nxt - datetime.now(nxt.tzinfo)).total_seconds()
        info["seconds_left"] = int(delta) if delta > 0 else 0
    except (TypeError, ValueError, OverflowError) as exc:
        log.debug("计算下次运行时间失败：%s", exc)
    return info


def _normalize_cron(expr: str) -> str:
    """兼容 5 段(标准) 或 6 段(青龙 style: 秒 分 时 日 月 周) cron。

    6 段时去掉开头的秒段，转成标准 5 段。
    """
    parts = (expr or "").strip().split()
    if len(parts) == 6:
        return " ".join(parts[1:])
    return (expr or "").strip()


def _install_topics_push_job() -> None:
    """按当前设置重建每日超话推送任务（幂等）。"""
    existing = scheduler.get_job("topics_daily_push")
    if existing:
        existing.remove()
    if database.get_setting("topics_daily_push", "0") != "1":
        return
    topics_push_cron = database.get_setting("topics_daily_push_cron", "0 8 * * *")
    fixed = _normalize_cron(topics_push_cron)
    try:
        trigger = CronTrigger.from_crontab(fixed, timezone="Asia/Shanghai")
        scheduler.add_job(_topics_daily_push_job, trigger, id="topics_daily_push",
                          name=f"每日超话总结推送 ({fixed})",
                          misfire_grace_time=300, coalesce=True)
        log.info("已配置每日超话总结推送：%s", fixed)
    except Exception as exc:
        log.error("超话推送表达式无效 %r：%s", topics_push_cron, exc)


def reload_schedule() -> None:
    """根据当前设置重建定时签到任务。

    注意：只能移除签到任务本身（weibo_checkin），不能清空全部 job，
    否则会误删 cookie_keepalive / log_purge / topics_daily_push 等维护任务。
    """
    existing = scheduler.get_job("weibo_checkin")
    if existing:
        existing.remove()

    enabled = database.get_setting("schedule_enabled", "1") == "1"
    cron_expr = database.get_setting("schedule_cron", "0 7 * * *")
    if not enabled:
        log.info("定时签到已关闭")
    else:
        try:
            fixed = _normalize_cron(cron_expr)
            if fixed != cron_expr.strip():
                log.info("检测到 6 段 cron，已转为标准 5 段：%s", fixed)
            trigger = CronTrigger.from_crontab(fixed, timezone="Asia/Shanghai")
            scheduler.add_job(_fire_scheduled, trigger, id="weibo_checkin",
                              name=f"微博定时签到 ({fixed})",
                              misfire_grace_time=300, coalesce=True)
            log.info("已配置定时签到：%s", fixed)
        except Exception as exc:
            log.error("定时表达式无效 %r：%s", cron_expr, exc)

    # 每日超话推送任务
    _install_topics_push_job()
    # Cookie 保活任务
    _install_keepalive_job()


def start_scheduler() -> None:
    """启动调度器（幂等）。"""
    if not scheduler.running:
        scheduler.start()
    # 每日清理过期日志（v7.0）
    if not scheduler.get_job("log_purge"):
        scheduler.add_job(
            _purge_logs_job, trigger=CronTrigger(hour=4, minute=30,
                                                 timezone="Asia/Shanghai"),
            id="log_purge", max_instances=1, coalesce=True,
            misfire_grace_time=3600,
        )
    reload_schedule()
    log.info("调度器已启动")


def _auto_refresh_topics_for_account(acc: dict, cookie_dict: dict,
                                         proxy_url: str | None,
                                         channel: str) -> None:
    """签到后自动刷新超话帖子缓存。

    用账号专属 Session 而不是 get_thread_session()：后者复用前会清空 cookie jar，
    这批请求换发到的新 SUB 会被抹掉，白跑一轮还拿着旧凭证继续撞接口。
    """
    try:
        log.info("🔄 自动刷新超话帖子：%s", acc.get("name"))
        session = new_account_session()
        topics = get_followed_topics(
            session, cookie_dict, channel=channel, proxy=proxy_url,
            force=False, allow_fallback=True,
        )
        count = 0
        for topic in topics:
            cid = topic.get("id")
            if not cid:
                continue
            try:
                result = fetch_topic_posts(
                    session, cookies=cookie_dict, containerid=cid,
                    channel=channel, proxy=proxy_url,
                    force=False, allow_fallback=True,
                )
                posts = result.get("posts", [])
                if posts:
                    database.set_topic_posts_cache(cid, posts)
                    count += 1
                time.sleep(0.3)
            except Exception as exc:
                log.warning("  刷新超话 %s 失败：%s", cid, safe_exc(exc))
        log.info("  ✓ 已刷新 %d/%d 个超话的帖子", count, len(topics))
        persist_refreshed_cookie(acc["id"], session, cookie_dict)
    except Exception as exc:
        log.warning("自动刷新超话帖子异常：%s", safe_exc(exc))


def persist_refreshed_cookie(account_id: int, session, base_cookie: dict) -> bool:
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
            "cookie_last_rotated_at": _now_str()}
    wrote = database.merge_account_cookie(
        account_id, new_cookie_str,
        meta=_cookie_state_meta(view, observed="ok", rotated_keys=rotated),
    )
    if wrote:
        log.info("  🔄 %s 已续期 cookie（%s）", acc.get("name", account_id), ",".join(rotated))
    return wrote


def _topics_daily_push_job() -> None:
    """每日自动推送超话总结到 TG。"""
    try:
        enabled = database.get_setting("topics_daily_push", "0") == "1"
        if not enabled:
            return
        # 获取所有去重超话
        topics = database.get_all_topics(limit=200)
        if not topics.get("items"):
            log.info("每日超话推送：无超话数据，跳过")
            return
        # 优先使用用户勾选了「推送」的超话，未勾选时回退到全部
        push_setting = database.get_setting("topics_daily_push_mode", "all")
        if push_setting == "selected":
            items = [t for t in topics["items"] if t.get("push_enabled") == 1]
            if not items:
                log.info("每日超话推送：没有勾选推送的超话，跳过")
                return
        else:
            items = topics["items"]
        items = items[:5]
        lines = []
        for t in items:
            name = t.get("name", "")
            topic_id = t.get("topic_id", "")
            if not name or not topic_id:
                continue
            # 获取帖子缓存
            cache = database.get_topic_posts_cache(topic_id)
            if not cache or not cache.get("posts"):
                continue
            posts = cache["posts"][:10]  # 最多 10 条
            text = "\n".join(
                f"[{i+1}] {p.get('user', {}).get('screen_name', '未知')}: {p.get('text', '')}"
                for i, p in enumerate(posts)
            )
            # 调用 AI 总结（使用辅助函数，不依赖 FastAPI 路由）
            from .api.topics import call_ai_summary
            result = call_ai_summary(text, topic_name=name)
            if result.get("ok"):
                lines.append(f"📊 {name}\n{result.get('summary', '')}\n")
        if lines:
            from .notifier import send_telegram
            send_telegram("\n\n".join(lines), "📊 每日超话总结")
            log.info("每日超话总结已推送")
        else:
            log.info("每日超话推送：无有效数据")
    except Exception as exc:
        log.error("每日超话推送异常: %s", safe_exc(exc))


def _purge_logs_job() -> None:
    """根据设置自动清理旧签到日志。"""
    try:
        days = int(database.get_setting("log_retention_days", "30") or 0)
    except ValueError:
        days = 30
    if days <= 0:
        return
    removed = database.purge_old_logs(days)
    if removed:
        log.info("已清理 %d 条超过 %d 天的签到日志", removed, days)


# ========================= Cookie 保活 =========================

# 每 N 轮保活掺一次超话列表请求：只戳 /api/config 的流量形状太机器化。
_KEEPALIVE_LIST_EVERY = 6
_keepalive_ticks: dict[int, int] = {}


def _keepalive_one(acc: dict) -> dict:
    """对单个账号做一轮续期探测，返回 {name, health, rotated, reason}。调用方持账号锁。"""
    name = acc.get("name", "未命名账号")
    raw_cookie = acc.get("cookie") or acc.get("cookie_raw") or ""
    cookie_dict = normalize_cookie(raw_cookie)
    if not cookie_dict:
        database.mark_cookie_dead(acc["id"])
        return {"name": name, "id": acc["id"], "health": "invalid",
                "rotated": False, "reason": "Cookie 为空，请重新登录"}

    proxy_url = acc.get("proxy") or None
    channel = "socks" if proxy_url else "direct"
    opts = CheckinOptions.from_settings(database.get_setting)
    session = new_account_session()

    ticks = _keepalive_ticks.get(acc["id"], 0) + 1
    _keepalive_ticks[acc["id"]] = ticks
    try:
        logged_in, _st = verify_cookie(
            session, cookie_dict, channel=channel, proxy=proxy_url,
            force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
        )
        if logged_in and ticks % _KEEPALIVE_LIST_EVERY == 0:
            # 掺一个真实业务端点；它同样会换发 cookie，由下面的统一回写收走
            get_followed_topics(
                session, cookie_dict, channel=channel, proxy=proxy_url,
                force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
            )
    except RateLimitedError as exc:
        _record_failure_streak(acc["id"], "rate_limited")
        return {"name": name, "id": acc["id"], "health": "rate_limited",
                "rotated": False, "reason": safe_exc(exc)}
    except (CookieExpiredError, NetworkError) as exc:
        # 列表接口会拿 -100 明确告知登录态没了，按失效处理而不是退避重试
        if isinstance(exc, CookieExpiredError):
            database.mark_cookie_dead(acc["id"])
            return {"name": name, "id": acc["id"], "health": "invalid",
                    "rotated": False, "reason": safe_exc(exc)}
        _record_failure_streak(acc["id"], "network_error")
        return {"name": name, "id": acc["id"], "health": "network_error",
                "rotated": False, "reason": safe_exc(exc)}
    except Exception as exc:
        _record_failure_streak(acc["id"], "network_error")
        return {"name": name, "id": acc["id"], "health": "network_error",
                "rotated": False, "reason": safe_exc(exc)}

    if not logged_in:
        lifetime = database.mark_cookie_dead(acc["id"])
        log.info("🔐 保活发现 %s 登录态已失效（实测寿命 %d 天）", name, lifetime)
        return {"name": name, "id": acc["id"], "health": "invalid",
                "rotated": False, "reason": "Cookie 无效或已过期，请重新登录"}

    merged, changed, added = harvest_cookie_rotation(session, cookie_dict)
    result = {
        "status": "success", "message": "保活通过", "total": 0, "success": 0,
        "fail": 0, "results": [], "cookie": merged, "cookie_changed": changed,
        "cookie_added": added, "channel": channel, "failure_type": None,
    }
    fresh = {**acc, "cookie": cookie_to_string(merged),
             "cookie_raw": cookie_to_string(merged)}
    persist_cookie_observation(fresh, raw_cookie, cookie_dict, result)
    merged_str = cookie_to_string(merged)
    shrunk = reject_cookie_shrink(cookie_dict, normalize_cookie(merged_str))
    rotated = bool(cookie_diff_gate(changed, added)) and not shrunk
    stored = database.get_account(acc["id"]) or {}
    # 直接用刚写库的观测结果，避免面板徽章和 cookie_health 列对不上
    health = stored.get("cookie_health") or resolve_cookie_health("ok", "unknown")
    reason = (",".join(dict.fromkeys(changed + added)) if rotated
              else (shrunk or "登录态有效"))
    return {"name": name, "id": acc["id"], "health": health,
            "rotated": rotated, "reason": reason}


def _within_checkin_window(minutes: int = 15) -> bool:
    """临近定时签到时刻就让路，避免同一 IP 上叠加认证请求。"""
    job = scheduler.get_job("weibo_checkin")
    next_run = getattr(job, "next_run_time", None) if job else None
    if next_run is None:
        return False
    now = datetime.now(next_run.tzinfo) if next_run.tzinfo else datetime.now()
    return timedelta(0) <= next_run - now <= timedelta(minutes=minutes)


def _keepalive_job() -> None:
    """定时保活 tick：只处理到期的账号，任何异常都不外泄。"""
    if not _keepalive_lock.acquire(blocking=False):
        return
    try:
        if database.get_setting("keepalive_enabled", "1") != "1":
            return
        if _running_lock.locked():
            log.info("签到进行中，本轮保活让路")
            return
        if _within_checkin_window():
            log.info("临近定时签到，本轮保活让路")
            return
        run_keepalive_once()
    except Exception as exc:
        log.error("保活任务异常：%s", safe_exc(exc))
    finally:
        _keepalive_lock.release()


def run_keepalive_once(account_ids: list[int] | None = None, *,
                       force: bool = False) -> dict:
    """执行一轮 cookie 保活；force=True 时忽略开关、到期时间与签到窗口。

    返回 {checked, rotated, alive, invalid, network, skipped, changes}。
    """
    stats = {"checked": 0, "rotated": 0, "alive": 0, "invalid": 0,
             "network": 0, "skipped": 0, "changes": [], "reason": ""}
    if not force:
        if database.get_setting("keepalive_enabled", "1") != "1":
            stats["reason"] = "disabled"
            return stats
        if _running_lock.locked():
            stats["reason"] = "checkin_running"
            return stats
        if _within_checkin_window():
            stats["reason"] = "near_scheduled_checkin"
            return stats

    if account_ids:
        accounts = [a for a in (database.get_account(i) for i in account_ids) if a]
        accounts = [a for a in accounts if a.get("enabled", 1)]
    else:
        accounts = database.list_keepalive_due(
            limit=_int_setting("keepalive_max_per_tick", 20))
    if not accounts:
        return stats

    gap = _int_setting("keepalive_gap_seconds", 30)
    previous = {a["id"]: (a.get("cookie_health") or "unknown") for a in accounts}
    for index, acc in enumerate(accounts):
        if index and gap > 0:
            time.sleep(gap)
        lock = get_account_lock(acc["id"])
        # 账号正被签到流程占住时跳过，而不是把调度线程挂在这里
        if not lock.acquire(timeout=60):
            stats["skipped"] += 1
            continue
        try:
            outcome = _keepalive_one(acc)
        except Exception as exc:
            log.exception("账号 %s 保活异常", acc.get("name"))
            outcome = {"name": acc.get("name", ""), "id": acc["id"],
                       "health": "network_error", "rotated": False,
                       "reason": safe_exc(exc)}
        finally:
            lock.release()
        stats["checked"] += 1
        if outcome["rotated"]:
            stats["rotated"] += 1
        if outcome["health"] == "invalid":
            stats["invalid"] += 1
        elif outcome["health"] in ("network_error", "rate_limited"):
            stats["network"] += 1
        else:
            stats["alive"] += 1
        if previous.get(acc["id"]) != outcome["health"]:
            stats["changes"].append(outcome)

    if stats["changes"] and database.get_setting("tg_enabled", "0") == "1":
        try:
            notifier.send_cookie_warning(stats["changes"])
        except Exception as exc:
            log.warning("保活预警推送失败：%s", safe_exc(exc))
    log.info("保活完成：检查 %d，续期 %d，有效 %d，失效 %d，网络/风控 %d",
             stats["checked"], stats["rotated"], stats["alive"],
             stats["invalid"], stats["network"])
    return stats


def _install_keepalive_job() -> None:
    """按当前设置重建保活任务（幂等，仿 _install_topics_push_job）。"""
    existing = scheduler.get_job("cookie_keepalive")
    if existing:
        existing.remove()
    if database.get_setting("keepalive_enabled", "1") != "1":
        log.info("Cookie 保活已关闭")
        return
    scheduler.add_job(
        _keepalive_job, IntervalTrigger(minutes=30, timezone="Asia/Shanghai"),
        id="cookie_keepalive", name="Cookie 保活续期",
        max_instances=1, coalesce=True, misfire_grace_time=120,
    )
    log.info("已启用 Cookie 保活（每 30 分钟检查一次到期账号）")



def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("调度器已停止")
