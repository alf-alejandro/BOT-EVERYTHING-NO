"""
app.py — Web server + bot runner para Railway (Underdog Hunter)
Corre el bot en un thread de fondo y sirve el dashboard en el puerto $PORT
"""

import os
import threading
import time
import logging
from pathlib import Path
from flask import Flask, jsonify

from bot import (
    run_cycle, load_state, save_state, init_csv,
    YES_MAX_THRESHOLD, FIXED_ENTRY_USD, CSV_FILE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

shared_state = {"data": None, "lock": threading.Lock()}
BOT_INTERVAL = int(os.environ.get("BOT_INTERVAL", 300))


# ──────────────────────────────────────────────────────────────────────────────
# Bot thread
# ──────────────────────────────────────────────────────────────────────────────

def bot_loop():
    init_csv()
    state = load_state()
    log.info("Bot iniciado — umbral YES <%.0f%% | intervalo %ds", YES_MAX_THRESHOLD * 100, BOT_INTERVAL)

    while True:
        try:
            run_cycle(state)
            save_state(state)
            with shared_state["lock"]:
                shared_state["data"] = build_snapshot(state)
        except Exception:
            log.exception("Error en ciclo del bot")
        time.sleep(BOT_INTERVAL)


def build_snapshot(state):
    import csv as csv_module

    stats    = state.get("stats", {})
    open_pos = state.get("open_positions", {})

    open_list = []
    for cid, pos in open_pos.items():
        open_list.append({
            "question":    pos["question"],
            "yes_side":    pos.get("yes_side", "?"),
            "no_side":     pos.get("no_side", "?"),
            "entry_yes":   round(pos["entry_yes"] * 100, 1),
            "current_no":  round(pos.get("current_no", pos.get("entry_no", 0)) * 100, 1),
            "current_yes": round(pos.get("current_yes", pos["entry_yes"]) * 100, 1),
            "volume":      pos["volume"],
            "entry_time":  pos["entry_time"],
            "allocated":   pos["allocated"],
            "won_confirm": pos.get("won_confirm_count", 0),
        })

    # Últimas 50 filas del CSV
    history = []
    if CSV_FILE.exists():
        with open(CSV_FILE, newline="", encoding="utf-8") as f:
            rows = list(csv_module.DictReader(f))
            for r in reversed(rows[-50:]):
                history.append({
                    "closed_at":  r["closed_at"],
                    "question":   r["question"],
                    "yes_side":   r.get("yes_side", "?"),
                    "no_side":    r.get("no_side", "?"),
                    "entry_yes":  round(float(r["entry_yes_price"]) * 100, 1),
                    "exit_yes":   round(float(r["exit_yes_price"]) * 100, 1),
                    "pnl":        float(r["pnl_usd"]),
                    "result":     r["result"],
                    "duration":   r["duration_min"],
                })

    total    = stats.get("total", 0)
    won      = stats.get("won", 0)
    lost     = stats.get("lost", 0)
    win_rate = round(won / (won + lost) * 100, 1) if (won + lost) > 0 else 0

    return {
        "stats": {
            "total":    total,
            "won":      won,
            "lost":     lost,
            "expired":  stats.get("expired", 0),
            "pnl":      round(stats.get("pnl", 0), 4),
            "win_rate": win_rate,
            "open":     len(open_pos),
        },
        "open_positions": open_list,
        "history":        history,
        "config": {
            "threshold": YES_MAX_THRESHOLD * 100,
            "entry_usd": FIXED_ENTRY_USD,
            "interval":  BOT_INTERVAL,
        },
    }


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>POLYBOT — Underdog Hunter</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:        #090c10;
    --surface:   #0d1117;
    --border:    #1c2333;
    --muted:     #30363d;
    --text:      #c9d1d9;
    --dim:       #6e7681;
    --green:     #39d353;
    --red:       #f85149;
    --yellow:    #e3b341;
    --blue:      #58a6ff;
    --purple:    #bc8cff;
    --mono:      'Space Mono', monospace;
    --sans:      'Syne', sans-serif;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--mono);
    font-size: 13px;
    min-height: 100vh;
    overflow-x: hidden;
  }

  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background: repeating-linear-gradient(
      0deg,
      transparent,
      transparent 2px,
      rgba(0,0,0,0.03) 2px,
      rgba(0,0,0,0.03) 4px
    );
    pointer-events: none;
    z-index: 9999;
  }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 18px 28px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .logo {
    font-family: var(--sans);
    font-weight: 800;
    font-size: 20px;
    letter-spacing: -0.5px;
    color: #fff;
  }
  .logo span { color: var(--green); }

  .tagline {
    font-size: 11px;
    color: var(--dim);
    letter-spacing: 2px;
    text-transform: uppercase;
  }

  .status-pill {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 11px;
    color: var(--dim);
    letter-spacing: 1px;
  }
  .dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--green);
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.3; }
  }

  /* ── Layout ── */
  .main {
    padding: 24px 28px;
    display: grid;
    grid-template-columns: 1fr 1fr 1fr 1fr;
    grid-template-rows: auto auto auto;
    gap: 16px;
    max-width: 1400px;
    margin: 0 auto;
  }

  /* ── Stat cards ── */
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 18px 20px;
    position: relative;
    overflow: hidden;
    animation: fadeIn 0.4s ease both;
  }
  .stat-card::after {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
  }
  .stat-card.green::after  { background: var(--green); }
  .stat-card.red::after    { background: var(--red); }
  .stat-card.blue::after   { background: var(--blue); }
  .stat-card.yellow::after { background: var(--yellow); }

  .stat-label {
    font-size: 10px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--dim);
    margin-bottom: 10px;
  }
  .stat-value {
    font-family: var(--sans);
    font-weight: 800;
    font-size: 32px;
    line-height: 1;
    color: #fff;
  }
  .stat-value.pos { color: var(--green); }
  .stat-value.neg { color: var(--red); }
  .stat-sub {
    margin-top: 6px;
    font-size: 11px;
    color: var(--dim);
  }

  /* ── Panels ── */
  .panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    overflow: hidden;
    animation: fadeIn 0.5s ease both;
  }
  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 18px;
    border-bottom: 1px solid var(--border);
    background: rgba(255,255,255,0.02);
  }
  .panel-title {
    font-size: 10px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--dim);
  }
  .panel-count {
    font-size: 11px;
    color: var(--blue);
  }

  /* ── Open positions ── */
  .open-panel {
    grid-column: 1 / 3;
    max-height: 400px;
    overflow-y: auto;
  }

  /* 5 columnas: pregunta | apuesta | NO% | YES% | Vol */
  .col-header {
    display: grid;
    grid-template-columns: 1fr 130px 60px 60px 65px;
    gap: 8px;
    padding: 7px 18px;
    border-bottom: 1px solid var(--border);
    font-size: 10px;
    letter-spacing: 1px;
    color: var(--muted);
    text-transform: uppercase;
  }
  .col-header span:not(:first-child) { text-align: right; }

  .pos-row {
    display: grid;
    grid-template-columns: 1fr 130px 60px 60px 65px;
    gap: 8px;
    padding: 10px 18px;
    border-bottom: 1px solid var(--border);
    align-items: center;
    transition: background 0.15s;
  }
  .pos-row:hover { background: rgba(255,255,255,0.03); }
  .pos-row:last-child { border-bottom: none; }

  .pos-question {
    font-size: 11px;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Badge de apuesta: Over, Under, spread, empate */
  .pos-bet {
    text-align: right;
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 0.5px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .pos-bet.over   { color: var(--green); }
  .pos-bet.under  { color: var(--blue); }
  .pos-bet.spread { color: var(--yellow); }
  .pos-bet.draw   { color: var(--purple); }
  .pos-bet.other  { color: var(--dim); }

  .pos-no {
    text-align: right;
    font-size: 11px;
    color: var(--dim);
  }
  .pos-yes {
    text-align: right;
    font-size: 11px;
    color: var(--green);
    font-weight: bold;
  }
  .pos-vol {
    text-align: right;
    font-size: 11px;
    color: var(--dim);
  }

  .warning-row { background: rgba(57, 211, 83, 0.08) !important; }
  .warning-row .pos-yes { color: #fff; }

  /* ── History ── */
  .history-panel {
    grid-column: 3 / 5;
    max-height: 400px;
    overflow-y: auto;
  }

  /* 6 columnas: estado | mercado | apuesta | entry | dur | pnl */
  .hist-col-header {
    display: grid;
    grid-template-columns: 55px 1fr 110px 55px 50px 65px;
    gap: 6px;
    padding: 7px 18px;
    border-bottom: 1px solid var(--border);
    font-size: 10px;
    letter-spacing: 1px;
    color: var(--muted);
    text-transform: uppercase;
  }
  .hist-col-header span:not(:first-child):not(:nth-child(2)):not(:nth-child(3)) {
    text-align: right;
  }

  .hist-row {
    display: grid;
    grid-template-columns: 55px 1fr 110px 55px 50px 65px;
    gap: 6px;
    padding: 9px 18px;
    border-bottom: 1px solid var(--border);
    align-items: center;
  }
  .hist-row:last-child { border-bottom: none; }
  .hist-row:hover { background: rgba(255,255,255,0.03); }

  .badge {
    display: inline-block;
    padding: 2px 6px;
    border-radius: 2px;
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 1px;
  }
  .badge.WON  { background: rgba(57,211,83,0.15);  color: var(--green); }
  .badge.LOST { background: rgba(248,81,73,0.15);  color: var(--red); }

  .hist-q {
    font-size: 11px;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .hist-side {
    font-size: 10px;
    font-weight: bold;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .hist-side.over   { color: var(--green); }
  .hist-side.under  { color: var(--blue); }
  .hist-side.spread { color: var(--yellow); }
  .hist-side.draw   { color: var(--purple); }
  .hist-side.other  { color: var(--dim); }

  .hist-entry { text-align: right; font-size: 11px; color: var(--dim); }
  .hist-dur   { text-align: right; font-size: 11px; color: var(--dim); }
  .hist-pnl   { text-align: right; font-size: 12px; font-weight: bold; }
  .hist-pnl.pos { color: var(--green); }
  .hist-pnl.neg { color: var(--red); }

  /* ── Config bar ── */
  .config-bar {
    grid-column: 1 / 5;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 12px 20px;
    display: flex;
    align-items: center;
    gap: 32px;
    font-size: 11px;
    color: var(--dim);
  }
  .config-item { display: flex; gap: 8px; align-items: center; }
  .config-label { letter-spacing: 1.5px; text-transform: uppercase; }
  .config-val { color: var(--blue); font-weight: bold; }

  /* ── Legend ── */
  .legend {
    display: flex;
    gap: 16px;
    align-items: center;
    margin-left: auto;
    font-size: 10px;
  }
  .legend-item { display: flex; gap: 5px; align-items: center; }
  .leg-dot { width: 8px; height: 8px; border-radius: 50%; }

  /* ── Chart ── */
  .chart-panel {
    grid-column: 1 / 5;
    height: 160px;
    overflow: hidden;
  }
  .chart-panel .panel-header { margin-bottom: 0; }
  #pnl-chart { width: 100%; height: 120px; display: block; }

  /* ── Empty / scroll ── */
  .empty {
    padding: 32px;
    text-align: center;
    color: var(--muted);
    font-size: 12px;
    letter-spacing: 1px;
  }
  ::-webkit-scrollbar { width: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--muted); border-radius: 2px; }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(6px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .last-update { font-size: 10px; color: var(--muted); letter-spacing: 1px; }
</style>
</head>
<body>

<header>
  <div>
    <div class="logo">POLY<span>BOT</span></div>
    <div class="tagline">Underdog Hunter Simulator (Fútbol)</div>
  </div>
  <div style="display:flex;align-items:center;gap:20px;">
    <span class="last-update" id="last-update">—</span>
    <div class="status-pill"><div class="dot"></div>LIVE</div>
  </div>
</header>

<div class="main">

  <div class="stat-card green" style="animation-delay:0.05s">
    <div class="stat-label">PnL Total</div>
    <div class="stat-value" id="s-pnl">—</div>
    <div class="stat-sub" id="s-roi">ROI: —</div>
  </div>

  <div class="stat-card blue" style="animation-delay:0.1s">
    <div class="stat-label">Win Rate</div>
    <div class="stat-value" id="s-winrate">—</div>
    <div class="stat-sub" id="s-wl">W: — / L: —</div>
  </div>

  <div class="stat-card yellow" style="animation-delay:0.15s">
    <div class="stat-label">Posiciones Abiertas</div>
    <div class="stat-value" id="s-open">—</div>
    <div class="stat-sub" id="s-total">Total cerradas: —</div>
  </div>

  <div class="stat-card" style="animation-delay:0.2s">
    <div class="stat-label">Trades Cerrados</div>
    <div class="stat-value" id="s-total2">—</div>
    <div class="stat-sub" id="s-expired">WON + LOST: —</div>
  </div>

  <div class="panel chart-panel" style="animation-delay:0.25s">
    <div class="panel-header">
      <span class="panel-title">PnL Acumulado</span>
      <span class="panel-count" id="chart-range">últimas 50 ops</span>
    </div>
    <canvas id="pnl-chart"></canvas>
  </div>

  <div class="panel open-panel" style="animation-delay:0.3s">
    <div class="panel-header">
      <span class="panel-title">Posiciones Abiertas</span>
      <span class="panel-count" id="open-count">0</span>
    </div>
    <div class="col-header">
      <span>Mercado</span>
      <span>Apostando</span>
      <span>NO%</span>
      <span>YES%</span>
      <span>Vol</span>
    </div>
    <div id="open-list"><div class="empty">Cargando...</div></div>
  </div>

  <div class="panel history-panel" style="animation-delay:0.35s">
    <div class="panel-header">
      <span class="panel-title">Historial Reciente</span>
      <span class="panel-count" id="hist-count">0</span>
    </div>
    <div class="hist-col-header">
      <span>Estado</span>
      <span>Mercado</span>
      <span>Apostó</span>
      <span>Entry</span>
      <span>Dur</span>
      <span>PnL</span>
    </div>
    <div id="hist-list"><div class="empty">Cargando...</div></div>
  </div>

  <div class="config-bar" style="animation-delay:0.4s">
    <div class="config-item">
      <span class="config-label">Umbral YES</span>
      <span class="config-val" id="cfg-threshold">—</span>
    </div>
    <div class="config-item">
      <span class="config-label">Entrada</span>
      <span class="config-val" id="cfg-entry">—</span>
    </div>
    <div class="config-item">
      <span class="config-label">Intervalo</span>
      <span class="config-val" id="cfg-interval">—</span>
    </div>
    <div class="legend">
      <div class="legend-item"><div class="leg-dot" style="background:#39d353"></div><span style="color:#6e7681">Over / Spread fav</span></div>
      <div class="legend-item"><div class="leg-dot" style="background:#58a6ff"></div><span style="color:#6e7681">Under</span></div>
      <div class="legend-item"><div class="leg-dot" style="background:#e3b341"></div><span style="color:#6e7681">Spread</span></div>
      <div class="legend-item"><div class="leg-dot" style="background:#bc8cff"></div><span style="color:#6e7681">Empate</span></div>
    </div>
    <div style="font-size:10px;color:var(--muted)">Auto-refresh 30s</div>
  </div>

</div>

<script>
function fmt(n, d=2) {
  return (n >= 0 ? '+' : '') + n.toFixed(d);
}
function fmtVol(v) {
  if (v >= 1000000) return '$' + (v/1000000).toFixed(1) + 'M';
  if (v >= 1000)    return '$' + (v/1000).toFixed(0) + 'K';
  return '$' + v.toFixed(0);
}

// Clasifica el texto del side para colorear
function sideClass(side) {
  const s = (side || '').toLowerCase();
  if (s === 'over')                        return 'over';
  if (s === 'under')                       return 'under';
  if (s.includes('draw') || s === 'yes')   return 'draw';
  // Spread: tiene paréntesis con número
  if (s.includes('(') && (s.includes('+') || s.includes('-'))) return 'spread';
  return 'other';
}

function drawChart(history) {
  const canvas = document.getElementById('pnl-chart');
  const ctx    = canvas.getContext('2d');
  const W      = canvas.parentElement.clientWidth;
  const H      = 120;
  canvas.width  = W;
  canvas.height = H;

  const pts = [...history].reverse().map(h => h.pnl);
  if (pts.length === 0) return;

  const cum = [];
  let acc = 0;
  for (const p of pts) { acc += p; cum.push(acc); }

  const minV = Math.min(0, ...cum);
  const maxV = Math.max(0, ...cum);
  const range = maxV - minV || 1;
  const pad   = { top: 12, bottom: 12, left: 12, right: 12 };

  const toX = i => pad.left + (i / (cum.length - 1 || 1)) * (W - pad.left - pad.right);
  const toY = v => pad.top + (1 - (v - minV) / range) * (H - pad.top - pad.bottom);

  ctx.clearRect(0, 0, W, H);

  const zeroY = toY(0);
  ctx.strokeStyle = 'rgba(255,255,255,0.08)';
  ctx.setLineDash([4, 4]);
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, zeroY);
  ctx.lineTo(W - pad.right, zeroY);
  ctx.stroke();
  ctx.setLineDash([]);

  const lastVal   = cum[cum.length - 1];
  const lineColor = lastVal >= 0 ? '#39d353' : '#f85149';
  const grad      = ctx.createLinearGradient(0, pad.top, 0, H);
  grad.addColorStop(0, lastVal >= 0 ? 'rgba(57,211,83,0.25)' : 'rgba(248,81,73,0.25)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');

  ctx.beginPath();
  ctx.moveTo(toX(0), toY(cum[0]));
  for (let i = 1; i < cum.length; i++) ctx.lineTo(toX(i), toY(cum[i]));
  ctx.lineTo(toX(cum.length - 1), H);
  ctx.lineTo(toX(0), H);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  ctx.beginPath();
  ctx.moveTo(toX(0), toY(cum[0]));
  for (let i = 1; i < cum.length; i++) ctx.lineTo(toX(i), toY(cum[i]));
  ctx.strokeStyle = lineColor;
  ctx.lineWidth   = 2;
  ctx.stroke();

  ctx.fillStyle = lineColor;
  ctx.font      = 'bold 11px Space Mono, monospace';
  ctx.textAlign = 'right';
  ctx.fillText(fmt(lastVal) + ' USD', W - pad.right, pad.top + 10);
}

async function refresh() {
  try {
    const res = await fetch('/api/state');
    const d   = await res.json();
    const s   = d.stats;
    const cfg = d.config;

    const pnl      = s.pnl;
    const invested = s.total * cfg.entry_usd;
    const roi      = invested > 0 ? (pnl / invested * 100) : 0;

    const pnlEl = document.getElementById('s-pnl');
    pnlEl.textContent = fmt(pnl) + ' USD';
    pnlEl.className   = 'stat-value ' + (pnl >= 0 ? 'pos' : 'neg');

    document.getElementById('s-roi').textContent     = 'ROI: ' + fmt(roi, 1) + '%  |  Capital: $' + invested.toFixed(2);
    document.getElementById('s-winrate').textContent = s.win_rate + '%';
    document.getElementById('s-wl').textContent      = 'W: ' + s.won + '  /  L: ' + s.lost;
    document.getElementById('s-open').textContent    = s.open;
    document.getElementById('s-total').textContent   = 'Total cerradas: ' + s.total;
    document.getElementById('s-total2').textContent  = s.total;
    document.getElementById('s-expired').textContent = 'WON + LOST: ' + (s.won + s.lost);

    document.getElementById('cfg-threshold').textContent = '<' + cfg.threshold + '%';
    document.getElementById('cfg-entry').textContent     = '$' + cfg.entry_usd + ' / op';
    document.getElementById('cfg-interval').textContent  = cfg.interval + 's';

    // ── Posiciones abiertas ──
    const openList = document.getElementById('open-list');
    document.getElementById('open-count').textContent = d.open_positions.length;
    if (d.open_positions.length === 0) {
      openList.innerHTML = '<div class="empty">Buscando oportunidades...</div>';
    } else {
      openList.innerHTML = d.open_positions.map(p => {
        const warn = p.current_yes > 80;
        const sc   = sideClass(p.yes_side);
        return `<div class="pos-row ${warn ? 'warning-row' : ''}">
          <div class="pos-question" title="${p.question}">${p.question}</div>
          <div class="pos-bet ${sc}" title="Apostando a: ${p.yes_side}">${p.yes_side || '?'}</div>
          <div class="pos-no">${p.current_no}%</div>
          <div class="pos-yes">${p.current_yes}%</div>
          <div class="pos-vol">${fmtVol(p.volume)}</div>
        </div>`;
      }).join('');
    }

    // ── Historial ──
    const histList = document.getElementById('hist-list');
    document.getElementById('hist-count').textContent = d.history.length;
    if (d.history.length === 0) {
      histList.innerHTML = '<div class="empty">Sin historial todavía</div>';
    } else {
      histList.innerHTML = d.history.map(h => {
        const pnlCls = h.pnl >= 0 ? 'pos' : 'neg';
        const sc     = sideClass(h.yes_side);
        return `<div class="hist-row">
          <span><span class="badge ${h.result}">${h.result.slice(0,3)}</span></span>
          <span class="hist-q" title="${h.question}">${h.question}</span>
          <span class="hist-side ${sc}" title="${h.yes_side}">${h.yes_side || '?'}</span>
          <span class="hist-entry">${h.entry_yes}%</span>
          <span class="hist-dur">${parseFloat(h.duration).toFixed(0)}m</span>
          <span class="hist-pnl ${pnlCls}">${fmt(h.pnl)}</span>
        </div>`;
      }).join('');
    }

    drawChart(d.history);
    document.getElementById('last-update').textContent =
      'UPDATE ' + new Date().toLocaleTimeString('es-CL');

  } catch(e) {
    console.error('Error fetching state:', e);
  }
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""

# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html"}

@app.route("/api/state")
def api_state():
    with shared_state["lock"]:
        data = shared_state["data"]
    if data is None:
        state = load_state()
        data  = build_snapshot(state)
    return jsonify(data)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 8080))
    log.info("Dashboard en http://0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
