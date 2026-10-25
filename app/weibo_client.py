"""
微博超话签到客户端
复用原青龙脚本的核心逻辑，改造成可被管理面板调用的库。
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import requests

from .security import (
    WEIBO_HOSTS, guarded_request, safe_exc, validate_weibo_url,
)

log = logging.getLogger("weibo.client")

# SOCKS 支持检测（缺失时给出明确提示）
try:
    import socks as _socks  # noqa: F401  (PySocks)
    SOCKS_AVAILABLE = True
except ImportError:
    SOCKS_AVAILABLE = False

BASE = "https://m.weibo.cn"
BASE_PC = "https://weibo.com"
CONFIG_URL = BASE + "/api/config"
TOPICS_URL = BASE + "/api/container/getIndex"
FOLLOWED_CONTAINER = "100803_-_followsuper"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    ),
    "Referer": BASE + "/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "MWeibo-Pwa": "1",
}

_RETRYABLE = (
    requests.exceptions.ProxyError,
    requests.exceptions.ConnectionError,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.SSLError,
)

# 单端点最大翻页数，限制请求放大
MAX_TOPIC_PAGES = 3

# ========================= Session 复用 =========================

_thread_sessions = threading.local()


def get_thread_session() -> requests.Session:
    """线程本地复用的 Session（用于公开接口，减少 TLS 握手开销）。

    账号级 Cookie 一律通过请求参数传入；复用前清空 Cookie jar，
    避免上一个账号的响应 Set-Cookie 混入下一个账号的请求。
    """
    session = getattr(_thread_sessions, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(HEADERS)
        _thread_sessions.session = session
    session.cookies.clear()
    return session


def new_account_session() -> requests.Session:
    """账号专属 Session：登录态请求必须独占 jar，才能回收本次响应的 Set-Cookie。

    不要改成复用 get_thread_session() —— 它复用前会清空 jar，轮换凭证会被抹掉。
    """
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=4)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session

# ========================= 超话页面（v1.3.0） =========================


def _clean_mblog_text(raw_text):
    """去掉微博文本里的 HTML 标签、实体与截断尾巴（…全文 / 展开等）。"""
    import re as _re, html as _html
    text = _re.sub(r'<[^>]+>', '', str(raw_text or '')).strip()
    text = _html.unescape(text)
    if text:
        # 先去掉尾部的时间戳行（如 "Thu Sep 17 21:13:15 +0800 2026"）
        text = _re.sub(r'\n[A-Z][a-z]{2}\s[A-Z][a-z]{2}\s\d{1,2}\s\d{2}:\d{2}:\d{2}\s[+-]\d{4}\s\d{4}\s*$', '', text)
        # 去掉尾部的「来自 XXX」行（source 字段已包含此信息）
        text = _re.sub(r'(?:^|\n)来自\s+\S+\s*$', '', text)
        # 去掉尾部的「转发了」等无意义结尾
        text = _re.sub(r'(?:^|\n)(转发了|轉發了|Repost)\s*$', '', text)
        # 去掉截断链接残留的「…全文」「展开」「收起」尾巴
        text = _re.sub(r'(?:\s*(?:\.{3}|…)?\s*(?:全文|展开|收起))+$', '', text)
        text = text.strip()
    return text


_REPOST_NOISE = ("转发微博", "轉發微博", "Repost", "分享微博", "转发了", "轉發了")


def _clean_source(raw_source):
    """source 字段常带 HTML（如 <a href="...">荣耀8X</a>），只留纯文本设备名。"""
    import re as _re, html as _html
    src = _re.sub(r'<[^>]+>', '', str(raw_source or '')).strip()
    src = _html.unescape(src)
    return _re.sub(r'\s+', ' ', src)[:30]


def _normalize_mblog(mblog_raw):
    """将 mblog 对象（PC 或移动端格式）标准化为统一输出格式。"""
    if not mblog_raw or not isinstance(mblog_raw, dict):
        return None
    if not mblog_raw.get("id"):
        return None
    text = _clean_mblog_text(mblog_raw.get("text", ""))
    user = mblog_raw.get("user") or {}
    if not isinstance(user, dict):
        user = {}
    # 转发帖：真实内容在被转发的原博里，主文本只剩「转发微博」等噪声时用原博替换
    retweeted = None
    rt_raw = mblog_raw.get("retweeted_status")
    if isinstance(rt_raw, dict) and rt_raw.get("id"):
        rt_user_obj = rt_raw.get("user") or {}
        rt_user_name = rt_user_obj.get("screen_name", "") if isinstance(rt_user_obj, dict) else ""
        retweeted = {
            "mid": rt_raw.get("idstr") or str(rt_raw.get("id", "")),
            "text": _clean_mblog_text(rt_raw.get("text", "")),
            "user": {"screen_name": rt_user_name},
            "created_at": rt_raw.get("created_at", ""),
        }
        if (not text or text in _REPOST_NOISE
                or any(text.startswith(n) for n in _REPOST_NOISE)):
            text = f"//@{rt_user_name}: {retweeted['text']}" if rt_user_name else retweeted["text"]
    # pics: pic_ids (list of str) or pic_infos (dict)
    pics = []
    pic_infos = mblog_raw.get("pic_infos") or {}
    if pic_infos:
        for pic_info in pic_infos.values():
            if isinstance(pic_info, dict):
                # original can be a string URL or a dict with url key
                original = pic_info.get("original")
                if isinstance(original, str) and original:
                    pics.append(original)
                elif isinstance(original, dict) and original.get("url"):
                    pics.append(original["url"])
                else:
                    large = pic_info.get("large")
                    if isinstance(large, str) and large:
                        pics.append(large)
                    elif isinstance(large, dict) and large.get("url"):
                        pics.append(large["url"])
    elif mblog_raw.get("pic_ids"):
        # pic_ids are photo IDs, convert to URLs
        for pic_id in mblog_raw["pic_ids"]:
            pics.append(f"https://wx2.sinaimg.cn/orj1080/{pic_id}.jpg")
    return {
        "mid": mblog_raw.get("idstr") or str(mblog_raw.get("id", "")),
        "text": text,
        "created_at": mblog_raw.get("created_at", ""),
        "source": _clean_source(mblog_raw.get("source", "")),
        "reposts_count": mblog_raw.get("reposts_count", 0),
        "comments_count": mblog_raw.get("comments_count", 0),
        "attitudes_count": mblog_raw.get("attitudes_count", 0),
        "pics": pics,
        "retweeted": retweeted,
        "user": {
            "id": user.get("idstr") or str(user.get("id", "")),
            "screen_name": user.get("screen_name", ""),
            "profile_image_url": str(user.get("profile_image_url", "") or "").replace("http://", "https://"),
        },
    }


def _extract_posts_from_payload(payload):
    """从 API 响应中提取帖子列表，兼容 PC chaohua/page 和移动端 container/getIndex。"""
    if not isinstance(payload, dict):
        return []
    results = []

    # 格式 1: PC chaohua/page - items[].data 直接是 mblog
    items = payload.get("items") or []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            mblog = item.get("data")
            if not isinstance(mblog, dict):
                mblog = item.get("mblog")
            if isinstance(mblog, dict) and (item.get("category") == "feed" or mblog.get("id") or mblog.get("idstr")):
                normalized = _normalize_mblog(mblog)
                if normalized:
                    results.append(normalized)
    if results:
        return results

    # 格式 2: 移动端 container/getIndex - data.cards[].card_group[].mblog（含嵌套一层）
    data = payload.get("data")
    if not isinstance(data, dict):
        return results
    cards = data.get("cards") or []
    if not isinstance(cards, list):
        cards = []

    def _collect_from_group(group, depth=0):
        found = []
        if not isinstance(group, list):
            return found
        for entry in group:
            if not isinstance(entry, dict):
                continue
            nested = entry.get("card_group")
            if isinstance(nested, list) and depth < 2:
                found.extend(_collect_from_group(nested, depth + 1))
                continue
            mblog = entry.get("mblog")
            if isinstance(mblog, dict):
                normalized = _normalize_mblog(mblog)
                if normalized:
                    found.append(normalized)
        return found

    for card in cards:
        if not isinstance(card, dict):
            continue
        results.extend(_collect_from_group(card.get("card_group")))

    # 格式 3: data.cards 直接是 mblog 列表（无 card_group 包装）
    if not results:
        for card in cards:
            if not isinstance(card, dict):
                continue
            mblog = card.get("mblog") or card
            normalized = _normalize_mblog(mblog)
            if normalized:
                results.append(normalized)

    # 格式 4: data.list[] / data.items[] 直接是 mblog 列表
    if not results:
        direct_list = data.get("list") or data.get("items") or []
        if isinstance(direct_list, list):
            for entry in direct_list:
                if not isinstance(entry, dict):
                    continue
                mblog = entry.get("mblog") or entry
                normalized = _normalize_mblog(mblog)
                if normalized:
                    results.append(normalized)

    return results


def _format_weibo_time(raw_time: str) -> str:
    """将微博时间格式化为可读字符串。
    处理：
    - 相对时间（"10分钟前"、"今天 12:30"）→ 转为 YYYY-MM-DD HH:MM
    - 已经是标准格式的 → 原样返回
    - 无法解析 → 返回原始值
    """
    if not raw_time:
        return ""
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.now()

    try:
        # PC 端英文格式 "Fri Sep 25 21:13:15 +0800 2026"
        if len(raw_time) >= 25 and raw_time[0] in "MonTueWedThuFriSat" and " " in raw_time[:4]:
            dt = _dt.strptime(raw_time, "%a %b %d %H:%M:%S %z %Y")
            return dt.astimezone().strftime("%Y-%m-%d %H:%M")
        # 标准格式 YYYY-MM-DD HH:MM 或更长
        if len(raw_time) >= 10 and raw_time[4] == '-' and raw_time[7] == '-':
            return raw_time[:16]
        # "今天 HH:MM"
        if raw_time.startswith("今天"):
            t = raw_time.replace("今天", "").strip()
            return f"{now.strftime('%Y-%m-%d')} {t}"
        # "昨天 HH:MM"
        if raw_time.startswith("昨天"):
            t = raw_time.replace("昨天", "").strip()
            yest = now - _td(days=1)
            return f"{yest.strftime('%Y-%m-%d')} {t}"
        # "N分钟前"
        if "分钟前" in raw_time:
            n = int(raw_time.replace("分钟前", "").strip())
            out = now - _td(minutes=n)
            return out.strftime("%Y-%m-%d %H:%M")
        # "N小时前"
        if "小时前" in raw_time:
            n = int(raw_time.replace("小时前", "").strip())
            out = now - _td(hours=n)
            return out.strftime("%Y-%m-%d %H:%M")
        # "N天前"
        if "天前" in raw_time:
            n = int(raw_time.replace("天前", "").strip())
            out = now - _td(days=n)
            return out.strftime("%Y-%m-%d %H:%M")
        # "MM-DD HH:MM"（无年份，补当前年）
        if len(raw_time) >= 11 and raw_time[2] == '-':
            return f"{now.year}-{raw_time}"
        # "YYYY-MM-DD" 纯日期
        if len(raw_time) == 10 and raw_time[4] == '-':
            return f"{raw_time} 00:00"
        # "刚刚" → 当前时间
        if raw_time.strip() == "刚刚":
            return now.strftime("%Y-%m-%d %H:%M")
        # "X秒前" → 当前时间
        if "秒前" in raw_time:
            return now.strftime("%Y-%m-%d %H:%M")
        # "X个月前" → 近似计算
        if "个月前" in raw_time:
            n = int(raw_time.replace("个月前", "").strip())
            out = now - _td(days=n * 30)
            return out.strftime("%Y-%m-%d %H:%M")
        # "X年前" → 近似计算
        if "年前" in raw_time:
            n = int(raw_time.replace("年前", "").strip())
            out = now - _td(days=n * 365)
            return out.strftime("%Y-%m-%d %H:%M")
        # "昨天" 单独出现（无时间）
        if raw_time.strip() == "昨天":
            yest = now - _td(days=1)
            return yest.strftime("%Y-%m-%d %H:%M")
        # "今天" 单独出现（无时间）
        if raw_time.strip() == "今天":
            return now.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return raw_time


def fetch_topic_posts(session, cookies, containerid: str, channel="auto",
                      proxy=None, force=False, allow_fallback=True, count: int = 20):
    """拉取指定超话的最新帖子列表（默认前 count 条）。

    微博超话帖子接口是公开的，不需要登录态。
    优先尝试 PC 端 chaohua/page，失败则回退到移动端 container/getIndex。
    """
    posts = []
    errors = []
    tried = 0

    # 清理 containerid，去掉可能的后缀
    clean_cid = containerid.split("_-_")[0] if "_-_" in containerid else containerid
    # 去掉可能已存在的 100808 前缀
    if clean_cid.startswith("100808"):
        clean_cid = clean_cid[6:]

    # 端点列表（按优先级排序，确保获取最新帖子而非精华帖）
    endpoints = [
        # PC 端（chaohua/page）- 按时间排序（最可靠）
        {
            "url": f"{BASE_PC}/ajax_proxy/chaohua/page",
            "params": {"flowId": f"100808{clean_cid}_-_sort_time", "vtype": 1},
            "headers": {"Referer": f"{BASE_PC}/p/100808{clean_cid}"},
        },
        # PC 端（chaohua/page）- 默认排序
        {
            "url": f"{BASE_PC}/ajax_proxy/chaohua/page",
            "params": {"flowId": f"100808{clean_cid}", "vtype": 1},
            "headers": {"Referer": f"{BASE_PC}/p/100808{clean_cid}"},
        },
        # 移动端（container/getIndex）- 带 100808 前缀
        {
            "url": f"{BASE}/api/container/getIndex",
            "params": {"containerid": f"100808{clean_cid}", "page": 1, "count": 25, "vtype": 1},
            "headers": {"Referer": f"{BASE}/p/100808{clean_cid}"},
        },
        # 移动端（container/getIndex）- 无前缀
        {
            "url": f"{BASE}/api/container/getIndex",
            "params": {"containerid": clean_cid, "page": 1, "count": 25, "vtype": 1},
            "headers": {"Referer": f"{BASE}/p/{clean_cid}"},
        },
        # 移动端 - _all 全部（回退方案）
        {
            "url": f"{BASE}/api/container/getIndex",
            "params": {"containerid": f"100808{clean_cid}_all", "page": 1, "count": 25, "vtype": 1},
            "headers": {"Referer": f"{BASE}/p/100808{clean_cid}"},
        },
    ]

    for ep in endpoints:
        page = 1
        since_id = ""
        tried += 1
        while len(posts) < count:
            params = dict(ep["params"])
            if page > 1:
                params["page"] = page
            if since_id:
                params["since_id"] = since_id
            try:
                # 端点专属 headers 走请求级参数，避免污染 session 级配置
                payload = request_json(
                    session, "GET", ep["url"], params=params, cookies=cookies,
                    headers=ep.get("headers") or None,
                    channel=channel, proxy=proxy, force=force,
                    allow_fallback=allow_fallback,
                )
            except NetworkError as exc:
                errors.append(f"[{tried}] {ep['url']} p{page}: 网络错误 {safe_exc(exc)}")
                break
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                if status == 432:
                    # 微博风控拦截：通常是无/低可信 Cookie 触发
                    errors.append(f"[{tried}] {ep['url']} p{page}: 微博风控拦截(432)")
                else:
                    errors.append(f"[{tried}] {ep['url']} p{page}: 请求失败 {safe_exc(exc)}")
                break
            except RuntimeError as exc:
                errors.append(f"[{tried}] {ep['url']} p{page}: 请求失败 {safe_exc(exc)}")
                break
            except Exception as exc:
                errors.append(f"[{tried}] {ep['url']} p{page}: 未知错误 {safe_exc(exc)}")
                break

            # 检查响应状态（PC 端没有 ok 字段，直接有 items）
            if "items" in payload:
                # PC 端格式
                pass
            else:
                # 移动端格式，检查 ok
                ok_val = payload.get("ok")
                if ok_val == -100:
                    # 带 Cookie 时 -100 是登录态失效；不带 Cookie 则是微博要求登录
                    detail = "Cookie 过期" if cookies else "微博要求登录（匿名访问已关闭）"
                    errors.append(f"[{tried}] {ep['url']} p{page}: {detail}")
                    break
                if ok_val != 1 and ok_val != "1":
                    log.warning(f"fetch_topic_posts: ok={ok_val!r}, url={ep['url']}, page={page}")
                    break

            extracted = _extract_posts_from_payload(payload)
            if not extracted:
                errors.append(f"[{tried}] {ep['url']} p{page}: 无帖子数据")
                break

            posts.extend(extracted)
            if len(posts) >= count:
                break

            # 翻页：since_id 优先，否则用 page 递增；两种方式都受 MAX_TOPIC_PAGES 约束
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            cardlist_info = data.get("cardlistInfo") if isinstance(data.get("cardlistInfo"), dict) else {}
            next_since = cardlist_info.get("since_id", "")
            if next_since:
                since_id = next_since
            else:
                page += 1
            if page > MAX_TOPIC_PAGES:
                break
            time.sleep(0.3)

        if posts:
            break

    # Deduplicate by mid
    seen_mids = set()
    unique_posts = []
    for p in posts:
        mid = p.get("mid", "")
        if mid and mid in seen_mids:
            continue
        seen_mids.add(mid)
        unique_posts.append(p)

    # 格式化时间
    for p in unique_posts[:count]:
        p["created_at"] = _format_weibo_time(p.get("created_at", ""))

    error_msg = "; ".join(errors) if errors else ""
    return {"posts": unique_posts[:count], "error": error_msg, "tried": tried}


class NetworkError(RuntimeError):
    """整遍网络层失败。"""


class CookieExpiredError(RuntimeError):
    """微博接口明确判定登录态已失效（区别于网络失败与接口抖动）。"""


class RateLimitedError(NetworkError):
    """微博风控/限流（HTTP 429、432）。是网络类失败，但不能当成登录过期。"""


# ========================= Cookie 工具 =========================

def normalize_cookie(raw) -> dict:
    """将 dict / 字符串 Cookie 统一成 dict。"""
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v is not None}
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return normalize_cookie(parsed)
            except json.JSONDecodeError:
                pass
        result = {}
        for part in raw.split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                result[key.strip()] = value.strip()
        return result
    return {}


def cookie_to_string(cookie_dict: dict) -> str:
    """dict Cookie 转回字符串，便于回写。"""
    return "; ".join(f"{k}={v}" for k, v in cookie_dict.items() if v)


# 微博登录态的真实凭据：SUB 单独存在不算登录（m.weibo.cn 会给未登录访客
# 下发一个"假 SUB"），必须再带上三者之一。
REAL_LOGIN_KEYS = ("SCF", "SSOLoginState", "ALF")
# 值得回写的字段：微博轮换/补发这些 key 说明登录态在续期。
ROTATE_CRITICAL_KEYS = ("SUB", "SUBP", "MLOGIN") + REAL_LOGIN_KEYS


def is_real_login(cookies: dict) -> bool:
    """是否具备真实登录凭据（而非匿名访客的裸 SUB）。"""
    return bool(cookies.get("SUB")) and any(cookies.get(key) for key in REAL_LOGIN_KEYS)


def harvest_cookie_rotation(session, base: dict) -> tuple[dict, list, list]:
    """合并响应里的 Set-Cookie，返回 (合并结果, 值变化的 key, 本次新增的 key)。

    微博会在登录态有效期内轮换 SUB/SUBP、并按需补发缺失的 SCF 等字段；
    把这些变化持久化下去，登录态才能自我续期。
    """
    merged = dict(base)
    changed: list[str] = []
    added: list[str] = []
    for cookie in session.cookies:
        if not cookie.value:
            continue
        if cookie.name not in base:
            added.append(cookie.name)
        elif base[cookie.name] != cookie.value:
            changed.append(cookie.name)
        merged[cookie.name] = cookie.value
    return merged, list(dict.fromkeys(changed)), list(dict.fromkeys(added))


def cookie_diff_gate(changed: list, added: list) -> bool:
    """是否需要把合并结果回写数据库。

    只认同名 key 的变化会漏掉微博补发的登录凭据（库里那份永远缺 SCF 就再也
    补不回来）；但也不能让微博顺带下发的无关 cookie 污染库，所以新增字段
    只在命中登录关键字段时才触发回写。
    """
    if changed:
        return True
    return any(key in ROTATE_CRITICAL_KEYS for key in added)


def reject_cookie_shrink(base: dict, merged: dict) -> str | None:
    """合并结果丢了登录凭据时给出拒绝原因，避免把一次脏响应写回库里判死账号。"""
    if base.get("SUB") and not merged.get("SUB"):
        return "合并结果丢失 SUB"
    if any(base.get(key) for key in REAL_LOGIN_KEYS) and \
            not any(merged.get(key) for key in REAL_LOGIN_KEYS):
        return "合并结果丢失全部登录关键字段"
    return None


def parse_cookie_expiry(raw_cookie) -> datetime | None:
    """解析 cookie 中的 ALF（account login expire time，秒级时间戳）。

    ALF 常写着一年，而服务端 session 实际约 7 天就失效，所以它只是"名义到期"，
    预警以 observed_deadline 为准。
    """
    cookies = normalize_cookie(raw_cookie)
    raw = str(cookies.get("ALF") or "").strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    if value > 10_000_000_000:  # 毫秒时间戳
        value //= 1000
    if value < 1_000_000_000:   # 2001 年以前，判定为脏值
        return None
    try:
        return datetime.fromtimestamp(value)
    except (OverflowError, OSError, ValueError):
        return None


def parse_time(value) -> datetime | None:
    """解析库里的字符串时间（_now() 格式），失败返回 None。"""
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def describe_cookie_lifespan(acc: dict, *, ttl_days: int = 7,
                             warn_days: float = 2,
                             now: datetime | None = None) -> dict:
    """综合 ALF 名义到期与经验 session 寿命，给出到期视图。

    取多把尺子里最早的那个作为 deadline：ALF 常写着一年，而微博实测在登录后约
    7 天就吊销服务端 session。特别注意不能只以"上次探活成功"为基准 —— 每轮保活
    都会把那个时钟归零，于是永远报不出"即将失效"；凭证录入时间（cookie_issued_at）
    才是那把不会退休的尺子。
    """
    now = now or datetime.now()
    alf_at = parse_cookie_expiry(acc.get("cookie") or acc.get("cookie_raw") or "")

    horizon = timedelta(days=max(ttl_days, 1))
    candidates = []
    for key in ("cookie_issued_at", "cookie_last_rotated_at", "cookie_last_seen_valid_at"):
        anchor = parse_time(acc.get(key))
        if anchor:
            candidates.append(anchor + horizon)
    observed_at = min(candidates) if candidates else None

    every_one = [dt for dt in (alf_at, observed_at) if dt]
    deadline = min(every_one) if every_one else None

    days_left = round((deadline - now).total_seconds() / 86400, 2) if deadline else None
    if deadline is None:
        state = "unknown"
        reason = "缺少 ALF 与续期记录，无法估算到期"
    elif days_left <= 0:
        state = "expired"
        reason = "按已有记录已到预计失效时间"
    elif days_left <= warn_days:
        state = "expiring"
        reason = f"预计 {days_left:g} 天后失效"
    else:
        state = "ok"
        reason = f"预计还能用 {days_left:g} 天"

    return {
        "alf_expires_at": alf_at.strftime("%Y-%m-%d %H:%M:%S") if alf_at else "",
        "observed_deadline": observed_at.strftime("%Y-%m-%d %H:%M:%S") if observed_at else "",
        "deadline": deadline.strftime("%Y-%m-%d %H:%M:%S") if deadline else "",
        "days_left": days_left,
        "state": state,
        "reason": reason,
    }


# ========================= 代理 =========================

def parse_proxies(raw: str) -> list[str]:
    """解析代理配置，支持换行 / 逗号 / 分号。仅保留 socks5:// 开头。"""
    if not raw:
        return []
    items = re.split(r"[\s,;]+", raw)
    urls = [i.rstrip("/") for i in items if i]
    return [u for u in urls if u.startswith(("socks5://", "socks5h://"))]


