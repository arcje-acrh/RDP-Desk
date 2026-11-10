#!/usr/bin/env python3
"""RDP Desk: keep, edit and open Remote Desktop (RDP) connections.

GTK 4 + libadwaita window. FreeRDP 3 makes the connections. Passwords live in the
desktop keyring (Secret Service: GNOME Keyring, KWallet, KeePassXC, ...).
Each connection is a plain .rdp file, the format Windows uses, so files move
freely between this app, Windows and FreeRDP.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

VERSION = "0.1.0"
APP_ID = "io.github.rdpdesk"
DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "rdpdesk"
FOLDERS_KEY = "rdpdesk folders"  # app-only key (JSON list of paths); FreeRDP and Windows skip unknown keys
# FreeRDP 3 clients, best first: the SDL one can ask about certificates and sizes itself to the window.
CLIENTS = ("sdl-freerdp3", "sdl-freerdp", "xfreerdp3", "wlfreerdp3", "xfreerdp", "wlfreerdp")
# ponytail: SDL full screen fails on Wayland (FreeRDP 3.32 + SDL 3.4: "virtual desktop width 80"),
# so full screen takes the X11 client first; drop this when SDL full screen works again.
FULLSCREEN_CLIENTS = ("xfreerdp3", "xfreerdp") + CLIENTS


# ---------------------------------------------------------------- .rdp files

LINE = re.compile(r"^([^:]+):([sib]):(.*)$")


class RdpFile:
    """An .rdp file: ordered `key:type:value` lines. Lines it does not know stay as they are.
    Keeps the file's encoding (Windows writes UTF-16, others UTF-8)."""

    def __init__(self, path):
        self.path = Path(path)
        raw = self.path.read_bytes() if self.path.exists() else b""
        self.utf16 = raw[:2] in (b"\xff\xfe", b"\xfe\xff")
        text = raw.decode("utf-16" if self.utf16 else "utf-8-sig", errors="replace")
        self.lines = []  # [key, type, value] or a raw string
        for line in text.splitlines():
            m = LINE.match(line)
            self.lines.append([m[1].strip(), m[2], m[3].strip()] if m else line)

    def _find(self, key):
        key = key.lower()
        return next((l for l in self.lines if isinstance(l, list) and l[0].lower() == key), None)

    def get(self, key, default=None):
        line = self._find(key)
        return line[2] if line else default

    def set(self, key, typ, value):
        line = self._find(key)
        if line:
            line[1], line[2] = typ, str(value)
        else:
            self.lines.append([key, typ, str(value)])

    def save(self, path=None):
        path = Path(path or self.path)
        text = "".join((f"{l[0]}:{l[1]}:{l[2]}" if isinstance(l, list) else l) + "\r\n" for l in self.lines)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(text.encode("utf-16") if self.utf16 else text.encode())
        os.replace(tmp, path)

    def folders(self):
        try:
            return [p for p in json.loads(self.get(FOLDERS_KEY, "[]")) if isinstance(p, str)]
        except ValueError:
            return []

    def set_folders(self, paths):
        self.set(FOLDERS_KEY, "s", json.dumps(paths))


# ---------------------------------------------------------------- settings, as the Windows client groups them

def R(key, title, kind="switch", default="0", choices=None, lo=0, hi=0, subtitle="", typ=None):
    """One setting. kind: text | switch | inv (switch, on = "0") | star (switch, on = "*") | combo | spin."""
    typ = typ or ("s" if kind in ("text", "star") else "i")
    return dict(key=key, title=title, kind=kind, default=default, choices=choices, lo=lo, hi=hi, subtitle=subtitle, typ=typ)


