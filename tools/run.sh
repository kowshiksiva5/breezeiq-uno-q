#!/usr/bin/env bash
# BreezeIQ. Run from anywhere; paths resolve to the repo root.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || [ "${1:-}" = setup ] || { echo "no venv — run: ./tools/run.sh setup"; exit 1; }
# Credentials live outside git; absent is fine, the layers report it honestly.
[ -f "$ROOT/.env" ] && { set -a; . "$ROOT/.env"; set +a; }

case "${1:-help}" in
  setup)   python3 -m venv "$ROOT/.venv"
           "$ROOT/.venv/bin/pip" install -q --upgrade pip
           "$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
           echo "ready — now: ./tools/run.sh demo" ;;
  demo)    cd "$ROOT/brain" && PYTHONPATH="$ROOT/brain:$ROOT/sense:$ROOT/act" \
             $PY loop.py --dry-run --source mock --ticks "${2:-10}" ;;
  console) cd "$ROOT/brain" && $PY console.py ;;
  control) shift; cd "$ROOT/brain" && PYTHONPATH="$ROOT/brain:$ROOT/sense:$ROOT/act" $PY loop.py "$@" ;;
  devices) shift; cd "$ROOT/act" && PYTHONPATH="$ROOT/brain:$ROOT/sense:$ROOT/act" $PY -m devices.cli "$@" ;;
  vision)  shift; cd "$ROOT/sense" && PYTHONPATH="$ROOT/sense" $PY -m vision.cli "$@" ;;
  wifi)    shift; "$ROOT/tools/board-connect-wifi.sh" "$@" ;;
  deploy)  "$ROOT/tools/deploy.sh" ;;
  *) cat <<'USAGE'
BreezeIQ

  ./tools/run.sh setup                venv + dependencies
  ./tools/run.sh demo [ticks]         run the whole loop on simulated sensors,
                                      no hardware, commands nothing
  ./tools/run.sh console              serve the web console on :8000
  ./tools/run.sh control --dry-run    decide against live sensors, touch nothing
  ./tools/run.sh control --live       command real devices (needs .env + gates)
  ./tools/run.sh devices health       device layer status
  ./tools/run.sh vision test          camera person-count check
  ./tools/run.sh wifi <ssid>          put the UNO Q on Wi-Fi over USB
  ./tools/run.sh deploy               push the app to the UNO Q over SSH
USAGE
  ;;
esac