def proxy_proxies_dict(url: str) -> dict:
    return {"http": url, "https": url}


def proxy_display_name(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or "未知"
    return f"{host}:{parsed.port}" if parsed.port else host


# ========================= 请求 =========================

def request_via_proxy(session, method, url, channel="auto", proxy=None,
                      force=False, allow_fallback=True, **kwargs):
    """
    channel: auto / socks / direct
    force: 严格代理，失败不回退直连。
    allow_fallback: 允许请求级失败回退直连。
    """
    if channel == "direct":
        kwargs.pop("proxies", None)
        return session.request(method, url, **kwargs)

    if proxy and channel in ("socks", "auto"):
        if not SOCKS_AVAILABLE:
            msg = ("配置了 SOCKS5 代理，但缺少 PySocks 支持。请安装：pip install PySocks "
                   "（或移除设置里的 SOCKS 代理节点改用直连）")
            log.error(msg)
            if channel == "socks" or force or not allow_fallback:
                raise NetworkError(msg)
            log.warning("PySocks 缺失，回退直连")
            kwargs.pop("proxies", None)
            return session.request(method, url, **kwargs)

        pk = dict(kwargs)
        merged = proxy_proxies_dict(proxy)
        merged.update(pk.get("proxies") or {})
        pk["proxies"] = merged
        try:
            return session.request(method, url, **pk)
        except _RETRYABLE as exc:
            # 异常文本可能携带 socks5://user:pass@host 完整代理 URL，必须脱敏
            if channel == "socks":
                raise NetworkError(f"socks 代理请求失败：{safe_exc(exc)}") from exc
            if force or not allow_fallback:
                raise NetworkError(f"socks 代理请求失败：{safe_exc(exc)}") from exc
            log.warning("socks 代理失败（%s），回退直连", safe_exc(exc))
        except Exception:
            raise

    kwargs.pop("proxies", None)
    return session.request(method, url, **kwargs)


def request_json(session, method, url, channel="auto", proxy=None,
                 force=False, allow_fallback=True, url_guard_hosts=None,
                 **kwargs):
    """url_guard_hosts：初始 URL 及每个重定向目标都限定在该 host 后缀白名单内
    （checkin_topic 的 scheme 来自微博响应，30x 目标不可信，必须逐跳校验）。"""
    if url_guard_hosts is not None:
        response = guarded_request(
            session, method, url,
            allowed_hosts=url_guard_hosts,
            sender=lambda m, u, **kw: request_via_proxy(
                session, m, u, channel=channel, proxy=proxy,
                force=force, allow_fallback=allow_fallback, **kw,
            ),
            timeout=kwargs.pop("timeout", 15), **kwargs,
        )
    else:
        response = request_via_proxy(
            session, method, url, channel=channel, proxy=proxy,
            force=force, allow_fallback=allow_fallback,
            timeout=kwargs.pop("timeout", 15), **kwargs,
        )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"微博接口返回非 JSON：HTTP {response.status_code}，URL={url}"
        ) from exc


