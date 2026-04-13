"""
simulation_bot.py — Polymarket "Nothing Ever Happens" Simulator

Lógica:
  1. Escanea TODOS los mercados YES/NO que expiran HOY via Gamma API
  2. Filtra: NO > 86% (= YES < 14%) → evento muy improbable
  3. Simula entrada de $1 a NO en cada candidato
  4. Verifica resolución: NO >= 0.99 → WON, YES >= 0.99 → LOST
  5. Guarda cada posición cerrada en simulation_results.csv

Uso:
  python simulation_bot.py                  # corre una vez y muestra estado
  python simulation_bot.py --loop           # corre en bucle cada 5 min
  python simulation_bot.py --loop --interval 120  # cada 2 min
  python simulation_bot.py --report         # solo muestra reporte CSV
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
CSV_FILE   = DATA_DIR / "simulation_results.csv"
STATE_FILE = DATA_DIR / "simulation_state.json"

NO_MIN_THRESHOLD  = 0.86   # NO price mínimo para entrar
NO_MAX_THRESHOLD  = 0.985  # NO price máximo — por encima ya está resuelto o sin liquidez real
MIN_VOLUME_USD    = 500    # volumen mínimo para que el mercado tenga sentido
FIXED_ENTRY_USD   = 1.00   # monto fijo por posición simulada
MAX_POSITIONS     = 50     # máximo de posiciones abiertas simultáneas

# Confirmación de LOST — evita flasheazos del oráculo
LOST_CONFIRM_CHECKS  = 4    # cuántas veces debe verse YES≥0.99 antes de confirmar LOST
LOST_CONFIRM_DELAY_S = 8    # segundos entre cada re-verificación


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
        raise  # siempre propagar Ctrl+C
    except Exception as e:
        log.debug("GET %s → %s", url, e)
        return None


def parse_prices(m):
    raw = m.get("outcomePrices") or "[]"
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        yes = float(prices[0]) if len(prices) > 0 else None
        no  = float(prices[1]) if len(prices) > 1 else None
        # Rechazar precios fuera de rango útil — incluye mercados ya resueltos (0 o 1 exacto)
        if yes is not None and not (0.01 <= yes <= 0.99):
            yes = None
        if no  is not None and not (0.01 <= no  <= 0.99):
            no  = None
        return yes, no
    except Exception:
        return None, None


def fetch_yes_clob(yes_token_id):
    """Devuelve (ask, bid) del CLOB para YES token."""
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


# ──────────────────────────────────────────────────────────────────────────────
# Gamma scanner — mercados que expiran hoy con NO > 86%
# ──────────────────────────────────────────────────────────────────────────────

def scan_todays_markets():
    """
    Devuelve lista de dicts con info de mercados candidatos:
    - conditionId, question, yes_price, no_price, volume, end_date, slug,
      yes_token_id, no_token_id
    """
    today    = now_utc().date()
    tomorrow = today + timedelta(days=1)

    # Gamma acepta filtros de fecha en /markets
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

            # Solo YES/NO binarios
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

            # Filtro: NO en rango útil (86%–98.5%) — excluye mercados ya resueltos
            if no_price < NO_MIN_THRESHOLD or no_price > NO_MAX_THRESHOLD:
                continue

            volume = float(m.get("volume") or 0)

            # Filtro: volumen mínimo — descarta mercados fantasma sin liquidez
            if volume < MIN_VOLUME_USD:
                continue

            # Token IDs para CLOB
            raw_ids = m.get("clobTokenIds") or "[]"
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
                "question":      m.get("question", ""),
                "slug":          m.get("slug", ""),
                "yes_price":     round(yes_price, 4),
                "no_price":      round(no_price, 4),
                "volume":        round(volume, 2),
                "end_date":      end_dt.isoformat() if end_dt else None,
                "yes_token_id":  yes_token_id,
                "no_token_id":   no_token_id,
            })

        # Paginación
        if isinstance(data, list) or len(markets) < params["limit"]:
            break
        params["offset"] += params["limit"]

    # Ordenar: NO más alto primero (más seguro)
    candidates.sort(key=lambda x: x["no_price"], reverse=True)
    log.info(
        "Scan: %d mercados | NO %.0f%%–%.0f%% | vol≥$%d",
        len(candidates), NO_MIN_THRESHOLD * 100, NO_MAX_THRESHOLD * 100, MIN_VOLUME_USD,
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
    return {"open_positions": {}, "stats": {"total": 0, "won": 0, "lost": 0, "pnl": 0.0}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ──────────────────────────────────────────────────────────────────────────────
# CSV
# ──────────────────────────────────────────────────────────────────────────────

CSV_HEADERS = [
    "closed_at", "condition_id", "question",
    "entry_no_price", "exit_no_price",
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


# ──────────────────────────────────────────────────────────────────────────────
# Core simulation cycle
# ──────────────────────────────────────────────────────────────────────────────

def run_cycle(state):
    now = now_utc()
    open_pos = state["open_positions"]

    # 1. Escanear nuevos candidatos
    if len(open_pos) < MAX_POSITIONS:
        candidates = scan_todays_markets()
        new_entries = 0
        for c in candidates:
            if c["condition_id"] in open_pos:
                continue
            if len(open_pos) >= MAX_POSITIONS:
                break

            # Simular entrada a NO
            # tokens_no = $1 / no_price (cuántos tokens NO recibimos)
            tokens_no = round(FIXED_ENTRY_USD / c["no_price"], 6)

            open_pos[c["condition_id"]] = {
                "question":      c["question"],
                "slug":          c["slug"],
                "entry_no":      c["no_price"],
                "entry_yes":     c["yes_price"],
                "current_no":    c["no_price"],
                "current_yes":   c["yes_price"],
                "tokens_no":     tokens_no,
                "allocated":     FIXED_ENTRY_USD,
                "volume":        c["volume"],
                "end_date":      c["end_date"],
                "yes_token_id":  c["yes_token_id"],
                "no_token_id":   c["no_token_id"],
                "entry_time":    now.isoformat(),
                "lost_confirm_count": 0,  # conteo de confirmaciones de LOST
            }
            new_entries += 1
            log.info(
                "ENTRY  NO=%.1f%% vol=$%.0f | %s",
                c["no_price"] * 100, c["volume"], c["question"][:60],
            )

        if new_entries:
            log.info("Abiertas %d nuevas posiciones. Total open: %d", new_entries, len(open_pos))
    else:
        log.info("MAX_POSITIONS alcanzado (%d). Solo monitoreando.", MAX_POSITIONS)

    # 2. Actualizar precios y verificar resolución
    closed_ids = []
    for cid, pos in list(open_pos.items()):
        yes_tid = pos.get("yes_token_id")
        ask, bid = fetch_yes_clob(yes_tid) if yes_tid else (None, None)

        current_yes = ask if ask is not None else pos["current_yes"]
        current_no  = round(1 - current_yes, 4) if ask is not None else pos["current_no"]

        pos["current_yes"] = current_yes
        pos["current_no"]  = current_no

        result    = None
        exit_no   = current_no
        pnl       = 0.0

        # En Polymarket, un token NO comprado a precio P cuesta P dólares.
        # Si el mercado resuelve NO=1: cada token NO paga $1.00
        # Si el mercado resuelve YES=1: cada token NO paga $0.00
        # tokens_no = allocated / entry_no_price
        # PnL_WON  = tokens_no * 1.00 - allocated = allocated/entry_no - allocated = allocated*(1/entry_no - 1)
        # PnL_LOST = tokens_no * 0.00 - allocated = -allocated

        # Resolución: YES → 1 (evento ocurrió) → tokens NO valen 0 → LOST total
        # Requiere LOST_CONFIRM_CHECKS lecturas consecutivas para evitar flasheazos
        if current_yes >= 0.99:
            pos["lost_confirm_count"] = pos.get("lost_confirm_count", 0) + 1
            if pos["lost_confirm_count"] < LOST_CONFIRM_CHECKS:
                log.info(
                    "⚠️  LOST candidato (%d/%d) — re-verificando en %ds | %s",
                    pos["lost_confirm_count"], LOST_CONFIRM_CHECKS,
                    LOST_CONFIRM_DELAY_S, pos["question"][:55],
                )
                time.sleep(LOST_CONFIRM_DELAY_S)
                # Re-leer precio fresco
                ask2, _ = fetch_yes_clob(yes_tid) if yes_tid else (None, None)
                if ask2 is not None:
                    current_yes = ask2
                    current_no  = round(1 - ask2, 4)
                    pos["current_yes"] = current_yes
                    pos["current_no"]  = current_no
                if current_yes < 0.99:
                    log.info("✅ Flasheazo descartado — YES volvió a %.1f%% | %s",
                             current_yes * 100, pos["question"][:55])
                    pos["lost_confirm_count"] = 0  # resetear contador
            else:
                result  = "LOST"
                exit_no = 0.00
                pnl     = round(-pos["allocated"], 4)
        else:
            pos["lost_confirm_count"] = 0  # si YES bajó, resetear contador

        # Resolución: NO → 1 (evento no ocurrió) → tokens NO valen $1 cada uno → WON
        if result is None and current_no >= 0.99:
            result  = "WON"
            exit_no = 1.00
            # pago = tokens_no * $1.00; ganancia = pago - lo_invertido
            pnl     = round(pos["tokens_no"] * 1.0 - pos["allocated"], 4)

        # Mercado vencido sin resolución formal — solo cerramos después de 24h
        elif result is None and pos.get("end_date"):
            try:
                end_dt = datetime.fromisoformat(pos["end_date"])
                if now > end_dt + timedelta(hours=24):
                    result  = "EXPIRED_UNRESOLVED"
                    exit_no = current_no
                    # PnL estimado al precio actual de mercado
                    pnl     = round(pos["tokens_no"] * exit_no - pos["allocated"], 4)
            except Exception:
                pass

        if result:
            entry_time = datetime.fromisoformat(pos["entry_time"])
            duration_min = round((now - entry_time).total_seconds() / 60, 1)

            row = {
                "closed_at":      now.isoformat(),
                "condition_id":   cid,
                "question":       pos["question"],
                "entry_no_price": pos["entry_no"],
                "exit_no_price":  exit_no,
                "allocated_usd":  pos["allocated"],
                "pnl_usd":        pnl,
                "result":         result,
                "volume_at_entry": pos["volume"],
                "end_date":       pos["end_date"],
                "duration_min":   duration_min,
            }
            append_csv(row)

            state["stats"]["total"] += 1
            if result == "WON":
                state["stats"]["won"] += 1
            elif result == "LOST":
                state["stats"]["lost"] += 1
            elif result == "EXPIRED_UNRESOLVED":
                state["stats"]["expired"] = state["stats"].get("expired", 0) + 1
            state["stats"]["pnl"] = round(state["stats"]["pnl"] + pnl, 4)

            emoji = "✅" if result == "WON" else ("❌" if result == "LOST" else "⏳")
            log.info(
                "%s %s | exit_NO=%.1f%% PnL=$%+.2f | %s",
                emoji, result, exit_no * 100, pnl, pos["question"][:55],
            )
            closed_ids.append(cid)

    for cid in closed_ids:
        open_pos.pop(cid, None)

    # 3. Resumen
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

    rows = []
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
    # WON PnL esperado = sum(allocated * (1/entry_no - 1)) para WON reales
    won_pnl  = sum(float(r["pnl_usd"]) for r in rows if r["result"] == "WON")
    lost_pnl = sum(float(r["pnl_usd"]) for r in rows if r["result"] == "LOST")
    win_rt   = won / (won + lost) * 100 if (won + lost) > 0 else 0

    print(f"\n{'='*60}")
    print(f"  REPORTE — Nothing Ever Happens Simulator")
    print(f"{'='*60}")
    print(f"  Total cerradas            : {total}")
    print(f"  WON                       : {won}  ({win_rt:.1f}% vs LOST)")
    print(f"  LOST                      : {lost}")
    print(f"  EXPIRED sin resolver      : {expired}")
    print(f"  ─────────────────────────────────────────")
    print(f"  PnL WON                   : ${won_pnl:+.4f}")
    print(f"  PnL LOST                  : ${lost_pnl:+.4f}")
    print(f"  PnL TOTAL                 : ${pnl:+.4f}")
    print(f"  Capital simulado          : ${inv:.2f}")
    print(f"  ROI                       : {pnl/inv*100:+.2f}%" if inv > 0 else "  ROI: n/a")
    print(f"{'='*60}")
    print(f"  Nota: WON paga $1/token → PnL = $1/entry_no - $1")
    print(f"        Ej: entry NO=87.5% → paga $1/0.875=$1.143 → +$0.143")
    print(f"{'='*60}\n")

    print(f"{'Resultado':<8} {'NO entry':>9} {'NO exit':>8} {'PnL':>7}  Pregunta")
    print("-" * 75)
    for r in rows[-20:]:
        print(
            f"{r['result']:<8} {float(r['entry_no_price'])*100:>8.1f}%"
            f" {float(r['exit_no_price'])*100:>7.1f}%"
            f" ${float(r['pnl_usd']):>+6.2f}  {r['question'][:45]}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Nothing Ever Happens — Polymarket Simulator")
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

    log.info("Nothing Ever Happens Simulator — inicio")
    log.info("Umbral NO: >%.0f%%  |  Entrada fija: $%.2f", NO_MIN_THRESHOLD * 100, FIXED_ENTRY_USD)

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