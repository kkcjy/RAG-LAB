# ---------------------------------------------------------------------------
#  RAG-lab 应用镜像
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

# 系统依赖：
#   build-essential/gcc     —— 部分包的编译
#   libpq-dev               —— psycopg2
#   poppler-utils           —— pdfplumber 的 PDF 解析后端
#   libgl1 / libglib2.0-0   —— PyMuPDF 与图像处理运行时依赖
#   antiword                —— 老式 .doc 文档解析
#   fonts-wqy-zenhei        —— 中文字体，PDF 渲染与文本提取需要
#   curl                    —— 健康检查
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libpq-dev \
        poppler-utils \
        libgl1 \
        libglib2.0-0 \
        antiword \
        fonts-wqy-zenhei \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/data/uploads /app/data/logs /app/data/hf_cache

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
