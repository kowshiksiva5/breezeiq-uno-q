#!/usr/bin/env bash
# Connect the UNO Q to Wi-Fi without accepting or logging a password argument.
# Requires a USB/ADB connection. nmcli prompts interactively with echo disabled.
set -euo pipefail

SSID="${1:-${BREEZEIQ_WIFI_SSID:-}}"
if [ "$#" -gt 1 ] || [ -z "$SSID" ]; then
  echo "usage: BREEZEIQ_WIFI_SSID=<ssid> $0" >&2
  echo "The Wi-Fi password is requested interactively and is never an argument." >&2
  exit 2
fi
case "$SSID" in
  *$'\n'*|*$'\r'*) echo "SSID must be one line" >&2; exit 2 ;;
esac

ADB=(adb)
if [ -n "${BREEZEIQ_ADB_SERIAL:-}" ]; then
  ADB+=(-s "$BREEZEIQ_ADB_SERIAL")
fi
"${ADB[@]}" get-state >/dev/null 2>&1 || {
  echo "no UNO Q over ADB; connect a USB-C data cable first" >&2
  exit 1
}

printf -v QUOTED_SSID '%q' "$SSID"
echo "Connecting UNO Q to: $SSID"
echo "Enter the board Linux password if sudo asks, then the Wi-Fi password."
"${ADB[@]}" shell -t "sudo nmcli --ask device wifi connect $QUOTED_SSID"

echo
echo "Network state"
"${ADB[@]}" shell "nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status; hostname -I"

echo
echo "Internet and DNS proof"
"${ADB[@]}" shell "ping -c 2 -W 2 1.1.1.1 >/dev/null \
  && getent ahostsv4 docs.arduino.cc | head -1"

echo "Wi-Fi and Internet verified from the UNO Q."
