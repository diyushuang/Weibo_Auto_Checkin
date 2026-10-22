# -*- coding: utf-8 -*-
"""超话列表抓取与缓存的唯一实现（面板 API 与 mini CLI 共用）。"""
from __future__ import annotations

import logging

from . import cookie_lifecycle, database
from .security import safe_exc
from .weibo_client import (
    CheckinOptions,
    get_followed_topics,
    new_account_session,
    normalize_cookie,
)

log = logging.getLogger("weibo.topics_service")


def refresh_topics_for_account(acc: dict) -> dict:
    """抓取账号关注超话 → 回收换发 Cookie → 写账号缓存 → 合并去重表。

    这是全项目唯一的"超话列表抓取"实现；/refresh、/refresh_all、签到后的
    自动刷新与 mini CLI 都必须走这里，禁止再复制循环。

    调用方若与签到/保活并发操作同一账号，需自行持有账号锁
    （cookie_lifecycle.get_account_lock；签到流程内已持有，勿重复获取）。

    返回 {ok, count, topics, error}。
    """
    account_id = acc["id"]
    cookie = normalize_cookie(acc.get("cookie") or acc.get("cookie_raw") or "")
    if not cookie:
        return {"ok": False, "count": 0, "topics": [], "error": "账号 Cookie 为空"}

    opts = CheckinOptions.from_settings(database.get_setting)
    proxy = (acc.get("proxy") or "").strip() or None
    channel = "socks" if proxy else "direct"
    session = new_account_session()

    try:
        topics = get_followed_topics(
            session, cookie, channel=channel, proxy=proxy,
            force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
        )
    except Exception as exc:
        # 失败也不能丢掉本次响应里可能换发的登录凭据
        try:
            cookie_lifecycle.persist_refreshed_cookie(
                account_id, session, cookie, source="topics")
        except Exception:
            pass
        log.warning("账号 %s 获取超话列表失败：%s", acc.get("name", ""), safe_exc(exc))
        return {"ok": False, "count": 0, "topics": [], "error": safe_exc(exc)}

    cookie_lifecycle.persist_refreshed_cookie(
        account_id, session, cookie, source="topics")

    for t in topics:
        if t.get("id"):
            t["url"] = f"https://weibo.com/page/{t['id']}"

    database.set_topic_cache(account_id, topics)

    # 合并进全量去重表；upsert 只覆盖非空字段，刷新不会冲掉已有名称/头像/推送勾选
    for t in topics:
        cid = t.get("id", "")
        if not cid:
            continue
        new_name = (t.get("name") or "").strip()
        if not new_name and not database.get_all_topic(cid):
            continue
        database.upsert_all_topic(
            topic_id=cid,
            name=new_name,
            topic_url=f"https://weibo.com/page/{cid}",
            avatar_url=t.get("avatar", ""),
            description=t.get("description", ""),
            member_count=t.get("member_count", 0),
        )

    return {"ok": True, "count": len(topics), "topics": topics, "error": ""}
