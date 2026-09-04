"""Release the SDR dongles on demand so OP25 can claim one — and take them back.

This Pi is a SHARED rig: OP25 (P25 scanner) may need a dongle at any moment, but
tmt-sdr@ / tmt-decode@ otherwise hold theirs continuously, which blocks OP25 from
starting. Rather than making the user SSH in and stop services, the dashboard
flips a cooperative release flag:

  * A flag file (config sdr.release_file, default /run/tmt/radio-release) is the
    single source of truth. It lives on tmpfs alongside the GPS fix, so the
    nightly reboot always comes back up collecting — a release can never be
    accidentally permanent.
  * The SDR loops check it every cycle and park WITHOUT opening a dongle, the
    same way they already park for the thermal governor. The services stay
    active; they simply stop touching the radio, so the USB handle is genuinely
    free and OP25 can claim it.
  * Releasing also signals any in-flight rtl_433 / rtl_power so the dongle frees
    immediately rather than at the end of a 60s decode burst. Those are our own
    children running as the same user, so no privilege escalation is involved.

Why not just `systemctl stop`: tmt-api runs session-less with NoNewPrivileges=true,
so stopping units from the API would need a polkit or sudo grant. A cooperative
flag needs no system-level privilege at all, and leaves systemd's own view of the
services untouched.
"""

import json
import os
import signal
import time
from pathlib import Path

from . import config as configmod

# Radio helpers we spawn that actually hold a dongle. Matched against /proc comm.
RADIO_PROCS = ("rtl_433", "rtl_power", "rtl_fm", "rtl_tcp")

DEFAULT_RELEASE_FILE = "/run/tmt/radio-release"
DEFAULT_POLL_SECONDS = 5.0


def _sdr_cfg():
    return (configmod.load().get("sdr") or {})


def release_path():
    return Path(_sdr_cfg().get("release_file") or DEFAULT_RELEASE_FILE)


def poll_seconds():
    try:
        return float(_sdr_cfg().get("release_poll_seconds")
                     or DEFAULT_POLL_SECONDS)
    except (TypeError, ValueError):
        return DEFAULT_POLL_SECONDS


def read_state():
    """The release record ({ts, reason, by}) or None when not released."""
    try:
        return json.loads(release_path().read_text())
    except (OSError, ValueError):
        # A flag file that exists but is unreadable/corrupt still means
        # released — fail SAFE toward giving the dongle up, never toward
        # grabbing a radio the user asked us to let go of.
        return {"ts": None, "reason": None, "by": None} \
            if release_path().exists() else None


def is_released():
    return release_path().exists()


def _our_radio_pids():
    """PIDs of radio helpers owned by this user (so we may signal them)."""
    me = os.getuid()
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        p = Path("/proc", entry)
        try:
            if (p / "comm").read_text().strip() not in RADIO_PROCS:
                continue
            if p.stat().st_uid != me:
                continue
        except (OSError, ValueError):
            continue                      # process vanished mid-scan
        pids.append(int(entry))
    return sorted(pids)


def radio_processes():
    """[{pid, name}] for radio helpers currently holding a dongle."""
    out = []
    for pid in _our_radio_pids():
        try:
            name = Path("/proc", str(pid), "comm").read_text().strip()
        except OSError:
            continue
        out.append({"pid": pid, "name": name})
    return out


def _signal_radio_children(grace=2.5):
    """Make in-flight radio helpers exit so the dongle frees NOW.

    SIGTERM first, then SIGKILL for anything still holding the radio after a
    short grace. The escalation matters: rtl_power only checks its exit flag
    between scan passes, so a polite TERM can outlast someone standing at OP25
    waiting for the dongle. These helpers are stateless — rtl_power's partial
    CSV is discarded by the caller and rtl_433 just streams — so killing one
    loses nothing but the current cycle.

    Best-effort by design: a helper that already exited, or that we may not
    signal, must never turn a release into an error. The flag is what actually
    stops the loops; this only makes it immediate.
    """
    termed = []
    for pid in _our_radio_pids():
        try:
            os.kill(pid, signal.SIGTERM)
            termed.append(pid)
        except OSError:
            continue
    deadline = time.time() + grace
    while time.time() < deadline and _our_radio_pids():
        time.sleep(0.15)
    killed = []
    for pid in _our_radio_pids():
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except OSError:
            continue
    return {"term": termed, "kill": killed}


def release(reason=None, by=None):
    """Park the SDR loops and free the dongles. Idempotent."""
    path = release_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "reason": reason, "by": by}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record))
    os.replace(tmp, path)                 # atomic: never a half-written flag
    signalled = _signal_radio_children()
    return {"released": True, "state": record, "signalled": signalled}


def resume():
    """Clear the flag so the SDR loops pick the dongles back up. Idempotent."""
    try:
        release_path().unlink()
    except FileNotFoundError:
        pass
    return {"released": False}


def wait_while_released(sleep_fn=time.sleep, poll=None, on_state=None):
    """Block while released. Mirrors Governor.wait_until_safe so the sensor
    loops read the same way for both reasons they can park.

    Returns True if we actually waited.
    """
    on_state = on_state or (lambda *a, **k: None)
    if not is_released():
        return False
    poll = poll or poll_seconds()
    on_state("released", read_state())
    while is_released():
        sleep_fn(poll)
    on_state("resumed", None)
    return True


def status():
    """Everything the dashboard needs to render the release toggle."""
    st = read_state()
    procs = radio_processes()
    try:
        from .devices import list_devices
        dongles = [{"serial": d.get("serial"), "product": d.get("product")}
                   for d in list_devices()]
    except Exception:
        dongles = []
    return {
        "released": st is not None,
        "since": (st or {}).get("ts"),
        "reason": (st or {}).get("reason"),
        "by": (st or {}).get("by"),
        "release_file": str(release_path()),
        # Non-empty while released means a helper has not exited yet; the
        # dashboard uses this to say "freeing…" rather than claiming success.
        "radio_processes": procs,
        "dongles": dongles,
    }
