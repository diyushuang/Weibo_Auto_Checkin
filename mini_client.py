#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简微博超话签到客户端（DB-backed CLI，与 WebUI 完全同源）。

账号、Cookie 生命周期、保活设置、超话缓存全部存放在面板的 SQLite 数据库
里；签到/保活直接复用面板调度器（防封、代理分组并行、日志、TG 通知），
超话抓取与登录导入走共享服务层。适合：

  - 服务器 cron 定时跑（与面板同库共存，Cookie 写入有 CAS 保护）
  - 青龙面板（挂仓库目录后 task mini_client.py checkin）
  - 不想开面板时的纯命令行使用

常用命令（默认库 data/weibo_checkin.db，可用 --db 指定其它库）：
    python mini_client.py checkin                 # 全部启用账号签到一轮
    python mini_client.py keepalive --force       # 立即保活所有启用账号
    python mini_client.py keepalive               # 只保活到期账号
    python mini_client.py import                  # 导入 mini_accounts.txt
    python mini_client.py topics list             # 查看超话缓存
    python mini_client.py topics refresh          # 抓取超话列表并更新缓存
    python mini_client.py rotate-probe            # 实验性：探测 PC 端点能否换发 Cookie
    python mini_client.py checkin --json          # 机器可读输出

账号文件（mini_accounts.txt，# 注释，每行一个账号）：
    SUB=xxx; SCF=xxx                        # 纯 cookie
    小号A|SUB=xxx; SCF=xxx                  # 名字|cookie
    小号B|socks5://127.0.0.1:1080|SUB=xxx   # 名字|代理|cookie

旧版 mini_state.json 会在首次运行时自动迁移进数据库并改名 .migrated。
退出码：0=成功/部分成功/网络抖动；1=有账号失败或登录失效；2=配置错误。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from app import accounts_service, cookie_lifecycle, database, topics_service
from app import scheduler as sched
from app import weibo_client as wb
from app.security import safe_exc, validate_weibo_url

DEFAULT_DB = "data/weibo_checkin.db"
DEFAULT_IMPORT = "mini_accounts.txt"
DEFAULT_LEGACY = "mini_state.json"

log = logging.getLogger("mini")


# ========================= 初始化与迁移 =========================

def _init_db(path: str) -> None:
    database.DB_PATH = Path(path)
    database._local.conn = None
    database.init_db()


def _maybe_migrate(legacy: Path) -> None:
    """把旧版 mini_state.json 里的账号迁入数据库（一次性，成功后改名备份）。"""
    if not legacy.exists():
        return
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[migrate] 无法读取 {legacy}：{exc}，跳过迁移")
        return
    migrated = 0
    for name, st in (data.get("accounts") or {}).items():
        cookie = (st.get("cookie") or st.get("file_cookie") or "").strip()
        if not cookie:
            continue
        try:
            accounts_service.upsert_account(
                cookie, name=name, proxy=st.get("proxy", ""),
                remark="旧版迁移", match_name=name)
            migrated += 1
        except Exception as exc:
            print(f"[migrate] 账号 {name} 迁移失败：{exc}")
    backup = legacy.with_name(legacy.name + ".migrated")
    try:
        legacy.rename(backup)
    except OSError as exc:
        print(f"[migrate] 迁移完成但改名失败：{exc}")
    if migrated:
        print(f"[migrate] 已迁移 {migrated} 个账号到数据库（备份：{backup.name}）")
    else:
        print(f"[migrate] 旧状态无可迁移账号，已改名 {backup.name}")


# ========================= 账号筛选 =========================

def _resolve_account_ids(args: argparse.Namespace) -> list[int]:
    """把 --account（id 或名称，可多次）解析成账号 id 列表。"""
    selectors = getattr(args, "account", None) or []
    if not selectors:
        return []
    ids: list[int] = []
    for sel in selectors:
        acc = None
        if str(sel).isdigit():
            acc = database.get_account(int(sel))
        if acc is None:
            acc = next((a for a in database.get_accounts()
                        if a.get("name") == str(sel)), None)
        if acc is None:
            raise SystemExit(f"账号不存在：{sel}")
        ids.append(acc["id"])
    return ids


