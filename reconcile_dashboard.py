#!/usr/bin/env python3
"""
EnergyWizards ─ Bank Reconciliation Dashboard
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Run :  python3 reconcile_dashboard.py
Open:  http://localhost:5757

• Fetches unreconciled bank lines + all open invoices/bills from Odoo
• Proposes matches with a confidence score
• Toggle ✅ Reconcile / ❌ Skip per line
• "Apply" posts approved reconciliations back to Odoo via XML-RPC
• Data auto-refreshes every 3 days (configurable)
"""

import json, re, threading, time, webbrowser, xmlrpc.client
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ── CONFIGURATION ────────────────────────────────────────────────────────────
ODOO_URL     = "https://energywizards.odoo.com"
ODOO_DB      = "energywizards-main-9044464"
ODOO_USER    = "stijn@energywizards.be"
ODOO_API_KEY = "87a096579785fdfdca0714173403fe59c62a86ec"
PORT         = 5757
DATA_FILE    = Path("reconcile_data.json")
REFRESH_DAYS = 3
AMOUNT_TOL   = 0.10          # EUR tolerance for amount matching
SCORE_HIGH   = 60            # ≥ high confidence (green)
SCORE_MED    = 40            # ≥ medium confidence (amber)
# ─────────────────────────────────────────────────────────────────────────────

# ── ODOO CLIENT ──────────────────────────────────────────────────────────────
class OdooClient:
    def __init__(self):
        self.url     = ODOO_URL
        self.db      = ODOO_DB
        self.user    = ODOO_USER
        self.api_key = ODOO_API_KEY
        self.uid     = None
        self.models  = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        self._authenticate()

    def _authenticate(self):
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        uid = common.authenticate(self.db, self.user, self.api_key, {})
        if not uid:
            raise RuntimeError("Odoo authentication failed — check ODOO_USER / ODOO_API_KEY")
        self.uid = uid

    def call(self, model, method, args, kwargs=None):
        return self.models.execute_kw(
            self.db, self.uid, self.api_key,
            model, method, args, kwargs or {}
        )

    def search_read(self, model, domain, fields, limit=200, order=None):
        kw = {"fields": fields, "limit": limit}
        if order:
            kw["order"] = order
        return self.call(model, "search_read", [domain], kw)

# ── DATA FETCHER ─────────────────────────────────────────────────────────────
def fetch_data(odoo: OdooClient) -> dict:
    """Pull fresh data from Odoo and return a structured dict."""
    print("[fetch] Fetching unreconciled bank lines …")
    bank_lines = odoo.search_read(
        "account.bank.statement.line",
        [("is_reconciled", "=", False)],
        ["id","date","payment_ref","partner_id","partner_name",
         "amount","currency_id","move_id"],
        limit=500, order="date desc"
    )

    print("[fetch] Fetching open customer invoices …")
    open_invoices = odoo.search_read(
        "account.move",
        [("move_type","=","out_invoice"),
         ("state","=","posted"),
         ("payment_state","!=","paid"),
         ("amount_residual",">",0)],
        ["id","name","partner_id","invoice_date","invoice_date_due",
         "amount_total","amount_residual","ref","currency_id"],
        limit=200, order="invoice_date_due asc"
    )

    print("[fetch] Fetching open vendor bills …")
    open_bills = odoo.search_read(
        "account.move",
        [("move_type","=","in_invoice"),
         ("state","=","posted"),
         ("amount_residual",">",0)],
        ["id","name","partner_id","invoice_date","invoice_date_due",
         "amount_total","amount_residual","ref","currency_id"],
        limit=200, order="invoice_date_due asc"
    )

    print(f"[fetch] Got {len(bank_lines)} bank lines, "
          f"{len(open_invoices)} invoices, {len(open_bills)} bills")

    suggestions = run_matching(bank_lines, open_invoices, open_bills)
    return {
        "fetched_at": datetime.now().isoformat(),
        "bank_lines": bank_lines,
        "open_invoices": open_invoices,
        "open_bills": open_bills,
        "suggestions": suggestions,
    }


# ── MATCHING ENGINE ───────────────────────────────────────────────────────────
def _pname(field):
    if isinstance(field, list) and len(field) > 1: return field[1]
    if isinstance(field, list) and len(field) == 1: return field[0]
    return str(field) if field else ""

def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").lower().strip())

