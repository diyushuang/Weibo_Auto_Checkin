# 安全审计报告（v1.3.1）

> 审计对象：Weibo_Auto_Checkin（FastAPI + SQLite 微博超话签到管理面板）
> 审计方式：全量代码人工审读（`app/` 全部模块 + API 路由 + 部署链 + 前端渲染点）
> 本文档记录发现的问题、严重程度、修复方式与遗留建议。

---

## 一、高危（3 项，已全部修复）

### H-1 `/data` 静态挂载把整个数据目录暴露为可下载资源

- **位置**：`app/main.py`（原 `app.mount("/data", StaticFiles(directory=DATA_DIR))`）
- **问题**：`DATA_DIR` 是 SQLite 数据库所在目录。数据库文件（含 WAL/SHM）与头像、
  图片缓存同级，整体挂载意味着认证一旦失效（会话 token 泄漏、`auth_enabled` 被关闭、
  中间件疏漏），攻击者可直接 `GET /data/weibo_checkin.db` 下载整库 ——
  库内含微博账号 Cookie、代理账密、密码哈希与会话 token，等于全线失守。
- **修复**：移除根目录挂载，改为仅挂载 `data/avatars` 纯图片子目录。
  超话图片本就走带鉴权的 `/api/topics/img` 代理，前端对 `/data/` 的唯一引用
  即头像路径，收窄后功能不受影响。另有专项测试 `test_database_file_not_served`
  验证 `.db`/`.db-wal` 返回 404。

### H-2 代理凭据经异常文本泄漏进数据库与通知

- **位置**：`app/weibo_client.py`（`request_via_proxy`、`run_account_checkin`）、`app/scheduler.py`（`run_checkin` 异常分支）
- **问题**：requests 的代理异常消息形如
  `ProxyError(... socks5://user:pass@host:port ...)`，携带完整代理账密。
  原代码把 `str(exc)` 原文写进 `result["message"]` → `checkin_logs` /
  `accounts.last_message` → 面板轮询展示 → Telegram 报告，泄漏链完整成立。
- **修复**：新增 `app/security.py::safe_exc()` 统一脱敏
  （剥离 URL userinfo、Bot Token、`sk-` 形式 key、压平换行、限长 500），
  覆盖 `weibo_client`、`scheduler`、`notifier`、`topics` 中所有
  `f"...{exc}"` / `str(exc)` 入库、入日志、入通知的位置。

### H-3 Telegram Bot Token 经异常文本持久化

- **位置**：`app/notifier.py::send_telegram`
- **问题**：连接类异常消息含 `/bot<TOKEN>/sendMessage` 完整 URL，`str(exc)`
  被写入 `notifications` 表并回显面板；`log.exception` 亦完整打印。
  Token 泄漏后可被用于向该 bot 的全部会话发消息/读消息。
- **修复**：异常经 `safe_exc()` 脱敏后再入库；日志降为 `log.error` 且只输出
  脱敏文本。另修复 200 响应非 JSON 时 `resp.json()` 抛错导致错误归类失真的问题。

---

## 二、中危（6 项，已全部修复）

### M-1 CORS 全开 + 允许凭据

- **位置**：`app/main.py`
- **问题**：`allow_origins=["*"] + allow_credentials=True` 违反 CORS 规范；
  Starlette 对带凭据请求会反射具体 Origin，任意网站可携带受害者 Cookie
  跨源读取 API 响应，与旧版 `/api/accounts/export`（GET 回完整 Cookie）叠加危害放大。
- **修复**：移除 CORS 中间件（自托管同源面板无需跨域）。

### M-2 `checkin_topic` 跟随外部 scheme 外发账号 Cookie（SSRF）

- **位置**：`app/weibo_client.py::checkin_topic`
- **问题**：`url = scheme if scheme.startswith("http") else BASE + scheme`，
  `scheme` 来自微博接口响应（不可信输入），绝对 URL 可指向任意外部主机
  （含 `http://` 明文），请求携带账号 Cookie，且无任何 host 校验。
