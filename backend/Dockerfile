# 后端镜像 —— Python 3.9 + FastAPI
FROM python:3.9-slim

# 系统依赖：matplotlib 运行库 + 中文字体（图表中文显示）
# 国内服务器构建时使用阿里云 Debian 镜像，兼容不同 Debian 基础镜像的源文件布局。
RUN (sed -i -e 's|deb.debian.org|mirrors.aliyun.com|g' \
        -e 's|security.debian.org/debian-security|mirrors.aliyun.com/debian-security|g' \
        /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources 2>/dev/null || true) \
    # 该基础镜像固定在 Bullseye；构建时仅忽略已签名旧仓库清单的过期时间，仍保留签名校验。
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
        libgomp1 \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，利用构建缓存
COPY requirements.txt .
# 国内服务器构建时使用清华 PyPI 镜像，避免 Docker Hub/PyPI 直连不稳定。
ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir --index-url "$PYPI_INDEX_URL" -r requirements.txt

COPY app ./app

# 非 root 运行 + 数据目录（挂载卷）
RUN useradd -m appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

CMD ["python", "app/main.py"]