def _target_accounts(args: argparse.Namespace, enabled_only: bool = False):
    ids = _resolve_account_ids(args)
    if ids:
        accounts = [database.get_account(i) for i in ids]
        return [a for a in accounts if a]
    accounts = database.get_enabled_accounts() if enabled_only else database.get_accounts()
    return accounts


# ========================= 命令：import =========================

def cmd_import(args: argparse.Namespace) -> int:
    added = updated = skipped = 0
    if getattr(args, "cookie", None):
        items = [{"name": (args.name or "cli").strip(), "proxy": "",
                  "cookie": args.cookie.strip()}]
    else:
        path = Path(args.file)
        if not path.exists():
            print(f"账号文件不存在：{path}\n"
                  f"可复制 mini_accounts.example.txt 为 {DEFAULT_IMPORT} 后填写，"
                  f"或用 --cookie 直接导入单个账号。")
            return 2
        items = []
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                parsed = accounts_service.parse_account_line(line, i)
            except ValueError as exc:
                print(f"  [跳过] {exc}")
                skipped += 1
                continue
            if parsed:
                items.append(parsed)

    for item in items:
        try:
            _acc, action = accounts_service.upsert_account(
                item["cookie"],
                name=item["name"],
                proxy=item.get("proxy", ""),
                remark="CLI 导入",
                match_name=item["name"],
            )
        except Exception as exc:
            print(f"  [失败] {item.get('name', '?')}：{exc}")
            skipped += 1
            continue
        if action == "created":
            added += 1
            print(f"  [新增] {item['name']}")
        elif action == "updated":
            updated += 1
            print(f"  [更新] {item['name']}")
        else:
            skipped += 1
    print(f"导入完成：新增 {added}，更新 {updated}，跳过 {skipped}")
    return 0 if skipped == 0 else (0 if added or updated else 2)


# ========================= 命令：checkin / keepalive =========================

def cmd_checkin(args: argparse.Namespace) -> int:
    ids = _resolve_account_ids(args)
    summary = sched.run_checkin("cli", account_ids=ids or None)
    entries = summary.get("detail") or []
    # 面板的批状态只统计超话粒度：账号整体失败（如 cookie_invalid，total=0）
    # 时批状态仍是 success。CLI 退出码必须看账号级明细。
    failed = [e for e in entries if e.get("status") == "failed"]
    if getattr(args, "json", False):
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 1 if failed else 0
    print(f"== 签到完成（{summary.get('status')}）==")
    for entry in entries:
        line = f"  [{entry.get('status', '?')}] {entry.get('name', '?')}"
        if entry.get("message"):
            line += f" - {entry['message']}"
        line += f"（成功 {entry.get('success', 0)}/{entry.get('total', 0)}）"
        print(line)
    print("总体：", summary.get("status"),
          f"（失败账号 {len(failed)} 个）" if failed else "")
    return 1 if failed else 0


def cmd_keepalive(args: argparse.Namespace) -> int:
    if args.force:
        ids = [a["id"] for a in _target_accounts(args, enabled_only=True)]
    else:
        ids = _resolve_account_ids(args) or None
    stats = sched.run_keepalive_once(ids, force=args.force)
    if getattr(args, "json", False):
        print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
        return 1 if stats.get("invalid") else 0
    print("== 保活完成 ==")
    print(f"  检查 {stats.get('checked', 0)}，续期 {stats.get('rotated', 0)}，"
          f"有效 {stats.get('alive', 0)}，失效 {stats.get('invalid', 0)}，"
          f"网络/风控 {stats.get('network', 0)}，跳过 {stats.get('skipped', 0)}")
    for change in stats.get("changes") or []:
        print(f"  [{change.get('health', '?')}] {change.get('name', '?')} - "
              f"{change.get('reason', '')}")
    if stats.get("reason") and not stats.get("checked"):
        print(f"  （未执行：{stats['reason']}）")
    return 1 if stats.get("invalid") else 0


# ========================= 命令：topics =========================