- **修复**：新增 `app/security.py::validate_outbound_url()`（仅 http/https、
  拒绝 localhost/环回/私有/保留/链路本地地址、DNS 解析结果逐 IP 校验防重绑定、
  可选 host 后缀白名单），签到目标限定 `weibo.cn / weibo.com / weibocdn.com`。
  同一守卫覆盖：
  - 图片代理 `/api/topics/img` 与 `_download_image`（拒绝私网目标）；
  - 扫码登录跳转 URL（限定微博域名）。

### M-3 登录限流可被伪造 `X-Forwarded-For` 绕过

- **位置**：`app/auth.py::client_ip`
- **问题**：默认信任 XFF 首段。面板直连暴露时，爆破者每次请求伪造不同 XFF
  即可绕过「5 次/300 秒」限流。
- **修复**：新增 `WCM_TRUST_PROXY` 环境变量（默认 `0`）。仅在显式开启时采信
  `X-Forwarded-For` / `X-Real-IP` / `X-Forwarded-Proto`，否则取直连地址。
  `is_secure_request`（Secure Cookie 判定）同步收紧。反向代理部署请设
  `WCM_TRUST_PROXY=1`（已写入 `.env.example` 与 compose 文件）。

### M-4 限流状态仅存进程内存

- **问题**：重启清零、多 worker 各自计数，锁定形同虚设。
- **修复**：限流状态落 SQLite（`login_locks` 表），重启不丢、进程间共享；
  读写经 `database` 层写锁串行化。

### M-5 SQLite 无 `busy_timeout`，并行签到并发写库报错

- **位置**：`app/database.py`
- **问题**：多代理分组并行签到时多线程同时写库，WAL 只保证多读单写，
  写冲突会直接抛 `database is locked`，导致签到记录丢失。
- **修复**：连接建立时 `PRAGMA busy_timeout=5000`，并新增进程内
  `_write_lock` 写锁串行化写事务（读不受影响）。

### M-6 `/api/accounts/export` GET 返回全部账号完整 Cookie

- **修复**：改为 `POST` + 请求体 `confirm: true` 显式确认（前端同步增加
  确认弹窗），GET 直接不可用；配合 M-1 消除跨源读取面。

---

## 三、低危（8 项，已全部修复）

| # | 问题 | 位置 | 修复 |
|---|------|------|------|
| L-1 | 会话 token 明文入库，拖库即可复用 | `database.py::create_session` | 只存 SHA-256 摘要，lookup 按摘要进行（升级后旧会话失效，需重新登录） |
| L-2 | 账号列表返回 `cookie_preview`（前 20 字符凭据片段） | `app/api/accounts.py::_public` | 移除，仅保留 `cookie_length` |
| L-3 | 公开路径白名单用 `startswith` 宽匹配（`/api/auth/login-xxx` 也放行） | `app/main.py` | 改为精确匹配集合 `PUBLIC_API_PATHS` |
| L-4 | 无安全响应头 | `app/main.py` | 新增中间件：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: same-origin`、基础 CSP（script/style 因前端内联事件保留 `unsafe-inline`，仍可阻断外部脚本注入与 framing） |
| L-5 | `/api/auth/init` 存在首次部署竞态（检查与写入非原子） | `app/api/auth.py` | 新增 `database.try_create_first_user()`：`BEGIN IMMEDIATE` 事务内原子判断+写入 |
| L-6 | 密码最短 6 位 | `app/api/auth.py`、`init.html`、`app.js` | 最短 8 位（服务端为准，前端提示同步） |
| L-7 | 归属地兜底接口走明文 HTTP（`http://ip-api.com`），可被出口篡改 | `app/proxy_geo.py` | 替换为 HTTPS 服务 `api.ip.sb/geoip`，全部服务仅 HTTPS |
| L-8 | `verify_cookie` 裸 `except` 把解析错误误判为「未登录」，账号被错误标记 `cookie_invalid` | `app/weibo_client.py` | 非 RequestException 异常统一按网络类失败上抛，不再误置 `cookie_invalid` |

---

## 四、性能与代码质量优化（已全部实施）

1. **Dockerfile**
   - 删除 `RUN echo "Build timestamp..."` 缓存杀手（其后所有层缓存每次构建失效）；
   - 移除 `gcc g++ build-essential`：全部依赖均有预编译 wheel，
     加 `PIP_ONLY_BINARY=:all:` 护栏 —— 将来引入无 wheel 依赖会立刻报错而非 QEMU 里编译；
   - 非 root 用户运行（uid 10001）。
