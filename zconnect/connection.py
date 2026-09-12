"""Connection lifecycle.

This mirrors the Android client (pdanet-vpn-client) rather than reimplementing
it. Both sides run the *same engine*: tun2proxy 0.7.19.

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
tun2proxy rewrites DNS as TCP under `OVER_TCP`.

TEARDOWN — the part Linux cannot inherit
----------------------------------------
Android's teardown is atomic and guaranteed by the OS:

    Tun2proxy.shutdown(); pfd.close()

Closing the VpnService descriptor makes the framework drop the interface, its
routes and its DNS in one step. Nothing can be left behind, so Android needs no
recovery logic and never touches the WiFi association.

Linux has no such descriptor. Every piece has to be undone by hand, and if the
engine is killed hard — or the machine suspends, or the app crashes — the pieces
survive. The dangerous one is the /etc/resolv.conf bind mount: it outlives the
process, it is invisible to NetworkManager, and it redirects every DNS query on
whatever network you join next. Bind mounts also STACK, so one umount is not
necessarily enough. That is what makes a reboot look necessary.

So the Android behaviour is reproduced here by:
  * restoring only what we changed (rp_filter's real prior value; the WiFi
    association only if we were the ones who made it),
  * unmounting resolv.conf until no layers remain,
  * verifying afterwards that routing and DNS actually work again, and
  * sweeping leftovers at startup, which is the Linux stand-in for the
    guarantee Android gets from closing the fd.
"""

import os
import shutil
import socket
import subprocess
import time

# NOTE: --setup chooses the tunnel's own addressing; we do not set it. Verified
# on a live run: tun0 = 10.0.0.33 peer 10.0.0.1/24, and /etc/resolv.conf is
# pointed at 10.0.0.1 (tun2proxy's stub, which forwards to DNS_FALLBACK over
# TCP). Android's 10.1.10.1/32 is the VpnService.Builder value and does NOT
# apply here. Nothing below should assume a specific tunnel address.
TUN_DEVICE = "tun0"
PDANET_GATEWAY = "192.168.49.1"
PDANET_SUBNET_PREFIX = "192.168.49."
PROXY_PORT = 8000                  # HTTP CONNECT proxy, not SOCKS5
PROXY_URL = "http://%s:%d" % (PDANET_GATEWAY, PROXY_PORT)

# Android skips 192.168.0.0/16 across its 16 addRoute() calls; one --bypass
# expresses the same exclusion.
BYPASS_CIDR = "192.168.0.0/16"

# Matches Util.getDefaultDNS()'s fallback on the Android side.
DNS_FALLBACK = "8.8.8.8"
DNS_STRATEGY = "over-tcp"          # == Tun2proxy.DnsStrategy.OVER_TCP

TUN2PROXY_VERSION = "v0.7.19"      # same build as app/src/main/jniLibs/*/libtun2proxy.so

RESOLV_CONF = "/etc/resolv.conf"


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
TUN2PROXY_NAME = os.path.basename(TUN2PROXY_BIN)


