"""
微博超话签到管理面板 - 数据库层
使用 SQLite 单文件数据库。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .weibo_client import cookie_to_string, normalize_cookie, parse_cookie_expiry, parse_time

# 数据库文件默认放在项目根目录下
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "weibo_checkin.db"

log = logging.getLogger("weibo.database")

_local = threading.local()

# 写锁：并行签到线程同时写库时，WAL 允许多读单写，
# 配合 busy_timeout 排队写请求，避免 "database is locked" 直接抛错。
_write_lock = threading.RLock()


def _get_conn() -> sqlite3.Connection:
    """获取当前线程的数据库连接（线程本地）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        # 写冲突时等待而非立刻失败（毫秒）
        conn.execute("PRAGMA busy_timeout=5000;")
        _local.conn = conn
    return conn


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db() -> None:
    """初始化数据库表结构。"""
    conn = _get_conn()
    conn.executescript(
        """
        -- 全局设置（键值对）
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL
        );

        -- 微博账号
        CREATE TABLE IF NOT EXISTS accounts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL DEFAULT '未命名账号',
            cookie       TEXT NOT NULL DEFAULT '',
            cookie_raw   TEXT NOT NULL DEFAULT '',   -- 原始 cookie 字符串（可选）
            enabled      INTEGER NOT NULL DEFAULT 1,  -- 是否启用自动签到
            proxy        TEXT NOT NULL DEFAULT '',     -- 该账号使用的 socks 链接（归属地由 proxy_geo 识别）
            proxy_index  INTEGER NOT NULL DEFAULT 0,  -- 兼容旧字段（已弃用，改用 proxy）
            remark       TEXT NOT NULL DEFAULT '',
            avatar_url   TEXT NOT NULL DEFAULT '',    -- 微博头像地址
            weibo_uid    TEXT NOT NULL DEFAULT '',    -- 微博用户 UID
            last_status  TEXT NOT NULL DEFAULT 'unknown', -- success/failed/partial/unknown
            last_checkin TEXT,                         -- 上次签到时间
            last_message TEXT NOT NULL DEFAULT '',
            -- Cookie 生命周期（微博靠服务端 session 维持登录，需要持续观测与续期）
            cookie_expires_at             TEXT    NOT NULL DEFAULT '',  -- ALF 解析出的名义到期
            cookie_issued_at              TEXT    NOT NULL DEFAULT '',  -- 当前这份 cookie 的录入时间
            cookie_last_seen_valid_at     TEXT    NOT NULL DEFAULT '',  -- 上次确认登录态有效
            cookie_last_rotated_at        TEXT    NOT NULL DEFAULT '',  -- 上次成功换发回写
            cookie_last_rotate_keys       TEXT    NOT NULL DEFAULT '',  -- 上次换发变化的 key
            cookie_last_rotate_source     TEXT    NOT NULL DEFAULT '',  -- 上次换发来源端点（keepalive/config 等）
            cookie_next_keepalive_at      TEXT    NOT NULL DEFAULT '',  -- 下次保活时间
            cookie_health                 TEXT    NOT NULL DEFAULT 'unknown', -- ok/expiring/stale/invalid/network_error/rate_limited/unknown
            cookie_fail_streak            INTEGER NOT NULL DEFAULT 0,   -- 连续保活失败次数（退避用）
            cookie_warned_at              TEXT    NOT NULL DEFAULT '',  -- 上次 TG 到期预警（去重用）
            cookie_lifetime_observed_days INTEGER NOT NULL DEFAULT 0,   -- 上次真死时实测的 session 寿命
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );

        -- 签到日志
        CREATE TABLE IF NOT EXISTS checkin_logs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id    INTEGER,
            account_name  TEXT NOT NULL,
            task_id       TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL,               -- success/partial/failed
            channel       TEXT NOT NULL DEFAULT 'direct',
            total         INTEGER NOT NULL DEFAULT 0,
            success       INTEGER NOT NULL DEFAULT 0,
            fail          INTEGER NOT NULL DEFAULT 0,
            detail        TEXT NOT NULL DEFAULT '',     -- JSON 明细
            message       TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL,
            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL
        );

        -- 执行任务（每次手动或定时触发的运行）
        CREATE TABLE IF NOT EXISTS tasks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id       TEXT NOT NULL UNIQUE,       -- UUID
            trigger_type  TEXT NOT NULL DEFAULT 'manual', -- manual/schedule
            status        TEXT NOT NULL DEFAULT 'running', -- running/success/partial/failed
            started_at    TEXT NOT NULL,
            finished_at   TEXT,
            summary       TEXT NOT NULL DEFAULT ''
        );

        -- 通知记录
        CREATE TABLE IF NOT EXISTS notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            channel    TEXT NOT NULL DEFAULT 'telegram',
            title      TEXT NOT NULL DEFAULT '',
            body       TEXT NOT NULL DEFAULT '',
            success    INTEGER NOT NULL DEFAULT 0,
            error      TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        -- 用户（登录）
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at    TEXT NOT NULL
        );

        -- SOCKS5 代理节点（归属地自动识别）
        CREATE TABLE IF NOT EXISTS proxies (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            label        TEXT NOT NULL DEFAULT '',
            ip           TEXT NOT NULL DEFAULT '',
            port         INTEGER NOT NULL DEFAULT 0,
            username     TEXT NOT NULL DEFAULT '',
            password     TEXT NOT NULL DEFAULT '',
            url          TEXT NOT NULL DEFAULT '',    -- 完整 socks5:// 链接
            geo_country   TEXT NOT NULL DEFAULT '',
            geo_region    TEXT NOT NULL DEFAULT '',
            geo_country_code TEXT NOT NULL DEFAULT '',
            geo_ip        TEXT NOT NULL DEFAULT '',
            enabled      INTEGER NOT NULL DEFAULT 1,
            remark       TEXT NOT NULL DEFAULT '',
            last_test     TEXT NOT NULL DEFAULT '',     -- 最近测试结果(ok/fail)
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );

        -- 登录会话（token 存库，便于登出/校验）
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_logs_account ON checkin_logs(account_id);
        CREATE INDEX IF NOT EXISTS idx_logs_time   ON checkin_logs(created_at);

        -- v1.0.0：单账号关注超话缓存
        CREATE TABLE IF NOT EXISTS topic_cache (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id   INTEGER NOT NULL,
            topics       TEXT NOT NULL DEFAULT '[]',
            cached_at    TEXT NOT NULL,
            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
            UNIQUE(account_id)
        );

        -- v1.0.0：所有账号关注过的全量超话（去重）
        CREATE TABLE IF NOT EXISTS all_topics (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id     TEXT NOT NULL UNIQUE,
            name         TEXT NOT NULL DEFAULT '',
            avatar_url   TEXT NOT NULL DEFAULT '',
            description  TEXT NOT NULL DEFAULT '',
            topic_url    TEXT NOT NULL DEFAULT '',
            member_count INTEGER NOT NULL DEFAULT 0,
            post_count   INTEGER NOT NULL DEFAULT 0,
            push_enabled INTEGER NOT NULL DEFAULT 0,
            fetched_at   TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_topic_cache_account ON topic_cache(account_id);
        CREATE INDEX IF NOT EXISTS idx_all_topics_topic_id ON all_topics(topic_id);
        -- v1.0.0：超话帖子缓存
        CREATE TABLE IF NOT EXISTS topic_posts_cache (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id     TEXT NOT NULL UNIQUE,
            posts        TEXT NOT NULL DEFAULT '[]',
            fetched_at   TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_topic_posts_cache_topic_id ON topic_posts_cache(topic_id);

        -- 登录失败限流（持久化，重启不丢、多进程共享）
        CREATE TABLE IF NOT EXISTS login_locks (
            ip          TEXT PRIMARY KEY,
            fails       TEXT NOT NULL DEFAULT '[]',  -- JSON：窗口内失败时间戳列表
            locked_until REAL NOT NULL DEFAULT 0     -- 锁定截止时间戳（秒）
        );

        """
    )
    conn.commit()
    _migrate(conn)
    _seed_defaults(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """兼容旧数据库的增量迁移。"""
    account_cols = [r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()]
    if "proxy" not in account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN proxy TEXT NOT NULL DEFAULT ''")
        log.info("accounts 表已迁移：新增 proxy 列")
    account_additions = {
        "avatar_url": "TEXT NOT NULL DEFAULT ''",
        "weibo_uid": "TEXT NOT NULL DEFAULT ''",
        # Cookie 生命周期观测与保活
        "cookie_expires_at": "TEXT NOT NULL DEFAULT ''",
        "cookie_issued_at": "TEXT NOT NULL DEFAULT ''",
        "cookie_last_seen_valid_at": "TEXT NOT NULL DEFAULT ''",
        "cookie_last_rotated_at": "TEXT NOT NULL DEFAULT ''",
        "cookie_last_rotate_keys": "TEXT NOT NULL DEFAULT ''",
        "cookie_last_rotate_source": "TEXT NOT NULL DEFAULT ''",
        "cookie_next_keepalive_at": "TEXT NOT NULL DEFAULT ''",
        "cookie_health": "TEXT NOT NULL DEFAULT 'unknown'",
        "cookie_fail_streak": "INTEGER NOT NULL DEFAULT 0",
        "cookie_warned_at": "TEXT NOT NULL DEFAULT ''",
        "cookie_lifetime_observed_days": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in account_additions.items():
        if name not in account_cols:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {definition}")
            log.info("accounts 表已迁移：新增 %s 列", name)
    # 老账号补齐 cookie 录入时间基准，否则寿命/到期算不出来
    conn.execute(
        "UPDATE accounts SET cookie_issued_at = created_at "
        "WHERE cookie_issued_at = '' AND created_at IS NOT NULL"
    )

    # 代理测试结果需要持久显示，避免前端刷新后延迟/结果消失。
    proxy_cols = [r["name"] for r in conn.execute("PRAGMA table_info(proxies)").fetchall()]
    additions = {
        "last_latency_ms": "INTEGER NOT NULL DEFAULT 0",
        "last_test_at": "TEXT NOT NULL DEFAULT ''",
        "last_test_message": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in additions.items():
        if name not in proxy_cols:
            conn.execute(f"ALTER TABLE proxies ADD COLUMN {name} {definition}")
            log.info("proxies 表已迁移：新增 %s", name)

    # v1.3.0：all_topics 表新增 push_enabled 列（每日推送选择）
    all_topics_cols = [r["name"] for r in conn.execute("PRAGMA table_info(all_topics)").fetchall()]
    if "push_enabled" not in all_topics_cols:
        conn.execute("ALTER TABLE all_topics ADD COLUMN push_enabled INTEGER NOT NULL DEFAULT 0")
        log.info("all_topics 表已迁移：新增 push_enabled 列")
    conn.commit()


def _seed_defaults(conn: sqlite3.Connection) -> None:
    """写入默认设置。"""
    defaults = {
        "tg_bot_token": "",
        "tg_user_id": "",
        "tg_enabled": "0",
        "schedule_enabled": "1",       # 是否启用自动定时签到
        "schedule_cron": "0 7 * * *",  # 每天 7 点
        "anti_ban_enabled": "1",       # 防封策略总开关
        "anti_ban_wait_min": "120",    # 账号间最小随机等待（秒）
        "anti_ban_wait_max": "300",    # 账号间最大随机等待（秒）
        "anti_ban_window_hour": "7",   # 凌晨 N 点前启用等待
        "proxies": "",                # SOCKS5 节点，一行一个
        "proxy_force": "0",           # 严格代理（失败不回退直连）
        "proxy_fallback": "1",        # 允许失败回退直连
        "checkin_delay_min": "3",     # 超话间随机延时（秒）
        "checkin_delay_max": "8",
        "auth_enabled": "1",          # 是否启用登录保护
        "admin_initialized": "0",     # 默认管理员是否已初始化
        # ---- v7.0 新增 ----
        "tg_quote_enabled": "1",      # TG 推送附带每日一言
        "tg_only_on_change": "0",     # 仅在有失败/异常时推送
        "tg_silent": "0",             # 静默推送（不震动提示）
        "log_retention_days": "30",   # 日志保留天数（0=不清理）
        # ---- v1.0.0 超话 AI 总结 ----
        "ai_base_url": "",          # OpenAI 兼容 API base_url
        "ai_api_key": "",           # API Key
        "ai_model": "gpt-4o-mini",  # 模型名
        "ai_topic_prompt": "你是一个超话内容总结助手。请对以下超话帖子内容进行简洁总结（200字以内），包括：1. 主要讨论话题 2. 热门帖子要点 3. 整体氛围。只输出总结文字，不要任何前缀或格式标记。",
        # ---- Cookie 保活与到期预警 ----
        "keepalive_enabled": "1",            # 定时戳微博接口续期登录态
        "keepalive_interval_hours": "12",    # 每个账号的续期间隔（小时）
        "keepalive_urgent_interval_hours": "6",  # 临近预计失效时的加密续期间隔
        "keepalive_quiet_hours": "0-6",       # 深夜静默时段（如 0-6；off 关闭），保活别 24 小时在线
        "keepalive_gap_seconds": "30",       # 保活轮内账号之间的间隔
        "keepalive_max_per_tick": "20",      # 单次 tick 最多处理账号数
        "cookie_session_ttl_days": "7",      # 服务端 session 经验寿命（微博约 7 天）
        "cookie_expiry_warn_days": "2",      # 距预计失效几天前预警
        "cookie_warn_cooldown_hours": "12",  # 同一账号预警最小间隔
        # ---- v1.3.6 扫码续期闭环 ----
        "cookie_relogin_stale_hours": "48",  # 临期且 N 小时无成功换发 → 建议扫码续期
        "panel_url": "",                     # TG 预警附带的面板访问地址（空=不加链接）
        # ---- v1.3.6 实验性 PC 端点静默换发 ----
        "keepalive_passport_enabled": "0",   # 临期时追加请求 PC 端点尝试换发（实验性）
        "keepalive_passport_urls": "https://weibo.com/,https://passport.weibo.com/",
        # ---- v1.3.7 手机 App 同步 ----
        "mobile_sync_enabled": "0",          # 手机保活总开关（令牌鉴权的同步端点）
        "mobile_sync_token": "",             # 同步令牌（仅 regenerate 接口可写）
        "mobile_sync_last_at": "",           # 最近一次手机同步时间
        "mobile_sync_last_result": "",       # 最近一次手机同步结果
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()


# ========================= 设置 =========================

def get_setting(key: str, default: str = "") -> str:
    row = _get_conn().execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def get_settings() -> dict:
    rows = _get_conn().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_settings(values: dict) -> None:
    conn = _get_conn()
    for key, value in values.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
    conn.commit()


# ========================= 账号 =========================

def add_proxy(data: dict) -> int:
    conn = _get_conn()
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO proxies
            (label, ip, port, username, password, url, geo_country, geo_region,
             geo_country_code, geo_ip, enabled, remark, last_test, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (data.get("label", ""), data.get("ip", ""), int(data.get("port", 0) or 0),
         data.get("username", ""), data.get("password", ""), data.get("url", ""),
         data.get("geo_country", ""), data.get("geo_region", ""),
         data.get("geo_country_code", ""), data.get("geo_ip", ""),
         1 if data.get("enabled", True) else 0, data.get("remark", ""),
         data.get("last_test", ""), now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_proxy(proxy_id: int, data: dict) -> bool:
    conn = _get_conn()
    allowed = ("label", "ip", "port", "username", "password", "url",
               "geo_country", "geo_region", "geo_country_code", "geo_ip",
               "enabled", "remark", "last_test", "last_latency_ms",
               "last_test_at", "last_test_message")
    fields = {k: v for k, v in data.items() if k in allowed}
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    if not fields:
        return False
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values())
    values.append(_now())
    values.append(proxy_id)
    conn.execute(f"UPDATE proxies SET {sets}, updated_at = ? WHERE id = ?", values)
    conn.commit()
    return True


def delete_proxy(proxy_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
    conn.commit()
    return cur.rowcount > 0


def get_proxy(proxy_id: int) -> dict | None:
    row = _get_conn().execute("SELECT * FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
    return dict(row) if row else None


def get_proxies(include_disabled: bool = True) -> list[dict]:
    if include_disabled:
        rows = _get_conn().execute("SELECT * FROM proxies ORDER BY id").fetchall()
    else:
        rows = _get_conn().execute(
            "SELECT * FROM proxies WHERE enabled = 1 ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def mask_proxy_url(url: str) -> str:
    """把 socks5://user:pass@host:port 打码成 socks5://***@host:port（v7.1）。

    面板/API 只应看到打码结果；真实凭据只在服务端发起请求时使用。
    """
    s = (url or "").strip()
    if not s:
        return ""
    if "@" not in s:
        return s
    head, _, tail = s.partition("://")
    if not tail:
        return "***@" + s.split("@", 1)[-1]
    return f"{head}://***@" + tail.split("@", 1)[-1]


def find_proxy_by_url(url: str) -> dict | None:
    """按完整链接反查代理记录（用于账号 proxy 字段 → 代理 id 映射）。"""
    s = (url or "").strip()
    if not s:
        return None
    for p in get_proxies(include_disabled=True):
        full = p.get("url") or build_proxy_url(p)
        if full and full == s:
            return p
    # 退化匹配：host:port 相同即认为同一节点
    tail = s.split("@", 1)[-1]
    for p in get_proxies(include_disabled=True):
        if p.get("ip") and f"{p['ip']}:{p.get('port')}" == tail:
            return p
    return None


def proxy_display_label(p: dict) -> str:
    """代理的人类可读名称（不含凭据）。"""
    if not p:
        return ""
    if p.get("label"):
        return str(p["label"])
    if p.get("geo_country"):
        return f"{p['geo_country']} {p.get('geo_region') or ''}".strip()
    if p.get("ip"):
        return f"{p['ip']}:{p.get('port')}"
    return "代理节点"


def build_proxy_url(p: dict) -> str:
    """由字段拼接 socks5 链接。"""
    ip, port = p.get("ip", ""), p.get("port", 0)
    if not ip or not port:
        return p.get("url", "") or ""
    user = p.get("username", "")
    pwd = p.get("password", "")
    auth = f"{user}:{pwd}@" if user else ""
    return f"socks5://{auth}{ip}:{port}"


def add_account(data: dict) -> int:
    conn = _get_conn()
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO accounts
            (name, cookie, cookie_raw, enabled, proxy, remark, avatar_url, weibo_uid,
             last_status, last_message, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unknown', '', ?, ?)
        """,
        (
            data.get("name", "未命名账号"),
            data.get("cookie", ""),
            data.get("cookie_raw", ""),
            1 if data.get("enabled", True) else 0,
            data.get("proxy", ""),   # socks 链接或标识
            data.get("remark", ""),
            data.get("avatar_url", ""),
            data.get("weibo_uid", ""),
            now, now,
        ),
    )
    conn.commit()
    account_id = cur.lastrowid
    on_cookie_replaced(
        account_id, data.get("cookie") or data.get("cookie_raw") or "")
    return account_id


def update_account(account_id: int, data: dict) -> bool:
    conn = _get_conn()
    allowed = ("name", "cookie", "cookie_raw", "enabled", "proxy", "proxy_index", "remark", "avatar_url", "weibo_uid")
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return False
    # 兼容：旧字段 proxy_index 映射到 proxy 非空时才用
    if "proxy_index" in fields and "proxy" not in fields:
        pi = fields.pop("proxy_index")
        if pi:
            fields["proxy"] = str(pi)

    # 换了新 cookie（手工粘贴 / 导入 / 扫码覆盖）要把生命周期基准重算，
    # 否则旧的"上次续期时间"会把新凭据误判成快到期。
    replaced_cookie = ""
    if "cookie" in fields or "cookie_raw" in fields:
        row = conn.execute(
            f"SELECT {_EFFECTIVE_COOKIE} AS effective FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        old_effective = (row["effective"] if row else "") or ""
        new_effective = fields.get("cookie") or fields.get("cookie_raw") or old_effective
        if new_effective and new_effective != old_effective:
            replaced_cookie = new_effective

    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values())
    values.append(_now())
    values.append(account_id)
    conn.execute(
        f"UPDATE accounts SET {sets}, updated_at=? WHERE id=?",
        values,
    )
    conn.commit()
    if replaced_cookie:
        on_cookie_replaced(account_id, replaced_cookie)
    return True


def delete_account(account_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()
    return cur.rowcount > 0


def get_account(account_id: int) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    return dict(row) if row else None


def get_accounts() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM accounts ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_enabled_accounts() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM accounts WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def touch_account_result(account_id: int, status: str, message: str) -> None:
    conn = _get_conn()
    conn.execute(
        """
        UPDATE accounts
        SET last_status = ?, last_message = ?, last_checkin = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, message, _now(), _now(), account_id),
    )
    conn.commit()


# ========================= Cookie 生命周期 =========================

# 保活/回写逻辑可写入的状态列。刻意不并入 update_account 的 allowed 白名单，
# 否则 PUT /api/accounts/{id} 能把这些内部观测字段当普通字段重置。
COOKIE_STATE_COLUMNS = (
    "cookie_expires_at", "cookie_issued_at", "cookie_last_seen_valid_at",
    "cookie_last_rotated_at", "cookie_last_rotate_keys",
    "cookie_last_rotate_source",
    "cookie_next_keepalive_at", "cookie_health", "cookie_fail_streak",
    "cookie_warned_at", "cookie_lifetime_observed_days",
)

# 账号当前真正生效的那份 cookie（历史上手填/批量导入只写过 cookie_raw）
_EFFECTIVE_COOKIE = "(CASE WHEN cookie <> '' THEN cookie ELSE cookie_raw END)"


def _fmt_time(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else ""


def _keepalive_interval_hours() -> float:
    try:
        value = float(get_setting("keepalive_interval_hours", "12") or 12)
    except (TypeError, ValueError):
        return 12.0
    return min(max(value, 1.0), 72.0)


def merge_cookie_strings(old: str, new: str) -> str:
    """union 合并两份 cookie：新值优先，但绝不丢弃库里已有的字段。"""
    merged = dict(normalize_cookie(old))
    for key, value in normalize_cookie(new).items():
        if value:
            merged[key] = value
    return cookie_to_string(merged)


def set_cookie_state(account_id: int, **cols) -> bool:
    """只写 cookie_* 观测列。"""
    fields = {key: value for key, value in cols.items() if key in COOKIE_STATE_COLUMNS}
    if not fields:
        return False
    conn = _get_conn()
    sets = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [_now(), account_id]
    conn.execute(f"UPDATE accounts SET {sets}, updated_at = ? WHERE id = ?", values)
    conn.commit()
    return True


def merge_account_cookie(account_id: int, cookie_str: str, *,
                         meta: dict | None = None,
                         expected_old: str | None = None) -> bool:
    """原子地"读-合并-写"账号 cookie。

    - 事务内重新读库再 union，避免并发线程互相覆盖掉对方续到的字段；
    - expected_old 走 CAS：库里现值已不是我们发请求时读到的那份，说明期间别的
      线程已经续过一期，此时写回一份更旧的 SUB 反而会把登录态作死，直接放弃；
    - cookie 与 cookie_raw 始终保持同值（读取侧两者都要看）。
    """
    if not cookie_str:
        return False
    conn = _get_conn()
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                f"SELECT {_EFFECTIVE_COOKIE} AS effective FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            current = row["effective"] or ""
            merged_str = merge_cookie_strings(current, cookie_str)
            if not merged_str:
                conn.rollback()
                return False

            sets = ["cookie = ?", "cookie_raw = ?"]
            values: list = [merged_str, merged_str]
            for key, value in (meta or {}).items():
                if key in COOKIE_STATE_COLUMNS:
                    sets.append(f"{key} = ?")
                    values.append(value)
            sets.append("updated_at = ?")
            values.append(_now())
            values.append(account_id)

            where = "id = ?"
            if expected_old is not None:
                where += f" AND {_EFFECTIVE_COOKIE} = ?"
                values.append(expected_old)

            cur = conn.execute(
                f"UPDATE accounts SET {', '.join(sets)} WHERE {where}", values
            )
            if cur.rowcount == 0:
                conn.rollback()
                return False
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def on_cookie_replaced(account_id: int, cookie_str: str) -> None:
    """账号换了全新 cookie（手工粘贴 / 导入 / 扫码登录）后重算生命周期基准。"""
    now = datetime.now()
    expires_at = parse_cookie_expiry(cookie_str)
    set_cookie_state(
        account_id,
        cookie_expires_at=_fmt_time(expires_at),
        cookie_issued_at=_fmt_time(now),
        cookie_last_seen_valid_at="",
        cookie_last_rotated_at="",
        cookie_last_rotate_keys="",
        cookie_last_rotate_source="",
        cookie_next_keepalive_at=_fmt_time(
            now + timedelta(hours=_keepalive_interval_hours())),
        cookie_health="unknown",
        cookie_fail_streak=0,
        cookie_warned_at="",
    )


def list_keepalive_due(now: str | None = None, limit: int = 20) -> list[dict]:
    """取出到保活时间的启用账号；next_at 为空视为"从没排过"，即刻待处理。"""
    moment = now or _now()
    try:
        size = max(int(limit), 1)
    except (TypeError, ValueError):
        size = 20
    rows = _get_conn().execute(
        f"""
        SELECT * FROM accounts
        WHERE enabled = 1
          AND cookie_health <> 'invalid'
          AND {_EFFECTIVE_COOKIE} <> ''
          AND (cookie_next_keepalive_at = '' OR cookie_next_keepalive_at <= ?)
        ORDER BY CASE WHEN cookie_next_keepalive_at = '' THEN 0 ELSE 1 END,
                 cookie_next_keepalive_at
        LIMIT ?
        """,
        (moment, size),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_cookie_dead(account_id: int) -> int:
    """判定登录态已死：停掉保活，并记下这次实测出的 session 寿命供自适应间隔使用。"""
    acc = get_account(account_id)
    if not acc:
        return 0
    issued = parse_time(acc.get("cookie_issued_at")) or parse_time(acc.get("created_at"))
    lifetime_days = max((datetime.now() - issued).days, 0) if issued else 0
    set_cookie_state(
        account_id,
        cookie_health="invalid",
        cookie_next_keepalive_at="",
        cookie_lifetime_observed_days=lifetime_days,
    )
    return lifetime_days


def count_cookie_health() -> dict:
    rows = _get_conn().execute(
        """
        SELECT cookie_health AS health, COUNT(*) AS c
        FROM accounts WHERE enabled = 1 GROUP BY cookie_health
        """
    ).fetchall()
    return {(r["health"] or "unknown"): r["c"] for r in rows}


# ========================= 日志 =========================

def add_log(entry: dict) -> int:
    conn = _get_conn()
    cur = conn.execute(
        """
        INSERT INTO checkin_logs
            (account_id, account_name, task_id, status, channel,
             total, success, fail, detail, message, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.get("account_id"),
            entry.get("account_name", ""),
            entry.get("task_id", ""),
            entry.get("status", "failed"),
            entry.get("channel", "direct"),
            int(entry.get("total", 0)),
            int(entry.get("success", 0)),
            int(entry.get("fail", 0)),
            json.dumps(entry.get("detail", []), ensure_ascii=False),
            entry.get("message", ""),
            _now(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _safe_channel(value: str) -> str:
    """读取层脱敏：旧日志可能存了完整代理 URL（含用户名密码），
    统一折叠为“SOCKS5 代理 / 直连”，避免历史数据在面板泄露凭据。"""
    s = (value or "").strip()
    if not s:
        return ""
    low = s.lower()
    if "socks" in low:
        return "SOCKS5 代理"
    if "direct" in low or "直连" in s:
        return "直连"
    if "@" in s:  # 其他带认证信息的形式，一律不展示原值
        return "代理"
    return s


def _row_to_log(row) -> dict:
    d = dict(row)
    try:
        d["detail"] = json.loads(d["detail"]) if d["detail"] else []
    except json.JSONDecodeError:
        d["detail"] = []
    d["channel"] = _safe_channel(d.get("channel", ""))
    return d


def get_logs_paginated(limit: int = 50, offset: int = 0, account_id: int | None = None) -> dict:
    """分页获取日志，返回 {items, total, has_more}。"""
    conn = _get_conn()
    base_where = "WHERE account_id = ?" if account_id is not None else ""
    count_row = conn.execute(
        f"SELECT COUNT(*) c FROM checkin_logs {base_where}",
        (account_id,) if account_id is not None else (),
    ).fetchone()
    total = count_row["c"] if count_row else 0
    if account_id is not None:
        rows = conn.execute(
            "SELECT * FROM checkin_logs WHERE account_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (account_id, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM checkin_logs ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return {
        "items": [_row_to_log(r) for r in rows],
        "total": total,
        "has_more": offset + len(rows) < total,
    }


def get_logs(limit: int = 50, account_id: int | None = None) -> list[dict]:
    """保留旧接口，调用分页版本。"""
    return get_logs_paginated(limit=limit, offset=0, account_id=account_id)["items"]


def get_log_stats() -> dict:
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM checkin_logs").fetchone()["c"]
    success = conn.execute(
        "SELECT COUNT(*) c FROM checkin_logs WHERE status='success'"
    ).fetchone()["c"]
    fail = conn.execute(
        "SELECT COUNT(*) c FROM checkin_logs WHERE status='failed'"
    ).fetchone()["c"]
    partial = conn.execute(
        "SELECT COUNT(*) c FROM checkin_logs WHERE status='partial'"
    ).fetchone()["c"]
    today = datetime.now().strftime("%Y-%m-%d")
    today_count = conn.execute(
        "SELECT COUNT(*) c FROM checkin_logs WHERE created_at LIKE ?",
        (today + "%",),
    ).fetchone()["c"]
    topics_signed = conn.execute(
        "SELECT COALESCE(SUM(success),0) s FROM checkin_logs"
    ).fetchone()["s"]
    last_row = conn.execute(
        "SELECT created_at FROM checkin_logs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "total": total,
        "success": success,
        "partial": partial,
        "fail": fail,
        "today": today_count,
        "topics_signed": topics_signed,
        "last_log_at": last_row["created_at"] if last_row else "",
        "success_rate": round(success / total * 100) if total else 0,
    }


def get_daily_trend(days: int = 7) -> list[dict]:
    """近 N 天每天签到统计（用于仪表盘趋势图）。"""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT substr(created_at, 1, 10) AS day,
               COUNT(*) AS runs,
               COALESCE(SUM(success), 0) AS success,
               COALESCE(SUM(fail), 0) AS fail
        FROM checkin_logs
        WHERE created_at >= ?
        GROUP BY day ORDER BY day
        """,
        ((datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d"),),
    ).fetchall()
    by_day = {r["day"]: dict(r) for r in rows}
    result = []
    for i in range(days - 1, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        item = by_day.get(day) or {"day": day, "runs": 0, "success": 0, "fail": 0}
        result.append({
            "day": day,
            "label": day[5:],
            "runs": item["runs"],
            "success": item["success"],
            "fail": item["fail"],
        })
    return result


def purge_old_logs(days: int) -> int:
    """删除 N 天前的日志，返回删除条数。days<=0 时不做任何事。"""
    if days <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    conn = _get_conn()
    cur = conn.execute("DELETE FROM checkin_logs WHERE created_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount or 0


def clear_logs() -> int:
    """清空全部签到日志，返回删除条数。"""
    conn = _get_conn()
    count = conn.execute("SELECT COUNT(*) c FROM checkin_logs").fetchone()["c"]
    conn.execute("DELETE FROM checkin_logs")
    conn.commit()
    return count


def get_logs_grouped(limit: int = 20) -> list[dict]:
    """获取日志，按执行日期分组；同一次 task 的所有账号归并到一组。
    返回结构：
    [
      {
        "date": "2026-08-14",
        "groups": [   # 同一天内的多次执行
          {
            "task_id": "...", "trigger_type": "...",
            "started_at": "...", "accounts": [...],
            "total": N, "success": N, "fail": N, "status": "..."
          }
        ]
      }
    ]
    """
    conn = _get_conn()
    # 取最近的日志（按 id 倒序），再倒回正向拼装
    rows = conn.execute(
        "SELECT * FROM checkin_logs ORDER BY id DESC LIMIT ?", (limit * 5,)
    ).fetchall()
    entries = []
    for r in reversed(rows):  # 变回时间正序
        entries.append(_row_to_log(r))

    # 按 task_id 归组
    tasks = {}
    task_order = []
    for e in entries:
        tid = e.get("task_id") or f"manual-{e['id']}"
        if tid not in tasks:
            tasks[tid] = {
                "task_id": tid,
                "trigger_type": "",
                "created_at": e.get("created_at", ""),
                "accounts": [],
                "total": 0, "success": 0, "fail": 0,
            }
            task_order.append(tid)
        g = tasks[tid]
        g["accounts"].append(e)
        g["total"] += e.get("total", 0)
        g["success"] += e.get("success", 0)
        g["fail"] += e.get("fail", 0)
        if not g["trigger_type"] and e.get("message"):
            pass

    # 按日期分组
    by_date = {}
    date_order = []
    for tid in task_order:
        g = tasks[tid]
        date = (g["created_at"] or "")[:10] or "未知"
        if date not in by_date:
            by_date[date] = []
            date_order.append(date)
        # 组状态：任一 failed → failed；任一 partial → partial；否则 success
        st = "success"
        for a in g["accounts"]:
            if a.get("status") == "failed": st = "failed"; break
            if a.get("status") == "partial": st = "partial"
        g["status"] = st
        by_date[date].append(g)

    result = []
    for date in reversed(date_order):  # 最新日期在前
        result.append({"date": date, "groups": by_date[date]})
    return result


# ========================= 任务 =========================

def create_task(task_id: str, trigger_type: str = "manual") -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO tasks (task_id, trigger_type, status, started_at) VALUES (?, ?, 'running', ?)",
        (task_id, trigger_type, _now()),
    )
    conn.commit()


def finish_task(task_id: str, status: str, summary: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE tasks SET status = ?, finished_at = ?, summary = ? WHERE task_id = ?",
        (status, _now(), summary, task_id),
    )
    conn.commit()


def get_tasks(limit: int = 20) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ========================= 通知记录 =========================

def add_notification(title: str, body: str, success: bool, error: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO notifications (title, body, success, error, created_at) VALUES (?, ?, ?, ?, ?)",
        (title, body, 1 if success else 0, error, _now()),
    )
    conn.commit()


def get_notifications(limit: int = 10) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ========================= 用户 & 会话 =========================

def create_user(username: str, password_hash: str) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT OR IGNORE INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, password_hash, _now()),
    )
    conn.commit()
    return cur.lastrowid


def try_create_first_user(username: str, password_hash: str) -> int | None:
    """仅当系统中还不存在任何用户时创建用户（事务内原子判断，杜绝并发双开）。

    返回新用户 id；已存在用户时返回 None。
    """
    conn = _get_conn()
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            if count > 0:
                conn.rollback()
                return None
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, _now()),
            )
            conn.commit()
            return cur.lastrowid
        except Exception:
            conn.rollback()
            raise


def update_user_password(user_id: int, password_hash: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (password_hash, user_id),
    )
    conn.commit()


def get_user_by_name(username: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    return dict(row) if row else None


def get_user(user_id: int) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def count_users() -> int:
    return _get_conn().execute("SELECT COUNT(*) c FROM users").fetchone()["c"]


def _token_digest(token: str) -> str:
    """会话 token 只存 SHA-256 摘要：拖库后 token 无法直接复用。"""
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(token: str, user_id: int, ttl_hours: int = 168) -> None:
    """创建会话，默认 7 天有效。库中仅保存 token 摘要。"""
    from datetime import timedelta

    conn = _get_conn()
    expires = (
        datetime.now() + timedelta(hours=ttl_hours)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (_token_digest(token), user_id, _now(), expires),
    )
    conn.commit()


def get_session_user(token: str) -> dict | None:
    """根据 token 返回用户（校验有效期）。"""
    if not token:
        return None
    conn = _get_conn()
    digest = _token_digest(token)
    row = conn.execute(
        "SELECT s.user_id, s.expires_at FROM sessions s WHERE s.token = ?", (digest,)
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] < _now():
        conn.execute("DELETE FROM sessions WHERE token = ?", (digest,))
        conn.commit()
        return None
    # 清理过期会话
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
    conn.commit()
    return get_user(row["user_id"])


def delete_session(token: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM sessions WHERE token = ?", (_token_digest(token),))
    conn.commit()


def delete_user_sessions(user_id: int) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()


# ========================= 登录失败限流（持久化） =========================

def login_lock_remaining(ip: str, lock_seconds: int) -> int:
    """该 IP 剩余锁定秒数；0 表示未锁定。"""
    if not ip:
        return 0
    row = _get_conn().execute(
        "SELECT locked_until FROM login_locks WHERE ip = ?", (ip,)
    ).fetchone()
    if not row:
        return 0
    import time
    remaining = row["locked_until"] - time.time()
    if remaining <= 0:
        return 0
    return int(remaining) + 1


def record_login_failure(ip: str, max_fails: int, window_seconds: int,
                         lock_seconds: int) -> int:
    """记录一次登录失败；触发锁定则返回锁定秒数，否则 0。"""
    if not ip:
        return 0
    import json
    import time

    conn = _get_conn()
    with _write_lock:
        now = time.time()
        row = conn.execute(
            "SELECT fails FROM login_locks WHERE ip = ?", (ip,)
        ).fetchone()
        try:
            hits = [t for t in json.loads(row["fails"]) if now - t < window_seconds]
        except (TypeError, ValueError):
            hits = []
        hits.append(now)
        if len(hits) >= max_fails:
            conn.execute(
                "INSERT INTO login_locks (ip, fails, locked_until) VALUES (?, ?, ?) "
                "ON CONFLICT(ip) DO UPDATE SET fails = ?, locked_until = ?",
                (ip, "[]", now + lock_seconds, "[]", now + lock_seconds),
            )
            conn.commit()
            return lock_seconds
        conn.execute(
            "INSERT INTO login_locks (ip, fails, locked_until) VALUES (?, ?, 0) "
            "ON CONFLICT(ip) DO UPDATE SET fails = ?",
            (ip, json.dumps(hits), json.dumps(hits)),
        )
        conn.commit()
    return 0


def reset_login_failures(ip: str) -> None:
    """登录成功后清掉该 IP 的失败计数与锁定。"""
    if not ip:
        return
    conn = _get_conn()
    conn.execute("DELETE FROM login_locks WHERE ip = ?", (ip,))
    conn.commit()


def reset_all_login_locks() -> None:
    """清空全部登录限流状态（测试与运维用途）。"""
    conn = _get_conn()
    conn.execute("DELETE FROM login_locks")
    conn.commit()


# ========================= v1.0.0 超话缓存 =========================

# ---------- topic_cache 表（单账号关注超话缓存） ----------

def get_topic_cache(account_id: int) -> dict | None:
    """获取单账号的关注超话缓存。返回 dict 或 None。"""
    row = _get_conn().execute(
        "SELECT * FROM topic_cache WHERE account_id = ?", (account_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["topics"] = json.loads(d["topics"]) if d["topics"] else []
    except json.JSONDecodeError:
        d["topics"] = []
    return d


def set_topic_cache(account_id: int, topics: list) -> None:
    """写入/更新单账号的关注超话缓存。topics 是 list[dict]。"""
    conn = _get_conn()
    now = _now()
    conn.execute(
        "INSERT INTO topic_cache (account_id, topics, cached_at) VALUES (?, ?, ?) "
        "ON CONFLICT(account_id) DO UPDATE SET topics=excluded.topics, cached_at=excluded.cached_at",
        (account_id, json.dumps(topics, ensure_ascii=False), now),
    )
    conn.commit()


def delete_topic_cache(account_id: int) -> None:
    """清除单个账号的超话缓存。"""
    conn = _get_conn()
    conn.execute("DELETE FROM topic_cache WHERE account_id = ?", (account_id,))
    conn.commit()


def clear_topic_caches() -> int:
    """清除所有超话缓存。"""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM topic_cache")
    conn.commit()
    return cur.rowcount or 0


# ---------- all_topics 表（全量超话去重列表） ----------

def upsert_all_topic(topic_id: str, name: str | None = None,
                     avatar_url: str | None = None,
                     description: str | None = None,
                     topic_url: str | None = None,
                     member_count: int | None = None,
                     post_count: int | None = None,
                     push_enabled: int | None = None,
                     fetched_at: str = "") -> int:
    """插入或更新一条全量超话（按 topic_id 去重）。返回 id。

    更新采用「只覆盖非空字段」语义：空字符串 / None / 非正数一律保留库里的旧值，
    防止刷新某个局部信息时把名称、头像或推送勾选冲掉。
    """
    conn = _get_conn()
    now = _now()
    row = conn.execute(
        "SELECT id FROM all_topics WHERE topic_id = ?", (topic_id,)
    ).fetchone()
    if row:
        sets, params = [], []
        for col, val in (("name", name), ("avatar_url", avatar_url),
                         ("description", description), ("topic_url", topic_url)):
            if isinstance(val, str) and val.strip():
                sets.append(f"{col} = ?")
                params.append(val.strip() if col == "name" else val)
        for col, val in (("member_count", member_count), ("post_count", post_count)):
            if isinstance(val, int) and val > 0:
                sets.append(f"{col} = ?")
                params.append(val)
        if push_enabled in (0, 1):
            sets.append("push_enabled = ?")
            params.append(push_enabled)
        if fetched_at:
            sets.append("fetched_at = ?")
            params.append(fetched_at)
        sets.append("updated_at = ?")
        params.append(now)
        params.append(row["id"])
        conn.execute(
            f"UPDATE all_topics SET {', '.join(sets)} WHERE id = ?", params
        )
        conn.commit()
        return row["id"]
    else:
        cur = conn.execute(
            "INSERT INTO all_topics (topic_id, name, avatar_url, description, topic_url,"
            " member_count, post_count, push_enabled, fetched_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (topic_id, (name or "").strip(), avatar_url or "", description or "",
             topic_url or "", member_count if isinstance(member_count, int) and member_count > 0 else 0,
             post_count if isinstance(post_count, int) and post_count > 0 else 0,
             push_enabled if push_enabled in (0, 1) else 0,
             fetched_at or now, now, now),
        )
        conn.commit()
        return cur.lastrowid


def get_all_topics(limit: int = 50, offset: int = 0) -> dict:
    """分页获取全量超话列表，返回 {items, total, has_more}。"""
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM all_topics").fetchone()["c"]
    rows = conn.execute(
        "SELECT * FROM all_topics ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "has_more": offset + len(rows) < total,
    }


def get_all_topic(topic_id: str) -> dict | None:
    """根据 topic_id 获取一条全量超话。"""
    row = _get_conn().execute(
        "SELECT * FROM all_topics WHERE topic_id = ?", (topic_id,)
    ).fetchone()
    return dict(row) if row else None


def clear_all_topics() -> int:
    """清空全量超话列表。"""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM all_topics")
    conn.commit()
    return cur.rowcount or 0


# ---------- topic_posts_cache 表（超话帖子缓存） ----------

def get_topic_posts_cache(topic_id: str) -> dict | None:
    """获取超话帖子缓存。返回 dict 或 None。"""
    row = _get_conn().execute(
        "SELECT * FROM topic_posts_cache WHERE topic_id = ?", (topic_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["posts"] = json.loads(d["posts"]) if d["posts"] else []
    except json.JSONDecodeError:
        d["posts"] = []
    return d


def set_topic_posts_cache(topic_id: str, posts: list) -> None:
    """写入/更新超话帖子缓存。posts 是 list[dict]。"""
    conn = _get_conn()
    now = _now()
    conn.execute(
        "INSERT INTO topic_posts_cache (topic_id, posts, fetched_at) VALUES (?, ?, ?) "
        "ON CONFLICT(topic_id) DO UPDATE SET posts=excluded.posts, fetched_at=excluded.fetched_at",
        (topic_id, json.dumps(posts, ensure_ascii=False), now),
    )
    conn.commit()


def delete_topic_posts_cache(topic_id: str) -> None:
    """清除超话帖子缓存。"""
    conn = _get_conn()
    conn.execute("DELETE FROM topic_posts_cache WHERE topic_id = ?", (topic_id,))
    conn.commit()
