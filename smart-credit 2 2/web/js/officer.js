// Credit officer workbench. Every action is a server call that validates state,
// persists the decision, updates the customer's records and writes an audit event.
(function () {
  const h = (s) => fmt.esc(s);
  const $ = (id) => document.getElementById(id);
  const st = { code: Store.get("ow.code", ""), officer: Store.get("ow.officer", "officer.nguyen"), tab: Store.get("ow.tab", "apps"),
    selected: Store.get("ow.sel", null), case: null, baselines: null, lastSig: "", policy: null };
  $("code").value = st.code; $("officer").value = st.officer;
  const DOCS = { payslip: "Recent payslip / proof of income", employment: "Employment confirmation or contract", bank_statement: "Bank statement (last 3 months)", additional_income: "Proof of additional income" };

  function headers() { return { "X-Demo-Officer-Code": st.code, "X-Demo-Officer": st.officer }; }
  const call = (m, p, b) => api(m, p, b, headers());
  function flash(msg, err) {
    const d = document.createElement("div");
    d.className = "toast" + (err ? " err" : ""); Object.assign(d.style, { position: "fixed", left: "auto", right: "24px", bottom: "24px", maxWidth: "420px" });
    d.textContent = msg; document.body.appendChild(d); setTimeout(() => d.remove(), 4500);
  }
  $("connect").onclick = () => { st.code = $("code").value.trim(); st.officer = $("officer").value.trim() || "officer.demo"; Store.set("ow.code", st.code); Store.set("ow.officer", st.officer); st.lastSig = ""; loadQueues(true); };
  $("tabs").onclick = (e) => { const b = e.target.closest("[data-tab]"); if (!b) return; st.tab = b.dataset.tab; Store.set("ow.tab", st.tab); st.selected = null; Store.del("ow.sel"); st.lastSig = ""; showTab(); loadQueues(); };
  function showTab() {
    $("q-apps").style.display = st.tab === "apps" ? "" : "none"; $("q-limits").style.display = st.tab === "limits" ? "" : "none";
    document.querySelectorAll("#tabs [data-tab]").forEach(b => b.className = "chip " + (b.dataset.tab === st.tab ? "solid-red" : "grey"));
  }
  showTab();

  async function loadQueues(showErr) {
    if (!st.code) return;
    let q, lq, clock;
    try { [q, lq, clock] = await Promise.all([call("GET", "/api/officer/cases"), call("GET", "/api/officer/limit-requests"), api("GET", "/api/demo/clock")]); }
    catch (e) { if (showErr) flash(e.message, true); $("case").innerHTML = `<div class="panel placeholder">${h(e.message)}</div>`; return; }
    $("bdate").innerHTML = `Demo business date: <b>${fmt.date(clock.business_date)}</b> (drives due dates, reminders, overdue)`;
    $("c-lim").textContent = lq.pending_review.length ? `(${lq.pending_review.length})` : "";
    const sig = JSON.stringify([q, lq, st.tab]);
    if (sig === st.lastSig) return;
    st.lastSig = sig;
    const item = (c) => `<button class="queue-item ${c.application_id === st.selected ? "on" : ""}" data-app="${c.application_id}">
      <div class="r1"><span>${h(c.customer_name)}</span><span>${fmt.short(c.amount_vnd)}</span></div>
      <div class="r2">${h(c.merchant_name)} · <span class="mono">${h(c.application_id)}</span>${c.documents ? ` · 📄 ${c.documents}` : ""}</div>
      <div class="r2">${chip("credit", c.credit_state)} ${chip("readiness", c.readiness_status)} ${chip("tx", c.transaction_state)}</div>
      ${c.rule_ids.length ? `<div class="r2 mono">${c.rule_ids.map(h).join(" · ")}</div>` : ""}</button>`;
    const litem = (r) => `<button class="queue-item ${r.request_id === st.selected ? "on" : ""}" data-lim="${r.request_id}">
      <div class="r1"><span>${h(r.display_name)}</span><span>${fmt.short(r.requested_limit_vnd)}</span></div>
      <div class="r2">${fmt.short(r.current_limit_vnd)} → ${fmt.short(r.requested_limit_vnd)} · <span class="mono">${h(r.request_id)}</span></div>
      <div class="r2"><span class="chip ${r.status === "APPROVED" ? "green" : r.status === "REJECTED" ? "red" : "amber"}">${h(r.status.replace(/_/g, " ").toLowerCase())}</span></div></button>`;
    $("q-pending").innerHTML = q.pending_review.map(item).join("") || `<div class="note">No cases waiting. Run a review scenario in the customer app.</div>`;
    $("q-await").innerHTML = q.awaiting_customer.map(item).join("") || `<div class="note">None.</div>`;
    $("q-decided").innerHTML = q.decided.slice(0, 15).map(item).join("") || `<div class="note">None.</div>`;
    $("c-pending").textContent = q.pending_review.length; $("c-await").textContent = q.awaiting_customer.length; $("c-decided").textContent = q.decided.length;
    $("q-lpending").innerHTML = lq.pending_review.map(litem).join("") || `<div class="note">No limit requests waiting.</div>`;
    $("q-lawait").innerHTML = lq.awaiting_customer.map(litem).join("") || `<div class="note">None.</div>`;
    $("q-ldecided").innerHTML = lq.decided.slice(0, 15).map(litem).join("") || `<div class="note">None.</div>`;
    $("c-lpending").textContent = lq.pending_review.length; $("c-lawait").textContent = lq.awaiting_customer.length; $("c-ldecided").textContent = lq.decided.length;
    if (st.selected) (st.tab === "limits" ? loadLimit : loadCase)(st.selected, true); else showOverview();
  }

  document.querySelector("aside").addEventListener("click", (e) => {
    const b = e.target.closest("[data-app], [data-lim]"); if (!b) return;
    st.selected = b.dataset.app || b.dataset.lim; Store.set("ow.sel", st.selected);
    document.querySelectorAll(".queue-item").forEach(x => x.classList.toggle("on", (x.dataset.app || x.dataset.lim) === st.selected));
    (b.dataset.lim ? loadLimit : loadCase)(st.selected);
  });

  async function showOverview() {
    if (!st.baselines) { try { st.baselines = await api("GET", "/api/reference/dataset"); } catch (e) { st.baselines = { error: e.message }; } }
    $("case").innerHTML = `<div class="panel placeholder">Select a ${st.tab === "limits" ? "limit request" : "case"} from the queue.</div>` + baselinePanel();
  }

  function baselinePanel() {
    const b = st.baselines;
    if (!b || b.error) return `<div class="panel"><h2>Dataset reference</h2><div class="note">${h(b ? b.error : "Loading…")}</div></div>`;
    const f = b.findings, base = b.baselines_reference_only, rp = b.readiness_replay;
    return `<div class="panel"><h2>Dataset reference · ${fmt.num(b.rows)} records <span class="chip grey">Reference only · not used in live decisions</span></h2>
      <div class="grid3">
        <div class="stat"><div class="k">has_observable_tx = 0 with tx values</div><div class="v">${fmt.num(f.has_observable_tx_0_with_populated_tx_fields)}</div><div class="note">→ VERIFY_DATA</div></div>
        <div class="stat"><div class="k">rapid_drain_flag = 1</div><div class="v">${fmt.num(f.rapid_drain_flag_1)}</div><div class="note">→ documents + review, never auto-decline</div></div>
        <div class="stat"><div class="k">Default label w/o approved amount</div><div class="v">${fmt.num(f.is_default_90d_1_with_non_positive_approved_amount)}</div><div class="note">All defaults; label unusable</div></div>
        <div class="stat"><div class="k">Unsupported tenure values</div><div class="v">${fmt.num(f.tenure_outside_supported_list)}</div><div class="note">${f.tenure_outside_supported_values.map(x => x[0] + "m×" + x[1]).join(", ")}</div></div>
        <div class="stat"><div class="k">Requested amount range</div><div class="v" style="font-size:14px">${fmt.num(f.requested_loan_vnd_min)} – ${fmt.num(f.requested_loan_vnd_max)}</div><div class="note">1 row above the 90,100,000 limit</div></div>
        <div class="stat"><div class="k">No formal CIC history</div><div class="v">${fmt.num(f.no_formal_cic_history)}</div><div class="note">Never a rejection reason alone</div></div>
      </div>
      <h2 style="margin-top:16px">Recorded process baselines <span class="chip amber">Existing process · not Smart Credit results</span></h2>
      <div class="grid3"><div class="stat"><div class="k">Manual decision (median)</div><div class="v">${base.manual_decision_days_median} business days</div></div>
        <div class="stat"><div class="k">Existing STP decision (median)</div><div class="v">${base.stp_decision_minutes_median} minutes</div></div>
        <div class="stat"><div class="k">Cost per application (median)</div><div class="v" style="font-size:14px">Manual ${fmt.vnd(base.manual_review_cost_vnd_median)}<br>STP ${fmt.vnd(base.stp_review_cost_vnd_median)}</div></div></div>
      <div class="note" style="margin-top:8px">${h(base.note)} Smart Credit's own turnaround and cost have not been measured.</div>
      <h2 style="margin-top:16px">Readiness engine replay (pre-assessment fields only)</h2>
      <table class="t"><tr><th>Status</th><th>Records</th></tr>${Object.entries(rp.status_counts).map(([k, v]) => `<tr><td>${chip("readiness", k)}</td><td>${fmt.num(v)}</td></tr>`).join("")}</table>
      <div class="note" style="margin-top:6px">${h(rp.note)}</div></div>`;
  }

  function profilePanel(p) {
    const ri = p.risk_indicators, rh = p.repayment_history, cr = p.credit;
    const opTxt = { eq: "=", lt: "<", gt: ">" };
    return `<div class="grid2"><div class="panel"><h2>Credit profile & risk indicators</h2>
        <div class="grid3"><div class="stat"><div class="k">Limit (fictional)</div><div class="v" style="font-size:14px">${fmt.vnd(cr.limit_vnd)}</div></div>
          <div class="stat"><div class="k">Outstanding</div><div class="v" style="font-size:14px">${fmt.vnd(cr.outstanding_vnd)}</div></div>
          <div class="stat"><div class="k">Available</div><div class="v" style="font-size:14px">${fmt.vnd(cr.available_vnd)}</div></div></div>
        <table class="t" style="margin-top:10px"><tr><th>Rule</th><th>Indicator</th><th>Value</th><th>Demo rule</th><th>Basis</th><th></th></tr>
        ${ri.map(r => `<tr><td class="mono">${h(r.rule_id)}</td><td class="mono">${h(r.field)}</td><td>${r.value === null || r.value === undefined ? '<span class="note">n/a</span>' : h(r.value)}</td><td class="mono">${opTxt[r.op]} ${h(r.threshold)}</td><td><span class="chip grey">${h(r.source)}</span></td><td class="${r.triggered ? "res-WARN" : "res-PASS"}">${r.triggered ? "FLAG" : "ok"}</td></tr>`).join("")}</table>
        <div class="note" style="margin-top:6px">Flags trigger document verification and officer review only, never an automatic decline. Percentile cut-offs describe the dataset; they are not validated risk thresholds.</div></div>
      <div class="panel"><h2>Repayment history (internal, simulated)</h2>
        <div class="grid3"><div class="stat"><div class="k">Loans</div><div class="v">${rh.loans.length}</div></div><div class="stat"><div class="k">Overdue instalments</div><div class="v ${rh.overdue_instalments ? "res-FAIL" : ""}">${rh.overdue_instalments}</div></div>
          <div class="stat"><div class="k">Missed-payment flag</div><div class="v ${rh.internal_delinquency_flag ? "res-FAIL" : ""}">${rh.internal_delinquency_flag ? "Yes (RF-01)" : "No"}</div></div></div>
        ${rh.loans.length ? `<table class="t" style="margin-top:10px"><tr><th>Loan</th><th>Merchant</th><th>Paid</th><th>Outstanding</th><th>Status</th></tr>${rh.loans.map(l => `<tr><td class="mono">${h(l.loan_ref)}</td><td>${h(l.merchant_name)}</td><td>${l.paid_count}/${l.instalment_count}</td><td>${fmt.vnd(l.outstanding_vnd)}</td><td>${l.overdue_count ? '<span class="chip red">overdue</span>' : h(l.status.toLowerCase())}</td></tr>`).join("")}</table>` : '<div class="note" style="margin-top:8px">No Smart Credit loans yet.</div>'}
        <div class="note" style="margin-top:6px">${h(rh.note)}</div></div></div>`;
  }
  function docsPanel(docs, title) {
    const stc = { UPLOADED: "blue", SUBMITTED: "amber", ACCEPTED: "green", REVIEWED: "grey", MORE_REQUESTED: "red", REPLACED: "grey" };
    return `<div class="panel"><h2>${h(title || "Submitted documents")} <span class="chip grey">Simulated upload · metadata only</span></h2>
      ${docs.length ? `<table class="t"><tr><th>ID</th><th>Type</th><th>File</th><th>Size</th><th>Uploaded</th><th>Status</th></tr>${docs.map(d => `<tr><td class="mono">${h(d.doc_id)}</td><td>${h(DOCS[d.doc_type] || d.doc_type)}</td><td>${h(d.file_name)}</td><td>${Math.max(1, Math.round((d.size_bytes || 0) / 1024))} KB</td><td>${fmt.dt(d.uploaded_at)}</td><td><span class="chip ${stc[d.status] || "grey"}">${h(d.status.replace("_", " ").toLowerCase())}</span>${d.replaced_by ? `<div class="note">→ ${h(d.replaced_by)}</div>` : ""}</td></tr>`).join("")}</table>` : '<div class="note">No documents.</div>'}
      <div class="note" style="margin-top:6px">File contents are never transmitted in this prototype; verify against the declared type and name.</div></div>`;
  }
  function docPicker() {
    return `<div class="field">Documents to request<div style="display:flex;flex-wrap:wrap;gap:10px;font-weight:500">${Object.entries(DOCS).map(([k, l], i) => `<label style="display:flex;gap:6px;align-items:center"><input type="checkbox" class="reqdoc" value="${k}" ${i === 0 ? "checked" : ""}>${h(l)}</label>`).join("")}</div></div>`;
  }

  async function loadCase(appId, quiet) {
    let c;
    try { c = await call("GET", "/api/officer/cases/" + appId); }
    catch (e) { if (e.status === 404) { st.selected = null; Store.del("ow.sel"); showOverview(); return; } if (!quiet) flash(e.message, true); return; }
    const draft = $("note") ? $("note").value : "";
    st.case = c;
    renderCase(c, draft);
  }

  function renderCase(c, draft) {
    const a = c.application, tx = c.transaction, rd = c.readiness || {}, rt = c.routing || {}, inp = c.inputs || {};
    const notUsed = rd.fields_not_used || {};
    const groups = [["Validation errors", rd.validation_errors], ["Verification warnings", rd.verification_warnings], ["Evidence gaps", rd.evidence_gaps], ["Risk flags / review signals", rd.review_reasons]];
    $("case").innerHTML = `
      <div class="panel"><div class="case-head">
        <div><div class="note mono">${h(a.application_id)} · ${h(tx.transaction_id)} · v${a.version}</div><div class="who">${h(c.customer.display_name)}</div>
          <div class="note">${h(c.customer.customer_id)} · fictional, derived from dataset row ${h(c.customer.derived_from)}${c.customer.fixture_adjustment ? " · adjusted: " + h(c.customer.fixture_adjustment) : ""}</div>
          <div class="note">T&amp;C ${h(tx.tc_version || "—")} accepted ${tx.tc_accepted_at ? fmt.dt(tx.tc_accepted_at) : "—"}</div></div>
        <div style="text-align:right"><div class="amount sm">${fmt.vnd(tx.amount_vnd)}</div><div class="note">${h(c.merchant.icon)} ${h(c.merchant.name)} · ${h(c.merchant.category)}</div><div class="note mono">${h(tx.qr_ref)}</div></div></div>
        <div class="grid3" style="margin-top:14px">
          <div class="stat"><div class="k">Credit decision</div><div class="v">${chip("credit", a.credit_state)}</div><div class="note">${h(a.decided_by || "")}</div></div>
          <div class="stat"><div class="k">Data readiness</div><div class="v">${chip("readiness", a.readiness_status)}</div><div class="note">Assessment runs: ${a.assessment_count}</div></div>
          <div class="stat"><div class="k">Payment transaction</div><div class="v">${chip("tx", tx.state)}</div><div class="note">${h(tx.closure_reason || tx.payment_ref || "")}</div></div></div>
        <div class="grid3" style="margin-top:12px"><div class="stat"><div class="k">Requested · tenure</div><div class="v" style="font-size:14px">${fmt.vnd(a.requested_loan_vnd)} · ${a.tenure_months} mo</div></div>
          <div class="stat"><div class="k">Available credit</div><div class="v" style="font-size:14px">${fmt.vnd(c.profile.credit.available_vnd)}</div></div>
          <div class="stat"><div class="k">Route</div><div class="v" style="font-size:14px">${h(rt.route || "—")}</div><div class="note mono">${(rt.state_path || []).join(" → ")}</div></div></div>
      </div>

      <div class="panel"><h2>Decision ${c.can_decide ? '<span class="chip amber">Action required</span>' : '<span class="chip grey">Read-only</span>'}</h2>
        ${c.can_decide ? `<label class="field">Officer note (required for reject / request info)<textarea id="note" rows="2" placeholder="e.g. Payslip matches declared income (simulated)">${h(draft || "")}</textarea></label>
          ${docPicker()}
          <div class="actions"><button class="btn green" data-act="approve">✓ Approve</button><button class="btn amber" data-act="request_info">✎ Request documents</button><button class="btn primary" data-act="reject">✕ Reject</button></div>
          <div class="note" style="margin-top:8px">Approve gives the customer a new 5-minute window to confirm terms; it does not pay. Reject cancels with no disbursement. Checked against case version v${a.version}; reviews never expire.</div>`
        : `<div class="note">${a.credit_state === "MANUAL_REVIEW" ? "The customer's transaction is no longer pending (" + h(tx.state) + "); it cannot be approved or paid." : a.credit_state === "MORE_INFORMATION_REQUIRED" ? "Waiting for the customer to provide documents/details." : "Decisions are only available while the case is in MANUAL_REVIEW."}</div>`}
      </div>
      ${profilePanel(c.profile)}
      ${docsPanel(c.documents)}
      <div class="grid2">
        <div class="panel"><h2>Reasons for review / routing</h2>
          ${(rt.reasons || []).length ? rt.reasons.map(r => `<div class="reason"><span class="rid">${h(r.rule_id)}</span>${h(r.message)}</div>`).join("") : '<div class="note">—</div>'}
          ${(rt.approval_conditions || []).length ? `<table class="t" style="margin-top:8px"><tr><th>Approval condition (demo)</th><th>Result</th></tr>${rt.approval_conditions.map(x => `<tr><td><span class="mono">${h(x.id)}</span> ${h(x.label)}${x.detail ? `<div class="note">${h(x.detail)}</div>` : ""}</td><td class="${x.passed ? "res-PASS" : "res-FAIL"}">${x.passed ? "PASS" : "FAIL"}</td></tr>`).join("")}</table>` : ""}
          <div class="note" style="margin-top:8px">${h(rd.explanation || "")}</div>
          ${groups.filter(g => (g[1] || []).length).map(g => `<h2 style="margin-top:12px;font-size:13px">${g[0]}</h2>${g[1].map(f => `<div class="reason"><span class="rid">${h(f.rule_id)}</span>${h(f.message)}</div>`).join("")}`).join("")}
          ${c.info_request ? `<h2 style="margin-top:12px;font-size:13px">Latest information request</h2><div class="note">${h(c.info_request.requested_by)} · ${fmt.dt(c.info_request.ts)}: ${h(c.info_request.note || "")}<br>${(c.info_request.items || []).filter(i => i.type !== "evidence_options").map(i => h(i.label || i.field)).join(", ")}${c.info_request.third_party_declined ? "<br>Customer declined third-party data check." : ""}</div>` : ""}
        </div>
        <div class="panel"><h2>Validation & verification checks</h2>
          <table class="t"><tr><th>#</th><th>Check</th><th>Result</th></tr>${(rd.checks_performed || []).map(ch => `<tr><td class="mono">${h(ch.check_id)}</td><td>${h(ch.name)}<div class="note">${h(ch.detail)}</div></td><td class="res-${ch.result}">${h(ch.result)}</td></tr>`).join("")}</table>
          <div class="note" style="margin-top:6px">Policy ${h(rd.policy_version || "")} · evidence paths met: ${(rd.evidence_paths_met || []).join(", ") || "none"}</div></div>
      </div>
      <div class="grid2">
        <div class="panel"><h2>Assessment inputs (pre-assessment fields only)</h2>
          <table class="t"><tr><th>Field</th><th>Value</th><th></th></tr>${Object.entries(inp.applicant || {}).map(([k, v]) => `<tr><td class="mono">${h(k)}</td><td>${v === null ? '<span class="note">missing</span>' : h(typeof v === "number" && v > 1000 ? fmt.num(v) : v)}</td><td>${notUsed[k] ? `<span class="chip amber" title="${h(notUsed[k])}">not used</span>` : ""}</td></tr>`).join("")}</table>
          <h2 style="margin-top:12px;font-size:13px">Data sources (simulated)</h2>
          <table class="t"><tr><th>Source</th><th>Verified</th><th>Observed</th><th>Method</th></tr>${Object.entries(inp.sources || {}).map(([k, s]) => `<tr><td class="mono">${h(k)}</td><td class="${s.verified ? "res-PASS" : "res-WARN"}">${s.verified ? "yes" : "pending"}</td><td>${h(s.observed_at || "—")}</td><td class="note">${h(s.method || "")}</td></tr>`).join("")}</table></div>
        <div class="panel"><h2>Consent & data basis</h2>
          <table class="t"><tr><th>Recorded</th><th>Kind</th><th>Granted</th><th>Categories</th></tr>${c.consents.map(x => `<tr><td>${fmt.dt(x.recorded_at)}${x.withdrawn_at ? `<div class="res-FAIL">withdrawn ${fmt.time(x.withdrawn_at)}</div>` : ""}</td><td>${h(x.kind)}<div class="note">${h(x.purpose || "")}</div></td><td class="${x.granted ? "res-PASS" : "res-FAIL"}">${x.granted ? "yes" : "declined"}</td><td>${x.categories.map(h).join(", ")}</td></tr>`).join("")}</table>
          <h2 style="margin-top:14px">Decision history</h2>
          <table class="t"><tr><th>Time</th><th>Actor</th><th>Transition</th><th>Note</th></tr>${c.decisions.map(d => `<tr><td>${fmt.time(d.ts)}</td><td>${h(d.actor_type)}<div class="note">${h(d.actor_id)}</div></td><td class="mono">${h(d.from_state)} → ${h(d.to_state)}<div class="note">${h(d.action)} ${d.rule_ids.join(" ")}</div></td><td>${h(d.note || "")}</td></tr>`).join("")}</table></div>
      </div>
      ${auditPanel(c.audit)}`;
    document.querySelectorAll("[data-act]").forEach(b => b.onclick = () => decide(b.dataset.act));
  }
  function auditPanel(audit) {
    return `<div class="panel"><h2>Audit log (append-only) · ${audit.length} events</h2>
      <table class="t"><tr><th>#</th><th>Time</th><th>Actor</th><th>Event</th><th>State</th><th>Rules / details</th></tr>${audit.map(e => `<tr><td class="mono">${e.event_id}</td><td>${fmt.time(e.ts)}</td><td>${h(e.actor_type)}<div class="note">${h(e.actor_id || "")}</div></td><td><b>${h(e.event_type)}</b></td><td class="mono">${e.from_state ? h(e.from_state) + " → " : ""}${h(e.to_state || "")}</td><td class="mono" style="max-width:360px;word-break:break-word">${h((e.rule_ids || []).join(" "))} ${h(JSON.stringify(e.details)).slice(0, 260)}</td></tr>`).join("")}</table></div>`;
  }
  function pickedDocs() { return [...document.querySelectorAll(".reqdoc:checked")].map(x => x.value); }

  async function decide(action) {
    const note = $("note").value.trim();
    if ((action === "reject" || action === "request_info") && !note) return flash("Please add a note for this action.", true);
    const docs = pickedDocs();
    if (action === "request_info" && !docs.length) return flash("Choose at least one document to request.", true);
    const label = { approve: "approve", reject: "reject", request_info: "request documents for" }[action];
    if (!await ask(`Confirm: ${label} ${st.case.application.application_id}?`)) return;
    document.querySelectorAll("[data-act]").forEach(b => b.disabled = true);
    try {
      await call("POST", `/api/officer/cases/${st.case.application.application_id}/decision`,
        { action, note: note || null, expected_version: st.case.application.version, requested_documents: docs });
      flash("Decision saved. The customer's status and notifications are updated.");
      $("note").value = "";
      st.lastSig = ""; await loadQueues(); await loadCase(st.case.application.application_id);
    } catch (e) {
      flash(e.message + (e.code === "STALE_DECISION" ? " (reloaded)" : ""), true);
      document.querySelectorAll("[data-act]").forEach(b => b.disabled = false);
      loadCase(st.case.application.application_id);
    }
  }

  // ------------------------------------------------------------ limit increase requests
  async function loadLimit(id, quiet) {
    let r;
    try { r = await call("GET", "/api/officer/limit-requests/" + id); }
    catch (e) { if (e.status === 404) { st.selected = null; Store.del("ow.sel"); showOverview(); return; } if (!quiet) flash(e.message, true); return; }
    const draft = $("lnote") ? $("lnote").value : "";
    st.limit = r;
    const cr = r.profile.credit;
    $("case").innerHTML = `<div class="panel"><div class="case-head"><div><div class="note mono">${h(r.request_id)} · v${r.version}</div><div class="who">${h(r.profile.customer.display_name)}</div>
        <div class="note">Credit-limit increase request · ${fmt.dt(r.created_at)}${r.linked_transaction_id ? " · linked purchase " + h(r.linked_transaction_id) : ""}</div></div>
        <div style="text-align:right"><div class="amount sm">${fmt.vnd(r.current_limit_vnd)} → ${fmt.vnd(r.requested_limit_vnd)}</div><span class="chip ${r.status === "APPROVED" ? "green" : r.status === "REJECTED" ? "red" : "amber"}">${h(r.status)}</span></div></div>
        <div class="grid3" style="margin-top:12px"><div class="stat"><div class="k">Current limit</div><div class="v" style="font-size:14px">${fmt.vnd(cr.limit_vnd)}</div></div>
          <div class="stat"><div class="k">Outstanding exposure</div><div class="v" style="font-size:14px">${fmt.vnd(cr.outstanding_vnd)}</div></div>
          <div class="stat"><div class="k">Reason</div><div class="v" style="font-size:13px">${h(r.reason || "—")}</div></div></div></div>
      <div class="panel"><h2>Decision ${r.can_decide ? '<span class="chip amber">Action required</span>' : '<span class="chip grey">Read-only</span>'}</h2>
        ${r.can_decide ? `<label class="field">Approved limit (VND) <input id="alim" type="number" step="1000000" value="${r.requested_limit_vnd}" min="${cr.limit_vnd + 1}" max="${r.requested_limit_vnd}"></label>
          <label class="field">Officer note (required for reject / request documents)<textarea id="lnote" rows="2">${h(draft)}</textarea></label>${docPicker()}
          <div class="actions"><button class="btn green" data-lact="approve">✓ Approve limit</button><button class="btn amber" data-lact="request_info">✎ Request documents</button><button class="btn primary" data-lact="reject">✕ Reject</button></div>
          <div class="note" style="margin-top:8px">The customer's limit changes only after approval; available credit = new limit − outstanding exposure. Documents don't guarantee approval.</div>`
        : `<div class="note">${r.status === "APPROVED" ? "Approved limit " + fmt.vnd(r.approved_limit_vnd) + " by " + h(r.decided_by) : r.status === "MORE_INFORMATION_REQUIRED" ? "Waiting for customer documents." : h(r.status)}${r.decision_note ? " · " + h(r.decision_note) : ""}</div>`}</div>
      ${docsPanel(r.documents, "Supporting documents")}
      ${profilePanel(r.profile)}
      ${auditPanel(r.audit)}`;
    document.querySelectorAll("[data-lact]").forEach(b => b.onclick = () => decideLimit(b.dataset.lact));
  }
  async function decideLimit(action) {
    const note = $("lnote").value.trim();
    if ((action === "reject" || action === "request_info") && !note) return flash("Please add a note for this action.", true);
    const docs = pickedDocs();
    if (!await ask(`Confirm: ${action.replace("_", " ")} limit request ${st.limit.request_id}?`)) return;
    try {
      await call("POST", `/api/officer/limit-requests/${st.limit.request_id}/decision`, { action, note: note || null, expected_version: st.limit.version,
        approved_limit_vnd: +$("alim").value, requested_documents: docs });
      flash("Limit decision saved and customer notified.");
      st.lastSig = ""; await loadQueues(); await loadLimit(st.limit.request_id);
    } catch (e) { flash(e.message, true); loadLimit(st.limit.request_id); }
  }

  $("force-fail").onchange = async (e) => {
    try { await call("POST", "/api/demo/controls", { key: "force_next_payment_failure", value: e.target.checked ? "1" : "0" }); flash(e.target.checked ? "Next simulated payment will fail." : "Forced failure cleared."); }
    catch (err) { e.target.checked = !e.target.checked; flash(err.message, true); }
  };
  document.querySelectorAll("[data-adv]").forEach(b => b.onclick = async () => {
    try { const r = await api("POST", "/api/demo/clock", { advance_days: +b.dataset.adv }); flash("Demo date: " + fmt.date(r.business_date)); st.lastSig = ""; loadQueues(); } catch (e) { flash(e.message, true); }
  });
  $("reset").onclick = async () => {
    if (!await ask("Reset all demo data? All demo transactions, loans, cases and audit events will be cleared and fixtures re-seeded.")) return;
    try { await call("POST", "/api/demo/reset"); st.selected = null; Store.del("ow.sel"); st.lastSig = ""; flash("Demo data reset."); loadQueues(); }
    catch (e) { flash(e.message, true); }
  };

  loadQueues();
  setInterval(() => loadQueues(false), 4000);
})();