# ========================= 微博接口 =========================

def verify_cookie(session, cookies, channel="auto", proxy=None,
                  force=False, allow_fallback=True):
    try:
        data = request_json(
            session, "GET", CONFIG_URL, cookies=cookies,
            channel=channel, proxy=proxy, force=force,
            allow_fallback=allow_fallback,
        ).get("data", {})
        return bool(data.get("login")), data.get("st")
    except NetworkError:
        raise
    except requests.exceptions.HTTPError as exc:
        status = getattr(exc.response, "status_code", 0)
        if status in (429, 432):
            # 风控要用限流身份上报：按"网络失败"处理会退避重试，
            # 按"登录过期"处理又会把好好的账号误判死、催用户重新扫码。
            raise RateLimitedError(f"微博风控拦截（HTTP {status}）") from exc
        raise NetworkError(f"验证 Cookie 请求失败：HTTP {status}") from exc
    except requests.exceptions.RequestException as exc:
        raise NetworkError(f"验证 Cookie 网络失败：{safe_exc(exc)}") from exc
    except Exception as exc:
        # 解析错误等瞬时异常不能误判为"未登录"（会导致账号被错误标记
        # cookie_invalid、要求重新扫码），统一按网络类失败上抛。
        raise NetworkError(f"验证 Cookie 响应异常：{safe_exc(exc)}") from exc


