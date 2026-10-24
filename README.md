# 微博超话签到管理面板

带 **Web 管理面板**的微博超话自动签到系统。FastAPI + SQLite，支持扫码登录、自动定时签到、Telegram 通知、SOCKS5 代理池与防封，可 Docker 一键部署。

## ✨ 功能

- 🖥️ **管理面板**：仪表盘 / 账号 / 日志 / 设置，白天黑夜主题
- 🔐 **登录保护**：首次部署可视化初始化设置管理员；未登录拦截；改密 / 退出
- 📱 **微博扫码添加账号**：弹二维码 → 微博 App 扫码确认 → 自动获取 Cookie
- 🍪 **Cookie 生成器**：粘贴 Cookie 自动解析，一键导入账号
- 🕓 **Cookie 智能保活**：自适应间隔 + 随机抖动 + 临期自动加密 + 真实业务流量掺入 + 夜间静默，尽量把登录态续得更久
- 🔄 **Cookie 临期扫码续期**：保活换发无效时，面板横幅 / 账号行「续期」按钮 / TG 预警（可附带面板链接）引导重新扫码；新 Cookie 自动覆盖同微博号的原账号，代理、备注、启用状态全部保留，无需删除重建
- ⏰ **自动定时签到**：Cron 可配（支持 5 段 / 6 段青龙格式）
- ✅ **已签自动跳过**：不重复签到
- 🛡️ **防封策略**：凌晨窗口随机等待、SOCKS5 代理池、失败回退、三遍重试
- 🌍 **智能代理调度**：独立「代理」页管理 Socks5 节点（手动输入或粘贴链接自动识别归属地）；每个账号可指定 socks；**不同 socks 的账号并行签到**，同 socks 依次签到
- 📲 **Telegram 推送**：签到完成自动推送汇总
- 📜 **分组日志**：按日期分区，单次执行的所有账号归并一组
- 🗄️ **SQLite**：账号 / 日志 / 任务 / 用户 / 通知全部持久化
- 🧰 **极简 CLI 模式**：`mini_client.py` 与面板同库的命令行入口（签到/保活/超话），cron/青龙友好，Docker 镜像内置
- 🖥️ **客户端页（CLI 可视化）**：mini_client 命令全部按钮化，点击即执行、输出实时渲染在页面；折叠区保留交互式终端（xterm.js + WebSocket），默认关闭、单会话、空闲自动断开
- 🐳 **Docker 一键部署**

## 🚀 快速部署

方式一：**App 镜像一键部署（推荐，无需源码）**

```bash
# 下载脚本后运行
curl -o install.sh https://raw.githubusercontent.com/diyushuang/Weibo_Auto_Checkin/main/install.sh
chmod +x install.sh
bash install.sh            # 默认端口 8000
bash install.sh 8080       # 指定端口
```

脚本会自动：安装 Docker → 拉取镜像 → 启动容器 → 等待健康。完成后访问 `http://<服务器IP>:<端口>`，**首次进入初始化页设置管理员账号密码**。


## 🔄 更新到最新版

数据持久化在 `data/` 卷里，更新不会丢账号/日志/配置：

```bash
bash install.sh update
```

## 🧰 日常管理

```bash
bash install.sh status     # 查看容器状态与版本（无需 root）
bash install.sh logs       # 跟随查看日志
bash install.sh start      # 启动
bash install.sh stop       # 停止
bash install.sh restart    # 重启
```

## 🐳 手动 Docker（不用脚本）

```bash
# 安装
docker run -d --name weibo-checkin --restart unless-stopped \
  -p 8000:8000 -v /opt/weibo-checkin/data:/app/data \
  kaoqy666/weibo-checkin:latest

# 更新
docker pull kaoqy666/weibo-checkin:latest
docker rm -f weibo-checkin
docker run -d --name weibo-checkin --restart unless-stopped \
  -p 8000:8000 -v /opt/weibo-checkin/data:/app/data \
  kaoqy666/weibo-checkin:latest

# 确认
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/health  # 期望 200
```

> GitHub Actions 会在推送 tag 时自动构建 Docker Hub 多架构镜像（**linux/amd64 + linux/arm64**，x86 服务器、树莓派、ARM 云主机均可直接 `docker pull`），版本号与 Git tag 一致（如 `v1.3.3`），`latest` 永远指向最新。

## 🛠️ 开发者：本地构建 / 发布

