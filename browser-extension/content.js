// Content script: deterministic capture of meaningful web actions.
// Runs in the top frame only (v1). Sends events to the background worker.
//
// Privacy: password/hidden/sensitive fields are NEVER read (we log that an
// input happened, not its value). All text is truncated; the local server
// stores only a short preview + hash and strips URL query strings.

(function () {
  if (window.top !== window) return; // top frame only for now

  const send = (event) => { try { chrome.runtime.sendMessage({ type: "wm-event", event }); } catch (e) {} };
  const url = () => location.origin + location.pathname;
  const trunc = (s, n = 80) => (s || "").replace(/\s+/g, " ").trim().slice(0, n);

  function descr(el) {
    if (!el || el === document || !el.tagName) return "";
    let d = el.tagName.toLowerCase();
    if (el.id) d += "#" + el.id;
    else if (el.getAttribute && el.getAttribute("name")) d += "[name=" + el.getAttribute("name") + "]";
    else if (typeof el.className === "string" && el.className.trim())
      d += "." + el.className.trim().split(/\s+/).slice(0, 2).join(".");
    const role = el.getAttribute && el.getAttribute("role");
    if (role) d += "[" + role + "]";
    const label = el.getAttribute && (el.getAttribute("aria-label") || el.getAttribute("placeholder"));
    if (label) d += " «" + trunc(label, 30) + "»";
    return d.slice(0, 120);
  }

  const SENSITIVE = /pass|card|cvv|cvc|ssn|secur|otp|one-?time|routing|account.?number/i;
  function sensitive(el) {
    const t = (el.type || "").toLowerCase();
    if (t === "password" || t === "hidden") return true;
    const ac = (el.autocomplete || (el.getAttribute && el.getAttribute("autocomplete")) || "");
    if (/cc-|card|password|one-time|otp/i.test(ac)) return true;
    const idish = (el.id || "") + " " + (el.name || "") + " " + ((el.getAttribute && el.getAttribute("aria-label")) || "");
    return SENSITIVE.test(idish);
  }

  // Clicks on meaningful controls
  document.addEventListener("click", (e) => {
    const el = e.target;
    if (!el || !el.closest) return;
    const node = el.closest("a,button,[role=button],[role=link],[role=tab],[role=menuitem],input[type=submit],input[type=button]") || el;
    const txt = trunc(node.innerText || node.value || "", 40);
    send({ kind: "click", url: url(), title: document.title, target: descr(node), text: txt, len: txt.length });
  }, true);

  // Form entries (change fires on commit / blur)
  document.addEventListener("change", (e) => {
    const el = e.target;
    if (!el) return;
    const tag = (el.tagName || "").toLowerCase();
    if (!/input|textarea|select/.test(tag) && !el.isContentEditable) return;
    if (sensitive(el)) { send({ kind: "input", url: url(), title: document.title, target: descr(el), text: null, len: null }); return; }
    const val = el.isContentEditable ? el.innerText : el.value;
    send({ kind: "input", url: url(), title: document.title, target: descr(el), text: trunc(val), len: (val || "").length });
  }, true);

  // Text selection — the "read" side of read-here-type-there
  let selTimer = null;
  document.addEventListener("selectionchange", () => {
    if (selTimer) clearTimeout(selTimer);
    selTimer = setTimeout(() => {
      const sel = (window.getSelection && window.getSelection().toString()) || "";
      const s = trunc(sel, 80);
      if (s.length >= 8) send({ kind: "select", url: url(), title: document.title, target: "", text: s, len: sel.length });
    }, 700);
  });
})();
