"""测试微博客户端核心逻辑（Cookie 解析、防封、代理解析）。"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.weibo_client import (  # noqa: E402
    CheckinOptions,
    cookie_diff_gate,
    cookie_to_string,
    describe_cookie_lifespan,
    harvest_cookie_rotation,
    is_real_login,
    merge_refreshed_cookies,
    NetworkError,
    normalize_cookie,
    parse_cookie_expiry,
    parse_proxies,
    proxy_display_name,
    reject_cookie_shrink,
    RateLimitedError,
    CookieExpiredError,
)
from app.anti_ban import AntiBanPolicy, node_rotation  # noqa: E402


# ---------- Cookie 解析 ----------

def test_normalize_dict():
    assert normalize_cookie({"SUB": "a", "SCF": "b"}) == {"SUB": "a", "SCF": "b"}


def test_normalize_string():
    raw = "SUB=a; SUBP=b; SCF=c"
    assert normalize_cookie(raw) == {"SUB": "a", "SUBP": "b", "SCF": "c"}


def test_normalize_json_string():
    raw = '{"SUB": "a", "SCF": "b"}'
    assert normalize_cookie(raw) == {"SUB": "a", "SCF": "b"}


def test_normalize_empty():
    assert normalize_cookie("") == {}
    assert normalize_cookie(None) == {}


def test_cookie_to_string_roundtrip():
    d = {"SUB": "a", "SCF": "b"}
    s = cookie_to_string(d)
    assert normalize_cookie(s) == d


# ---------- 代理解析 ----------

def test_parse_proxies_single():
    assert parse_proxies("socks5://user:pass@1.1.1.1:8388") == \
        ["socks5://user:pass@1.1.1.1:8388"]


def test_parse_proxies_multi():
    raw = "socks5://a@1.1.1.1:1, socks5://b@2.2.2.2:2\nsocks5://c@3.3.3.3:3"
    out = parse_proxies(raw)
    assert len(out) == 3


def test_parse_proxies_ignore_http():
    assert parse_proxies("http://x:1") == []
    assert parse_proxies("") == []


def test_proxy_display_hides_password():
    name = proxy_display_name("socks5://user:secret@1.1.1.1:8388")
    assert "secret" not in name
    assert name == "1.1.1.1:8388"


# ---------- 防封 ----------

def test_anti_ban_window():
    # 构造一个永远 in_window 的策略（window_hour=99）
    p = AntiBanPolicy(enabled=True, wait_min=1, wait_max=3, window_hour=99)
    assert p.should_wait() is True
    p2 = AntiBanPolicy(enabled=False, wait_min=1, wait_max=3, window_hour=99)
    assert p2.should_wait() is False


def test_node_rotation():
    assert node_rotation(3, 1) == 0
    assert node_rotation(3, 2) == 1
    assert node_rotation(3, 4) == 0
    assert node_rotation(0, 5) == 0


# ---------- Cookie 合并 ----------

def test_merge_refreshed_cookies():
    class FakeCookie:
        def __init__(self, name, value):
            self.name = name
            self.value = value

    class FakeSession:
        cookies = [FakeCookie("SUB", "newval"), FakeCookie("SCF", "same")]

    old = {"SUB": "old", "SCF": "same"}
    merged, changed = merge_refreshed_cookies(FakeSession(), old)
    assert merged["SUB"] == "newval"
    assert merged["SCF"] == "same"
    assert changed == ["SUB"]


def test_merge_refreshed_cookies_ignores_new_cookie_for_change_detection():
    """响应中新出现的无关 Cookie 可以合并，但不应触发数据库回写。"""
    class FakeCookie:
        def __init__(self, name, value):
            self.name = name
            self.value = value

    class FakeSession:
        cookies = [FakeCookie("SUB", "same"), FakeCookie("WBTOKEN", "new-token")]

    merged, changed = merge_refreshed_cookies(FakeSession(), {"SUB": "same"})
    assert merged == {"SUB": "same", "WBTOKEN": "new-token"}
    assert changed == []


# ---------- CheckinOptions ----------

def test_checkin_options_from_settings():
    class FakeDB:
        def __call__(self, key, default=""):
            m = {
                "checkin_delay_min": "2", "checkin_delay_max": "5",
                "proxies": "socks5://a@1:1", "proxy_force": "0",
                "proxy_fallback": "1",
            }
            return m.get(key, default)

    opts = CheckinOptions.from_settings(FakeDB())
    assert opts.checkin_delay_min == 2
    assert opts.checkin_delay_max == 5
    assert len(opts.proxies) == 1
    assert opts.proxy_force is False


# ---------- Cookie 换发（保活的核心） ----------

class _JarCookie:
    def __init__(self, name, value):
        self.name = name
        self.value = value


class _JarSession:
    def __init__(self, pairs):
        self.cookies = [_JarCookie(n, v) for n, v in pairs]


def test_harvest_separates_changed_and_added():
    session = _JarSession([("SUB", "new"), ("SCF", "same"), ("SUBP", "fresh")])
    merged, changed, added = harvest_cookie_rotation(
        session, {"SUB": "old", "SCF": "same"})
    assert merged == {"SUB": "new", "SCF": "same", "SUBP": "fresh"}
    assert changed == ["SUB"]
    assert added == ["SUBP"]


def test_harvest_skips_empty_values():
    merged, changed, added = harvest_cookie_rotation(
        _JarSession([("SUB", "")]), {"SUB": "keep"})
    assert merged == {"SUB": "keep"}
    assert (changed, added) == ([], [])


def test_gate_opens_for_new_login_key():
    """微博补发 SCF 必须触发回写 —— 库里缺了这个字段就再也补不回来。"""
    assert cookie_diff_gate([], ["SCF"]) is True
    assert cookie_diff_gate(["SUB"], []) is True
    assert cookie_diff_gate([], ["SUB", "MLOGIN"]) is True


def test_gate_stays_closed_for_irrelevant_new_key():
    assert cookie_diff_gate([], []) is False
    assert cookie_diff_gate([], ["WBTOKEN", "SSO_LANG_V2"]) is False


def test_merge_refreshed_cookies_keeps_old_semantics():
    """兼容壳：只报同名变化，避免既有调用方语义漂移。"""
    merged, changed = merge_refreshed_cookies(
        _JarSession([("SUB", "new"), ("SCF", "same")]), {"SUB": "old", "SCF": "same"})
    assert merged["SUB"] == "new"
    assert changed == ["SUB"]


def test_reject_cookie_shrink_blocks_credential_loss():
    base = {"SUB": "s", "SCF": "c", "ALF": "123"}
    assert reject_cookie_shrink(base, {"SCF": "c", "ALF": "123"}) == "合并结果丢失 SUB"
    assert reject_cookie_shrink(base, {"SUB": "s2"}) == "合并结果丢失全部登录关键字段"
    assert reject_cookie_shrink(base, {"SUB": "s2", "SSOLoginState": "x"}) is None


def test_is_real_login_rejects_anonymous_sub_only():
    assert is_real_login({"SUB": "fake"}) is False
    assert is_real_login({"SUB": "s", "ALF": "1"}) is True


# ---------- ALF 与到期视图 ----------

def test_parse_cookie_expiry_seconds_and_millis():
    from datetime import datetime
    assert parse_cookie_expiry("ALF=1893456000") == datetime.fromtimestamp(1893456000)
    assert parse_cookie_expiry("ALF=1893456000000") == datetime.fromtimestamp(1893456000)


def test_parse_cookie_expiry_rejects_junk():
    assert parse_cookie_expiry("ALF=abc") is None
    assert parse_cookie_expiry("ALF=0") is None
    assert parse_cookie_expiry("ALF=999999999") is None   # 2001 年前，脏值
    assert parse_cookie_expiry("SUB=only") is None
    assert parse_cookie_expiry({"ALF": "1893456000"}) is not None


def test_lifespan_prefers_observed_deadline_over_alf():
    """ALF 常写着一年，但经验 session 寿命更短 —— 预警必须用更早的那把尺子。"""
    acc = {
        "cookie": "SUB=s; SCF=c; ALF=1893456000",
        "cookie_last_rotated_at": "2026-10-13 10:00:00",
        "cookie_last_seen_valid_at": "",
    }
    view = describe_cookie_lifespan(acc, ttl_days=7, warn_days=2,
                                    now=datetime(2026, 10, 14, 10, 0, 0))
    assert view["deadline"] == "2026-10-20 10:00:00"
    assert view["days_left"] == 6.0
    assert view["state"] == "ok"


def test_lifespan_expiring_within_warn_window():
    acc = {
        "cookie": "SUB=s; SCF=c; ALF=1893456000",
        "cookie_last_rotated_at": "2026-10-09 10:00:00",
        "cookie_last_seen_valid_at": "2026-10-10 10:00:00",
    }
    view = describe_cookie_lifespan(acc, ttl_days=7, warn_days=2,
                                    now=datetime(2026, 10, 15, 10, 0, 0))
    assert view["state"] == "expiring"
    assert view["reason"].startswith("预计")


def test_lifespan_without_any_anchor_is_unknown():
    view = describe_cookie_lifespan({"cookie": "SUB=s"}, ttl_days=7, warn_days=2)
    assert view["state"] == "unknown"
    assert view["deadline"] == ""


def test_lifespan_uses_non_retiring_ruler_anchored_at_issue():
    """保活每轮都会把"上次有效"刷新成 now —— 若只按它算，倒计时永远归零，
    于是再也报不出"即将失效"。凭证录入时间才是不会退休的那把尺子。"""
    acc = {
        "cookie": "SUB=s; SCF=c; ALF=1893456000",         # 名义上还有一年
        "cookie_issued_at": "2026-10-09 10:00:00",
        "cookie_last_seen_valid_at": "2026-10-15 09:50:00",  # 刚刚探活成功
        "cookie_last_rotated_at": "2026-10-15 09:50:00",
    }
    view = describe_cookie_lifespan(acc, ttl_days=7, warn_days=2,
                                    now=datetime(2026, 10, 15, 10, 0, 0))
    assert view["deadline"] == "2026-10-16 10:00:00"      # 以录入时间为基准
    assert view["state"] == "expiring"


def test_health_prefers_observed_evidence_but_not_stale_estimates():
    from app.scheduler import resolve_cookie_health
    # 微博刚确认有效，但已超过经验寿命：不能谎报"已过期"，也不能隐瞒成"正常"
    assert resolve_cookie_health("ok", "expired") == "expiring"
    # 确认失效优先于任何乐观估计
    assert resolve_cookie_health("invalid", "ok") == "invalid"
    assert resolve_cookie_health("", "expiring") == "expiring"
    assert resolve_cookie_health("network_error", "ok") == "network_error"
    assert resolve_cookie_health("", "") == "unknown"


# ---------- 失效/风控异常分类 ----------

def test_rate_limited_is_a_network_error():
    """风控必须是 NetworkError 子类：既不能被当成登录过期误杀，也要被既有
    except NetworkError 分支接住。"""
    assert issubclass(RateLimitedError, NetworkError)
    assert issubclass(NetworkError, RuntimeError)
    assert not issubclass(CookieExpiredError, NetworkError)