def _sim(a, b):
    if not a or not b: return 0.0
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def run_matching(bank_lines, open_invoices, open_bills) -> list:
    suggestions = []
    for bl in bank_lines:
        bl_amt    = float(bl.get("amount") or 0)
        bl_ref    = bl.get("payment_ref") or ""
        bl_pname  = _pname(bl.get("partner_id")) or bl.get("partner_name") or ""
        bl_abs    = abs(bl_amt)

        # positive = money in = match customer invoices
        # negative = money out = match vendor bills
        candidates = open_invoices if bl_amt > 0 else open_bills
        direction  = "out_invoice"  if bl_amt > 0 else "in_invoice"

        best: list = []
        for inv in candidates:
            inv_res     = float(inv.get("amount_residual") or 0)
            inv_total   = float(inv.get("amount_total") or 0)
            inv_partner = _pname(inv.get("partner_id"))
            inv_name    = str(inv.get("name") or "").strip()
            inv_ref     = str(inv.get("ref")  or "").strip()
            score  = 0
            reasons= []

            # ─ Amount ───────────────────────────────────────────
            if abs(bl_abs - inv_res) <= AMOUNT_TOL:
                score += 60; reasons.append("Exact amount match")
            elif abs(bl_abs - inv_total) <= AMOUNT_TOL:
                score += 50; reasons.append("Matches invoice total")
            elif inv_res > 0 and bl_abs > 0:
                ratio = bl_abs / inv_res
                if 0.97 <= ratio <= 1.03:
                    score += 40; reasons.append(f"Near-match ({ratio*100:.1f}%)")
                elif 0.85 <= ratio < 0.97:
                    score += 20; reasons.append(f"Partial ({ratio*100:.1f}%)")

            # ─ Partner name ──────────────────────────────────────
            if inv_partner and bl_pname:
                sim = _sim(inv_partner, bl_pname)
                if sim >= 0.85:
                    score += 30; reasons.append(f"Partner match ({sim*100:.0f}%)")
                elif sim >= 0.55:
                    score += 12; reasons.append(f"Partial partner ({sim*100:.0f}%)")

            # ─ Invoice # in bank ref ─────────────────────────────
            if inv_name and inv_name not in ("False","0","") and inv_name in bl_ref:
                score += 40; reasons.append(f"Invoice # '{inv_name}' in ref")

            # ─ Structured communication ──────────────────────────
            if inv_ref and inv_ref not in ("False","0") and len(inv_ref) > 3:
                if _norm(inv_ref) in _norm(bl_ref):
                    score += 35; reasons.append("Ref match")
                else:
                    digs_inv = re.sub(r"\D","", inv_ref)
                    digs_bl  = re.sub(r"\D","", bl_ref)
                    if len(digs_inv) > 5 and digs_inv in digs_bl:
                        score += 20; reasons.append("Ref digits match")

            if score >= SCORE_MED:
                best.append({
                    "score":       score,
                    "reasons":     reasons,
                    "inv_id":      inv.get("id"),
                    "inv_name":    inv_name,
                    "inv_partner": inv_partner,
                    "inv_residual":inv_res,
                    "inv_total":   inv_total,
                    "inv_due":     inv.get("invoice_date_due") or "",
                    "inv_date":    inv.get("invoice_date") or "",
                    "direction":   direction,
                })

        best.sort(key=lambda x: -x["score"])

        suggestions.append({
            "bl_id":      bl.get("id"),
            "bl_amt":     bl_amt,
            "bl_date":    bl.get("date") or "",
            "bl_ref":     bl_ref[:120],
            "bl_partner": bl_pname,
            "bl_move_id": _pname(bl.get("move_id")),
            "matches":    best[:2],
        })

    # sort: high confidence first, then by abs amount desc
    suggestions.sort(key=lambda x: (
        0 if x["matches"] and x["matches"][0]["score"] >= SCORE_HIGH else
        1 if x["matches"] and x["matches"][0]["score"] >= SCORE_MED  else 2,
        -abs(x["bl_amt"])
    ))
    return suggestions


