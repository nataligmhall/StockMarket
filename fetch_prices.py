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

            # RSI from full history (pre-war lookback for accurate priming)
            closes_full = hist_full["Close"].dropna().tolist()
            rsi_val = calculate_rsi(closes_full)

            # MFI from full history
            mfi_val = None
            try:
                highs   = hist_full["High"].dropna().tolist()
                lows    = hist_full["Low"].dropna().tolist()
                volumes = hist_full["Volume"].dropna().tolist()
                mfi_val = calculate_mfi(highs, lows, closes_full, volumes)
            except Exception:
                pass

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
            }
            sign    = "+" if pct_change >= 0 else ""
            rsi_str = f"RSI {rsi_val}" if rsi_val is not None else "RSI n/a"
            print(f"  ✓ {symbol:6s}  ${current:.2f}  ({sign}{pct_change}%)  {rsi_str}")

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
                    dt = datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S")
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
