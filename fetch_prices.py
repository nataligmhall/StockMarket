#!/usr/bin/env python3
"""
fetch_prices.py
Fetches live prices for the Iran War Market Dashboard using yfinance (free, no API key).
Rewrites the DATA_JSON block inside index.html, then commits via git.
Run daily via GitHub Actions.
"""

import json
import re
import sys
from datetime import datetime, timezone
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

def fetch_data():
    print("Fetching price history...")
    results = {}

    for symbol, meta in TICKERS.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(start=WAR_START, interval="1d")
            if hist.empty:
                print(f"  ⚠ No data for {symbol}")
                continue

            closes = hist["Close"].dropna()
            baseline = float(closes.iloc[0])
            current  = float(closes.iloc[-1])
            pct_change = round((current - baseline) / baseline * 100, 2)

            # Last 20 days for sparkline
            series = [round(float(v), 2) for v in closes.tolist()[-20:]]
            dates  = [d.strftime("%b %d") for d in closes.index.tolist()[-20:]]

            results[symbol] = {
                **meta,
                "ticker":     symbol,
                "current":    round(current, 2),
                "baseline":   round(baseline, 2),
                "pct_change": pct_change,
                "series":     series,
                "dates":      dates,
            }
            sign = "+" if pct_change >= 0 else ""
            print(f"  ✓ {symbol:6s}  ${current:.2f}  ({sign}{pct_change}%)")

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

def rewrite_html(stock_data, oil_data):
    html_path = "index.html"
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        print(f"✗ {html_path} not found — run from repo root")
        sys.exit(1)

    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "war_start": WAR_START,
        "stocks": stock_data,
        "oil": oil_data,
    }

    json_str = json.dumps(payload, indent=2)

    # Replace between sentinel comments
    pattern = r"(<!-- DATA_JSON_START -->).*?(<!-- DATA_JSON_END -->)"
    replacement = f"<!-- DATA_JSON_START -->\n<script id=\"dashData\" type=\"application/json\">\n{json_str}\n</script>\n<!-- DATA_JSON_END -->"

    new_html, n = re.subn(pattern, replacement, html, flags=re.DOTALL)
    if n == 0:
        print("✗ Sentinel comments not found in index.html")
        sys.exit(1)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"\n✓ index.html updated ({len(stock_data)} stocks, oil data)")

if __name__ == "__main__":
    stock_data = fetch_data()
    print("\nFetching oil futures...")
    oil_data = fetch_oil()
    rewrite_html(stock_data, oil_data)
