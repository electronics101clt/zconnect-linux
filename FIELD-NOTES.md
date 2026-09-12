# Field notes — switching to PdaNet as primary internet

Written 2026-09-12, before the first real run against the phone.

## What should happen

1. Start PdaNet+ on the server phone, WiFi Direct mode.
2. NetworkManager joins it on its own — the app raised those profiles to
   `autoconnect-priority=100` at startup, so the phone wins over other saved
   networks that are in range.
3. Within ~5s the tray icon turns amber then **green**, and the panel shows an
   uptime counter.
4. Traffic and DNS go through `http://192.168.49.1:8000`.

Watch it happen: tray menu -> **Details and activity log…**

## If it does not connect

The log names the failing step. Most likely causes, in order:

- **"PdaNet proxy ... is not answering"** — the phone's proxy is not up, or
  PdaNet+ is in USB/Bluetooth mode rather than WiFi Direct. The app probes TCP
  `192.168.49.1:8000` directly; nothing else matters at that point.
- **"no address on the PdaNet subnet"** — associated but no DHCP lease yet.
  Usually resolves on the next tick.
- **Tunnel up but pages do not load** — DNS. Confirm the log shows
  `--dns over-tcp`, and that `/etc/resolv.conf` is bind-mounted while connected.

## Getting back to normal internet

Leaving the PdaNet network is enough. Within 5s the app sees the SSID no longer
matches, destroys the tunnel, and verifies routing and DNS are back.

If anything looks wrong, use the tray item **Repair network**. It sweeps
leftovers and reports. No terminal needed, no reboot.

### Why a reboot used to seem necessary

`tun2proxy --setup` bind-mounts over `/etc/resolv.conf`. A hard kill, a crash or
a suspend leaves that mount in place, so DNS on the *next* network points into a
dead tunnel. NetworkManager cannot see it. Reboot clears it because it is a
mount, not a file. They also stack, so one `umount` may not be enough.

The app now sweeps this at startup, before every connect, and on any idle tick
where it finds leftovers.

Note: on Ubuntu `/etc/resolv.conf` is a symlink, and mount(2) resolves symlinks,
so the mount actually appears on `/run/systemd/resolve/stub-resolv.conf`.
Detection follows the symlink — checking only the literal path finds nothing.

## Manual recovery (only if the app is not running)

```bash
sudo killall -TERM tun2proxy-bin        # TERM, so it restores routes and DNS
sudo ip link delete tun0 2>/dev/null
while mountpoint -q /etc/resolv.conf; do sudo umount /etc/resolv.conf; done
sudo sysctl -w net.ipv4.conf.all.rp_filter=2
```

`rp_filter` on this machine is **2** (loose), not the more common 1.

## First live run — verified working 2026-09-12

Against PdaNet+ on the server phone (SSID `DIRECT-xx-<device>-PdaNet`):

```
wlp3s0             192.168.49.x/24          joined by NetworkManager
tun0               10.0.0.33 peer 10.0.0.1  created by --setup
route to 1.1.1.1   dev tun0                 general traffic crosses the tunnel
route to 192.168.49.1  dev wlp3s0           bypass holds, no proxy loop
DNS example.com    172.66.147.243           DNS-over-TCP through the proxy
https://example.com  200
egress IP          <carrier IP>             the phone's carrier, not the local ISP
```

Note the tunnel address is **not** Android's `10.1.10.1/32` — `--setup`
chooses its own (`10.0.0.33 peer 10.0.0.1/24`) and points resolv.conf at
`10.0.0.1`, its own stub, which forwards to `--dns-addr` over TCP. It installs
`0.0.0.0/1` + `128.0.0.0/1` rather than replacing the default route, which is
the same "cover everything without touching the real default" approach as
Android's 16 `addRoute` calls. Nothing should hardcode a tunnel address.

Previously verified standalone: DNS-over-TCP through a stub HTTP CONNECT
proxy, the sweep recovering a deliberately stranded state, and the passive
trigger on real WiFi.

## Other machines

Everything lives in this repo. Copy it, run `./install.sh`, and the machine
configures itself — including the PdaNet profile priority. Nothing was
hand-wired outside the app.