TABS = [
    ("general", "General", "computer-symbolic", [
        ("Connection", "", ["NAME", R("full address", "Computer (name or IP, optional :port)", "text", "")]),
        ("Sign in", "", [
            R("username", "User name", "text", ""),
            R("domain", "Domain", "text", ""),
            "PASSWORD",
            R("prompt for credentials", "Always ask for credentials", subtitle="Never use the saved password"),
        ]),
    ]),
    ("display", "Display", "video-display-symbolic", [
        ("Display configuration", "", [
            R("screen mode id", "Show the remote desktop", "combo", "1", [("1", "In a window"), ("2", "Full screen")]),
            R("desktopwidth", "Width", "spin", "1600", lo=200, hi=8192),
            R("desktopheight", "Height", "spin", "900", lo=200, hi=8192),
            R("dynamic resolution", "Resize the remote desktop with the window", default="1"),
            R("smart sizing", "Scale the remote desktop to fit the window"),
            R("desktopscalefactor", "Scaling on the remote computer", "combo", "100",
              [(str(p), f"{p} %") for p in (100, 125, 150, 175, 200, 250, 300)]),
        ]),
        ("Monitors", "", [
            R("use multimon", "Use all my monitors for the remote session"),
            R("span monitors", "Span one desktop across all monitors"),
            R("selectedmonitors", "Only these monitors (IDs, e.g. 0,1)", "text", ""),
        ]),
        ("Colours", "", [
            R("session bpp", "Colour depth", "combo", "32", [
                ("15", "High Colour (15 bit)"), ("16", "High Colour (16 bit)"),
                ("24", "True Colour (24 bit)"), ("32", "Highest Quality (32 bit)")]),
        ]),
        ("Full screen", "", [
            R("displayconnectionbar", "Show the connection bar in full screen", default="1"),
            R("pinconnectionbar", "Pin the connection bar", default="1"),
        ]),
    ]),
    ("local", "Local Resources", "audio-speakers-symbolic", [
        ("Remote audio", "", [
            R("audiomode", "Remote audio playback", "combo", "0", [
                ("0", "Play on this computer"), ("1", "Play on the remote computer"), ("2", "Do not play")]),
            R("audiocapturemode", "Record from this computer", subtitle="Microphone"),
            R("audioqualitymode", "Audio quality", "combo", "0", [("0", "Dynamic"), ("1", "Medium"), ("2", "High")]),
        ]),
        ("Keyboard", "", [
            R("keyboardhook", "Apply Windows key combinations", "combo", "2", [
                ("0", "On this computer"), ("1", "On the remote computer"), ("2", "Only in full screen")]),
        ]),
        ("Local devices and resources", "", [
            R("redirectclipboard", "Clipboard", default="1"),
            R("redirectprinters", "Printers"),
            R("redirectsmartcards", "Smart cards"),
            R("redirectcomports", "Serial ports"),
            R("drivestoredirect", "All local drives", "star", ""),
            R("usbdevicestoredirect", "USB devices", "star", ""),
            R("camerastoredirect", "Cameras", "star", ""),
            R("redirectwebauthn", "Security keys (WebAuthn)"),
            R("redirectlocation", "Location"),
        ]),
        "FOLDERS",
    ]),
    ("programs", "Programs", "system-run-symbolic", [
        ("Start a program on connection", "Leave empty for the normal desktop.", [
            R("alternate shell", "Program path and file name", "text", ""),
            R("shell working directory", "Start in this folder", "text", ""),
        ]),
        ("RemoteApp", "Open one program window instead of the whole desktop.", [
            R("remoteapplicationmode", "Run as a RemoteApp"),
            R("remoteapplicationprogram", "Program (alias, e.g. ||notepad)", "text", ""),
            R("remoteapplicationname", "Program name", "text", ""),
            R("remoteapplicationcmdline", "Command-line arguments", "text", ""),
        ]),
    ]),
    ("experience", "Experience", "preferences-desktop-appearance-symbolic", [
        ("Performance", "", [
            R("connection type", "Connection speed", "combo", "7", [
                ("1", "Modem (56 Kbps)"), ("2", "Low-speed broadband (256 Kbps – 2 Mbps)"),
                ("3", "Satellite (2 – 16 Mbps, high latency)"), ("4", "High-speed broadband (2 – 10 Mbps)"),
                ("5", "WAN (10 Mbps or more, high latency)"), ("6", "LAN (10 Mbps or more)"),
                ("7", "Detect connection quality automatically")]),
        ]),
        ("Allow the following", "", [
            R("disable wallpaper", "Desktop background", "inv"),
            R("allow font smoothing", "Font smoothing", default="1"),
            R("allow desktop composition", "Desktop composition", default="1"),
            R("disable full window drag", "Show window contents while dragging", "inv"),
            R("disable menu anims", "Menu and window animation", "inv"),
            R("disable themes", "Visual styles", "inv"),
            R("bitmapcachepersistenable", "Persistent bitmap caching", default="1"),
        ]),
        ("Reconnecting", "", [
            R("autoreconnection enabled", "Reconnect if the connection drops", default="1"),
            R("autoreconnect max retries", "Attempts", "spin", "20", lo=1, hi=1000),
        ]),
    ]),
    ("advanced", "Advanced", "preferences-system-symbolic", [
        ("Server authentication", "", [
            R("authentication level", "If server authentication fails", "combo", "2", [
                ("0", "Connect and don't warn me"), ("2", "Warn me"), ("1", "Do not connect")]),
            R("enablecredsspsupport", "Network Level Authentication", default="1"),
            R("negotiate security layer", "Negotiate the security layer", default="1"),
            R("administrative session", "Connect to the console (admin) session"),
        ]),
        ("Connect from anywhere", "An RD Gateway reaches computers behind a company firewall.", [
            R("gatewayusagemethod", "RD Gateway", "combo", "0", [
                ("0", "Do not use an RD Gateway"), ("1", "Always use this RD Gateway"),
                ("2", "Use it if a direct connection fails"), ("4", "Use it, but not for local addresses")]),
            R("gatewayhostname", "RD Gateway server", "text", ""),
            R("gatewaycredentialssource", "Gateway sign-in method", "combo", "4", [
                ("0", "Ask for password (NTLM)"), ("1", "Smart card"), ("4", "Let me choose later")]),
            R("promptcredentialonce", "Use my gateway credentials for the remote computer", default="1"),
        ]),
    ]),
]
HELP = {
    # General
    "NAME": "What this connection is called in the list. It is also its file name (Name.rdp) in ~/.local/share/rdpdesk, so Export hands out a file with this name.",
    "full address": "The computer to connect to: its name (office-pc, pc.example.com) or IP address (192.168.1.20). Add :port only if it does not use the standard port 3389, e.g. office-pc:3390. With a port, write IPv6 addresses in brackets: [2001:db8::1]:3390.",
    "username": "The Windows account to sign in with. Work (domain) account: DOMAIN\\name or name@company.com. Microsoft account: its email address. Local account on that PC: just the name.",
    "domain": "The Windows domain the account belongs to, if you did not write it into the user name. Leave it empty for home PCs and Microsoft accounts.",
    "PASSWORD": "Kept in your keyring, never in the .rdp file, and used when you connect. Type it and press Enter (or the tick) to save; empty the field and press Enter to forget it. With no saved password you are asked when connecting.",
    "prompt for credentials": "Ignore the saved password and ask for the user name and password every time you connect. For shared computers, or accounts you do not want stored.",
    # Display
    "screen mode id": "In a window: the remote desktop opens in a normal window you can move and tile. Full screen: it covers the whole monitor; Ctrl+Alt+Enter switches between the two while connected.",
    "desktopwidth": "Width of the remote desktop in pixels when it opens in a window. With 'Resize the remote desktop with the window' on, this is only the size it starts at.",
    "desktopheight": "Height of the remote desktop in pixels when it opens in a window. With 'Resize the remote desktop with the window' on, this is only the size it starts at.",
    "dynamic resolution": "When you resize the window, the remote computer changes its resolution to match, so nothing is stretched or cut off. Needs Windows 8.1 / Server 2012 R2 or newer.",
    "smart sizing": "Shrink or stretch the remote picture to fit the window instead of changing the remote resolution. Text can look soft; meant for old computers that cannot resize.",
    "desktopscalefactor": "How big text and icons are drawn on the remote computer, like Settings > Display > Scale in Windows. On a sharp laptop screen 125–150 % keeps things readable.",
    "use multimon": "Use every monitor of this computer for the session; each becomes a separate monitor on the remote computer. Works in full screen.",
    "span monitors": "Join all monitors into one wide remote screen instead of separate monitors. For remote computers that cannot handle several monitors.",
    "selectedmonitors": "With 'Use all my monitors' on: use only these monitors, by number, counting from 0. Leave empty to use all of them.",
    "session bpp": "How many colours the remote picture uses. 32 bit looks best; fewer colours send less data on very slow connections.",
    "displayconnectionbar": "In full screen, a small bar at the top with the computer's name and buttons to minimise, leave full screen or disconnect. Not every FreeRDP client draws it.",
    "pinconnectionbar": "Keep the connection bar on screen instead of letting it slide away until the mouse touches the top edge.",
    # Local Resources
    "audiomode": "Where sound from the remote computer plays: here on your speakers, on the remote computer itself, or nowhere.",
    "audiocapturemode": "Let the remote computer use this computer's microphone, e.g. for Teams or Zoom calls inside the remote desktop.",
    "audioqualitymode": "Dynamic adjusts the sound quality to the connection speed. High always uses the best quality (more data). Medium is compressed, in between.",
    "keyboardhook": "Where shortcuts like Alt+Tab, the Windows key and Ctrl+Esc go: to this computer, to the remote computer, or to the remote computer only while it is full screen.",
    "redirectclipboard": "Copy and paste text, pictures and files between this computer and the remote one.",
    "redirectprinters": "Your printers here appear on the remote computer, so what you print there comes out here.",
    "redirectsmartcards": "Use a smart card reader on this computer to sign in or sign documents on the remote one.",
    "redirectcomports": "Give the remote computer this computer's serial (COM) ports, for older devices such as scales, tills or modems.",
    "drivestoredirect": "Show all of this computer's drives, and USB sticks plugged in later, on the remote computer under 'This PC'. To share only some folders, use Shared folders below instead.",
    "usbdevicestoredirect": "Pass USB devices plugged into this computer straight through to the remote one, which then uses its own drivers for them. The remote computer must allow USB redirection.",
    "camerastoredirect": "Let the remote computer use this computer's webcams, e.g. for video calls. Needs Windows 11 / Server 2022 or newer on the remote side.",
    "redirectwebauthn": "Use security keys and passkeys (YubiKey, Windows Hello style sign-ins) from this computer to sign in to websites inside the remote session.",
    "redirectlocation": "Share this computer's location with the remote one, for apps like Maps and for setting the time zone.",
    "FOLDERS": "Each folder listed here appears on the remote computer as a drive under 'This PC', named after the folder. Handy for sharing just Downloads or a NAS folder instead of every drive.",
    # Programs
    "alternate shell": "Run this program instead of the normal desktop when you connect, e.g. C:\\Program Files\\App\\app.exe. Closing the program ends the session. Leave empty for the full desktop.",
    "shell working directory": "The folder the program above starts in (its working directory), e.g. C:\\Users\\you\\Documents.",
    "remoteapplicationmode": "Show only one program's window, as if it ran on this computer, instead of the whole remote desktop. The remote computer must publish that program as a RemoteApp.",
    "remoteapplicationprogram": "The program to run as a RemoteApp: its published alias with two bars in front, e.g. ||notepad, or its full path.",
    "remoteapplicationname": "The name shown for the RemoteApp window.",
    "remoteapplicationcmdline": "Extra arguments for the RemoteApp program, e.g. the file it should open.",
    # Experience
    "connection type": "Tells the remote computer how fast your connection is, so it can switch off visual effects to stay quick. 'Detect connection quality automatically' measures it and is right almost always.",
    "disable wallpaper": "Show the remote computer's wallpaper. Turn it off to save data on slow connections.",
    "allow font smoothing": "ClearType smoothing for text: easier to read, costs a little more data.",
    "allow desktop composition": "Windows' transparency, blur and window shadow effects.",
    "disable full window drag": "Show a window's contents while you drag it. Off shows just an outline, which is quicker on slow connections.",
    "disable menu anims": "Fade and slide animations for menus and windows.",
    "disable themes": "Windows' visual styles (modern buttons, title bars and scroll bars). Off gives the plain classic look.",
    "bitmapcachepersistenable": "Keep parts of the screen this computer already received in a cache on disk, so they are not sent again. Makes scrolling and reconnecting faster.",
    "autoreconnection enabled": "If the network drops for a moment, reconnect by itself and carry on in the same session, with everything still open.",
    "autoreconnect max retries": "How many times to try reconnecting before giving up.",
    # Advanced
    "authentication level": "What to do when the remote computer's identity cannot be verified, e.g. its certificate is self-signed (normal for home PCs). 'Warn me' asks you. 'Connect and don't warn me' trusts it silently: only for networks you trust. 'Do not connect' refuses.",
    "enablecredsspsupport": "Network Level Authentication (NLA): you sign in before the remote desktop is even shown. Safer, and Windows requires it by default. Turn it off only if connecting fails, e.g. with very old computers or an account that must change its password first.",
    "negotiate security layer": "Let both sides pick the strongest encryption they share. Turn off only to force the old RDP encryption for very old servers.",
    "administrative session": "Connect to the computer's console session for administration instead of starting a new session. On Windows Server it does not use up a Remote Desktop licence.",
    "gatewayusagemethod": "An RD Gateway is a company server that lets you reach computers inside the office network from the internet over HTTPS. Use it always, only when a direct connection fails, or not for addresses on your local network.",
    "gatewayhostname": "The RD Gateway's address, e.g. rdgateway.company.com. Your IT department gives you this.",
    "gatewaycredentialssource": "How you sign in to the gateway: with a password, with a smart card, or choose when connecting.",
    "promptcredentialonce": "Sign in once: use the same user name and password for the gateway and for the remote computer.",
}

