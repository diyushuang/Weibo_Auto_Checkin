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
from urllib.parse import urljoin, urlunparse, urlparse

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

# 扫码登录的跨域同步回调还会落在新浪通行域（login.sina.com.cn 等），
# 仅登录链路（qrcode/check 回调 / login_url）放宽到这组，其余出站仍用 WEIBO_HOSTS。
WEIBO_LOGIN_HOSTS = WEIBO_HOSTS + ("sina.com.cn",)


def validate_weibo_url(url: str) -> str:
    return validate_outbound_url(url, allowed_hosts=WEIBO_HOSTS)


def validate_weibo_login_url(url: str) -> str:
    """扫码登录链路的出站校验：微博自家域 + 新浪通行域，拒绝其余 host 与私网地址。"""
    return validate_outbound_url(url, allowed_hosts=WEIBO_LOGIN_HOSTS)


# ========================= 逐跳受控请求 =========================

# requests 自动重定向不校验目标：白名单域上一个 30x 就能把请求（连带 Cookie jar
# 里域匹配的凭据）导去内网/云元数据地址，架空上面的出站守卫。凡初始 URL 来自
# 不可信输入（微博接口返回的 scheme / 头像地址 / 扫码回调），一律改走本函数：
# 关闭自动重定向，每个 30x 的 Location 解析成绝对地址后重新过 validate_outbound_url，
# 通过才跟随；不通过立即中止并抛 UnsafeURLError。
#
# 残余限制（记录于 SECURITY_AUDIT.md）：校验时解析 DNS、请求时 requests 会再次
# 独立解析，理论上存在重绑定窗口；本函数把校验收紧到"每跳发送前一刻"，已将
# 窗口压到 check-then-use 的最小形态，不做 IP 锁定（会破坏 TLS SNI 与代理链路）。
_MAX_REDIRECT_HOPS = 8


def guarded_request(session, method: str, url: str, *,
                    allowed_hosts: tuple[str, ...] = (),
                    sender=None, **kwargs):
    """带逐跳出站校验的 HTTP 请求（默认 GET 场景）。

    - session / sender：sender 缺省用 session.request；也可注入自定义传输
      （如带代理回退的 request_via_proxy），签名 (method, url, **kwargs)。
    - kwargs 原样传给每一跳（timeout / headers / cookies 等）；
      allow_redirects 由本函数强制关闭，不接受外部覆盖。
    - 返回最终 Response（非重定向状态）；重定向目标任一跳不合法、
      或超过 8 跳，抛 UnsafeURLError。
    """
    kwargs = dict(kwargs)
    kwargs["allow_redirects"] = False
    if sender is not None:
        send = sender
    elif hasattr(session, "request"):
        send = session.request
    else:
        # 兼容只实现 get 的鸭子类型会话对象（测试替身）；守卫出口当前全是 GET
        def send(method, url, _get=session.get, **kw):
            return _get(url, **kw)
    url = validate_outbound_url(url, allowed_hosts=allowed_hosts)
    response = None
    for _ in range(_MAX_REDIRECT_HOPS):
        response = send(method, url, **kwargs)
        if not getattr(response, "is_redirect", False):
            return response
        location = response.headers.get("Location", "")
        response.close()
        if not location:
            return response
        next_url = urljoin(url, location)
        try:
            next_url = validate_outbound_url(next_url, allowed_hosts=allowed_hosts)
        except UnsafeURLError as exc:
            raise UnsafeURLError(f"重定向目标被拒绝：{exc}") from exc
        url = next_url
    response.close()
    raise UnsafeURLError(f"重定向超过 {_MAX_REDIRECT_HOPS} 跳，疑似循环，已中止")
