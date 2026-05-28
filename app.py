import os
import re
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps

import pandas as pd
import yfinance as yf
from flask import (Flask, abort, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import (LoginManager, UserMixin, current_user,
                         login_required, login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

# ── App ────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
_secret_key = os.environ.get('SECRET_KEY', '')
_debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
if not _secret_key:
    if _debug_mode:
        _secret_key = 'dev-secret-do-not-use-in-production'
    else:
        raise RuntimeError('SECRET_KEY environment variable must be set in production. '
                           'Set FLASK_DEBUG=1 to run in development mode.')
app.config['SECRET_KEY'] = _secret_key
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///stocktrack.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please sign in to continue.'
login_manager.login_message_category = 'warning'

# ── In-memory cache ───────────────────────────────────────────────────────────
_cache: dict = {}
_lock = threading.Lock()
CACHE_MAX_ITEMS = 1500
CACHE_TRIM_TO = 1200
_rate_limit_bucket: dict[str, deque] = defaultdict(deque)

TTL_QUOTE_FAST = 45
TTL_QUOTE_MEDIUM = 180
TTL_QUOTE_SLOW = 600

TICKER_PATTERN = re.compile(r'^[A-Z0-9.\-^]{1,12}$')
MINIMUM_SHARE_THRESHOLD = 1e-8


def _utc_now():
    return datetime.now(timezone.utc)


def cache_get(key: str, ttl: int = 60):
    with _lock:
        entry = _cache.get(key)
        if entry:
            age = (_utc_now() - entry['ts']).total_seconds()
            if age < ttl:
                return entry['data']
            _cache.pop(key, None)
    return None


def cache_set(key: str, data):
    with _lock:
        _cache[key] = {'data': data, 'ts': _utc_now()}
        if len(_cache) > CACHE_MAX_ITEMS:
            oldest_keys = sorted(_cache.keys(), key=lambda k: _cache[k]['ts'])[:max(0, len(_cache) - CACHE_TRIM_TO)]
            for old_key in oldest_keys:
                _cache.pop(old_key, None)


def _normalize_ticker(ticker: str) -> str | None:
    ticker = (ticker or '').upper().strip()
    if not ticker or not TICKER_PATTERN.match(ticker):
        return None
    return ticker


def _rate_limit_key(scope: str) -> str:
    uid = current_user.get_id() if current_user.is_authenticated else 'anon'
    ip = request.remote_addr or '0.0.0.0'
    return f'{scope}:{uid}:{ip}'


def rate_limit(scope: str, limit: int = 60, window_seconds: int = 60):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            now = _utc_now().timestamp()
            key = _rate_limit_key(scope)
            with _lock:
                bucket = _rate_limit_bucket[key]
                while bucket and now - bucket[0] > window_seconds:
                    bucket.popleft()
                if len(bucket) >= limit:
                    retry_after = max(1, int(window_seconds - (now - bucket[0])))
                    return jsonify({
                        'error': 'Rate limit exceeded',
                        'retry_after_seconds': retry_after,
                    }), 429
                bucket.append(now)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


# ── Models ─────────────────────────────────────────────────────────────────────
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    positions = db.relationship('Position', backref='user', lazy=True,
                                cascade='all, delete-orphan')
    watchlist = db.relationship('WatchlistItem', backref='user', lazy=True,
                                cascade='all, delete-orphan')

    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)


class Position(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    ticker = db.Column(db.String(10), nullable=False)
    shares = db.Column(db.Float, nullable=False)
    avg_cost = db.Column(db.Float, nullable=False)
    added_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    notes = db.Column(db.String(500))
    __table_args__ = (db.Index('ix_position_user_ticker', 'user_id', 'ticker'),)


class WatchlistItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    ticker = db.Column(db.String(10), nullable=False)
    added_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        db.UniqueConstraint('user_id', 'ticker'),
        db.Index('ix_watchlist_user_ticker', 'user_id', 'ticker'),
    )


class Trade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    ticker = db.Column(db.String(10), nullable=False)
    side = db.Column(db.String(4), nullable=False)  # BUY / SELL
    shares = db.Column(db.Float, nullable=False)
    price = db.Column(db.Float, nullable=False)
    realized_pnl = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        db.Index('ix_trade_user_created', 'user_id', 'created_at'),
        db.Index('ix_trade_user_ticker', 'user_id', 'ticker'),
    )


class PriceAlert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    ticker = db.Column(db.String(10), nullable=False)
    target_price = db.Column(db.Float, nullable=False)
    direction = db.Column(db.String(5), nullable=False)  # above / below
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    triggered_at = db.Column(db.DateTime)
    last_trigger_price = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        db.Index('ix_alert_user_active', 'user_id', 'is_active'),
        db.Index('ix_alert_user_ticker', 'user_id', 'ticker'),
    )


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


