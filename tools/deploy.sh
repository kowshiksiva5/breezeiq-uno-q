#!/usr/bin/env bash
# Assemble the App Lab app and replace it only after board-side import checks.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="/home/arduino/ArduinoApps/breezeiq"
BOOT_SCRIPT="/home/arduino/breezeiq-boot.sh"
NEXT="/home/arduino/ArduinoApps/.breezeiq-next"
PREVIOUS="/home/arduino/ArduinoApps/.breezeiq-previous"
DATA_ROOT="$TARGET/data"
DATA_DB="$DATA_ROOT/telemetry.sqlite3"
BACKUP_ROOT="$TARGET/backups"
STATE_ROOT="$TARGET/state"
LEGACY_DATA_ROOT="/home/arduino/breezeiq-data"
LEGACY_BACKUP_ROOT="/home/arduino/breezeiq-backups"
STAGE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/breezeiq-deploy.XXXXXX")"
STAGE="$STAGE_ROOT/breezeiq"
trap 'rm -rf "$STAGE_ROOT"' EXIT

# TRANSPORT — ssh first, adb second, and that order matters physically.
# The UNO Q has ONE USB controller. Attaching a USB-C data cable puts it in
# peripheral mode: that is what serves adb, and it is also what holds the
# usb_vbus regulator disabled so no USB camera can enumerate. Deploying over
# adb therefore costs you the camera. SSH over WiFi has no such conflict, so
# the cable-free path is the default and adb is the fallback for a board that
# is not yet on the network.
# breeze.local via mDNS (avahi runs on the board) — survives DHCP lease
# changes, which renumbered the board once already (.2 -> .15 -> .21).
BOARD="${BOARD:-breeze.local}"
BOARD_USER="${BOARD_USER:-arduino}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8)

if ssh "${SSH_OPTS[@]}" "$BOARD_USER@$BOARD" true 2>/dev/null; then
  TRANSPORT=ssh
elif adb get-state >/dev/null 2>&1; then
  TRANSPORT=adb
  echo "WARNING: deploying over adb. While USB is attached the board cannot" >&2
  echo "         host a USB camera. Unplug and re-run over SSH before a demo." >&2
else
  echo "board unreachable over ssh ($BOARD_USER@$BOARD) or adb" >&2
  echo "  ssh needs the board powered, on WiFi, and your key installed" >&2
  exit 1
fi
echo "transport: $TRANSPORT"

board_sh() {
  case "$TRANSPORT" in
    ssh) ssh "${SSH_OPTS[@]}" "$BOARD_USER@$BOARD" "$@" ;;
    adb) adb shell "$@" ;;
  esac
}

# tar over the pipe rather than scp -r: it reproduces the tree exactly,
# including dotfiles, which `scp -r src/.` does not do reliably.
board_push() {
  local from="$1" to="$2"
  case "$TRANSPORT" in
    ssh) tar -C "$from" -cf - . | ssh "${SSH_OPTS[@]}" "$BOARD_USER@$BOARD" \
           "tar -C '$to' -xf -" && echo "  pushed $(find "$from" -type f | wc -l | tr -d ' ') files" ;;
    adb) adb push "$from/." "$to/" 2>&1 | tail -1 ;;
  esac
}

# Replacing code under a live Python process can mix old and new modules. App
# Lab launches relative filenames, so command text alone cannot identify the
# owner. Resolve each process working directory on the board instead.
running="$(board_sh "for process in /proc/[0-9]*; do \
  cwd=\$(readlink \"\$process/cwd\" 2>/dev/null || true); \
  case \"\$cwd\" in $TARGET/python*) \
    pid=\${process##*/}; \
    cmd=\$(tr '\\000' ' ' < \"\$process/cmdline\" 2>/dev/null || true); \
    printf '%s %s\\n' \"\$pid\" \"\$cmd\";; esac; done" | tr -d '\r')"
running="$(printf '%s\n' "$running" | grep -v 'devices\.lan_bridge' || true)"
if [ -n "$running" ]; then
  echo "BreezeIQ is running on the board; stop it before deploying" >&2
  printf '%s\n' "$running" >&2
  echo "No files were changed." >&2
  exit 1
fi

# THE PROBE ABOVE CANNOT SEE THE CONTAINER, which is the only way this app runs.
# It reads /proc from the host, but App Lab launches the app inside a container
# whose cwd is `/app/python` — a path that can never match $TARGET/python, so the
# guard passed on a board that was plainly running.
#
# What that costs is not a mixed-module import, it is the whole durable record.
# The swap below gives $TARGET a NEW inode and then deletes the old one, while a
# running container's bind mount stays pinned to the inode it was created with.
# Observed: the app kept ticking and deciding for two hours against a deleted
# directory, writing telemetry no one could read, while the dashboard beside it
# correctly reported "database file does not exist". Nothing crashed, nothing
# logged, and the room looked fine the entire time. Ask docker as well.
# Matched by MOUNT, not by name. A name filter also catches the vision runner,
# which bind-mounts only the Edge Impulse model directories and so cannot be
# orphaned by the swap — blocking a deploy on it would be a guard that cries
# wolf, and those get worked around rather than heeded. The precise condition is
# "a running container whose bind mount is the directory about to be replaced".
container="$(board_sh "docker ps -q 2>/dev/null \
  | xargs -r docker inspect --format '{{.Name}} {{range .Mounts}}{{.Source}} {{end}}' 2>/dev/null \
  | grep -F '$TARGET ' | awk '{print \$1}' | tr -d '/' || true" | tr -d '\r')"
