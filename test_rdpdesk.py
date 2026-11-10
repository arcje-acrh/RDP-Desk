"""Self-check for the .rdp handling and the FreeRDP glue: python3 test_rdpdesk.py"""
import tempfile
from pathlib import Path

import rdpdesk as r

with tempfile.TemporaryDirectory() as d:
    d = Path(d)

    # Windows writes UTF-16 with CRLF; keys it does not know and odd lines must survive.
    win = d / "Office PC.rdp"
    win.write_bytes("screen mode id:i:2\r\nfull address:s:pc.example.com:3390\r\n"
                    "some future key:s:a:b:c\r\nnot a setting line\r\n".encode("utf-16"))
    f = r.RdpFile(win)
    assert f.utf16 and f.get("FULL ADDRESS") == "pc.example.com:3390"  # keys are case-blind, colons kept
    assert f.get("some future key") == "a:b:c"
    f.set("screen mode id", "i", 1)
    r.complete(f)
    f.save()
    raw = win.read_bytes()
    assert raw[:2] == b"\xff\xfe", "UTF-16 kept"
    text = raw.decode("utf-16")
    assert text.startswith("screen mode id:i:1\r\nfull address:s:pc.example.com:3390\r\n"), "order and CRLF kept"
    assert "not a setting line\r\n" in text and "some future key:s:a:b:c" in text
    assert "dynamic resolution:i:1" in text and "networkautodetect:i:1" in text, "defaults filled"
    assert r.RdpFile(win).get("screen mode id") == "1"

    # The real-world file: UTF-8, one line, no newline, a space before an IPv6 address.
    v6 = d / "v6.rdp"
    v6.write_text("full address:s: 2001:db8:173:2d91:5e93:1833:3cce:1286")
    assert r.RdpFile(v6).get("full address") == "2001:db8:173:2d91:5e93:1833:3cce:1286"

    # Shared folders live in an app-only key; FreeRDP gets them as /drive: arguments.
    f = r.RdpFile(v6)
    f.set_folders(["/home/u/Shared stuff", "/home/u/a,b"])
    f.set("authentication level", "i", "0")
    f.save()
    f = r.RdpFile(v6)
    args, skipped = r.client_args(f, "/usr/bin/sdl-freerdp3", "me", "pa:ss word")
    assert args[0] == str(v6) and "/t:v6" in args and "/p:pa:ss word" in args and "/u:me" in args
    assert "/drive:Shared stuff,/home/u/Shared stuff" in args and skipped == ["/home/u/a,b"]
    assert "/cert:ignore" in args
    f.set("authentication level", "i", "2")
    assert not any(a.startswith("/cert") for a in r.client_args(f, "/usr/bin/sdl-freerdp3", "", None)[0])
    assert "/cert:tofu" in r.client_args(f, "/usr/bin/xfreerdp3", "", None)[0], "non-SDL clients cannot ask"
    assert not any(a.startswith("/p:") for a in r.client_args(f, "x", "", None)[0])
    assert "-gfx" not in r.client_args(f, "x", "", None)[0]
    f.set(r.GFX_KEY, "i", "0")
    assert "-gfx" in r.client_args(f, "x", "", None)[0], "graphics acceleration off -> -gfx"

# FreeRDP's log -> plain words (this line is from a real failed connect).
log = ["[13:24:17:099] [29336:00007298] [ERROR][com.freerdp.client.SDL] - [handleShow]: "
       "ERRCONNECT_CONNECT_FAILED [0x00020006]"]
assert r.failure(log).startswith("The computer did not answer")
assert r.failure(["... ERRCONNECT_CONNECT_CANCELLED ..."]) is None
assert r.failure(["... ERRCONNECT_SOMETHING_NEW ..."]) == "FreeRDP reported ERRCONNECT_SOMETHING_NEW."
pre = ["[x] [ERROR][com.freerdp.settings] - [check]: Monitor configuration virtual desktop width must be 200 <= 80",
       "[x] [ERROR][com.freerdp.client.SDL] - [handleShow]: ERRCONNECT_PRE_CONNECT_FAILED [0x00020001]"]
assert r.failure(pre).endswith("Monitor configuration virtual desktop width must be 200 <= 80"), r.failure(pre)
assert "/p:" in " ".join(r.client_args(r.RdpFile(win), "x", "", "")[0]), "an empty password is still a password"
assert r.failure(["[INFO] all fine"]) is None

# Every setting row is well formed.
for s in r.SPECS:
    assert s["kind"] in ("text", "switch", "inv", "star", "combo", "spin"), s
    if s["kind"] == "combo":
        assert s["default"] in [v for v, _ in s["choices"]], s["key"]
    if s["kind"] == "spin":
        assert s["lo"] <= int(s["default"]) <= s["hi"], s["key"]
assert len({s["key"] for s in r.SPECS}) == len(r.SPECS), "no key twice"
missing = [s["key"] for s in r.SPECS if s["key"] not in r.HELP]
assert not missing, f"settings without help: {missing}"

print(f"ok: {len(r.SPECS)} settings")
