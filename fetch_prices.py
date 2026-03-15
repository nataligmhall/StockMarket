#!/usr/bin/env python3
"""
fetch_prices.py
Fetches live prices for the Iran War Market Dashboard using yfinance (free, no API key).
Rewrites the DATA_JSON block inside index.html, then commits via git.
Run daily via GitHub Actions.
"""

import email.utils
import json
import re
import sys
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
from urllib.request import urlopen, Request
import yfinance as yf

# ── Tickers to track ─────────────────────────────────────────────────────────
TICKERS = {
    # Defense
    "LMT":  {"name": "Lockheed Martin",    "sector": "Defense",    "side": "winner"},
    "RTX":  {"name": "RTX Corp",           "sector": "Defense",    "side": "winner"},
    "NOC":  {"name": "Northrop Grumman",   "sector": "Defense",    "side": "winner"},
    # Energy
    "XOM":  {"name": "ExxonMobil",         "sector": "Energy",     "side": "winner"},
    "CVX":  {"name": "Chevron",            "sector": "Energy",     "side": "winner"},
    "COP":  {"name": "ConocoPhillips",     "sector": "Energy",     "side": "winner"},
    "LNG":  {"name": "Cheniere Energy",    "sector": "LNG",        "side": "winner"},
    # Safe haven
    "GLD":  {"name": "Gold ETF (SPDR)",    "sector": "Safe Haven", "side": "winner"},
    # Airlines / Travel losers
    "UAL":  {"name": "United Airlines",    "sector": "Airlines",   "side": "loser"},
    "AAL":  {"name": "American Airlines",  "sector": "Airlines",   "side": "loser"},
    "DAL":  {"name": "Delta Air Lines",    "sector": "Airlines",   "side": "loser"},
    "CCL":  {"name": "Carnival Corp",      "sector": "Travel",     "side": "loser"},
    # Benchmark
    "^GSPC": {"name": "S&P 500",           "sector": "Index",      "side": "benchmark"},
}

# War start date – used to calculate % change since onset
WAR_START = "2026-02-27"

# ── Technical indicator helpers ───────────────────────────────────────────────

def calculate_rsi(closes, period=14):
    """Wilder's smoothed RSI over `period` bars. Returns None if insufficient data."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    gains  = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def calculate_mfi(highs, lows, closes, volumes, period=14):
    """14-period Money Flow Index. Returns None if insufficient data."""
    if len(closes) < period + 1:
        return None

    tp  = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(closes))]
    rmf = [tp[i] * volumes[i] for i in range(len(closes))]

    pos_mf, neg_mf = [], []
    for i in range(1, len(closes)):
        if tp[i] > tp[i - 1]:
            pos_mf.append(rmf[i]); neg_mf.append(0.0)
        elif tp[i] < tp[i - 1]:
            pos_mf.append(0.0);    neg_mf.append(rmf[i])
        else:
            pos_mf.append(0.0);    neg_mf.append(0.0)

    pos_sum = sum(pos_mf[-period:])
    neg_sum = sum(neg_mf[-period:])

    if neg_sum == 0:
        return 100.0
    return round(100 - (100 / (1 + pos_sum / neg_sum)), 1)


# ── Swing-trading indicator helpers ──────────────────────────────────────────

def calculate_ema_series(closes, period):
    """Full EMA series; pre-period bars are None."""
    if len(closes) < period:
        return [None] * len(closes)
    k = 2 / (period + 1)
    seed = sum(closes[:period]) / period
    result = [None] * (period - 1) + [seed]
    for price in closes[period:]:
        seed = price * k + seed * (1 - k)
        result.append(seed)
    return result


def calculate_macd(closes, fast=12, slow=26, signal_period=9):
    """MACD(12,26,9). Returns (macd_val, signal_val, histogram) or (None,None,None)."""
    if len(closes) < slow + signal_period:
        return None, None, None
    ema_fast = calculate_ema_series(closes, fast)
    ema_slow = calculate_ema_series(closes, slow)
    macd_line = [
        (f - s if f is not None and s is not None else None)
        for f, s in zip(ema_fast, ema_slow)
    ]
    macd_vals = [v for v in macd_line if v is not None]
    if len(macd_vals) < signal_period:
        return None, None, None
    k = 2 / (signal_period + 1)
    sig = sum(macd_vals[:signal_period]) / signal_period
    for v in macd_vals[signal_period:]:
        sig = v * k + sig * (1 - k)
    current_macd = macd_vals[-1]
    return round(current_macd, 4), round(sig, 4), round(current_macd - sig, 4)


def calculate_bollinger_bands(closes, period=20):
    """Bollinger Bands (SMA20 ± 2σ). Returns (upper, mid, lower, pct_b) or Nones."""
    if len(closes) < period:
        return None, None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    std = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    upper = round(mid + 2 * std, 2)
    lower = round(mid - 2 * std, 2)
    mid   = round(mid, 2)
    pct_b = round((closes[-1] - lower) / (upper - lower), 3) if upper != lower else 0.5
    return upper, mid, lower, pct_b


def calculate_atr(highs, lows, closes, period=14):
    """14-period Average True Range using Wilder's smoothing."""
    if len(closes) < period + 1:
        return None
    true_ranges = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    if len(true_ranges) < period:
        return None
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 4)