class ConnectionManager:
    """Drives one tether connection. All methods block; the UI calls
    connect()/disconnect() from a worker thread."""

    def __init__(self, log):
        self.log = log
        self.proc = None
        self.connected = False
        self.network = None
        self.wifi_interface = None
        # Restore-state: only undo what we actually did.
        self.saved_rp_filter = None

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
        if self._run(["sudo", "-n", "true"], timeout=5)[0] != 0:
            problems.append("passwordless sudo is not configured — run ./install.sh")
        return problems

    def sweep(self):
        """Remove leftovers from a previous run. Safe to call any time.

        Android does not need this: closing the VpnService fd cannot fail
        halfway. On Linux a hard kill, a crash or a suspend can strand the tun
        device and — the one that actually breaks normal WiFi — the
        /etc/resolv.conf bind mount. Running this at startup is what keeps the
        tray app maintenance-free.
        """
        found = []

        if self._tun2proxy_running():
            found.append("an orphaned tun2proxy process")
            self._stop_engine()

        if self._link_exists(TUN_DEVICE):
            found.append("a leftover %s device" % TUN_DEVICE)
            self._sudo([IP_BIN, "link", "delete", TUN_DEVICE])

        layers = self._resolv_mount_count()
        if layers:
            found.append("%d stranded %s mount%s"
                         % (layers, RESOLV_CONF, "" if layers == 1 else "s"))
            self._unmount_resolv_all()

        if found:
            self.log("Swept leftovers: %s." % ", ".join(found))
            for problem in self.verify_restored():
                self.log("WARNING: %s" % problem)
        return found

    def ensure_pdanet_preferred(self, priority=100):
        """Make PdaNet the network NetworkManager reaches for first.

        Out in the field there IS no real WiFi, so the phone is the primary
        uplink, not a fallback. This raises autoconnect-priority on every saved
        PdaNet profile so NM joins the phone on its own.

        This is *profile preference*, not association: it never brings a
        connection up or down, so it cannot fight you the way the old active
        model did. NM still decides, and only among networks actually in range.

        Kept inside the app deliberately -- it is reproducible on every machine
        that runs this code, instead of being hand-applied wiring on one PC.
        """
        changed = []
        out = self._run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])[1]
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) < 2 or "wireless" not in parts[1]:
                continue
            name = parts[0]
            if "pdanet" not in name.lower():
                continue
            cur = self._run(["nmcli", "-t", "-f",
                             "connection.autoconnect,connection.autoconnect-priority",
                             "connection", "show", name])[1]
            want_auto = "connection.autoconnect:yes" in cur
            want_prio = "connection.autoconnect-priority:%d" % priority in cur
            if want_auto and want_prio:
                continue
            self._run(["nmcli", "connection", "modify", name,
                       "connection.autoconnect", "yes",
                       "connection.autoconnect-priority", str(priority)])
            changed.append(name)
        if changed:
            self.log("Set PdaNet as preferred network: %s" % ", ".join(changed))
        return changed

    def leftovers_present(self):
        """Cheap check for anything of ours still installed."""
        return bool(self._resolv_mount_count()
                    or self._link_exists(TUN_DEVICE)
                    or self._tun2proxy_running())

    def start_tunnel(self, ssid):
        """Build the tunnel over the link we are ALREADY on.

        Passive, like ZLauncher's KeepAliveService: WiFi association belongs to
        NetworkManager and the user. This never joins or leaves a network, so it
        can never fight you when you switch back to normal WiFi.
        """
        try:
            problems = self.preflight()
            if problems:
                for problem in problems:
                    self.log("ERROR: %s" % problem)
                return False

            # Never stack a second tunnel on top of a stranded one.
            self.sweep()

            self.log("PdaNet link detected: %s" % ssid)

            if not self._wait_for_network():
                self.log("ERROR: no address on the PdaNet subnet")
                self.cleanup()
                return False

            self.log("Verifying gateway...")
            if not self._gateway_reachable():
                self.log("ERROR: PdaNet proxy %s is not answering" % PROXY_URL)
                self.cleanup()
                return False

            # rp_filter is a Linux-only accommodation with no Android
            # counterpart: replies arriving on tun0 fail the kernel's reverse
            # path check while the default route still points at wlan.
            self.saved_rp_filter = self._read_rp_filter()
            self._sudo([SYSCTL_BIN, "-w", "net.ipv4.conf.all.rp_filter=0"])

            self.log("Starting tunnel (tun2proxy %s, dns=%s)..."
                     % (TUN2PROXY_VERSION, DNS_STRATEGY))
            if not self._start_tun2proxy():
                self.log("ERROR: tun2proxy did not start")
                self.cleanup()
                return False

            self.network = ssid
            self.connected = True

            self.log("Verifying connection...")
            if not self._verify():
                self.log("WARNING: connectivity check failed, tunnel is up anyway")

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
        """True if the tunnel is still up; self-heals if it died.

        Liveness is judged the way Android judges it — by the link, not by an
        SSID scan. PdaNetMonitorService watches for the 192.168.49.1 gateway on
        the WiFi link and tears down the moment it is gone.
        """
        if not self.connected:
            return False

        if self.proc is not None and self.proc.poll() is not None:
            self.log("tun2proxy exited, cleaning up...")
            self.cleanup()
            self.connected = False
            return False

        if not self._link_exists(TUN_DEVICE):
            self.log("TUN interface gone, cleaning up...")
            self.cleanup()
            self.connected = False
            return False

        if not self.pdanet_link_present():
            self.log("PdaNet link lost — tearing down...")
            self.cleanup()
            self.connected = False
            return False

        return True

    def current_ssid(self):
        """SSID we are associated with right now, or None."""
        return self._current_ssid()

    def on_pdanet(self):
        """ZLauncher's trigger, verbatim in spirit.

        seekAndConnectToPdaNet() matches the *currently connected* SSID with
        contains("pdanet", ignoreCase=true) -- not a DIRECT-* regex -- and
        checkPdaNetGateway() corroborates with the 192.168.49.1 gateway. Either
        signal is enough; both are re-evaluated every tick.
        """
        ssid = self._current_ssid()
        if ssid and "pdanet" in ssid.lower():
            return True, ssid
        if self.pdanet_link_present():
            return True, ssid or "PdaNet"
        return False, ssid

    def hotspot_active(self):
        """Linux analogue of isWifiHotspotActive().

        ZLauncher refuses to build a tunnel while the device is itself serving
        WiFi. On Linux that is an active NetworkManager connection in AP mode.
        """
        out = self._run(["nmcli", "-t", "-f", "NAME,TYPE,STATE", "connection", "show", "--active"])[1]
        for line in out.splitlines():
            if "wireless" in line and "activated" in line:
                name = line.split(":")[0]
                mode = self._run(["nmcli", "-t", "-f", "802-11-wireless.mode",
                                  "connection", "show", name])[1]
                if "ap" in mode.lower():
                    return True
        return False

    def pdanet_link_present(self):
        """Is an interface still holding a 192.168.49.x address?

        The Linux equivalent of PdaNetMonitorService.checkNetwork(): proof we
        are still on the phone's network, independent of any SSID scan.
        """
        out = self._run([IP_BIN, "-o", "-4", "addr", "show"])[1]
        return PDANET_SUBNET_PREFIX in out

    def verify_restored(self):
        """After teardown, confirm normal networking actually works again.

        Returns a list of problems; empty means you can join real WiFi without
        rebooting.
        """
        problems = []

        if not self._run([IP_BIN, "route", "show", "default"])[1].strip():
            problems.append("no default route")

        if self._resolv_mount_count():
            problems.append("%s is still bind-mounted" % RESOLV_CONF)

        if self._link_exists(TUN_DEVICE):
            problems.append("%s still exists" % TUN_DEVICE)

        if not problems and not self._dns_works():
            problems.append("DNS is not resolving")

        return problems

    # ---------- steps ----------

    def _wait_for_network(self):
        """Wait for an address on the PdaNet subnet and note the interface."""
        for _ in range(15):
            out = self._run([IP_BIN, "-o", "-4", "addr", "show"])[1]
            for line in out.splitlines():
                if PDANET_SUBNET_PREFIX in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        self.wifi_interface = parts[1]
                    self.log("WiFi interface: %s" % self.wifi_interface)
                    return True
            time.sleep(1)
        return False

    def _gateway_reachable(self):
        """Probe the proxy port, not ICMP.

        Android never pings — it only checks that the gateway *is*
        192.168.49.1. A phone that drops ICMP would fail a ping test while the
        proxy works perfectly, so test the thing we actually depend on.
        """
        try:
            with socket.create_connection((PDANET_GATEWAY, PROXY_PORT), timeout=5):
                self.log("Proxy %s is reachable" % PROXY_URL)
                return True
        except OSError as exc:
            self.log("Proxy probe failed: %s" % exc)
            return False

    def _link_dns(self):
        """DNS servers of the current link, mirroring Util.getDefaultDNS()."""
        out = self._run(["nmcli", "-t", "-f", "IP4.DNS", "device", "show"])[1]
        for line in out.splitlines():
            _, _, value = line.partition(":")
            value = value.strip()
            if value and not value.startswith(PDANET_SUBNET_PREFIX):
                return value
        return DNS_FALLBACK

    def _start_tun2proxy(self):
        # --setup makes tun2proxy do on Linux what VpnService.Builder does on
        # Android: create the device, install routes, and point DNS at the
        # tunnel (via a bind mount over /etc/resolv.conf, undone on exit).
        dns_addr = self._link_dns()
        cmd = [
            "sudo", "-n", TUN2PROXY_BIN,
            "--setup",
            "--tun", TUN_DEVICE,
            "--proxy", PROXY_URL,
            "--dns", DNS_STRATEGY,
            "--dns-addr", dns_addr,
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
            if self._link_exists(TUN_DEVICE):
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

    # ---------- teardown ----------

    def cleanup(self):
        """Undo everything, then prove normal networking is back.

        This is the hand-rolled equivalent of Android's `pfd.close()`.
        """
        self.log("Cleaning up...")

        self._stop_engine()

        # Safety net for a forced kill: routes vanish with the device, but the
        # resolv.conf overlay does not, and it is what breaks the next network.
        if self._link_exists(TUN_DEVICE):
            self._sudo([IP_BIN, "link", "delete", TUN_DEVICE])
        self._unmount_resolv_all()

        # Restore the value that was actually there — it is commonly 2 (loose),
        # not 1 (strict), and forcing 1 would silently tighten the machine.
        if self.saved_rp_filter is not None:
            self._sudo([SYSCTL_BIN, "-w",
                        "net.ipv4.conf.all.rp_filter=%s" % self.saved_rp_filter])
            self.saved_rp_filter = None

        # ZLauncher never writes to WiFi, so neither do we. The association
        # stays exactly as the user left it; only the tunnel is destroyed.

        problems = self.verify_restored()
        if problems:
            for problem in problems:
                self.log("WARNING: after cleanup — %s" % problem)
            self.log("Run ./install.sh or see README 'Manual cleanup'.")
        else:
            self.log("Cleanup complete — routing and DNS restored.")

    def _stop_engine(self):
        """SIGTERM first so tun2proxy can restore routes and unmount DNS."""
        if self.proc is None and not self._tun2proxy_running():
            return

        self._sudo([KILLALL_BIN, "-TERM", TUN2PROXY_NAME])
        for _ in range(20):
            if not self._tun2proxy_running():
                break
            time.sleep(0.5)
        if self._tun2proxy_running():
            self.log("tun2proxy did not exit in 10s, forcing.")
            self._sudo([KILLALL_BIN, "-KILL", TUN2PROXY_NAME])
            time.sleep(1)

        if self.proc is not None:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            self.proc = None

    def _unmount_resolv_all(self):
        """Unmount every stacked overlay on /etc/resolv.conf.

        Bind mounts stack, so a single umount can leave an older layer exposed
        and DNS still hijacked. Keep going until the file is a real file again.
        """
        removed = 0
        for _ in range(10):
            if not self._resolv_mount_count():
                break
            if self._sudo([UMOUNT_BIN, RESOLV_CONF])[0] != 0:
                break
            removed += 1
        if removed:
            self.log("Unmounted %d %s overlay%s"
                     % (removed, RESOLV_CONF, "" if removed == 1 else "s"))
        if self._resolv_mount_count():
            self.log("WARNING: %s is still mounted — DNS will be wrong on other "
                     "networks. Try: sudo umount %s" % (RESOLV_CONF, RESOLV_CONF))
        return removed

    # ---------- helpers ----------

    def _tun2proxy_running(self):
        return self._run(["pgrep", "-x", TUN2PROXY_NAME])[0] == 0

    def _link_exists(self, dev):
        return self._run([IP_BIN, "link", "show", dev])[0] == 0

    def _resolv_mount_count(self):
        """Count overlays on resolv.conf, following the symlink.

        On Ubuntu /etc/resolv.conf is a symlink to
        /run/systemd/resolve/stub-resolv.conf, and mount(2) resolves symlinks:
        the bind mount lands on the RESOLVED path, so mountinfo never mentions
        /etc/resolv.conf at all. Checking only the literal path misses every
        stranded mount on a stock Ubuntu desktop. Verified empirically.
        """
        targets = {RESOLV_CONF, os.path.realpath(RESOLV_CONF)}
        try:
            with open("/proc/self/mountinfo") as fh:
                return sum(1 for line in fh
                           if any(" %s " % t in line for t in targets))
        except OSError:
            return 0

    def _read_rp_filter(self):
        out = self._run([SYSCTL_BIN, "-n", "net.ipv4.conf.all.rp_filter"])[1].strip()
        return out if out in ("0", "1", "2") else None

    def _current_ssid(self):
        out = self._run(["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi"])[1]
        for line in out.splitlines():
            if line.startswith("yes:"):
                return line[4:].strip()
        return None

    def _dns_works(self):
        try:
            socket.setdefaulttimeout(5)
            socket.gethostbyname("one.one.one.one")
            return True
        except OSError:
            return False
        finally:
            socket.setdefaulttimeout(None)

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
