"""
Telegram 通知推送模块。
支持通过 Bot Token + Chat ID 发送文本消息，并记录到数据库。
"""
from __future__ import annotations

import logging

import requests

from . import database
from . import hitokoto
from .security import safe_exc

log = logging.getLogger("weibo.notifier")

TELEGRAM_API = "https://api.telegram.org"


def _get_tg_config() -> tuple[str, str, bool]:
    token = database.get_setting("tg_bot_token", "").strip()
    user_id = database.get_setting("tg_user_id", "").strip()
    enabled = database.get_setting("tg_enabled", "0") == "1"
    return token, user_id, enabled


def send_telegram(text: str, title: str = "微博签到") -> bool:
    """发送 TG 消息。成功返回 True，失败记录到数据库并返回 False。"""
    token, user_id, enabled = _get_tg_config()
    if not enabled:
        log.info("TG 推送未启用，跳过")
        return False
    if not token or not user_id:
        error = "TG 未配置 Bot Token 或 Chat ID"
        database.add_notification(title, text, False, error)
        log.warning(error)
        return False

    full = f"{title}\n{text}" if title and not text.startswith(title) else text
    silent = database.get_setting("tg_silent", "0") == "1"
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            data={
                "chat_id": user_id,
                "text": full,
                "disable_web_page_preview": True,
                "disable_notification": "true" if silent else "false",
            },
            timeout=20,
        )
        try:
            ok = resp.status_code == 200 and resp.json().get("ok") is True
        except ValueError:
            ok = False
        error = "" if ok else f"HTTP {resp.status_code} {resp.text[:200]}"
        database.add_notification(title, text, ok, error)
        return ok
    except Exception as exc:
        # 连接类异常消息含 /bot<TOKEN>/ 完整 URL，脱敏后再入库/日志
        database.add_notification(title, text, False, safe_exc(exc))
        log.error("TG 推送异常：%s", safe_exc(exc))
        return False


def _topic_with_rank(result: dict) -> str:
    rank = result.get("rank")
    name = result.get("name", "未知超话")
    return f"{name}（第{rank}名）" if isinstance(rank, int) and rank > 0 else name


# 需要主动预警的 cookie 健康度（network_error 属代理抖动，累计多次才提示，不在这张表里）
_ALERT_HEALTHY = {"invalid", "expired", "expiring", "rate_limited"}
_HEALTH_LABEL = {
    "invalid": "🔐 已失效",
    "expired": "⏰ 已到预计失效时间",
    "expiring": "⚠️ 即将失效",
    "rate_limited": "🚧 被风控",
    "network_error": "📡 网络不通",
    "ok": "✅ 正常",
    "stale": "💤 保活未运行",
    "unknown": "ℹ️ 未知",
}


