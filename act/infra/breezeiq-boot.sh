#!/bin/bash
# Start BreezeIQ at boot, and heal the MCU bridge a reflash leaves stale.
#
# One manifest now, and it declares no Brick — the camera is the YOLO counter's
# own (cv2 on /dev/video), so nothing here can fail to start for want of a
# device, and the old camera/nocamera manifest split is gone.
#
# What DID need fixing is the bridge. `arduino-app-cli app restart` reflashes
# the STM32 sketch, which resets the micro out from under the running
# arduino-router; the router's serial link goes stale and every breezeiq/read
# comes back "method not available" until the router re-syncs. The fix, proven
# by hand, is to restart the router with NO app racing the micro reset, THEN
# start the app fresh so the wait_linux_boot=app handshake completes. Doing it
# here makes both a cold boot and a post-deploy run self-heal.
set -u
LOG="logger -t breezeiq-autostart"

for _ in $(seq 1 12); do                 # docker settle
  systemctl is-active --quiet docker && break
  sleep 5
done

# 1. App stopped, so nothing drives the micro while the router resets it.
/usr/bin/arduino-app-cli app stop user:breezeiq 2>&1 | $LOG || true

# 2. Restart arduino-router -> clean micro reset + re-synced serial link.
#    systemctl needs root. The arduino user is not root but IS in the docker
#    group, and the daemon runs as root, so a --pid=host --user 0 container
#    reaches the host's own systemd (the --pid=host is what lets systemd's
#    peer-credential check on its private socket accept the connection). No
#    password, and nothing installed. Non-fatal: if it cannot restart the
#    router, the app still starts — the bridge may then need a manual heal.
restart_router() {
  local img
  img=$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
        | grep -m1 'python-apps-base') || return 1
  [ -n "$img" ] || return 1
  docker run --rm --pid=host --user 0:0 -v /:/host --entrypoint chroot \
    "$img" /host systemctl restart arduino-router
}
if out=$(restart_router 2>&1); then
  $LOG "arduino-router restarted; micro reset cleanly"
else
  $LOG "WARNING: could not restart arduino-router; bridge may need a manual heal: $out"
fi
sleep 3

# 3. Start the app fresh. The flash now completes the handshake because the
#    router just re-synced. `restart` is idempotent and reconciles compose
#    state, unlike `start` which no-ops when the app is already marked running.
/usr/bin/arduino-app-cli app restart user:breezeiq 2>&1 | $LOG
