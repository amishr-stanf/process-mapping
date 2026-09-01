"""
Generic in-app sensor for ANY Windows desktop application.

This is the answer to "I can't predict what app someone will use". Instead of
writing a probe per application, we listen to the OS accessibility layer
(MSAA/UI Automation) that Windows itself exposes for screen readers. Every
mainstream UI framework -- Win32, WinForms, WPF, Electron, Java Swing, Qt,
browsers -- publishes control-level events there, because accessibility support
is effectively mandatory for business software.

So the same sensor sees:
    * a claims system built in 2003 WinForms
    * a healthcare admin app nobody outside the sector has heard of
    * Excel, Word, Acrobat
    * an internal Electron tool

What we hook (SetWinEventHook, out-of-context, no DLL injection):
    EVENT_OBJECT_INVOKED      button / menu item / ribbon command activated
    EVENT_OBJECT_VALUECHANGE  an edit field's value changed (e.g. a search box)
    EVENT_OBJECT_SELECTION    a list / grid / tab selection changed
    EVENT_SYSTEM_DIALOGSTART  a dialog opened (this is how we see "Find")

For each event we resolve the control via oleacc's AccessibleObjectFromEvent to
get its NAME and ROLE, then emit a normalized action. Values are truncated and
never stored in full; password-ish controls are dropped entirely.

Pure ctypes -- no third-party dependency.
"""

import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
oleacc = ctypes.windll.oleacc
ole32 = ctypes.windll.ole32

# --- event ids we care about -------------------------------------------------
# Frameworks differ in what they emit: classic Win32/WinForms apps fire INVOKED,
# while WinUI/Electron/Qt often only fire FOCUS + VALUECHANGE. Hooking the union
# is what makes this work across the whole long tail of business software.
EVENT_SYSTEM_MENUSTART = 0x0004
EVENT_SYSTEM_DIALOGSTART = 0x0010
EVENT_OBJECT_FOCUS = 0x8005
EVENT_OBJECT_SELECTION = 0x8006
EVENT_OBJECT_VALUECHANGE = 0x800E
EVENT_OBJECT_INVOKED = 0x8013

WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

VERB_FOR = {
    EVENT_OBJECT_INVOKED: "invoke",
    EVENT_OBJECT_VALUECHANGE: "edit",
    EVENT_OBJECT_SELECTION: "select",
    EVENT_OBJECT_FOCUS: "field",
    EVENT_SYSTEM_DIALOGSTART: "dialog",
    EVENT_SYSTEM_MENUSTART: "menu",
}

# Roles worth recording (oleacc role ids). Anything else is usually chrome.
ROLE_NAMES = {
    9: "window", 10: "client", 11: "menupopup", 12: "menuitem", 22: "list",
    23: "listitem", 24: "outline", 25: "outlineitem", 27: "pagetab",
    33: "cell", 34: "link", 36: "list", 37: "listitem",
    42: "text", 43: "button", 44: "checkbox", 45: "radiobutton",
    46: "combobox", 50: "progressbar", 52: "slider", 60: "spinbutton",
}
NOISY_ROLES = {50, 52}          # progress bars / sliders fire constantly

SENSITIVE = ("password", "passwd", "pin", "cvv", "ssn", "secret", "token")

# Window chrome and continuously-firing widgets: real UI, but not "work".
NOISE_NAMES = {
    "zoom", "zoom in", "zoom out", "minimize", "maximize", "restore", "close",
    "scroll bar", "horizontal", "vertical", "line up", "line down", "page up",
    "page down", "position", "system", "application", "task switching",
    "desktopwindowxamlsource", "namedcontainerautomationpeer",
}

OBJID_WINDOW = 0
OBJID_CLIENT = -4
CHILDID_SELF = 0


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", ctypes.c_ulonglong), ("lParam", ctypes.c_ulonglong),
                ("time", wintypes.DWORD), ("pt", _POINT)]


WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    ctypes.c_long, ctypes.c_long, wintypes.DWORD, wintypes.DWORD)

user32.SetWinEventHook.restype = wintypes.HANDLE
user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                   WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
oleacc.GetRoleTextW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.UINT]


def _accessible_from_event(hwnd, id_object, id_child):
    """Resolve (name, role_id) for the control an event came from."""
    try:
        import comtypes  # noqa: F401  (only used if present; not required)
    except Exception:
        pass
    pacc = ctypes.POINTER(ctypes.c_void_p)()
    varchild = (ctypes.c_byte * 24)()   # VARIANT, opaque to us
    hr = oleacc.AccessibleObjectFromEvent(hwnd, id_object, id_child,
                                          ctypes.byref(pacc), ctypes.byref(varchild))
    if hr != 0 or not pacc:
        return None, None
    # IAccessible vtable: 0..2 IUnknown, 3.. IDispatch(4), then get_accParent(7),
    # get_accChildCount(8), get_accChild(9), get_accName(10), ... get_accRole(13)
    try:
        vtbl = ctypes.cast(pacc, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

        # get_accName(this, VARIANT child, BSTR* out)
        name_fn = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_byte * 24, ctypes.POINTER(ctypes.c_wchar_p)
        )(vtbl[10])
        bstr = ctypes.c_wchar_p()
        name = None
        if name_fn(pacc, varchild, ctypes.byref(bstr)) == 0 and bstr.value:
            name = bstr.value[:120]

        # get_accRole(this, VARIANT child, VARIANT* out)
        role_fn = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_byte * 24, ctypes.c_void_p
        )(vtbl[13])
        rolevar = (ctypes.c_byte * 24)()
        role_id = None
        if role_fn(pacc, varchild, ctypes.byref(rolevar)) == 0:
            # VARIANT: vt at offset 0 (VT_I4 == 3), value at offset 8
            vt = ctypes.cast(ctypes.byref(rolevar), ctypes.POINTER(ctypes.c_ushort)).contents.value
            if vt == 3:
                role_id = ctypes.cast(ctypes.byref(rolevar, 8),
                                      ctypes.POINTER(ctypes.c_long)).contents.value
        return name, role_id
    except Exception:
        return None, None
    finally:
        try:
            release = ctypes.cast(
                ctypes.cast(pacc, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[2],
                ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p))
            release(pacc)
        except Exception:
            pass


