"""BreezeIQ — the application.

Runs on the UNO Q's Linux side under App Lab. Three things happen here:

  1. the control loop decides, every 30 s
  2. the dashboard serves on :8000 over the board's own WiFi
  3. telemetry is written to SQLite, which is what Edge Impulse will train on

Nothing here decides anything itself. `brain.py` is the pure comfort policy;
this file is wiring.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if HERE.name != "python":
    # Source-tree execution before deploy. The staged board app is flat under
    # python/, while the repository keeps sense, brain, and act separate.
    for _path in (HERE.parents[1] / "brain", HERE.parents[1] / "sense",
                  HERE.parents[1] / "act"):
        sys.path.insert(0, str(_path))

from arduino.app_utils import App, Logger          # noqa: E402
from envfile import load_env, route_loopback_url   # noqa: E402

log = Logger(__name__) if callable(Logger) else None


def _say(msg: str) -> None:
    print(f"[breezeiq] {msg}", flush=True)


def _start_dashboard() -> None:
    """Serve on 0.0.0.0 so a phone on the same WiFi can open it. That is the
    whole point of the console living on the board instead of a laptop.

    One console, ONE port: 8000. It used to bind 8001 as well — the port the
    console was introduced on — so old bookmarks kept working. Two ports
    serving byte-identical pages is two things to check, two things to publish
    in app.yaml, and two chances to report "console up" while the one somebody
    typed is the one that is down. 8000 is what is written on the cards and in
    the handoff, so 8000 is the one that stayed.
    """
    os.environ.setdefault("BREEZEIQ_HOST", "0.0.0.0")
    try:
        import console
    except Exception as exc:
        _say(f"console did not start: {type(exc).__name__}: {exc}")
        return

    port = int(os.environ.get("BREEZEIQ_CONSOLE_PORT", "8000"))

    def run():
        try:
            console.serve(host="0.0.0.0", port=port)
        except Exception as exc:
            _say(f"console on :{port} stopped: {type(exc).__name__}: {exc}")
    threading.Thread(target=run, daemon=True).start()
    _say(f"console on http://0.0.0.0:{port}")


def _start_control() -> None:
    """The control loop, in its own thread so a slow device call never stalls
    the dashboard (and vice versa)."""
    try:
        from loop import Controller
    except Exception as exc:
        _say(f"control loop unavailable: {type(exc).__name__}: {exc}")
        return

    live_requested = os.environ.get("BREEZEIQ_LIVE", "0") == "1"
    safety_validated = os.environ.get("BREEZEIQ_SAFETY_VALIDATED", "0") == "1"
    live = live_requested and safety_validated
    automatic_actuation = (
        os.environ.get("BREEZEIQ_AUTOMATION_CONTROL_ENABLED", "0") == "1")
    source = os.environ.get("BREEZEIQ_SOURCE", "router")

    def run():
        c = Controller(live=live, source_name=source,
                       automatic_actuation=automatic_actuation)
        command = c.start_command_server()
        if command.error:
            _say(f"manual control unavailable: {command.error}")
        # Wake on arrivals and temperature steps instead of sleeping a fixed
        # interval. Same Controller method the standalone CLI uses, so the two
        # entrypoints cannot drift into different cadences.
        c.start_change_watcher()
        mode = ("LIVE manual, automatic observe-only"
                if live and not automatic_actuation else
                "LIVE automatic and manual" if live else "dry-run")
        if live_requested and not safety_validated:
            mode += " (live request blocked by safety gate)"
        _say(f"control loop started: {mode}, source={source}")
        while True:
            try:
                state = c.tick()
                if state.get("ok"):
                    p = state["plan"]
                    _say(f"PMV {state['pmv']:+.2f} fan={p.fan} ac={p.ac} "
                         f"blinds={'shut' if p.blinds_shut else 'open'} :: {p.reason}")
                else:
                    _say(f"holding — {state.get('why')}")
            except Exception as exc:                # a bad tick must not kill the app
                _say(f"tick error: {type(exc).__name__}: {exc}")
            try:
                c.wait_for_next_tick()
            except Exception:
                # Never let a broken wait become a hot loop that pins the board.
                time.sleep(5.0)

    threading.Thread(target=run, daemon=True).start()


def loop():
    # App.run wants a callable; the real work is in the threads above.
    time.sleep(30)


_say("starting")
# App Lab launches Python directly. Load the preserved board-local environment
# without shell expansion; existing injected variables keep precedence.
_default_env = HERE.parent / ".env" if HERE.name == "python" else HERE.parents[1] / ".env"
_env_path = Path(os.environ.get("BREEZEIQ_ENV_FILE", _default_env))
_loaded = load_env(_env_path)
if _loaded:
    _say(f"loaded {_loaded} board-local settings")
# App Lab isolates Python in Docker. Its compose file maps this hostname to
# the UNO Q Linux host, where Home Assistant publishes port 8123. Keep the
# stored URL valid for standalone systemd and translate it only in App Lab.
_ha_url = os.environ.get("HA_URL", "")
_routed_ha_url = route_loopback_url(_ha_url, "msgpack-rpc-router")
if _routed_ha_url != _ha_url:
    os.environ["HA_URL"] = _routed_ha_url
    _say("Home Assistant routed through the App Lab host gateway")
# App Lab supplies the board-native USB-camera detector Brick.  Standalone
# systemd mode may override these with fomo/opencv and an explicit source.
os.environ.setdefault("BREEZEIQ_COUNTER", "app-lab")
os.environ.setdefault("BREEZEIQ_SOURCE", "router")
# /app is the board directory bind-mounted by App Lab. Paths elsewhere in the
# container overlay can disappear when App Lab recreates the container.
if os.environ.get("BREEZEIQ_DB", "") in {
        "", "/home/arduino/breezeiq-data/telemetry.sqlite3"}:
    os.environ["BREEZEIQ_DB"] = "/app/data/telemetry.sqlite3"
if os.environ.get("BREEZEIQ_BACKUP_DIR", "") in {
        "", "/home/arduino/breezeiq-backups"}:
    os.environ["BREEZEIQ_BACKUP_DIR"] = "/app/backups"
os.environ.setdefault("BREEZEIQ_STATE_DIR", "/app/state")
os.environ.setdefault("BREEZEIQ_COMMAND_SOCKET", "/tmp/breezeiq-control.sock")
_start_dashboard()
_start_control()
App.run(user_loop=loop)