def calculate_stochastic(highs, lows, closes, k_period=14, d_period=3):
    """Fast Stochastic Oscillator (%K, %D). Returns (k, d) or (None, None)."""
    n = len(closes)
    if n < k_period + d_period:
        return None, None
    k_vals = []
    for i in range(k_period - 1, n):
        hh = max(highs[i - k_period + 1: i + 1])
        ll = min(lows[i  - k_period + 1: i + 1])
        k_vals.append(50.0 if hh == ll else round((closes[i] - ll) / (hh - ll) * 100, 2))
    if len(k_vals) < d_period:
        return None, None
    d = round(sum(k_vals[-d_period:]) / d_period, 2)
    return round(k_vals[-1], 2), d


def calculate_volume_surge(volumes, closes, period=20):
    """
    Volume surge check. Returns +1 if bullish surge, -1 if bearish, 0 otherwise.
    A surge is current volume > 1.5x the period-day average (excluding today).
    """
    if len(volumes) < period + 1 or len(closes) < 2:
        return 0
    avg = sum(volumes[-(period + 1):-1]) / period
    if avg <= 0:
        return 0
    if volumes[-1] / avg > 1.5:
        return 1 if closes[-1] >= closes[-2] else -1
    return 0


def generate_swing_signal(closes, highs, lows, volumes, rsi, mfi):
    """
    7-factor swing trading signal. Score range −7 to +7.
      ≥+4 → STRONG BUY  |  +2/+3 → BUY  |  −1 to +1 → HOLD
      −2/−3 → SELL       |  ≤−4   → STRONG SELL
    Factors: RSI · MFI · MACD · SMA20 · Bollinger %B · Stochastic · Volume surge
    """
    score, reasons = 0, []

    # Rule 1 – RSI momentum
    if rsi is not None:
        if rsi <= 30:
            score += 1; reasons.append(f"RSI {rsi} → oversold, bounce candidate")
        elif rsi >= 70:
            score -= 1; reasons.append(f"RSI {rsi} → overbought, pullback risk")

    # Rule 2 – MFI volume pressure
    if mfi is not None:
        if mfi <= 30:
            score += 1; reasons.append(f"MFI {mfi} → oversold volume, buying pressure")
        elif mfi >= 80:
            score -= 1; reasons.append(f"MFI {mfi} → overbought volume, selling pressure")

    # Rule 3 – MACD trend direction
    macd_val, sig_val, histogram = calculate_macd(closes)
    if histogram is not None:
        if histogram > 0:
            score += 1; reasons.append(f"MACD hist +{histogram:.3f} → bullish momentum")
        else:
            score -= 1; reasons.append(f"MACD hist {histogram:.3f} → bearish momentum")

    # Rule 4 – Price vs 20-day SMA
    sma20 = None
    if len(closes) >= 20:
        sma20 = round(sum(closes[-20:]) / 20, 2)
        if closes[-1] > sma20:
            score += 1; reasons.append(f"Price ${closes[-1]:.2f} above SMA20 ${sma20}")
        else:
            score -= 1; reasons.append(f"Price ${closes[-1]:.2f} below SMA20 ${sma20}")

    # Rule 5 – Bollinger Band position
    bb_upper, bb_mid, bb_lower, pct_b = calculate_bollinger_bands(closes)
    if pct_b is not None:
        if pct_b <= 0.2:
            score += 1; reasons.append(f"Near lower Bollinger Band (%B {pct_b:.2f})")
        elif pct_b >= 0.8:
            score -= 1; reasons.append(f"Near upper Bollinger Band (%B {pct_b:.2f})")

    # Rule 6 – Stochastic momentum/timing
    stoch_k, stoch_d = calculate_stochastic(highs, lows, closes)
    if stoch_k is not None:
        if stoch_k <= 20:
            score += 1; reasons.append(f"Stoch %K {stoch_k:.1f} → oversold zone")
        elif stoch_k >= 80:
            score -= 1; reasons.append(f"Stoch %K {stoch_k:.1f} → overbought zone")
        elif stoch_d is not None:
            if stoch_k > stoch_d:
                score += 1; reasons.append(f"Stoch %K {stoch_k:.1f} > %D {stoch_d:.1f} → bullish crossover")
            elif stoch_k < stoch_d:
                score -= 1; reasons.append(f"Stoch %K {stoch_k:.1f} < %D {stoch_d:.1f} → bearish crossover")

    # Rule 7 – Volume surge confirmation
    vol_surge = calculate_volume_surge(volumes, closes)
    if vol_surge == 1:
        score += 1; reasons.append("Volume surge confirms bullish move (>1.5× avg)")
    elif vol_surge == -1:
        score -= 1; reasons.append("Volume surge confirms bearish move (>1.5× avg)")

    if   score >= 4:  label = "STRONG BUY"
    elif score >= 2:  label = "BUY"
    elif score <= -4: label = "STRONG SELL"
    elif score <= -2: label = "SELL"
    else:             label = "HOLD"

    return {
        "signal":    label,
        "score":     score,
        "reasons":   reasons,
        "macd":      macd_val,
        "macd_sig":  sig_val,
        "macd_hist": histogram,
        "sma20":     sma20,
        "bb_upper":  bb_upper,
        "bb_lower":  bb_lower,
        "bb_pct_b":  pct_b,
        "stoch_k":   stoch_k,
        "stoch_d":   stoch_d,
    }


