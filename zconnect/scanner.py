"""Scans for PdaNet WiFi Direct networks via nmcli.

Port of NetworkScanner.java. Same nmcli calls, same matching rules.
"""

import re
import subprocess

# DIRECT-*-*-PdaNet / DIRECT-*PdaNet*
PDANET_RE = re.compile(r"DIRECT-.*PdaNet.*|DIRECT-.*-PdaNet", re.IGNORECASE)


def _run(args, timeout=20):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (subprocess.TimeoutExpired, OSError):
        return 1, ""


def _split_terse(line):
    """Split an `nmcli -t` line on unescaped ':'.

    nmcli escapes ':' inside a field as '\\:', so a naive split mangles any
    SSID containing a colon.
    """
    fields, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    fields.append("".join(cur))
    return fields


def is_pdanet(ssid):
    if not ssid:
        return False
    return bool(PDANET_RE.fullmatch(ssid)) or "pdanet" in ssid.lower()


class Network:
    def __init__(self, ssid, signal=None, saved=False):
        self.ssid = ssid
        self.signal = signal
        self.saved = saved

    def label(self):
        if self.signal is not None:
            return "%s  (%s%%)" % (self.ssid, self.signal)
        if self.saved:
            return "%s  (saved)" % self.ssid
        return self.ssid

    def __eq__(self, other):
        return isinstance(other, Network) and other.ssid == self.ssid

    def __hash__(self):
        return hash(self.ssid)


class NetworkScanner:

    def rescan(self):
        """Ask NetworkManager to re-scan. Harmless if it rate-limits us."""
        _run(["nmcli", "device", "wifi", "rescan"], timeout=15)

    def scan(self):
        """Return the PdaNet networks currently visible, strongest first."""
        found = {}

        rc, out = _run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
        )
        for line in out.splitlines():
            parts = _split_terse(line)
            if not parts:
                continue
            ssid = parts[0].strip()
            if not ssid or not is_pdanet(ssid):
                continue
            signal = None
            if len(parts) >= 2:
                try:
                    signal = int(parts[1].strip())
                except ValueError:
                    signal = None
            prev = found.get(ssid)
            if prev is None or (signal or 0) > (prev.signal or 0):
                found[ssid] = Network(ssid, signal)

        for net in self._saved_connections():
            found.setdefault(net.ssid, net)

        return sorted(found.values(), key=lambda n: -(n.signal or 0))

    def _saved_connections(self):
        out = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])[1]
        nets = []
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) >= 2 and "wireless" in parts[1] and is_pdanet(parts[0].strip()):
                nets.append(Network(parts[0].strip(), saved=True))
        return nets

    def signal_for(self, ssid):
        """Signal strength 0-100 for an SSID, or None if not visible."""
        out = _run(["nmcli", "-t", "-f", "SSID,SIGNAL", "device", "wifi", "list"])[1]
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) >= 2 and parts[0].strip() == ssid:
                try:
                    return int(parts[1].strip())
                except ValueError:
                    return None
        return None

    def connected_ssid(self):
        out = _run(["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi"])[1]
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) >= 2 and parts[0].strip() == "yes":
                return parts[1].strip()
        return None