def touch_passport_endpoints(session, cookies, urls, channel="auto", proxy=None,
                             force=False, allow_fallback=True) -> list[str]:
    """实验性（v1.3.6）：逐个请求 PC 端候选端点，尝试让微博换发新的登录凭据。

    服务端保活平时只戳 m.weibo.cn，临期换不动时没有第二手段。这里对管理员
    配置的 PC 端点（weibo.com 首页 / passport SSO 跳转等）各发一次带登录态的
    GET，响应 Set-Cookie 落进账号专属 session 后由 harvest 机制统一回收。
    微博没有公开承诺这些端点必然换发，有效性以 rotate-probe 实测为准。

    返回实际请求成功的 URL 列表。每个 URL 出站前必须过 validate_weibo_url
    白名单（非微博域 / 私网地址直接跳过）；单端点失败只记日志，绝不影响
    保活主流程的判定 —— 实验手段不能反过来把账号观测搞脏。
    """
    touched = []
    for raw in (urls or []):
        url = str(raw or "").strip()
        if not url:
            continue
        try:
            validate_weibo_url(url)
        except Exception as exc:
            log.warning("passport 换发跳过非法端点 %r：%s", url, safe_exc(exc))
            continue
        try:
            # PC 端点返回的是 HTML 而非 JSON，直接走 request_via_proxy；
            # cookies 已在 session jar 里，显式再带一份保证 m 域凭据同步携带。
            request_via_proxy(
                session, "GET", url, channel=channel, proxy=proxy,
                force=force, allow_fallback=allow_fallback,
                timeout=15, cookies=cookies,
                headers={"Referer": BASE_PC + "/"},
            )
            touched.append(url)
        except Exception as exc:
            log.info("passport 换发端点请求失败 %s：%s", url, safe_exc(exc))
    return touched