SPECS = [r for _, _, _, groups in TABS for g in groups if isinstance(g, tuple) for r in g[2] if isinstance(r, dict)]


def complete(rdp):
    """Fill every setting the file lacks with its default and keep derived keys in step."""
    for s in SPECS:
        if rdp.get(s["key"]) is None:
            rdp.set(s["key"], s["typ"], s["default"])
    auto = "1" if rdp.get("connection type") == "7" else "0"
    rdp.set("networkautodetect", "i", auto)
    rdp.set("bandwidthautodetect", "i", auto)
    rdp.set("gatewayprofileusagemethod", "i", "1")  # use the gateway settings above, not system defaults


# ---------------------------------------------------------------- FreeRDP

def find_client(names=CLIENTS):
    """First installed FreeRDP 3 client (it must know /args-from), or None. RDPDESK_FREERDP overrides."""
    if os.environ.get("RDPDESK_FREERDP"):
        names = [os.environ["RDPDESK_FREERDP"]]
    for name in names:
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            out = subprocess.run([exe, "/help"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        if "/args-from" in out:
            return exe
    return None


def client_args(rdp, client, user, password):
    """Arguments for one session. They go to FreeRDP on stdin (/args-from:stdin), never on the
    command line, so the password does not show in `ps`. Returns (args, skipped folders)."""
    args = [str(rdp.path), f"/t:{rdp.path.stem}", "/log-level:WARN"]
    if user:
        args.append("/u:" + user)
    if password is not None:
        args.append("/p:" + password)
    skipped = []
    for path in rdp.folders():
        if "," in path or "\n" in path:  # FreeRDP splits /drive: on commas
            skipped.append(path)
        else:
            args.append(f"/drive:{Path(path).name or 'root'},{path}")
    # Only the SDL client can ask about an unknown certificate; the others trust it the first time.
    cert = {"0": "ignore", "1": "deny"}.get(rdp.get("authentication level"))
    cert = cert or (None if "sdl-" in Path(client).name else "tofu")
    if cert:
        args.append("/cert:" + cert)
    return args, skipped


ERRORS = {
    "ERRCONNECT_CONNECT_FAILED": "The computer did not answer. Check the address, and that Remote Desktop is on there.",
    "ERRCONNECT_DNS_NAME_NOT_FOUND": "The computer name could not be found.",
    "ERRCONNECT_LOGON_FAILURE": "Wrong user name or password.",
    "ERRCONNECT_AUTHENTICATION_FAILED": "Wrong user name or password.",
    "ERRCONNECT_WRONG_PASSWORD": "Wrong user name or password.",
    "ERRCONNECT_NO_OR_MISSING_CREDENTIALS": "No user name or password was given.",
    "ERRCONNECT_ACCOUNT_LOCKED_OUT": "This account is locked.",
    "ERRCONNECT_ACCOUNT_DISABLED": "This account is disabled.",
    "ERRCONNECT_ACCOUNT_EXPIRED": "This account has expired.",
    "ERRCONNECT_PASSWORD_EXPIRED": "The password has expired. Change it on the computer itself first.",
    "ERRCONNECT_PASSWORD_MUST_CHANGE": "The password must be changed. Change it on the computer itself first.",
    "ERRCONNECT_PASSWORD_CERTAINLY_EXPIRED": "The password has expired. Change it on the computer itself first.",
    "ERRCONNECT_LOGON_TYPE_NOT_GRANTED": "This account may not sign in over Remote Desktop.",
    "ERRCONNECT_TLS_CONNECT_FAILED": "The secure connection failed. Try switching Network Level Authentication in Advanced.",
    "ERRCONNECT_SECURITY_NEGO_CONNECT_FAILED": "Security negotiation failed. Try switching Network Level Authentication in Advanced.",
    "ERRCONNECT_CONNECT_TRANSPORT_FAILED": "The connection dropped while connecting.",
}


def failure(lines):
    """Plain words for why a connection failed, from FreeRDP's log; None if it did not fail
    while connecting (or the user cancelled)."""
    for line in reversed(lines):
        m = re.search(r"ERRCONNECT_[A-Z_]+", line)
        if m:
            code = m[0]
            if code == "ERRCONNECT_CONNECT_CANCELLED":
                return None
            if code in ERRORS:
                return ERRORS[code]
            first = next((l.split("]: ", 1)[-1].strip() for l in lines if "[ERROR]" in l and "ERRCONNECT_" not in l), "")
            return f"FreeRDP reported {code}." + (f"\n\n{first}" if first else "")
    return None


# ---------------------------------------------------------------- user interface

import gi  # noqa: E402  (the file part above runs without a display, for the self-check)

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("Secret", "1")
    from gi.repository import Secret
except (ValueError, ImportError):
    Secret = None


class Keyring:
    """Passwords in the Secret Service keyring. `ok` is False when no keyring answers."""

    def __init__(self):
        self.ok = False
        if Secret:
            self.schema = Secret.Schema.new(APP_ID, Secret.SchemaFlags.NONE,
                                            {"connection": Secret.SchemaAttributeType.STRING})
            try:
                Secret.password_lookup_sync(self.schema, {"connection": ""}, None)
                self.ok = True
            except GLib.Error:
                pass

    def get(self, name):
        if not self.ok:
            return None
        try:
            return Secret.password_lookup_sync(self.schema, {"connection": name}, None)
        except GLib.Error:
            return None

    def set(self, name, password):
        """Raises GLib.Error if the keyring refuses."""
        if not self.ok:
            return
        if password:
            Secret.password_store_sync(self.schema, {"connection": name}, Secret.COLLECTION_DEFAULT,
                                       f"Remote Desktop: {name}", password, None)
        else:
            Secret.password_clear_sync(self.schema, {"connection": name}, None)

    def copy(self, old, new, move=False):
        password = self.get(old)
        if password:
            self.set(new, password)
            if move:
                self.set(old, "")


def help_icon(text):
    """A small (i) at the end of a row; hovering it shows what the setting does."""
    return Gtk.Image(icon_name="help-about-symbolic", tooltip_text=text, valign=Gtk.Align.CENTER,
                     css_classes=["dim-label"])


CSS = """
.hero {
  padding: 20px 22px;
  border-radius: 18px;
  background-image: linear-gradient(120deg, alpha(@accent_bg_color, 0.28), alpha(@accent_bg_color, 0.07) 55%, alpha(@accent_bg_color, 0.0));
  box-shadow: inset 0 0 0 1px alpha(@accent_bg_color, 0.22);
}
.hero-name { font-size: 1.7em; font-weight: 800; letter-spacing: -0.02em; }
.hero-host { font-family: monospace; opacity: 0.72; }
.status { border-radius: 999px; padding: 2px 10px; font-size: 0.78em; font-weight: 800;
          letter-spacing: 0.04em; background: alpha(@window_fg_color, 0.08); }
.status.online { background: alpha(@success_color, 0.18); color: @success_color; }
.status.warning { background: alpha(@warning_color, 0.18); color: @warning_color; }
.chip { border-radius: 999px; padding: 3px 11px; font-size: 0.86em; background: alpha(@window_fg_color, 0.07); }
.big-connect { padding: 10px 26px; font-weight: 800; font-size: 1.05em; }
.online-dot { color: @success_color; font-size: 0.75em; }
"""


def safe_name(text):
    return text.strip().replace("/", "-").lstrip(".").strip() or "Connection"


def set_quietly(row, text):
    """Set an entry row's text without showing its apply button (that is for the user's edits)."""
    row.set_show_apply_button(False)
    row.set_text(text)
    row.set_show_apply_button(True)


def free_path(name):
    path, n = DATA / f"{name}.rdp", 2
    while path.exists():
        path, n = DATA / f"{name} {n}.rdp", n + 1
    return path


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="RDP Desk", default_width=1220, default_height=800)
        self.set_size_request(360, 420)
        DATA.mkdir(parents=True, exist_ok=True)
        self.keyring = Keyring()
        self.clients = {}  # full screen? -> FreeRDP client
        self.sessions = {}  # .rdp path -> running FreeRDP process
        self.rdp = None  # the selected connection
        self.loading = False
        self.rows = []  # (setting, row)

        for name, fn in [("new", self.new), ("import", self.pick_import), ("folder", self.open_folder),
                         ("about", self.about), ("connect", self.toggle_session), ("duplicate", self.duplicate),
                         ("export", self.export), ("delete", self.delete), ("search", self.focus_search)]:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_, f=fn: f())
            self.add_action(action)

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=260, max_sidebar_width=340)
        self.split.set_sidebar(Adw.NavigationPage(title="Connections", child=self.build_sidebar()))
        self.split.set_content(Adw.NavigationPage(title="Connection", child=self.build_content()))
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1400sp"))
        narrow.add_setter(self.switcher, "visible", False)
        narrow.add_setter(self.switcher_bar, "reveal", True)
        self.add_breakpoint(narrow)
        phone = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        phone.add_setter(self.split, "collapsed", True)
        phone.add_setter(self.switcher, "visible", False)
        phone.add_setter(self.switcher_bar, "reveal", True)
        self.add_breakpoint(phone)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", lambda _t, value, _x, _y: bool(self.import_files(value.get_files())))
        self.add_controller(drop)
        self.connect("close-request", self.on_close)
        self.reload()

    # ---- layout

    def build_sidebar(self):
        self.side_title = Adw.WindowTitle(title="RDP Desk")
        header = Adw.HeaderBar(title_widget=self.side_title)
        header.pack_start(Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New connection (Ctrl+N)",
                                     action_name="win.new"))
        menu = Gio.Menu()
        menu.append("Import .rdp files…", "win.import")
        menu.append("Open the connections folder", "win.folder")
        menu.append("About RDP Desk", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))

        self.search = Gtk.SearchEntry(placeholder_text="Search connections", margin_start=8, margin_end=8,
                                      margin_bottom=6)
        self.search.connect("search-changed", lambda *_: self.list.invalidate_filter())
        self.list = Gtk.ListBox(css_classes=["navigation-sidebar"], activate_on_single_click=False)
        self.list.set_sort_func(lambda a, b: (a.name.casefold() > b.name.casefold()) - (a.name.casefold() < b.name.casefold()))
        self.list.set_filter_func(lambda row: self.search.get_text().casefold() in (row.name + " " + row.get_subtitle()).casefold())
        self.list.connect("row-selected", self.on_select)
        self.list.connect("row-activated", lambda *_: self.toggle_session())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.search)
        box.append(Gtk.ScrolledWindow(child=self.list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        view = Adw.ToolbarView(content=box)
        view.add_top_bar(header)
        return view

    def build_content(self):
        self.tabs = Adw.ViewStack()
        for tab_id, title, icon, groups in TABS:
            page = Adw.PreferencesPage()
            for group in groups:
                page.add(self.build_folders() if group == "FOLDERS" else self.build_group(*group))
            self.tabs.add_titled_with_icon(page, tab_id, title, icon)

        self.switcher = Adw.ViewSwitcher(stack=self.tabs, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.tabs)
        more = Gio.Menu()
        more.append("Duplicate", "win.duplicate")
        more.append("Export…", "win.export")
        more.append("Delete", "win.delete")
        header = Adw.HeaderBar(title_widget=self.switcher)
        header.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=more, tooltip_text="More"))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.build_hero())
        self.tabs.set_vexpand(True)
        body.append(self.tabs)
        edit = Adw.ToolbarView(content=body)
        edit.add_top_bar(header)
        edit.add_bottom_bar(self.switcher_bar)

        new = Gtk.Button(label="New connection", action_name="win.new", halign=Gtk.Align.CENTER,
                         css_classes=["pill", "suggested-action"])
        imp = Gtk.Button(label="Import .rdp files…", action_name="win.import", halign=Gtk.Align.CENTER,
                         css_classes=["pill"])
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        buttons.append(new)
        buttons.append(imp)
        empty = Adw.ToolbarView(content=Adw.StatusPage(
            icon_name="network-server-symbolic", title="No connections yet",
            description="Add a computer and its desktop is one click away.\nGot .rdp files? Import them, or just drop them here.",
            child=buttons))
        empty.add_top_bar(Adw.HeaderBar(show_title=False))

        self.pages = Gtk.Stack()
        self.pages.add_named(edit, "edit")
        self.pages.add_named(empty, "empty")
        return self.pages

    def build_hero(self):
        """The banner over the tabs: who this is, where it goes, how it is set up, and the big button."""
        self.hero_avatar = Adw.Avatar(size=64, show_initials=True, valign=Gtk.Align.CENTER)
        self.hero_name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["hero-name"])
        self.hero_host = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, selectable=True, css_classes=["hero-host"])
        self.hero_status = Gtk.Label(css_classes=["status"], valign=Gtk.Align.CENTER)
        line = Gtk.Box(spacing=10)
        line.append(self.hero_host)
        line.append(self.hero_status)
        self.hero_chips = Gtk.Box(spacing=6)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        for w in (self.hero_name, line):
            text.append(w)
        self.connect_button = Gtk.Button(label="Connect", tooltip_text="Connect (Ctrl+Enter, or double-click it in the list)",
                                         action_name="win.connect", valign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=18)
        for w in (self.hero_avatar, text, self.connect_button):
            top.append(w)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, css_classes=["hero"])
        box.append(top)
        box.append(self.hero_chips)
        return Adw.Clamp(maximum_size=600, tightening_threshold=560, child=box,
                         margin_top=18, margin_bottom=2, margin_start=12, margin_end=12)

    def update_hero(self):
        if not self.rdp:
            return
        rdp, name = self.rdp, self.rdp.path.stem
        running = str(rdp.path) in self.sessions
        host = rdp.get("full address", "")
        self.hero_avatar.set_text(name)
        self.hero_name.set_label(name)
        self.hero_host.set_label(host or "no computer set")
        status, kind = ("● CONNECTED", "online") if running else ("READY", "") if host else ("NEEDS A COMPUTER", "warning")
        self.hero_status.set_label(status)
        self.hero_status.set_css_classes(["status"] + ([kind] if kind else []))
        self.connect_button.set_label("Disconnect" if running else "Connect")
        self.connect_button.set_css_classes(["pill", "big-connect", "destructive-action" if running else "suggested-action"])
        while child := self.hero_chips.get_first_child():
            self.hero_chips.remove(child)
        chips = self.chips(rdp)
        shown = chips if len(chips) <= 4 else chips[:3] + [f"+{len(chips) - 3}"]
        for chip in shown:
            label = Gtk.Label(label=chip, css_classes=["chip"])
            if chip.startswith("+"):
                label.set_tooltip_text(" · ".join(chips[3:]))
            self.hero_chips.append(label)

    @staticmethod
    def chips(rdp):
        """A few words on how this connection is set up."""
        g = rdp.get
        out = ["Full screen" if g("screen mode id") == "2" else
               "Window · resizes" if g("dynamic resolution") == "1" else f"Window · {g('desktopwidth')}×{g('desktopheight')}"]
        if g("use multimon") == "1":
            out.append("All monitors")
        user = g("username", "")
        if user:
            out.append(f"{g('domain')}\\{user}" if g("domain") else user)
        n = len(rdp.folders())
        if n:
            out.append(f"{n} shared folder{'s' if n > 1 else ''}")
        if g("audiocapturemode") == "1":
            out.append("Microphone")
        if g("remoteapplicationmode") == "1":
            out.append("RemoteApp")
        if g("gatewayusagemethod", "0") != "0" and g("gatewayhostname"):
            out.append("Via RD Gateway")
        if g("administrative session") == "1":
            out.append("Admin session")
        return out

    def build_group(self, title, description, items):
        group = Adw.PreferencesGroup(title=title, description=description)
        for item in items:
            if item == "NAME":
                self.name_row = Adw.EntryRow(title="Name", show_apply_button=True)
                self.name_row.connect("apply", lambda *_: self.rename())
                self.name_row.add_suffix(help_icon(HELP["NAME"]))
                group.add(self.name_row)
            elif item == "PASSWORD":
                self.password_row = Adw.PasswordEntryRow(title="Password", show_apply_button=True)
                self.password_row.connect("apply", lambda *_: self.save_password())
                self.password_row.add_suffix(help_icon(HELP["PASSWORD"]))
                group.add(self.password_row)
            else:
                row = self.build_row(item)
                self.rows.append((item, row))
                group.add(row)
        return group

    def build_row(self, s):
        kind = s["kind"]
        if kind == "text":
            row = Adw.EntryRow(title=s["title"])
            row.connect("changed", lambda r: self.edit(s, r.get_text()))
        elif kind == "combo":
            row = Adw.ComboRow(title=s["title"], model=Gtk.StringList.new([label for _, label in s["choices"]]))
            row.connect("notify::selected", lambda r, _: r.get_selected() < len(s["choices"])
                        and self.edit(s, s["choices"][r.get_selected()][0]))
        elif kind == "spin":
            row = Adw.SpinRow.new_with_range(s["lo"], s["hi"], 1)
            row.set_title(s["title"])
            row.connect("notify::value", lambda r, _: self.edit(s, str(int(r.get_value()))))
        else:  # switch, inv, star
            on, off = {"switch": ("1", "0"), "inv": ("0", "1"), "star": ("*", "")}[kind]
            row = Adw.SwitchRow(title=s["title"], subtitle=s["subtitle"])
            row.connect("notify::active", lambda r, _: self.edit(s, on if r.get_active() else off))
        row.add_suffix(help_icon(HELP[s["key"]]))
        return row

    def build_folders(self):
        self.folder_group = Adw.PreferencesGroup(
            title="Shared folders", description="These folders show up as drives on the remote computer.")
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Share a folder", css_classes=["flat"])
        add.connect("clicked", lambda *_: self.pick_folder())
        suffix = Gtk.Box(spacing=6)
        suffix.append(help_icon(HELP["FOLDERS"]))
        suffix.append(add)
        self.folder_group.set_header_suffix(suffix)
        self.folder_rows = []
        return self.folder_group

    # ---- list

    def reload(self, select=None):
        """Re-read the connections folder; select `select` (a path), else keep or take the first."""
        select = str(select or (self.rdp and self.rdp.path) or "")
        self.list.remove_all()
        chosen = None
        for path in DATA.glob("*.rdp"):
            try:
                host = RdpFile(path).get("full address", "")
            except OSError:
                continue
            row = Adw.ActionRow(title=GLib.markup_escape_text(path.stem))
            row.add_prefix(Adw.Avatar(size=34, text=path.stem, show_initials=True))
            row.dot = Gtk.Label(label="●", css_classes=["online-dot"], visible=False, tooltip_text="Connected")
            row.add_suffix(row.dot)
            row.name, row.path, row.host = path.stem, path, host
            self.list.append(row)
            if str(path) == select:
                chosen = row
        self.mark_running()
        first = chosen or self.list.get_row_at_index(0)
        if first:
            self.list.select_row(first)
        else:
            self.rdp = None
            self.pages.set_visible_child_name("empty")

    def mark_running(self):
        i = 0
        while row := self.list.get_row_at_index(i):
            row.set_subtitle(GLib.markup_escape_text(row.host or "No computer set"))
            row.dot.set_visible(str(row.path) in self.sessions)
            i += 1
        live = len(self.sessions)
        self.side_title.set_subtitle(f"{i} connection{'' if i == 1 else 's'}" + (f" · {live} connected" if live else ""))
        self.update_hero()

    def on_select(self, _list, row):
        if not row:
            return
        try:
            self.rdp = RdpFile(row.path)
        except OSError as e:
            return self.toast(f"Could not read {row.name}: {e.strerror}")
        self.loading = True
        set_quietly(self.name_row, row.name)
        for s, w in self.rows:
            self.show_value(s, w, self.rdp.get(s["key"], s["default"]))
        set_quietly(self.password_row, self.keyring.get(row.name) or "")
        self.password_row.set_sensitive(self.keyring.ok)
        self.password_row.set_title("Password" if self.keyring.ok else "Password (no keyring running: asked when connecting)")
        self.show_folders()
        self.loading = False
        self.split.get_content().set_title(row.name)
        self.pages.set_visible_child_name("edit")
        self.split.set_show_content(True)
        self.mark_running()

    def show_value(self, s, row, value):
        kind = s["kind"]
        if kind == "text":
            row.set_text(value)
        elif kind == "combo":
            row.set_selected(next((i for i, (v, _) in enumerate(s["choices"]) if v == value), 0))
        elif kind == "spin":
            try:
                row.set_value(float(value))
            except ValueError:
                row.set_value(float(s["default"]))
        else:
            row.set_active({"switch": value == "1", "inv": value != "1", "star": bool(value)}[kind])

    def show_folders(self):
        for row in self.folder_rows:
            self.folder_group.remove(row)
        self.folder_rows = []
        for path in self.rdp.folders():
            row = Adw.ActionRow(title=GLib.markup_escape_text(Path(path).name or path),
                                subtitle=GLib.markup_escape_text(path))
            remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Stop sharing",
                                valign=Gtk.Align.CENTER, css_classes=["flat"])
            remove.connect("clicked", lambda *_, p=path: self.set_folders([f for f in self.rdp.folders() if f != p]))
            row.add_suffix(remove)
            self.folder_group.add(row)
            self.folder_rows.append(row)

    # ---- editing

    def edit(self, s, value):
        if self.loading or not self.rdp:
            return
        self.rdp.set(s["key"], s["typ"], value)
        self.write()
        self.update_hero()
        if s["key"] == "full address":
            self.list.get_selected_row().host = value
            self.mark_running()

    def write(self):
        complete(self.rdp)
        try:
            self.rdp.save()
        except OSError as e:
            self.toast(f"Could not save: {e.strerror}")

    def set_folders(self, paths):
        self.rdp.set_folders(paths)
        self.write()
        self.show_folders()
        self.update_hero()

    def pick_folder(self):
        def done(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            path = folder.get_path() if folder else None
            if path and path not in self.rdp.folders():
                self.set_folders(self.rdp.folders() + [path])
        if self.rdp:
            Gtk.FileDialog(title="Share a folder").select_folder(self, None, done)

    def rename(self):
        name = safe_name(self.name_row.get_text())
        old = self.rdp.path
        if name == old.stem:
            return
        new = DATA / f"{name}.rdp"
        if new.exists():
            self.name_row.set_text(old.stem)
            return self.toast(f"There is already a connection called {name}")
        try:
            old.rename(new)
        except OSError as e:
            return self.toast(f"Could not rename: {e.strerror}")
        self.keyring.copy(old.stem, name, move=True)
        if str(old) in self.sessions:
            self.sessions[str(new)] = self.sessions.pop(str(old))
        self.reload(select=new)

    def save_password(self):
        if not self.rdp:
            return
        try:
            self.keyring.set(self.rdp.path.stem, self.password_row.get_text())
            self.toast("Password saved in the keyring" if self.password_row.get_text() else "Password removed")
        except GLib.Error as e:
            self.toast(f"The keyring refused: {e.message}")

    # ---- connections

    def new(self):
        path = free_path("New connection")
        rdp = RdpFile(path)
        complete(rdp)
        try:
            rdp.save()
        except OSError as e:
            return self.toast(f"Could not create: {e.strerror}")
        self.reload(select=path)
        self.tabs.set_visible_child_name("general")
        self.name_row.grab_focus()

    def import_files(self, files):
        """Copy .rdp files into the connections folder (the same file twice is not copied again).
        Returns the paths in the folder."""
        paths = []
        known = {p.read_bytes(): p for p in DATA.glob("*.rdp")}
        for f in files:
            src = Path(f.get_path() or "")
            if src.suffix.lower() != ".rdp" or not src.is_file():
                continue
            if src.parent == DATA:
                paths.append(src)
                continue
            try:
                raw = src.read_bytes()
                if raw in known:
                    paths.append(known[raw])
                    continue
                rdp = RdpFile(src)
                complete(rdp)
                dest = free_path(safe_name(src.stem))
                rdp.save(dest)
                known[raw] = dest
                paths.append(dest)
            except OSError as e:
                self.toast(f"Could not import {src.name}: {e.strerror}")
        if paths:
            self.reload(select=paths[-1])
        elif files:
            self.toast("No .rdp files to import")
        return paths

    def pick_import(self):
        def done(dialog, result):
            try:
                files = dialog.open_multiple_finish(result)
            except GLib.Error:
                return
            self.import_files([files.get_item(i) for i in range(files.get_n_items())])
        rdp_filter = Gtk.FileFilter(name="Remote Desktop files")
        rdp_filter.add_pattern("*.rdp")
        rdp_filter.add_pattern("*.RDP")
        Gtk.FileDialog(title="Import connections", default_filter=rdp_filter).open_multiple(self, None, done)

    def duplicate(self):
        if not self.rdp:
            return
        dest = free_path(f"{self.rdp.path.stem} copy")
        try:
            shutil.copyfile(self.rdp.path, dest)
        except OSError as e:
            return self.toast(f"Could not duplicate: {e.strerror}")
        self.keyring.copy(self.rdp.path.stem, dest.stem)
        self.reload(select=dest)

    def export(self):
        def done(dialog, result):
            try:
                dest = dialog.save_finish(result)
                shutil.copyfile(self.rdp.path, dest.get_path())
                self.toast(f"Saved {Path(dest.get_path()).name} (without the password)")
            except GLib.Error:
                pass
            except OSError as e:
                self.toast(f"Could not export: {e.strerror}")
        if self.rdp:
            Gtk.FileDialog(title="Export connection", initial_name=self.rdp.path.name).save(self, None, done)

    def delete(self):
        if not self.rdp:
            return
        path = self.rdp.path

        def answered(_dialog, response):
            if response != "delete":
                return
            proc = self.sessions.get(str(path))
            if proc:
                proc.stopped = True
                proc.terminate()
            try:
                path.unlink()
            except OSError as e:
                return self.toast(f"Could not delete: {e.strerror}")
            try:
                self.keyring.set(path.stem, "")
            except GLib.Error:
                pass
            self.rdp = None
            self.reload()
        dialog = Adw.AlertDialog(heading=f"Delete “{path.stem}”?",
                                 body="The connection and its saved password are removed.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", answered)
        dialog.present(self)

    def toggle_session(self):
        if not self.rdp:
            return
        proc = self.sessions.get(str(self.rdp.path))
        if proc:
            proc.stopped = True
            proc.terminate()
        else:
            self.start(self.rdp)

    def start(self, rdp):
        if str(rdp.path) in self.sessions:
            return
        if not rdp.get("full address"):
            return self.alert("No computer set", "Type the computer's name or IP address on the General tab.")
        always_ask = rdp.get("prompt for credentials") == "1"
        password = None if always_ask else self.keyring.get(rdp.path.stem)
        if password is None:
            return self.ask_credentials(rdp, remember=self.keyring.ok and not always_ask)
        self.launch(rdp, rdp.get("username", ""), password)

    def ask_credentials(self, rdp, remember):
        """Our own sign-in dialog, so every FreeRDP client works (only the SDL one could ask)."""
        user = Adw.EntryRow(title="User name", text=rdp.get("username", ""), activates_default=True)
        password = Adw.PasswordEntryRow(title="Password", activates_default=True)
        keep = Adw.SwitchRow(title="Remember the password", subtitle="In the keyring", active=remember,
                             visible=remember)
        rows = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        for row in (user, password, keep):
            rows.append(row)
        dialog = Adw.AlertDialog(heading=f"Sign in to {rdp.path.stem}", body=rdp.get("full address", ""),
                                 extra_child=rows, default_response="connect", close_response="cancel")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("connect", "Connect")
        dialog.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)

        def answered(_dialog, response):
            if response == "connect":
                self.answer_sign_in(rdp, user.get_text(), password.get_text(), keep.get_active())
        dialog.connect("response", answered)
        dialog.present(self)
        (password if user.get_text() else user).grab_focus()

    def answer_sign_in(self, rdp, user, password, keep):
        if keep:
            try:
                self.keyring.set(rdp.path.stem, password)
                if rdp is self.rdp:
                    set_quietly(self.password_row, password)
            except GLib.Error as e:
                self.toast(f"The keyring refused: {e.message}")
        self.launch(rdp, user, password)

    def launch(self, rdp, user, password):
        if str(rdp.path) in self.sessions:
            return
        full = rdp.get("screen mode id") == "2"
        client = self.clients.get(full) or self.clients.setdefault(full, find_client(FULLSCREEN_CLIENTS if full else CLIENTS))
        if not client:
            return self.alert("FreeRDP 3 is not installed",
                              "Install your distribution's FreeRDP 3 package (on Arch: freerdp) and try again.")
        name = rdp.path.stem
        args, skipped = client_args(rdp, client, user, password)
        if skipped:
            self.toast(f"Not shared (FreeRDP cannot take commas in a path): {', '.join(skipped)}")
        if any("\n" in a or "\r" in a for a in args):
            return self.alert("Cannot connect", "The name or password contains a line break.")
        try:
            proc = subprocess.Popen([client, "/args-from:stdin"], stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
            proc.stdin.write("\n".join(args) + "\n")
            proc.stdin.close()
        except OSError as e:
            return self.alert("Could not start FreeRDP", str(e))
        proc.stopped = False
        self.toast(f"Connecting to {name}…")
        self.sessions[str(rdp.path)] = proc
        threading.Thread(target=self.watch, args=(name, str(rdp.path), proc), daemon=True).start()
        self.mark_running()

    def watch(self, name, key, proc):
        """Thread: keep the end of FreeRDP's log, then report back on the main loop."""
        tail = deque(proc.stderr, maxlen=60)
        code = proc.wait()
        GLib.idle_add(self.ended, name, key, proc, code, list(tail))

    def ended(self, name, key, proc, code, tail):
        if self.sessions.get(key) is proc:
            del self.sessions[key]
        self.mark_running()
        if proc.stopped:
            return False
        why = failure(tail)
        if why:
            self.alert(f"Could not connect to {name}", why)
        elif code:
            errors = [l.split(" - ", 1)[-1].strip() for l in tail if "[ERROR]" in l]
            self.toast(f"{name}: session ended" + (f" ({errors[-1]})" if errors else ""))
        return False

    # ---- small things

    def on_close(self, _window):
        if not self.sessions:
            return False

        def answered(_dialog, response):
            if response == "quit":
                for proc in list(self.sessions.values()):
                    proc.stopped = True
                    proc.terminate()
                self.sessions.clear()
                self.destroy()
        n = len(self.sessions)
        dialog = Adw.AlertDialog(heading="Disconnect and quit?",
                                 body=f"{n} remote session{'s are' if n > 1 else ' is'} still open.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("quit", "Disconnect and quit")
        dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", answered)
        dialog.present(self)
        return True

    def focus_search(self):
        self.split.set_show_content(False)
        self.search.grab_focus()

    def open_folder(self):
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(DATA))).launch(self, None, None)

    def about(self):
        Adw.AboutDialog(application_name="RDP Desk", application_icon=APP_ID, version=VERSION,
                        comments="Keep, edit and open Remote Desktop connections.\nConnections by FreeRDP.",
                        license_type=Gtk.License.MIT_X11, developer_name="Nitant").present(self)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def alert(self, heading, body):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for action, keys in [("win.new", ["<Control>n"]), ("win.import", ["<Control>o"]),
                             ("win.search", ["<Control>f"]), ("win.connect", ["<Control>Return"]),
                             ("window.close", ["<Control>w", "<Control>q"])]:
            self.set_accels_for_action(action, keys)

    def do_activate(self):
        (self.props.active_window or Window(self)).present()

    def do_open(self, files, _n, _hint):
        """Opening .rdp files (a double-click in the file manager): import, then connect."""
        self.activate()
        window = self.props.active_window
        paths = window.import_files(files)
        if paths:
            window.start(RdpFile(paths[-1]))


def main():
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