# ── RECONCILE ENGINE ──────────────────────────────────────────────────────────
def reconcile_one(odoo: OdooClient, bl_id: int, inv_id: int, direction: str) -> dict:
    """
    Reconcile bank statement line bl_id against invoice inv_id.
    Returns {"ok": True} or {"ok": False, "error": "..."}
    """
    try:
        # 1. Find the receivable / payable move line on the invoice
        acc_types = (["asset_receivable"] if direction == "out_invoice"
                     else ["liability_payable"])
        aml_domain = [
            ("move_id",    "=", inv_id),
            ("account_id.account_type", "in", acc_types),
            ("amount_residual", "!=", 0),
        ]
        amls = odoo.search_read(
            "account.move.line", aml_domain,
            ["id","amount_residual"], limit=1
        )
        if not amls:
            return {"ok": False,
                    "error": "Could not find receivable/payable line on invoice"}

        aml_id = amls[0]["id"]

        # 2. Call reconcile on the bank statement line
        odoo.call(
            "account.bank.statement.line", "reconcile",
            [[bl_id], [{"id": aml_id}]]
        )
        return {"ok": True}

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── HTML PAGE ─────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>⚡ EnergyWizards — Bank Reconciliation</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css" rel="stylesheet">
<style>
  :root {
    --ew-green : #16a34a;
    --ew-amber : #d97706;
    --ew-gray  : #6b7280;
    --ew-red   : #dc2626;
    --ew-bg    : #f1f5f9;
  }
  body { background: var(--ew-bg); font-family: 'Segoe UI', system-ui, sans-serif; }

  /* ── HEADER ── */
  .ew-header {
    background: linear-gradient(135deg, #1e3a5f 0%, #0f766e 100%);
    color: #fff; padding: 1.1rem 2rem;
    display: flex; align-items: center; justify-content: space-between;
    box-shadow: 0 2px 12px rgba(0,0,0,.25);
  }
  .ew-header h1 { font-size: 1.35rem; font-weight: 700; margin: 0; letter-spacing: -.3px; }
  .ew-header .meta { font-size: .78rem; opacity: .8; }

  /* ── STATS BAR ── */
  .stats-bar { background:#fff; border-bottom:1px solid #e2e8f0; padding:.7rem 2rem; }
  .stat-pill {
    display:inline-flex; align-items:center; gap:.4rem;
    background:#f8fafc; border:1px solid #e2e8f0;
    border-radius:999px; padding:.3rem .85rem; font-size:.82rem; font-weight:600;
    margin-right:.5rem;
  }
  .stat-pill .dot { width:9px;height:9px;border-radius:50%; }

  /* ── FILTER TABS ── */
  .filter-row { padding:.9rem 2rem .4rem; }
  .filter-btn {
    border:1.5px solid #cbd5e1; background:#fff; color:#475569;
    border-radius:999px; padding:.35rem 1rem; font-size:.82rem; font-weight:600;
    cursor:pointer; transition:all .15s; margin-right:.4rem;
  }
  .filter-btn.active, .filter-btn:hover { border-color:#0f766e; color:#0f766e; background:#f0fdf4; }

  /* ── CARDS ── */
  .cards-wrap { padding:0 1.5rem 6rem; max-width:1300px; margin:0 auto; }

  .recon-card {
    background:#fff; border-radius:14px; margin-bottom:1rem;
    border:2px solid transparent;
    box-shadow:0 1px 4px rgba(0,0,0,.07);
    transition: border-color .2s, box-shadow .2s;
    overflow:hidden;
  }
  .recon-card.approved  { border-color: var(--ew-green); box-shadow:0 0 0 4px rgba(22,163,74,.12); }
  .recon-card.skipped   { border-color: #e5e7eb; opacity:.55; }
  .recon-card.applied   { border-color: var(--ew-green); background:#f0fdf4; }
  .recon-card.error-card{ border-color: var(--ew-red); background:#fff5f5; }

  .conf-bar {
    height:4px; border-radius:4px 4px 0 0;
  }
  .conf-high   { background: var(--ew-green); }
  .conf-medium { background: var(--ew-amber); }
  .conf-none   { background: var(--ew-gray); }

  .card-body-inner {
    display:grid; grid-template-columns:1fr auto 1fr; gap:1.1rem;
    align-items:center; padding:1rem 1.25rem .6rem;
  }

  /* bank side */
  .bank-side .amount {
    font-size:1.35rem; font-weight:700; line-height:1;
  }
  .amount-in  { color: var(--ew-green); }
  .amount-out { color: var(--ew-red);   }

  .bank-side .ref-text {
    font-size:.72rem; color:#6b7280; margin-top:.2rem;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:340px;
  }
  .bank-side .partner-badge {
    display:inline-block; background:#f1f5f9; border-radius:6px;
    padding:.15rem .55rem; font-size:.75rem; font-weight:600; color:#334155;
    margin-top:.35rem;
  }
  .bank-side .date-badge {
    font-size:.72rem; background:#e0f2fe; color:#0369a1;
    border-radius:6px; padding:.1rem .5rem; font-weight:600;
    display:inline-block; margin-bottom:.25rem;
  }

  /* arrow middle */
  .arrow-col { text-align:center; }
  .conf-badge {
    display:inline-flex; flex-direction:column; align-items:center;
    gap:.2rem; padding:.4rem .7rem; border-radius:10px;
    font-size:.72rem; font-weight:700; min-width:70px;
  }
  .conf-badge.high   { background:#dcfce7; color: var(--ew-green); }
  .conf-badge.medium { background:#fef3c7; color: var(--ew-amber); }
  .conf-badge.none   { background:#f3f4f6; color: var(--ew-gray);  }
  .conf-badge i { font-size:1.1rem; }

  /* invoice side */
  .inv-side { background:#f8fafc; border-radius:10px; padding:.7rem .9rem; min-height:80px; }
  .inv-side.no-match { color:#9ca3af; text-align:center; padding:1.2rem .9rem; }
  .inv-num  { font-weight:700; font-size:.9rem; color:#1e293b; }
  .inv-partner { font-size:.78rem; color:#475569; margin:.1rem 0; }
  .inv-amounts { font-size:.8rem; font-weight:600; color:#0f766e; margin-top:.2rem; }
  .inv-due  { font-size:.72rem; color:#9ca3af; }
  .reason-badges { margin-top:.4rem; display:flex; flex-wrap:wrap; gap:.25rem; }
  .rbadge {
    font-size:.66rem; padding:.1rem .45rem; border-radius:999px; font-weight:600;
    background:#e0f2fe; color:#0369a1;
  }

  /* actions row */
  .actions-row {
    display:flex; align-items:center; justify-content:flex-end;
    padding:.5rem 1.25rem .75rem; gap:.5rem; border-top:1px solid #f1f5f9;
  }
  .btn-reconcile {
    border:none; border-radius:999px; padding:.4rem 1.1rem;
    font-size:.82rem; font-weight:700; cursor:pointer; transition:all .15s;
    display:inline-flex; align-items:center; gap:.4rem;
  }
  .btn-yes { background:#dcfce7; color: var(--ew-green); }
  .btn-yes:hover, .btn-yes.active { background: var(--ew-green); color:#fff; }
  .btn-no  { background:#f3f4f6; color:#6b7280; }
  .btn-no:hover, .btn-no.active  { background:#fee2e2; color: var(--ew-red); }
  .btn-odoo {
    border:1.5px solid #e2e8f0; border-radius:999px; padding:.35rem .9rem;
    font-size:.78rem; color:#475569; background:#fff; cursor:pointer;
    text-decoration:none; display:inline-flex; align-items:center; gap:.35rem;
  }
  .btn-odoo:hover { border-color:#0f766e; color:#0f766e; background:#f0fdf4; }

  .result-badge {
    font-size:.78rem; font-weight:600; padding:.25rem .75rem;
    border-radius:999px;
  }
  .result-ok    { background:#dcfce7; color: var(--ew-green); }
  .result-error { background:#fee2e2; color: var(--ew-red); }

  /* ── STICKY APPLY BAR ── */
  .apply-bar {
    position:fixed; bottom:0; left:0; right:0;
    background:#1e3a5f; color:#fff;
    padding:.9rem 2rem; display:flex; align-items:center;
    justify-content:space-between; box-shadow:0 -4px 20px rgba(0,0,0,.2);
    z-index:999;
  }
  .apply-bar .summary { font-size:.88rem; opacity:.85; }
  .btn-apply {
    background: linear-gradient(135deg, #16a34a, #0f766e);
    color:#fff; border:none; border-radius:999px;
    padding:.6rem 1.8rem; font-size:.92rem; font-weight:700;
    cursor:pointer; transition: opacity .2s;
    display:inline-flex; align-items:center; gap:.5rem;
  }
  .btn-apply:disabled { opacity:.4; cursor:not-allowed; }
  .btn-apply .spinner {
    display:none; width:16px; height:16px;
    border:2px solid rgba(255,255,255,.4); border-top-color:#fff;
    border-radius:50%; animation:spin .6s linear infinite;
  }
  @keyframes spin { to { transform:rotate(360deg); } }

  /* ── EMPTY STATE ── */
  .empty-state { text-align:center; padding:5rem 2rem; color:#9ca3af; }
  .empty-state i { font-size:3rem; margin-bottom:1rem; }

  /* ── LOADING ── */
  #loading { display:flex; align-items:center; justify-content:center;
    flex-direction:column; gap:1rem; padding:6rem 2rem; color:#475569; }
  .spinner-lg {
    width:48px; height:48px; border:4px solid #e2e8f0;
    border-top-color:#0f766e; border-radius:50%;
    animation:spin .8s linear infinite;
  }
</style>
</head>
<body>

<!-- HEADER -->
<div class="ew-header">
  <div>
    <h1><i class="fa-solid fa-bolt"></i> EnergyWizards — Bank Reconciliation</h1>
    <div class="meta">
      <span id="hdr-fetched">Loading …</span>
      &nbsp;·&nbsp;
      <span id="hdr-next">next refresh in …</span>
    </div>
  </div>
  <div class="d-flex align-items-center gap-2">
    <button class="btn-odoo" onclick="triggerRefresh()">
      <i class="fa-solid fa-rotate"></i> Refresh now
    </button>
  </div>
</div>

<!-- STATS BAR -->
<div class="stats-bar" id="stats-bar" style="display:none">
  <span class="stat-pill"><span class="dot" style="background:#0ea5e9"></span><span id="stat-total">0</span> bank lines</span>
  <span class="stat-pill"><span class="dot" style="background:var(--ew-green)"></span><span id="stat-high">0</span> high confidence</span>
  <span class="stat-pill"><span class="dot" style="background:var(--ew-amber)"></span><span id="stat-med">0</span> medium</span>
  <span class="stat-pill"><span class="dot" style="background:var(--ew-gray)"></span><span id="stat-none">0</span> no match</span>
  <span class="stat-pill"><span class="dot" style="background:#a78bfa"></span><span id="stat-approved">0</span> approved</span>
</div>

<!-- FILTER ROW -->
<div class="filter-row" id="filter-row" style="display:none">
  <button class="filter-btn active" onclick="setFilter('all',this)">All</button>
  <button class="filter-btn" onclick="setFilter('high',this)"><i class="fa-solid fa-circle-check" style="color:var(--ew-green)"></i> High confidence</button>
  <button class="filter-btn" onclick="setFilter('medium',this)"><i class="fa-solid fa-circle-half-stroke" style="color:var(--ew-amber)"></i> Medium confidence</button>
  <button class="filter-btn" onclick="setFilter('none',this)"><i class="fa-solid fa-circle-question" style="color:var(--ew-gray)"></i> No match</button>
  <button class="filter-btn" onclick="setFilter('approved',this)"><i class="fa-solid fa-check-double" style="color:#7c3aed"></i> Approved</button>
</div>

<!-- LOADING STATE -->
<div id="loading">
  <div class="spinner-lg"></div>
  <div>Loading reconciliation data from Odoo …</div>
</div>

<!-- CARDS -->
<div class="cards-wrap" id="cards-wrap" style="display:none"></div>

<!-- EMPTY STATE -->
<div class="empty-state" id="empty-state" style="display:none">
  <i class="fa-solid fa-inbox"></i>
  <div style="font-size:1.1rem;font-weight:600;color:#374151">No lines in this filter</div>
  <div style="font-size:.85rem;margin-top:.4rem">Try a different filter or refresh data</div>
</div>

<!-- APPLY BAR -->
<div class="apply-bar">
  <div class="summary" id="bar-summary">Select reconciliations above, then click Apply.</div>
  <button class="btn-apply" id="btn-apply" onclick="applyReconciliations()" disabled>
    <span class="spinner" id="apply-spinner"></span>
    <i class="fa-solid fa-link" id="apply-icon"></i>
    Apply approved reconciliations
  </button>
</div>

<script>
// ── STATE ────────────────────────────────────────────────────────────────────
let DATA       = null;      // full API response
let decisions  = {};        // { bl_id: 'yes' | 'no' | null }
let applied    = {};        // { bl_id: { ok, error } }
let activeFilter = 'all';

// ── BOOTSTRAP ────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', loadData);

async function loadData() {
  showLoading(true);
  try {
    const r = await fetch('/api/data');
    DATA = await r.json();
    renderAll();
  } catch(e) {
    showLoading(false);
    document.getElementById('loading').innerHTML =
      `<i class="fa-solid fa-triangle-exclamation" style="color:#dc2626;font-size:2rem"></i>
       <div style="color:#dc2626;font-weight:600">Could not load data: ${e.message}</div>`;
    document.getElementById('loading').style.display = 'flex';
  }
}

async function triggerRefresh() {
  showLoading(true);
  document.getElementById('cards-wrap').style.display = 'none';
  document.getElementById('stats-bar').style.display  = 'none';
  document.getElementById('filter-row').style.display = 'none';
  decisions = {}; applied = {};
  await fetch('/api/refresh', {method:'POST'});
  await loadData();
}

// ── RENDER ───────────────────────────────────────────────────────────────────
function renderAll() {
  showLoading(false);
  if (!DATA || !DATA.suggestions) return;

  document.getElementById('stats-bar').style.display = '';
  document.getElementById('filter-row').style.display = '';

  updateStats();
  renderCards();
}

function renderCards() {
  const wrap = document.getElementById('cards-wrap');
  const sugg = DATA.suggestions;

  let html = '';
  let shown = 0;

  for (const s of sugg) {
    const id       = s.bl_id;
    const decision = decisions[id] || null;
    const app      = applied[id]   || null;
    const topMatch = s.matches.length ? s.matches[0] : null;
    const score    = topMatch ? topMatch.score : 0;
    const confClass= score >= 60 ? 'high' : score >= 40 ? 'medium' : 'none';
    const confLabel= score >= 60 ? 'High' : score >= 40 ? 'Medium' : 'No match';
    const confIcon = score >= 60 ? 'fa-circle-check' : score >= 40 ? 'fa-circle-half-stroke' : 'fa-circle-xmark';

    // Filter
    if (activeFilter === 'high'     && score < 60)              continue;
    if (activeFilter === 'medium'   && !(score>=40 && score<60))continue;
    if (activeFilter === 'none'     && score >= 40)             continue;
    if (activeFilter === 'approved' && decision !== 'yes')      continue;
    shown++;

    const amtSign  = s.bl_amt >= 0 ? '+' : '';
    const amtClass = s.bl_amt >= 0 ? 'amount-in' : 'amount-out';
    const amtFmt   = new Intl.NumberFormat('nl-BE',{style:'currency',currency:'EUR'}).format(s.bl_amt);

    const cardClass = app ? (app.ok ? 'applied' : 'error-card')
                    : decision === 'yes' ? 'approved'
                    : decision === 'no'  ? 'skipped' : '';

    // Invoice side
    let invHtml = '';
    if (topMatch) {
      const reasons = topMatch.reasons.map(r=>`<span class="rbadge">${r}</span>`).join('');
      const altMatch = s.matches.length > 1 ? s.matches[1] : null;
      let altHtml = '';
      if (altMatch) {
        altHtml = `<div style="font-size:.68rem;color:#94a3b8;margin-top:.5rem">
          Alt: <strong>${altMatch.inv_name||'–'}</strong> · ${altMatch.inv_partner} · 
          €${altMatch.inv_residual.toFixed(2)} (${altMatch.score}pts)
        </div>`;
      }
      invHtml = `
        <div class="inv-side">
          <div class="inv-num">${topMatch.inv_name || '(draft/no number)'}</div>
          <div class="inv-partner"><i class="fa-solid fa-user" style="font-size:.65rem"></i> ${topMatch.inv_partner}</div>
          <div class="inv-amounts">Open: €${topMatch.inv_residual.toLocaleString('nl-BE',{minimumFractionDigits:2})}</div>
          <div class="inv-due">Due: ${topMatch.inv_due || '—'}</div>
          <div class="reason-badges">${reasons}</div>
          ${altHtml}
        </div>`;
    } else {
      invHtml = `<div class="inv-side no-match">
        <i class="fa-solid fa-magnifying-glass" style="font-size:1.3rem;margin-bottom:.3rem;display:block"></i>
        No invoice match found<br>
        <small>Identify manually in Odoo</small>
      </div>`;
    }

    // Action row
    let actionHtml = '';
    if (app) {
      if (app.ok) {
        actionHtml = `<span class="result-badge result-ok"><i class="fa-solid fa-check"></i> Reconciled in Odoo</span>`;
      } else {
        actionHtml = `<span class="result-badge result-error"><i class="fa-solid fa-xmark"></i> Error: ${app.error}</span>
          <a class="btn-odoo" href="${app.odoo_url||'#'}" target="_blank">Open in Odoo</a>`;
      }
    } else {
      const yesActive = decision === 'yes' ? 'active' : '';
      const noActive  = decision === 'no'  ? 'active' : '';
      const yesDisabled = !topMatch ? 'disabled style="opacity:.35;cursor:not-allowed"' : '';

      actionHtml = `
        <a class="btn-odoo" href="${odooLineUrl(id)}" target="_blank">
          <i class="fa-solid fa-arrow-up-right-from-square"></i> Open in Odoo
        </a>
        <button class="btn-reconcile btn-no ${noActive}" ${topMatch?'':''}
          onclick="setDecision('${id}','no')">
          <i class="fa-solid fa-xmark"></i> Skip
        </button>
        <button class="btn-reconcile btn-yes ${yesActive}" ${yesDisabled}
          onclick="${topMatch ? `setDecision('${id}','yes')` : ''}">
          <i class="fa-solid fa-check"></i> Reconcile
        </button>`;
    }

    html += `
    <div class="recon-card ${cardClass}" id="card-${id}" data-conf="${confClass}" data-decision="${decision||''}">
      <div class="conf-bar conf-${confClass}"></div>
      <div class="card-body-inner">
        <!-- BANK SIDE -->
        <div class="bank-side">
          <div class="date-badge"><i class="fa-regular fa-calendar"></i> ${s.bl_date}</div>
          <div class="amount ${amtClass}">${amtSign}${amtFmt}</div>
          ${s.bl_partner ? `<div class="partner-badge"><i class="fa-solid fa-building" style="font-size:.65rem"></i> ${s.bl_partner}</div>` : ''}
          <div class="ref-text" title="${s.bl_ref}">${s.bl_ref || '—'}</div>
        </div>
        <!-- ARROW / CONFIDENCE -->
        <div class="arrow-col">
          <div class="conf-badge ${confClass}">
            <i class="fa-solid ${confIcon}"></i>
            <div>${confLabel}</div>
            ${score ? `<div style="font-size:.65rem">${score} pts</div>` : ''}
          </div>
          <div style="color:#cbd5e1;font-size:1.4rem;margin-top:.3rem">→</div>
        </div>
        <!-- INVOICE SIDE -->
        <div>${invHtml}</div>
      </div>
      <div class="actions-row">${actionHtml}</div>
    </div>`;
  }

  wrap.innerHTML = html || '';
  wrap.style.display = shown ? '' : 'none';
  document.getElementById('empty-state').style.display = shown ? 'none' : '';
  updateApplyBar();
}

// ── DECISIONS ─────────────────────────────────────────────────────────────────
function setDecision(id, val) {
  // toggle: clicking the same button again clears it
  decisions[id] = (decisions[id] === val) ? null : val;
  const card = document.getElementById(`card-${id}`);
  if (card) {
    card.className = card.className.replace(/\b(approved|skipped)\b/g,'').trim();
    if (decisions[id] === 'yes') card.classList.add('approved');
    if (decisions[id] === 'no')  card.classList.add('skipped');

    // update button states
    card.querySelectorAll('.btn-yes').forEach(b => {
      b.classList.toggle('active', decisions[id] === 'yes');
    });
    card.querySelectorAll('.btn-no').forEach(b => {
      b.classList.toggle('active', decisions[id] === 'no');
    });
  }
  updateStats();
  updateApplyBar();
}

function setFilter(f, btn) {
  activeFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderCards();
}

// ── STATS ─────────────────────────────────────────────────────────────────────
function updateStats() {
  if (!DATA) return;
  const sugg = DATA.suggestions;
  document.getElementById('stat-total').textContent    = sugg.length;
  document.getElementById('stat-high').textContent     = sugg.filter(s=>s.matches.length && s.matches[0].score>=60).length;
  document.getElementById('stat-med').textContent      = sugg.filter(s=>s.matches.length && s.matches[0].score>=40 && s.matches[0].score<60).length;
  document.getElementById('stat-none').textContent     = sugg.filter(s=>!s.matches.length || s.matches[0].score<40).length;
  document.getElementById('stat-approved').textContent = Object.values(decisions).filter(v=>v==='yes').length;

  const d = new Date(DATA.fetched_at);
  document.getElementById('hdr-fetched').textContent   = `Last fetched: ${d.toLocaleString('nl-BE')}`;
  const next = new Date(d.getTime() + REFRESH_DAYS_JS*24*60*60*1000);
  const diff = Math.max(0, next - Date.now());
  const hrs  = Math.floor(diff/3600000);
  const mins = Math.floor((diff%3600000)/60000);
  document.getElementById('hdr-next').textContent = `Next refresh: in ${hrs}h ${mins}m`;
}

function updateApplyBar() {
  const approved = Object.entries(decisions).filter(([,v])=>v==='yes');
  const skipped  = Object.entries(decisions).filter(([,v])=>v==='no');
  const btn  = document.getElementById('btn-apply');
  const summ = document.getElementById('bar-summary');
  if (approved.length) {
    summ.textContent = `${approved.length} approved · ${skipped.length} skipped`;
    btn.disabled = false;
  } else {
    summ.textContent = 'Select reconciliations above, then click Apply.';
    btn.disabled = true;
  }
}

// ── APPLY ─────────────────────────────────────────────────────────────────────
async function applyReconciliations() {
  const items = [];
  for (const s of DATA.suggestions) {
    if (decisions[s.bl_id] === 'yes' && s.matches.length) {
      const m = s.matches[0];
      items.push({
        bl_id:     s.bl_id,
        inv_id:    m.inv_id,
        direction: m.direction,
      });
    }
  }
  if (!items.length) return;

  // Show spinner
  document.getElementById('apply-spinner').style.display = 'inline-block';
  document.getElementById('apply-icon').style.display    = 'none';
  document.getElementById('btn-apply').disabled = true;
  document.getElementById('bar-summary').textContent = `Reconciling ${items.length} items in Odoo …`;

  try {
    const r   = await fetch('/api/reconcile', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({items})
    });
    const res = await r.json();

    // Mark results
    for (const [key, val] of Object.entries(res.results || {})) {
      applied[key] = val;
    }
    renderCards();
    const ok  = Object.values(res.results||{}).filter(v=>v.ok).length;
    const err = Object.values(res.results||{}).filter(v=>!v.ok).length;
    document.getElementById('bar-summary').textContent =
      `✅ ${ok} reconciled · ${err ? `❌ ${err} errors` : 'all good'}`;
  } catch(e) {
    document.getElementById('bar-summary').textContent = `Network error: ${e.message}`;
  } finally {
    document.getElementById('apply-spinner').style.display = 'none';
    document.getElementById('apply-icon').style.display    = '';
    document.getElementById('btn-apply').disabled = false;
  }
}

// ── HELPERS ───────────────────────────────────────────────────────────────────
function odooLineUrl(blId) {
  return `${ODOO_BASE_URL}/odoo/accounting/bank`;
}

function showLoading(yes) {
  document.getElementById('loading').style.display    = yes ? 'flex' : 'none';
  document.getElementById('cards-wrap').style.display = yes ? 'none' : '';
}

const ODOO_BASE_URL   = '{{ODOO_URL}}';
const REFRESH_DAYS_JS = {{REFRESH_DAYS}};
</script>
</body>
</html>
"""


# ── HTTP REQUEST HANDLER ──────────────────────────────────────────────────────
_lock      = threading.Lock()
_data_cache: dict = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence default logging

    def _send(self, code, body, ctype="application/json"):
        enc = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(enc))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(enc)

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/" or p == "/index.html":
            page = (HTML
                    .replace("{{ODOO_URL}}", ODOO_URL)
                    .replace("{{REFRESH_DAYS}}", str(REFRESH_DAYS)))
            self._send(200, page, "text/html; charset=utf-8")

        elif p == "/api/data":
            with _lock:
                self._send(200, json.dumps(_data_cache, default=str))

        elif p == "/api/status":
            with _lock:
                ft = _data_cache.get("fetched_at","never")
            self._send(200, json.dumps({"fetched_at": ft}))

        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        p    = urlparse(self.path).path
        ln   = int(self.headers.get("Content-Length","0"))
        body = self.rfile.read(ln)

        if p == "/api/refresh":
            threading.Thread(target=do_refresh, daemon=True).start()
            self._send(200, '{"status":"refresh started"}')

        elif p == "/api/reconcile":
            payload = json.loads(body or "{}")
            items   = payload.get("items", [])
            results = {}
            try:
                odoo = OdooClient()
                for item in items:
                    key = str(item["bl_id"])
                    res = reconcile_one(odoo, item["bl_id"], item["inv_id"], item["direction"])
                    res["odoo_url"] = f"{ODOO_URL}/odoo/accounting/bank"
                    results[key]    = res
                    print(f"[reconcile] BL#{item['bl_id']} inv#{item['inv_id']} → {res}")
            except Exception as e:
                print(f"[reconcile] Fatal error: {e}")
                for item in items:
                    results[str(item["bl_id"])] = {"ok": False, "error": str(e),
                                                    "odoo_url": f"{ODOO_URL}/odoo/accounting/bank"}
            self._send(200, json.dumps({"results": results}))

        else:
            self._send(404, '{"error":"not found"}')


# ── REFRESH LOGIC ─────────────────────────────────────────────────────────────
def do_refresh():
    global _data_cache
    print("[refresh] Pulling data from Odoo …")
    try:
        odoo = OdooClient()
        data = fetch_data(odoo)
        with _lock:
            _data_cache = data
        DATA_FILE.write_text(json.dumps(data, indent=2, default=str))
        print(f"[refresh] Done — {len(data['suggestions'])} suggestions saved.")
    except Exception as e:
        print(f"[refresh] ERROR: {e}")


def maybe_refresh():
    """Refresh if data is stale or missing."""
    if DATA_FILE.exists():
        try:
            saved = json.loads(DATA_FILE.read_text())
            ft    = datetime.fromisoformat(saved["fetched_at"])
            if datetime.now() - ft < timedelta(days=REFRESH_DAYS):
                print(f"[refresh] Using cached data from {ft.strftime('%Y-%m-%d %H:%M')}")
                global _data_cache
                with _lock:
                    _data_cache = saved
                return
        except Exception:
            pass
    do_refresh()


def background_scheduler():
    """Re-fetch every REFRESH_DAYS days."""
    while True:
        time.sleep(REFRESH_DAYS * 24 * 3600)
        do_refresh()


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print("━" * 58)
    print(" ⚡ EnergyWizards — Bank Reconciliation Dashboard")
    print("━" * 58)

    # Load or refresh data
    maybe_refresh()

    # Background auto-refresh thread
    t = threading.Thread(target=background_scheduler, daemon=True)
    t.start()

    # Start server
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"\n  ✅ Server running at {url}")
    print(f"  🔄 Data refreshes every {REFRESH_DAYS} days automatically")
    print(f"  ⌨️   Press Ctrl+C to stop\n")

    # Open browser
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] Stopped.")


if __name__ == "__main__":
    main()
