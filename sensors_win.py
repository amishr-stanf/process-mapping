"""
Windows capture backend (pure ctypes against Win32 -- no dependencies).

Exposes the platform-neutral sensor interface used by logger.py:
    get_foreground()            -> (app_name, window_title)
    read_clipboard(preview)     -> (clip_type, length, sha256, preview)
    idle_seconds()              -> float
    clipboard_sequence()        -> int   (changes whenever the clipboard changes)
"""

import ctypes
import hashlib
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Set restypes/argtypes explicitly -- critical on 64-bit so HANDLE/pointer
# values are not silently truncated to 32 bits (which would crash the reads).
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
kernel32.GetTickCount.restype = wintypes.DWORD

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIB = 8
CF_BITMAP = 2


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def get_foreground():
    """Return (process_name, window_title) for the focused window, or (None, None)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return _process_name(pid.value), title


def _process_name(pid):
    """Best-effort executable name (e.g. 'chrome.exe') for a pid."""
    if not pid:
        return None
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        size = wintypes.DWORD(4096)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(h)
    return None


def idle_seconds():
    """Seconds since the last keyboard/mouse input."""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return (kernel32.GetTickCount() - lii.dwTime) / 1000.0


def clipboard_sequence():
    """A number that changes whenever the clipboard changes."""
    return int(user32.GetClipboardSequenceNumber())


def read_clipboard(preview_chars=80):
    """
    Inspect the clipboard without mutating it.

    Returns (clip_type, length, sha256_hash, preview). Only text content is
    read; for files/images we record the type only. Any failure degrades
    gracefully to (type-or-None, None, None, None) -- never raises.
    """
    try:
        if user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            pass
        elif user32.IsClipboardFormatAvailable(CF_HDROP):
            return "files", None, None, None
        elif user32.IsClipboardFormatAvailable(CF_DIB) or user32.IsClipboardFormatAvailable(CF_BITMAP):
            return "image", None, None, None
        else:
            return None, None, None, None
    except Exception:
        return None, None, None, None

    # Read the unicode text with proper pointer handling.
    text = None
    if not user32.OpenClipboard(None):
        return "text", None, None, None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if handle:
            ptr = kernel32.GlobalLock(handle)
            if ptr:
                try:
                    text = ctypes.wstring_at(ptr)
                finally:
                    kernel32.GlobalUnlock(handle)
    except Exception:
        text = None
    finally:
        user32.CloseClipboard()

    if text is None:
        return "text", None, None, None

    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    preview = None
    if preview_chars > 0:
        preview = " ".join(text.split())[:preview_chars]
    return "text", len(text), digest, preview
