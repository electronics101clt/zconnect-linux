"""Connection lifecycle.

This mirrors the Android client (pdanet-vpn-client) rather than reimplementing
it. Both sides now run the *same engine*: tun2proxy 0.7.19.

  Android  Tun2HttpVpnService.kt -> libtun2proxy.so (JNI, tunFd from VpnService)
  Linux    this file             -> tun2proxy-bin  (CLI, --setup)

Android configures the tunnel through VpnService.Builder:

    addAddress("10.1.10.1", 32)
    addRoute(...)            x16, everything except 192.168.0.0/16
    addDnsServer(...)        active-network DNS, else 8.8.8.8 / 8.8.4.4
    setMtu(1500)
    Tun2proxy.start(proxyUrl = "http://192.168.49.1:8000",
                    dnsStrategy = DnsStrategy.OVER_TCP)

On Linux there is no VpnService, so tun2proxy's own `--setup` does the
equivalent: it creates the tun device, installs the routes, and bind-mounts
/etc/resolv.conf so DNS enters the tunnel. `--bypass 192.168.0.0/16` reproduces
the one range Android deliberately leaves off the tunnel — the proxy lives at
192.168.49.1, and routing that into the tunnel would loop.

Why the engine matters: an HTTP CONNECT proxy is TCP-only by definition
(RFC 9110 §9.3.6), so UDP DNS cannot traverse it. Android never hit this because
tun2proxy rewrites DNS as TCP under `OVER_TCP`. The previous Linux build used
xjasonlyu/tun2socks, which has no DNS strategy at all — it copied Android's
routing table but not the mechanism that makes DNS work through it.
"""

import os
import shutil
import subprocess
import time

TUN_DEVICE = "tun0"
PDANET_GATEWAY = "192.168.49.1"
PROXY_PORT = 8000                  # HTTP CONNECT proxy, not SOCKS5
PROXY_URL = "http://%s:%d" % (PDANET_GATEWAY, PROXY_PORT)

# Android skips 192.168.0.0/16 across its 16 addRoute() calls; one --bypass
# expresses the same exclusion.
BYPASS_CIDR = "192.168.0.0/16"

# Matches Util.getDefaultDNS()'s fallback on the Android side.
DNS_ADDR = "8.8.8.8"
DNS_STRATEGY = "over-tcp"          # == Tun2proxy.DnsStrategy.OVER_TCP

TUN2PROXY_VERSION = "v0.7.19"      # same build as app/src/main/jniLibs/*/libtun2proxy.so


def _which(name, fallbacks):
    found = shutil.which(name)
    if found:
        return found
    for path in fallbacks:
        if os.path.exists(path):
            return path
    return fallbacks[0] if fallbacks else name


IP_BIN = _which("ip", ["/usr/sbin/ip", "/sbin/ip"])
SYSCTL_BIN = _which("sysctl", ["/usr/sbin/sysctl", "/sbin/sysctl"])
KILLALL_BIN = _which("killall", ["/usr/bin/killall"])
UMOUNT_BIN = _which("umount", ["/usr/bin/umount", "/bin/umount"])
TUN2PROXY_BIN = _which("tun2proxy-bin", ["/usr/local/bin/tun2proxy-bin"])


