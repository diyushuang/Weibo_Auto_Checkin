# syntax=docker/dockerfile:1.4
# ============================================================
# 微博超话签到管理面板 · 多阶段构建
#   阶段1 build: 创建 venv 并安装依赖
#   阶段2 runtime: 精简运行镜像（仅保留 venv + 代码）
# 支持架构：linux/amd64 + linux/arm64（所有依赖均有 aarch64 wheel）
# ============================================================

# ---------- 阶段1：构建依赖 ----------
FROM python:3.11-slim AS build

WORKDIR /build

# 依赖全部有预编译 wheel（pydantic-core/uvloop/httptools 等），
# PIP_ONLY_BINARY=:all: 作为护栏：将来引入无 wheel 的依赖会立刻报错，
# 而不是在 QEMU 交叉构建里用 gcc 慢慢编译。
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && PIP_ONLY_BINARY=:all: /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---------- 阶段2：运行 ----------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

# 仅安装运行所需的时区和 CA 证书；扫码登录使用 requests，不需要浏览器运行库。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && ln -fs /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

# 复制精简虚拟环境
COPY --from=build --link /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 移除运行时用不到的 pip/setuptools + 清 pyc 缓存，进一步瘦身（省 ~37MB）
RUN /opt/venv/bin/pip uninstall -y pip setuptools 2>/dev/null || true \
    && find /opt/venv -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# 扫码登录使用纯 requests 实现，无需安装浏览器或浏览器运行库。
WORKDIR /app

# 复制应用代码
COPY run.py .
COPY start.sh .
COPY app/ ./app/

# 数据目录（数据库持久化挂载点），归属非 root 运行用户
RUN mkdir -p /app/data \
    && useradd --create-home --uid 10001 wcm \
    && chown -R wcm:wcm /app
USER wcm

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys,os; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ['APP_PORT']+'/api/health', timeout=3).status==200 else 1)"

EXPOSE 8000

# 默认启动 Web 面板；可用 CMD 覆盖为 "checkin" 跑一次性签到
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8000"]
