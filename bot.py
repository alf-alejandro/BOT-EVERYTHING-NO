# bot.py — Underdog Hunter Simulator
# Take profit: si YES sube de ~5% a >30%, cerramos con ganancia parcial.
# Sin anti-flasheazo. Resolución: Gamma formal, CLOB 0.99, o take profit.

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
CSV_FILE   = DATA_DIR / "simulation_results_underdog.csv"
STATE_FILE = DATA_DIR / "simulation_state_underdog.json"

YES_MAX_THRESHOLD = 0.12
YES_MIN_THRESHOLD = 0.015
MIN_VOLUME_USD    = 500
FIXED_ENTRY_USD   = 1.00
MAX_POSITIONS     = 50

# Take profit: si YES sube a este precio o más, vendemos (salida parcial real)
TAKE_PROFIT_THRESHOLD = 0.30

GRACE_HOURS = 2

SOCCER_KEYWORDS = [
    "end in a draw",
    "o/u",
]
SPREAD_INDICATORS = ["(+", "(-"]


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
    data = _get(f"{GAMMA_BASE}/markets/{cid}")
    if not data or not data.get("resolved"):
        return None
    res_price = data.get("resolutionPrice")
    if res_price is None:
        return None
    res_price = float(res_price)
    if res_price >= 0.99:
        return "WON"
    if res_price <= 0.01:
        return "LOST"
    return None


def parse_outcomes(m):
    raw = m.get("outcomes") or "[]"
    try:
        outcomes = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        outcomes = []

    if not outcomes or len(outcomes) < 2:
        return "YES", "NO"

    yes_side = str(outcomes[0]).strip()
    no_side  = str(outcomes[1]).strip()

    for indicator in SPREAD_INDICATORS:
        if indicator in yes_side or indicator in no_side:
            return None, None

    return yes_side, no_side


def is_spread_market(m):
    question = m.get("question", "").lower()
    if "spread:" in question:
        return True
    yes_side, _ = parse_outcomes(m)
    if yes_side is None:
        return True
    return False


def scan_todays_markets(already_seen_cids: set):
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

    candidates   = []
    seen_in_scan = set()

    while True:
        data = _get(f"{GAMMA_BASE}/markets", params=params)
        if not data:
            break

        markets = data if isinstance(data, list) else data.get("markets", [])
        if not markets:
            break

        for m in markets:
            cid = m.get("conditionId") or m.get("condition_id")
            if not cid:
                continue
            if cid in already_seen_cids or cid in seen_in_scan:
                continue
            seen_in_scan.add(cid)

            question = m.get("question", "")
            q_lower  = question.lower()

            if not any(kw in q_lower for kw in SOCCER_KEYWORDS):
                continue
            if is_spread_market(m):
                continue

            yes_side, no_side = parse_outcomes(m)
            if yes_side is None:
                continue

            yes_price, no_price = parse_prices(m)
            if yes_price is None or no_price is None:
                continue
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

            if end_dt and now_utc() > end_dt:
                continue

            candidates.append({
                "condition_id":  cid,
                "question":      question,
                "slug":          m.get("slug", ""),
                "yes_price":     round(yes_price, 4),
                "no_price":      round(no_price, 4),
                "yes_side":      yes_side,
                "no_side":       no_side,
                "volume":        round(volume, 2),
                "end_date":      end_dt.isoformat() if end_dt else None,
                "yes_token_id":  yes_token_id,
                "no_token_id":   no_token_id,
            })

        if isinstance(data, list) or len(markets) < params["limit"]:
            break
        params["offset"] += params["limit"]

    candidates.sort(key=lambda x: x["yes_price"])
    log.info(
        "Scan: %d mercados nuevos | YES %.1f%%–%.1f%% | vol≥$%d",
        len(candidates), YES_MIN_THRESHOLD * 100, YES_MAX_THRESHOLD * 100, MIN_VOLUME_USD,
    )
    return candidates


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "open_positions": {},
        "closed_cids":    [],
        "stats": {"total": 0, "won": 0, "lost": 0, "tp": 0, "pnl": 0.0},
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