if [ -n "$container" ]; then
  echo "BreezeIQ container is running on the board; stop it before deploying" >&2
  printf '  %s\n' "$container" >&2
  echo "  stop it with: ssh $BOARD_USER@$BOARD 'docker stop $container'" >&2
  echo "  deploying over a live container orphans its bind mount and the app" >&2
  echo "  writes telemetry into a deleted directory until it is restarted." >&2
  echo "No files were changed." >&2
  exit 1
fi

mkdir -p "$STAGE/python" "$STAGE/sketch"
# One manifest now, and it declares no Brick. The camera is the YOLO counter's,
# opened directly with cv2, so there is nothing here that App Lab can refuse to
# start for want of a device — the old camera/nocamera split and its board-side
# probe are gone.
cp "$ROOT/app/breezeiq/app.yaml" "$STAGE/app.yaml"
cp "$ROOT/app/breezeiq/README.md" "$STAGE/"
cp "$ROOT/app/breezeiq/main.py" "$STAGE/python/"
# App Lab pip-installs this at app start; without it the YOLO counter
# cannot import onnxruntime and occupancy silently drops to PIR-only.
cp "$ROOT/app/breezeiq/requirements.txt" "$STAGE/python/"
cp "$ROOT"/brain/*.py "$STAGE/python/"
# Fitted forecast weights ship when they exist. fit.py only writes this file
# when the fit beats EWMA on held-out data, so its presence means "better".
[ -f "$ROOT/brain/ar_weights.json" ] && cp "$ROOT/brain/ar_weights.json" "$STAGE/python/"
cp "$ROOT/brain/about.html" "$STAGE/python/"
cp -r "$ROOT/sense/reader" "$ROOT/act/devices" "$STAGE/python/"

# Runtime vision code ships. Raw frames, datasets and heavy desktop weights do
# not. App Lab provides the preferred board camera Brick.
cp -r "$ROOT/sense/vision" "$STAGE/python/"
rm -rf "$STAGE/python/vision/dataset" "$STAGE/python/vision/debug"
find "$STAGE/python/vision/models" -type f \
     ! -name ".gitkeep" ! -name "*.eim" ! -name "*.onnx" -delete 2>/dev/null || true

cp "$ROOT/firmware/breezeiq/breezeiq.ino" "$STAGE/sketch/sketch.ino"
cp "$ROOT/firmware/breezeiq/sketch.yaml" "$STAGE/sketch/"
find "$STAGE" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "staged $(find "$STAGE" -type f | wc -l | tr -d ' ') files"

# App Lab bind-mounts TARGET as /app. Data under /app therefore lives on the
# board instead of the disposable container overlay. Migrate either legacy
# database with SQLite's online backup API. Credentials never leave the board.
board_sh "mkdir -p $DATA_ROOT $BACKUP_ROOT $STATE_ROOT \
  && chmod 700 $DATA_ROOT $BACKUP_ROOT $STATE_ROOT"
board_sh python3 - "$DATA_DB" "$TARGET/python/telemetry.sqlite3" \
  "$LEGACY_DATA_ROOT/telemetry.sqlite3" <<'PY'
import pathlib, sqlite3, sys
target = pathlib.Path(sys.argv[1])
source = next((pathlib.Path(item) for item in sys.argv[2:]
               if pathlib.Path(item).is_file()), None)
if source is not None and not target.exists():
    with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as old:
        with sqlite3.connect(target) as new:
            old.backup(new)
    print("  migrated legacy telemetry database")
PY

if board_sh "test -d $LEGACY_BACKUP_ROOT"; then
  board_sh "cp -an $LEGACY_BACKUP_ROOT/. $BACKUP_ROOT/"
fi

board_sh "rm -rf $NEXT && mkdir -p $NEXT"
board_push "$STAGE" "$NEXT"
board_sh "if [ -f $TARGET/.env ]; then \
  install -m 0600 $TARGET/.env $NEXT/.env; fi"
board_sh "for item in data backups state; do \
  if [ -d $TARGET/\$item ]; then cp -a $TARGET/\$item $NEXT/\$item; fi; \
  done"
board_sh "cd $NEXT/python && python3 -c 'import sys; sys.path.insert(0,\".\"); import brain, loop, telemetry; print(\"  board import check: OK\")'" 2>&1 | tail -1

# Failed staging/import leaves the current app untouched. A successful check
# makes the new tree visible as one directory rename.
board_sh "rm -rf $PREVIOUS; \
  if [ -d $TARGET ]; then mv $TARGET $PREVIOUS; fi; \
  mv $NEXT $TARGET; \
  rm -rf $PREVIOUS"

# AUTONOMY. App Lab's "default app" property does NOT start the app at boot —
# verified the hard way: after a power cut the daemon was enabled and knew the
# default, while both containers sat at `Exited (255)` and never came back.
# A user crontab @reboot entry does the job with no root, and the boot script
# also chooses the manifest that matches the hardware actually present.
board_push_file() {
  case "$TRANSPORT" in
    ssh) ssh "${SSH_OPTS[@]}" "$BOARD_USER@$BOARD" "cat > '$2'" < "$1" ;;
    adb) adb push "$1" "$2" >/dev/null ;;
  esac
}
board_push_file "$ROOT/act/infra/breezeiq-boot.sh" "$BOOT_SCRIPT"
board_sh "chmod +x '$BOOT_SCRIPT'"
board_sh "crontab -l 2>/dev/null | grep -q 'breezeiq-boot.sh' || { \
  (crontab -l 2>/dev/null; echo '@reboot sleep 30 && $BOOT_SCRIPT') | crontab -; }"
board_sh "crontab -l 2>/dev/null | grep -c 'breezeiq-boot.sh'" \
  | tr -d '\r' | sed 's/^/  boot-autostart entries: /'

echo "deployed to $TARGET"
echo "runtime remains stopped; start it in App Lab (recommended) or systemd"
