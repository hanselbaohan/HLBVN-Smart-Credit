// Thin API client shared by the customer app and the officer workbench.
// All business state lives on the server; the UI only renders what it returns.
const Store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  del(k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } },
};

class ApiError extends Error {
  constructor(status, code, message, details) { super(message); this.status = status; this.code = code; this.details = details || {}; }
}

async function api(method, path, body, headers) {
  const h = Object.assign({ "Content-Type": "application/json" }, headers || {});
  let res;
  try {
    res = await fetch(path, { method, headers: h, body: body === undefined ? (method === "POST" ? "{}" : undefined) : JSON.stringify(body) });
  } catch (e) {
    throw new ApiError(0, "NETWORK", "Can't reach the demo server. Is it running?");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-json */ }
  if (!res.ok) {
    const err = (data && data.error) || {};
    throw new ApiError(res.status, err.code || "HTTP_" + res.status, err.message || "Request failed", err.details);
  }
  return data;
}

const fmt = {
  vnd(n) { return n === null || n === undefined ? "—" : "VND " + Number(n).toLocaleString("en-US"); },
  num(n) { return n === null || n === undefined ? "—" : Number(n).toLocaleString("en-US"); },
  short(n) { if (n >= 1e6) return (n / 1e6).toFixed(n % 1e6 ? 1 : 0) + "M"; if (n >= 1e3) return Math.round(n / 1e3) + "K"; return String(n); },
  date(s) { if (!s) return "—"; const d = new Date(s); return isNaN(d) ? s : d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }); },
  time(s) { if (!s) return "—"; const d = new Date(s); return isNaN(d) ? s : d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" }); },
  dt(s) { return fmt.date(s) + " · " + fmt.time(s); },
  esc(s) { return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); },
};

function uuid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return "k-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}

// Human labels for server states (customer- and officer-facing).
const LABELS = {
  credit: {
    ASSESSMENT_PENDING: ["Assessment pending", "grey"], STP_ELIGIBLE: ["STP eligible", "blue"],
    MANUAL_REVIEW: ["Manual review", "amber"], POLICY_STOP: ["Policy stop", "red"], APPROVED: ["Approved", "green"],
    REJECTED: ["Rejected", "red"], MORE_INFORMATION_REQUIRED: ["More info required", "amber"],
  },
  readiness: {
    INCOMPLETE: ["Incomplete", "amber"], INVALID_DATA: ["Invalid data", "red"], VERIFY_DATA: ["Verify data", "amber"],
    INSUFFICIENT_EVIDENCE: ["Insufficient evidence", "amber"], DATA_READY: ["Data ready", "green"],
  },
  tx: {
    QR_CREATED: ["QR created", "grey"], PAYMENT_METHOD_SELECTED: ["Method selected", "grey"], CONSENT_PENDING: ["Consent pending", "blue"],
    CONSENT_COMPLETED: ["Consent completed", "blue"], ASSESSMENT_IN_PROGRESS: ["Assessing", "blue"], PENDING_REVIEW: ["Pending", "amber"],
    TERMS_PENDING_CONFIRMATION: ["Awaiting confirmation", "blue"], DISBURSEMENT_PROCESSING: ["Processing", "blue"],
    PAYMENT_COMPLETED: ["Paid", "green"], PAYMENT_FAILED: ["Payment failed", "red"], CANCELLED: ["Cancelled", "grey"],
  },
};
function chip(kind, value) {
  if (!value) return "";
  const [label, color] = (LABELS[kind] && LABELS[kind][value]) || [value, "grey"];
  return `<span class="chip ${color}" title="${fmt.esc(value)}">${fmt.esc(label)}</span>`;
}

// In-app confirmation dialog (keeps the Figma look; avoids blocking native dialogs).
function ask(message, okLabel) {
  return new Promise((resolve) => {
    const host = document.getElementById("phone") || document.body;
    const wrap = document.createElement("div");
    wrap.className = "modal-wrap";
    wrap.innerHTML = `<div class="modal" role="dialog" aria-modal="true"><p>${fmt.esc(message)}</p>
      <div class="modal-actions"><button class="btn ghost" data-v="0">Go back</button><button class="btn primary" data-v="1">${fmt.esc(okLabel || "Confirm")}</button></div></div>`;
    wrap.addEventListener("click", (e) => {
      const b = e.target.closest("[data-v]");
      if (!b && e.target !== wrap) return;
      wrap.remove();
      resolve(!!b && b.dataset.v === "1");
    });
    host.appendChild(wrap);
    wrap.querySelector('[data-v="1"]').focus();
  });
}

// Generic sheet: resolves with the clicked button's value (or null when dismissed).
function sheet(title, bodyHtml, buttons) {
  return new Promise((resolve) => {
    const host = document.getElementById("phone") || document.body;
    const wrap = document.createElement("div");
    wrap.className = "modal-wrap";
    wrap.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-label="${fmt.esc(title)}">
      <h3 style="margin:0 0 8px;font-size:16px">${fmt.esc(title)}</h3><div class="doc-modal">${bodyHtml}</div>
      <div class="modal-actions" style="margin-top:14px;grid-template-columns:${buttons.map(() => "1fr").join(" ")}">
      ${buttons.map(b => `<button class="btn ${b.cls || "ghost"}" data-v="${fmt.esc(b.value)}">${fmt.esc(b.label)}</button>`).join("")}</div></div>`;
    wrap.addEventListener("click", (e) => {
      const b = e.target.closest("[data-v]");
      if (!b && e.target !== wrap) return;
      wrap.remove();
      resolve(b ? b.dataset.v : null);
    });
    host.appendChild(wrap);
  });
}