def cmd_topics(args: argparse.Namespace) -> int:
    if args.topics_cmd == "list":
        accounts = _target_accounts(args)
        if not accounts:
            print("没有账号。先用 import 导入或在面板扫码登录。")
            return 0
        for acc in accounts:
            cache = database.get_topic_cache(acc["id"]) or {}
            topics = cache.get("topics") or []
            print(f"== {acc.get('name', acc['id'])}"
                  f"（{len(topics)} 个超话，更新于 {cache.get('cached_at') or '从未'}）==")
            for t in topics:
                mark = "✓" if t.get("done") else "·"
                extra = f"（{t.get('member_count')} 人）" if t.get("member_count") else ""
                print(f"  {mark} {t.get('name', '?')}{extra}")
        return 0

    # refresh
    accounts = _target_accounts(args, enabled_only=True)
    if not accounts:
        print("没有可刷新的启用账号。")
        return 0
    failures = 0
    for acc in accounts:
        if not (acc.get("cookie") or acc.get("cookie_raw") or "").strip():
            print(f"  [跳过] {acc.get('name', acc['id'])}：Cookie 为空")
            continue
        with cookie_lifecycle.get_account_lock(acc["id"]):
            result = topics_service.refresh_topics_for_account(acc)
        if result["ok"]:
            print(f"  [OK] {acc.get('name', acc['id'])}：{result['count']} 个超话")
        else:
            failures += 1
            print(f"  [失败] {acc.get('name', acc['id'])}：{result['error']}")
    return 0 if failures == 0 else 1


# ========================= 命令：rotate-probe（v1.3.6 实验性） =========================

def _probe_candidate_urls(args: argparse.Namespace) -> list[str]:
    if getattr(args, "urls", None):
        return [u.strip() for u in str(args.urls).split(",") if u.strip()]
    return [u.strip() for u in (database.get_setting(
        "keepalive_passport_urls",
        "https://weibo.com/,https://passport.weibo.com/") or "").split(",")
        if u.strip()]


def cmd_rotate_probe(args: argparse.Namespace) -> int:
    """实验性：逐个 PC 端点探测微博是否静默换发新凭据。

    用途：验证 keepalive_passport_enabled 实验功能对当前账号是否有效。
    每个端点用独立 session 请求一次，harvest 出 Set-Cookie 变化并回写
    数据库（CAS 保护，与面板/保活并发安全）；换发成功的端点会在结尾
    汇总成 keepalive_passport_urls 的配置建议。
    """
    urls = _probe_candidate_urls(args)
    if not urls:
        print("没有可探测的端点。用 --urls \"https://weibo.com/,…\" 或先设置 keepalive_passport_urls。")
        return 2
    if getattr(args, "all", False):
        accounts = database.get_enabled_accounts()
    else:
        ids = _resolve_account_ids(args)
        if ids:
            accounts = [database.get_account(i) for i in ids if database.get_account(i)]
        else:
            # 默认只探测临期账号：健康账号保活本身就续得上，探测是浪费请求
            accounts = [a for a in database.get_enabled_accounts()
                        if cookie_lifecycle.cookie_expiry_view(a).get("state")
                        in ("expiring", "expired")]
    if not accounts:
        print("没有需要探测的账号（健康账号保活即可续期；可用 --account / --all 指定）。")
        return 0

    opts = wb.CheckinOptions.from_settings(database.get_setting)
    dead = 0
    for acc in accounts:
        name = acc.get("name", acc["id"])
        raw_cookie = acc.get("cookie") or acc.get("cookie_raw") or ""
        cookie_dict = wb.normalize_cookie(raw_cookie)
        print(f"== rotate-probe：{name} ==")
        if not cookie_dict:
            print("  Cookie 为空，无法探测（请重新扫码导入）")
            dead += 1
            continue
        channel = "socks" if acc.get("proxy") else "direct"
        proxy_url = acc.get("proxy") or None
        try:
            session = wb.new_account_session()
            logged_in, _st = wb.verify_cookie(
                session, cookie_dict, channel=channel, proxy=proxy_url,
                force=opts.proxy_force, allow_fallback=opts.proxy_fallback)
        except Exception as exc:
            print(f"  探活请求失败：{safe_exc(exc)}（代理/网络抖动，稍后再试）")
            continue
        if not logged_in:
            print("  登录态已失效：旧凭据换不出新 Cookie，请扫码续期")
            dead += 1
            continue
        winners = []
        for i, url in enumerate(urls, 1):
            try:
                validate_weibo_url(url)
            except Exception as exc:
                print(f"  [{i}/{len(urls)}] {url}\n      跳过：非法端点（{safe_exc(exc)}）")
                continue
            probe_session = wb.new_account_session()
            try:
                wb.request_via_proxy(
                    probe_session, "GET", url, channel=channel, proxy=proxy_url,
                    force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
                    timeout=15, cookies=cookie_dict,
                    headers={"Referer": wb.BASE_PC + "/"},
                )
            except Exception as exc:
                print(f"  [{i}/{len(urls)}] {url}\n      请求失败：{safe_exc(exc)}")
                continue
            merged, changed, added = wb.harvest_cookie_rotation(probe_session, cookie_dict)
            if not (changed or added):
                print(f"  [{i}/{len(urls)}] {url}\n      无 Set-Cookie 变化")
                continue
            keys = ", ".join(dict.fromkeys(changed + added))
            print(f"  [{i}/{len(urls)}] {url}\n      换发：{keys}")
            with cookie_lifecycle.get_account_lock(acc["id"]):
                wrote = cookie_lifecycle.persist_refreshed_cookie(
                    acc["id"], probe_session, cookie_dict, source="rotate-probe")
            if wrote:
                print("      ✅ 新凭据已回写数据库")
                winners.append(url)
                fresh = database.get_account(acc["id"])
                if fresh:
                    cookie_dict = wb.normalize_cookie(
                        fresh.get("cookie") or fresh.get("cookie_raw") or "")
        if winners:
            print(f"  建议：把 {'、'.join(winners)} 保留在设置 keepalive_passport_urls，其余可删")
        else:
            print("  所有端点均未换发新凭据：PC 端点对该账号无效，扫码续期仍是唯一手段")
    return 1 if dead else 0


