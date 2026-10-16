# ---------------------------------------------------------------------------
#  RAG-LAB 应用镜像
# ---------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/data/hf_cache \
    SENTENCE_TRANSFORMERS_HOME=/app/data/hf_cache \
    HF_ENDPOINT=https://hf-mirror.com \
    HF_HUB_ENABLE_HF_TRANSFER=0

WORKDIR /app

# 使用国内镜像加速 apt 与 pip（deb.debian.org 在本网络下极慢）
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources; \
    fi \
    && if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list; \
    fi

# 系统依赖：
#   build-essential/gcc     —— 部分包的编译
#   libpq-dev               —— psycopg2
#   poppler-utils           —— pdfplumber 的 PDF 解析后端 + pdftoppm 渲染扫描件
#   libgl1 / libglib2.0-0   —— PyMuPDF / OpenCV 运行时依赖
#   libgomp1                —— onnxruntime 多线程运行时
#   antiword                —— 老式 .doc 文档解析
#   fonts-wqy-zenhei        —— 中文字体，PDF 渲染与 OCR 识别需要
#   curl                    —— 健康检查
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libpq-dev \
        poppler-utils \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        antiword \
        fonts-wqy-zenhei \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用 Docker 层缓存）
# pip 走清华源，避免直连 pypi 超时
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
# torch 单独从 PyTorch 官方 CPU 源装：体积仅约 190MB，
# 远小于 PyPI 上带 CUDA 的完整版（约 900MB），本项目只用 CPU 推理。
ARG TORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install --upgrade pip -i ${PIP_INDEX} \
    && pip install torch==2.5.1 --index-url ${TORCH_CPU_INDEX} \
    && pip install -r requirements.txt -i ${PIP_INDEX}

COPY . .

# 以非 root 用户运行：避免容器写入的文件在宿主机上归属 root，
# 否则用户在宿主机删除/备份 data/uploads 下的文件需要 sudo。
# UID/GID 与宿主机用户保持一致（1000），确保挂载目录读写权限对齐。
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} appuser 2>/dev/null || true \
    && useradd -u ${APP_UID} -g ${APP_GID} -m -s /bin/bash appuser 2>/dev/null || true \
    && mkdir -p /app/data/uploads /app/data/logs /app/data/hf_cache \
    && chown -R ${APP_UID}:${APP_GID} /app

USER ${APP_UID}:${APP_GID}

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
