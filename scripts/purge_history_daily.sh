#!/bin/bash
# 每日清理 llq Gemini 账号的历史会话（自愈链路 → 循环 UI 删除 → 日志）。
# 链路: 宿主9229 → llq-cdp-bridge容器 → chrome-seat-1内socat(9223) → CDP Chrome(9222)。
NODE=/root/.nvm/versions/node/v24.19.0/bin/node
BASE=/www/wwwroot/gemini-web2api
LOG=$BASE/logs/purge_history.log
MAX_ROUNDS=80

ts() { date '+%F %T'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

# --- 自愈 1: CDP Chrome（容器内 9222）---
if ! docker exec chrome-seat-1 curl -s -m 4 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
  log "9222 不通，重启容器内 CDP Chrome"
  docker exec chrome-seat-1 bash -c '
    mkdir -p /root/chrome-cdp-profile
    export DISPLAY=:1
    setsid /opt/google/chrome/chrome --password-store=basic --no-sandbox --no-first-run       --disable-search-engine-choice-screen --start-maximized       --user-data-dir=/root/chrome-cdp-profile --remote-debugging-port=9222       --remote-allow-origins=* >>/root/chrome-cdp.log 2>&1 < /dev/null &
  ' 2>/dev/null
  sleep 6
fi

# --- 自愈 2: socat 9223（容器内 loopback → 0.0.0.0）---
if ! docker exec chrome-seat-1 curl -s -m 4 http://127.0.0.1:9223/json/version >/dev/null 2>&1; then
  log "9223 不通，补启 socat"
  docker exec -d chrome-seat-1 socat TCP-LISTEN:9223,bind=0.0.0.0,fork,reuseaddr TCP:127.0.0.1:9222
  sleep 2
fi

# --- 自愈 3: 桥容器（宿主 9229）---
if ! curl -s -m 5 http://127.0.0.1:9229/json/version >/dev/null 2>&1; then
  log "9229 不通，重建 llq-cdp-bridge"
  docker rm -f llq-cdp-bridge >/dev/null 2>&1
  docker run -d --name llq-cdp-bridge --restart unless-stopped \
    --network chrome-guest-desktop_chrome-net \
    --network gemini-web2api_default 2>/dev/null \
    -p 127.0.0.1:9229:22 alpine/socat \
    TCP-LISTEN:22,fork,reuseaddr TCP:chrome-seat-1:9223 >/dev/null 2>&1 || \
  docker run -d --name llq-cdp-bridge --restart unless-stopped \
    --network chrome-guest-desktop_chrome-net \
    -p 127.0.0.1:9229:22 alpine/socat \
    TCP-LISTEN:22,fork,reuseaddr TCP:chrome-seat-1:9223 >/dev/null 2>&1
  sleep 3
  docker network connect gemini-web2api_default llq-cdp-bridge >/dev/null 2>&1
fi

if ! curl -s -m 5 http://127.0.0.1:9229/json/version >/dev/null 2>&1; then
  log "链路修复失败，退出"
  exit 1
fi

# --- 循环删除 ---
deleted=0; skipped=0; fails=0
for i in $(seq 1 $MAX_ROUNDS); do
  OUT=$(timeout 60 $NODE $BASE/scripts/purge_history.mjs 2>/dev/null | tail -1)
  ST=$(echo "$OUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("status","?"))
except Exception: print("parse-fail")' 2>/dev/null)
  case "$ST" in
    deleted)    deleted=$((deleted+1));;
    skip-notebook) skipped=$((skipped+1));;
    exhausted)  log "完成: 删=$deleted 跳过=$skipped 失败=$fails"
                exit 0;;
    *)          fails=$((fails+1)); log "异常轮[$i]: $ST $OUT"
                [ $fails -ge 3 ] && { log "连续失败过多，终止"; exit 1; }
                sleep 3;;
  esac
  sleep 1
done
log "达到轮次上限: 删=$deleted 跳过=$skipped 失败=$fails"
exit 0