# ── Price fetch ───────────────────────────────────────────────────────────────

def fetch_data():
    print("Fetching price history...")
    results = {}

    # Need ~20 trading days of pre-war history to prime RSI/MFI
    rsi_start = (datetime.strptime(WAR_START, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    war_date  = datetime.strptime(WAR_START, "%Y-%m-%d").date()

    for symbol, meta in TICKERS.items():
        try:
            ticker    = yf.Ticker(symbol)
            hist_full = ticker.history(start=rsi_start, interval="1d")
            if hist_full.empty:
                print(f"  ⚠ No data for {symbol}")
                continue

            # Slice from war-start for baseline / series
            hist     = hist_full[hist_full.index.date >= war_date]
            closes_w = hist["Close"].dropna()
            if closes_w.empty:
                continue

            baseline   = float(closes_w.iloc[0])
            current    = float(closes_w.iloc[-1])
            pct_change = round((current - baseline) / baseline * 100, 2)
            series     = [round(float(v), 2) for v in closes_w.tolist()[-20:]]
            dates      = [d.strftime("%b %d") for d in closes_w.index.tolist()[-20:]]

            # Full OHLCV history for all indicators (pre-war lookback for accurate priming)
            closes_full  = hist_full["Close"].dropna().tolist()
            highs_full   = hist_full["High"].dropna().tolist()
            lows_full    = hist_full["Low"].dropna().tolist()
            volumes_full = hist_full["Volume"].dropna().tolist()

            rsi_val = calculate_rsi(closes_full)

            mfi_val = atr_val = stoch_k = stoch_d = None
            try:
                mfi_val         = calculate_mfi(highs_full, lows_full, closes_full, volumes_full)
                atr_val         = calculate_atr(highs_full, lows_full, closes_full)
                stoch_k, stoch_d = calculate_stochastic(highs_full, lows_full, closes_full)
            except Exception:
                pass

            # Swing trading signal (7-factor: RSI · MFI · MACD · SMA20 · BB · Stochastic · Volume)
            swing = generate_swing_signal(closes_full, highs_full, lows_full, volumes_full, rsi_val, mfi_val)

            results[symbol] = {
                **meta,
                "ticker":     symbol,
                "current":    round(current, 2),
                "baseline":   round(baseline, 2),
                "pct_change": pct_change,
                "series":     series,
                "dates":      dates,
                "rsi":        rsi_val,
                "mfi":        mfi_val,
                "atr":        atr_val,
                "stoch_k":    stoch_k,
                "stoch_d":    stoch_d,
                "swing":      swing,
            }
            sign    = "+" if pct_change >= 0 else ""
            rsi_str = f"RSI {rsi_val}" if rsi_val is not None else "RSI n/a"
            sk_str  = f"Stoch {stoch_k:.0f}" if stoch_k is not None else "Stoch n/a"
            print(f"  ✓ {symbol:6s}  ${current:.2f}  ({sign}{pct_change}%)  {rsi_str}  {sk_str}  [{swing['signal']}]")

        except Exception as e:
            print(f"  ✗ {symbol}: {e}")

    return results


def fetch_oil():
    """Fetch Brent (BZ=F) and WTI (CL=F) crude futures."""
    oil = {}
    for sym, label in [("BZ=F", "Brent"), ("CL=F", "WTI")]:
        try:
            hist = yf.Ticker(sym).history(start=WAR_START, interval="1d")
            if hist.empty:
                continue
            closes = hist["Close"].dropna()
            oil[label] = {
                "dates":   [d.strftime("%b %d") for d in closes.index.tolist()],
                "prices":  [round(float(v), 2) for v in closes.tolist()],
                "current": round(float(closes.iloc[-1]), 2),
                "start":   round(float(closes.iloc[0]), 2),
            }
            print(f"  ✓ {sym:8s}  ${oil[label]['current']:.2f}")
        except Exception as e:
            print(f"  ✗ {sym}: {e}")
    return oil


def fetch_macro():
    """Fetch VIX fear index and 10-Year Treasury yield (^TNX)."""
    print("\nFetching macro indicators (VIX, 10Y yield)...")
    macro = {}
    for sym, label, unit in [("^VIX", "VIX", ""), ("^TNX", "10Y Yield", "%")]:
        try:
            hist = yf.Ticker(sym).history(period="5d", interval="1d")
            if hist.empty:
                continue
            closes = hist["Close"].dropna()
            val = round(float(closes.iloc[-1]), 2)
            macro[label] = {"current": val, "unit": unit}
            print(f"  ✓ {sym:6s}  {val}{unit}")
        except Exception as e:
            print(f"  ✗ {sym}: {e}")
    return macro


def fetch_news():
    """Pull relevant headlines from free RSS feeds (no API key required)."""
    print("\nFetching news RSS feeds...")

    feeds = [
        ("BBC Middle East",  "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"),
        ("Google News",      "https://news.google.com/rss/search?q=Iran+war+oil+defense+market&hl=en-US&gl=US&ceid=US:en"),
        ("Reuters World",    "https://feeds.reuters.com/reuters/worldNews"),
    ]

    KEYWORDS = [
        "iran", "hormuz", "oil", "crude", "defense", "military", "strike",
        "war", "sanction", "lockheed", "raytheon", "northrop", "brent",
        "wti", "conflict", "missile", "irgc", "strait", "opec",
    ]

    items  = []
    hdrs   = {"User-Agent": "Mozilla/5.0 (compatible; MarketDashBot/1.0)"}

    for source, url in feeds:
        try:
            req  = Request(url, headers=hdrs)
            data = urlopen(req, timeout=10).read()
            root = ET.fromstring(data)

            for item in root.iter("item"):
                title_el  = item.find("title")
                link_el   = item.find("link")
                pubdate_el = item.find("pubDate")
                if title_el is None or not title_el.text:
                    continue

                title = title_el.text.strip()
                link  = (link_el.text or "").strip() if link_el is not None else ""
                pub   = pubdate_el.text.strip() if pubdate_el is not None and pubdate_el.text else ""

                if not any(kw in title.lower() for kw in KEYWORDS):
                    continue

                try:
                    parsed = email.utils.parsedate(pub)
                    dt = datetime(*parsed[:6]) if parsed else datetime.utcnow()
                except Exception:
                    dt = datetime.utcnow()

                items.append({
                    "title":  title,
                    "source": source,
                    "url":    link,
                    "time":   pub[:16] if pub else "",
                    "ts":     dt.timestamp(),
                })

            print(f"  ✓ {source}")
        except Exception as e:
            print(f"  ✗ {source}: {e}")

    # Sort by recency, deduplicate on first 40 chars
    items.sort(key=lambda x: x["ts"], reverse=True)
    seen, unique = set(), []
    for it in items:
        key = it["title"][:40].lower()
        if key not in seen:
            seen.add(key)
            unique.append(it)
        if len(unique) >= 8:
            break

    for it in unique:
        del it["ts"]  # don't serialise epoch float into JSON

    print(f"  → {len(unique)} relevant headlines collected")
    return unique


def rewrite_html(stock_data, oil_data, macro_data, news_data):
    html_path = "index.html"
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        print(f"✗ {html_path} not found — run from repo root")
        sys.exit(1)

    payload = {
        "updated":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "war_start": WAR_START,
        "stocks":    stock_data,
        "oil":       oil_data,
        "macro":     macro_data,
        "news":      news_data,
    }

    json_str = json.dumps(payload, indent=2)
    pattern  = r"(<!-- DATA_JSON_START -->).*?(<!-- DATA_JSON_END -->)"
    replacement = (
        f"<!-- DATA_JSON_START -->\n"
        f"<script id=\"dashData\" type=\"application/json\">\n"
        f"{json_str}\n"
        f"</script>\n"
        f"<!-- DATA_JSON_END -->"
    )

    new_html, n = re.subn(pattern, replacement, html, flags=re.DOTALL)
    if n == 0:
        print("✗ Sentinel comments not found in index.html")
        sys.exit(1)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"\n✓ index.html updated — {len(stock_data)} stocks · oil · macro · {len(news_data)} headlines")


if __name__ == "__main__":
    stock_data = fetch_data()
    print("\nFetching oil futures...")
    oil_data   = fetch_oil()
    macro_data = fetch_macro()
    news_data  = fetch_news()
    rewrite_html(stock_data, oil_data, macro_data, news_data)
