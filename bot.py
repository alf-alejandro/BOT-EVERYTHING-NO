"""
bot.py — Polymarket "Underdog Hunter" Simulator

Lógica (Estrategia Inversa / Francotirador):
  1. Escanea mercados que expiran HOY via Gamma API.
  2. Filtra SOLO partidos de fútbol (por palabras clave: FC, Draw, O/U, etc.).
  3. Filtra YES barato: YES > 0.01 y YES <= 0.12 (El Underdog).
  4. Simula entrada de $1 a YES.
  5. Verifica resolución:
        - Gamma FORMAL: resolutionPrice = 1.0 (WON), resolutionPrice = 0.0 (LOST).
        - CLOB: YES >= 0.99 → WON (con confirmación anti-flasheazo por VAR/goles anulados).
        - CLOB: NO >= 0.99 → LOST.
  6. Guarda cada posición cerrada en simulation_results_underdog.csv
"""

import csv
import json
import logging
import time
import argparse
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"

DATA_DIR   = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
# [NUEVO] Archivos separados para no sobreescribir tu base de datos anterior
CSV_FILE   = DATA_DIR / "simulation_results_underdog.csv"
STATE_FILE = DATA_DIR / "simulation_state_underdog.json"

# [NUEVO] Umbrales para comprar YES
YES_MAX_THRESHOLD = 0.12   # Precio máximo que pagaremos por el YES (12%)
YES_MIN_THRESHOLD = 0.015  # Precio mínimo (evitamos mercados muertos al 1%)
MIN_VOLUME_USD    = 500    # Volumen mínimo
FIXED_ENTRY_USD   = 1.00   # Monto fijo simulado
MAX_POSITIONS     = 50     # Máximo de posiciones

# [NUEVO] Palabras clave para detectar Fútbol
SOCCER_KEYWORDS = [
    " fc ", " sc ", " afc ", " cd ", " cs ", " ca ", " fbpa ",  # Siglas comunes de clubes
    "end in a draw", 
    "o/u", 
    "spread:", 
    "exact score", 
    "both teams to score", 
    " vs. " # Polymarket usa siempre " vs. " para enfrentar equipos (Ej: Grêmio FBPA vs. CD Riestra)
]

# Confirmación de WON (antes era para LOST) — evita flasheazos del oráculo por el VAR
WON_CONFIRM_CHECKS   = 4   
WON_CONFIRM_DELAY_S  = 8   


# ──────────────────────────────────────────────────────────────────────────────
# API helpers
# ──────────────────────────────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc)


def _get(url, params=None, timeout=(5, 10)):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log.debug("GET %s → %s", url, e)
        return None


def parse_prices(m):
    raw = m.get("outcomePrices") or "[]"
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        yes = float(prices[0]) if len(prices) > 0 else None
        no  = float(prices[1]) if len(prices) > 1 else None
        return yes, no
    except Exception:
        return None, None


def fetch_yes_clob(yes_token_id):
    if not yes_token_id:
        return None, None
    data = _get(f"{CLOB_BASE}/book", params={"token_id": yes_token_id}, timeout=(3, 5))
    if not data:
        return None, None
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    ask = min(float(a["price"]) for a in asks) if asks else None
    bid = max(float(b["price"]) for b in bids) if bids else None
    return ask, bid


def check_resolution_gamma(cid):
    """
    Gamma devuelve:
      - resolutionPrice: 1.0 = YES ganó, 0.0 = NO ganó
    """
    data = _get(f"{GAMMA_BASE}/markets/{cid}")
    if not data or not data.get("resolved"):
        return None

    res_price = data.get("resolutionPrice")
    if res_price is None:
        return None

    res_price = float(res_price)

    # [NUEVO] resolutionPrice = 1.0 → YES ganó → nosotros ganamos
    if res_price >= 0.99:
        return "WON"

    # [NUEVO] resolutionPrice = 0.0 → NO ganó → evento NO ocurrió → perdimos
    if res_price <= 0.01:
        return "LOST"

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Gamma scanner — mercados YES baratos
# ──────────────────────────────────────────────────────────────────────────────