def _normalize_topic_id(raw_id: str) -> str:
    """统一超话 containerid：微博 scheme 里常带 -feeds / %2B 等后缀，
    不归一会导致同一超话在去重表里出现多条记录（名字、头像各缺一半）。"""
    if not raw_id:
        return ""
    match = re.search(r"100808\d+", raw_id)
    return match.group() if match else raw_id.strip()


def get_followed_topics(session, cookies, channel="auto", proxy=None,
                        force=False, allow_fallback=True):
    """获取关注超话列表，返回 [{name,id,scheme,done,avatar,description,member_count}]。"""
    topics = []
    since_id = ""
    seen_pages = set()
    while True:
        params = {"containerid": FOLLOWED_CONTAINER}
        if since_id:
            params["since_id"] = since_id
        payload = request_json(
            session, "GET", TOPICS_URL, params=params, cookies=cookies,
            channel=channel, proxy=proxy, force=force,
            allow_fallback=allow_fallback,
        )
        ok_val = payload.get("ok")
        if ok_val != 1 and ok_val != "1":
            # 不要把登录过期、风控或微博接口错误误报成「没有关注超话」。
            detail = payload.get("msg") or payload.get("errmsg") or payload.get("error") or ok_val
            if not topics:
                if str(ok_val) == "-100":
                    # 微博用 ok=-100 表示登录态已失效，要和接口抖动区分开，
                    # 否则 cookie 过期会被误标成普通失败、不提示重新登录。
                    raise CookieExpiredError(f"Cookie 已失效，微博要求重新登录：{detail}")
                raise RuntimeError(f"微博未能返回关注超话列表：{detail}")
            log.warning("关注超话翻页提前结束：%s", detail)
            break
        data = payload.get("data") or {}
        for card in data.get("cards", []):
            items = card.get("card_group") or [card]
            for item in items:
                if str(item.get("card_type", "")) != "8" or not item.get("buttons"):
                    continue
                name = item.get("title_sub", "").strip()
                scheme = item.get("scheme", "")
                query = parse_qs(urlparse(scheme).query)
                topic_id = _normalize_topic_id(query.get("containerid", [""])[0])
                button = item["buttons"][0]
                button_name = button.get("name", "")
                button_scheme = button.get("scheme")
                done = (
                    button_name in ("已签", "已簽", "已签到", "已簽到")
                    or not button_scheme
                )
                # 提取头像、描述、成员数（m.weibo.cn 关注卡片头像在 icon 字段）
                avatar = (item.get("pic", "") or item.get("portrait", "")
                          or item.get("icon", "") or "")
                if avatar.startswith("//"):
                    avatar = "https:" + avatar
                avatar = avatar.replace("http://", "https://")
                desc = item.get("desc", "") or item.get("desc1", "") or ""
                member = item.get("member_count", 0) or 0
                if name:
                    topics.append({
                        "name": name,
                        "id": topic_id,
                        "scheme": None if done else button_scheme,
                        "done": done,
                        "avatar": avatar,
                        "description": desc,
                        "member_count": member,
                    })
        since_id = (data.get("cardlistInfo") or {}).get("since_id", "")
        if not since_id or since_id in seen_pages:
            break
        seen_pages.add(since_id)
        time.sleep(0.5)
    return topics