2. **compose**：镜像内已带 HEALTHCHECK，删除两份 compose 的重复声明；
   `compose.prod.yml` 补 logging 限制；两份 compose 均加 `read_only: true` +
   `/tmp` tmpfs（应用只写 `/app/data`）；`install.sh` 生成的 compose 同步。
3. **start.sh**：去掉 `pip install ... || true`，依赖装失败立即报错退出。
4. **Session 复用**：`weibo_client.get_thread_session()` 线程本地 Session +
   HTTPAdapter 连接池，供超话刷新等公开接口复用 TLS 连接（复用前清空
   Cookie jar，避免跨账号串 Cookie）；签到主流程仍每账号独立 Session
   （Cookie 隔离是正确性要求，有意保留）。
5. **`fetch_topic_posts`**：端点专属 headers 改请求级参数（替代
   `session.headers.clear()/restore` 脆弱模式）；翻页加 `MAX_TOPIC_PAGES=3`
   总量上限（原先 `since_id` 分页无上限，存在请求放大）；错误消息脱敏。
6. **scheduler**：`topics_daily_push` cron 装配逻辑去重（`_install_topics_push_job`
   + `_normalize_cron`，`start_scheduler` 不再重建一次）；`auto_refresh_topics`
   设置读取提到每账号循环外；`get_current_run()` 返回浅拷贝、`_current_run`
   全局替换加锁；`next_run_info` 裸 `except: pass` 改为窄异常 + debug 日志；
   `__import__("requests")` 等隐藏依赖改为正常 import。
7. **anti_ban / CheckinOptions**：`int()` 设置解析损坏时回退默认值；
   修 logger 名拼写 `weibo.antibab → weibo.antiban`。
8. **proxy_geo**：3 个归属地服务并发探测取最快成功者（最坏耗时 3×12s → 12s，
   fatal 代理错误仍立即返回）；缓存加 512 条上限并按时间淘汰最旧条目。
9. **AI 总结 Q&A 模式**：补齐测试期望但缺失的 `question` 参数支持
   （帖子内容进 system 上下文、问题作 user 消息、响应带 `qa_mode` 字段）——
   这是仓库预存的「测试先行、实现缺失」缺陷。
10. **CI**：`build-docker.yml` 增加 `platforms: linux/amd64,linux/arm64`
    （工作流原本已装 QEMU/Buildx，仅漏传参数，导致只有 amd64 镜像）。

## 五、安全设计说明（有意保留的行为）

- **管理员自配置的 AI Base URL 与 SOCKS 代理不做私网限制**：这类输入属于
  运维自配置的可信面，本地 LLM 网关（如 `http://127.0.0.1:11434`）、内网代理
  是合法场景；仍强制 scheme 校验（http/https / socks5/socks5h）。
  SSRF 守卫针对的是**不可信来源的请求目标**（微博接口返回的 URL、页面参数）。
- **CSP 保留 `unsafe-inline`**：前端使用内联事件（头像 `onerror` 兜底等），
  完全收紧会破坏面板；当前策略已阻断外部脚本源。
- **`data/img_cache`、`data/topics` 不再经静态挂载暴露**：分别由带鉴权的
  `/api/topics/img` 代理与文件接口提供。

## 六、遗留建议（不影响发布，供后续迭代）

- 敏感凭据（微博 Cookie、代理账密）仍是明文落库；可考虑应用层加密
  （密钥放环境变量/密钥文件）。当前依赖文件系统权限 + 静态挂载收窄防护。
- 登录限流按 IP；如需抗分布式爆破可叠加按账号维度限流。
- 前端 CSP 若后续重构掉内联事件，可移除 `unsafe-inline`。
- `tests/frontend-render.test.js`（jsdom）建议纳入 CI。

---

# 安全审计报告 · 第二轮（v1.3.8，2026-10-25）

> 审计方式：Mimosa 密封深度扫描（scan-2026-10-25T08-06-51.057Z-a753fb16c556，
> seal sha256:6c686ed1…7116，10 项静态发现）+ 全路由授权矩阵 / 前端 XSS /
> 部署链人工复核。扫描器 10 项发现（database.py DDL f-string、hitokoto 常量
> URL、6 处防封节拍 random）经逐条复核均为上下文误报，证据见提交记录。
> 以下为人工复核确认的真实问题与修复（已全部修复，测试 235 项通过）。