def _warn_cooldown_passed(acc: dict, cooldown_hours: float) -> bool:
    from datetime import datetime, timedelta
    warned = str(acc.get("cookie_warned_at") or "").strip()
    if not warned:
        return True
    try:
        last = datetime.strptime(warned[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return datetime.now() - last >= timedelta(hours=cooldown_hours)


def send_cookie_warning(changes: list[dict]) -> bool:
    """保活轮里的状态变化预警（提前于"签不上"通知用户重新登录）。

    用 cookie_warned_at 做每账号去重：保活每 30 分钟一轮，不去重会把 TG 刷爆。
    """
    items = [c for c in changes if c.get("health") in _ALERT_HEALTHY]
    if not items:
        return False
    cooldown = 12.0
    try:
        cooldown = float(database.get_setting("cookie_warn_cooldown_hours", "12") or 12)
    except (TypeError, ValueError):
        pass

    lines = []
    alerted = []
    for item in items:
        acc = database.get_account(item.get("id")) or {}
        if not _warn_cooldown_passed(acc, cooldown):
            continue
        reason = item.get("reason") or ""
        rotated = acc.get("cookie_last_rotated_at") or "从未"
        expire_hint = acc.get("cookie_expires_at") or ""
        text = f"{_HEALTH_LABEL.get(item['health'], item['health'])} {item.get('name', '未知账号')}｜{reason}"
        lines.append(text)
        lines.append(f"   上次续期 {rotated}" + (f"，ALF 名义到期 {expire_hint}" if expire_hint else ""))
        database.set_cookie_state(item["id"], cookie_warned_at=database._now())
        alerted.append(item)

    if not lines:
        log.info("Cookie 预警处于冷却期，跳过推送")
        return False
    lines.insert(0, "若账号已失效，请重新扫码或粘贴新 Cookie；未失效的会继续自动续期。")
    lines.insert(0, f"🍪 Cookie 状态变化 {len(alerted)} 个账号")
    return send_telegram("\n".join(lines), "🍪 微博 Cookie 预警")


def send_checkin_report(task_summary: dict) -> bool:
    """发送一份签到汇总报告。task_summary 由调度器构建。"""
    accounts = task_summary.get("detail", [])
    cookie_invalid_accounts = [
        a.get("name", "未知账号")
        for a in accounts
        if a.get("failure_type") == "cookie_invalid"
    ]
    all_ok = task_summary.get("fail", 0) == 0 and not any(
        a.get("status") == "failed" for a in accounts
    ) and not cookie_invalid_accounts

    # 仅异常推送模式：全部成功时不打扰
    if all_ok and database.get_setting("tg_only_on_change", "0") == "1":
        log.info("TG 仅异常推送已开启且本次全部成功，跳过推送")
        return False

    total = task_summary.get("total", 0)
    success = task_summary.get("success", 0)
    fail = task_summary.get("fail", 0)
    rate = round(success / total * 100) if total else 0
    trigger = task_summary.get("trigger_type", "")
    trigger_label = {"schedule": "⏰ 定时触发", "manual": "👆 手动触发"}.get(trigger, "")

    lines = [
        "🕒 " + task_summary.get("time", ""),
    ]
    if trigger_label:
        lines.append(trigger_label)
    lines += [
        "━━━━━━━━━━━━━━━━━━",
        f"👤 账号：{task_summary.get('accounts', 0)} 个",
        f"📋 超话：{total} 个 ｜ ✅ 成功：{success} ｜ ❌ 失败：{fail}",
        f"📈 成功率：{rate}%  {_progress_bar(rate)}",
    ]
    if cookie_invalid_accounts:
        lines += [
            "",
            "🔐 Cookie 已失效：" + "、".join(cookie_invalid_accounts),
            "请重新登录上述账号并更新 Cookie。",
        ]
    for idx, acc in enumerate(accounts, start=1):
        icon = {"success": "✅", "partial": "⚠️", "failed": "❌"}.get(
            acc.get("status"), "ℹ️"
        )
        signed = [_topic_with_rank(r)
                  for r in acc.get("results", [])
                  if r.get("success") and r.get("message") != "今日已签到"]
        already = [r.get("name") for r in acc.get("results", [])
                   if r.get("success") and r.get("message") == "今日已签到"]
        fails = [r for r in acc.get("results", []) if not r.get("success")]
        lines.append("")
        lines.append(f"{icon} {idx}. {acc.get('name', '未知')}")
        if acc.get("failure_type") == "cookie_invalid":
            lines.append("   🔐 Cookie 已失效，请重新登录并更新 Cookie")
        if acc.get("cookie_rotated_at"):
            lines.append(f"   🔄 上次续期 {acc['cookie_rotated_at']}")
        if signed:
            lines.append("   🎉 本次签到：" + "、".join(signed))
        if already:
            lines.append("   ☑️ 今日已签：" + "、".join(already))
        if fails:
            lines.append("   ❌ 失败：" + "、".join(
                f"{r.get('name')}（{r.get('message', '未知')}）" for r in fails))
        if not acc.get("results"):
            lines.append(f"   ℹ️ {acc.get('message', '')}")

    # 每日一言（可关闭）
    if database.get_setting("tg_quote_enabled", "1") == "1":
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(hitokoto.format_quote())

    title = "✅ 微博超话签到完成" if all_ok else "⚠️ 微博超话签到有异常"
    return send_telegram("\n".join(lines), title)


def _progress_bar(percent: int, width: int = 10) -> str:
    """用方块画一个简单进度条。"""
    filled = max(0, min(width, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def send_text_message(text: str) -> bool:
    """发送一条自定义 TG 消息（测试用）。"""
    if database.get_setting("tg_quote_enabled", "1") == "1":
        text = text + "\n\n" + hitokoto.format_quote()
    return send_telegram(text, "微博签到")


def send_daily_quote() -> bool:
    """单独推送一条每日一言。"""
    return send_telegram(hitokoto.format_quote(), "🌟 每日一言")
