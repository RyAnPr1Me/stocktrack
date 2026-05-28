# StockTrack

A comprehensive stock portfolio tracking web app with a Google-style dark mode UI and RGB accent highlights.

## Features

| Feature | Details |
|---|---|
| **Dashboard** | Live market indices, sector performance heatmap, top gainers/losers, portfolio & watchlist widgets |
| **Portfolio** | Add positions with cost basis, unrealized P&L, day gain, asset allocation donut chart |
| **Stock Detail** | Multi-timeframe price charts, key statistics, valuation, financials, analyst consensus, company info, latest news |
| **Watchlist** | Track stocks with price, change, volume, market cap, P/E, 52-week range bar |
| **Markets** | Major indices ticker cards, sector ETF heatmap grid, sector comparison bar chart, top movers table |
| **Search** | Live search by ticker symbol or company name |
| **Auth** | Register, login, persistent sessions, per-user portfolio & watchlist |

## Tech Stack

- **Backend:** Python · Flask · SQLAlchemy · Flask-Login
- **Data:** yfinance (Yahoo Finance)
- **Database:** SQLite (default) or any SQLAlchemy-compatible DB
- **Frontend:** Vanilla JS · Chart.js · Custom CSS (dark theme + RGB accents)

## Quick Start

```bash
# 1. Clone and enter the repo
git clone https://github.com/RyAnPr1Me/stocktrack.git
cd stocktrack

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) Configure environment
cp .env.example .env
# Edit .env and set a SECRET_KEY

# 5. Run the app
python app.py
```

Open **http://localhost:5000** in your browser.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `dev-secret-CHANGE-IN-PROD` | Flask session secret — **always change in production** |
| `DATABASE_URL` | `sqlite:///stocktrack.db` | SQLAlchemy database URI |
| `FLASK_DEBUG` | `0` | Set to `1` to enable debug mode (development only) |
| `PORT` | `5000` | Port to listen on |

## Project Structure

```
stocktrack/
├── app.py                  # Flask application, routes, models, yfinance helpers
├── requirements.txt
├── .env.example
├── templates/
│   ├── base.html           # Sidebar layout for authenticated pages
│   ├── auth_base.html      # Centered layout for login/register
│   ├── landing.html        # Public marketing page
│   ├── dashboard.html      # Main dashboard
│   ├── portfolio.html      # Portfolio tracker
│   ├── stock.html          # Stock detail + chart
│   ├── watchlist.html      # Watchlist
│   ├── market.html         # Market overview
│   ├── search.html         # Stock search
│   ├── login.html
│   └── register.html
└── static/
    ├── css/main.css        # Dark mode + RGB theme
    └── js/main.js          # Shared utilities, modal helpers, chart defaults
```

## Data Source

Market data is fetched from **Yahoo Finance** via [yfinance](https://github.com/ranaroussi/yfinance).
Results are cached in memory (30–600 seconds depending on data type) to minimize API calls.

> **Disclaimer:** This app is for informational and educational purposes only.
> It is not financial advice. Always do your own research before investing.
