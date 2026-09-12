#!/bin/bash
#
# Z Connect installer.
#
# Assumes nothing is installed. Puts every dependency in place so the app works
# on first run, and pins tun2proxy to the same version the Android client
# (pdanet-vpn-client) ships, so both ends behave identically.
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
TUN2PROXY_VERSION="v0.7.19"
TUN2PROXY_REPO="tun2proxy/tun2proxy"
INSTALL_BIN="/usr/local/bin/tun2proxy-bin"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mERROR:\033[0m %s\n\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Run this as your normal user, not as root (it calls sudo itself)."

say "Z Connect installer"
echo "  Target: $APP_DIR"
echo "  User:   $USER"

# ---------------------------------------------------------------- apt packages
say "1/6  System packages"

APT_PKGS=()
need_pkg() {
    dpkg -s "$1" >/dev/null 2>&1 || APT_PKGS+=("$1")
}

need_pkg python3-gi                   # GTK bindings
need_pkg python3-gi-cairo             # icon rendering
need_pkg gir1.2-gtk-3.0               # GTK3 typelib
need_pkg network-manager              # nmcli
need_pkg curl
need_pkg unzip
need_pkg iproute2
need_pkg psmisc                       # killall
need_pkg libnotify-bin                # desktop notifications

# The tray needs an AppIndicator typelib. Ubuntu ships two flavours; either
# works, so only install one if neither is present.
if ! python3 -c "
import gi
try:
    gi.require_version('AppIndicator3','0.1')
except ValueError:
    gi.require_version('AyatanaAppIndicator3','0.1')
" >/dev/null 2>&1; then
    if apt-cache show gir1.2-appindicator3-0.1 >/dev/null 2>&1; then
        APT_PKGS+=(gir1.2-appindicator3-0.1)
    else
        APT_PKGS+=(gir1.2-ayatanaappindicator3-0.1)
    fi
fi

if [ ${#APT_PKGS[@]} -gt 0 ]; then
    echo "  Installing: ${APT_PKGS[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y "${APT_PKGS[@]}"
else
    ok "all present"
fi

python3 -c "
import gi
gi.require_version('Gtk','3.0')
try:
    gi.require_version('AppIndicator3','0.1')
    from gi.repository import AppIndicator3
except ValueError:
    gi.require_version('AyatanaAppIndicator3','0.1')
    from gi.repository import AyatanaAppIndicator3
import cairo
" || die "GTK/AppIndicator bindings still unavailable after install."
ok "GTK + AppIndicator bindings"

# ------------------------------------------------------------------- tun kernel
say "2/6  TUN device"
if [ ! -e /dev/net/tun ]; then
    sudo modprobe tun || die "Could not load the 'tun' kernel module."
fi
[ -e /dev/net/tun ] || die "/dev/net/tun still missing."
ok "/dev/net/tun present"

# --------------------------------------------------------------------- tun2proxy
say "3/6  tun2proxy $TUN2PROXY_VERSION"
echo "  Same build as the Android client's libtun2proxy.so, so DNS-over-TCP"
echo "  through the HTTP CONNECT proxy behaves identically on both ends."

install_tun2proxy=1
if [ -x "$INSTALL_BIN" ]; then
    have="$("$INSTALL_BIN" --version 2>/dev/null | awk '{print $2}')"
    if [ "v${have:-0}" = "$TUN2PROXY_VERSION" ]; then
        ok "already at $TUN2PROXY_VERSION"
        install_tun2proxy=0
    else
        warn "found $have, replacing with $TUN2PROXY_VERSION"
    fi
fi

if [ "$install_tun2proxy" -eq 1 ]; then
    case "$(uname -m)" in
        x86_64)         ASSET="tun2proxy-x86_64-unknown-linux-gnu.zip" ;;
        aarch64|arm64)  ASSET="tun2proxy-aarch64-unknown-linux-gnu.zip" ;;
        armv7l)         ASSET="tun2proxy-armv7-unknown-linux-musleabihf.zip" ;;
        i686|i386)      ASSET="tun2proxy-i686-unknown-linux-musl.zip" ;;
        *) die "No tun2proxy build for architecture $(uname -m)." ;;
    esac

    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    URL="https://github.com/$TUN2PROXY_REPO/releases/download/$TUN2PROXY_VERSION/$ASSET"
    echo "  Downloading $ASSET"
    curl -fsSL "$URL" -o "$TMP/t2p.zip" || die "Download failed: $URL"
    unzip -o -q "$TMP/t2p.zip" -d "$TMP"
    [ -f "$TMP/tun2proxy-bin" ] || die "Archive did not contain tun2proxy-bin."
    sudo install -m 0755 "$TMP/tun2proxy-bin" "$INSTALL_BIN"
    ok "installed $("$INSTALL_BIN" --version 2>/dev/null | head -1)"