本地源码运行（开发调试）：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py          # http://localhost:8000
.venv/bin/python run.py checkin  # 命令行跑一次签到
```

发布：推送 tag 触发 GitHub Actions 自动构建多架构 Docker Hub 镜像（见 `.github/workflows/build-docker.yml`）。

```bash
git tag v1.3.3
git push origin v1.3.3   # 自动触发 Docker Hub 构建（amd64 + arm64）
```

本地 Docker 构建：

```bash
docker build -t weibo-checkin:latest .
docker compose up -d --build

# 本地验证多架构构建（arm64 走 QEMU，较慢）
docker buildx build --platform linux/amd64,linux/arm64 -t weibo-checkin:multi .
```

## 🧰 极简 CLI（无面板模式）

不想开面板？仓库根目录的 `mini_client.py` 与面板**共享同一个 SQLite 数据库**
和全部业务逻辑（签到编排、保活引擎、超话抓取、扫码导入的账号），只多提供
一个命令行入口，额外零依赖：

```bash
cp mini_accounts.example.txt mini_accounts.txt   # 填入你的 cookie
python mini_client.py import                     # 导入账号文件进数据库
python mini_client.py checkin                    # 全部启用账号签到一轮
python mini_client.py keepalive                  # 到期账号保活（--force 立即全量）
python mini_client.py topics refresh             # 抓取关注超话并更新缓存
python mini_client.py topics list                # 查看超话缓存
python mini_client.py rotate-probe               # 实验性：探测 PC 端点能否换发 Cookie
python mini_client.py checkin --json             # 机器可读输出
```

- 账号文件支持 `cookie` / `名字|cookie` / `名字|socks5代理|cookie` 三种格式；
  重复导入按 SUB/名称去重更新，不会产生重复账号
- 换发到的新 cookie 直接写回数据库，面板健康徽章、日志、超话页全部可见
- 面板「设置」里的保活间隔、夜间静默、防封等配置对 CLI 同样生效
- 旧版 `mini_state.json` 会在首次运行时自动迁移进数据库并改名 `.migrated`
- 面板与 CLI 可共用同一数据库；Cookie 并发写入有 CAS 保护

cron 示例：

```cron
0 7,19 * * *  cd /path/to/repo && python mini_client.py keepalive --force
30 7 * * *    cd /path/to/repo && python mini_client.py checkin
```

退出码：`0` 成功/部分成功/网络抖动；`1` 有账号签到失败或登录失效；
`2` 配置错误。适合接 cron / 青龙 / systemd timer 的告警链路。

### 🐳 容器内使用 CLI

`mini_client.py` 已打入 Docker 镜像（工作目录 `/app`），容器里无需挂载源码即可使用：

```bash
docker exec -it weibo-checkin python mini_client.py checkin
docker exec -it weibo-checkin python mini_client.py keepalive --force
docker exec -it weibo-checkin python mini_client.py topics list
```

## 🖥️ 客户端页（CLI 可视化 · v1.3.5）

不想敲命令？侧边栏**「客户端」页**把 mini_client 的常用命令做成了按钮——
「立即签到 / 强制保活 / 到期保活 / 超话列表 / 刷新超话 / 命令帮助」点击即执行，
运行输出实时渲染在页面里（命令行高亮、进度条取最终态），与面板运行在
**同一容器/同一数据库**；「超话速览」卡片记录最近浏览的超话、可一键跳回超话
浏览页，超话页也有「⌨ 一键刷新」按钮联动。页面底部的折叠区提供完整的
交互式终端（xterm.js + WebSocket），可手动输入任意命令。

安全设计（命令在管理员权限的 Shell 中执行，请谨慎开启）：

- **默认关闭**：需在「设置」里显式开启
- 复用面板会话认证（未登录无法连接 WebSocket）
- **全局单会话**：同一时刻只允许一个终端连接
- **空闲 30 分钟自动断开**并回收进程；断开时整个进程组被终止
- 连接与退出均写入服务日志（用户、时长）

⚠️ 公网部署时请务必保持强口令 + HTTPS（反代需支持 WebSocket 透传），
不要在不受信任的网络环境下开启该功能。

## 🔄 Cookie 临期扫码续期（v1.3.6）

微博服务端 session 实测约 7 天，保活的本质是「带旧凭据访问 → 收下响应里
换发的新凭据」。但换发不保证一直成功——临期却续不动时，**扫码重登是唯一
可靠手段**（微博没有凭旧 Cookie 静默换新的公开接口，账密自动重登又需要
存密码且风控风险高）。v1.3.6 把这条人工链路做成了闭环：

1. **检测**：账号失效 / 已到预计失效时间 / 临期且超过 `cookie_relogin_stale_hours`
   （默认 48 小时）仍无成功换发时，判定为「需要扫码续期」；
2. **引导**：账号管理页顶部出现横幅、账号行出现「🔄 续期」按钮、TG 预警
   （`panel_url` 设置了面板地址时会附带链接，手机上点开即可）；
3. **接管**：点按钮弹出现有扫码弹窗，扫码确认后新 Cookie **自动覆盖同微博号
   的原账号**（按 weibo_uid / SUB 匹配），代理、备注、启用状态保留，生命周期
   基准自动重置，无需删除重建。

### 实验性：PC 端点静默换发

设置里默认关闭的「实验性 PC 端点换发」会在临期保活轮追加请求 PC 端候选端点
（`keepalive_passport_urls`，仅允许微博域名，强制出站白名单校验），尝试让
微博在响应里换发新凭据。微博未承诺这些端点的有效性，请先用 CLI 实测：

```bash
python mini_client.py rotate-probe            # 只探测临期账号
python mini_client.py rotate-probe --all      # 探测全部启用账号
python mini_client.py rotate-probe --urls "https://weibo.com/,https://passport.weibo.com/"
```

probe 会逐端点打印换发了哪些 cookie 字段并把新凭据回写数据库（CAS 保护），
结尾给出「哪些端点有效、建议保留在设置里」的汇总。实测确认有效后再打开
设置开关。

## 📲 手机 App：真机签到 + 登录态根（v1.3.7）

把登录态的「根」放到手机上：App 内用微博官方页面登录一次（账密不经本应用），
之后：

- **打开 App 即自动签到**：真机 + 家宽/移动网络直接请求 m.weibo.cn，逐账号
  列超话 → 对未签的超话随机间隔 3-8 秒逐个签到（已签自动跳过，与面板互相幂等）；
- **📷 原图抓取（App v0.3.0）**：Material 3 界面（底部导航：首页 / 图片 / 设置，
  Android 12+ 自动跟随系统取色）。添加博主（UID/主页链接/个性域名）并选择相册目录后，
  一键增量抓取其全部原创微博的原图（`/large/` 原始分辨率），**Live Photo 在端内
  转 GIF**（自研 GIF89a 编码器：中值切分 256 色调色板 + LZW + Bayer 抖动，
  12fps/长边 640，与 ffmpeg 方案同口径，转换失败自动回退保存原始视频），
  按日期命名平铺进所选目录；同名跳过、断点续跑，可开启「签到后自动抓取」；
- **单条抓取（App v0.3.1）**：粘贴微博链接直接抓该条；或输入博主 UID 列出其最近
  微博（时间 / 图数 / 置顶转发标记 / 正文摘要）点选抓取；JVM 单元测试覆盖
  GIF 编码器（ImageIO 解码回验）与 URL 白名单等纯函数，随 CI 构建运行；
- **后台每 6 小时保活轮换**：WorkManager 静默探活并收下微博换发的新 Cookie，
  跨重启自动持久化（无需开机自启权限）；
- **同步面板**：配置面板地址 + 同步令牌后，App 定期把最新 Cookie 推送到面板
  `POST /api/mobile/sync`，自动覆盖同微博号账号并重置生命周期基准——手机在，
  面板的 Cookie 就永远新鲜。

面板侧配置：**设置 → 手机保活** → 打开开关 → 「生成 / 重新生成」令牌并复制，
填进 App 的「面板设置」即可。令牌权限极小（只能推送 Cookie），错误尝试按 IP
限流；探活失败的 Cookie 不会覆盖面板上仍健康的账号。

App 工程在 `android-app/`（Kotlin，仅 `INTERNET` 权限），APK 由 GitHub Actions
自动构建（`.github/workflows/build-apk.yml`，Actions 页面下载 `wcm-app-debug`
产物）；也可以用 Android Studio 直接打开构建。

## 🎯 使用

1. 初始化/登录面板
2. 「设置 → 网络/代理」填 SOCKS5 节点（每行一个）→ 点「识别归属地」查看每个节点的国家/地区
3. 添加账号时可为每个账号「指定 socks 节点」（下拉显示归属地）
   - **不同 socks 的账号 → 并行签到**
   - **同 socks / 未指定的账号 → 依次签到**
4. 「设置」配 TG 通知、定时、防封
5. 点「立即签到」或等定时任务自动执行

## 🔒 安全

- 登录保护默认开启；密码 PBKDF2 哈希存储；会话 token 仅存摘要；登录限流持久化（5 次/5 分钟锁定 5 分钟）
- 数据目录仅暴露头像子目录，数据库文件不可经 HTTP 下载
- 出站请求有 SSRF 守卫：微博返回的链接限定微博域名，拒绝本地/私有/保留地址
- 代理凭据、Bot Token 不出现在日志、数据库错误消息与 TG 通知中
- Cookie 存本地 SQLite，勿公开 `data/` 目录
- 建议反向代理 + HTTPS 访问；反代部署请设 `WCM_TRUST_PROXY=1`（否则应用不采信 `X-Forwarded-*`，见 `.env.example`）
- 完整审计报告见 [docs/SECURITY_AUDIT.md](docs/SECURITY_AUDIT.md)（v1.3.1 全量安全加固）

## 🧪 测试

```bash
.venv/bin/python -m pytest tests/ -v   # 191 个测试
node tests/frontend-render.test.js      # 前端渲染测试（自动装 jsdom）
```

## 📂 结构

```
weibo-checkin-manager/
├── run.py            # 本地启动
├── mini_client.py    # 极简 CLI（与面板同库：签到/保活/超话/导入）
├── mini_accounts.example.txt  # CLI 账号文件模板（复制为 mini_accounts.txt）
├── install.sh        # 用户一键安装/更新/管理（推荐）
├── compose.prod.yml  # 生产 compose（仅拉镜像运行）
├── Dockerfile        # 多阶段构建（amd64/arm64，非 root 运行）
├── app/              # 后端 + 前端
│   ├── main.py       # FastAPI 入口 + 认证中间件 + 安全响应头
│   ├── security.py   # 凭据脱敏 + SSRF 出站守卫
│   ├── weibo_client.py
│   ├── cookie_lifecycle.py  # Cookie 健康度/自适应保活/换发回写（唯一实现）
│   ├── accounts_service.py  # 账号 upsert 与行解析（唯一实现）
│   ├── topics_service.py    # 超话列表抓取与缓存（唯一实现）
│   ├── proxy_geo.py  # SOCKS5 代理归属地识别（HTTPS 服务并发探测）
│   ├── scheduler.py  # 定时 + 防封 + 分组并行调度
│   ├── notifier.py   # TG 推送
│   ├── api/          # accounts / proxies / tasks / auth / topics
│   └── static/       # 前端页面（含代理管理页）
├── docs/SECURITY_AUDIT.md  # 安全审计报告
└── tests/
```

## 🙏 致谢

本项目基于原作者 [kaoqy/Weibo_Auto_Checkin](https://github.com/kaoqy/Weibo_Auto_Checkin) fork 并持续改进，感谢原作者的开源贡献。原项目的 MIT 授权允许自由使用、修改与再分发，本项目遵循相同协议继续开源。

本 fork 的主要增强：

| 版本 | 主要内容 |
|---|---|
| [v1.3.2](https://github.com/diyushuang/Weibo_Auto_Checkin/releases/tag/v1.3.2) | 安全加固（登录限流、PBKDF2 口令哈希、SSRF 守卫与响应脱敏）、Cookie 健康度体系（到期预警 / 保活续期 / 轮换记录）、主题美化 |
| [v1.3.3](https://github.com/diyushuang/Weibo_Auto_Checkin/releases/tag/v1.3.3) | WebUI 设计令牌化重构（统一间距 / 字阶 / 圆角体系）、设置页与仪表盘布局重组、组件精修与响应式优化 |
| [v1.3.4](https://github.com/diyushuang/Weibo_Auto_Checkin/releases/tag/v1.3.4) | 业务逻辑分层（cookie_lifecycle / accounts_service / topics_service 三处唯一实现，API 与 CLI 同源）、Cookie 保活增强（临期加密、随机抖动、业务端点掺入、夜间静默、换发来源记录）、新增 mini_client.py 极简 CLI |

## 📄 License

本项目采用 [MIT License](LICENSE) 开源。版权声明同时保留原项目作者（kaoqy）与本仓库维护者（diyushuang）的署名，fork 后的修改部分同样以 MIT 协议发布。
