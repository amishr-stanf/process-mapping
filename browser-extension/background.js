// Service worker: batches web events from content scripts + tab/nav signals
// and posts them to the local workflow-mapper server. Loopback only; if the
// server isn't running the batch is simply dropped. No AI, no external calls.

const INGEST = "http://127.0.0.1:8765/api/ingest";

let queue = [];
let enabled = true;
let flushTimer = null;

chrome.storage.local.get({ enabled: true }, v => { enabled = v.enabled; });
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.enabled) enabled = changes.enabled.newValue;
});

function stripUrl(u) {
  try { const x = new URL(u); return x.origin + x.pathname; } catch (e) { return u; }
}

function enqueue(ev) {
  if (!enabled || !ev) return;
  ev.ts = ev.ts || Date.now() / 1000;
  queue.push(ev);
  if (queue.length >= 15) flush();
  else scheduleFlush();
}

function scheduleFlush() {
  if (flushTimer) return;
  flushTimer = setTimeout(() => { flushTimer = null; flush(); }, 1200);
}

async function flush() {
  if (!queue.length) return;
  const batch = queue.splice(0, queue.length);
  try {
    await fetch(INGEST, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events: batch }),
    });
  } catch (e) {
    // Server not running — drop this batch rather than grow unbounded.
  }
}

// Keep-alive flush (alarms survive worker sleep better than setInterval).
chrome.alarms.create("flush", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(a => { if (a.name === "flush") flush(); });

// Tab focus -> pageview
chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const t = await chrome.tabs.get(tabId);
    if (t && t.url) enqueue({ kind: "pageview", url: stripUrl(t.url), title: t.title });
  } catch (e) {}
});

// Main-frame navigation -> nav
chrome.webNavigation.onCommitted.addListener(d => {
  if (d.frameId !== 0) return;
  enqueue({ kind: "nav", url: stripUrl(d.url) });
});

// Content-script events (clicks, inputs, selections)
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg && msg.type === "wm-event") enqueue(msg.event);
});
