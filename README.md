# Iran War Market Dashboard

Auto-updating market tracker for defense, energy, airline, and safe-haven stocks since the Feb 28, 2026 US–Israel strikes on Iran.

**Live dashboard → [your-username.github.io/iran-war-dashboard]()**

---

## How it works

```
GitHub Actions (daily, 4pm ET)
       │
       ▼
fetch_prices.py          ← pulls live prices via yfinance (Yahoo Finance)
       │
       ▼
index.html (DATA_JSON)   ← injects fresh prices into a JSON block
       │
       ▼
git commit + push        ← triggers GitHub Pages redeploy (~30s)
       │
       ▼
Live website updated ✓
```

---

## Setup (5 minutes)

### 1. Fork / create the repo

```bash
git clone https://github.com/YOUR_USERNAME/iran-war-dashboard
cd iran-war-dashboard
```

### 2. Enable GitHub Pages

- Go to repo **Settings → Pages**
- Source: **Deploy from branch**
- Branch: `main` · folder: `/ (root)`
- Hit **Save** — your site will be live at `https://YOUR_USERNAME.github.io/REPO_NAME`

### 3. Enable GitHub Actions

Actions are already configured in `.github/workflows/daily-update.yml`.

- Go to **Actions** tab in your repo
- If prompted, click **"I understand my workflows, enable them"**
- That's it — it runs automatically every weekday at 21:00 UTC (4pm ET / after US market close)

### 4. Trigger a manual first run

- Go to **Actions → Daily Price Update → Run workflow**
- This populates the dashboard with real data immediately

---

## Files

| File | Purpose |
|------|---------|
| `index.html` | The dashboard (HTML + Chart.js, self-contained) |
| `fetch_prices.py` | Fetches prices via yfinance, injects into `index.html` |
| `.github/workflows/daily-update.yml` | Runs the script daily on GitHub's servers |

---

## Customize tickers

Edit the `TICKERS` dict in `fetch_prices.py`:

```python
TICKERS = {
    "LMT":  {"name": "Lockheed Martin", "sector": "Defense", "side": "winner"},
    "SHEL": {"name": "Shell",           "sector": "Energy",  "side": "winner"},
    # add any Yahoo Finance ticker symbol here
}
```

Valid `side` values: `winner` · `loser` · `benchmark`

---

## Swing Trading Signal Engine

Each stock is evaluated against **5 independent rules**. Each rule contributes `+1` (bullish), `−1` (bearish), or `0` (neutral) to a composite score.

| Rule | BUY trigger | SELL trigger |
|------|-------------|--------------|
| **RSI(14)** | ≤ 30 — oversold | ≥ 70 — overbought |
| **MFI(14)** | ≤ 30 — oversold volume | ≥ 80 — overbought volume |
| **MACD(12/26/9) histogram** | > 0 — bullish crossover | < 0 — bearish crossover |
| **Price vs SMA20** | price > SMA20 — uptrend | price < SMA20 — downtrend |
| **Bollinger %B** | ≤ 0.20 — near lower band, mean-revert up | ≥ 0.80 — near upper band, mean-revert down |

**Score → Signal label (range −5 to +5):**

| Score | Signal |
|-------|--------|
| +3 to +5 | 🟢 STRONG BUY |
| +1 to +2 | 🟩 BUY |
| 0 | ⬜ HOLD |
| −1 to −2 | 🟥 SELL |
| −3 to −5 | 🔴 STRONG SELL |

---

## Notes

- Data is from Yahoo Finance via `yfinance` — typically 15-minute delayed
- The workflow only runs Mon–Fri (US market days)
- No API keys required — all free
- **Not financial advice**
