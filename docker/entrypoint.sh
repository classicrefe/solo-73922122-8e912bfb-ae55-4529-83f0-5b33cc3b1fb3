#!/bin/sh
# 容器入口：建库 + 幂等写入 v1 种子名录，然后启动服务
set -e

echo "[entrypoint] 初始化数据库并写入种子名录（幂等）..."
python -m scripts.seed

echo "[entrypoint] 启动 API 服务: $*"
exec "$@"