# ── Market constants ───────────────────────────────────────────────────────────
INDEX_TICKERS = {
    '^GSPC': 'S&P 500',
    '^IXIC': 'NASDAQ',
    '^DJI': 'DOW',
    '^RUT': 'Russell 2K',
    '^VIX': 'VIX',
}

SECTOR_ETFS = {
    'XLK': 'Technology',
    'XLF': 'Financials',
    'XLV': 'Healthcare',
    'XLE': 'Energy',
    'XLY': 'Cons. Disc.',
    'XLP': 'Cons. Staples',
    'XLI': 'Industrials',
    'XLB': 'Materials',
    'XLU': 'Utilities',
    'XLRE': 'Real Estate',
    'XLC': 'Comm. Svcs',
}

POPULAR_TICKERS = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'BRK-B',
    'JPM', 'V', 'UNH', 'JNJ', 'WMT', 'PG', 'MA', 'HD', 'DIS',
    'INTC', 'AMD', 'CRM', 'NFLX', 'ADBE', 'ORCL', 'CSCO', 'PYPL',
]


# ── yFinance helpers ───────────────────────────────────────────────────────────
def _safe_float(val, fallback=None):
    try:
        return round(float(val), 4) if val is not None else fallback
    except (TypeError, ValueError):
        return fallback


def get_quote(ticker: str, ttl: int = TTL_QUOTE_FAST) -> dict | None:
    ticker = _normalize_ticker(ticker)
    if not ticker:
        return None
    cached = cache_get(f'q:{ticker}', ttl)
    if cached:
        return cached
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        # Use history for reliable price data
        hist = t.history(period='5d', interval='1d')
        if hist.empty:
            return None
        price = float(hist['Close'].iloc[-1])
        prev_close = float(hist['Close'].iloc[-2]) if len(hist) > 1 else price
        change = price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0

        data = {
            'ticker': ticker,
            'name': info.get('longName') or info.get('shortName') or ticker,
            'price': round(price, 2),
            'prev_close': round(prev_close, 2),
            'change': round(change, 2),
            'change_pct': round(change_pct, 2),
            'volume': info.get('volume') or info.get('regularMarketVolume'),
            'avg_volume': info.get('averageVolume') or info.get('averageDailyVolume10Day'),
            'market_cap': info.get('marketCap'),
            'pe_ratio': _safe_float(info.get('trailingPE') or info.get('forwardPE')),
            'eps': _safe_float(info.get('trailingEps')),
            'beta': _safe_float(info.get('beta')),
            'week_52_high': _safe_float(info.get('fiftyTwoWeekHigh')),
            'week_52_low': _safe_float(info.get('fiftyTwoWeekLow')),
            'day_high': _safe_float(info.get('dayHigh') or float(hist['High'].iloc[-1])),
            'day_low': _safe_float(info.get('dayLow') or float(hist['Low'].iloc[-1])),
            'open_price': _safe_float(info.get('open') or float(hist['Open'].iloc[-1])),
            'dividend_yield': _safe_float(info.get('dividendYield')),
            'forward_pe': _safe_float(info.get('forwardPE')),
            'price_to_book': _safe_float(info.get('priceToBook')),
            'sector': info.get('sector', ''),
            'industry': info.get('industry', ''),
            'description': info.get('longBusinessSummary', ''),
            'website': info.get('website', ''),
            'employees': info.get('fullTimeEmployees'),
            'currency': info.get('currency', 'USD'),
            'exchange': info.get('exchange', ''),
            'analyst_rating': _safe_float(info.get('recommendationMean')),
            'analyst_key': info.get('recommendationKey', ''),
            'target_price': _safe_float(info.get('targetMeanPrice')),
            'revenue': info.get('totalRevenue'),
            'profit_margin': _safe_float(info.get('profitMargins')),
            'roe': _safe_float(info.get('returnOnEquity')),
            'roa': _safe_float(info.get('returnOnAssets')),
            'debt_to_equity': _safe_float(info.get('debtToEquity')),
            'current_ratio': _safe_float(info.get('currentRatio')),
            'free_cash_flow': info.get('freeCashflow'),
            'gross_margins': _safe_float(info.get('grossMargins')),
            'operating_margins': _safe_float(info.get('operatingMargins')),
            'revenue_growth': _safe_float(info.get('revenueGrowth')),
            'earnings_growth': _safe_float(info.get('earningsGrowth')),
            'shares_outstanding': info.get('sharesOutstanding'),
            'float_shares': info.get('floatShares'),
            'short_ratio': _safe_float(info.get('shortRatio')),
            'peg_ratio': _safe_float(info.get('pegRatio')),
            'country': info.get('country', ''),
            'city': info.get('city', ''),
        }
        cache_set(f'q:{ticker}', data)
        return data
    except Exception as exc:
        print(f'[get_quote] {ticker}: {exc}')
        return None