## H-A 出站 SSRF 守卫不覆盖 HTTP 重定向（中低，已修复）

- **位置**：`app/security.py`（原 `validate_outbound_url` 只校验初始 URL）
- **问题**：requests 默认跟随重定向且不校验目标。微博白名单域上一个 30x
  （或被攻破的微博响应）可把图床探测 / 头像下载 / 签到请求（连带域匹配 Cookie）
  导引至 `169.254.169.254` 云元数据或内网地址，架空整套出站守卫。
- **修复**：新增 `security.guarded_request()`——关闭自动重定向，每个 30x 的
  Location 解析为绝对地址后重新过 `validate_outbound_url`（含 host 白名单与
  私网/保留段拦截），8 跳上限防循环。接入全部 5 个不可信 URL 出口：
  `checkin_topic`（经 `request_json(url_guard_hosts=…)`）、`/api/topics/img`
  图床下载、`_cache_weibo_avatar` 头像、扫码回调 crossDomain 列表、扫码
  登录跳转 URL。
- **残余（记录）**：校验时解析 DNS、请求时 requests 再次独立解析，存在理论
  重绑定窗口；逐跳"发送前一刻校验"已把窗口压到 check-then-use 最小形态，
  不做 IP 锁定（会破坏 TLS SNI 与代理链路）。代码常量 URL（m.weibo.cn API
  等）的重定向不在守卫范围（沿用第五节"URL 来源不在代码常量里才校验"的
  设计边界）。

## M-A `/api/health` 匿名信息泄露（低，已修复）

- **问题**：公开返回 `auth_enabled`（告知攻击者是否需登录）、Cookie 健康统计、
  版本号，等于替攻击者画部署画像。
- **修复**：匿名访问只回 `{"ok": true}`（Docker HEALTHCHECK 只看状态码，不受
  影响）；带有效会话返回全量。前端健康按钮在已登录页面上，功能不变。

## M-B 手机同步限流状态仅在进程内存（低，已修复）

- **问题**：`_rate_stats` 字典重启清零、多进程不共享；反代透传伪造 XFF 时
  条目无界增长（内存放大）。
- **修复**：限流状态落 SQLite（新表 `mobile_sync_rate`，与 `login_locks` 同套
  写锁口径），重启/多进程共享；表条目封顶 4096（`MOBILE_RATE_MAX_IPS`），
  超限按最旧淘汰。语义与原实现一致（10 失败/15 分钟 → 锁 15 分钟；
  成功 30 次/小时只拒绝不锁）。

## L-A PBKDF2 迭代次数偏低（已修复，格式版本化）

- **问题**：存量 120,000 迭代低于 OWASP 2023 建议的 600,000。
- **修复**：新哈希格式 `pbkdf2$iterations$salt$hash` 自描述迭代数（600k）；
  校验兼容旧格式 `salt$hash`（按 120k），存量管理员无需重置密码，
  下次改密自然升级。

## L-B 依赖声明无上界（已修复）

- `requirements.txt` 全部依赖补主版本上界（`<1`/`<3` 等），配合既有
  `PIP_ONLY_BINARY=:all:` 护栏，降低未来大版本破坏与供应链突变面。

## L-C 测试脚本 execSync 字符串拼接（已修复）

- `tests/frontend-render.test.js` 的 jsdom 自动安装改 `execFileSync` 参数
  数组形式（路径来自 mkdtemp，本无注入，消除模式告警）。

## 本轮确认无误的面（未改动）

- 全部 37 条 API 路由 `require_admin`；终端 WS 自行鉴权、无匿名降级、
  单会话 + 30 分钟空闲断开；令牌生成均用 `secrets`；`update_proxy` /
  `set_cookie_state` 动态 SET 子句键名取自硬编码白名单；头像/图片缓存
  文件名消毒（正则白名单 / URL 哈希）；前端 `esc()` 纪律完整（含
  `linkifyText` 预转义、AI 输出受限 markdown）；git 无敏感文件入库。
