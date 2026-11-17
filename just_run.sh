#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

PY=.venv/bin/python

if [ ! -f data/cards.json ]; then
  echo "缺少 data/cards.json。这个仓库不提供个人卡片数据，请先按 README 的格式准备。"
  exit 1
fi

# 前端是 React + Vite。依赖缺失时先装，再把构建产物写到 web/dist，
# 服务端只负责静态文件和 API。
if [ ! -d frontend/node_modules ]; then
  (cd frontend && npm install --no-audit --no-fund)
fi
(cd frontend && npm run build >/dev/null)

# 服务端只用 Python 标准库，默认不装依赖，启动最快。
# 要跑 tests/ 下的验收或 scripts/enrich_cards.py 时，用 INSTALL_DEV=1 ./just_run.sh
if [ "${INSTALL_DEV:-0}" = "1" ]; then
  "$PY" -m pip install -q -r requirements.txt
fi

BASE_PORT=8899
PORT=""

# 先看本项目是不是已经在跑。同一个项目重复启动时只打开页面并退出，
# 不再从 8900 起第二个实例，避免两个进程同时写 cards.json 与进度库。
for p in $(seq $BASE_PORT $((BASE_PORT + 19))); do
  if ! nc -z -G 1 -w 1 127.0.0.1 $p 2>/dev/null; then
    continue
  fi
  PID=$(lsof -ti tcp:$p -sTCP:LISTEN 2>/dev/null | head -1)
  if [ -z "$PID" ]; then
    continue
  fi
  CWD=$(lsof -p "$PID" -a -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
  if [ "$CWD" = "$PWD" ]; then
    printf '\033]0;知识点问答卡:%s\007' "$p"
    echo "知识点问答卡已在运行 http://127.0.0.1:$p"
    open "http://127.0.0.1:$p"
    exit 0
  fi
done

# 端口被别的项目占用就往后找，最多试 20 个。绝不 kill 别人的进程
for p in $(seq $BASE_PORT $((BASE_PORT + 19))); do
  if ! nc -z -G 1 -w 1 127.0.0.1 $p 2>/dev/null; then
    PORT=$p
    break
  fi
done

if [ -z "$PORT" ]; then
  echo "端口 $BASE_PORT 到 $((BASE_PORT + 19)) 都被占用了，先释放其中一个"
  exit 1
fi

printf '\033]0;知识点问答卡:%s\007' "$PORT"

# 只负责等服务就绪后打开页面，探测成功或超时就退出，不承载任何业务
(
  for i in $(seq 1 60); do
    if curl -s -o /dev/null "http://127.0.0.1:$PORT/api/progress"; then
      open "http://127.0.0.1:$PORT"
      exit 0
    fi
    sleep 0.25
  done
  echo "页面没有自动打开，请手动访问 http://127.0.0.1:$PORT"
) &

export FLASH_PORT=$PORT
echo "服务地址 http://127.0.0.1:$PORT"


exec "$PY" server.py