_PROC_CACHE = {}


def process_for_hwnd(hwnd):
    """Executable name owning a window, cached (events fire at high rate)."""
    if not hwnd:
        return None
    if hwnd in _PROC_CACHE:
        return _PROC_CACHE[hwnd]
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            size = wintypes.DWORD(4096)
            buf = ctypes.create_unicode_buffer(size.value)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = buf.value.rsplit("\\", 1)[-1]
                if len(_PROC_CACHE) > 512:
                    _PROC_CACHE.clear()
                _PROC_CACHE[hwnd] = name
                return name
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass
    return None


def role_text(role_id):
    if role_id is None:
        return None
    if role_id in ROLE_NAMES:
        return ROLE_NAMES[role_id]
    buf = ctypes.create_unicode_buffer(64)
    if oleacc.GetRoleTextW(role_id, buf, 64):
        return (buf.value or "").lower() or None
    return None


def _sensitive(name, role_id):
    n = (name or "").lower()
    return any(s in n for s in SENSITIVE)


class UIASensor:
    """Subscribes to OS accessibility events and forwards normalized actions.

    on_action(app_hint, verb, control_name, control_role) is called from the
    sensor thread; keep it fast and never let it raise.
    """

    def __init__(self, on_action, dedupe_seconds=0.4):
        self.on_action = on_action
        self.dedupe_seconds = dedupe_seconds
        self._thread = None
        self._stop = threading.Event()
        self._tid = None
        self._last = (None, 0.0)

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_running():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="uia", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if not self.is_running():
            return False
        self._stop.set()
        if self._tid:                       # nudge the message loop awake
            user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)   # WM_QUIT
        self._thread.join(timeout=3)
        return True

    # -- internals ----------------------------------------------------------
    def _handle(self, hWinEventHook, event, hwnd, id_object, id_child, thread_id, ts):
        try:
            if id_object not in (OBJID_WINDOW, OBJID_CLIENT) and id_object < 0:
                return
            name, role_id = _accessible_from_event(hwnd, id_object, id_child)
            if role_id in NOISY_ROLES:
                return
            if not name or len(name.strip()) < 2:
                return
            if _sensitive(name, role_id):
                return
            if name.strip().lower() in NOISE_NAMES:
                return
            verb = VERB_FOR.get(event)
            if not verb:
                return
            role = role_text(role_id)
            if role in ("slider", "progressbar", "scrollbar"):
                return

            # Collapse repeat-fire (many frameworks emit several per interaction)
            key = (verb, name, role)
            now = time.time()
            if key == self._last[0] and (now - self._last[1]) < self.dedupe_seconds:
                return
            self._last = (key, now)

            self.on_action(hwnd, verb, name.strip(), role)
        except Exception:
            pass

    def _run(self):
        try:
            ole32.CoInitialize(None)
        except Exception:
            pass
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        proc = WINEVENTPROC(self._handle)
        self._proc_ref = proc                     # keep alive
        flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        hooks = []
        for lo, hi in ((EVENT_SYSTEM_MENUSTART, EVENT_SYSTEM_DIALOGSTART),
                       (EVENT_OBJECT_FOCUS, EVENT_OBJECT_SELECTION),
                       (EVENT_OBJECT_VALUECHANGE, EVENT_OBJECT_VALUECHANGE),
                       (EVENT_OBJECT_INVOKED, EVENT_OBJECT_INVOKED)):
            h = user32.SetWinEventHook(lo, hi, None, proc, 0, 0, flags)
            if h:
                hooks.append(h)

        msg = _MSG()
        while not self._stop.is_set():
            r = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)  # PM_REMOVE
            if r:
                if msg.message == 0x0012:         # WM_QUIT
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.02)

        for h in hooks:
            try:
                user32.UnhookWinEvent(h)
            except Exception:
                pass
        try:
            ole32.CoUninitialize()
        except Exception:
            pass


if __name__ == "__main__":
    # Manual smoke test: prints control-level events from whatever you click.
    def show(hwnd, verb, name, role):
        print(f"  [{verb:7}] {role or '?':12} {name}")
    print("Watching accessibility events for 20s — click around any app…")
    s = UIASensor(show)
    s.start()
    time.sleep(20)
    s.stop()
    print("done.")
