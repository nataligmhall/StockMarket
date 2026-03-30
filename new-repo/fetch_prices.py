#!/usr/bin/env python3
"""
fetch_prices.py — Stock Market Dashboard data fetcher.

Pulls price history + technical indicators for a curated list of stocks,
grouped by sector, and injects a JSON payload into index.html between:
    <!-- DATA_JSON_START --> ... <!-- DATA_JSON_END -->

Runs via GitHub Actions every 15 minutes on weekdays.
No API keys required — uses yfinance (Yahoo Finance).
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree
from urllib.request import urlopen, Request
from urllib.error import URLError

import yfinance as yf

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Baseline date: % change is computed from this date forward.
# Change to any YYYY-MM-DD that marks the start of your tracking period.
BASELINE_DATE = "2026-01-01"

# Days of history to load before BASELINE_DATE so indicators can warm up.
INDICATOR_WARMUP_DAYS = 60

# Keep this many data points in the chart series.
SERIES_MAX_POINTS = 90

# Benchmark indices shown in the macro bar (not in sector cards).
BENCHMARK_TICKERS = {
    "^GSPC": "S&P 500",
    "^NDX":  "Nasdaq 100",
    "^DJI":  "Dow Jones",
}

# Stocks grouped by sector. Edit freely.
# NOTE: ISCC does not appear to be a valid ticker and has been omitted.
#       AVGO and COIN appeared twice in the source list — deduplicated here.
SECTORS: dict[str, dict[str, str]] = {
    "Large-Cap Tech": {
        "GOOGL": "Alphabet (Google)",
        "MSFT":  "Microsoft",
        "META":  "Meta Platforms",
        "ORCL":  "Oracle",
        "ADBE":  "Adobe",
    },
    "Semiconductors": {
        "NVDA": "NVIDIA",
        "AVGO": "Broadcom",
        "QCOM": "Qualcomm",
        "LSCC": "Lattice Semiconductor",
    },
    "Cybersecurity": {
        "CRWD": "CrowdStrike",
    },
    "Fintech & Finance": {
        "MA":   "Mastercard",
        "BAC":  "Bank of America",
        "PYPL": "PayPal",
        "COIN": "Coinbase",
    },
    "Healthcare & Pharma": {
        "MRK":  "Merck",
        "CVS":  "CVS Health",
        "VALN": "Valneva SE",
        "ANGX": "Angion Biomedica",
    },
    "Retail & Consumer": {
        "WMT":  "Walmart",
        "SBUX": "Starbucks",
        "SIG":  "Signet Jewelers",
        "BKE":  "Buckle Inc.",
        "DIS":  "Walt Disney",
    },
    "Energy": {
        "ET":  "Energy Transfer",
        "OXY": "Occidental Petroleum",
    },
    "Industrial & Infrastructure": {
        "XYL":   "Xylem Inc.",
        "VEOEY": "Veolia Environnement",
    },
    "Emerging Tech": {
        "IMMR": "Immersion Corp.",
        "VUZI": "Vuzix Corp.",
        "U":    "Unity Software",
        "LITE": "Lumentum Holdings",
    },
}

# Brief sector descriptions shown in the dashboard.
SECTOR_DESCRIPTIONS: dict[str, str] = {
    "Large-Cap Tech": (
        "The dominant software, cloud, and digital-advertising platforms. "
        "AI integration is a key growth driver across all names."
    ),
    "Semiconductors": (
        "Chip designers and foundry enablers powering AI, data centers, and "
        "mobile devices. Heavily exposed to US–China trade dynamics."
    ),
    "Cybersecurity": (
        "Pure-play cybersecurity platform with cloud-native endpoint and SIEM "
        "offerings. Demand remains structurally high across enterprise and government."
    ),
    "Fintech & Finance": (
        "A mix of legacy banking (BAC) and fintech disruptors. Payments volume, "
        "interest rates, and crypto sentiment all drive performance here."
    ),
    "Healthcare & Pharma": (
        "Ranges from large diversified pharma (MRK, CVS) to small-cap biotech "
        "(VALN, ANGX). Drug pipeline events and FDA decisions are key catalysts."
    ),
    "Retail & Consumer": (
        "Covers defensive big-box retail (WMT) through discretionary dining, "
        "jewellery, and entertainment. Consumer spending data drives sentiment."
    ),
    "Energy": (
        "Midstream MLP (ET) and integrated E&P (OXY). Driven by oil/gas prices, "
        "pipeline throughput, and capital-return programs."
    ),
    "Industrial & Infrastructure": (
        "Water-technology and environmental-services companies with recurring "
        "municipal and industrial revenue. Defensive, ESG-aligned."
    ),
    "Emerging Tech": (
        "Small/mid-cap companies in haptics, augmented reality, gaming engines, "
        "and photonics. Higher volatility; sentiment-driven; watch for catalysts."
    ),
}

# ---------------------------------------------------------------------------
# Technical-indicator helpers (identical logic to original repo)
# ---------------------------------------------------------------------------

def _ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average."""
    k = 2.0 / (period + 1)
    result: list[float] = []
    ema = values[0]
    for v in values:
        ema = v * k + ema * (1 - k)
        result.append(ema)
    return result


