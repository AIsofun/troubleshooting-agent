#!/usr/bin/env bash
# =============================================================
# scripts/up.sh — 运维助手 Agent 一键管理脚本 (Linux / macOS)
# Usage:
#   ./scripts/up.sh           # 启动核心服务
#   ./scripts/up.sh --obs     # 启动核心 + 可观测性栈
#   ./scripts/up.sh --build   # 构建后启动
#   ./scripts/up.sh --down    # 停止服务
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# Defaults
OBS=false
BUILD=false
PULL=false
DOWN=false
LOGS=false
STATUS=false

# Parse args
for arg in "$@"; do
    case $arg in
        --obs)    OBS=true ;;
        --build)  BUILD=true ;;
        --pull)   PULL=true ;;
        --down)   DOWN=true ;;
        --logs)   LOGS=true ;;
        --status) STATUS=true ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

# Build profile args
PROFILES="--profile core"
[[ "$OBS" == "true" ]] && PROFILES="$PROFILES --profile obs"

dc() { docker compose $PROFILES "$@"; }

# .env check
if [[ ! -f .env ]]; then
    echo "Warning: .env not found. Copying from .env.example..."
    cp .env.example .env
    echo "Please review .env and set POSTGRES_PASSWORD before production use."
fi

if [[ "$DOWN" == "true" ]]; then
    dc down --remove-orphans
    echo "Services stopped."
    exit 0
fi

if [[ "$STATUS" == "true" ]]; then
    docker compose ps
    exit 0
fi

if [[ "$LOGS" == "true" ]]; then
    docker compose logs -f agent-api
    exit 0
fi

[[ "$PULL" == "true" ]] && dc pull
[[ "$BUILD" == "true" ]] && docker compose build --no-cache agent-api

echo "Starting services..."
dc up -d --remove-orphans

# Wait for healthcheck
echo "Waiting for agent-api..."
for i in $(seq 1 20); do
    state=$(docker inspect --format '{{.State.Health.Status}}' agent-api 2>/dev/null || echo "starting")
    [[ "$state" == "healthy" ]] && break
    echo "  (${i}×3s) $state"
    sleep 3
done

echo ""
echo "✅ Agent API:   http://localhost:8000"
[[ "$OBS" == "true" ]] && echo "📊 Grafana:     http://localhost:3000"
[[ "$OBS" == "true" ]] && echo "🔍 Prometheus:  http://localhost:9090"
echo ""
echo "Stop:  ./scripts/up.sh --down"
echo "Logs:  ./scripts/up.sh --logs"
