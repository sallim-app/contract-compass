#!/usr/bin/env bash
# 계약나침반 콜드 스탠바이 동기화 — naru → quant:/home/ubuntu/standby/contract-compass
# (2026-07-30 P3. 장애 시 전환 절차는 /data/ops/RUNBOOK-contract-failover.md, 함대 기록은 /data/ops/FLEET.md)
# 제외: 런타임 상태(세션·카운터·로그)는 스탠바이에서 새로 시작하는 게 맞다.
set -euo pipefail
SRC=/data/apps/contract-compass/
DST=quant:/home/ubuntu/standby/contract-compass/
ssh -o ConnectTimeout=10 quant "mkdir -p /home/ubuntu/standby/contract-compass"
rsync -a --delete \
  --exclude '__pycache__/' --exclude '.git/' --exclude 'logs/' \
  --exclude 'data/sessions.db*' --exclude 'data/rate_limiter.db*' \
  --exclude 'data/openai_daily_cap*.json' --exclude 'frontend/node_modules/' \
  "$SRC" "$DST"
echo "$(date -Is) sync OK" | ssh quant "cat >> /home/ubuntu/standby/contract-compass/.last-sync"

# 임베딩 모델 캐시(HF_HUB_OFFLINE=1이라 캐시 필수 — 2026-07-30 스모크에서 발견).
# 동일하면 no-op. 모델 교체 시 자동 추종.
rsync -a "$HOME/.cache/huggingface/hub/" quant:.cache/huggingface/hub/
