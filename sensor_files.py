"""
File activity sensor (Tier 1, generic).

Closes the "where did that file come from / go to" gap: it links the attachment
you downloaded to the workbook you opened, and the workbook you edited to the
document you saved from it.

Watches a small set of folders (Downloads plus the user's document folders) for
created / modified / renamed files and emits one action per change. Pure stdlib
polling -- no watchdog dependency, no kernel hooks -- which is slower to notice a
change than ReadDirectoryChangesW but is dependency-free, cross-platform and
entirely sufficient at a 3-second cadence for workflow mapping.

Only file METADATA is recorded: name, extension, folder and size. File contents
are never read.
"""

import os
import threading
import time

# Files that are noise: lock files, temp saves, and browser part-downloads.
IGNORE_EXT = {".tmp", ".crdownload", ".part", ".partial", ".lock", ".ini", ".db-journal"}
IGNORE_PREFIX = ("~$", ".~", "~")
INTERESTING_EXT = {
    ".xlsx", ".xlsm", ".xls", ".csv", ".docx", ".doc", ".pdf", ".pptx", ".ppt",
    ".txt", ".json", ".xml", ".zip", ".msg", ".eml",
}
MAX_FILES_PER_DIR = 4000


def default_roots():
    home = os.path.expanduser("~")
    cands = [
        os.path.join(home, "Downloads"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "OneDrive", "Documents"),
        os.path.join(home, "OneDrive", "Desktop"),
    ]
    return [p for p in cands if os.path.isdir(p)]


def _interesting(name):
    low = name.lower()
    if low.startswith(IGNORE_PREFIX):
        return False
    ext = os.path.splitext(low)[1]
    if ext in IGNORE_EXT:
        return False
    return ext in INTERESTING_EXT


def _scan(roots, depth=2):
    """{path: (mtime, size)} for interesting files, shallow-recursive."""
    out = {}
    for root in roots:
        base_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath.count(os.sep) - base_depth >= depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "~", "$"))]
            for fn in filenames:
                if not _interesting(fn):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                    out[p] = (st.st_mtime, st.st_size)
                except OSError:
                    continue
                if len(out) >= MAX_FILES_PER_DIR:
                    return out
    return out


class FileSensor:
    """Emits (verb, filename, folder) for file changes.

    verbs: file_new (appeared -- e.g. a download landing)
           file_save (modified -- e.g. Excel saving a workbook)
    """

    def __init__(self, on_action, roots=None, interval=3.0):
        self.on_action = on_action
        self.roots = roots or default_roots()
        self.interval = interval
        self._thread = None
        self._stop = threading.Event()

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_running() or not self.roots:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="files", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if not self.is_running():
            return False
        self._stop.set()
        self._thread.join(timeout=4)
        return True

    def _run(self):
        try:
            prev = _scan(self.roots)
        except Exception:
            prev = {}
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if self._stop.is_set():
                break
            try:
                cur = _scan(self.roots)
            except Exception:
                continue
            try:
                for path, (mtime, size) in cur.items():
                    old = prev.get(path)
                    if old is None:
                        self.on_action("file_new", os.path.basename(path),
                                       os.path.dirname(path))
                    elif old[0] != mtime and abs(old[1] - size) >= 0:
                        self.on_action("file_save", os.path.basename(path),
                                       os.path.dirname(path))
            except Exception:
                pass
            prev = cur


if __name__ == "__main__":
    def show(verb, name, folder):
        print(f"  [{verb:9}] {name}   ({folder})")
    s = FileSensor(show)
    print("Watching:", ", ".join(s.roots))
    s.start(); time.sleep(20); s.stop()
