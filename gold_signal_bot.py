#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Олтин (XAUUSD) сигнал боти — Telegram учун.
Стратегия: 9/21 EMA кесишуви + 200 EMA тренд + RSI + ADX + Лондон сессияси.
Фақат Python стандарт кутубхонаси керак (pip ўрнатиш шарт эмас).
Бот савдо очмайди, фақат Telegram'га сигнал юборади.

Ишга тушириш:   python gold_signal_bot.py
Синаш:          python gold_signal_bot.py test
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    LONDON = ZoneInfo("Europe/London")
except Exception:  # zoneinfo ёки tzdata йўқ бўлса
    LONDON = None

# ================== СОЗЛАМАЛАР ==================
BOT_TOKEN = "ВАШ_ТОКЕН"      # @BotFather берган токен
CHAT_ID = "ВАШ_CHAT_ID"      # @userinfobot берган рақам

SYMBOL = "XAUUSD=X"          # агар маълумот чиқмаса: "GC=F"
INTERVAL = "15m"             # "15m" ёки "30m"
CHECK_SECONDS = 60           # неча секундда текшириш

FAST, SLOW, TREND = 9, 21, 200
RSI_LEN, ATR_LEN, ADX_LEN = 14, 14, 14
ADX_MIN = 20.0
SL_MULT = 1.5                # Stop-Loss = ATR x 1.5
RR = 2.0                     # Take-Profit = SL x 2
SESSION_START, SESSION_END = 7, 17   # Лондон вақти (соат)
# =================================================

# GitHub Actions'да токен махфий созламалардан (Secrets) келади
BOT_TOKEN = os.environ.get("BOT_TOKEN", BOT_TOKEN)
CHAT_ID = os.environ.get("CHAT_ID", CHAT_ID)
STATE_FILE = "state.json"
API_BASE = "https://api.telegram.org/bot"

# Брокер ва бот нархи фарқи (брокер - бот). Telegram'да /offset орқали ўрнатилади.
OFFSET = 0.0
OFFSET_SET = False
LIVE_PRICE = None


