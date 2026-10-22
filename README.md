# 微博超话签到管理面板

带 **Web 管理面板**的微博超话自动签到系统。FastAPI + SQLite，支持扫码登录、自动定时签到、Telegram 通知、SOCKS5 代理池与防封，可 Docker 一键部署。

## ✨ 功能

- 🖥️ **管理面板**：仪表盘 / 账号 / 日志 / 设置，白天黑夜主题
- 🔐 **登录保护**：首次部署可视化初始化设置管理员；未登录拦截；改密 / 退出
- 📱 **微博扫码添加账号**：弹二维码 → 微博 App 扫码确认 → 自动获取 Cookie
- 🍪 **Cookie 生成器**：粘贴 Cookie 自动解析，一键导入账号
- 🕓 **Cookie 智能保活**：自适应间隔 + 随机抖动 + 临期自动加密 + 真实业务流量掺入 + 夜间静默，尽量把登录态续得更久
- ⏰ **自动定时签到**：Cron 可配（支持 5 段 / 6 段青龙格式）
- ✅ **已签自动跳过**：不重复签到
- 🛡️ **防封策略**：凌晨窗口随机等待、SOCKS5 代理池、失败回退、三遍重试
- 🌍 **智能代理调度**：独立「代理」页管理 Socks5 节点（手动输入或粘贴链接自动识别归属地）；每个账号可指定 socks；**不同 socks 的账号并行签到**，同 socks 依次签到
- 📲 **Telegram 推送**：签到完成自动推送汇总
- 📜 **分组日志**：按日期分区，单次执行的所有账号归并一组
- 🗄️ **SQLite**：账号 / 日志 / 任务 / 用户 / 通知全部持久化
- 🧰 **极简 CLI 模式**：`mini_client.py` 与面板同库的命令行入口（签到/保活/超话），cron/青龙友好
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
