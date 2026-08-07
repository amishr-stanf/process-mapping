const toggle = document.getElementById("toggle");
const dot = document.getElementById("dot");
const statusText = document.getElementById("statusText");

function render(enabled) {
  toggle.textContent = enabled ? "On" : "Off";
  toggle.classList.toggle("off", !enabled);
}

chrome.storage.local.get({ enabled: true }, v => render(v.enabled));

toggle.addEventListener("click", () => {
  chrome.storage.local.get({ enabled: true }, v => {
    const next = !v.enabled;
    chrome.storage.local.set({ enabled: next }, () => render(next));
  });
});

// Show whether the local server is reachable.
fetch("http://127.0.0.1:8765/api/status")
  .then(r => r.json())
  .then(s => {
    dot.classList.add("ok");
    statusText.textContent = `server connected · ${s.web_total ?? 0} web events`;
  })
  .catch(() => { statusText.textContent = "server not running (start app.py)"; });