# ---------- Индикаторлар (соф Python) ----------
def ema(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(vals[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rma(vals, n):
    """Wilder силлиқлаш (RSI, ATR, ADX учун). Бошидаги None'лар рухсат."""
    out = [None] * len(vals)
    start = next((i for i, v in enumerate(vals) if v is not None), None)
    if start is None or len(vals) - start < n:
        return out
    prev = sum(vals[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(vals)):
        prev = (prev * (n - 1) + vals[i]) / n
        out[i] = prev
    return out


def rsi(closes, n):
    gains, losses = [None], [None]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = rma(gains, n), rma(losses, n)
    out = []
    for g, l in zip(ag, al):
        if g is None or l is None:
            out.append(None)
        elif l == 0:
            out.append(100.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + g / l))
    return out


def true_range(highs, lows, closes):
    tr = [None]
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
    return tr


def atr(highs, lows, closes, n):
    return rma(true_range(highs, lows, closes), n)


def adx(highs, lows, closes, n):
    tr = true_range(highs, lows, closes)
    pdm, mdm = [None], [None]
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
    s_tr, s_p, s_m = rma(tr, n), rma(pdm, n), rma(mdm, n)
    dx = []
    for a, p, m in zip(s_tr, s_p, s_m):
        if a is None or p is None or m is None or a == 0:
            dx.append(None)
            continue
        pdi, mdi = 100.0 * p / a, 100.0 * m / a
        s = pdi + mdi
        dx.append(0.0 if s == 0 else 100.0 * abs(pdi - mdi) / s)
    return rma(dx, n)


# ---------- Маълумот ва Telegram ----------
SOURCE = SYMBOL


def fetch():
    """Yahoo Finance'дан ёпилган шамларни олади: [(vaqt, high, low, close), ...]
    Бир нечта манзил ва заҳира тикерни кетма-кет синайди."""
    global SOURCE, LIVE_PRICE
    candidates = []
    for sym in (SYMBOL, "GC=F"):
        for host in ("query1", "query2"):
            if (host, sym) not in candidates:
                candidates.append((host, sym))
    errors = []
    for host, sym in candidates:
        url = ("https://" + host + ".finance.yahoo.com/v8/finance/chart/"
               + urllib.parse.quote(sym, safe="=") + "?interval=" + INTERVAL + "&range=10d")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
            res = data["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            rows = [(t, h, l, c) for t, h, l, c in
                    zip(res["timestamp"], q["high"], q["low"], q["close"])
                    if h is not None and l is not None and c is not None]
            if len(rows) < TREND + 25:
                raise ValueError("шам сони кам: " + str(len(rows)))
            SOURCE = sym
            LIVE_PRICE =rows[-2][3]  # res.get("meta", {}).get("regularMarketPrice") or rows[-1][3]
            return rows[:-1]  # охирги шам ҳали шаклланяпти — олиб ташлаймиз
        except Exception as e:
            errors.append(host + " " + sym + ": " + str(e))
    raise RuntimeError("Нарх олиб бўлмади -> " + " | ".join(errors))


def send(text):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    url = "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage"
    urllib.request.urlopen(url, data=data, timeout=20).read()


def in_session(ts):
    if LONDON is not None:
        hour = datetime.fromtimestamp(ts, LONDON).hour
    else:
        hour = datetime.fromtimestamp(ts, timezone.utc).hour
    return SESSION_START <= hour < SESSION_END


# ---------- Таҳлил ----------
def analyze(rows):
    """Охирги ёпилган шам бўйича сигнални қайтаради (ёки None) ва индикатор қийматлари."""
    if len(rows) < TREND + 20:
        return None, None
    ts = [r[0] for r in rows]
    hi = [r[1] for r in rows]
    lo = [r[2] for r in rows]
    cl = [r[3] for r in rows]

    ef, es, et = ema(cl, FAST), ema(cl, SLOW), ema(cl, TREND)
    rs = rsi(cl, RSI_LEN)
    at = atr(hi, lo, cl, ATR_LEN)
    ax = adx(hi, lo, cl, ADX_LEN)

    vals = [ef[-1], ef[-2], es[-1], es[-2], et[-1], rs[-1], at[-1], ax[-1]]
    if any(v is None for v in vals):
        return None, None

    info = {"close": cl[-1], "rsi": rs[-1], "adx": ax[-1], "atr": at[-1],
            "ema_fast": ef[-1], "ema_slow": es[-1], "ema_trend": et[-1],
            "session": in_session(ts[-1])}

    cross_up = ef[-2] <= es[-2] and ef[-1] > es[-1]
    cross_dn = ef[-2] >= es[-2] and ef[-1] < es[-1]
    ok_adx = ax[-1] > ADX_MIN

    side = None
    if cross_up and cl[-1] > et[-1] and 50 < rs[-1] < 70 and ok_adx and info["session"]:
        side = "BUY"
    elif cross_dn and cl[-1] < et[-1] and 30 < rs[-1] < 50 and ok_adx and info["session"]:
        side = "SELL"
    if side is None:
        return None, info

    dist = at[-1] * SL_MULT
    sign = 1 if side == "BUY" else -1
    sig = {"side": side, "entry": cl[-1],
           "sl": cl[-1] - sign * dist,
           "tp": cl[-1] + sign * dist * RR,
           "sl_dist": dist, "tp_dist": dist * RR,
           "rsi": rs[-1], "adx": ax[-1]}
    return sig, info


def format_signal(s, age_min=0):
    icon = "🟢" if s["side"] == "BUY" else "🔴"
    lines = [
        f"{icon} {s['side']} ОЛТИН (XAUUSD) {INTERVAL}",
        f"Сигнал ёши: {age_min} дақиқа",
        "",
        "МАСОФА (асосий, шуни ишлатинг):",
        f"Stop-Loss: {s['sl_dist']:.2f}$",
        f"Take-Profit: {s['tp_dist']:.2f}$",
        "",
    ]
    if OFFSET_SET:
        lines += [
            f"Брокер учун тахминий нарх (фарқ {OFFSET:+.2f} ҳисобга олинган):",
            f"Кириш: ~{s['entry'] + OFFSET:.2f}",
            f"Stop-Loss: ~{s['sl'] + OFFSET:.2f}",
            f"Take-Profit: ~{s['tp'] + OFFSET:.2f}",
            "",
            "Кириш нархини MT5 даги ЖОРИЙ нархдан олинг, SL/TP ни масофа бўйича қўйинг.",
        ]
    else:
        lines += [
            f"Бот нархи ({SOURCE}): {s['entry']:.2f}",
            "Брокер нархидан фарқ бор. MT5 даги жорий нархдан масофа бўйича ҳисобланг.",
            "Фарқни ўргатиш: ботга /offset ЯНГИ_ФАРҚ юборинг (брокер минус бот).",
        ]
    lines += [f"RSI: {s['rsi']:.0f} | ADX: {s['adx']:.0f}",
              "Риск: депозитнинг 1-2%и."]
    return "\n".join(lines)


def interval_seconds():
    return 1800 if INTERVAL == "30m" else 900


def process_commands(state):
    """Telegram'даги /offset буйруқларини ўқийди. Ўзгариш бўлса True қайтаради."""
    try:
        url = (API_BASE + BOT_TOKEN + "/getUpdates?timeout=0&offset="
               + str(state.get("tg_offset", 0)))
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)
    except Exception as e:
        print("getUpdates хато:", e)
        return False

    changed = False
    for upd in data.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        changed = True
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
            continue
        parts = (msg.get("text") or "").strip().replace(",", ".").split()
        if not parts or parts[0].lower().split("@")[0] != "/offset":
            continue
        live = f"{LIVE_PRICE:.2f}" if LIVE_PRICE else "?"
        if len(parts) == 1:
            cur = f"{state['offset']:+.2f}" if "offset" in state else "ўрнатилмаган"
            send(f"Ҳозирги фарқ: {cur}. Ботнинг жорий нархи: {live}.\n"
                 f"Ўрнатиш: /offset -6.8 (брокер нархи минус бот нархи).")
            continue
        try:
            val = float(parts[1])
            if abs(val) > 50:
                raise ValueError
        except ValueError:
            send("Нотўғри қиймат. Мисол: /offset -6.8 (-50 дан 50 гача).")
            continue
        state["offset"] = val
        approx = f"{LIVE_PRICE + val:.2f}" if LIVE_PRICE else "?"
        send(f"✅ Фарқ сақланди: {val:+.2f}\n"
             f"Ботнинг жорий нархи: {live} → брокер учун тахминан {approx}.\n"
             f"Шуни MT5 билан солиштиринг.")
    return changed


def run_once():
    """Бир марта текширади (GitHub Actions учун). Ҳолатни state.json'да сақлайди."""
    global OFFSET, OFFSET_SET
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    last_sent = state.get("last_signal_ts", 0)

    rows = fetch()
    now = time.time()
    changed = process_commands(state)
    if "offset" in state:
        OFFSET, OFFSET_SET = float(state["offset"]), True
    for k in range(3, -1, -1):  # охирги 4 та шам: эскисидан янгисига
        part = rows[:len(rows) - k]
        if len(part) < TREND + 20:
            continue
        sig, _ = analyze(part)
        bar_ts = part[-1][0]
        close_time = bar_ts + interval_seconds()
        if sig and bar_ts > last_sent and now - close_time <= 3600:
            send(format_signal(sig, int(max(now - close_time, 0) // 60)))
            last_sent = bar_ts
            changed = True
            print("Сигнал юборилди:", sig["side"], sig["entry"])

    if changed:
        state["last_signal_ts"] = last_sent
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    print("Текширув тугади. Шам сони:", len(rows), "Манба:", SOURCE)


def main():
    if "ВАШ" in BOT_TOKEN or "ВАШ" in CHAT_ID:
        print("Аввал файл бошидаги BOT_TOKEN ва CHAT_ID ни тўлдиринг.")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "once":
        run_once()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        rows = fetch()
        sig, info = analyze(rows)
        print("Шам сони:", len(rows))
        print("Индикаторлар:", info)
        send("✅ Тест: бот Telegram'га уланди. Нарх: "
             + (f"{info['close']:.2f}" if info else "маълумот етарли эмас")
             + " (манба: " + SOURCE + ")\n"
             + "MT5 нархи билан фарқ бўлса: /offset -6.8 деб юборинг (брокер минус бот).")
        return

    send("✅ Олтин сигнал боти ишга тушди (" + SYMBOL + ", " + INTERVAL + ").")
    last_ts = None
    first = True
    while True:
        try:
            rows = fetch()
            ts = rows[-1][0]
            if ts != last_ts:
                last_ts = ts
                if first:
                    first = False   # эски сигналларни юбормаслик учун
                else:
                    sig, _ = analyze(rows)
                    if sig:
                        send(format_signal(sig))
                        print("Сигнал юборилди:", sig["side"], sig["entry"])
        except Exception as e:
            print("Хато:", e)
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
