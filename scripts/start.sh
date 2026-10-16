#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# RAG-LAB 启动脚本
#   ./scripts/start.sh          启动全部服务
#   ./scripts/start.sh --build  重新构建镜像后启动
# ---------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")/.."

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; DIM='\033[2m'; NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  RAG-LAB 启动${NC}"
echo -e "${GREEN}========================================${NC}"

# 1) 检查 .env
if [ ! -f .env ]; then
    echo -e "${YELLOW}未找到 .env，从 .env.example 复制...${NC}"
    cp .env.example .env
    echo -e "${RED}请编辑 .env 填写 API Key 后重新运行！${NC}"
    exit 1
fi

# 2) 检查必填项
missing=0
for key in DEEPSEEK_API_KEY; do
    val=$(grep -E "^${key}=" .env | head -1 | cut -d'=' -f2- || true)
    if [ -z "$val" ]; then
        echo -e "${RED}警告: .env 中 ${key} 为空${NC}"
        missing=1
    fi
done
if [ "$missing" = "1" ]; then
    echo -e "${YELLOW}提示: LLM key 为空会导致问答失败，请补全后重启。${NC}"
fi

# 3) 检查 docker
if ! command -v docker >/dev/null 2>&1; then
    echo -e "${RED}未检测到 docker，请先安装。${NC}"
    exit 1
fi

# 4) 创建数据目录
mkdir -p data/uploads data/logs data/hf_cache

# 5) 启动
if [ "${1:-}" = "--build" ]; then
    echo -e "${GREEN}构建并启动...${NC}"
    docker compose up -d --build
else
    echo -e "${GREEN}启动...${NC}"
    docker compose up -d
fi

# 6) 等待就绪
echo -e "${YELLOW}等待服务就绪...${NC}"
for i in $(seq 1 40); do
    if curl -fsS http://localhost:8000/api/health >/dev/null 2>&1; then
        echo -e "${GREEN}✓ 服务已就绪${NC}"
        break
    fi
    sleep 3
    if [ "$i" = "40" ]; then
        echo -e "${YELLOW}服务仍在启动中（BGE 模型首次下载可能较慢）${NC}"
        echo -e "${YELLOW}查看日志: docker compose logs -f backend${NC}"
    fi
done

echo ""
docker compose ps
echo ""
echo -e "${GREEN}API 文档:  http://localhost:8000/docs${NC}"
echo -e "${GREEN}健康检查:  http://localhost:8000/api/health${NC}"

# 从 .env 读实际的管理员账号（不要写死，改过密码就误导）
ADMIN_USER=$(grep -E '^ADMIN_USERNAME=' .env | head -1 | cut -d'=' -f2-)
ADMIN_PWD=$(grep -E '^ADMIN_PASSWORD=' .env | head -1 | cut -d'=' -f2-)
if [ -n "$ADMIN_USER" ]; then
    echo -e "${GREEN}默认管理员: ${ADMIN_USER} / ${ADMIN_PWD}${NC}  ${DIM}(来自 .env，仅首次启动创建时生效)${NC}"
    echo -e "${DIM}  若已改过密码，此提示不再有效${NC}"
fi