class ConnectionManager:
    """Drives one tether connection. All methods block; the UI calls
    connect()/disconnect() from a worker thread."""

    def __init__(self, log):
        self.log = log
        self.proc = None
        self.connected = False
        self.network = None
        self.wifi_interface = None

    # ---------- public API ----------

    def preflight(self):
        """Return a list of human-readable problems, empty if good to go."""
        problems = []
        if not os.path.exists(TUN2PROXY_BIN):
            problems.append("tun2proxy-bin is not installed — run ./install.sh")
        if shutil.which("nmcli") is None:
            problems.append("NetworkManager (nmcli) is not installed")
        if not os.path.exists("/dev/net/tun"):
            problems.append("/dev/net/tun is missing — the tun kernel module is not loaded")
        rc, _ = self._run(["sudo", "-n", "true"], timeout=5)
        if rc != 0:
            problems.append("passwordless sudo is not configured — run ./install.sh")
        return problems

    def connect(self, ssid):
        try:
            problems = self.preflight()
            if problems:
                for p in problems:
                    self.log("ERROR: %s" % p)
                return False

            self.log("Target network: %s" % ssid)

            self.log("Connecting to WiFi...")
            if not self._connect_wifi(ssid):
                self.log("ERROR: failed to join %s" % ssid)
                return False

            self.log("Waiting for network...")
            if not self._wait_for_network():
                self.log("ERROR: no address on the PdaNet subnet")
                self._disconnect_wifi(ssid)
                return False

            self.log("Verifying gateway...")
            if not self._ping_gateway():
                self.log("ERROR: cannot reach PdaNet gateway %s" % PDANET_GATEWAY)
                self._disconnect_wifi(ssid)
                return False

            # rp_filter is a Linux-only accommodation with no Android
            # counterpart: replies arriving on tun0 fail the kernel's reverse
            # path check while the default route still points at wlan.
            self._sudo([SYSCTL_BIN, "-w", "net.ipv4.conf.all.rp_filter=0"])

            self.log("Starting tunnel (tun2proxy %s, dns=%s)..."
                     % (TUN2PROXY_VERSION, DNS_STRATEGY))
            if not self._start_tun2proxy():
                self.log("ERROR: tun2proxy did not start")
                self.cleanup()
                return False

            self.log("Verifying connection...")
            if not self._verify():
                self.log("WARNING: connectivity check failed, tunnel is up anyway")

            self.network = ssid
            self.connected = True
            self.log("Connection established.")
            return True

        except Exception as exc:              # noqa: BLE001 - surfaced in the log
            self.log("ERROR: %s" % exc)
            self.cleanup()
            return False

    def disconnect(self):
        self.log("Disconnecting...")
        self.cleanup()
        self.connected = False
        self.network = None

    def is_connected(self):
        """True if the tunnel is still up; self-heals if it died."""
        if not self.connected:
            return False

        if self.proc is not None and self.proc.poll() is not None:
            self.log("tun2proxy exited, cleaning up...")
            self.cleanup()
            self.connected = False
            return False

        if self._run([IP_BIN, "link", "show", TUN_DEVICE])[0] != 0:
            self.log("TUN interface gone, cleaning up...")
            self.cleanup()
            self.connected = False
            return False

        return True

    # ---------- steps ----------

    def _connect_wifi(self, ssid):
        rc, out = self._run(["nmcli", "connection", "up", ssid], timeout=45)
        for line in out.splitlines():
            self.log("nmcli: %s" % line.strip())
        if rc == 0:
            return True

        self.log("Trying as a new connection...")
        rc, out = self._run(["nmcli", "device", "wifi", "connect", ssid], timeout=45)
        for line in out.splitlines():
            self.log("nmcli: %s" % line.strip())
        return rc == 0

    def _disconnect_wifi(self, ssid):
        self._run(["nmcli", "connection", "down", ssid], timeout=20)

    def _wait_for_network(self):
        """Wait for an address on the PdaNet subnet and note the interface."""
        for _ in range(15):
            out = self._run([IP_BIN, "-o", "-4", "addr", "show"])[1]
            for line in out.splitlines():
                if "192.168.49." in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        self.wifi_interface = parts[1]
                    self.log("WiFi interface: %s" % self.wifi_interface)
                    return True
            time.sleep(1)
        return False

    def _ping_gateway(self):
        return self._run(["ping", "-c", "1", "-W", "2", PDANET_GATEWAY], timeout=10)[0] == 0

    def _start_tun2proxy(self):
        # --setup makes tun2proxy do on Linux what VpnService.Builder does on
        # Android: create the device, install routes, and point DNS at the
        # tunnel (via a bind mount over /etc/resolv.conf, undone on exit).
        cmd = [
            "sudo", "-n", TUN2PROXY_BIN,
            "--setup",
            "--tun", TUN_DEVICE,
            "--proxy", PROXY_URL,
            "--dns", DNS_STRATEGY,
            "--dns-addr", DNS_ADDR,
            "--bypass", BYPASS_CIDR,
            "--exit-on-fatal-error",
            "-v", "info",
        ]
        self.log("exec: %s" % " ".join(cmd[1:]))
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

        import threading

        def pump(proc):
            try:
                for line in proc.stdout:
                    self.log("tun2proxy: %s" % line.rstrip())
            except (ValueError, OSError):
                pass

        threading.Thread(target=pump, args=(self.proc,), daemon=True).start()

        # Wait for the interface --setup creates, rather than a fixed sleep.
        for _ in range(15):
            if self.proc.poll() is not None:
                return False
            if self._run([IP_BIN, "link", "show", TUN_DEVICE])[0] == 0:
                self.log("%s is up" % TUN_DEVICE)
                return True
            time.sleep(1)
        return False

    def _verify(self):
        """Check name resolution *and* transport, since DNS is the part that
        silently broke before."""
        rc, out = self._run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--connect-timeout", "8", "https://www.google.com"],
            timeout=20,
        )
        ok = rc == 0 and out.strip() in ("200", "301", "302")
        self.log("connectivity check: %s" % ("ok" if ok else "failed (%s)" % out.strip()))
        return ok

    def cleanup(self):
        self.log("Cleaning up...")

        # SIGTERM lets tun2proxy restore the routes and unmount the
        # /etc/resolv.conf overlay it installed. SIGKILL would strand both,
        # so it is only a fallback, and the safety net below covers it.
        if self.proc is not None or self._tun2proxy_running():
            self._sudo([KILLALL_BIN, "-TERM", os.path.basename(TUN2PROXY_BIN)])
            for _ in range(10):
                if not self._tun2proxy_running():
                    break
                time.sleep(0.5)
            if self._tun2proxy_running():
                self.log("tun2proxy did not exit, forcing.")
                self._sudo([KILLALL_BIN, "-KILL", os.path.basename(TUN2PROXY_BIN)])
        if self.proc is not None:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            self.proc = None

        # Safety net for a forced kill: routes vanish with the device, but the
        # resolv.conf bind mount does not.
        if self._run([IP_BIN, "link", "show", TUN_DEVICE])[0] == 0:
            self._sudo([IP_BIN, "link", "delete", TUN_DEVICE])
        if self._is_resolv_conf_mounted():
            self.log("Removing leftover /etc/resolv.conf overlay.")
            self._sudo([UMOUNT_BIN, "/etc/resolv.conf"])

        self._sudo([SYSCTL_BIN, "-w", "net.ipv4.conf.all.rp_filter=1"])

        if self.network:
            self._disconnect_wifi(self.network)

        self.log("Cleanup complete.")

    # ---------- helpers ----------

    def _tun2proxy_running(self):
        return self._run(["pgrep", "-x", os.path.basename(TUN2PROXY_BIN)])[0] == 0

    def _is_resolv_conf_mounted(self):
        try:
            with open("/proc/self/mountinfo") as fh:
                return any(" /etc/resolv.conf " in line for line in fh)
        except OSError:
            return False

    def _run(self, args, timeout=20):
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            return 1, "timed out: %s" % " ".join(args)
        except OSError as exc:
            return 1, str(exc)

    def _sudo(self, args, timeout=20):
        return self._run(["sudo", "-n"] + args, timeout=timeout)