def get_quotes_map(tickers: list[str], ttl: int = TTL_QUOTE_FAST) -> dict[str, dict]:
    result: dict[str, dict] = {}
    seen = set()
    normalized = []
    for ticker in tickers:
        t = _normalize_ticker(ticker)
        if t and t not in seen:
            seen.add(t)
            normalized.append(t)

    for t in normalized:
        cached = cache_get(f'q:{t}', ttl)
        if cached:
            result[t] = cached
    missing = [t for t in normalized if t not in result]
    for t in missing:
        q = get_quote(t, ttl=ttl)
        if q:
            result[t] = q
    return result


def get_chart_data(ticker: str, period: str = '1mo') -> dict | None:
    period_to_interval = {
        '1d': '5m', '5d': '15m', '1mo': '1d', '3mo': '1d',
        '6mo': '1d', '1y': '1wk', '2y': '1wk', '5y': '1mo', 'max': '1mo',
    }
    interval = period_to_interval.get(period, '1d')
    ticker = _normalize_ticker(ticker)
    if not ticker:
        return None
    key = f'chart:{ticker}:{period}'
    cached = cache_get(key, TTL_QUOTE_MEDIUM)
    if cached:
        return cached
    try:
        hist = yf.Ticker(ticker).history(period=period, interval=interval)
        if hist.empty:
            return None
        hist.index = pd.to_datetime(hist.index)
        data = {
            'labels': [str(d)[:16] for d in hist.index],
            'open':   [round(float(v), 2) for v in hist['Open']],
            'high':   [round(float(v), 2) for v in hist['High']],
            'low':    [round(float(v), 2) for v in hist['Low']],
            'close':  [round(float(v), 2) for v in hist['Close']],
            'volume': [int(v) for v in hist['Volume']],
        }
        cache_set(key, data)
        return data
    except Exception as exc:
        print(f'[get_chart_data] {ticker}: {exc}')
        return None


def get_news(ticker: str) -> list:
    ticker = _normalize_ticker(ticker)
    if not ticker:
        return []
    cached = cache_get(f'news:{ticker}', TTL_QUOTE_SLOW)
    if cached is not None:
        return cached
    try:
        raw = yf.Ticker(ticker).news or []
        result = []
        for item in raw[:12]:
            content = item.get('content', {})
            if isinstance(content, dict):
                title = content.get('title', '')
                url_obj = content.get('canonicalUrl', {})
                link = url_obj.get('url', '') if isinstance(url_obj, dict) else ''
                pub = (content.get('provider', {}) or {}).get('displayName', '')
                pub_time = content.get('pubDate', '')
            else:
                title = item.get('title', '')
                link = item.get('link', '')
                pub = item.get('publisher', '')
                pub_time = str(item.get('providerPublishTime', ''))
            if title:
                result.append({'title': title, 'url': link,
                                'publisher': pub, 'published': pub_time})
        cache_set(f'news:{ticker}', result)
        return result
    except Exception as exc:
        print(f'[get_news] {ticker}: {exc}')
        return []


def get_indices() -> list:
    cached = cache_get('indices', TTL_QUOTE_FAST)
    if cached:
        return cached
    result = []
    quotes = get_quotes_map(list(INDEX_TICKERS.keys()), ttl=TTL_QUOTE_FAST)
    for ticker, name in INDEX_TICKERS.items():
        q = quotes.get(ticker)
        if q:
            result.append({'ticker': ticker, 'name': name,
                           'price': q['price'], 'change': q['change'],
                           'change_pct': q['change_pct']})
    cache_set('indices', result)
    return result


def get_sector_performance() -> list:
    cached = cache_get('sectors', TTL_QUOTE_MEDIUM)
    if cached:
        return cached
    result = []
    quotes = get_quotes_map(list(SECTOR_ETFS.keys()), ttl=TTL_QUOTE_MEDIUM)
    for ticker, name in SECTOR_ETFS.items():
        q = quotes.get(ticker)
        if q:
            result.append({'ticker': ticker, 'name': name,
                           'change_pct': q['change_pct'], 'price': q['price'],
                           'change': q['change']})
    result.sort(key=lambda x: x['change_pct'], reverse=True)
    cache_set('sectors', result)
    return result


def get_movers_data() -> dict:
    cached = cache_get('movers', TTL_QUOTE_MEDIUM)
    if cached:
        return cached
    quotes = []
    quote_map = get_quotes_map(POPULAR_TICKERS, ttl=TTL_QUOTE_FAST)
    for ticker in POPULAR_TICKERS:
        q = quote_map.get(ticker)
        if q:
            quotes.append({'ticker': q['ticker'], 'name': q['name'],
                           'price': q['price'], 'change': q['change'],
                           'change_pct': q['change_pct'],
                           'volume': q.get('volume', 0)})
    gainers = sorted(quotes, key=lambda x: x['change_pct'], reverse=True)[:6]
    losers = sorted(quotes, key=lambda x: x['change_pct'])[:6]
    data = {'gainers': gainers, 'losers': losers}
    cache_set('movers', data)
    return data


