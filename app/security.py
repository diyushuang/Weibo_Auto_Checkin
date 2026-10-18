"""
安全工具集（跨模块复用）：
- sanitize_message / safe_exc：从异常文本与日志中剥离代理凭据、Bot Token 等敏感信息。
  requests 的代理异常消息会携带完整 `socks5://user:pass@host` 代理 URL，
  Telegram 连接异常会携带 `/bot<TOKEN>/` 完整 URL，这些文本一旦入库或进通知
  就等于凭据泄漏，因此所有对外消息必须先过这里的过滤器。
- validate_outbound_url：出站 URL 守卫。凡 URL 来源不在代码常量里
  （微博接口返回的 scheme、用户提供的图片地址等），发请求前必须校验：
  仅允许 http/https，host 不得是 localhost/环回/私有/保留地址，
  可选 host 后缀白名单。校验失败抛 UnsafeURLError，宁可拒绝不可放行。

说明：管理员在设置页自行填写的 AI Base URL、SOCKS 代理属于运维自配置
的可信输入，不做私网限制（本地 LLM 网关、内网代理是合法场景），
但仍强制 scheme 校验。
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlunparse, urlparse

MAX_MESSAGE_LEN = 500

# userinfo 形式的凭据：scheme://user:pass@host
_USERINFO_RE = re.compile(r"(?<=//)[^/@\s]+@[^/\s]", re.IGNORECASE)
# Telegram Bot API URL 中的 token：/bot123456:ABCxxx/
_BOT_TOKEN_RE = re.compile(r"/bot\d+:[A-Za-z0-9_-]{20,}")
# OpenAI 风格 Bearer key（避免异常文本回显 sk-...）
_BEARER_RE = re.compile(r"(sk-[A-Za-z0-9_-]{16,})")


class UnsafeURLError(ValueError):
    """URL 未通过出站校验。"""


def sanitize_message(text: str) -> str:
    """清洗任意将入库/入日志/入通知的文本。"""
    if not text:
        return ""
    text = str(text)
    text = _USERINFO_RE.sub("***@", text)
    text = _BOT_TOKEN_RE.sub("/bot***", text)
    text = _BEARER_RE.sub("sk-***", text)
    # 压平换行，防止日志伪造；限制长度防止异常刷屏
    text = " ".join(text.split())
    if len(text) > MAX_MESSAGE_LEN:
        text = text[: MAX_MESSAGE_LEN - 3] + "..."
    return text


def safe_exc(exc: BaseException) -> str:
    """异常 → '类型名: 清洗后消息'。替代所有 f'...{exc}' / str(exc) 拼接。"""
    return sanitize_message(f"{type(exc).__name__}: {exc}")


# ========================= 出站 URL 守卫 =========================

_ALLOWED_SCHEMES = ("http", "https")

# 私网/保留段之外的本地称谓也一律拒绝
_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost",
                    "ip6-loopback", "metadata.google.internal"}


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _check_host(host: str) -> None:
    """校验单个 host：字面量 IP 直接判段，域名则解析后逐地址判断（防 DNS 重绑定）。"""
    if not host:
        raise UnsafeURLError("URL 缺少 host")
    if host.strip().lower().rstrip(".") in _LOCAL_HOSTNAMES:
        raise UnsafeURLError(f"禁止访问本地地址：{host}")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _is_forbidden_ip(addr):
            raise UnsafeURLError(f"禁止访问私有/保留地址：{host}")
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise UnsafeURLError(f"host 无法解析：{host}") from exc
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if _is_forbidden_ip(addr):
            raise UnsafeURLError(f"host 解析到私有/保留地址：{host}")


def validate_outbound_url(url: str, allowed_hosts: tuple[str, ...] = ()) -> str:
    """校验并原样返回 URL；不合法抛 UnsafeURLError。

    allowed_hosts：host 后缀白名单（大小写不敏感），为空表示不限制。
    """
    if not url or not isinstance(url, str):
        raise UnsafeURLError("URL 为空")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"仅允许 http/https 协议，收到：{parsed.scheme or '空'}")
    host = parsed.hostname or ""
    if allowed_hosts:
        host_lower = host.lower().rstrip(".")
        if not any(host_lower == h or host_lower.endswith("." + h)
                   for h in allowed_hosts):
            raise UnsafeURLError(f"host 不在允许范围内：{host}")
    _check_host(host)
    return url


# ========================= AI 接口地址校验（运维自配置） =========================

def validate_ai_base_url(base_url: str) -> str:
    """管理员配置的 AI Base URL：仅校验 scheme（本地网关是合法场景），返回规范化 URL。"""
    base_url = (base_url or "").strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.hostname:
        raise UnsafeURLError("AI Base URL 必须是合法的 http(s) 地址")
    return base_url


# ========================= 便捷封装 =========================

# 微博签到允许的 host 后缀（checkin_topic 只允许跟随微博自家链接）
WEIBO_HOSTS = ("weibo.cn", "weibo.com", "weibocdn.com")


def validate_weibo_url(url: str) -> str:
    return validate_outbound_url(url, allowed_hosts=WEIBO_HOSTS)