fi

# ----------------------------------------------------------------------- sudoers
say "4/6  Passwordless sudo"
echo "  tun2proxy --setup needs root to create tun0, install routes, and"
echo "  overlay /etc/resolv.conf. Without this the app would silently stall"
echo "  on a password prompt it cannot display."

TMP_SUDO="$(mktemp)"
sed "s|__USER__|$USER|g" "$APP_DIR/zconnect-sudoers" > "$TMP_SUDO"
if sudo visudo -c -f "$TMP_SUDO" >/dev/null 2>&1; then
    sudo install -m 0440 -o root -g root "$TMP_SUDO" /etc/sudoers.d/zconnect
    ok "/etc/sudoers.d/zconnect"
else
    rm -f "$TMP_SUDO"
    die "Generated sudoers file failed validation; refusing to install it."
fi
rm -f "$TMP_SUDO"

sudo -n "$INSTALL_BIN" --version >/dev/null 2>&1 \
    && ok "verified: sudo tun2proxy-bin runs without a password" \
    || warn "sudo still prompts — check /etc/sudoers.d/zconnect"

# -------------------------------------------------------------------- desktop
say "5/6  Application entry"
python3 -c "
import sys; sys.path.insert(0, '$APP_DIR')
from zconnect import icons
print('  Icons:', icons.ensure_icons())
"
chmod +x "$APP_DIR/zconnect.sh" "$APP_DIR/zconnect.py"

mkdir -p ~/.local/share/applications ~/.config/autostart
sed "s|^Exec=.*|Exec=$APP_DIR/zconnect.sh|" "$APP_DIR/zconnect.desktop" \
    > ~/.local/share/applications/zconnect.desktop
cp ~/.local/share/applications/zconnect.desktop ~/.config/autostart/zconnect.desktop
update-desktop-database ~/.local/share/applications/ 2>/dev/null || true
ok "menu entry + autostart"

# ------------------------------------------------------------------- gnome ext
say "6/6  Tray support"
if command -v gnome-extensions >/dev/null 2>&1; then
    EXT=""
    for candidate in ubuntu-appindicators@ubuntu.com appindicatorsupport@rgcjonas.gmail.com; do
        if gnome-extensions list 2>/dev/null | grep -qx "$candidate"; then
            EXT="$candidate"; break
        fi
    done
    if [ -n "$EXT" ]; then
        if gnome-extensions list --enabled 2>/dev/null | grep -qx "$EXT"; then
            ok "$EXT already enabled"
        else
            gnome-extensions enable "$EXT" 2>/dev/null \
                && ok "enabled $EXT" \
                || warn "could not enable $EXT — enable it in the Extensions app"
        fi
    else
        warn "No AppIndicator GNOME extension found."
        echo "     Install one so the tray icon appears:"
        echo "       sudo apt install gnome-shell-extension-appindicator"
        echo "     then log out and back in."
    fi
else
    ok "not GNOME — StatusNotifierItem is handled by your panel"
fi

say "Done"
cat <<EOF
  Start now:   $APP_DIR/zconnect.sh
  Or search "Z Connect" in your applications. It also starts at login.

  On connect the app runs:
    sudo tun2proxy-bin --setup --tun tun0 \\
         --proxy http://192.168.49.1:8000 \\
         --dns over-tcp --dns-addr 8.8.8.8 \\
         --bypass 192.168.0.0/16

  That is the same engine, proxy protocol and DNS strategy the Android
  client uses, so behaviour should match what is already proven there.

EOF
