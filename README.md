# Z Connect Linux

Native Linux tray client for connecting to PdaNet WiFi Direct hotspots.

## Overview

A GTK3 tray application (AppIndicator / StatusNotifierItem) that finds the
`DIRECT-*PdaNet*` hotspot published by an Android phone running Z Connect Host,
joins it, and bridges the machine's traffic through the phone's HTTP CONNECT
proxy.

It runs in the panel only — there is no main window. Clicking the indicator
gives a normal GTK menu; "Details and activity log…" opens a window with the
live connection state and the full log.

## Same engine as the Android client

This app does not reimplement the Android client — it runs **the same engine**
with the same settings, so behaviour matches what is already proven there.

| | Android (`pdanet-vpn-client`) | Linux (this app) |
|---|---|---|
| Engine | `libtun2proxy.so` (JNI) | `tun2proxy-bin` (CLI) |
| Version | 0.7.19 | 0.7.19 (pinned by `install.sh`) |
| Tunnel setup | `VpnService.Builder` | `tun2proxy --setup` |
| Address | `10.1.10.1/32` | same, created by `--setup` |
| Routes | 16 × `addRoute`, skipping `192.168.0.0/16` | `--bypass 192.168.0.0/16` |
| Proxy | `http://192.168.49.1:8000` | same |
| DNS | `addDnsServer` + `DnsStrategy.OVER_TCP` | `--dns over-tcp --dns-addr 8.8.8.8` |
| MTU | 1500 | 1500 (Linux tun default) |

On connect the app runs:

```bash
sudo tun2proxy-bin --setup --tun tun0 \
     --proxy http://192.168.49.1:8000 \
     --dns over-tcp --dns-addr 8.8.8.8 \
     --bypass 192.168.0.0/16 \
     --exit-on-fatal-error -v info
```

### Why DNS is the part that matters

An HTTP CONNECT proxy is **TCP-only by definition** (RFC 9110 §9.3.6), so UDP
DNS cannot traverse it. Android never hits this because tun2proxy rewrites DNS
queries as TCP under `OVER_TCP`.

An earlier Linux build used `xjasonlyu/tun2socks`, which has no DNS strategy at
all — `tun2socks --help` offers `-mtu`, `-udp-timeout` and so on, but nothing
for DNS. It had copied Android's 16-entry routing table without the mechanism
that makes DNS work through it, so the tunnel would come up and name resolution
would silently fail. Using the same engine as Android removes that whole class
of divergence.

### What `--setup` does that VpnService did