def scan_todays_markets():
    today    = now_utc().date()
    tomorrow = today + timedelta(days=1)

    params = {
        "end_date_min": today.isoformat(),
        "end_date_max": tomorrow.isoformat(),
        "active":       "true",
        "closed":       "false",
        "limit":        500,
        "offset":       0,
    }

    candidates = []
    seen       = set()

    while True:
        data = _get(f"{GAMMA_BASE}/markets", params=params)
        if not data:
            break

        markets = data if isinstance(data, list) else data.get("markets", [])
        if not markets:
            break

        for m in markets:
            cid = m.get("conditionId") or m.get("condition_id")
            if not cid or cid in seen:
                continue
            seen.add(cid)

            question = m.get("question", "")
            q_lower = question.lower()
            
            # [NUEVO] 1. Filtrar solo fútbol
            is_soccer = any(kw in q_lower for kw in SOCCER_KEYWORDS)
            if not is_soccer:
                continue

            outcomes = m.get("outcomes")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = []
            if not outcomes or len(outcomes) != 2:
                continue

            yes_price, no_price = parse_prices(m)
            if yes_price is None or no_price is None:
                continue

            # [NUEVO] 2. Filtrar YES barato (Underdog)
            if yes_price < YES_MIN_THRESHOLD or yes_price > YES_MAX_THRESHOLD:
                continue

            volume = float(m.get("volume") or 0)
            if volume < MIN_VOLUME_USD:
                continue

            raw_ids  = m.get("clobTokenIds") or "[]"
            clob_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else (raw_ids or [])
            yes_token_id = clob_ids[0] if len(clob_ids) > 0 else None
            no_token_id  = clob_ids[1] if len(clob_ids) > 1 else None

            end_raw = m.get("endDate") or m.get("end_date")
            try:
                end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            except Exception:
                end_dt = None

            candidates.append({
                "condition_id":  cid,
                "question":      question,
                "slug":          m.get("slug", ""),
                "yes_price":     round(yes_price, 4),
                "no_price":      round(no_price, 4),
                "volume":        round(volume, 2),
                "end_date":      end_dt.isoformat() if end_dt else None,
                "yes_token_id":  yes_token_id,
                "no_token_id":   no_token_id,
            })

        if isinstance(data, list) or len(markets) < params["limit"]:
            break
        params["offset"] += params["limit"]

    # Ordenamos por los YES más baratos primero
    candidates.sort(key=lambda x: x["yes_price"])
    log.info(
        "Scan: %d partidos de fútbol | YES %.1f%%–%.1f%% | vol≥$%d",
        len(candidates), YES_MIN_THRESHOLD * 100, YES_MAX_THRESHOLD * 100, MIN_VOLUME_USD,
    )
    return candidates


# ──────────────────────────────────────────────────────────────────────────────
# State persistence
# ──────────────────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "open_positions": {},
        "stats": {"total": 0, "won": 0, "lost": 0, "expired": 0, "pnl": 0.0},
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ──────────────────────────────────────────────────────────────────────────────
# CSV
# ──────────────────────────────────────────────────────────────────────────────

# [NUEVO] Cambiado headers para registrar la entrada de YES
CSV_HEADERS = [
    "closed_at", "condition_id", "question",
    "entry_yes_price", "exit_yes_price",
    "allocated_usd", "pnl_usd", "result",
    "volume_at_entry", "end_date", "duration_min",
]

def init_csv():
    if not CSV_FILE.exists():
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()


def append_csv(row):
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=CSV_HEADERS).writerow(row)