# ========================= 命令行入口 =========================

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=DEFAULT_DB,
                        help=f"SQLite 数据库路径（默认 {DEFAULT_DB}，与面板同库）")
    common.add_argument("--account", action="append", metavar="ID|名称",
                        help="限定账号（可多次）")
    common.add_argument("--legacy-state", default=DEFAULT_LEGACY,
                        help=f"旧版 JSON 状态路径（默认 {DEFAULT_LEGACY}，自动迁移）")

    # 注意：--db 等公共选项只挂在子命令上，避免 argparse 父子解析器同名
    # 选项互相覆盖默认值的经典陷阱（全局值会被子命令默认值冲掉）。
    parser = argparse.ArgumentParser(
        prog="mini_client.py",
        description="极简微博超话签到客户端（与面板共享数据库与全部业务逻辑）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("checkin", parents=[common], help="全部启用账号签到一轮")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_checkin)

    p = sub.add_parser("keepalive", parents=[common], help="保活登录态（默认只跑到期账号）")
    p.add_argument("--force", action="store_true", help="忽略排期，立即保活所有启用账号")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_keepalive)

    p = sub.add_parser("import", parents=[common], help="把账号文件/单个 cookie 导入数据库")
    p.add_argument("--file", default=DEFAULT_IMPORT,
                   help=f"账号文件（默认 {DEFAULT_IMPORT}）")
    p.add_argument("--cookie", help="直接导入单个 cookie（忽略账号文件）")
    p.add_argument("--name", default="cli", help="--cookie 模式的账号名（默认 cli）")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("topics", parents=[common], help="查看/刷新关注超话缓存")
    p.add_argument("topics_cmd", choices=["list", "refresh"])
    p.set_defaults(func=cmd_topics)

    p = sub.add_parser("rotate-probe", parents=[common],
                       help="实验性：探测 PC 端点能否静默换发 Cookie")
    p.add_argument("--all", action="store_true",
                   help="探测全部启用账号（默认只探测临期账号）")
    p.add_argument("--urls", metavar="URL[,URL…]",
                   help="逗号分隔的候选端点，覆盖设置 keepalive_passport_urls")
    p.set_defaults(func=cmd_rotate_probe)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    _init_db(args.db)
    if args.command != "import" or not getattr(args, "cookie", None):
        _maybe_migrate(Path(args.legacy_state))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