There is no `VpnService` on Linux, so `--setup` performs the equivalent: it
creates the tun device, installs the routes, and **bind-mounts
`/etc/resolv.conf`** so DNS enters the tunnel. The mount is undone when the
process exits, which is why the app stops it with `SIGTERM` rather than
`SIGKILL` (and unmounts as a safety net if a forced kill was ever needed).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                 Z Connect  (zconnect.py)                     │
│                   GTK3 + AppIndicator3                       │
├─────────────────────────────────────────────────────────────┤
│  app.py         │  scanner.py        │  connection.py        │
│  - tray + menu  │  - finds DIRECT-   │  - joins WiFi (nmcli) │
│  - details win  │    *PdaNet* via    │  - runs tun2proxy     │
│  - state machine│    nmcli           │    --setup            │
│  - icons.py     │  - live vs saved   │  - preflight checks   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                 tun2proxy 0.7.19  (--setup)                  │
│   tun0 ↔ HTTP CONNECT proxy   ·   DNS rewritten to TCP       │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│              Android Phone (Z Connect Host)                  │
│  WiFi Direct: DIRECT-xx-device-PdaNet                        │
│  HTTP Proxy:  192.168.49.1:8000                              │
│  Network:     192.168.49.0/24                                │
└─────────────────────────────────────────────────────────────┘
```

## Protocol Details

| Component | Value |
|-----------|-------|
| WiFi Direct SSID | `DIRECT-xx-[device]-PdaNet` |
| Network Subnet | `192.168.49.0/24` |
| Phone/Gateway IP | `192.168.49.1` |
| Proxy Type | HTTP CONNECT (not SOCKS5) |
| Proxy Port | `8000` |
| TUN Interface | `tun0` at `10.1.10.1/32` |
| Bypass | `192.168.0.0/16` (the proxy lives there — routing it in would loop) |

## Tray states

The icon colour is the status — no hovering required.

| Colour | Meaning |
|--------|---------|
| 🟢 green | Connected (panel shows uptime) |
| 🟠 amber | Connecting |
| 🔵 blue | PdaNet hotspot in range, not connected |
| ⚫ grey | No PdaNet hotspot in range |
| 🔴 red | Connection failed, or setup incomplete |

## Installation

```bash
./install.sh
```

Assumes nothing is installed. It puts everything in place:

1. apt packages — `python3-gi`, `python3-gi-cairo`, an AppIndicator typelib,
   `network-manager`, `curl`, `unzip`, `iproute2`, `psmisc`, `libnotify-bin`
2. loads the `tun` kernel module and checks `/dev/net/tun`
3. downloads **tun2proxy v0.7.19** for your architecture to
   `/usr/local/bin/tun2proxy-bin`
4. installs `/etc/sudoers.d/zconnect` (validated with `visudo -c` first) and
   verifies `sudo` no longer prompts
5. renders the panel icons, installs the menu entry and autostart entry
6. enables the AppIndicator GNOME extension if one is present

The app also runs its own preflight check at startup and shows
"Setup incomplete — see the log" in the menu if anything is missing.

## Usage

Launch "Z Connect" from the Activities menu, or run `./zconnect.sh`. It starts
at login once installed.

- **Connect automatically** (on by default) joins a PdaNet hotspot as soon as
  one comes in range.
- **Connect now** joins the strongest hotspot, and will also target a *saved*
  profile that is not currently broadcasting.
- **Quit Z Connect** tears the tunnel down cleanly before exiting.

Settings live in `~/.config/zconnect/config.json`.

### Auto-connect only acts on hotspots in range

A saved NetworkManager profile is not proof the phone is nearby. Auto-connect
only fires on networks returned by a live scan, because trying to join an
out-of-range profile drops the WiFi you are already on for a connect attempt
that cannot succeed. Manual "Connect now" can still target a saved profile.

### sudo

Privileged steps invoke the target binary directly (`sudo -n /usr/sbin/ip ...`),
never through `sudo bash -c`. `zconnect-sudoers` grants NOPASSWD per binary, and
a wrapping shell is not covered by those rules — going through one would make
the app prompt for a password it has no terminal to read.

## Files

```
zconnect-app/
├── zconnect.py              # entry point
├── zconnect/
│   ├── app.py               # tray, menu, details window, state machine
│   ├── scanner.py           # nmcli network discovery
│   ├── connection.py        # WiFi + tun2proxy lifecycle
│   └── icons.py             # panel icons, rendered at runtime with cairo
├── zconnect.sh              # launch script
├── zconnect.desktop         # desktop / autostart entry
├── zconnect-sudoers         # NOPASSWD rules (__USER__ substituted on install)
└── install.sh               # installer
```

Icons are generated into `~/.cache/zconnect/icons/` on first run.

## Troubleshooting

### No tray icon
GNOME needs the AppIndicator extension to show StatusNotifierItems:
```bash
gnome-extensions enable ubuntu-appindicators@ubuntu.com
```
Confirm the app registered:
```bash
gdbus call --session --dest org.kde.StatusNotifierWatcher \
  --object-path /StatusNotifierWatcher \
  --method org.freedesktop.DBus.Properties.Get \
  org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems
```
`zconnect` should appear in the list.

### "Setup incomplete"
Open "Details and activity log…" — the log names the missing piece. Re-running
`./install.sh` fixes all of them.

### App won't launch
```bash
cd ~/zconnect-app
python3 zconnect.py        # run in a terminal to see the error
```

### Can't find PdaNet networks
```bash
nmcli device wifi rescan
nmcli device wifi list | grep -i pdanet
```

### Connection fails
```bash
sudo -n /usr/local/bin/tun2proxy-bin --version   # must not prompt
ls -l /dev/net/tun
```

### Pages don't load but the tunnel is up
That is the DNS symptom. Check the log for `--dns over-tcp` in the tun2proxy
command line, and that `/etc/resolv.conf` is bind-mounted while connected:
```bash
grep " /etc/resolv.conf " /proc/self/mountinfo
```

### Manual cleanup
```bash
sudo killall -TERM tun2proxy-bin      # TERM, so it restores routes and DNS
sudo ip link delete tun0 2>/dev/null
sudo umount /etc/resolv.conf 2>/dev/null
sudo sysctl -w net.ipv4.conf.all.rp_filter=1
nmcli connection down "DIRECT-xx-device-PdaNet"
```

## History

- **v2.1.0** — switched from `xjasonlyu/tun2socks` to `tun2proxy 0.7.19`, the
  same engine and version the Android client uses, fixing DNS through the
  HTTP CONNECT proxy.
- **v2.0.0** — replaced the Java/JavaFX/Gradle implementation with this GTK one.

This repository starts at v2.1.0. The earlier Java implementation and the
commits that worked through the DNS problem are not in this history; they are
archived in `zconnect-app-oldhistory.bundle` alongside the project directory
(`git clone zconnect-app-oldhistory.bundle old-zconnect` to read it).