def close_position(cid, pos, result, exit_yes, pnl, state, now):
    entry_time   = datetime.fromisoformat(pos["entry_time"])
    duration_min = round((now - entry_time).total_seconds() / 60, 1)

    append_csv({
        "closed_at":       now.isoformat(),
        "condition_id":    cid,
        "question":        pos["question"],
        "entry_yes_price": pos["entry_yes"], # [NUEVO]
        "exit_yes_price":  exit_yes,         # [NUEVO]
        "allocated_usd":   pos["allocated"],
        "pnl_usd":         pnl,
        "result":          result,
        "volume_at_entry": pos["volume"],
        "end_date":        pos["end_date"],
        "duration_min":    duration_min,
    })

    state["stats"]["total"] += 1
    if result == "WON":
        state["stats"]["won"] += 1
    elif result == "LOST":
        state["stats"]["lost"] += 1
    else:
        state["stats"]["expired"] = state["stats"].get("expired", 0) + 1

    state["stats"]["pnl"] = round(state["stats"]["pnl"] + pnl, 4)

    emoji = "🔥" if result == "WON" else ("💀" if result == "LOST" else "⏳")
    log.info(
        "%s %s | exit_YES=%.1f%% PnL=$%+.2f | %s",
        emoji, result, exit_yes * 100, pnl, pos["question"][:55],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Core simulation cycle
# ──────────────────────────────────────────────────────────────────────────────

def run_cycle(state):
    now      = now_utc()
    open_pos = state["open_positions"]

    # ── 1. Abrir nuevas posiciones ────────────────────────────────────────────
    if len(open_pos) < MAX_POSITIONS:
        candidates = scan_todays_markets()
        new_entries = 0
        for c in candidates:
            if c["condition_id"] in open_pos:
                continue
            if len(open_pos) >= MAX_POSITIONS:
                break

            # [NUEVO] Compramos tokens YES
            tokens_yes = round(FIXED_ENTRY_USD / c["yes_price"], 6)

            open_pos[c["condition_id"]] = {
                "question":           c["question"],
                "slug":               c["slug"],
                "entry_no":           c["no_price"],
                "entry_yes":          c["yes_price"],
                "current_no":         c["no_price"],
                "current_yes":        c["yes_price"],
                "tokens_yes":         tokens_yes,  # [NUEVO]
                "allocated":          FIXED_ENTRY_USD,
                "volume":             c["volume"],
                "end_date":           c["end_date"],
                "yes_token_id":       c["yes_token_id"],
                "no_token_id":        c["no_token_id"],
                "entry_time":         now.isoformat(),
                "won_confirm_count":  0,           # [NUEVO]
            }
            new_entries += 1
            log.info(
                "ENTRY  YES=%.1f%% (NO=%.1f%%) | vol=$%.0f | %s",
                c["yes_price"] * 100, c["no_price"] * 100, c["volume"], c["question"][:60],
            )

        if new_entries:
            log.info("Abiertas %d nuevas posiciones (Underdogs). Total open: %d", new_entries, len(open_pos))
    else:
        log.info("MAX_POSITIONS alcanzado (%d). Solo monitoreando.", MAX_POSITIONS)

    # ── 2. Monitorear y cerrar posiciones ────────────────────────────────────
    closed_ids = []

    for cid, pos in list(open_pos.items()):

        formal = check_resolution_gamma(cid)

        # [NUEVO] Si Gamma dice YES (1.0), ganamos
        if formal == "WON":
            exit_yes = 1.00
            pnl      = round(pos["tokens_yes"] * 1.0 - pos["allocated"], 4)
            close_position(cid, pos, "WON", exit_yes, pnl, state, now)
            closed_ids.append(cid)
            continue

        # [NUEVO] Si Gamma dice NO (0.0), perdimos
        if formal == "LOST":
            exit_yes = 0.00
            pnl      = round(-pos["allocated"], 4)
            close_position(cid, pos, "LOST", exit_yes, pnl, state, now)
            closed_ids.append(cid)
            continue

        # ── PASO B: CLOB ─────────
        yes_tid    = pos.get("yes_token_id")
        ask, bid   = fetch_yes_clob(yes_tid) if yes_tid else (None, None)

        if ask is not None:
            current_yes = ask
            current_no  = round(1 - ask, 4)
            pos["current_yes"] = current_yes
            pos["current_no"]  = current_no
        else:
            current_yes = pos["current_yes"]
            current_no  = pos["current_no"]

        result   = None
        exit_yes = current_yes
        pnl      = 0.0

        # ── PASO C: Anti-flasheazo para WON vía CLOB (Posible Gol/VAR) ───────────
        if current_yes >= 0.99:
            pos["won_confirm_count"] = pos.get("won_confirm_count", 0) + 1
            if pos["won_confirm_count"] < WON_CONFIRM_CHECKS:
                log.info(
                    "⚠️  GOLAZO/WON candidato (%d/%d) — esperando VAR/confirmación en %ds | %s",
                    pos["won_confirm_count"], WON_CONFIRM_CHECKS,
                    WON_CONFIRM_DELAY_S, pos["question"][:55],
                )
                time.sleep(WON_CONFIRM_DELAY_S)
                ask2, _ = fetch_yes_clob(yes_tid) if yes_tid else (None, None)
                if ask2 is not None:
                    current_yes = ask2
                    current_no  = round(1 - ask2, 4)
                    pos["current_yes"] = current_yes
                    pos["current_no"]  = current_no
                if current_yes < 0.99:
                    log.info(
                        "❌ Falsa alarma / VAR anulado — YES bajó a %.1f%% | %s",
                        current_yes * 100, pos["question"][:55],
                    )
                    pos["won_confirm_count"] = 0
            else:
                result   = "WON"
                exit_yes = 1.00
                pnl      = round(pos["tokens_yes"] * 1.0 - pos["allocated"], 4)
        else:
            pos["won_confirm_count"] = 0

        # ── PASO D: LOST vía CLOB (NO llegó a 0.99+, evento descartado) ────
        if result is None and current_no >= 0.99:
            result   = "LOST"
            exit_yes = 0.00
            pnl      = round(-pos["allocated"], 4)

        # ── PASO E: EXPIRED ───────
        if result is None and pos.get("end_date"):
            try:
                end_dt = datetime.fromisoformat(pos["end_date"])
                if now > end_dt + timedelta(hours=24):
                    result   = "EXPIRED_UNRESOLVED"
                    exit_yes = current_yes
                    pnl      = round(pos["tokens_yes"] * exit_yes - pos["allocated"], 4)
            except Exception:
                pass

        if result:
            close_position(cid, pos, result, exit_yes, pnl, state, now)
            closed_ids.append(cid)

    for cid in closed_ids:
        open_pos.pop(cid, None)

    s = state["stats"]
    log.info(
        "── Stats: total=%d won=%d lost=%d expired=%d PnL=$%+.4f | Open=%d ──",
        s["total"], s["won"], s["lost"], s.get("expired", 0), s["pnl"], len(open_pos),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Report
# ──────────────────────────────────────────────────────────────────────────────

def print_report():
    if not CSV_FILE.exists():
        print("No hay resultados todavía. Corre el bot primero.")
        return

    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("CSV vacío.")
        return

    total   = len(rows)
    won     = sum(1 for r in rows if r["result"] == "WON")
    lost    = sum(1 for r in rows if r["result"] == "LOST")
    expired = sum(1 for r in rows if r["result"] == "EXPIRED_UNRESOLVED")
    pnl     = sum(float(r["pnl_usd"]) for r in rows)
    inv     = sum(float(r["allocated_usd"]) for r in rows)
    won_pnl  = sum(float(r["pnl_usd"]) for r in rows if r["result"] == "WON")
    lost_pnl = sum(float(r["pnl_usd"]) for r in rows if r["result"] == "LOST")
    win_rt   = won / (won + lost) * 100 if (won + lost) > 0 else 0

    print(f"\n{'='*60}")
    print(f"  REPORTE — Underdog Hunter (Football)")
    print(f"{'='*60}")
    print(f"  Total cerradas            : {total}")
    print(f"  WON (Underdog hit)        : {won}  ({win_rt:.1f}% hit rate)")
    print(f"  LOST (Favorito ganó)      : {lost}")
    print(f"  EXPIRED sin resolver      : {expired}")
    print(f"  ─────────────────────────────────────────")
    print(f"  PnL WON                   : ${won_pnl:+.4f}")
    print(f"  PnL LOST                  : ${lost_pnl:+.4f}")
    print(f"  PnL TOTAL                 : ${pnl:+.4f}")
    print(f"  Capital simulado          : ${inv:.2f}")
    print(f"  ROI                       : {pnl/inv*100:+.2f}%" if inv > 0 else "  ROI: n/a")
    print(f"{'='*60}")
    print(f"  Nota: WON paga $1/token → PnL = $1/entry_yes - $1")
    print(f"        Ej: entry YES=4% ($0.04) → paga $1/0.04=$25.00 → +$24.00")
    print(f"{'='*60}\n")

    print(f"{'Resultado':<8} {'YES entry':>9} {'YES exit':>8} {'PnL':>7}  Pregunta")
    print("-" * 75)
    for r in rows[-20:]:
        print(
            f"{r['result']:<8} {float(r['entry_yes_price'])*100:>8.1f}%"
            f" {float(r['exit_yes_price'])*100:>7.1f}%"
            f" ${float(r['pnl_usd']):>+6.2f}  {r['question'][:45]}"
        )


def main():
    parser = argparse.ArgumentParser(description="Underdog Hunter — Football Simulator")
    parser.add_argument("--loop",     action="store_true", help="Corre en bucle continuo")
    parser.add_argument("--interval", type=int, default=300, help="Segundos entre ciclos (default: 300)")
    parser.add_argument("--report",   action="store_true", help="Solo muestra reporte CSV")
    parser.add_argument("--reset",    action="store_true", help="Borra estado y CSV (nuevo inicio)")
    args = parser.parse_args()

    if args.report:
        print_report()
        return

    if args.reset:
        for f in [CSV_FILE, STATE_FILE]:
            if f.exists():
                f.unlink()
        print("Estado y CSV borrados. Listo para nueva simulación.")
        return

    init_csv()
    state = load_state()

    log.info("Underdog Hunter Simulator — inicio")
    log.info("Umbral YES: <%.0f%%  |  Entrada fija: $%.2f", YES_MAX_THRESHOLD * 100, FIXED_ENTRY_USD)

    if args.loop:
        log.info("Modo loop — intervalo %ds. Ctrl+C para detener.", args.interval)
        while True:
            try:
                run_cycle(state)
                save_state(state)
            except KeyboardInterrupt:
                break
            except Exception:
                log.exception("Error en ciclo")
            time.sleep(args.interval)
    else:
        run_cycle(state)
        save_state(state)

    print_report()


if __name__ == "__main__":
    main()
