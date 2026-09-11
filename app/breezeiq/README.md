# BreezeIQ — the App Lab app

Passive-first room comfort on the Arduino UNO Q. Both halves of the board are used:

- **STM32** (`sketch/`) — sensors, the blinds motor, the LED matrix, and a failsafe:
  10 s without a Bridge call and the MCU drives the blinds open at full speed and
  lights a matrix glyph on its own, re-arming when calls resume.
- **Linux MPU** (`python/`) — PMV comfort maths, the passive-first ladder, device
  control, and the console on `:8000`.

## Run

Press **Run** in App Lab. Then open `http://<board-ip>:8000` from any phone on the
same Wi-Fi. `tools/deploy.sh` also installs a boot hook, so the app comes back on
its own after a power cut with no laptop present.

Dry-run by default. Real commands require both `BREEZEIQ_LIVE=1` and
`BREEZEIQ_SAFETY_VALIDATED=1` in the board's own `.env`, which `deploy.sh` carries
forward and never uploads. Credentials live only there:

    HA_URL, HA_TOKEN                        Tuya devices via Home Assistant
    ATOMBERG_API_KEY, ATOMBERG_REFRESH_TOKEN, ATOMBERG_FAN_ID

Home Assistant runs at `http://127.0.0.1:8123` on the board and holds device
integrations only. BreezeIQ owns policy and manual-override authority.

## The one rule

`brain.py` is the only thing that makes a comfort decision, and it is a pure
function. Everything else — sensors, devices, dashboard — is transport. Keep it that
way and any surprise has exactly one suspect.
