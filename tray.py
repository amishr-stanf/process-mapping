"""
System-tray entry point for the packaged workflow-mapper app.

Runs the local server in a background thread and shows a tray icon with a menu
(Open dashboard / Start / Stop / Quit). This is the entry PyInstaller bundles
into the .exe. Falls back to a console-run if pystray isn't available.
"""

import threading
import webbrowser

import app as appmod

PORT = 8765
URL = f"http://127.0.0.1:{PORT}"

try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except Exception:
    HAVE_TRAY = False


def _make_image():
    # Gold rounded tile with three dark bars — matches the app's brand mark.
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, 58, 58], radius=14, fill=(242, 193, 78, 255))
    for i, y in enumerate((22, 32, 42)):
        d.rounded_rectangle([18, y, 46 - i * 6, y + 4], radius=2, fill=(58, 44, 11, 255))
    return img


def run():
    server = appmod.build_server(PORT)
    threading.Thread(target=server.serve_forever, name="server", daemon=True).start()
    appmod.capture.start()   # auto-start capturing on launch (Stop from the tray/dashboard)
    webbrowser.open(URL)

    if not HAVE_TRAY:
        print(f"workflow-mapper running at {URL} — close this window to quit.")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        appmod.capture.stop()
        server.shutdown()
        return

    def _open(icon, item): webbrowser.open(URL)
    def _start(icon, item): appmod.capture.start()
    def _stop(icon, item): appmod.capture.stop()
    def _quit(icon, item):
        appmod.capture.stop()
        server.shutdown()
        icon.stop()

    icon = pystray.Icon(
        "workflow-mapper", _make_image(), "workflow-mapper",
        menu=pystray.Menu(
            pystray.MenuItem("Open dashboard", _open, default=True),
            pystray.MenuItem("Start mapping", _start),
            pystray.MenuItem("Stop mapping", _stop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", _quit),
        ),
    )
    icon.run()


if __name__ == "__main__":
    run()
