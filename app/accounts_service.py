# -*- coding: utf-8 -*-
"""账号 upsert 与账号行解析的唯一实现（面板 API 与 mini CLI 共用）。

所有"把一份微博 Cookie 落成账号"的路径（扫码完成、粘贴创建、JSON 导入、
批量导入、CLI import、legacy 迁移）都必须经过 upsert_account，禁止再各自
实现查找/去重/新增逻辑。
"""
from __future__ import annotations

from . import database
from .weibo_client import normalize_cookie


def _cookie_field(cookie: str, key: str) -> str:
    raw = str(cookie or "")
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == key:
            return v.strip()
    return ""


def find_existing_account(cookie: str = "", weibo_uid: str = "",
                          name: str | None = None) -> dict | None:
    """查找同属一个微博号的已有账号：优先 weibo_uid，其次 SUB，可选名称兜底。"""
    sub = _cookie_field(cookie, "SUB")
    for acc in database.get_accounts():
        if weibo_uid and acc.get("weibo_uid") and str(acc["weibo_uid"]) == str(weibo_uid):
            return acc
        if sub:
            old_sub = _cookie_field(acc.get("cookie") or acc.get("cookie_raw") or "", "SUB")
            if old_sub and old_sub == sub:
                return acc
    if name:
        for acc in database.get_accounts():
            if acc.get("name", "") == name:
                return acc
    return None


def upsert_account(cookie: str, *, name: str = "", proxy: str = "",
                   enabled: bool | None = None, remark: str = "",
                   avatar_url: str = "", weibo_uid: str = "",
                   default_name: str = "", match_name: str | None = None,
                   skip_if_name_exists: bool = False) -> tuple[dict | None, str]:
    """把一份 cookie 落成账号；返回 (账号, action)。

    action ∈ {"created", "updated", "skipped"}；skipped 仅在
    skip_if_name_exists 且名称已存在时出现（JSON 导入的 overwrite=False 语义）。

    - 匹配优先级：weibo_uid → SUB → match_name（调用方显式开启名称匹配）
    - 更新分支只覆盖非空可选字段与显式传入的 enabled；cookie 永远覆盖
      （database.update_account 会自动重置生命周期基准）
    - 创建分支：name 为空时依次用 default_name、"未命名账号"
    """
    cookie = (cookie or "").strip()
    if not cookie:
        raise ValueError("cookie 为空")
    existing = find_existing_account(cookie=cookie, weibo_uid=weibo_uid)
    if existing is None and skip_if_name_exists and name:
        existing = find_existing_account(name=name)
        if existing is not None:
            return None, "skipped"
    if existing is None and match_name:
        existing = find_existing_account(name=match_name)

    if existing is not None:
        upd: dict = {"cookie": cookie, "cookie_raw": cookie}
        if name.strip():
            upd["name"] = name.strip()
        if proxy:
            upd["proxy"] = proxy
        if remark:
            upd["remark"] = remark
        if avatar_url:
            upd["avatar_url"] = avatar_url
        if weibo_uid:
            upd["weibo_uid"] = str(weibo_uid)
        if enabled is not None:
            upd["enabled"] = enabled
        database.update_account(existing["id"], upd)
        return database.get_account(existing["id"]), "updated"

    acc_id = database.add_account({
        "name": name.strip() or default_name or "未命名账号",
        "cookie": cookie,
        "cookie_raw": cookie,
        "enabled": True if enabled is None else bool(enabled),
        "proxy": proxy or "",
        "remark": remark or "",
        "avatar_url": avatar_url or "",
        "weibo_uid": str(weibo_uid or ""),
    })
    return database.get_account(acc_id), "created"


def parse_account_line(line: str, index: int = 1) -> dict | None:
    """解析账号文件的一行；空行/注释返回 None，格式错误抛 ValueError。

    支持三种格式：
        SUB=xxx; SCF=xxx                       → 纯 cookie
        名字|SUB=xxx; SCF=xxx                  → 名字|cookie
        名字|socks5://host:1080|SUB=xxx; ...   → 名字|代理|cookie
    """
    raw = (line or "").strip()
    if not raw or raw.startswith("#"):
        return None
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) == 1:
        name, proxy, cookie = "", "", parts[0]
    elif len(parts) == 2 and parts[0]:
        name, proxy, cookie = parts[0], "", parts[1]
    elif len(parts) >= 3:
        name, proxy, cookie = parts[0], parts[1], "|".join(parts[2:])
    else:
        name, proxy, cookie = "", "", raw
    if "=" not in cookie:
        raise ValueError(f"第 {index} 行 cookie 格式无效（缺少 key=value）")
    return {"name": name or f"账号{index}", "proxy": proxy, "cookie": cookie}
