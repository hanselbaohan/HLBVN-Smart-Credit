// HLB Connect Smart Credit - customer app (single-page, hash routed).
// Every screen is derived from server state; nothing here decides credit or payment outcomes.
(function () {
  const $screen = document.getElementById("screen");
  const $tabbar = document.getElementById("tabbar");
  let pollTimer = null, tickTimer = null;
  let renderToken = 0;
  const S = { customer: Store.get("sc.customer", null), policy: null, scenarios: [], customers: [], pendingQr: null };
  const ui = { tenure: {}, method: {}, payKey: {}, accepted: {}, tc: {}, sel: {} };
  const LOGO = '<img class="hlb-mark" src="/img/hlb-app-icon.png" alt="HLB">';

  // ------------------------------------------------------------ helpers
  const h = (s) => fmt.esc(s);
  function cust() { return S.customer && S.customer.customer_id; }
  function call(method, path, body, extra) {
    return api(method, path, body, Object.assign({ "X-Demo-Customer": cust() || "" }, extra || {}));
  }
  function toast(msg, isErr) {
    document.querySelectorAll(".toast").forEach(t => t.remove());
    const t = document.createElement("div");
    t.className = "toast" + (isErr ? " err" : "");
    t.textContent = msg;
    document.getElementById("phone").appendChild(t);
    setTimeout(() => t.remove(), 4500);
  }
  function fail(e) { toast(e.message || String(e), true); }
  function setTab(name) { $tabbar.querySelectorAll("a").forEach(a => a.classList.toggle("active", a.dataset.tab === name)); }
  function stopTimers() { if (pollTimer) clearInterval(pollTimer); if (tickTimer) clearInterval(tickTimer); pollTimer = tickTimer = null; }
  function topbar(title, sub, back) {
    return `<div class="topbar">${back ? `<button class="iconbtn" data-back="${h(back)}" aria-label="Back">‹</button>` : ""}
      <h1>${h(title)}${sub ? `<span class="sub">${h(sub)}</span>` : ""}</h1>
      <button class="iconbtn" data-go="#/home" aria-label="Close">✕</button></div>`;
  }
  function busy(btn, on, text) {
    if (!btn) return;
    if (on) { btn.dataset.label = btn.innerHTML; btn.disabled = true; btn.innerHTML = text || "Please wait…"; }
    else { btn.disabled = false; if (btn.dataset.label) btn.innerHTML = btn.dataset.label; }
  }
  function instalments(t) {
    const last = t.instalments[t.instalments.length - 1].amount_vnd;
    return last === t.monthly_instalment_vnd ? `${t.tenure_months} × ${fmt.vnd(t.monthly_instalment_vnd)}`
      : `${t.tenure_months - 1} × ${fmt.vnd(t.monthly_instalment_vnd)} + 1 × ${fmt.vnd(last)}`;
  }
  const DOC_ICON = { payslip: "💵", employment: "🪪", bank_statement: "🏦", additional_income: "📈" };
  function docLabel(t) { return (S.policy.documents.types || {})[t] || t; }

  function milestones(v) {
    const p = v.progress || {};
    const app = v.application;
    const ready = !app || !app.readiness_status ? ["wait", app ? "Pending" : "Not needed"]
      : p.data_ready_for_assessment ? ["yes", "Data ready"] : ["wait", (LABELS.readiness[app.readiness_status] || [app.readiness_status])[0]];
    const appr = !app ? ["wait", "Not needed"] : p.credit_approved === true ? ["yes", "Approved"]
      : p.credit_approved === false ? ["no", app.credit_state === "REJECTED" ? "Rejected" : "Stopped"] : ["wait", "Not yet"];
    const st = v.transaction.state;
    const paid = p.payment_completed ? ["yes", "Completed"] : st === "PAYMENT_FAILED" ? ["no", "Failed"]
      : st === "CANCELLED" ? ["no", "Not paid"] : st === "EXPIRED" ? ["no", "Expired"] : ["wait", "Not yet"];
    if (!app) { ready[1] = "Not needed"; appr[1] = "Not needed"; }
    return `<div class="milestones" aria-label="Status milestones">
      <div class="ms ${ready[0]}"><div class="k">1 · Assessment</div><div class="v">${ready[1]}</div></div>
      <div class="ms ${appr[0]}"><div class="k">2 · Credit</div><div class="v">${appr[1]}</div></div>
      <div class="ms ${paid[0]}"><div class="k">3 · Payment</div><div class="v">${paid[1]}</div></div></div>`;
  }
  function purchaseCard(v, chipHtml) {
    return `<div class="navy-card">
      <div class="row" style="display:flex;gap:10px;align-items:center">
        <div class="avatar" style="background:rgba(255,255,255,.12);border-radius:10px">${h(v.merchant.icon || "🏬")}</div>
        <div style="flex:1"><div class="label">QR payment · ${h(v.merchant.category)}</div>
          <div style="font-weight:700;font-size:16px">${h(v.merchant.name)}</div></div>
        ${chipHtml || '<span class="chip solid-green">QR verified</span>'}
      </div>
      <div class="label" style="margin-top:14px">Amount to pay</div>
      <div class="amount">${fmt.vnd(v.transaction.amount_vnd)}</div>
      <div class="note" style="margin-top:4px">${h(v.transaction.description)}</div>
      <div style="display:flex;justify-content:space-between;margin-top:12px;font-size:11.5px">
        <div><div class="label">Purchase date</div>${fmt.date(v.transaction.created_at)}</div>
        <div style="text-align:right"><div class="label">Txn ref</div><span class="mono">${h(v.transaction.qr_ref)}</span></div>
      </div></div>`;
  }
  // 5-minute countdown for customer-side checkout steps (server is the source of truth).
  function timerBar(v) {
    if (v.seconds_left === null || v.seconds_left === undefined) return "";
    return `<div class="timer ${v.seconds_left < 60 ? "low" : ""}" id="timer"><span>⏱ Complete this step within <b id="tleft">${mmss(v.seconds_left)}</b> or the request expires.</span>
      <button id="expire-now" title="Demo control: simulate the 5-minute timeout">Demo: expire now</button></div>`;
  }
  function mmss(s) { s = Math.max(0, s); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }
  function wireTimer(v) {
    if (v.seconds_left === null || v.seconds_left === undefined) return;
    const end = Date.now() + v.seconds_left * 1000;
    tickTimer = setInterval(() => {
      const left = Math.round((end - Date.now()) / 1000);
      const el = document.getElementById("tleft");
      if (el) el.textContent = mmss(left);
      const box = document.getElementById("timer");
      if (box && left < 60) box.classList.add("low");
      if (left <= 0) { clearInterval(tickTimer); tickTimer = null; setTimeout(refresh, 600); }
    }, 1000);
    const b = document.getElementById("expire-now");
    if (b) b.onclick = async () => {
      if (!await ask("Demo control: simulate the 5-minute timeout now? The checkout will be marked EXPIRED.", "Expire now")) return;
      try { await call("POST", `/api/demo/transactions/${v.transaction.transaction_id}/expire-now`); refresh(); } catch (e) { fail(e); }
    };
  }
  function timelineBlock() {
    return `<div class="card"><details class="details" id="tl"><summary>Status history</summary>
      <ul class="timeline" id="tl-list"><li><span></span><span class="note">Loading…</span></li></ul></details></div>`;
  }
  function wireTimeline(txId) {
    const d = document.getElementById("tl");
    if (!d) return;
    d.addEventListener("toggle", async () => {
      if (!d.open) return;
      try {
        const ev = await call("GET", `/api/transactions/${txId}/audit`);
        document.getElementById("tl-list").innerHTML = ev.map(e => `<li><span>${fmt.time(e.ts)}</span><span><b>${h(e.event_type.replace(/_/g, " ").toLowerCase())}</b>${e.to_state ? ` → ${h(e.to_state)}` : ""}</span></li>`).join("");
      } catch (e) { fail(e); }
    });
  }
  const TC_TEXT = `<p><b>Demo document.</b> Fictional Terms &amp; Conditions for the HLB Connect Smart Credit concept. Not a legal agreement of Hong Leong Bank Vietnam.</p>
    <h4>1. Service</h4><p>Pay Now debits your HLB account. Pay Later finances an eligible QR purchase after a credit assessment; the merchant is paid only after approval, your confirmation of the loan terms and successful (simulated) processing.</p>
    <h4>2. Assessment</h4><p>To assess Pay Later we use information the bank already holds about you (account and transaction history, internal credit records). A transparent rule-based demo engine checks completeness, validity, verification, consistency and evidence; cases needing judgement go to a credit officer. Missing CIC history alone is never a reason to decline.</p>
    <h4>3. Supporting documents</h4><p>We may ask for documents (payslips, employment confirmation, bank statements, other income evidence) to verify your situation. Uploading documents does not guarantee approval.</p>
    <h4>4. Third-party data</h4><p>We only ask to use data from third parties (e.g. an e-wallet partner) when it is genuinely needed, in a separate request you can decline. Declining never leads to automatic rejection.</p>
    <h4>5. Timing &amp; cancellation</h4><p>Unfinished checkouts expire after 5 minutes. You can cancel at any time before payment; cancelled or expired requests are never approved or paid.</p>
    <h4>6. Repayment</h4><p>Repayment terms shown before confirmation are fictional demo terms. Missed instalments are marked overdue and may affect future Smart Credit assessments within this demo. Nothing is reported to the Credit Information Centre (CIC).</p>`;
  const PRIVACY_TEXT = `<p><b>Demo document.</b> Fictional Privacy Policy for this prototype.</p>
    <h4>What we collect</h4><p>Purchase details from the merchant QR, your fictional account and transaction data, internal credit records, documents you choose to upload (in this demo only file name, type and size are recorded; contents are never transmitted or stored).</p>
    <h4>Why</h4><p>To process your payment, assess Pay Later, prevent fraud, service your loan and keep an audit trail.</p>
    <h4>Protection</h4><p>Access is limited to the purpose above; every decision and status change is logged in an append-only audit trail. All data in this prototype is fictional.</p>
    <h4>Your choices</h4><p>You can decline third-party data checks, cancel requests and withdraw permission for a pending request at any time.</p>`;
  async function openDoc(which) { await sheet(which === "privacy" ? "Privacy Policy (demo)" : "Terms & Conditions (demo)", which === "privacy" ? PRIVACY_TEXT : TC_TEXT, [{ label: "Close", value: "x", cls: "primary" }]); }

  // ------------------------------------------------------------ boot
  async function boot() {
    try {
      [S.policy, S.scenarios, S.customers] = await Promise.all([api("GET", "/api/config/policy"), api("GET", "/api/demo/scenarios"), api("GET", "/api/demo/customers")]);
    } catch (e) { $screen.innerHTML = `<div class="empty">${h(e.message)}</div>`; return; }
    renderSideScenarios();
    if (S.customer) {
      try { await refreshMe(); } catch (e) { S.customer = null; Store.del("sc.customer"); }
    }
    window.addEventListener("hashchange", route);
    route();
  }
  async function refreshMe() { S.customer = await api("GET", "/api/demo/customers/" + S.customer.customer_id); Store.set("sc.customer", S.customer); return S.customer; }

  function renderSideScenarios() {
    const box = document.getElementById("side-scenarios");
    box.innerHTML = `<h3>Demo scenarios</h3>` + S.scenarios.map(s =>
      `<div style="margin:6px 0"><a href="#" data-scenario="${s.id}">${s.id}. ${h(s.title)}</a><br><span style="opacity:.75;font-size:12px">${h(s.expect)}</span></div>`).join("");
    box.addEventListener("click", (e) => { const a = e.target.closest("[data-scenario]"); if (a) { e.preventDefault(); startScenario(+a.dataset.scenario); } });
  }
  function startScenario(id) {
    const s = S.scenarios.find(x => x.id === id);
    const c = S.customers.find(x => x.customer_id === s.customer_id);
    S.customer = c; Store.set("sc.customer", c);
    S.pendingQr = s.qr_ref;
    const target = id === 14 ? "#/loans" : id === 12 ? "#/scan" : "#/scan";
    toast(`Scenario ${id}: signed in as ${c.display_name}. ${id === 14 ? "Open a loan and use the demo date controls." : "Tap the highlighted QR."}`);
    location.hash = target;
    route();
  }

  // ------------------------------------------------------------ router
  async function route() {
    stopTimers();
    paint.outcome = "";
    document.querySelectorAll(".toast.err, .modal-wrap").forEach(t => t.remove());
    const token = ++renderToken;
    const [path, query] = (location.hash || "#/home").slice(2).split("?");
    const parts = path.split("/");
    const name = parts[0] || "home";
    if (!S.customer && name !== "signin") { location.hash = "#/signin"; return; }
    $tabbar.style.display = name === "signin" ? "none" : "";
    const fn = { signin: viewSignin, home: viewHome, payments: viewPayments, scan: viewScan, tx: viewTx, activity: viewActivity,
      "smart-credit": viewSmartCredit, profile: viewProfile, loans: viewLoans, notifications: viewNotifications, limit: viewLimit }[name] || viewHome;
    setTab({ scan: "payments", tx: "payments", loans: "smart-credit", limit: "smart-credit", notifications: "home" }[name] || name);
    try { await fn(parts.slice(1), token, new URLSearchParams(query || "")); }
    catch (e) { if (token === renderToken) $screen.innerHTML = `${topbar("Something went wrong", "", "#/home")}<div class="empty">${h(e.message)}</div>`; }
  }
  function refresh() { route(); }
  let lastPainted = "";
  function paint(html, token) {
    if (token !== renderToken) return false;
    const key = location.hash + "|" + (paint.outcome || "");
    $screen.innerHTML = html;
    if (key !== lastPainted) $screen.scrollTop = 0;
    lastPainted = key;
    return true;
  }
  $screen.addEventListener("click", (e) => {
    const doc = e.target.closest("[data-doc]"); if (doc) { e.preventDefault(); openDoc(doc.dataset.doc); return; }
    const go = e.target.closest("[data-go]"); if (go) { e.preventDefault(); location.hash = go.dataset.go; return; }
    const back = e.target.closest("[data-back]"); if (back) { e.preventDefault(); location.hash = back.dataset.back; }
  });
  function poll(v, ms) {
    const sig = (x) => x.transaction.state + "|" + (x.application && x.application.credit_state) + "|" + (x.application && x.application.version);
    const from = sig(v);
    pollTimer = setInterval(async () => {
      try {
        const n = await call("GET", "/api/transactions/" + v.transaction.transaction_id);
        if (sig(n) !== from) { toast("Status updated"); refresh(); }
      } catch (e) { /* keep polling */ }
    }, ms || 3000);
  }

  // ------------------------------------------------------------ sign in (HLB Connect welcome look)
  async function viewSignin(_, token) {
    const sel = Store.get("sc.signinSel", "CUS-001");
    const c0 = S.customers.find(c => c.customer_id === sel) || S.customers[0];
    paint(`<div class="sky">
        <div style="display:flex;justify-content:space-between;font-size:11px;font-weight:700;letter-spacing:.12em"><span>VIETNAM</span><span>🔔</span></div>
        <div class="welcome">WELCOME TO</div>
        <div class="brand-row" style="margin-top:4px"><img class="hlb-mark" style="width:46px;height:46px;border-radius:12px" src="/img/hlb-app-icon.png" alt="HLB">
          <span class="hlb-word">HLB CO<span class="n">N</span>NECT</span></div>
        <div class="tag">YOUR DIGITAL BANK · SMART CREDIT CONCEPT</div>
        <div class="glass"><select id="who" aria-label="Demo customer">${S.customers.map(c => `<option value="${c.customer_id}" ${c.customer_id === c0.customer_id ? "selected" : ""}>${h(c.display_name)}</option>`).join("")}</select>
          <button class="bio" id="bio" aria-label="Sign in with simulated biometrics">◉</button></div>
        <div class="skyline" aria-hidden="true"><i style="left:4%;width:16%;height:70px"></i><i style="left:20%;width:12%;height:105px"></i>
          <i class="tower" style="left:36%;width:17%;height:150px"><b></b></i><i style="left:55%;width:13%;height:120px"></i><i style="left:70%;width:12%;height:84px"></i><i style="left:84%;width:13%;height:60px"></i></div>
      </div>
      <div class="ref-actions">
        <button data-act="qr"><span class="ri red">⌗</span>QR Pay</button>
        <button data-act="loan"><span class="ri">💳</span>Pay Loan</button>
        <button data-act="sc"><span class="ri">✓</span>Smart Credit</button>
      </div>
      <div class="card" style="margin-top:10px"><h3>About this demo customer</h3><div class="note" id="pnote" style="font-size:12.5px">${h(c0.persona_note)}</div>
        <div class="note" style="margin-top:6px">Fictional persona derived from dataset row ${h(c0.derived_from)}. No real credentials, Face ID or banking access.</div></div>
      <div class="btn-stack"><button class="btn primary" id="signin">Sign in to demo →</button></div>
      <div class="alert blue"><span>ⓘ</span><div><b>No real credentials needed</b>Simulated sign-in. Never enter real banking credentials.</div></div>`, token);
    const who = document.getElementById("who");
    who.onchange = () => { Store.set("sc.signinSel", who.value); document.getElementById("pnote").textContent = S.customers.find(c => c.customer_id === who.value).persona_note; };
    const go = async (target) => {
      S.customer = S.customers.find(c => c.customer_id === who.value); Store.set("sc.customer", S.customer);
      await refreshMe(); location.hash = target;
    };
    document.getElementById("signin").onclick = () => go("#/home");
    document.getElementById("bio").onclick = () => go("#/home");
    $screen.querySelectorAll("[data-act]").forEach(b => b.onclick = () => go({ qr: "#/scan", loan: "#/loans", sc: "#/smart-credit" }[b.dataset.act]));
  }

  // ------------------------------------------------------------ home
  async function viewHome(_, token) {
    const [me, hist] = await Promise.all([refreshMe(), call("GET", "/api/transactions")]);
    const cr = me.credit;
    const active = hist.find(v => !["PAYMENT_COMPLETED", "CANCELLED", "EXPIRED"].includes(v.transaction.state));
    paint(`<div class="hero">
        <div class="row"><div class="brand-row">${LOGO}<div class="brand">HLB Connect<small>YOUR DIGITAL BANK</small></div></div>
          <div style="display:flex;gap:8px"><a class="iconbtn rel" href="#/notifications" style="background:rgba(255,255,255,.12);border:0;text-decoration:none" aria-label="Notifications">🔔${me.unread_notifications ? `<span class="badge-dot">${me.unread_notifications}</span>` : ""}</a><a class="avatar" href="#/profile" style="text-decoration:none">${h(me.initials)}</a></div></div>
        <div class="greet">Good morning 👋</div><div class="row"><div class="name">${h(me.display_name)}</div><span class="chip" style="background:rgba(255,255,255,.14);color:#fff">16 Oct 2026</span></div>
      </div>
      <div class="navy-card"><div class="label">HLB Everyday account</div><div class="note">${h(me.account_masked)} · Active · fictional balance</div>
        <div class="label" style="margin-top:12px">Available balance</div><div class="amount">${fmt.vnd(me.balance_vnd)}</div></div>
      ${active ? `<div class="alert amber" style="cursor:pointer" data-go="#/tx/${active.transaction.transaction_id}"><span>⏳</span><div><b>Purchase in progress · ${h(active.merchant.name)}</b>${fmt.vnd(active.transaction.amount_vnd)} · ${outcomeChip(active)} Tap to continue.</div></div>` : ""}
      ${cr.internal_delinquency_flag ? `<div class="alert red" data-go="#/loans" style="cursor:pointer"><span>!</span><div><b>You have a missed instalment</b>Tap to view My Loans and pay.</div></div>` : ""}
      <div class="quick q4">
        <a href="#/scan"><span class="qi red">⌗</span>QR Pay</a>
        <a href="#/loans"><span class="qi">💳</span>Pay Loan</a>
        <a href="#/loans"><span class="qi">📄</span>My Loans</a>
        <a href="#/notifications"><span class="qi rel">🔔${me.unread_notifications ? `<span class="badge-dot">${me.unread_notifications}</span>` : ""}</span>Alerts</a>
        <a href="#/activity"><span class="qi">🕘</span>History</a>
        <a href="#/limit"><span class="qi">⬆</span>Limit</a>
        <a href="#/smart-credit"><span class="qi">✓</span>Smart Credit</a>
        <button disabled><span class="qi">⇆</span>Transfer<span class="soon">n/a</span></button>
      </div>
      ${creditCard(cr)}
      <div class="section-title">Recent transactions <a href="#/activity">View all</a></div>
      ${historyList(hist.slice(0, 4))}
      <div class="alert blue"><span>🛡</span><div><b>A small security reminder</b>HLB will never ask for your password or OTP. Always check the merchant before paying.</div></div>`, token);
  }
  function creditCard(cr) {
    const pct = cr.limit_vnd ? Math.round(100 * cr.outstanding_vnd / cr.limit_vnd) : 0;
    return `<div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><span class="chip blue">Smart Credit · Pay Later</span><span class="chip grey">Demo</span></div>
      <div class="note" style="margin-top:10px">Available to spend with Pay Later</div><div class="amount sm">${fmt.vnd(cr.available_vnd)}</div>
      <div class="bar"><i style="width:${pct}%;background:linear-gradient(90deg,#c8202f,#e65562)"></i></div>
      <div style="display:flex;justify-content:space-between;font-size:11.5px;margin-top:6px" class="note"><span>Used ${fmt.vnd(cr.outstanding_vnd)}</span><span>Limit ${fmt.vnd(cr.limit_vnd)}</span></div>
      <div class="note">${h(cr.label)}.</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px"><button class="btn primary" data-go="#/scan" style="padding:11px">Scan QR</button>
        <button class="btn ghost" data-go="#/limit" style="padding:11px">${cr.open_limit_request ? "Limit request" : "Increase limit"}</button></div></div>`;
  }
  function outcomeChip(v) {
    const map = { PAYMENT_COMPLETED: ["Paid", "green"], PAYMENT_FAILED: ["Payment failed", "red"], UNDER_REVIEW: ["Under review", "amber"],
      MORE_INFO_REQUIRED: ["Action needed", "amber"], APPROVED_TERMS_PENDING: ["Approved · confirm", "blue"], POLICY_STOP: ["Stopped", "red"],
      REJECTED: ["Declined", "red"], CANCELLED: ["Cancelled", "grey"], EXPIRED: ["Expired", "grey"], PROCESSING: ["Processing", "blue"],
      CHECKOUT: ["Checkout", "grey"], TERMS_ACCEPTANCE: ["T&C pending", "grey"], READY_TO_ASSESS: ["Ready to assess", "blue"], ASSESSING: ["Assessing", "blue"] };
    const [l, c] = map[v.outcome] || [v.outcome, "grey"];
    return `<span class="chip ${c}">${l}</span>`;
  }
  function historyList(items) {
    if (!items.length) return `<div class="list"><div class="empty">No transactions yet. Scan a demo QR to start.</div></div>`;
    return `<div class="list">${items.map(v => `<a class="item clickable" href="#/tx/${v.transaction.transaction_id}">
      <span class="ic">${h(v.merchant.icon)}</span>
      <span class="main"><span class="t">${h(v.merchant.name)}</span><span class="s" style="display:block">${fmt.date(v.transaction.created_at)} · ${v.transaction.method === "PAY_LATER" ? "Pay Later" : v.transaction.method === "ACCOUNT" ? "Pay Now" : "Not chosen"}</span></span>
      <span class="r">${v.transaction.state === "PAYMENT_COMPLETED" ? "−" : ""}${fmt.num(v.transaction.amount_vnd)}<br>${outcomeChip(v)}</span></a>`).join("")}</div>`;
  }

  // ------------------------------------------------------------ payments & scan
  async function viewPayments(_, token) {
    const hist = await call("GET", "/api/transactions");
    const active = hist.filter(v => !["PAYMENT_COMPLETED", "CANCELLED", "EXPIRED"].includes(v.transaction.state));
    paint(`<div class="topbar"><h1>Payments</h1><span class="avatar">${h(S.customer.initials)}</span></div>
      <div class="hero" style="padding-top:14px"><div class="greet" style="margin:0">HLB Everyday · ${h(S.customer.account_masked)}</div>
        <div class="row"><div class="name" style="font-size:18px">${h(S.customer.display_name)}</div><div style="text-align:right"><div style="font-size:10px;opacity:.7">BALANCE</div><b>${fmt.num(S.customer.balance_vnd)}</b></div></div>
        <div style="display:flex;gap:8px;margin-top:12px"><a class="chip solid-red" href="#/scan" style="text-decoration:none;padding:7px 12px">⌗ QR Pay</a><a class="chip" href="#/loans" style="background:rgba(255,255,255,.14);color:#fff;padding:7px 12px;text-decoration:none">💳 Pay Loan</a></div></div>
      <div class="section-title">Active purchase sessions</div>
      ${active.length ? historyList(active) : `<div class="list"><div class="empty">No purchase in progress.</div></div>`}
      <div class="card" style="cursor:pointer" data-go="#/scan"><div style="display:flex;gap:12px;align-items:center"><span style="width:44px;height:44px;border-radius:12px;background:var(--red);color:#fff;display:grid;place-items:center;font-size:20px">⌗</span>
        <div style="flex:1"><b>Scan QR to pay</b><div class="note">Pay Now from your account or with HLB Pay Later</div></div><span>›</span></div></div>
      <div class="section-title">Recent payments <a href="#/activity">View all</a></div>${historyList(hist.filter(v => v.transaction.state === "PAYMENT_COMPLETED").slice(0, 3))}`, token);
  }

  async function viewScan(_, token) {
    const qrs = await api("GET", "/api/demo/qr-codes");
    const hi = S.pendingQr;
    paint(`${topbar("Scan & Pay", S.customer.display_name + " · " + S.customer.account_masked, "#/payments")}
      <div style="text-align:center;margin-top:12px"><span class="chip grey">📷 Point camera at any merchant QR code</span></div>
      <div class="viewfinder"><span class="tag">SIMULATED VIEW · CAMERA OFF</span><div class="frame"><i></i><i></i><i></i><i></i></div><div class="scanline"></div>
        <div class="hint">⚡ Demo camera preview · choose a demo QR below</div></div>
      <div class="section-title">Demo merchant QR codes</div>
      <div class="list" id="qrs">${qrs.map(q => `<button class="item clickable" data-payload="${h(q.payload)}" ${q.qr_ref === hi ? 'style="background:#fff4f5;box-shadow:inset 3px 0 0 var(--red)"' : ""}>
        <span class="qrmini" aria-hidden="true"></span>
        <span class="main"><span class="t">${h(q.icon)} ${h(q.merchant_name)}</span><span class="s" style="display:block">${h(q.description)}</span>
          <span class="s mono" style="display:block">${h(q.qr_ref)}${q.pay_later_eligible ? "" : " · Pay Later not available"}${q.simulated_failures ? " · simulated failure ×" + q.simulated_failures : ""}</span></span>
        <span class="r">${fmt.short(q.amount_vnd)}${q.already_paid ? '<br><span class="chip green">Paid</span>' : ""}</span></button>`).join("")}</div>
      <div class="card"><details class="details"><summary>Paste a QR payload (test tampering)</summary>
        <div class="field"><textarea id="raw" rows="3" class="mono" placeholder="HLBQR|v1|…"></textarea></div>
        <button class="btn ghost" style="margin-top:8px" id="rawgo">Read QR</button>
        <div class="note" style="margin-top:6px">The server verifies merchant, amount and simulated signature. Amounts from the app are never trusted.</div></details></div>`, token);
    const go = async (payload, btn) => {
      busy(btn, true, "Reading QR…");
      try {
        const r = await call("POST", "/api/qr/resolve", { payload });
        S.pendingQr = null;
        if (r.resumed) toast("You already started this purchase. Resuming where you left off.");
        location.hash = "#/tx/" + r.view.transaction.transaction_id;
      } catch (e) {
        busy(btn, false);
        if (e.code === "QR_ALREADY_PAID") { toast("Already paid: this QR order was paid before. No second payment was made.", true); if (e.details.transaction_id) setTimeout(() => location.hash = "#/tx/" + e.details.transaction_id, 1200); }
        else fail(e);
      }
    };
    document.getElementById("qrs").onclick = (e) => { const b = e.target.closest("[data-payload]"); if (b) go(b.dataset.payload, b); };
    document.getElementById("rawgo").onclick = (e) => go(document.getElementById("raw").value, e.target);
    if (hi) { const el = document.querySelector(`[data-payload*="${hi}"]`); if (el) el.scrollIntoView({ block: "center" }); }
  }

  // ------------------------------------------------------------ transaction screens
  async function viewTx(params, token) {
    const txId = params[0];
    const v = await call("GET", "/api/transactions/" + txId);
    if (token !== renderToken) return;
    paint.outcome = v.outcome;
    const screens = { TERMS_ACCEPTANCE: scrTC, CHECKOUT: scrCheckout, READY_TO_ASSESS: scrAssess, ASSESSING: scrAssessing,
      UNDER_REVIEW: scrReview, MORE_INFO_REQUIRED: scrMoreInfo, APPROVED_TERMS_PENDING: scrTerms, PROCESSING: scrProcessing,
      PAYMENT_COMPLETED: scrSuccess, PAYMENT_FAILED: scrFailed, POLICY_STOP: scrStopped, REJECTED: scrRejected,
      CANCELLED: scrCancelled, EXPIRED: scrExpired };
    await (screens[v.outcome] || scrCancelled)(v, token);
    wireTimeline(txId);
    wireTimer(v);
  }

  async function doCancel(id, btn, label) {
    if (!await ask(label || "Cancel this purchase? Nothing will be charged.", "Cancel purchase")) return;
    busy(btn, true);
    try { await call("POST", `/api/transactions/${id}/cancel`); refresh(); } catch (e) { busy(btn, false); fail(e); }
  }

  // Step 1 after scanning: ONE mandatory T&C / Privacy checkbox.
  function scrTC(v, token) {
    const id = v.transaction.transaction_id;
    paint(`${topbar("Confirm purchase", "Step 1 · Terms & Conditions", "#/scan")}
      ${purchaseCard(v)}${timerBar(v)}
      <div class="card"><h3>Before you continue</h3>
        <div class="check"><input type="checkbox" id="tc" aria-labelledby="tc-text" ${ui.tc[id] ? "checked" : ""}>
          <span id="tc-text">I have read and agree to the <a href="#" data-doc="tc">Terms &amp; Conditions</a> and <a href="#" data-doc="privacy">Privacy Policy</a>, including the terms governing the collection, processing, and protection of my personal information.</span></div>
        <div class="note">One checkbox for this purchase. If Pay Later is chosen, information the bank already holds is assessed under these terms; no extra consent is asked for internal data.</div></div>
      <div class="btn-stack"><button class="btn primary" id="next" ${ui.tc[id] ? "" : "disabled"}>Continue to payment options</button>
        <button class="btn ghost" id="cancel">Cancel</button></div>${timelineBlock()}`, token);
    const box = document.getElementById("tc"), next = document.getElementById("next");
    box.onchange = () => { ui.tc[id] = box.checked; next.disabled = !box.checked; };
    next.onclick = async () => {
      busy(next, true);
      try { await call("POST", `/api/transactions/${id}/terms-and-conditions`, { accepted: box.checked === true }); refresh(); }
      catch (e) { busy(next, false); fail(e); }
    };
    document.getElementById("cancel").onclick = (e) => doCancel(id, e.target);
  }

  // Step 2: Pay Now or Pay Later (range + available-credit validation, limit-increase CTA).
  function scrCheckout(v, token) {
    const tx = v.transaction, id = tx.transaction_id;
    const range = S.policy.case_ranges.requested_loan_vnd;
    const below = tx.amount_vnd < range.min, above = tx.amount_vnd > range.max;
    const overLimit = !below && !above && tx.amount_vnd > v.credit.available_vnd;
    const plOk = v.merchant.pay_later_eligible && !below && !above && !overLimit;
    const method = ui.method[id] || tx.method || (plOk ? "PAY_LATER" : "ACCOUNT");
    const tenure = ui.tenure[id] || tx.tenure_months || S.policy.repayment_terms.default_tenure;
    const sufficient = v.account.balance_vnd >= tx.amount_vnd;
    const monthly = Math.floor(tx.amount_vnd / tenure / 1000) * 1000;
    const plReason = below ? `Pay Later starts from ${fmt.vnd(range.min)}. This purchase is below the minimum.`
      : above ? `Pay Later is available up to ${fmt.vnd(range.max)} per purchase. This purchase is above the maximum.`
      : overLimit ? `Above your available Pay Later credit (${fmt.vnd(v.credit.available_vnd)}). Shortfall ${fmt.vnd(tx.amount_vnd - v.credit.available_vnd)}.`
      : !v.merchant.pay_later_eligible ? "This merchant isn't eligible for Pay Later in this demo." : "";
    paint(`${topbar("Choose how to pay", "Step 2 · Pay Now or Pay Later", "")}
      ${purchaseCard(v, '<span class="chip solid-green">T&C accepted</span>')}${timerBar(v)}
      <div class="section-title">Choose how to pay</div>
      <div class="pad">
        <div class="option ${method === "ACCOUNT" ? "selected" : ""}" data-m="ACCOUNT" role="button" tabindex="0"><div class="head"><span class="radio"></span><span class="name">Pay Now</span>
          <span class="chip ${sufficient ? "green" : "red"}">${sufficient ? "Sufficient" : "Insufficient"}</span></div>
          <div class="desc">From HLB Everyday ${h(v.account.account_masked)} · Balance ${fmt.vnd(v.account.balance_vnd)}</div></div>
        <div class="option ${method === "PAY_LATER" ? "selected" : ""} ${plOk ? "" : "disabled"}" data-m="PAY_LATER" role="button" tabindex="0" aria-disabled="${!plOk}">
          <div class="head"><span class="radio"></span><span class="name">Pay Later with HLB <span class="chip navy" style="margin-left:4px">Smart Credit</span></span></div>
          <div class="desc">${plOk ? `Available credit ${fmt.vnd(v.credit.available_vnd)} · credit check before approval` : h(plReason)}</div>
          ${method === "PAY_LATER" && plOk ? `<div class="tenures" id="tenures">${S.policy.repayment_terms.offered_tenures.map(t => `<button type="button" data-t="${t}" class="${t === tenure ? "on" : ""}">${t} mo</button>`).join("")}</div>
          <div class="plan"><b style="color:var(--red)">Selected plan · fictional demo terms</b><br>${tenure} × ~${fmt.vnd(monthly)} · ${S.policy.repayment_terms.annual_interest_rate_pct}% interest · ${fmt.vnd(S.policy.repayment_terms.upfront_fee_vnd)} fees<br>
            <span class="note">Illustrative only. Not an approval and not an HLBVN product term.</span></div>` : ""}
        </div>
      </div>
      ${overLimit ? `<div class="alert amber"><span>⬆</span><div><b>Need more Pay Later credit?</b>This purchase is ${fmt.vnd(tx.amount_vnd - v.credit.available_vnd)} above your available credit. Instead of a rejection, you can ask a credit officer to review a higher limit.${v.credit.open_limit_request ? `<br><b>You already have an open request (${h(v.credit.open_limit_request.request_id)}).</b>` : ""}
        <button class="btn primary" style="margin-top:10px;padding:10px" id="raise">${v.credit.open_limit_request ? "View my limit request" : "Request a credit limit increase"}</button></div></div>` : ""}
      <div class="alert blue"><span>🛡</span><div><b>${method === "PAY_LATER" ? "What happens next" : "Pay Now"}</b>${method === "PAY_LATER" ? "We check the information the bank already holds. If something needs verifying we'll tell you why and may ask for documents. Nothing is charged until you confirm the final terms." : "Your account is debited only after you confirm. This is a simulated debit."}</div></div>
      <div class="btn-stack">
        <button class="btn primary" id="next" ${method === "PAY_LATER" && !plOk ? "disabled" : ""}>${method === "PAY_LATER" ? "Continue with Pay Later →" : "Pay Now " + fmt.vnd(tx.amount_vnd)}</button>
        <button class="btn ghost" id="cancel">Cancel payment</button>
      </div>${timelineBlock()}`, token);
    document.querySelectorAll("[data-m]").forEach(b => b.onclick = (e) => { if (e.target.closest("[data-t], #raise") || b.classList.contains("disabled")) return; ui.method[id] = b.dataset.m; refresh(); });
    const ten = document.getElementById("tenures");
    if (ten) ten.onclick = (e) => { const b = e.target.closest("[data-t]"); if (b) { e.stopPropagation(); ui.tenure[id] = +b.dataset.t; refresh(); } };
    const raise = document.getElementById("raise");
    if (raise) raise.onclick = (e) => { e.stopPropagation(); location.hash = `#/limit?tx=${id}&need=${tx.amount_vnd}`; };
    document.getElementById("cancel").onclick = (e) => doCancel(id, e.target);
    document.getElementById("next").onclick = async (e) => {
      const btn = e.target.closest("button");
      busy(btn, true);
      try {
        const r = await call("POST", `/api/transactions/${id}/payment-method`, method === "PAY_LATER" ? { method, tenure_months: tenure } : { method });
        if (method === "ACCOUNT") await doPay(r, btn); else refresh();
      } catch (err) {
        busy(btn, false);
        if (err.code === "CREDIT_LIMIT_EXCEEDED") {
          const c = await sheet("Above your available credit", `<p>${h(err.message)}</p><p>Available: <b>${fmt.vnd(err.details.available_vnd)}</b> · Needed: <b>${fmt.vnd(err.details.amount_vnd)}</b></p><p class="note">A credit officer reviews limit requests. Documents help but don't guarantee approval.</p>`,
            [{ label: "Pay Now instead", value: "now" }, { label: "Request increase", value: "raise", cls: "primary" }]);
          if (c === "raise") location.hash = `#/limit?tx=${id}&need=${err.details.amount_vnd}`;
          if (c === "now") { ui.method[id] = "ACCOUNT"; refresh(); }
        } else fail(err);
      }
    };
  }

  async function doPay(v, btn) {
    const id = v.transaction.transaction_id;
    const k = id + ":" + (v.payments || []).length;  // one key per attempt; double clicks reuse it
    ui.payKey[k] = ui.payKey[k] || uuid();
    busy(btn, true, "Processing…");
    $screen.insertAdjacentHTML("afterbegin", `<div class="card" style="text-align:center"><div class="spinner"></div><p class="note" style="margin-top:10px">Processing simulated ${v.transaction.method === "PAY_LATER" ? "disbursement to merchant" : "account debit"}…</p></div>`);
    $screen.scrollTop = 0;
    try { await call("POST", `/api/transactions/${id}/pay`, {}, { "Idempotency-Key": ui.payKey[k] }); }
    catch (e) { if (e.code !== "PAYMENT_IN_PROGRESS") fail(e); }
    refresh();
  }

  const CHECK_NAMES = ["Data basis (T&C)", "Mandatory information", "Formats and valid ranges", "Transaction consistency",
    "Source verification", "Data freshness", "Related-field consistency", "Profile risk flags", "Credit history (CIC)", "Evidence sufficiency", "Demo approval conditions"];
  function assessingHtml(v, n) {
    return `${topbar("Smart Credit", "Assessment in progress", "")}${purchaseCard(v, '<span class="chip solid-red">Pay Later</span>')}
      <div class="card" style="text-align:center"><div class="spinner"></div><h3 style="margin-top:14px">Checking your credit profile…</h3>
        <div class="note">Rule-based demo engine on fictional internal data. No extra consent needed for internal data.</div></div>
      <div class="card"><ul class="steps">${CHECK_NAMES.map((c, i) => `<li><span class="dot ${i < n ? "done" : i === n ? "run" : ""}">${i < n ? "✓" : i + 1}</span>${c}</li>`).join("")}</ul></div>
      <div class="alert amber"><span>⚠</span><div><b>Merchant payment not yet completed</b>Nothing is charged while the assessment runs.</div></div>`;
  }
  async function scrAssess(v, token) {
    paint(assessingHtml(v, 0), token);
    let n = 0;
    const anim = setInterval(() => { n = Math.min(n + 1, CHECK_NAMES.length - 1); if (token === renderToken) $screen.innerHTML = assessingHtml(v, n); }, 160);
    let err = null;
    try { await Promise.all([call("POST", `/api/applications/${v.application.application_id}/assess`), new Promise(r => setTimeout(r, 1900))]); }
    catch (e) { err = e; }
    clearInterval(anim);
    if (token !== renderToken) return;
    if (err) {
      paint(`${topbar("Smart Credit", "Assessment", "")}<div class="result"><div class="badge red">!</div><h2>We couldn't finish the check</h2><p>${h(err.message)}</p></div>
        <div class="btn-stack"><button class="btn primary" id="retry">Try again</button></div>`, token);
      document.getElementById("retry").onclick = refresh;
      return;
    }
    refresh();
  }
  function scrAssessing(v, token) { paint(assessingHtml(v, 5), token); poll(v, 1500); }

  function reasonsBlock(app) {
    if (!app || !app.reasons_for_customer || !app.reasons_for_customer.length) return "";
    return `<ul class="reasons">${app.reasons_for_customer.map(r => `<li>${h(r)}</li>`).join("")}</ul>`;
  }
  function checksBlock(app) {
    if (!app || !app.checks || !app.checks.length) return "";
    const icon = { PASS: "✓", WARN: "!", INFO: "i", FAIL: "✕" };
    return `<div class="card"><details class="details"><summary>What we checked</summary><ul class="steps" style="margin-top:8px">
      ${app.checks.map(c => `<li><span class="dot ${c.result}">${icon[c.result] || "·"}</span>${h(c.name)}</li>`).join("")}</ul>
      <div class="note">Rule-based demo checks on fictional data. "Data ready" means ready to assess, not approved.</div></details></div>`;
  }
  function docsList(docs, title) {
    if (!docs || !docs.length) return "";
    const st = { UPLOADED: ["Ready to submit", "blue"], SUBMITTED: ["Submitted", "amber"], ACCEPTED: ["Verified", "green"], REVIEWED: ["Reviewed", "grey"], MORE_REQUESTED: ["Replacement requested", "red"] };
    return `<div class="card"><h3>${h(title || "Your documents")}</h3>${docs.map(d => `<div class="doc-row"><span class="di">${DOC_ICON[d.doc_type] || "📄"}</span>
      <span class="dm"><b>${h(docLabel(d.doc_type))}</b><span>${h(d.file_name)} · ${Math.max(1, Math.round((d.size_bytes || 0) / 1024))} KB · ${fmt.time(d.uploaded_at)}</span></span>
      <span class="chip ${(st[d.status] || ["", "grey"])[1]}">${(st[d.status] || [d.status])[0]}</span></div>`).join("")}
      <div class="note" style="margin-top:6px">Simulated upload: only file name, type and size are recorded. File contents never leave your device.</div></div>`;
  }

  function scrReview(v, token) {
    const id = v.transaction.transaction_id, app = v.application;
    paint(`${topbar("Application under review", app.application_id, "")}
      <div class="hero" style="padding:18px"><span class="chip amber">● Additional review required</span>
        <div class="name" style="font-size:24px;margin-top:10px">A person needs to review this</div>
        <div class="note" style="color:#c9d2e6">Payment not completed · No approval yet · No time limit while we review</div></div>
      ${milestones(v)}
      <div class="alert amber"><span>⚠</span><div><b>Merchant payment has not been completed</b>No loan or payment has been made. Don't treat this as approval. You can cancel at any time.</div></div>
      <div class="card"><h3>Why it's being reviewed</h3><div class="note" style="font-size:12.5px">${h(app.decision_message || "")}</div>${reasonsBlock(app)}
        <div class="hr"></div><dl class="kv"><dt>Status</dt><dd><span class="chip amber">Human review required</span></dd>
          <dt>Requested financing</dt><dd>${fmt.vnd(v.transaction.amount_vnd)}</dd><dt>Merchant</dt><dd>${h(v.merchant.name)}</dd>
          <dt>Data check</dt><dd>${chip("readiness", app.readiness_status)}</dd></dl></div>
      ${docsList(app.documents, "Documents received")}
      <div class="card"><h3>What happens next</h3><ul class="steps"><li><span class="dot done">1</span>A credit officer reviews your profile${app.documents.length ? " and documents" : ""}</li>
        <li><span class="dot">2</span>They approve, ask for more documents, or decline</li><li><span class="dot">3</span>If approved, you'll have 5 minutes to confirm the terms</li></ul>
        <div class="note">This screen and your notifications update automatically.</div></div>
      ${checksBlock(app)}
      <div class="btn-stack"><button class="btn ghost" data-go="#/home">Back to home</button>
        <button class="btn link" id="cancel">Cancel request</button><button class="btn link" id="withdraw">Withdraw data permission</button></div>${timelineBlock()}`, token);
    document.getElementById("cancel").onclick = (e) => doCancel(id, e.target, "Cancel this request? The officer review stops and nothing will be charged.");
    document.getElementById("withdraw").onclick = (e) => doWithdraw(id, e.target);
    poll(v);
  }
  async function doWithdraw(id, btn) {
    if (!await ask("Withdraw permission to use your data for this request? Pay Later will stop for this purchase.", "Withdraw")) return;
    busy(btn, true);
    try { await call("POST", `/api/transactions/${id}/consent/withdraw`); refresh(); } catch (e) { busy(btn, false); fail(e); }
  }

  // Customer action needed: missing details, supporting documents, or evidence options (incl. third-party consent).
  function scrMoreInfo(v, token) {
    const id = v.transaction.transaction_id, app = v.application, req = app.info_request || { items: [], options: [] };
    const fieldItems = req.items.filter(i => i.type === "field");
    const docTypes = req.items.filter(i => i.type === "document").map(i => i.document);
    const opts = req.options || [];
    const pending = app.documents.filter(d => d.status === "UPLOADED");
    const allTypes = Object.keys(S.policy.documents.types);
    const isDocs = !fieldItems.length;
    const title = fieldItems.length ? "We need a few details" : req.route === "EVIDENCE_OPTIONS" ? "Help us verify your request" : "Supporting documents needed";
    const why = req.route === "SUPPORTING_DOCUMENTS"
      ? "Your credit profile needs extra verification before we can decide. This is a standard check, not a rejection."
      : req.route === "EVIDENCE_OPTIONS" ? "We don't have enough verified information about your income yet. Choose how you'd like to add it."
      : req.requested_by === "Credit officer" ? "A credit officer asked for more documents." : "";
    paint(`${topbar(title, app.application_id, "")}
      <div class="result"><div class="badge amber">✎</div><h2>${h(title)}</h2><p>${h(req.note || app.decision_message || "")}</p></div>
      ${milestones(v)}
      ${why ? `<div class="alert amber"><span>ℹ</span><div><b>Why further verification is required</b>${h(why)}${reasonsBlock(app)}</div></div>` : ""}
      ${fieldItems.length ? `<div class="card"><h3>Please provide</h3>${fieldItems.map(it => it.field === "customer_profile" ? `<label class="field">Your income profile
          <select id="f-profile"><option value="">Select…</option>${S.policy.case_ranges.customer_profiles.allowed.map(p => `<option>${h(p)}</option>`).join("")}</select></label>` : `<div class="note">${h(it.field)}</div>`).join("")}
        <div class="note" style="margin-top:6px">Your answer is checked again before any decision.</div></div>` : ""}
      ${opts.includes("third_party_consent") ? `<div class="card"><h3>Fastest option</h3><div class="choice"><button id="tp"><b>📱 Let us check partner e-wallet activity</b><span>One-time, simulated partner check. We'll ask for your permission first; you can say no.</span></button></div></div>` : ""}
      ${isDocs && opts.includes("documents") ? `<div class="card" id="docs"><h3>Supporting documents</h3>
        ${(docTypes.length ? docTypes : allTypes).map(t => { const up = app.documents.find(d => d.doc_type === t && d.status !== "REPLACED");
          return `<div class="doc-row"><span class="di">${DOC_ICON[t] || "📄"}</span><span class="dm"><b>${h(docLabel(t))}${docTypes.includes(t) ? ' <span class="chip red">Required</span>' : ""}</b>
            <span>${up ? h(up.file_name) + " · " + (up.status === "UPLOADED" ? "ready to submit" : up.status.toLowerCase().replace("_", " ")) : "Not uploaded"}</span></span>
            <span style="display:grid;gap:4px;justify-items:end"><label class="upl">⤒ ${up ? "Replace" : "Upload"}<input type="file" data-up="${t}" accept=".pdf,.jpg,.jpeg,.png"></label><button class="linkbtn" data-sample="${t}">Use sample file</button></span></div>`; }).join("")}
        <div class="note" style="margin-top:6px">PDF/JPG/PNG up to ${S.policy.documents.max_file_mb} MB. Simulated upload: file contents never leave your device. Uploading documents doesn't guarantee approval.</div></div>` : ""}
      ${docsList(app.documents.filter(d => d.status !== "UPLOADED"), "Previously submitted")}
      <div class="alert amber"><span>⚠</span><div><b>Merchant payment not completed</b>Your purchase is on hold, nothing has been charged and there is no time limit while you gather documents.</div></div>
      <div class="btn-stack">
        ${fieldItems.length ? `<button class="btn primary" id="submit">Submit & continue assessment</button>` : ""}
        ${isDocs && opts.includes("documents") ? `<button class="btn primary" id="submit" ${pending.length ? "" : "disabled"}>Submit ${pending.length || ""} document${pending.length === 1 ? "" : "s"} for review</button>` : ""}
        ${opts.includes("manual_review") ? `<button class="btn ghost" id="manual">I can't provide this · ask for manual review</button>` : ""}
        <button class="btn link" id="cancel">Cancel request</button></div>${timelineBlock()}`, token);
    const appId = app.application_id;
    const upload = async (doc) => {
      try { await call("POST", `/api/applications/${appId}/documents`, doc); toast(`${docLabel(doc.doc_type)} uploaded (simulated).`); refresh(); } catch (e) { fail(e); }
    };
    $screen.querySelectorAll("[data-up]").forEach(inp => inp.onchange = () => {
      const f = inp.files && inp.files[0]; if (!f) return;
      upload({ doc_type: inp.dataset.up, file_name: f.name, mime_type: f.type, size_bytes: f.size });
    });
    $screen.querySelectorAll("[data-sample]").forEach(b => b.onclick = () => upload({ doc_type: b.dataset.sample,
      file_name: `sample_${b.dataset.sample}_${S.customer.initials.toLowerCase()}.pdf`, mime_type: "application/pdf", size_bytes: 180000 + Math.floor(Math.random() * 90000) }));
    const sub = document.getElementById("submit");
    if (sub) sub.onclick = async () => {
      const fields = {}; const p = document.getElementById("f-profile");
      if (p) { if (!p.value) return toast("Please choose your income profile.", true); fields.customer_profile = p.value; }
      busy(sub, true, "Submitting…");
      try { await call("POST", `/api/applications/${appId}/information`, { fields }); refresh(); } catch (err) { busy(sub, false); fail(err); }
    };
    const tp = document.getElementById("tp");
    if (tp) tp.onclick = async () => {
      const c = await sheet("Allow a one-time partner data check?", `<p>To finish assessing this Pay Later request we'd like to check your <b>e-wallet top-up activity (last 6 months)</b> with our partner.</p>
        <p><b>Why:</b> we don't have enough verified income information in the bank's own records.</p><p><b>What we receive:</b> monthly top-up count and its observation period. Nothing else.</p>
        <p><b>If you say no:</b> your request is not rejected. You can upload documents or ask for manual review instead.</p><p class="note">Simulated partner API. No real e-wallet is contacted.</p>`,
        [{ label: "Don't allow", value: "no" }, { label: "Allow check", value: "yes", cls: "primary" }]);
      if (!c) return;
      try { await call("POST", `/api/applications/${appId}/third-party-consent`, { granted: c === "yes" }); toast(c === "yes" ? "Thanks, re-checking with partner data…" : "No problem. Choose documents or manual review."); refresh(); } catch (e) { fail(e); }
    };
    const man = document.getElementById("manual");
    if (man) man.onclick = async () => {
      if (!await ask("Send this request to a credit officer without more data? They may still ask you for documents.", "Request review")) return;
      busy(man, true);
      try { await call("POST", `/api/applications/${appId}/manual-review`); refresh(); } catch (e) { busy(man, false); fail(e); }
    };
    document.getElementById("cancel").onclick = (e) => doCancel(id, e.target, "Cancel this request? Nothing will be charged.");
    poll(v);
  }

  async function scrTerms(v, token) {
    const id = v.transaction.transaction_id, app = v.application;
    const tenure = ui.tenure["t" + id] || app.tenure_months;
    const terms = tenure === app.tenure_months ? app.terms : await call("GET", `/api/applications/${app.application_id}/terms?tenure=${tenure}`);
    const confirmed = app.terms_confirmed_at && tenure === app.tenure_months;
    paint(`${topbar("Final decision", app.application_id, "")}
      <div class="result"><div class="badge green">✓</div><span class="chip green">Approved</span><h2>Financing approved in demo</h2>
        <p>${h(app.decided_by || "")} approved this Pay Later request. <b>Payment is not complete yet</b>: confirm the terms to pay ${h(v.merchant.name)}.</p></div>
      ${timerBar(v)}${milestones(v)}
      <div class="navy-card"><div class="label">Final demo agreement · ${h(app.application_id)}</div>
        <div style="display:flex;justify-content:space-between;margin-top:10px"><div><div class="label">Principal</div><div class="amount sm">${fmt.num(terms.principal_vnd)}</div></div>
          <div style="text-align:right"><div class="label">Monthly</div><div class="amount sm">${terms.tenure_months} × ${fmt.short(terms.monthly_instalment_vnd)}</div></div></div>
        <div class="note" style="margin-top:6px">${h(terms.label)}</div></div>
      <div class="card"><h3>Choose your term</h3><div class="tenures" style="margin-left:0" id="tenures">${S.policy.repayment_terms.offered_tenures.map(t => `<button data-t="${t}" class="${t === tenure ? "on" : ""}">${t} mo</button>`).join("")}</div>
        <div class="schedule">${terms.instalments.slice(0, 6).map(i => `<div class="inst" style="display:block;border:0;padding:0;text-align:center;font-size:10.5px;color:var(--muted)"><b style="display:grid;place-items:center;width:26px;height:26px;margin:0 auto 3px;border-radius:50%;background:var(--red-soft);color:var(--red)">${i.number}</b>${fmt.date(i.due_date).slice(0, 6)}<br>${fmt.short(i.amount_vnd)}</div>`).join("")}</div>
        ${terms.instalments.length > 6 ? `<div class="note" style="text-align:center;margin-top:4px">+ ${terms.instalments.length - 6} more instalments</div>` : ""}
        <div class="hr"></div><dl class="kv"><dt>Merchant</dt><dd>${h(v.merchant.name)}</dd><dt>Amount financed</dt><dd>${fmt.vnd(terms.principal_vnd)}</dd>
          <dt>Interest (demo, p.a.)</dt><dd>${terms.annual_interest_rate_pct}% · ${fmt.vnd(terms.total_interest_vnd)}</dd><dt>Fees (demo)</dt><dd>${fmt.vnd(terms.upfront_fee_vnd)}</dd>
          <dt>Late fee (demo)</dt><dd>${fmt.vnd(S.policy.loan_servicing.late_fee_vnd)}</dd>
          <dt>Instalments</dt><dd>${instalments(terms)}</dd><dt>Total repayment</dt><dd>${fmt.vnd(terms.total_repayable_vnd)}</dd>
          <dt>Due dates</dt><dd>${fmt.date(terms.first_due_date)} – ${fmt.date(terms.last_due_date)}</dd><dt>Account debit today</dt><dd>VND 0</dd></dl>
        <div class="alert blue" style="margin:12px 0 0"><span>ⓘ</span><div>${h(terms.label)}. Values come from a configurable demo policy, not an HLBVN rate, fee or product. Reminders are sent before each due date.</div></div>
        <label class="check"><input type="checkbox" id="accept" ${confirmed || ui.accepted[id] ? "checked" : ""}><span>I accept these <b>fictional demo loan terms</b> and authorise the simulated payment of ${fmt.vnd(terms.principal_vnd)} to ${h(v.merchant.name)}.</span></label></div>
      <div class="btn-stack"><button class="btn primary" id="pay">Confirm & pay ${fmt.vnd(terms.principal_vnd)}<small>Simulated disbursement to merchant</small></button>
        <button class="btn link" id="cancel">Cancel purchase</button></div>${checksBlock(app)}${timelineBlock()}`, token);
    document.getElementById("tenures").onclick = (e) => { const b = e.target.closest("[data-t]"); if (b) { ui.tenure["t" + id] = +b.dataset.t; refresh(); } };
    document.getElementById("accept").onchange = (e) => { ui.accepted[id] = e.target.checked; };
    document.getElementById("cancel").onclick = (e) => doCancel(id, e.target);
    document.getElementById("pay").onclick = async (e) => {
      const btn = e.target.closest("button");
      if (!document.getElementById("accept").checked) return toast("Please tick the box to accept the demo terms first.", true);
      busy(btn, true, "Confirming…");
      try {
        if (!confirmed) await call("POST", `/api/applications/${app.application_id}/terms/confirm`, { tenure_months: tenure, accepted: true });
        await doPay(await call("GET", "/api/transactions/" + id), btn);
      } catch (err) { busy(btn, false); fail(err); refresh(); }
    };
  }

  function scrProcessing(v, token) {
    paint(`${topbar("Processing", v.transaction.transaction_id, "")}<div class="result"><div class="spinner"></div><h2 style="margin-top:16px">Processing simulated payment…</h2>
      <p>Please don't pay again. We'll show the result as soon as the simulated processor responds.</p></div>${milestones(v)}`, token);
    poll(v, 1000);
  }

  function scrSuccess(v, token) {
    const tx = v.transaction, app = v.application, pl = tx.method === "PAY_LATER";
    const succeeded = (v.payments || []).find(p => p.status === "SUCCEEDED");
    paint(`${topbar("Payment confirmation", tx.transaction_id, "")}
      <div class="hero" style="text-align:center;padding:24px"><div style="margin:0 auto;width:62px;height:62px;border-radius:50%;display:grid;place-items:center;font-size:28px;background:#3aa76d;color:#fff">✓</div>
        <div class="name" style="margin-top:10px">Payment completed</div><div class="note" style="color:#c9d2e6">${pl ? "Approval, agreement & simulated processing done" : "Simulated account debit done"}</div>
        <span class="chip" style="background:rgba(255,255,255,.14);color:#fff;margin-top:8px">Ref: ${h(tx.payment_ref)}</span></div>
      ${milestones(v)}
      <div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><div><div class="note">Paid to merchant</div><b>${h(v.merchant.name)}</b></div><span class="chip green">✓ Success</span></div>
        <div class="note" style="margin-top:12px">Total amount paid</div><div class="amount">${fmt.vnd(tx.amount_vnd)}</div>
        <span class="chip ${pl ? "red" : "grey"}">${pl ? "HLB Pay Later" : "Pay Now"}</span> <span class="note">${pl ? "Fully financed · no account debit" : "Debited from " + h(v.account.account_masked)}</span>
        <div class="hr"></div><dl class="kv"><dt>Payment date</dt><dd>${fmt.dt(succeeded && succeeded.completed_at)}</dd>
          ${pl ? `<dt>Loan reference</dt><dd class="mono">${h(v.loan && v.loan.loan_ref)}</dd><dt>Repayment</dt><dd>${instalments(app.terms)}</dd><dt>First due</dt><dd>${fmt.date(app.terms.first_due_date)}</dd>` : ""}
          <dt>Transaction ref</dt><dd class="mono">${h(tx.qr_ref)}</dd><dt>Account funds used</dt><dd>${pl ? "VND 0" : fmt.vnd(tx.amount_vnd)}</dd><dt>Account balance</dt><dd>${fmt.vnd(v.account.balance_vnd)}</dd></dl></div>
      ${pl ? `<div class="alert green"><span>✓</span><div><b>Your loan is in My Loans</b>First instalment due ${fmt.date(app.terms.first_due_date)}. We'll remind you 3 days before and on the due date.</div></div>` : ""}
      <div class="btn-stack">${pl && v.loan ? `<button class="btn primary" data-go="#/loans/${h(v.loan.loan_ref)}">View in My Loans</button>` : ""}<button class="btn ${pl ? "ghost" : "primary"}" data-go="#/home">Back to home</button></div>${timelineBlock()}`, token);
  }

  function scrFailed(v, token) {
    const tx = v.transaction, last = (v.payments || []).slice(-1)[0] || {};
    const maxTries = S.policy.payment_simulation.max_retries + 1;
    paint(`${topbar("Payment not completed", tx.transaction_id, "")}
      <div class="result"><div class="badge red">✕</div><h2>Payment failed</h2><p>${h(last.failure_message || "The simulated payment could not be completed.")} Nothing was paid to the merchant.</p></div>
      ${timerBar(v)}${milestones(v)}
      ${v.application ? `<div class="alert blue"><span>ⓘ</span><div><b>Your Pay Later approval is still valid</b>The credit decision (${h(v.application.credit_state.toLowerCase())}) is separate from this payment attempt. You can retry within the time limit.</div></div>` : ""}
      <div class="card"><dl class="kv"><dt>Merchant</dt><dd>${h(v.merchant.name)}</dd><dt>Amount</dt><dd>${fmt.vnd(tx.amount_vnd)}</dd><dt>Attempts</dt><dd>${v.payments.length} of ${maxTries}</dd><dt>Error</dt><dd class="mono">${h(last.failure_code)}</dd></dl></div>
      <div class="btn-stack"><button class="btn primary" id="retry" ${v.payments.length >= maxTries ? "disabled" : ""}>Retry payment</button><button class="btn ghost" id="cancel">Cancel purchase</button></div>${timelineBlock()}`, token);
    document.getElementById("retry").onclick = (e) => doPay(v, e.target);
    document.getElementById("cancel").onclick = (e) => doCancel(tx.transaction_id, e.target);
  }

  function scrStopped(v, token) {
    const app = v.application;
    paint(`${topbar("Pay Later stopped", app && app.application_id, "")}
      <div class="result"><div class="badge red">⏻</div><h2>We can't continue with Pay Later</h2><p>${h((app && app.decision_message) || "")}</p></div>
      ${milestones(v)}
      <div class="alert red"><span>✕</span><div><b>No payment was made</b>No loan was opened and nothing was paid to ${h(v.merchant.name)}.</div></div>
      <div class="btn-stack"><button class="btn primary" data-go="#/scan">Scan again to pay another way</button><button class="btn ghost" data-go="#/home">Back to home</button></div>${timelineBlock()}`, token);
  }
  function scrRejected(v, token) {
    const app = v.application;
    paint(`${topbar("Final decision", app.application_id, "")}
      <div class="result"><div class="badge red">✕</div><h2>We can't offer suitable financing</h2><p>After reviewing your request, a credit officer could not approve Pay Later for this purchase.</p></div>
      ${milestones(v)}
      <div class="card"><dl class="kv"><dt>Application reference</dt><dd class="mono">${h(app.application_id)}</dd><dt>Amount requested</dt><dd>${fmt.vnd(v.transaction.amount_vnd)}</dd>
        <dt>Status</dt><dd><span class="chip red">Declined</span></dd><dt>Decided by</dt><dd>${h(app.decided_by || "")}</dd></dl></div>
      <div class="alert red"><span>✕</span><div><b>Merchant payment has not been completed</b>No loan has been opened, no financing disbursed and ${h(v.merchant.name)} has not been paid.</div></div>
      <div class="btn-stack"><button class="btn primary" data-go="#/scan">Pay another way</button><button class="btn ghost" data-go="#/home">Back to home</button></div>${timelineBlock()}`, token);
  }
  function scrCancelled(v, token) {
    const reason = { CANCELLED_BY_CUSTOMER: "You cancelled this request.", CONSENT_WITHDRAWN: "You withdrew permission, so Pay Later stopped." }[v.transaction.closure_reason] || "This purchase was cancelled.";
    paint(`${topbar("Request cancelled", v.transaction.transaction_id, "")}
      <div class="result"><div class="badge blue">↺</div><h2>Cancelled</h2><p>${h(reason)} Nothing was charged, and a cancelled request can never be approved or paid later.</p></div>${milestones(v)}
      <div class="btn-stack"><button class="btn primary" data-go="#/scan">Scan a QR</button><button class="btn ghost" data-go="#/home">Back to home</button></div>${timelineBlock()}`, token);
  }
  function scrExpired(v, token) {
    paint(`${topbar("Checkout expired", v.transaction.transaction_id, "")}
      <div class="result"><div class="badge blue">⏱</div><h2>This checkout expired</h2><p>It wasn't completed within 5 minutes. Nothing was charged and it can't be approved or paid. Please scan the merchant QR again to restart.</p></div>${milestones(v)}
      <div class="btn-stack"><button class="btn primary" data-go="#/scan">Restart: scan QR</button><button class="btn ghost" data-go="#/home">Back to home</button></div>${timelineBlock()}`, token);
  }

  // ------------------------------------------------------------ My Loans & Pay Loan
  async function viewLoans(params, token) {
    if (params[0]) return viewLoan(params[0], token);
    const d = await call("GET", "/api/loans");
    paint(`<div class="topbar"><button class="iconbtn" data-back="#/home" aria-label="Back">‹</button><h1>My Loans<span class="sub">Business date ${fmt.date(d.business_date)} (simulated)</span></h1><a class="iconbtn" href="#/notifications" aria-label="Notifications">🔔</a></div>
      ${creditCard(d.credit)}
      <div class="section-title">Pay Later loans</div>
      ${d.loans.length ? d.loans.map(l => `<div class="card" style="cursor:pointer" data-go="#/loans/${h(l.loan_ref)}">
        <div style="display:flex;justify-content:space-between;align-items:center"><b>${h(l.merchant_name)}</b><span class="chip ${l.status === "CLOSED" ? "grey" : l.status === "DEFAULT" ? "red" : l.overdue_count ? "red" : "green"}">${l.status === "CLOSED" ? "Repaid" : l.status === "DEFAULT" ? "Demo default" : l.overdue_count ? "Overdue" : "Active"}</span></div>
        <div class="note mono">${h(l.loan_ref)}</div>
        <div style="display:flex;justify-content:space-between;margin-top:10px"><div><div class="note">Outstanding</div><b>${fmt.vnd(l.outstanding_vnd)}</b></div>
          <div style="text-align:right"><div class="note">${l.next_due ? "Next due " + fmt.date(l.next_due.due_date) : "Fully repaid"}</div><b>${l.next_due ? fmt.vnd(l.next_due.total_due_vnd) : ""}</b></div></div>
        <div class="bar"><i style="width:${Math.round(100 * l.paid_count / l.instalment_count)}%"></i></div><div class="note" style="margin-top:4px">${l.paid_count} / ${l.instalment_count} instalments paid</div>
        ${["ACTIVE", "DEFAULT"].includes(l.status) ? `<button class="btn primary" style="margin-top:10px;padding:11px" data-go="#/loans/${h(l.loan_ref)}">Pay Loan</button>` : ""}</div>`).join("")
        : `<div class="list"><div class="empty">No loans yet. Complete a Pay Later purchase to see it here.</div></div>`}
      ${demoClock(d.business_date)}`, token);
    wireClock();
  }
  function demoClock(bd) {
    return `<div class="card"><h3>Demo date controls</h3><div class="note">Simulates time passing so reminders, due dates and overdue handling can be tested. Current simulated date: <b>${fmt.date(bd)}</b>.</div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-top:10px"><button class="btn ghost" style="padding:9px;font-size:12px" data-adv="1">+1 day</button>
        <button class="btn ghost" style="padding:9px;font-size:12px" data-adv="7">+7 days</button><button class="btn ghost" style="padding:9px;font-size:12px" data-adv="30">+30 days</button></div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-top:6px"><button class="btn ghost" style="padding:9px;font-size:11px" data-jump="-3">3 days before next due</button>
        <button class="btn ghost" style="padding:9px;font-size:11px" data-jump="0">On next due date</button><button class="btn ghost" style="padding:9px;font-size:11px" data-jump="1">1 day overdue</button></div></div>`;
  }
  function wireClock() {
    $screen.querySelectorAll("[data-adv]").forEach(b => b.onclick = async () => {
      try { const r = await call("POST", "/api/demo/clock", { advance_days: +b.dataset.adv }); toast("Demo date: " + fmt.date(r.business_date)); refresh(); } catch (e) { fail(e); }
    });
    $screen.querySelectorAll("[data-jump]").forEach(b => b.onclick = async () => {
      try {
        const d = await call("GET", "/api/loans");
        const nexts = d.loans.filter(l => l.next_due).map(l => l.next_due.due_date).sort();
        if (!nexts.length) return toast("No unpaid instalments to jump to.", true);
        const t = new Date(nexts[0] + "T00:00:00Z"); t.setUTCDate(t.getUTCDate() + +b.dataset.jump);
        const iso = t.toISOString().slice(0, 10);
        if (iso < d.business_date) return toast("That date is in the past for the demo clock.", true);
        await call("POST", "/api/demo/clock", { date: iso }); toast("Demo date: " + fmt.date(iso)); refresh();
      } catch (e) { fail(e); }
    });
  }
  async function viewLoan(ref, token) {
    const l = await call("GET", "/api/loans/" + ref);
    const unpaid = l.instalments.filter(i => i.status !== "PAID");
    const sel = ui.sel[ref] === undefined ? Math.min(1, unpaid.length) : ui.sel[ref];
    const chosen = unpaid.slice(0, sel);
    const total = chosen.reduce((a, i) => a + i.total_due_vnd, 0);
    const lbl = { PAID: ["Paid", "green"], OVERDUE: ["Overdue", "red"], DUE_TODAY: ["Due today", "amber"], DUE_SOON: ["Due soon", "amber"], SCHEDULED: ["Scheduled", "grey"] };
    paint(`<div class="topbar"><button class="iconbtn" data-back="#/loans" aria-label="Back">‹</button><h1>${h(l.merchant_name)}<span class="sub">${h(l.loan_ref)} · ${l.status === "CLOSED" ? "Repaid" : l.status === "DEFAULT" ? "Demo default" : "Active"}</span></h1></div>
      <div class="hero"><div class="label" style="font-size:10.5px;letter-spacing:.1em;opacity:.75">OUTSTANDING · HLB PAY LATER</div><div class="amount">${fmt.vnd(l.outstanding_vnd)}</div>
        <div class="bar" style="background:rgba(255,255,255,.2)"><i style="width:${Math.round(100 * l.paid_count / l.instalment_count)}%"></i></div>
        <div class="row" style="font-size:12px;margin-top:6px"><span>${l.paid_count} / ${l.instalment_count} paid</span><span>${l.overdue_count ? `<span class="chip red">${l.overdue_count} overdue</span>` : "On track"}</span></div></div>
      ${l.overdue_count ? `<div class="alert red"><span>!</span><div><b>Overdue instalment</b>${S.policy.loan_servicing.late_fee_vnd ? "A late fee applies." : "No late fee is configured in this demo."} A missed payment may affect future Smart Credit assessments in this demo. Nothing is reported to CIC.</div></div>` : ""}
      <div class="card"><h3>Instalments</h3>${l.instalments.map(i => { const idx = unpaid.indexOf(i); const on = idx > -1 && idx < sel;
        return `<div class="inst ${i.display_status}">${idx > -1 ? `<input type="checkbox" data-n="${idx}" ${on ? "checked" : ""} aria-label="Select instalment ${i.number}">` : `<span style="width:18px"></span>`}
        <span class="n">${i.status === "PAID" ? "✓" : i.number}</span><span class="m"><b>${fmt.date(i.due_date)}</b><small>${i.status === "PAID" ? "Paid " + fmt.date(i.paid_at) : "Instalment " + i.number}${i.late_fee_vnd ? " · late fee " + fmt.vnd(i.late_fee_vnd) : ""}</small></span>
        <span style="text-align:right"><b>${fmt.vnd(i.total_due_vnd)}</b><br><span class="chip ${lbl[i.display_status][1]}">${lbl[i.display_status][0]}</span></span></div>`; }).join("")}
        <div class="note" style="margin-top:6px">Pay the oldest instalments first. ${h(l.terms_label || "")}.</div></div>
      ${["ACTIVE", "DEFAULT"].includes(l.status) ? `<div class="card"><dl class="kv"><dt>Selected</dt><dd>${chosen.length} instalment(s)</dd><dt>Amount to pay</dt><dd>${fmt.vnd(total)}</dd><dt>Pay from</dt><dd>HLB Everyday ${h(l.account.account_masked)}</dd><dt>Balance</dt><dd>${fmt.vnd(l.account.balance_vnd)}</dd></dl>
        <button class="btn primary" style="margin-top:12px" id="repay" ${chosen.length ? "" : "disabled"}>Pay Loan · ${fmt.vnd(total)}</button></div>` : `<div class="alert green"><span>✓</span><div><b>Loan fully repaid</b>Closed ${fmt.date(l.closed_at)}.</div></div>`}
      <div class="card"><h3>Payment history</h3>${l.repayments.length ? l.repayments.map(r => `<div class="doc-row"><span class="di">${r.status === "SUCCEEDED" ? "✓" : "✕"}</span><span class="dm"><b>${fmt.vnd(r.amount_vnd)}</b><span>${h(r.repayment_id)} · instalment ${r.instalments.join(", ")} · ${fmt.dt(r.created_at)}${r.failure_message ? " · " + h(r.failure_message) : ""}</span></span><span class="chip ${r.status === "SUCCEEDED" ? "green" : "red"}">${r.status === "SUCCEEDED" ? "Paid" : "Failed"}</span></div>`).join("") : '<div class="note">No repayments yet.</div>'}</div>
      ${demoClock(l.business_date)}`, token);
    $screen.querySelectorAll("[data-n]").forEach(c => c.onchange = () => { ui.sel[ref] = c.checked ? +c.dataset.n + 1 : +c.dataset.n; refresh(); });
    wireClock();
    const btn = document.getElementById("repay");
    if (btn) btn.onclick = async () => {
      if (!await ask(`Pay ${fmt.vnd(total)} for ${chosen.length} instalment(s) from your HLB Everyday account? (simulated)`, "Pay now")) return;
      const k = "repay:" + ref + ":" + chosen.map(i => i.number).join(",");
      ui.payKey[k] = ui.payKey[k] || uuid();
      busy(btn, true, "Processing…");
      try {
        await call("POST", `/api/loans/${ref}/repay`, { instalments: chosen.map(i => i.number) }, { "Idempotency-Key": ui.payKey[k] });
        delete ui.sel[ref]; delete ui.payKey[k];
        await sheet("Repayment received", `<p style="font-size:28px;text-align:center;margin:6px 0">✓</p><p style="text-align:center"><b>${fmt.vnd(total)}</b> paid for instalment ${chosen.map(i => i.number).join(", ")}.</p><p class="note" style="text-align:center">Outstanding balance, progress and history are updated.</p>`, [{ label: "Done", value: "ok", cls: "primary" }]);
        refresh();
      } catch (e) { delete ui.payKey[k]; busy(btn, false); fail(e); refresh(); }
    };
  }

  // ------------------------------------------------------------ notifications
  async function viewNotifications(_, token) {
    const d = await call("GET", "/api/notifications");
    const icon = { REMINDER: "⏰", OVERDUE: "!", LOAN: "💳", STATUS: "ⓘ", ACTION: "✎" };
    paint(`<div class="topbar"><button class="iconbtn" data-back="#/home" aria-label="Back">‹</button><h1>Notifications<span class="sub">${d.unread} unread · demo date ${fmt.date(d.business_date)}</span></h1>
        <button class="iconbtn" id="readall" title="Mark all read">✓</button></div>
      <div class="list">${d.items.length ? d.items.map(n => `<a class="notif ${n.read_at ? "" : "unread"}" href="${h(n.link || "#/home")}"><span class="ni ${h(n.kind)}">${icon[n.kind] || "•"}</span>
        <span><b>${h(n.title)}</b><span>${h(n.body || "")}</span><br><span style="font-size:10.5px">${fmt.dt(n.ts)} · ${h(n.kind.toLowerCase())}</span></span></a>`).join("") : '<div class="empty">No notifications yet.</div>'}</div>
      <div class="note" style="margin:12px 18px">Reminders are simulated in-app notifications; no SMS or email is sent. My Loans holds the loan records.</div>`, token);
    document.getElementById("readall").onclick = async () => { try { await call("POST", "/api/notifications/read"); refresh(); } catch (e) { fail(e); } };
  }

  // ------------------------------------------------------------ credit limit increase
  async function viewLimit(_, token, q) {
    const [me, reqs] = await Promise.all([refreshMe(), call("GET", "/api/limit-requests")]);
    const cr = me.credit;
    const open = reqs.find(r => ["DRAFT", "PENDING_REVIEW", "MORE_INFORMATION_REQUIRED"].includes(r.status));
    const need = +(q.get("need") || 0);
    const suggest = Math.min(S.policy.credit_limit.max_limit_vnd, Math.max(cr.limit_vnd + 1000000, need ? Math.ceil((cr.outstanding_vnd + need) / 1000000) * 1000000 : cr.limit_vnd + 5000000));
    const stl = { DRAFT: ["Draft", "grey"], PENDING_REVIEW: ["Under review", "amber"], MORE_INFORMATION_REQUIRED: ["Documents needed", "red"], APPROVED: ["Approved", "green"], REJECTED: ["Not approved", "red"], CANCELLED: ["Cancelled", "grey"] };
    let body = "";
    if (!open) {
      body = `<div class="card"><h3>Request a higher limit</h3>
        ${need ? `<div class="alert amber" style="margin:0 0 10px"><span>ℹ</span><div>Your purchase of <b>${fmt.vnd(need)}</b> is above your available credit (${fmt.vnd(cr.available_vnd)}).</div></div>` : ""}
        <label class="field">New limit requested (VND)<input id="lim" type="number" step="1000000" min="${cr.limit_vnd + 1000000}" max="${S.policy.credit_limit.max_limit_vnd}" value="${suggest}"></label>
        <label class="field">Reason<textarea id="why" rows="2" placeholder="e.g. Planned purchase, higher income">${need ? "Purchase above my current available credit" : ""}</textarea></label>
        <div class="note" style="margin-top:6px">Max ${fmt.vnd(S.policy.credit_limit.max_limit_vnd)} (demo). Next you'll add supporting documents. A credit officer decides; documents don't guarantee approval.</div>
        <button class="btn primary" style="margin-top:12px" id="create">Continue · add documents</button></div>`;
    } else {
      const wanted = ((open.info_request || {}).items || []).map(i => i.document);
      const pending = open.documents.filter(d => d.status === "UPLOADED");
      const editable = ["DRAFT", "MORE_INFORMATION_REQUIRED"].includes(open.status);
      body = `<div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0">Request ${h(open.request_id)}</h3><span class="chip ${stl[open.status][1]}">${stl[open.status][0]}</span></div>
        <dl class="kv" style="margin-top:10px"><dt>Current limit</dt><dd>${fmt.vnd(open.current_limit_vnd)}</dd><dt>Requested</dt><dd>${fmt.vnd(open.requested_limit_vnd)}</dd><dt>Reason</dt><dd>${h(open.reason || "—")}</dd></dl>
        ${open.info_request ? `<div class="alert amber" style="margin:10px 0 0"><span>✎</span><div><b>Credit officer request</b>${h(open.info_request.note || "")}</div></div>` : ""}</div>
        ${editable ? `<div class="card"><h3>Supporting documents</h3>${Object.keys(S.policy.documents.types).map(t => { const up = open.documents.find(d => d.doc_type === t);
          return `<div class="doc-row"><span class="di">${DOC_ICON[t]}</span><span class="dm"><b>${h(docLabel(t))}${wanted.includes(t) ? ' <span class="chip red">Requested</span>' : ""}</b><span>${up ? h(up.file_name) + " · " + up.status.toLowerCase() : "Not uploaded"}</span></span>
          <span style="display:grid;gap:4px;justify-items:end"><label class="upl">⤒ ${up ? "Replace" : "Upload"}<input type="file" data-up="${t}"></label><button class="linkbtn" data-sample="${t}">Use sample file</button></span></div>`; }).join("")}
          <div class="note">Simulated upload: only file name, type and size are recorded.</div>
          <button class="btn primary" style="margin-top:12px" id="submit" ${pending.length || (open.status === "DRAFT" && open.documents.length) ? "" : "disabled"}>Submit for credit-officer review</button></div>` : docsList(open.documents, "Documents submitted")}
        <div class="btn-stack"><button class="btn link" id="cancelreq">Cancel limit request</button></div>`;
    }
    paint(`<div class="topbar"><button class="iconbtn" data-back="${q.get("tx") ? "#/tx/" + h(q.get("tx")) : "#/home"}" aria-label="Back">‹</button><h1>Credit limit<span class="sub">Smart Credit · fictional demo limit</span></h1></div>
      ${creditCard(cr)}${body}
      <div class="section-title">Request history</div>
      <div class="list">${reqs.length ? reqs.map(r => `<div class="item"><span class="ic">⬆</span><span class="main"><span class="t">${fmt.vnd(r.requested_limit_vnd)}</span><span class="s" style="display:block">${h(r.request_id)} · ${fmt.date(r.created_at)}${r.approved_limit_vnd ? " · approved " + fmt.vnd(r.approved_limit_vnd) : ""}${r.decision_note ? " · " + h(r.decision_note) : ""}</span></span><span class="chip ${stl[r.status][1]}">${stl[r.status][0]}</span></div>`).join("") : '<div class="empty">No requests yet.</div>'}</div>`, token);
    const create = document.getElementById("create");
    if (create) create.onclick = async () => {
      busy(create, true);
      try { await call("POST", "/api/limit-requests", { requested_limit_vnd: +document.getElementById("lim").value, reason: document.getElementById("why").value, transaction_id: q.get("tx") || null }); refresh(); }
      catch (e) { busy(create, false); fail(e); }
    };
    if (open) {
      const upload = async (doc) => { try { await call("POST", `/api/limit-requests/${open.request_id}/documents`, doc); toast(docLabel(doc.doc_type) + " uploaded (simulated)."); refresh(); } catch (e) { fail(e); } };
      $screen.querySelectorAll("[data-up]").forEach(inp => inp.onchange = () => { const f = inp.files && inp.files[0]; if (f) upload({ doc_type: inp.dataset.up, file_name: f.name, mime_type: f.type, size_bytes: f.size }); });
      $screen.querySelectorAll("[data-sample]").forEach(b => b.onclick = () => upload({ doc_type: b.dataset.sample, file_name: `sample_${b.dataset.sample}.pdf`, mime_type: "application/pdf", size_bytes: 210000 }));
      const sub = document.getElementById("submit");
      if (sub) sub.onclick = async () => { busy(sub, true); try { await call("POST", `/api/limit-requests/${open.request_id}/submit`); toast("Submitted. A credit officer will review it."); refresh(); } catch (e) { busy(sub, false); fail(e); } };
      const c = document.getElementById("cancelreq");
      if (c) c.onclick = async () => { if (!await ask("Cancel this limit request?", "Cancel request")) return; try { await call("POST", `/api/limit-requests/${open.request_id}/cancel`); refresh(); } catch (e) { fail(e); } };
      if (open.status === "PENDING_REVIEW") pollTimer = setInterval(async () => { try { const r = await call("GET", "/api/limit-requests/" + open.request_id); if (r.status !== open.status) { toast("Limit request updated"); refresh(); } } catch (e) { /* keep */ } }, 3000);
    }
  }

  // ------------------------------------------------------------ activity / smart credit / profile
  async function viewActivity(_, token) {
    const hist = await call("GET", "/api/transactions");
    const f = Store.get("sc.actFilter", "all");
    const show = hist.filter(v => f === "all" || (f === "pl" && v.transaction.method === "PAY_LATER") || (f === "open" && !["PAYMENT_COMPLETED", "CANCELLED", "EXPIRED"].includes(v.transaction.state)));
    paint(`<div class="topbar"><h1>Activity<span class="sub">Purchases, decisions and payment status</span></h1><span class="avatar">${h(S.customer.initials)}</span></div>
      <div class="pad" style="display:flex;gap:6px;margin-top:12px" id="filt">${[["all", "All"], ["pl", "Pay Later"], ["open", "In progress"]].map(([k, l]) => `<button class="chip ${k === f ? "solid-red" : "grey"}" data-f="${k}" style="border:0;padding:6px 12px">${l}</button>`).join("")}</div>
      ${historyList(show)}
      <div class="alert blue"><span>ⓘ</span><div><b>Three separate statuses</b>Each purchase shows whether data was ready for assessment, whether credit was approved, and whether payment completed. Approval alone never means paid.</div></div>`, token);
    document.getElementById("filt").onclick = (e) => { const b = e.target.closest("[data-f]"); if (b) { Store.set("sc.actFilter", b.dataset.f); refresh(); } };
  }

  async function viewSmartCredit(_, token) {
    const [me, loans] = await Promise.all([refreshMe(), call("GET", "/api/loans")]);
    paint(`<div class="hero"><div class="row"><div class="brand-row">${LOGO}<div class="brand">Smart Credit<small>HLB CONNECT · PAY LATER</small></div></div><span class="avatar">${h(me.initials)}</span></div>
        <div class="greet">Welcome back 👋</div><div class="row"><div class="name">${h(me.display_name)}</div><span class="chip solid-green">Demo</span></div></div>
      ${creditCard(me.credit)}
      <div class="quick q4" style="margin-top:14px"><a href="#/scan"><span class="qi red">⌗</span>QR Pay</a><a href="#/loans"><span class="qi">💳</span>Pay Loan</a><a href="#/limit"><span class="qi">⬆</span>Limit</a><a href="#/notifications"><span class="qi">🔔</span>Alerts</a></div>
      <div class="section-title">How QR Pay Later works</div>
      <div class="card"><ul class="steps"><li><span class="dot">1</span>Scan the merchant QR and tick the Terms & Conditions box</li><li><span class="dot">2</span>Choose <b>Pay Later</b> (VND 1.6M – 90.1M, within available credit)</li>
        <li><span class="dot">3</span>We check the bank's own data in seconds; documents only if needed</li><li><span class="dot">4</span>Confirm terms within 5 minutes · simulated payment to merchant</li><li><span class="dot">5</span>Repay in My Loans with reminders before each due date</li></ul></div>
      <div class="section-title">Possible decisions</div>
      <div class="alert green"><span>✓</span><div><b>Instant approval</b>Complete, verified, consistent data with no risk flags and within your available credit.</div></div>
      <div class="alert amber"><span>📄</span><div><b>Verification needed</b>Weaker profile, missed payments or unusual activity: upload documents (or allow a partner check). A credit officer decides. Never an automatic decline.</div></div>
      <div class="alert amber"><span>⏱</span><div><b>Credit officer review</b>Conflicting or unverified data. No time limit while we review.</div></div>
      <div class="alert red"><span>⏻</span><div><b>Policy stop</b>Only for explicit rules, such as an ineligible merchant.</div></div>
      <div class="section-title">My Loans <a href="#/loans">View all</a></div>
      ${loans.loans.length ? `<div class="list">${loans.loans.slice(0, 3).map(l => `<a class="item clickable" href="#/loans/${h(l.loan_ref)}"><span class="ic">💳</span><span class="main"><span class="t">${h(l.merchant_name)}</span><span class="s" style="display:block">${l.paid_count}/${l.instalment_count} paid · ${fmt.vnd(l.outstanding_vnd)} left</span></span><span class="chip ${l.status === "CLOSED" ? "grey" : l.status === "DEFAULT" ? "red" : l.overdue_count ? "red" : "green"}">${l.status === "CLOSED" ? "Repaid" : l.status === "DEFAULT" ? "Demo default" : l.overdue_count ? "Overdue" : "Active"}</span></a>`).join("")}</div>` : `<div class="list"><div class="empty">No loans yet.</div></div>`}
      <div class="section-title">Demo scenarios</div>
      <div class="list">${S.scenarios.map(s => `<button class="item clickable" data-scn="${s.id}"><span class="ic">${s.id}</span><span class="main"><span class="t">${h(s.title)}</span><span class="s" style="display:block">${h(s.expect)}</span></span><span>›</span></button>`).join("")}</div>`, token);
    $screen.querySelectorAll("[data-scn]").forEach(b => b.onclick = () => startScenario(+b.dataset.scn));
  }

  async function viewProfile(_, token) {
    const clock = await api("GET", "/api/demo/clock");
    paint(`<div class="topbar"><h1>Profile</h1></div>
      <div class="card" style="display:flex;gap:12px;align-items:center"><span class="avatar" style="width:46px;height:46px;font-size:16px">${h(S.customer.initials)}</span>
        <div><b>${h(S.customer.display_name)}</b><div class="note">${h(S.customer.customer_id)} · ${h(S.customer.account_masked)} · fictional customer</div></div></div>
      <div class="card"><h3>About this demo customer</h3><div class="note" style="font-size:12.5px">${h(S.customer.persona_note)}</div></div>
      <div class="list">
        <button class="item clickable" id="switch"><span class="ic">⇄</span><span class="main"><span class="t">Switch demo customer</span></span><span>›</span></button>
        <a class="item clickable" href="#/notifications"><span class="ic">🔔</span><span class="main"><span class="t">Notifications</span></span><span>›</span></a>
        <a class="item clickable" href="#" data-doc="tc"><span class="ic">📄</span><span class="main"><span class="t">Terms & Conditions (demo)</span></span><span>›</span></a>
        <a class="item clickable" href="#" data-doc="privacy"><span class="ic">🔒</span><span class="main"><span class="t">Privacy Policy (demo)</span></span><span>›</span></a>
        <a class="item clickable" href="/officer.html" target="_blank" rel="noopener"><span class="ic">🧑‍💼</span><span class="main"><span class="t">Open credit officer workbench</span><span class="s" style="display:block">Demo-only access</span></span><span>↗</span></a>
        <a class="item clickable" href="/api/config/policy" target="_blank" rel="noopener"><span class="ic">⚙</span><span class="main"><span class="t">Demo policy configuration</span><span class="s" style="display:block">Every rule tagged DOCUMENTED / DATASET_PERCENTILE / DEMO_ASSUMPTION / UNRESOLVED</span></span><span>↗</span></a>
      </div>
      ${demoClock(clock.business_date)}
      <div class="alert amber"><span>⚠</span><div><b>Simulation only</b>No connection to real banking systems, CIC, e-wallets or payment rails. The assessment engine is deterministic and rule-based, not a trained or validated AI model.</div></div>`, token);
    document.getElementById("switch").onclick = () => { S.customer = null; Store.del("sc.customer"); location.hash = "#/signin"; };
    wireClock();
  }

  boot();
})();