CSV_HEADERS = [
    "closed_at", "condition_id", "question",
    "yes_side", "no_side",
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


def close_position(cid, pos, result, exit_yes_price, state, now):
    """
    result: WON | LOST | TP
    exit_yes_price: precio real de salida (1.0 si WON, 0.0 si LOST, precio CLOB si TP)
    PnL = tokens_yes * exit_price - allocated
    """
    pnl = round(pos["tokens_yes"] * exit_yes_price - pos["allocated"], 4)

    if result == "WON":
        state["stats"]["won"] += 1
        emoji = "🔥"
    elif result == "TP":
        state["stats"]["tp"] += 1
        emoji = "💰"
    else:
        state["stats"]["lost"] += 1
        emoji = "💀"

    entry_time   = datetime.fromisoformat(pos["entry_time"])
    duration_min = round((now - entry_time).total_seconds() / 60, 1)

    append_csv({
        "closed_at":       now.isoformat(),
        "condition_id":    cid,
        "question":        pos["question"],
        "yes_side":        pos.get("yes_side", "?"),
        "no_side":         pos.get("no_side", "?"),
        "entry_yes_price": pos["entry_yes"],
        "exit_yes_price":  round(exit_yes_price, 4),
        "allocated_usd":   pos["allocated"],
        "pnl_usd":         pnl,
        "result":          result,
        "volume_at_entry": pos["volume"],
        "end_date":        pos["end_date"],
        "duration_min":    duration_min,
    })

    state["stats"]["total"] += 1
    state["stats"]["pnl"]   = round(state["stats"]["pnl"] + pnl, 4)

    if "closed_cids" not in state:
        state["closed_cids"] = []
    if cid not in state["closed_cids"]:
        state["closed_cids"].append(cid)
    state["closed_cids"] = state["closed_cids"][-500:]

    log.info(
        "%s %s | [%s] | entry=%.1f%% exit=%.1f%% PnL=$%+.2f | dur=%.1fm | %s",
        emoji, result,
        pos.get("yes_side", "?"),
        pos["entry_yes"] * 100,
        exit_yes_price * 100,
        pnl, duration_min,
        pos["question"][:55],
    )


def run_cycle(state):
    now      = now_utc()
    open_pos = state["open_positions"]
    already_seen = set(open_pos.keys()) | set(state.get("closed_cids", []))

    # ── 1. Abrir nuevas posiciones ────────────────────────────────────────────
    if len(open_pos) < MAX_POSITIONS:
        candidates  = scan_todays_markets(already_seen)
        new_entries = 0
        for c in candidates:
            if c["condition_id"] in already_seen:
                continue
            if len(open_pos) >= MAX_POSITIONS:
                break

            tokens_yes = round(FIXED_ENTRY_USD / c["yes_price"], 6)
            open_pos[c["condition_id"]] = {
                "question":     c["question"],
                "slug":         c["slug"],
                "yes_side":     c["yes_side"],
                "no_side":      c["no_side"],
                "entry_no":     c["no_price"],
                "entry_yes":    c["yes_price"],
                "current_no":   c["no_price"],
                "current_yes":  c["yes_price"],
                "tokens_yes":   tokens_yes,
                "allocated":    FIXED_ENTRY_USD,
                "volume":       c["volume"],
                "end_date":     c["end_date"],
                "yes_token_id": c["yes_token_id"],
                "no_token_id":  c["no_token_id"],
                "entry_time":   now.isoformat(),
            }
            already_seen.add(c["condition_id"])
            new_entries += 1
            log.info(
                "ENTRY [%s] | YES=%.1f%% → TP si llega a %.0f%% | vol=$%.0f | %s",
                c["yes_side"],
                c["yes_price"] * 100,
                TAKE_PROFIT_THRESHOLD * 100,
                c["volume"],
                c["question"][:60],
            )

        if new_entries:
            log.info("Abiertas %d nuevas posiciones. Total open: %d", new_entries, len(open_pos))

    # ── 2. Monitorear y cerrar posiciones ────────────────────────────────────
    closed_ids = []

    for cid, pos in list(open_pos.items()):

        # ── PASO A: Gamma formal ──────────────────────────────────────────────
        formal = check_resolution_gamma(cid)
        if formal == "WON":
            close_position(cid, pos, "WON", 1.0, state, now)
            closed_ids.append(cid)
            continue
        if formal == "LOST":
            close_position(cid, pos, "LOST", 0.0, state, now)
            closed_ids.append(cid)
            continue

        # ── PASO B: Precio CLOB actual ────────────────────────────────────────
        yes_tid  = pos.get("yes_token_id")
        ask, bid = fetch_yes_clob(yes_tid) if yes_tid else (None, None)

        if ask is not None:
            current_yes = ask
            current_no  = round(1 - ask, 4)
            pos["current_yes"] = current_yes
            pos["current_no"]  = current_no
        else:
            current_yes = pos["current_yes"]
            current_no  = pos["current_no"]

        result         = None
        exit_yes_price = None

        # ── PASO C: Take Profit — YES subió al umbral ─────────────────────────
        # Solo aplica si ya subió al menos 5pp desde la entrada (evitar ruido)
        if current_yes >= TAKE_PROFIT_THRESHOLD and current_yes > pos["entry_yes"] + 0.05:
            # Usamos el bid (precio al que podríamos vender) si existe, si no el ask
            sell_price = bid if bid is not None else current_yes
            if sell_price >= TAKE_PROFIT_THRESHOLD:
                result         = "TP"
                exit_yes_price = sell_price
                log.info(
                    "💰 TAKE PROFIT — YES=%.1f%% (entry=%.1f%%) sell=%.1f%% | [%s] | %s",
                    current_yes * 100, pos["entry_yes"] * 100, sell_price * 100,
                    pos.get("yes_side", "?"), pos["question"][:55],
                )

        # ── PASO D: LOST vía CLOB NO >= 0.99 ─────────────────────────────────
        if result is None and current_no >= 0.99:
            result         = "LOST"
            exit_yes_price = 0.0

        # ── PASO E: Timeout post-partido → LOST ──────────────────────────────
        if result is None and pos.get("end_date"):
            try:
                end_dt = datetime.fromisoformat(pos["end_date"])
                if now > end_dt + timedelta(hours=GRACE_HOURS):
                    log.info(
                        "⏱️  %dh gracia agotadas, Gamma no resolvió → LOST | [%s] | %s",
                        GRACE_HOURS, pos.get("yes_side", "?"), pos["question"][:55],
                    )
                    result         = "LOST"
                    exit_yes_price = 0.0
            except Exception:
                pass

        if result:
            close_position(cid, pos, result, exit_yes_price, state, now)
            closed_ids.append(cid)

    for cid in closed_ids:
        open_pos.pop(cid, None)

    s = state["stats"]
    log.info(
        "── Stats: total=%d won=%d tp=%d lost=%d PnL=$%+.4f | Open=%d ──",
        s["total"], s["won"], s.get("tp", 0), s["lost"], s["pnl"], len(open_pos),
    )


def print_report():
    if not CSV_FILE.exists():
        print("No hay resultados todavía.")
        return

    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("CSV vacío.")
        return

    total    = len(rows)
    won      = sum(1 for r in rows if r["result"] == "WON")
    tp       = sum(1 for r in rows if r["result"] == "TP")
    lost     = sum(1 for r in rows if r["result"] == "LOST")
    pnl      = sum(float(r["pnl_usd"]) for r in rows)
    inv      = sum(float(r["allocated_usd"]) for r in rows)
    won_pnl  = sum(float(r["pnl_usd"]) for r in rows if r["result"] in ("WON", "TP"))
    lost_pnl = sum(float(r["pnl_usd"]) for r in rows if r["result"] == "LOST")
    hit_rt   = (won + tp) / total * 100 if total > 0 else 0

    print(f"\n{'='*60}")
    print(f"  REPORTE — Underdog Hunter")
    print(f"{'='*60}")
    print(f"  Total cerradas            : {total}")
    print(f"  WON (Gamma 1.0)           : {won}")
    print(f"  TP  (Take Profit >{TAKE_PROFIT_THRESHOLD*100:.0f}%)      : {tp}")
    print(f"  LOST                      : {lost}")
    print(f"  Hit rate (WON+TP)         : {hit_rt:.1f}%")
    print(f"  ─────────────────────────────────────────")
    print(f"  PnL WON+TP                : ${won_pnl:+.4f}")
    print(f"  PnL LOST                  : ${lost_pnl:+.4f}")
    print(f"  PnL TOTAL                 : ${pnl:+.4f}")
    print(f"  Capital simulado          : ${inv:.2f}")
    print(f"  ROI                       : {pnl/inv*100:+.2f}%" if inv > 0 else "  ROI: n/a")
    print(f"{'='*60}")
    print(f"  TP: vende tokens al precio CLOB actual (bid)")
    print(f"  Ej: compra YES=5% ($0.05) → tokens=20 → vende a 30% → PnL=+$5")
    print(f"{'='*60}\n")

    print(f"{'Result':<6} {'Apuesta':<22} {'Buy%':>5} {'Sell%':>6} {'PnL':>7}  {'Dur':>5}  Pregunta")
    print("-" * 100)
    for r in rows[-25:]:
        print(
            f"{r['result']:<6} {r.get('yes_side','?'):<22}"
            f" {float(r['entry_yes_price'])*100:>5.1f}%"
            f" {float(r['exit_yes_price'])*100:>5.1f}%"
            f" ${float(r['pnl_usd']):>+6.2f}"
            f" {float(r['duration_min']):>5.0f}m"
            f"  {r['question'][:40]}"
        )


def main():
    parser = argparse.ArgumentParser(description="Underdog Hunter")
    parser.add_argument("--loop",     action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--report",   action="store_true")
    parser.add_argument("--reset",    action="store_true")
    args = parser.parse_args()

    if args.report:
        print_report()
        return

    if args.reset:
        for f in [CSV_FILE, STATE_FILE]:
            if f.exists():
                f.unlink()
        print("Borrado. Listo.")
        return

    init_csv()
    state = load_state()
    if "closed_cids" not in state:
        state["closed_cids"] = []
    if "tp" not in state["stats"]:
        state["stats"]["tp"] = 0

    log.info("YES umbral entrada: <%.0f%% | Take Profit: %.0f%% | Entrada: $%.2f",
             YES_MAX_THRESHOLD * 100, TAKE_PROFIT_THRESHOLD * 100, FIXED_ENTRY_USD)

    if args.loop:
        log.info("Loop cada %ds. Ctrl+C para parar.", args.interval)
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