def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder-smoothed RSI."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def calc_mfi(highs: list[float], lows: list[float],
             closes: list[float], volumes: list[float],
             period: int = 14) -> float | None:
    """Money Flow Index."""
    n = min(len(highs), len(lows), len(closes), len(volumes))
    if n < period + 1:
        return None
    typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    pos_flow, neg_flow = 0.0, 0.0
    for i in range(n - period, n):
        mf = typical[i] * volumes[i]
        if typical[i] >= typical[i - 1]:
            pos_flow += mf
        else:
            neg_flow += mf
    if neg_flow == 0:
        return 100.0
    return round(100 - 100 / (1 + pos_flow / neg_flow), 2)


def calc_macd(closes: list[float],
              fast: int = 12, slow: int = 26, signal: int = 9
              ) -> dict | None:
    """MACD, signal line, and histogram."""
    if len(closes) < slow + signal:
        return None
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = _ema(macd_line, signal)
    hist = macd_line[-1] - signal_line[-1]
    return {
        "macd":      round(macd_line[-1], 4),
        "signal":    round(signal_line[-1], 4),
        "histogram": round(hist, 4),
    }


def calc_bollinger(closes: list[float], period: int = 20) -> dict | None:
    """Bollinger Bands (±2σ) and %B."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    sma = sum(window) / period
    std = (sum((x - sma) ** 2 for x in window) / period) ** 0.5
    upper, lower = sma + 2 * std, sma - 2 * std
    pct_b = (closes[-1] - lower) / (upper - lower) if upper != lower else 0.5
    return {
        "upper": round(upper, 4),
        "mid":   round(sma, 4),
        "lower": round(lower, 4),
        "pct_b": round(pct_b, 4),
    }


def calc_sma(closes: list[float], period: int = 20) -> float | None:
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 4)


def calc_swing_signal(
    close: float,
    rsi: float | None,
    mfi: float | None,
    macd: dict | None,
    bb: dict | None,
    sma20: float | None,
) -> dict:
    """Composite swing-trading signal (score −5 … +5)."""
    score = 0
    reasons: list[str] = []

    if rsi is not None:
        if rsi <= 30:
            score += 1; reasons.append(f"RSI {rsi:.1f} — oversold (≤30)")
        elif rsi >= 70:
            score -= 1; reasons.append(f"RSI {rsi:.1f} — overbought (≥70)")
        else:
            reasons.append(f"RSI {rsi:.1f} — neutral")

    if mfi is not None:
        if mfi <= 30:
            score += 1; reasons.append(f"MFI {mfi:.1f} — oversold volume (≤30)")
        elif mfi >= 80:
            score -= 1; reasons.append(f"MFI {mfi:.1f} — overbought volume (≥80)")
        else:
            reasons.append(f"MFI {mfi:.1f} — neutral")

    if macd is not None:
        if macd["histogram"] > 0:
            score += 1; reasons.append(f"MACD hist +{macd['histogram']:.4f} — bullish cross")
        else:
            score -= 1; reasons.append(f"MACD hist {macd['histogram']:.4f} — bearish cross")

    if sma20 is not None:
        if close > sma20:
            score += 1; reasons.append(f"Price ${close:.2f} > SMA20 ${sma20:.2f}")
        else:
            score -= 1; reasons.append(f"Price ${close:.2f} < SMA20 ${sma20:.2f}")

    if bb is not None:
        if bb["pct_b"] <= 0.20:
            score += 1; reasons.append(f"%B {bb['pct_b']:.2f} — near lower band")
        elif bb["pct_b"] >= 0.80:
            score -= 1; reasons.append(f"%B {bb['pct_b']:.2f} — near upper band")
        else:
            reasons.append(f"%B {bb['pct_b']:.2f} — mid-band")

    if score >= 3:
        label = "STRONG BUY"
    elif score >= 1:
        label = "BUY"
    elif score <= -3:
        label = "STRONG SELL"
    elif score <= -1:
        label = "SELL"
    else:
        label = "HOLD"

    result: dict = {"signal": label, "score": score, "reasons": reasons}
    if macd:
        result.update(macd)
    if bb:
        result.update({"bb_upper": bb["upper"], "bb_lower": bb["lower"],
                       "bb_pct_b": bb["pct_b"]})
    if sma20:
        result["sma20"] = sma20
    return result


# ---------------------------------------------------------------------------
# Data-fetching helpers
# ---------------------------------------------------------------------------

def _fetch_start() -> str:
    """Date string INDICATOR_WARMUP_DAYS before BASELINE_DATE."""
    d = datetime.strptime(BASELINE_DATE, "%Y-%m-%d") - timedelta(days=INDICATOR_WARMUP_DAYS)
    return d.strftime("%Y-%m-%d")


def fetch_stocks() -> dict:
    """Fetch OHLCV data for every ticker and compute indicators."""
    fetch_from = _fetch_start()
    baseline_dt = datetime.strptime(BASELINE_DATE, "%Y-%m-%d")
    all_tickers = [t for sector in SECTORS.values() for t in sector]

    print(f"Fetching {len(all_tickers)} tickers from {fetch_from} …")
    raw = yf.download(
        all_tickers,
        start=fetch_from,
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    results: dict = {}

    for sector_name, ticker_map in SECTORS.items():
        for ticker, name in ticker_map.items():
            try:
                if len(all_tickers) == 1:
                    df = raw
                else:
                    df = raw[ticker] if ticker in raw.columns.get_level_values(0) else None

                if df is None or df.empty:
                    print(f"  [WARN] No data for {ticker}", file=sys.stderr)
                    continue

                df = df.dropna(subset=["Close"])

                closes  = df["Close"].tolist()
                highs   = df["High"].tolist()
                lows    = df["Low"].tolist()
                volumes = df["Volume"].tolist()
                dates_all = [str(d.date()) for d in df.index]

                # Baseline price: closing price on or just after BASELINE_DATE
                baseline_price = None
                for i, d in enumerate(df.index):
                    if d.date() >= baseline_dt.date():
                        baseline_price = closes[i]
                        break
                if baseline_price is None:
                    baseline_price = closes[0]

                current_price = closes[-1]
                pct_change = round((current_price - baseline_price) / baseline_price * 100, 2)

                # Indicators use all available data for warm-up
                rsi   = calc_rsi(closes)
                mfi   = calc_mfi(highs, lows, closes, volumes)
                macd  = calc_macd(closes)
                bb    = calc_bollinger(closes)
                sma20 = calc_sma(closes)
                swing = calc_swing_signal(current_price, rsi, mfi, macd, bb, sma20)

                # Series: only from BASELINE_DATE onward, capped at SERIES_MAX_POINTS
                series_mask = [d.date() >= baseline_dt.date() for d in df.index]
                series_closes = [c for c, m in zip(closes, series_mask) if m]
                series_dates  = [d for d, m in zip(dates_all, series_mask) if m]
                if len(series_closes) > SERIES_MAX_POINTS:
                    series_closes = series_closes[-SERIES_MAX_POINTS:]
                    series_dates  = series_dates[-SERIES_MAX_POINTS:]

                results[ticker] = {
                    "name":         name,
                    "sector":       sector_name,
                    "ticker":       ticker,
                    "current":      round(current_price, 4),
                    "baseline":     round(baseline_price, 4),
                    "pct_change":   pct_change,
                    "series":       [round(v, 4) for v in series_closes],
                    "dates":        series_dates,
                    "rsi":          rsi,
                    "mfi":          mfi,
                    "swing":        swing,
                }
                print(f"  ✓ {ticker:6s} ${current_price:.2f}  ({pct_change:+.2f}%)")

            except Exception as exc:
                print(f"  [ERROR] {ticker}: {exc}", file=sys.stderr)

    return results


def fetch_benchmarks() -> dict:
    """Fetch S&P 500, Nasdaq 100, and Dow Jones index data."""
    fetch_from = _fetch_start()
    baseline_dt = datetime.strptime(BASELINE_DATE, "%Y-%m-%d")
    results: dict = {}

    for ticker, name in BENCHMARK_TICKERS.items():
        try:
            df = yf.download(ticker, start=fetch_from, interval="1d",
                             auto_adjust=True, progress=False)
            df = df.dropna(subset=["Close"])
            if df.empty:
                continue

            closes = df["Close"].tolist()
            dates_all = [str(d.date()) for d in df.index]

            baseline_price = None
            for i, d in enumerate(df.index):
                if d.date() >= baseline_dt.date():
                    baseline_price = closes[i]
                    break
            if baseline_price is None:
                baseline_price = closes[0]

            current_price = closes[-1]
            pct_change = round((current_price - baseline_price) / baseline_price * 100, 2)

            series_mask   = [d.date() >= baseline_dt.date() for d in df.index]
            series_closes = [c for c, m in zip(closes, series_mask) if m]
            series_dates  = [d for d, m in zip(dates_all, series_mask) if m]
            if len(series_closes) > SERIES_MAX_POINTS:
                series_closes = series_closes[-SERIES_MAX_POINTS:]
                series_dates  = series_dates[-SERIES_MAX_POINTS:]

            results[ticker] = {
                "name":       name,
                "ticker":     ticker,
                "current":    round(current_price, 2),
                "baseline":   round(baseline_price, 2),
                "pct_change": pct_change,
                "series":     [round(v, 2) for v in series_closes],
                "dates":      series_dates,
            }
            print(f"  ✓ {ticker:6s} ({name}) {pct_change:+.2f}%")

        except Exception as exc:
            print(f"  [ERROR] benchmark {ticker}: {exc}", file=sys.stderr)

    return results


def fetch_macro() -> dict:
    """Fetch VIX and 10-year Treasury yield."""
    macro: dict = {}
    targets = {"^VIX": ("VIX", ""), "^TNX": ("10Y Yield", "%")}
    for ticker, (label, unit) in targets.items():
        try:
            df = yf.download(ticker, period="5d", interval="1d",
                             auto_adjust=True, progress=False)
            df = df.dropna(subset=["Close"])
            if not df.empty:
                macro[label] = {"current": round(df["Close"].iloc[-1], 2), "unit": unit}
        except Exception as exc:
            print(f"  [ERROR] macro {ticker}: {exc}", file=sys.stderr)
    return macro


def fetch_news(stock_tickers: list[str]) -> list[dict]:
    """Aggregate finance news via yfinance and RSS feeds."""

    FINANCE_KEYWORDS = [
        "stock", "market", "shares", "earnings", "revenue", "profit",
        "nasdaq", "s&p", "dow", "fed", "interest rate", "inflation",
        "quarter", "guidance", "outlook", "analyst", "upgrade", "downgrade",
        "buyback", "dividend", "ipo", "merger", "acquisition",
    ] + [t.lower() for t in stock_tickers[:20]]

    headlines: list[dict] = []
    seen_titles: set[str] = set()

    def add(title: str, source: str, url: str, ts: str) -> None:
        key = title.lower()[:60]
        if key not in seen_titles:
            seen_titles.add(key)
            headlines.append({"title": title, "source": source, "url": url, "time": ts})

    # Tier 1: yfinance news for a sample of tickers
    for ticker in stock_tickers[:8]:
        try:
            info = yf.Ticker(ticker).news or []
            for item in info[:3]:
                t = item.get("title", "")
                u = item.get("link", "#")
                ts_raw = item.get("providerPublishTime", 0)
                ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts_raw else ""
                src = item.get("publisher", ticker)
                add(t, src, u, ts)
        except Exception:
            pass

    # Tier 2: Finance RSS feeds
    RSS_FEEDS = [
        ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
        ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
        ("CNBC",        "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
        ("Yahoo Finance","https://finance.yahoo.com/news/rssindex"),
    ]
    for source, url in RSS_FEEDS:
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=6) as resp:
                tree = ElementTree.fromstring(resp.read())
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            items = tree.findall(".//item") or tree.findall(".//atom:entry", ns)
            for item in items[:10]:
                title_el = item.find("title")
                link_el  = item.find("link")
                pub_el   = item.find("pubDate") or item.find("atom:updated", ns)
                if title_el is None:
                    continue
                title = title_el.text or ""
                url_  = (link_el.text or "#") if link_el is not None else "#"
                ts    = pub_el.text or "" if pub_el is not None else ""
                low   = title.lower()
                if any(kw in low for kw in FINANCE_KEYWORDS):
                    add(title, source, url_, ts)
        except (URLError, Exception):
            pass

    # Tier 3: fallback — read existing headlines from index.html
    if len(headlines) < 3:
        try:
            with open("index.html", encoding="utf-8") as fh:
                html = fh.read()
            m = re.search(r'<!-- DATA_JSON_START -->\s*<script[^>]*>(.*?)</script>\s*<!-- DATA_JSON_END -->',
                          html, re.DOTALL)
            if m:
                old = json.loads(m.group(1))
                for item in old.get("news", []):
                    add(item["title"], item["source"], item["url"], item["time"])
        except Exception:
            pass

    return headlines[:10]


# ---------------------------------------------------------------------------
# Sector aggregation
# ---------------------------------------------------------------------------

def build_sector_summary(stocks: dict) -> dict:
    """Compute per-sector aggregate % change and member list."""
    summary: dict = {}
    for sector_name, ticker_map in SECTORS.items():
        changes = [
            stocks[t]["pct_change"]
            for t in ticker_map
            if t in stocks
        ]
        avg = round(sum(changes) / len(changes), 2) if changes else None
        summary[sector_name] = {
            "label":       sector_name,
            "description": SECTOR_DESCRIPTIONS.get(sector_name, ""),
            "avg_change":  avg,
            "tickers":     [t for t in ticker_map if t in stocks],
        }
    return summary


# ---------------------------------------------------------------------------
# HTML injection
# ---------------------------------------------------------------------------

HTML_FILE = "index.html"
MARKER_START = "<!-- DATA_JSON_START -->"
MARKER_END   = "<!-- DATA_JSON_END -->"


def rewrite_html(payload: dict) -> None:
    try:
        with open(HTML_FILE, encoding="utf-8") as fh:
            html = fh.read()
    except FileNotFoundError:
        print(f"[ERROR] {HTML_FILE} not found — cannot inject data.", file=sys.stderr)
        sys.exit(1)

    json_block = (
        f"{MARKER_START}\n"
        f'<script id="dashData" type="application/json">\n'
        f"{json.dumps(payload, separators=(',', ':'))}\n"
        f"</script>\n"
        f"{MARKER_END}"
    )

    pattern = re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END)
    new_html, n = re.subn(pattern, json_block, html, flags=re.DOTALL)

    if n == 0:
        print("[WARN] Marker pair not found — appending data block before </body>.")
        new_html = html.replace("</body>", f"{json_block}\n</body>")

    with open(HTML_FILE, "w", encoding="utf-8") as fh:
        fh.write(new_html)
    print(f"✓ {HTML_FILE} updated.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== Stock Dashboard update @ {now} ===")

    stocks     = fetch_stocks()
    benchmarks = fetch_benchmarks()
    macro      = fetch_macro()
    sectors    = build_sector_summary(stocks)
    news       = fetch_news(list(stocks.keys()))

    payload = {
        "updated":       now,
        "baseline_date": BASELINE_DATE,
        "sectors":       sectors,
        "stocks":        stocks,
        "benchmarks":    benchmarks,
        "macro":         macro,
        "news":          news,
    }

    rewrite_html(payload)
    print("=== Done ===")


if __name__ == "__main__":
    main()
