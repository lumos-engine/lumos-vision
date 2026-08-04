"""Ping-based TV presence with debounce.

Empty host means the feature is off.  A single dropped packet must not tear
down the USB camera; consecutive failures / successes gate idle transitions.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

from processor.utils.logging import get_logger

log = get_logger(__name__)

Transition = Literal["online", "offline"]


def ping_host(host: str, timeout_sec: float = 1.0) -> bool:
    """Return True when ``host`` answers one ICMP echo request."""
    host = (host or "").strip()
    if not host:
        return True
    ping = shutil.which("ping")
    if not ping:
        log.warning("ping binary not found; treating %s as online", host)
        return True
    # Linux: -c count, -W timeout seconds.  macOS uses -W in milliseconds;
    # Screen Sight's power idle targets the Linux ambilight host.
    timeout = max(1, int(round(float(timeout_sec))))
    try:
        result = subprocess.run(
            [ping, "-c", "1", "-W", str(timeout), host],
            capture_output=True,
            check=False,
            timeout=timeout + 2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("ping %s failed: %s", host, exc)
        return False
    return result.returncode == 0


@dataclass
class PresenceMonitor:
    """Track online/offline with consecutive-check hysteresis."""

    offline_checks: int = 2
    online_checks: int = 1
    online: bool = True
    _fails: int = 0
    _oks: int = 0

    def reset(self, online: bool = True) -> None:
        self.online = online
        self._fails = 0
        self._oks = 0

    def update(self, reachable: bool) -> Transition | None:
        """Feed one probe result; return a transition or ``None`` if unchanged."""
        offline_need = max(1, int(self.offline_checks))
        online_need = max(1, int(self.online_checks))

        if reachable:
            self._oks += 1
            self._fails = 0
            if not self.online and self._oks >= online_need:
                self.online = True
                self._oks = 0
                return "online"
            return None

        self._fails += 1
        self._oks = 0
        if self.online and self._fails >= offline_need:
            self.online = False
            self._fails = 0
            return "offline"
        return None
