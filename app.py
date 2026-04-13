"""
app.py — Web server + bot runner para Railway
Corre el bot en un thread de fondo y sirve el dashboard en el puerto $PORT
"""

import os
import threading
import time
import logging
from flask import Flask, jsonify, render_template

# Importar lógica del bot
from bot import (
    run_cycle, load_state, save_state, init_csv,
    NO_MIN_THRESHOLD, FIXED_ENTRY_USD,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# Estado global compartido entre bot thread y Flask
shared_state = {"data": None, "lock": threading.Lock()}
BOT_INTERVAL = int(os.environ.get("BOT_INTERVAL", 300))


# ──────────────────────────────────────────────────────────────────────────────
# Bot thread
# ──────────────────────────────────────────────────────────────────────────────

def bot_loop():
    init_csv()
    state = load_state()
    log.info("Bot iniciado — umbral NO >%.0f%% | intervalo %ds", NO_MIN_THRESHOLD * 100, BOT_INTERVAL)

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
    """Construye un dict serializable para la API."""
    import csv as csv_module
    from pathlib import Path

    stats = state.get("stats", {})
    open_pos = state.get("open_positions", {})

    open_list = []
    for cid, pos in open_pos.items():
        open_list.append({
            "question":    pos["question"],
            "entry_no":    round(pos["entry_no"] * 100, 1),
            "current_no":  round(pos.get("current_no", pos["entry_no"]) * 100, 1),
            "current_yes": round(pos.get("current_yes", 1 - pos["entry_no"]) * 100, 1),
            "volume":      pos["volume"],
            "entry_time":  pos["entry_time"],
            "allocated":   pos["allocated"],
            "lost_confirm": pos.get("lost_confirm_count", 0),
        })

    # Últimas 50 filas del CSV
    csv_path = Path("simulation_results.csv")
    history = []
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv_module.DictReader(f))
            for r in reversed(rows[-50:]):
                history.append({
                    "closed_at":   r["closed_at"],
                    "question":    r["question"],
                    "entry_no":    round(float(r["entry_no_price"]) * 100, 1),
                    "exit_no":     round(float(r["exit_no_price"]) * 100, 1),
                    "pnl":         float(r["pnl_usd"]),
                    "result":      r["result"],
                    "duration":    r["duration_min"],
                })

    total = stats.get("total", 0)
    won   = stats.get("won", 0)
    lost  = stats.get("lost", 0)
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
            "threshold": NO_MIN_THRESHOLD * 100,
            "entry_usd": FIXED_ENTRY_USD,
            "interval":  BOT_INTERVAL,
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    with shared_state["lock"]:
        data = shared_state["data"]
    if data is None:
        # Primera vez antes de que corra el bot
        state = load_state()
        data = build_snapshot(state)
    return jsonify(data)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Arrancar bot en thread de fondo
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 8080))
    log.info("Dashboard en http://0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
