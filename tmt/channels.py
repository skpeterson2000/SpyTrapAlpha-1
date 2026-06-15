"""Alert delivery channels. Each .send(alert) -> channel name if it acted.

Channels are best-effort and isolated: a failure in one (no speaker, no
network) must never stop the others or crash the engine. The engine records
which channels actually fired on each alert.
"""

import logging
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOUNDS_DIR = ROOT / "sounds"

log = logging.getLogger("tmt.alerts")


def _headline(alert):
    return (f"{alert['tier']} tracker: {alert.get('tracker') or alert['radio']} "
            f"@ {alert['identity']}")


def _body(alert):
    return (f"{alert['reason']}\nscore {alert['score']}, "
            f"{alert['n_sessions']} sessions")


class AudibleChannel:
    """Play a tier-mapped sound file through the Pi's audio out.

    Auto-detects a player by file extension at first use. Add files to sounds/
    and map them in config['audio']['sounds']; missing files are skipped
    quietly so a fresh install is silent rather than broken.
    """

    PLAYERS = {  # extension -> ordered candidate CLI players
        ".wav": ["paplay", "aplay", "ffplay"],
        ".mp3": ["mpg123", "ffplay", "cvlc"],
        ".ogg": ["paplay", "ogg123", "ffplay"],
    }

    def __init__(self, config):
        self.cfg = config.get("audio", {})
        self.sounds = self.cfg.get("sounds", {})
        self.forced = self.cfg.get("player")

    def _player_for(self, path: Path):
        if self.forced and shutil.which(self.forced):
            return self.forced
        for cand in self.PLAYERS.get(path.suffix.lower(), []):
            if shutil.which(cand):
                return cand
        return None

    def _args(self, player, path):
        if player == "ffplay":
            return [player, "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)]
        if player == "cvlc":
            return [player, "--play-and-exit", "--intf", "dummy", str(path)]
        return [player, str(path)]

    def send(self, alert):
        if not self.cfg.get("enabled", True):
            return None
        fname = self.sounds.get(alert["tier"]) or self.sounds.get("_default")
        if not fname:
            return None
        path = SOUNDS_DIR / fname
        if not path.exists():
            log.warning("audio: sound file missing: %s", path)
            return None
        player = self._player_for(path)
        if not player:
            log.warning("audio: no player found for %s", path.suffix)
            return None
        try:
            # Fire-and-forget so a long track doesn't block the engine loop.
            subprocess.Popen(self._args(player, path),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "audible"
        except Exception as e:
            log.warning("audio: playback failed: %s", e)
            return None


class PushChannel:
    """Phone push via ntfy (no account) or Pushover. Uses urllib (no deps)."""

    def __init__(self, config):
        self.cfg = config.get("push", {})

    def send(self, alert):
        if not self.cfg.get("enabled"):
            return None
        try:
            if self.cfg.get("provider") == "pushover":
                return self._pushover(alert)
            return self._ntfy(alert)
        except Exception as e:
            log.warning("push: send failed: %s", e)
            return None

    def _ntfy(self, alert):
        topic = self.cfg.get("ntfy_topic")
        if not topic:
            return None
        url = self.cfg["ntfy_server"].rstrip("/") + "/" + topic
        req = urllib.request.Request(
            url, data=_body(alert).encode(),
            headers={
                "Title": _headline(alert),
                "Priority": "urgent" if alert["tier"] == "HIGH" else "high",
                "Tags": "rotating_light",
            })
        urllib.request.urlopen(req, timeout=10).read()
        return "push:ntfy"

    def _pushover(self, alert):
        data = urllib.parse.urlencode({
            "token": self.cfg["pushover_token"],
            "user": self.cfg["pushover_user"],
            "title": _headline(alert),
            "message": _body(alert),
            "priority": 1 if alert["tier"] == "HIGH" else 0,
        }).encode()
        urllib.request.urlopen("https://api.pushover.net/1/messages.json",
                               data=data, timeout=10).read()
        return "push:pushover"


class LogChannel:
    """Persist a human-readable line to the alerts log file."""

    def __init__(self, config):
        self.cfg = config.get("log", {})
        self.path = self.cfg.get("file")

    def send(self, alert):
        if not self.cfg.get("enabled", True) or not self.path:
            return None
        import time
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{alert['tier']}] "
                f"{_headline(alert)} | {_body(alert)}".replace("\n", " ") + "\n")
        try:
            with open(self.path, "a") as fh:
                fh.write(line)
            return "log"
        except Exception as e:
            log.warning("log: write failed: %s", e)
            return None


def build_channels(config):
    return [AudibleChannel(config), PushChannel(config), LogChannel(config)]
