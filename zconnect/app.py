"""Z Connect — native GTK tray client for PdaNet WiFi Direct tethering.

Runs as an AppIndicator (StatusNotifierItem), which GNOME renders with a real
themed menu at a real font size. The previous AWT SystemTray build could only
produce a legacy XEmbed icon and an unthemed X11 menu under Wayland.
"""

import json
import os
import signal
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
gi.require_version("Notify", "0.7")

from gi.repository import AppIndicator3, GLib, Gtk, Notify  # noqa: E402

from . import icons                                          # noqa: E402
from .connection import (                                    # noqa: E402
    DNS_STRATEGY, PDANET_GATEWAY, PROXY_PORT, ConnectionManager,
)
from .scanner import NetworkScanner                          # noqa: E402

APP_ID = "zconnect"
SEEK_INTERVAL = 5          # == ZLauncher PDANET_SEEK_INTERVAL_MS
LOG_LIMIT = 500

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "zconnect"
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

STATE_TEXT = {
    "idle": "No PdaNet network in range",
    "scanning": "Scanning for PdaNet…",
    "connecting": "Connecting…",
    "connected": "Connected",
    "error": "Connection failed",
}


class ZConnectApp:

    def __init__(self):
        self.scanner = NetworkScanner()
        self.manager = ConnectionManager(self.log)

        self.state = "scanning"
        self.available = []
        self.busy = False
        self.last_seen = None
        self.started_at = 0
        self.signal_pct = None
        self.current_ssid = None
        self.log_lines = []
        self.running = True
        self.setup_problems = []

        self.config = self._load_config()
        self.details = None
        self.log_buffer = None

        Notify.init("Z Connect")
        self._build_indicator()
        self._apply_state()

        threading.Thread(target=self._preflight, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()
        GLib.timeout_add_seconds(1, self._tick)

    # ---------- config ----------

    def _load_config(self):
        defaults = {"show_uptime": True, "notify": True, "prefer_pdanet": True}
        try:
            with open(CONFIG_FILE) as fh:
                defaults.update(json.load(fh))
        except (OSError, ValueError):
            pass
        return defaults

    def _save_config(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE, "w") as fh:
                json.dump(self.config, fh, indent=2)
        except OSError:
            pass

    # ---------- indicator ----------

    def _build_indicator(self):
        theme_path = icons.ensure_icons()

        self.indicator = AppIndicator3.Indicator.new(
            APP_ID, icons.icon_name("scanning"),
            AppIndicator3.IndicatorCategory.COMMUNICATIONS,
        )
        self.indicator.set_icon_theme_path(theme_path)
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Z Connect")

        menu = Gtk.Menu()

        # Top row is a real, clickable summary — not a greyed-out placeholder.
        self.item_status = Gtk.MenuItem(label="Scanning for PdaNet…")
        self.item_status.connect("activate", self.on_details)
        menu.append(self.item_status)

        menu.append(Gtk.SeparatorMenuItem())

        # Passive model: no "Connect" item. The tunnel follows the link you
        # are on. The only manual override is tearing it down.
        self.item_action = Gtk.MenuItem(label="Disconnect tunnel")
        self.item_action.connect("activate", self.on_action)
        menu.append(self.item_action)

        self.item_rescan = Gtk.MenuItem(label="Re-check now")
        self.item_rescan.connect("activate", self.on_rescan)
        menu.append(self.item_rescan)

        menu.append(Gtk.SeparatorMenuItem())

        self.item_uptime = Gtk.CheckMenuItem(label="Show uptime in panel")
        self.item_uptime.set_active(self.config["show_uptime"])
        self.item_uptime.connect("toggled", self.on_toggle_uptime)
        menu.append(self.item_uptime)

        self.item_repair = Gtk.MenuItem(label="Repair network")
        self.item_repair.connect("activate", self.on_repair)
        menu.append(self.item_repair)

        self.item_details = Gtk.MenuItem(label="Details and activity log…")
        self.item_details.connect("activate", self.on_details)
        menu.append(self.item_details)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit Z Connect")
        item_quit.connect("activate", self.on_quit)
        menu.append(item_quit)

        menu.show_all()
        self.indicator.set_menu(menu)
        self.indicator.set_secondary_activate_target(self.item_status)

    def _apply_state(self):
        """Push the current state into the icon, panel label and menu."""
        self.indicator.set_icon_full(icons.icon_name(self.state), STATE_TEXT[self.state])

        live = self._live()
        if self.setup_problems and self.state != "connected":
            self.item_status.set_label("Setup incomplete — see the log")
            self.item_action.set_sensitive(False)
            self.item_rescan.set_sensitive(not self.busy)
            self.indicator.set_icon_full(icons.icon_name("error"),
                                         "Z Connect setup incomplete")
            self.indicator.set_label("", "")
            self._update_details()
            return
        if self.state == "connected":
            summary = "Connected — %s" % (self.manager.network or "PdaNet")
        elif self.state in ("connecting",):
            summary = "Connecting to %s…" % (self.last_seen or "PdaNet")
        elif self.current_ssid:
            summary = "On %s — not PdaNet" % self.current_ssid
        else:
            summary = STATE_TEXT[self.state]
        self.item_status.set_label(summary)

        self.item_action.set_label("Disconnect tunnel")
        self.item_action.set_sensitive(not self.busy and self.state == "connected")
        self.item_rescan.set_sensitive(not self.busy)

        if self.config["show_uptime"] and self.state == "connected" and self.started_at:
            self.indicator.set_label(self._uptime(), "00:00:00")
        else:
            self.indicator.set_label("", "")

        self._update_details()

    def _live(self):
        """Networks actually broadcasting right now.

        A saved NetworkManager profile is not proof the phone is in range, and
        trying to join one that is out of range drops the WiFi we are already
        on. Auto-connect therefore only ever acts on these.
        """
        return [n for n in self.available if n.signal is not None]

    def _uptime(self):
        if not self.started_at:
            return "--:--:--"
        secs = int(time.time() - self.started_at)
        return "%02d:%02d:%02d" % (secs // 3600, (secs % 3600) // 60, secs % 60)

    def _set_state(self, state):
        self.state = state
        self._apply_state()

    # ---------- details window ----------

    def _build_details(self):
        win = Gtk.Window(title="Z Connect")
        win.set_default_size(620, 460)
        win.set_icon_from_file(icons.icon_path("connected"))
        win.connect("delete-event", lambda w, e: w.hide_on_delete())

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_border_width(16)
        win.add(outer)

        self.detail_heading = Gtk.Label(xalign=0)
        self.detail_heading.set_markup("<big><b>Z Connect</b></big>")
        outer.pack_start(self.detail_heading, False, False, 0)

        grid = Gtk.Grid(column_spacing=18, row_spacing=6)
        outer.pack_start(grid, False, False, 0)

        self.detail_fields = {}
        for row, (key, caption) in enumerate([
            ("state", "Status"),
            ("network", "Network"),
            ("signal", "Signal"),
            ("uptime", "Uptime"),
            ("iface", "WiFi interface"),
            ("proxy", "Proxy"),
            ("dns", "DNS"),
        ]):
            cap = Gtk.Label(xalign=0)
            cap.set_markup("<b>%s</b>" % caption)
            grid.attach(cap, 0, row, 1, 1)
            val = Gtk.Label(xalign=0, selectable=True)
            val.set_label("—")
            grid.attach(val, 1, row, 1, 1)
            self.detail_fields[key] = val

        log_caption = Gtk.Label(xalign=0)
        log_caption.set_markup("<b>Activity log</b>")
        outer.pack_start(log_caption, False, False, 0)

        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_buffer = view.get_buffer()
        self.log_view = view

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(view)
        outer.pack_start(scroller, True, True, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        self.detail_action = Gtk.Button(label="Connect now")
        self.detail_action.connect("clicked", self.on_action)
        buttons.pack_start(self.detail_action, False, False, 0)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda b: win.hide())
        buttons.pack_start(close, False, False, 0)
        outer.pack_start(buttons, False, False, 0)

        self.log_buffer.set_text("\n".join(self.log_lines))
        return win

    def _update_details(self):
        if self.details is None:
            return
        connected = self.state == "connected"
        self.detail_heading.set_markup(
            "<big><b>%s</b></big>" % GLib.markup_escape_text(STATE_TEXT[self.state])
        )
        fields = {
            "state": STATE_TEXT[self.state],
            "network": self.manager.network or (
                self.available[0].ssid if self.available else "—"
            ),
            "signal": "%s%%" % self.signal_pct if self.signal_pct is not None else "—",
            "uptime": self._uptime() if connected else "—",
            "iface": self.manager.wifi_interface or "—",
            "proxy": "http://%s:%d" % (PDANET_GATEWAY, PROXY_PORT) if connected else "—",
            "dns": "%s (via proxy)" % DNS_STRATEGY if connected else "—",
        }
        for key, value in fields.items():
            self.detail_fields[key].set_label(value)
        self.detail_action.set_label("Disconnect" if connected else "Connect now")
        self.detail_action.set_sensitive(
            not self.busy and (connected or bool(self.available))
        )

    # ---------- logging ----------

    def log(self, message):
        stamp = time.strftime("%H:%M:%S")
        line = "[%s] %s" % (stamp, message)
        print(line, flush=True)
        GLib.idle_add(self._append_log, line)

    def _append_log(self, line):
        self.log_lines.append(line)
        if len(self.log_lines) > LOG_LIMIT:
            del self.log_lines[:-LOG_LIMIT]
        if self.log_buffer is not None:
            self.log_buffer.set_text("\n".join(self.log_lines))
            end = self.log_buffer.get_end_iter()
            self.log_view.scroll_to_iter(end, 0.0, False, 0, 0)
        return False

    def notify(self, title, body):
        if not self.config["notify"]:
            return
        try:
            note = Notify.Notification.new(title, body, icons.icon_path(self.state))
            note.show()
        except GLib.Error:
            pass

    # ---------- menu handlers ----------

    def on_action(self, _widget):
        if self.busy:
            return
        if self.state == "connected":
            self._run_async(self._do_disconnect)

    def on_rescan(self, _widget):
        if self.busy:
            return
        self.log("Manual scan requested")
        self._run_async(self._do_rescan)

    def on_toggle_uptime(self, widget):
        self.config["show_uptime"] = widget.get_active()
        self._save_config()
        self._apply_state()

    def on_repair(self, _widget):
        """Sweep leftovers and report whether normal networking is back."""
        if self.busy:
            return
        self._run_async(self._do_repair)

    def on_details(self, _widget):
        if self.details is None:
            self.details = self._build_details()
        self._update_details()
        self.details.show_all()
        self.details.present()

    def on_quit(self, _widget):
        self.running = False
        if self.manager.connected:
            self.log("Quitting — disconnecting first...")
            self.manager.disconnect()
        Notify.uninit()
        Gtk.main_quit()

    # ---------- work ----------

    def _run_async(self, fn, *args):
        self.busy = True
        self._apply_state()

        def runner():
            try:
                fn(*args)
            finally:
                GLib.idle_add(self._clear_busy)

        threading.Thread(target=runner, daemon=True).start()

    def _clear_busy(self):
        self.busy = False
        self._apply_state()
        return False

    def _do_connect(self, ssid):
        self.last_seen = ssid
        GLib.idle_add(self._set_state, "connecting")
        ok = self.manager.start_tunnel(ssid)
        if ok:
            self.started_at = time.time()
            GLib.idle_add(self._set_state, "connected")
            GLib.idle_add(self.notify, "Z Connect", "Connected to %s" % ssid)
        else:
            self.started_at = 0
            self.last_seen = None
            GLib.idle_add(self._set_state, "error")
            GLib.idle_add(self.notify, "Z Connect", "Could not connect to %s" % ssid)

    def _do_disconnect(self):
        self.manager.disconnect()
        self.started_at = 0
        self.last_seen = None
        GLib.idle_add(self._set_state, "idle")
        GLib.idle_add(self.notify, "Z Connect", "Disconnected")

    def _do_repair(self):
        self.log("Repairing network...")
        swept = self.manager.sweep()
        problems = self.manager.verify_restored()
        if problems:
            for problem in problems:
                self.log("STILL BROKEN: %s" % problem)
            GLib.idle_add(self.notify, "Z Connect",
                          "Could not fully restore: %s" % problems[0])
        else:
            self.log("Network is healthy%s."
                     % (" (cleaned up %d item(s))" % len(swept) if swept else ""))
            GLib.idle_add(self.notify, "Z Connect",
                          "Network restored — you can join normal WiFi")
        GLib.idle_add(self._apply_state)

    def _do_rescan(self):
        self.scanner.rescan()
        time.sleep(1)
        self._poll()
        GLib.idle_add(self._apply_state)

    def _preflight(self):
        problems = self.manager.preflight()
        self.setup_problems = problems
        if problems:
            self.log("Setup is incomplete:")
            for problem in problems:
                self.log("  - %s" % problem)
            GLib.idle_add(self._apply_state)
            GLib.idle_add(self.notify, "Z Connect",
                          "Setup incomplete — run install.sh")
        else:
            self.log("Setup OK (tun2proxy, sudo, nmcli, /dev/net/tun)")
            if self.config.get("prefer_pdanet", True):
                self.manager.ensure_pdanet_preferred()
            swept = self.manager.sweep()
            if swept:
                GLib.idle_add(self.notify, "Z Connect",
                              "Cleaned up leftovers from a previous session")
        return False

    def _worker(self):
        while self.running:
            try:
                self._poll()
            except Exception as exc:                    # noqa: BLE001
                self.log("scan error: %s" % exc)
            time.sleep(SEEK_INTERVAL)

    def _poll(self):
        """ZLauncher seekAndConnectToPdaNet(), retooled for Linux.

        Passive: never joins or leaves WiFi. It reads whatever link is already
        up and enforces the tunnel state to match, every tick, regardless of
        what happened on previous ticks.
        """
        if self.busy or self.setup_problems:
            return

        # "if this device is the one providing WiFi to something else, it has
        # no business also trying to become a WiFi client of a different
        # network at the same time."
        if self.manager.hotspot_active():
            if self.manager.connected:
                self.log("This machine is serving WiFi — tearing down tunnel")
                self._run_async(self._do_disconnect)
            return

        on_pdanet, ssid = self.manager.on_pdanet()
        self.current_ssid = ssid
        self.available = self.scanner.scan()

        if on_pdanet:
            if not self.manager.is_connected():
                self.log("SSID '%s' matches PdaNet — constructing tunnel" % ssid)
                self._run_async(self._do_connect, ssid)
            else:
                self.signal_pct = self.scanner.signal_for(self.manager.network)
                GLib.idle_add(self._set_state, "connected")
        else:
            if self.manager.connected:
                self.log("SSID '%s' does not match PdaNet — destructing tunnel"
                         % (ssid or "none"))
                self._run_async(self._do_disconnect)
            else:
                # Off PdaNet and idle: nothing of ours should be installed. If
                # anything is, it is stranded from a crash, a forced kill or a
                # suspend, and it is silently breaking the real network. Sweep
                # it without being asked -- this is the tick that keeps "use
                # real internet otherwise" true with no intervention.
                if self.manager.leftovers_present():
                    self.log("Leftovers found while off PdaNet — cleaning up")
                    self._run_async(self._do_repair)
                    return
                GLib.idle_add(self._set_state, "idle")

    def _tick(self):
        """Keep the panel uptime label and the details window ticking."""
        if self.state == "connected" and self.started_at:
            if self.config["show_uptime"]:
                self.indicator.set_label(self._uptime(), "00:00:00")
            self._update_details()
        return self.running


def main():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise SystemExit("Z Connect needs a graphical session.")

    app = ZConnectApp()

    def stop(*_args):
        app.on_quit(None)

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)

    Gtk.main()
