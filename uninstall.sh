#!/bin/bash
#
# Removes everything install.sh put in place, and makes sure no tunnel or
# stranded /etc/resolv.conf overlay is left behind.
#
set -uo pipefail

LIB_DIR="/usr/local/lib/zconnect"
BIN_LINK="/usr/local/bin/zconnect"
TUN2PROXY_BIN="/usr/local/bin/tun2proxy-bin"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }

say "Z Connect uninstaller"

# 1. Stop the app, then tear down anything it installed. Order matters: the
#    resolv.conf overlay outlives the process and would break DNS on every
#    network afterwards.
pkill -f 'zconnect\.py' 2>/dev/null && ok "stopped the app" || ok "app was not running"
sleep 1

if pgrep -x tun2proxy-bin >/dev/null 2>&1; then
    sudo killall -TERM tun2proxy-bin 2>/dev/null
    for _ in $(seq 1 20); do pgrep -x tun2proxy-bin >/dev/null || break; sleep 0.5; done
    pgrep -x tun2proxy-bin >/dev/null && sudo killall -KILL tun2proxy-bin 2>/dev/null
    ok "stopped tun2proxy"
fi

ip link show tun0 >/dev/null 2>&1 && { sudo ip link delete tun0; ok "removed tun0"; }

REMOVED=0
while mountpoint -q /etc/resolv.conf 2>/dev/null; do
    sudo umount /etc/resolv.conf || break
    REMOVED=$((REMOVED+1))
done
[ "$REMOVED" -gt 0 ] && ok "unmounted $REMOVED /etc/resolv.conf overlay(s)"

# 2. Application files
sudo rm -rf "$LIB_DIR" && ok "removed $LIB_DIR"
sudo rm -f "$BIN_LINK" && ok "removed $BIN_LINK"
rm -f ~/.local/share/applications/zconnect.desktop ~/.config/autostart/zconnect.desktop
update-desktop-database ~/.local/share/applications/ 2>/dev/null || true
ok "removed menu and autostart entries"

rm -rf ~/.cache/zconnect && ok "removed generated icons"

# 3. System configuration
sudo rm -f /etc/sudoers.d/zconnect && ok "removed /etc/sudoers.d/zconnect"
sudo rm -f /etc/modules-load.d/zconnect.conf 2>/dev/null

say "Left in place on purpose"
echo "  $TUN2PROXY_BIN        (other tools may use it -- sudo rm -f $TUN2PROXY_BIN)"
echo "  ~/.config/zconnect/config.json  (your settings)"
echo "  NetworkManager profile priorities (nmcli connection modify <name> \\"
echo "      connection.autoconnect-priority 0)"

say "Done"
echo "  Verifying normal networking..."
if getent hosts one.one.one.one >/dev/null 2>&1; then
    ok "DNS resolves"
else
    printf '  \033[31m!\033[0m DNS is not resolving -- check: mountpoint /etc/resolv.conf\n'
fi
ip route show default | sed 's/^/  route: /'
echo
