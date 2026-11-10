"""Make the README screenshots: python3 docs/screenshots.py [out dir, default docs/screenshots]

Runs RDP Desk with made-up demo connections in an invisible compositor (cage, headless), with libadwaita's
stock font and colours, and saves PNGs. Nothing is stored in the real keyring or connections folder.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "docs/screenshots").resolve()

if "RDPDESK_SHOTS" not in os.environ:  # parent: run this file again inside a hidden compositor
    data = tempfile.mkdtemp()
    config = tempfile.mkdtemp()  # no personal GTK theme / fonts: the stock look everyone gets
    os.makedirs(f"{config}/gtk-4.0")
    with open(f"{config}/gtk-4.0/settings.ini", "w") as ini:
        ini.write("[Settings]\ngtk-decoration-layout=appmenu:close\n")  # GNOME's default window buttons
    env = dict(os.environ, WLR_BACKENDS="headless", WLR_LIBINPUT_NO_DEVICES="1", WLR_RENDERER="pixman",
               GDK_BACKEND="wayland", XDG_DATA_HOME=data, XDG_CONFIG_HOME=config, GSETTINGS_BACKEND="memory", ADW_DISABLE_PORTAL="1", GDK_DEBUG="no-portals",
               ADW_DEBUG_COLOR_SCHEME="prefer-dark", RDPDESK_SHOTS=str(OUT))
    try:
        sys.exit(subprocess.call(["cage", "--", sys.executable, __file__], env=env, stderr=subprocess.DEVNULL))
    finally:
        shutil.rmtree(data)
        shutil.rmtree(config)

sys.path.insert(0, str(ROOT))
import rdpdesk as r  # noqa: E402

import gi  # noqa: E402

gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gio, GLib, Graphene, Gtk  # noqa: E402

DEMO = {
    "Office PC": {"full address": "office-pc.corp.example", "username": "alex", "domain": "CORP",
                  "rdpdesk folders": '["/home/alex/Documents", "/home/alex/Downloads"]', "redirectprinters": "1"},
    "Lab server": {"full address": "192.168.1.40:3390", "username": "administrator", "screen mode id": "2",
                   "use multimon": "1", "administrative session": "1"},
    "Build VM": {"full address": "buildvm.example.com", "username": "ci",
                 "gatewayusagemethod": "1", "gatewayhostname": "rdgateway.example.com"},
    "Design workstation": {"full address": "design-ws.local", "username": "alex", "desktopscalefactor": "150"},
    "Family laptop": {"full address": "192.168.1.23", "username": "family", "connection type": "4"},
}
r.DATA.mkdir(parents=True, exist_ok=True)
for name, values in DEMO.items():
    f = r.RdpFile(r.DATA / f"{name}.rdp")
    for key, value in values.items():
        f.set(key, "s" if key in ("full address", "username", "domain", "rdpdesk folders", "gatewayhostname") else "i", value)
    r.complete(f)
    f.save()

OUT.mkdir(parents=True, exist_ok=True)


def shot(win, name):
    w, h = win.get_width(), win.get_height()
    print(f"{name}: {w}x{h}")
    snap = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(win).snapshot(snap, w, h)
    texture = win.get_renderer().render_texture(snap.to_node(), Graphene.Rect().init(0, 0, w, h))
    texture.save_to_png(str(OUT / f"{name}.png"))
    print("saved", OUT / f"{name}.png")


def select(win, name):
    i = 0
    while row := win.list.get_row_at_index(i):
        if row.name == name:
            win.list.select_row(row)
            return row
        i += 1


def steps(app):
    win = app.props.active_window
    office = select(win, "Office PC")
    fake = subprocess.Popen(["sleep", "60"])  # shows Office PC as connected
    fake.stopped = True
    win.sessions[str(office.path)] = fake
    r.set_quietly(win.password_row, "not-a-real-password")
    win.mark_running()
    yield
    shot(win, "main")
    for tab in ("display", "experience", "advanced", "programs"):
        win.tabs.set_visible_child_name(tab)
        yield
        shot(win, tab)
    win.tabs.set_visible_child_name("local")  # the longest tab: scrolled down to the shared folders
    yield
    page = win.tabs.get_child_by_name("local")
    scroller = page.get_first_child()
    while scroller and not isinstance(scroller, Gtk.ScrolledWindow):
        scroller = scroller.get_first_child()
    adj = scroller.get_vadjustment()
    adj.set_value(adj.get_upper() - adj.get_page_size())
    yield
    shot(win, "local")
    win.tabs.set_visible_child_name("general")
    select(win, "Lab server")
    win.ask_credentials(win.rdp, remember=True)
    yield
    yield  # the dialog fades in
    shot(win, "sign-in")
    win.get_visible_dialog().force_close()
    win.list.select_row(office)
    r.set_quietly(win.password_row, "not-a-real-password")
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    yield
    shot(win, "light")
    fake.kill()
    app.quit()


def run(app):
    it = steps(app)

    def tick():
        try:
            next(it)
            return True
        except StopIteration:
            return False
    GLib.timeout_add(900, tick)


app = r.App()
app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)  # never hand over to a running RDP Desk
app.connect("activate", lambda a: GLib.idle_add(run, a))
app.run([])