def evaluate_price_alerts(user_id: int, quote_map: dict[str, dict] | None = None) -> list[dict]:
    alerts = PriceAlert.query.filter_by(user_id=user_id, is_active=True).all()
    if not alerts:
        return []
    tickers = [a.ticker for a in alerts]
    local_quote_map = quote_map or {}
    missing_tickers = [t for t in tickers if t not in local_quote_map]
    if missing_tickers:
        local_quote_map.update(get_quotes_map(missing_tickers, ttl=TTL_QUOTE_FAST))
    triggered = []
    changed = False
    for alert in alerts:
        q = local_quote_map.get(alert.ticker)
        if not q:
            continue
        price = q['price']
        hit = (alert.direction == 'above' and price >= alert.target_price) or (
            alert.direction == 'below' and price <= alert.target_price
        )
        if hit:
            alert.is_active = False
            alert.triggered_at = _utc_now()
            alert.last_trigger_price = price
            changed = True
            triggered.append({
                'id': alert.id,
                'ticker': alert.ticker,
                'target_price': round(alert.target_price, 2),
                'direction': alert.direction,
                'trigger_price': round(price, 2),
                'triggered_at': alert.triggered_at.strftime('%Y-%m-%d %H:%M:%S'),
            })
    if changed:
        db.session.commit()
    return triggered


def fmt_large(n) -> str:
    if n is None:
        return 'N/A'
    try:
        n = float(n)
    except (TypeError, ValueError):
        return 'N/A'
    if abs(n) >= 1e12:
        return f'${n/1e12:.2f}T'
    if abs(n) >= 1e9:
        return f'${n/1e9:.2f}B'
    if abs(n) >= 1e6:
        return f'${n/1e6:.2f}M'
    return f'${n:,.2f}'


app.jinja_env.globals['fmt_large'] = fmt_large


