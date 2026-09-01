"""
Excel enrichment probe (Tier 2).

The generic accessibility sensor (sensor_uia) already tells us WHICH control was
operated -- "Fill Color", "Number Format", the Find dialog. This probe adds the
thing accessibility cannot expose: the DOCUMENT MODEL. Which workbook, which
sheet, which exact range, and the formula that was entered.

    accessibility  ->  "you changed Number Format"
    this probe     ->  "...on Claims!D2:D5000, and wrote =VLOOKUP(A2,ref!A:D,3,0)"

It attaches to an ALREADY RUNNING Excel (never launches one) and subscribes to
application-level events, so it sees every open workbook at once.

The same COM interface used to read these events can also execute them, which is
why this probe is also the front half of the eventual automation engine.

Requires pywin32. Degrades to a no-op if Excel or pywin32 is unavailable.
"""

import threading
import time

_ATTACH_RETRY = 15.0     # seconds between attempts to find a running Excel
_MAX_TEXT = 120


def _trim(v):
    try:
        s = str(v)
    except Exception:
        return None
    s = " ".join(s.split())
    return s[:_MAX_TEXT] if s else None


def _address(target):
    """Range address. pywin32 exposes Address as a property here, but it is a
    method in some bindings -- handle both rather than losing the event."""
    try:
        a = target.Address
        return a if isinstance(a, str) else a(False, False)
    except Exception:
        try:
            return str(target.Address)
        except Exception:
            return "?"


class ExcelProbe:
    """Emits (verb, control, detail) for edits/saves in a running Excel.

    verbs: cell (a range was changed), formula (a formula was entered),
           save (workbook saved), open (workbook opened)
    """

    def __init__(self, on_action):
        self.on_action = on_action
        self._thread = None
        self._stop = threading.Event()
        self.attached = False

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_running():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="excel-probe", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if not self.is_running():
            return False
        self._stop.set()
        self._thread.join(timeout=4)
        return True

    # -- internals ----------------------------------------------------------
    def _run(self):
        try:
            import pythoncom
            import win32com.client
        except Exception:
            return  # pywin32 not present -> silently disabled

        emit = self.on_action

        class Events:
            """Application-level Excel event sink."""

            def OnSheetChange(self, sheet, target):
                try:
                    wb = sheet.Parent.Name
                    addr = _address(target)
                    formula = None
                    try:
                        f = target.Formula
                        if isinstance(f, str) and f.startswith("="):
                            formula = f
                    except Exception:
                        pass
                    if formula:
                        emit("formula", f"{sheet.Name}!{addr}",
                             f"{wb} {_trim(formula)}")
                    else:
                        val = None
                        try:
                            if target.Count == 1:
                                val = _trim(target.Value)
                        except Exception:
                            pass
                        emit("cell", f"{sheet.Name}!{addr}",
                             f"{wb}{(' = ' + val) if val else ''}")
                except Exception:
                    pass

            def OnWorkbookBeforeSave(self, wb, save_as_ui, cancel):
                try:
                    emit("save", wb.Name, "save_as" if save_as_ui else "save")
                except Exception:
                    pass

            def OnWorkbookOpen(self, wb):
                try:
                    emit("open", wb.Name, None)
                except Exception:
                    pass

        pythoncom.CoInitialize()
        app = None
        last_try = 0.0
        try:
            while not self._stop.is_set():
                if app is None and (time.time() - last_try) > _ATTACH_RETRY:
                    last_try = time.time()
                    try:
                        # GetActiveObject attaches ONLY to a running instance.
                        app = win32com.client.DispatchWithEvents(
                            win32com.client.GetActiveObject("Excel.Application"), Events)
                        self.attached = True
                    except Exception:
                        app = None
                        self.attached = False
                if app is not None:
                    try:
                        pythoncom.PumpWaitingMessages()
                        _ = app.Visible          # cheap liveness probe
                    except Exception:
                        app = None               # Excel closed; retry later
                        self.attached = False
                self._stop.wait(0.25)
        finally:
            app = None
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


if __name__ == "__main__":
    def show(verb, control, detail):
        print(f"  [{verb:8}] {control:28} {detail or ''}")
    print("Open Excel and edit some cells — watching for 25s…")
    p = ExcelProbe(show)
    p.start()
    time.sleep(25)
    p.stop()
    print("attached:", p.attached)