def _positive_int(value) -> int | None:
    """微博各版本接口把排名字段返回成 int / '12' / '第12名' 都有可能。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        if match:
            number = int(match.group())
            return number if number > 0 else None
    return None


def extract_checkin_rank(response) -> int | None:
    """从签到响应中提取超话签到排名，取不到时返回 None。"""
    if not isinstance(response, (dict, list)):
        return None

    # 越具体的键优先，避免误取榜单总长度或粉丝团等级里的数字
    rank_keys = (
        "rank", "today_rank", "sign_rank", "checkin_rank",
        "cur_rank", "rank_index", "rank_num", "position",
    )
    # 微博接口排名散落在不同嵌套层级，按可能性从高到低逐层查找
    containers = (
        ("data",),
        ("result",),
        ("data", "result"),
        ("data", "sign_data"),
        ("data", "checkin_data"),
        ("data", "extra"),
        ("sign_data",),
        ("checkin_data",),
        ("extra",),
        (),
    )
    for level in containers:
        for key in rank_keys:
            for path in _iter_rank_candidates(response, level, key):
                rank = _positive_int(path)
                if rank is not None:
                    return rank
    return None


def _iter_rank_candidates(node, path: tuple, wanted_key: str):
    """按给定容器路径查找目标键，并递归展开 JSON 字符串。"""
    if isinstance(node, str):
        text = node.strip()
        if text.startswith(("{", "[")):
            try:
                node = json.loads(text)
            except (ValueError, TypeError):
                return
        else:
            return
    if isinstance(node, dict):
        if not path:
            if wanted_key in node:
                yield node[wanted_key]
            return
        head, rest = path[0], path[1:]
        if head in node:
            yield from _iter_rank_candidates(node[head], rest, wanted_key)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_rank_candidates(item, path, wanted_key)


def checkin_topic(session, cookies, scheme, st, channel="auto", proxy=None,
                  force=False, allow_fallback=True):
    if not scheme:
        raise RuntimeError("缺少签到链接 scheme")
    url = scheme if scheme.startswith("http") else BASE + scheme
    # scheme 来自微博接口响应，属不可信输入：绝对 URL 只允许微博自家 host，
    # 并拒绝私有/保留地址，防止账号 Cookie 被发往任意外部主机；
    # 重定向目标同样不可信，逐跳走同一套守卫（url_guard_hosts）。
    url = validate_weibo_url(url)
    separator = "&" if "?" in url else "?"
    return request_json(
        session, "GET", f"{url}{separator}st={st}", cookies=cookies,
        channel=channel, proxy=proxy, force=force, allow_fallback=allow_fallback,
        url_guard_hosts=WEIBO_HOSTS,
    )


def merge_refreshed_cookies(session, cookie_dict: dict) -> tuple[dict, list]:
    """已废弃：只报告同名 key 的变化，会漏掉微博补发的登录凭据。
    新代码用 harvest_cookie_rotation + cookie_diff_gate。"""
    merged, changed, _added = harvest_cookie_rotation(session, cookie_dict)
    return merged, changed


# ========================= 单账号签到 =========================

class CheckinOptions:
    """签到参数配置。"""

    def __init__(self, checkin_delay_min=3, checkin_delay_max=8,
                 proxies=None, proxy_force=False, proxy_fallback=True):
        self.checkin_delay_min = checkin_delay_min
        self.checkin_delay_max = checkin_delay_max
        self.proxies = proxies or []
        self.proxy_force = proxy_force
        self.proxy_fallback = proxy_fallback

    @classmethod
    def from_settings(cls, db_get):
        def _int(key: str, default: int) -> int:
            try:
                return int(db_get(key, str(default)) or default)
            except (TypeError, ValueError):
                log.warning("设置 %s 值损坏，回退默认 %s", key, default)
                return default

        return cls(
            checkin_delay_min=_int("checkin_delay_min", 3),
            checkin_delay_max=_int("checkin_delay_max", 8),
            proxies=parse_proxies(db_get("proxies", "")),
            proxy_force=db_get("proxy_force", "0") == "1",
            proxy_fallback=db_get("proxy_fallback", "1") != "0",
        )


def run_account_checkin(cookie_dict: dict, opts: CheckinOptions,
                        proxy_url: str | None = None,
                        proxy_index: int = 0) -> dict:
    """对单个账号执行一遍签到。"""
    cookie_dict = normalize_cookie(cookie_dict)
    if not cookie_dict:
        return _bundle("failed", "Cookie 为空，请重新登录", 0, 0, 0, [],
                       cookie_dict, [], failure_type="cookie_invalid")

    proxy = None
    channel = "direct"
    if proxy_url and proxy_url.strip().startswith(("socks5://", "socks5h://")):
        proxy = proxy_url.strip()
        channel = "socks"
    elif opts.proxies:
        proxy = opts.proxies[proxy_index % len(opts.proxies)]
        channel = "socks"

    session = new_account_session()

    try:
        logged_in, st = verify_cookie(
            session, cookie_dict, channel=channel, proxy=proxy,
            force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
        )
    except RateLimitedError as exc:
        return _bundle("failed", f"验证 Cookie 被风控：{safe_exc(exc)}", 0, 0, 0, [],
                       cookie_dict, [], channel, failure_type="rate_limited")
    except NetworkError as exc:
        return _bundle("failed", f"验证 Cookie 网络失败：{safe_exc(exc)}", 0, 0, 0, [],
                       cookie_dict, [], channel, failure_type="network")

    if not logged_in:
        merged, changed, added = harvest_cookie_rotation(session, cookie_dict)
        return _bundle("failed", "Cookie 无效或已过期，请重新登录", 0, 0, 0,
                       [], merged, changed, channel,
                       failure_type="cookie_invalid", cookie_added=added)

    try:
        topics = get_followed_topics(
            session, cookie_dict, channel=channel, proxy=proxy,
            force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
        )
    except NetworkError as exc:
        return _bundle("failed", f"获取超话列表网络失败：{safe_exc(exc)}", 0, 0, 0, [],
                       cookie_dict, [], channel)
    except CookieExpiredError as exc:
        return _bundle("failed", f"Cookie 已失效：{safe_exc(exc)}", 0, 0, 0, [],
                       cookie_dict, [], channel, failure_type="cookie_invalid")
    except RuntimeError as exc:
        # 列表接口的其它异常不能再冒泡：一个账号的接口抖动会中断整批签到。
        return _bundle("failed", f"获取超话列表失败：{safe_exc(exc)}", 0, 0, 0, [],
                       cookie_dict, [], channel, failure_type="topics_list_error")

    if not topics:
        merged, changed, added = harvest_cookie_rotation(session, cookie_dict)
        return _bundle("success", "没有关注超话", 0, 0, 0, [],
                       merged, changed, channel, cookie_added=added)

    results = []
    total = len(topics)
    for idx, topic in enumerate(topics, start=1):
        if topic["done"]:
            results.append({"name": topic["name"], "success": True,
                            "message": "今日已签到"})
            continue
        try:
            response = checkin_topic(
                session, cookie_dict, topic["scheme"], st,
                channel=channel, proxy=proxy,
                force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
            )
            resp_text = str(response)
            if str(response.get("errno")) == "100015" or "验签" in resp_text or "驗簽" in resp_text:
                logged_in, st = verify_cookie(
                    session, cookie_dict, channel=channel, proxy=proxy,
                    force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
                )
                if not logged_in:
                    # 登录态中途死亡要立刻收尾：继续拿旧 SUB 逐个撞接口只会加重风控，
                    # 而且这类失败必须标成 cookie_invalid，否则 TG 不会提示重新登录。
                    merged, changed, added = harvest_cookie_rotation(session, cookie_dict)
                    done = sum(1 for r in results if r["success"])
                    return _bundle("failed", "Cookie 在签到过程中失效", total, done,
                                   total - done, results, merged, changed, channel,
                                   failure_type="cookie_invalid", cookie_added=added)
                response = checkin_topic(
                    session, cookie_dict, topic["scheme"], st,
                    channel=channel, proxy=proxy,
                    force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
                )
            message = response.get("msg", "")
            success = (
                response.get("ok") == 1
                or "成功" in message
                or "已签到" in message
                or "已簽到" in message
            )
            entry = {"name": topic["name"], "success": success,
                     "message": message or str(response)[:100]}
            if success:
                entry["rank"] = extract_checkin_rank(response)
                if entry["rank"] is not None:
                    log.info("  🏆 %s 签到成功，今日第 %s 名",
                             topic["name"], entry["rank"])
                else:
                    log.info("  %s 签到成功，但响应中未找到排名：%.200s",
                             topic["name"], resp_text)
            results.append(entry)
        except NetworkError:
            merged, changed, added = harvest_cookie_rotation(session, cookie_dict)
            return _bundle("failed", "签到过程中网络失败", total,
                           sum(1 for r in results if r["success"]),
                           len(results) - sum(1 for r in results if r["success"]),
                           results, merged, changed, channel, cookie_added=added)
        except Exception as exc:
            results.append({"name": topic["name"], "success": False,
                            "message": safe_exc(exc)})
        if idx < total:
            time.sleep(random.uniform(opts.checkin_delay_min, opts.checkin_delay_max))

    merged, changed, added = harvest_cookie_rotation(session, cookie_dict)
    success_count = sum(1 for r in results if r["success"])
    fail_count = total - success_count
    status = "success" if fail_count == 0 else "partial"
    return _bundle(status, _checkin_summary_message(results), total,
                   success_count, fail_count, results, merged, changed, channel,
                   cookie_added=added)


def _checkin_summary_message(results: list) -> str:
    """汇总本次签到：有排名时在消息里带上，供日志与 TG 直接展示。"""
    ranked = [(r["name"], r["rank"]) for r in results
              if r.get("success") and isinstance(r.get("rank"), int)]
    if not ranked:
        return "签到完成"
    ranked.sort(key=lambda item: item[1])
    fastest, best_rank = ranked[0]
    worst_name, worst_rank = ranked[-1]
    average = round(sum(rank for _, rank in ranked) / len(ranked))
    detail = "、".join(f"{name} #{rank}" for name, rank in ranked[:5])
    if len(ranked) > 5:
        detail += f" …（共 {len(ranked)} 个）"
    return (f"签到完成 · 排名 {len(ranked)} 个：{detail}"
            f" ｜ 最快 {fastest} #{best_rank}，最慢 {worst_name} #{worst_rank}，平均 #{average}")


def _bundle(status, message, total, success, fail, results, cookie,
            cookie_changed, channel="direct", failure_type=None,
            cookie_added=None):
    return {
        "status": status,
        "message": message,
        "total": total,
        "success": success,
        "fail": fail,
        "results": results,
        "cookie": cookie,
        "cookie_changed": cookie_changed,
        "cookie_added": cookie_added or [],
        "channel": channel,
        "failure_type": failure_type,
    }