# ── Auth routes ────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('landing.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))
        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier)
        ).first()
        if user and user.check_password(password):
            login_user(user, remember=remember)
            flash(f'Welcome back, {user.username}!', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid username or password.', 'error')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        errors = []
        if len(username) < 3:
            errors.append('Username must be at least 3 characters.')
        if '@' not in email:
            errors.append('Enter a valid email address.')
        if len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        if password != confirm:
            errors.append('Passwords do not match.')
        if not errors:
            if User.query.filter_by(username=username).first():
                errors.append('Username already taken.')
            if User.query.filter_by(email=email).first():
                errors.append('Email already registered.')
        if errors:
            for msg in errors:
                flash(msg, 'error')
        else:
            user = User(username=username, email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash('Account created! Welcome to StockTrack.', 'success')
            return redirect(url_for('dashboard'))
    return render_template('register.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been signed out.', 'info')
    return redirect(url_for('login'))


# ── Page routes ────────────────────────────────────────────────────────────────
@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')


@app.route('/portfolio')
@login_required
def portfolio():
    return render_template('portfolio.html')


@app.route('/portfolio/add', methods=['POST'])
@login_required
def add_position():
    ticker = _normalize_ticker(request.form.get('ticker', ''))
    try:
        shares = float(request.form.get('shares', 0))
        avg_cost = float(request.form.get('avg_cost', 0))
        if shares <= 0 or avg_cost <= 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('Enter valid positive values for shares and cost.', 'error')
        return redirect(url_for('portfolio'))
    if not ticker:
        flash('Enter a valid ticker symbol.', 'error')
        return redirect(url_for('portfolio'))
    q = get_quote(ticker)
    if not q:
        flash(f'Could not find ticker "{ticker}". Check the symbol and try again.', 'error')
        return redirect(url_for('portfolio'))
    notes = request.form.get('notes', '').strip()[:500]
    existing = Position.query.filter_by(user_id=current_user.id, ticker=ticker).first()
    if existing:
        total_shares = existing.shares + shares
        total_cost_basis = (existing.shares * existing.avg_cost) + (shares * avg_cost)
        existing.shares = total_shares
        existing.avg_cost = total_cost_basis / total_shares
        if notes:
            existing.notes = notes
        flash(f'Updated position in {ticker} — averaged to ${existing.avg_cost:.2f}/share.', 'success')
    else:
        db.session.add(Position(user_id=current_user.id, ticker=ticker,
                                shares=shares, avg_cost=avg_cost, notes=notes))
        flash(f'Added {shares:g} shares of {ticker} at ${avg_cost:.2f}.', 'success')
    db.session.add(Trade(user_id=current_user.id, ticker=ticker, side='BUY', shares=shares, price=avg_cost))
    db.session.commit()
    return redirect(url_for('portfolio'))


@app.route('/portfolio/remove/<int:pos_id>', methods=['POST'])
@login_required
def remove_position(pos_id):
    pos = Position.query.filter_by(id=pos_id, user_id=current_user.id).first_or_404()
    ticker = pos.ticker
    db.session.delete(pos)
    db.session.commit()
    flash(f'Removed {ticker} from portfolio.', 'info')
    return redirect(url_for('portfolio'))


@app.route('/portfolio/sell/<int:pos_id>', methods=['POST'])
@login_required
def sell_position(pos_id):
    pos = Position.query.filter_by(id=pos_id, user_id=current_user.id).first_or_404()
    try:
        shares = float(request.form.get('shares', 0))
        sell_price = float(request.form.get('price', 0))
        if shares <= 0 or sell_price <= 0 or shares > pos.shares:
            raise ValueError
    except (TypeError, ValueError):
        flash('Enter a valid share amount and sale price.', 'error')
        return redirect(url_for('portfolio'))

    realized = (sell_price - pos.avg_cost) * shares
    pos.shares -= shares
    if pos.shares <= MINIMUM_SHARE_THRESHOLD:
        db.session.delete(pos)
    db.session.add(Trade(
        user_id=current_user.id,
        ticker=pos.ticker,
        side='SELL',
        shares=shares,
        price=sell_price,
        realized_pnl=realized,
    ))
    db.session.commit()
    flash(f'Recorded sale of {shares:g} shares of {pos.ticker} at ${sell_price:.2f}.', 'success')
    return redirect(url_for('portfolio'))


@app.route('/watchlist')
@login_required
def watchlist():
    return render_template('watchlist.html')


@app.route('/watchlist/add', methods=['POST'])
@login_required
def add_watchlist():
    ticker = _normalize_ticker(request.form.get('ticker', ''))
    if not ticker:
        flash('Enter a valid ticker symbol.', 'error')
        return redirect(url_for('watchlist'))
    q = get_quote(ticker)
    if not q:
        flash(f'Could not find ticker "{ticker}".', 'error')
        return redirect(url_for('watchlist'))
    if WatchlistItem.query.filter_by(user_id=current_user.id, ticker=ticker).first():
        flash(f'{ticker} is already on your watchlist.', 'info')
    else:
        db.session.add(WatchlistItem(user_id=current_user.id, ticker=ticker))
        db.session.commit()
        flash(f'Added {ticker} ({q["name"]}) to watchlist.', 'success')
    return redirect(url_for('watchlist'))


@app.route('/watchlist/remove-by-ticker/<ticker>', methods=['POST'])
@login_required
def remove_watchlist_by_ticker(ticker):
    ticker = ticker.upper()
    item = WatchlistItem.query.filter_by(user_id=current_user.id, ticker=ticker).first()
    if item:
        db.session.delete(item)
        db.session.commit()
        flash(f'Removed {ticker} from watchlist.', 'info')
    return redirect(url_for('watchlist'))


@app.route('/watchlist/remove/<int:item_id>', methods=['POST'])
@login_required
def remove_watchlist(item_id):
    item = WatchlistItem.query.filter_by(id=item_id,
                                         user_id=current_user.id).first_or_404()
    ticker = item.ticker
    db.session.delete(item)
    db.session.commit()
    flash(f'Removed {ticker} from watchlist.', 'info')
    return redirect(url_for('watchlist'))


@app.route('/alerts/add', methods=['POST'])
@login_required
def add_alert():
    ticker = _normalize_ticker(request.form.get('ticker', ''))
    direction = (request.form.get('direction', 'above') or '').lower()
    try:
        target_price = float(request.form.get('target_price', 0))
        if target_price <= 0:
            raise ValueError
    except (TypeError, ValueError):
        flash('Enter a valid target price for alert.', 'error')
        return redirect(url_for('watchlist'))
    if direction not in ('above', 'below'):
        flash('Alert direction must be above or below.', 'error')
        return redirect(url_for('watchlist'))
    if not ticker:
        flash('Enter a valid ticker symbol.', 'error')
        return redirect(url_for('watchlist'))

    if not get_quote(ticker):
        flash(f'Could not create alert for "{ticker}".', 'error')
        return redirect(url_for('watchlist'))

    duplicate = PriceAlert.query.filter_by(
        user_id=current_user.id, ticker=ticker, target_price=target_price,
        direction=direction, is_active=True
    ).first()
    if duplicate:
        flash('An identical active alert already exists.', 'info')
        return redirect(url_for('watchlist'))

    db.session.add(PriceAlert(
        user_id=current_user.id,
        ticker=ticker,
        target_price=target_price,
        direction=direction,
        is_active=True,
    ))
    db.session.commit()
    flash(f'Alert set: {ticker} {direction} ${target_price:.2f}.', 'success')
    return redirect(url_for('watchlist'))


@app.route('/alerts/remove/<int:alert_id>', methods=['POST'])
@login_required
def remove_alert(alert_id):
    alert = PriceAlert.query.filter_by(id=alert_id, user_id=current_user.id).first_or_404()
    ticker = alert.ticker
    db.session.delete(alert)
    db.session.commit()
    flash(f'Removed alert for {ticker}.', 'info')
    return redirect(url_for('watchlist'))


@app.route('/stock/<ticker>')
@login_required
def stock_detail(ticker):
    ticker = ticker.upper()
    q = get_quote(ticker)
    if not q:
        flash(f'Unable to load data for "{ticker}". Verify the symbol is correct.', 'error')
        return redirect(url_for('dashboard'))
    in_watchlist = WatchlistItem.query.filter_by(
        user_id=current_user.id, ticker=ticker).first() is not None
    position = Position.query.filter_by(
        user_id=current_user.id, ticker=ticker).first()
    return render_template('stock.html', quote=q,
                           in_watchlist=in_watchlist, position=position)


@app.route('/market')
@login_required
def market():
    return render_template('market.html')


@app.route('/search')
@login_required
def search():
    return render_template('search.html', query=request.args.get('q', ''))


# ── API routes ─────────────────────────────────────────────────────────────────
@app.route('/api/quote/<ticker>')
@login_required
@rate_limit('api_quote', limit=120, window_seconds=60)
def api_quote(ticker):
    safe_ticker = _normalize_ticker(ticker)
    if not safe_ticker:
        return jsonify({'error': 'Invalid ticker symbol'}), 400
    data = get_quote(safe_ticker, ttl=TTL_QUOTE_FAST)
    if not data:
        return jsonify({'error': 'Ticker not found'}), 404
    return jsonify(data)


@app.route('/api/chart/<ticker>')
@login_required
@rate_limit('api_chart', limit=90, window_seconds=60)
def api_chart(ticker):
    period = request.args.get('period', '1mo')
    if period not in ('1d', '5d', '1mo', '3mo', '6mo', '1y', '2y', '5y', 'max'):
        period = '1mo'
    safe_ticker = _normalize_ticker(ticker)
    if not safe_ticker:
        return jsonify({'error': 'Invalid ticker symbol'}), 400
    data = get_chart_data(safe_ticker, period)
    if not data:
        return jsonify({'error': 'No chart data available'}), 404
    return jsonify(data)


@app.route('/api/news/<ticker>')
@login_required
def api_news(ticker):
    safe_ticker = _normalize_ticker(ticker)
    if not safe_ticker:
        return jsonify({'error': 'Invalid ticker symbol'}), 400
    return jsonify(get_news(safe_ticker))


@app.route('/api/portfolio/data')
@login_required
@rate_limit('api_portfolio', limit=60, window_seconds=60)
def api_portfolio_data():
    positions = Position.query.filter_by(user_id=current_user.id).all()
    quote_map = get_quotes_map([pos.ticker for pos in positions], ttl=TTL_QUOTE_FAST)
    rows = []
    total_value = total_cost = 0.0
    for pos in positions:
        q = quote_map.get(pos.ticker)
        if not q:
            continue
        mkt_val = pos.shares * q['price']
        cost_basis = pos.shares * pos.avg_cost
        gain = mkt_val - cost_basis
        gain_pct = (gain / cost_basis * 100) if cost_basis else 0
        day_gain = pos.shares * q['change']
        total_value += mkt_val
        total_cost += cost_basis
        rows.append({
            'id': pos.id,
            'ticker': pos.ticker,
            'name': q.get('name', pos.ticker),
            'shares': pos.shares,
            'avg_cost': round(pos.avg_cost, 2),
            'current_price': q['price'],
            'market_value': round(mkt_val, 2),
            'cost_basis': round(cost_basis, 2),
            'gain': round(gain, 2),
            'gain_pct': round(gain_pct, 2),
            'day_change': q['change'],
            'day_change_pct': q['change_pct'],
            'day_gain': round(day_gain, 2),
            'sector': q.get('sector', ''),
            'notes': pos.notes or '',
            'added_at': pos.added_at.strftime('%Y-%m-%d'),
        })
    rows.sort(key=lambda r: r['market_value'], reverse=True)
    for row in rows:
        row['contribution_pct'] = round((row['market_value'] / total_value * 100), 2) if total_value else 0
    total_gain = total_value - total_cost
    total_gain_pct = (total_gain / total_cost * 100) if total_cost else 0
    for row in rows:
        row['gain_contribution_pct'] = round((row['gain'] / total_gain * 100), 2) if total_gain else 0
    realized_gain = db.session.query(db.func.coalesce(db.func.sum(Trade.realized_pnl), 0.0)).filter_by(
        user_id=current_user.id, side='SELL'
    ).scalar() or 0.0
    day_gain = sum(r['day_gain'] for r in rows)
    prev_value = total_value - day_gain
    day_pct = (day_gain / prev_value * 100) if prev_value else 0
    return jsonify({
        'positions': rows,
        'total_value': round(total_value, 2),
        'total_cost': round(total_cost, 2),
        'total_gain': round(total_gain, 2),
        'total_gain_pct': round(total_gain_pct, 2),
        'analytics': {
            'unrealized_gain': round(total_gain, 2),
            'realized_gain': round(realized_gain, 2),
            'total_return': round(total_gain + realized_gain, 2),
            'day_gain': round(day_gain, 2),
            'day_gain_pct': round(day_pct, 2),
        },
    })


@app.route('/api/watchlist/data')
@login_required
@rate_limit('api_watchlist', limit=60, window_seconds=60)
def api_watchlist_data():
    items = WatchlistItem.query.filter_by(user_id=current_user.id).all()
    alerts = PriceAlert.query.filter_by(user_id=current_user.id, is_active=True).all()
    quote_map = get_quotes_map([item.ticker for item in items] + [a.ticker for a in alerts], ttl=TTL_QUOTE_FAST)
    triggered_alerts = evaluate_price_alerts(current_user.id, quote_map=quote_map)
    refreshed_alerts = PriceAlert.query.filter_by(user_id=current_user.id, is_active=True).all()
    alert_by_ticker: dict[str, list[dict]] = defaultdict(list)
    for a in refreshed_alerts:
        alert_by_ticker[a.ticker].append({
            'id': a.id,
            'target_price': round(a.target_price, 2),
            'direction': a.direction,
        })
    result = []
    for item in items:
        q = quote_map.get(item.ticker)
        if q:
            result.append({
                'id': item.id,
                'ticker': item.ticker,
                'name': q.get('name', item.ticker),
                'price': q['price'],
                'change': q['change'],
                'change_pct': q['change_pct'],
                'volume': q.get('volume'),
                'market_cap': q.get('market_cap'),
                'week_52_high': q.get('week_52_high'),
                'week_52_low': q.get('week_52_low'),
                'pe_ratio': q.get('pe_ratio'),
                'sector': q.get('sector', ''),
                'added_at': item.added_at.strftime('%Y-%m-%d'),
                'alerts': alert_by_ticker.get(item.ticker, []),
            })
    return jsonify({
        'items': result,
        'triggered_alerts': triggered_alerts,
    })


@app.route('/api/market/indices')
@login_required
def api_indices():
    return jsonify(get_indices())


@app.route('/api/market/sectors')
@login_required
def api_sectors():
    return jsonify(get_sector_performance())


@app.route('/api/market/movers')
@login_required
@rate_limit('api_movers', limit=60, window_seconds=60)
def api_movers():
    return jsonify(get_movers_data())


@app.route('/api/dashboard/summary')
@login_required
def api_dashboard_summary():
    indices = get_indices()
    positions = Position.query.filter_by(user_id=current_user.id).all()
    quote_map = get_quotes_map([pos.ticker for pos in positions], ttl=TTL_QUOTE_FAST)
    portfolio_value = portfolio_day_gain = 0.0
    for pos in positions:
        q = quote_map.get(pos.ticker)
        if q:
            portfolio_value += pos.shares * q['price']
            portfolio_day_gain += pos.shares * q['change']
    prev_val = portfolio_value - portfolio_day_gain
    day_pct = (portfolio_day_gain / prev_val * 100) if prev_val else 0
    return jsonify({
        'indices': indices,
        'portfolio_value': round(portfolio_value, 2),
        'portfolio_day_gain': round(portfolio_day_gain, 2),
        'portfolio_day_gain_pct': round(day_pct, 2),
        'position_count': len(positions),
        'watchlist_count': WatchlistItem.query.filter_by(
            user_id=current_user.id).count(),
    })


@app.route('/api/dashboard/data')
@login_required
@rate_limit('api_dashboard_data', limit=60, window_seconds=60)
def api_dashboard_data():
    indices = get_indices()
    sectors = get_sector_performance()
    movers = get_movers_data()

    positions = Position.query.filter_by(user_id=current_user.id).all()
    watchlist_items = WatchlistItem.query.filter_by(user_id=current_user.id).all()
    active_alerts = PriceAlert.query.filter_by(user_id=current_user.id, is_active=True).all()
    all_tickers = [p.ticker for p in positions] + [w.ticker for w in watchlist_items] + [a.ticker for a in active_alerts]
    quote_map = get_quotes_map(all_tickers, ttl=TTL_QUOTE_FAST)

    portfolio_rows = []
    portfolio_value = portfolio_day_gain = total_cost = 0.0
    for pos in positions:
        q = quote_map.get(pos.ticker)
        if not q:
            continue
        mkt_val = pos.shares * q['price']
        cost_basis = pos.shares * pos.avg_cost
        gain = mkt_val - cost_basis
        gain_pct = (gain / cost_basis * 100) if cost_basis else 0
        day_gain = pos.shares * q['change']
        portfolio_value += mkt_val
        portfolio_day_gain += day_gain
        total_cost += cost_basis
        portfolio_rows.append({
            'id': pos.id,
            'ticker': pos.ticker,
            'name': q.get('name', pos.ticker),
            'shares': pos.shares,
            'avg_cost': round(pos.avg_cost, 2),
            'market_value': round(mkt_val, 2),
            'gain_pct': round(gain_pct, 2),
            'day_gain': round(day_gain, 2),
        })
    portfolio_rows.sort(key=lambda r: r['market_value'], reverse=True)

    watchlist_rows = []
    for item in watchlist_items:
        q = quote_map.get(item.ticker)
        if q:
            watchlist_rows.append({
                'id': item.id,
                'ticker': item.ticker,
                'name': q.get('name', item.ticker),
                'price': q['price'],
                'change_pct': q['change_pct'],
            })

    prev_val = portfolio_value - portfolio_day_gain
    day_pct = (portfolio_day_gain / prev_val * 100) if prev_val else 0
    total_gain = portfolio_value - total_cost
    total_gain_pct = (total_gain / total_cost * 100) if total_cost else 0

    alert_summary = []
    for a in active_alerts:
        q = quote_map.get(a.ticker) or get_quote(a.ticker, ttl=TTL_QUOTE_FAST)
        if q:
            alert_summary.append({
                'id': a.id,
                'ticker': a.ticker,
                'target_price': round(a.target_price, 2),
                'direction': a.direction,
                'current_price': q['price'],
            })
    triggered_alerts = evaluate_price_alerts(current_user.id, quote_map=quote_map)

    return jsonify({
        'summary': {
            'indices': indices,
            'portfolio_value': round(portfolio_value, 2),
            'portfolio_day_gain': round(portfolio_day_gain, 2),
            'portfolio_day_gain_pct': round(day_pct, 2),
            'position_count': len(positions),
            'watchlist_count': len(watchlist_items),
            'total_gain': round(total_gain, 2),
            'total_gain_pct': round(total_gain_pct, 2),
            'active_alert_count': len(alert_summary),
        },
        'sectors': sectors,
        'movers': movers,
        'portfolio': {'positions': portfolio_rows},
        'watchlist': watchlist_rows,
        'alerts': {'active': alert_summary[:8], 'triggered': triggered_alerts[:8]},
    })


@app.route('/api/search')
@login_required
@rate_limit('api_search', limit=80, window_seconds=60)
def api_search():
    q_raw = request.args.get('q', '').strip()
    q_upper = q_raw.upper()
    if not q_upper:
        return jsonify([])
    if len(q_raw) > 40:
        return jsonify({'error': 'Query too long'}), 400
    cached = cache_get(f'search:{q_upper}', TTL_QUOTE_MEDIUM)
    if cached is not None:
        return jsonify(cached)
    tickers = set(POPULAR_TICKERS)
    for item in WatchlistItem.query.filter_by(user_id=current_user.id).all():
        tickers.add(item.ticker)
    for item in Position.query.filter_by(user_id=current_user.id).all():
        tickers.add(item.ticker)
    safe_query_ticker = _normalize_ticker(q_upper)
    if safe_query_ticker:
        tickers.add(safe_query_ticker)

    quote_map = get_quotes_map(list(tickers), ttl=TTL_QUOTE_FAST)
    results = []
    q_lower = q_raw.lower()
    for q in quote_map.values():
        ticker = q['ticker']
        name = (q.get('name') or '').strip()
        t_low = ticker.lower()
        n_low = name.lower()
        score = 0

        if t_low == q_lower:
            score += 120
        elif t_low.startswith(q_lower):
            score += 90
        elif q_lower in t_low:
            score += 60

        if n_low.startswith(q_lower):
            score += 80
        elif q_lower in n_low:
            score += 45

        if score <= 0:
            continue
        results.append({
            'ticker': ticker,
            'name': name or ticker,
            'price': q['price'],
            'change_pct': q['change_pct'],
            'sector': q.get('sector', ''),
            'exchange': q.get('exchange', ''),
            'score': score,
        })

    results.sort(key=lambda r: (-r['score'], r['ticker']))
    ranked = [{k: v for k, v in item.items() if k != 'score'} for item in results[:15]]
    cache_set(f'search:{q_upper}', ranked)
    return jsonify(ranked)


@app.route('/api/alerts/data')
@login_required
@rate_limit('api_alerts', limit=60, window_seconds=60)
def api_alerts_data():
    alerts = PriceAlert.query.filter_by(user_id=current_user.id).order_by(
        PriceAlert.is_active.desc(), PriceAlert.created_at.desc()
    ).all()
    quote_map = get_quotes_map([a.ticker for a in alerts], ttl=TTL_QUOTE_FAST)
    triggered = evaluate_price_alerts(current_user.id, quote_map=quote_map)
    rows = []
    for a in alerts:
        q = quote_map.get(a.ticker)
        rows.append({
            'id': a.id,
            'ticker': a.ticker,
            'target_price': round(a.target_price, 2),
            'direction': a.direction,
            'is_active': a.is_active,
            'current_price': q['price'] if q else None,
            'created_at': a.created_at.strftime('%Y-%m-%d'),
            'triggered_at': a.triggered_at.strftime('%Y-%m-%d %H:%M:%S') if a.triggered_at else None,
        })
    return jsonify({'alerts': rows, 'triggered_alerts': triggered})


# ── Init ───────────────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=_debug_mode, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
