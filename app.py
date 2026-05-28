import os
import threading
from datetime import datetime, timedelta

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
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-CHANGE-IN-PROD')
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


def cache_get(key: str, ttl: int = 60):
    with _lock:
        entry = _cache.get(key)
        if entry:
            age = (datetime.utcnow() - entry['ts']).total_seconds()
            if age < ttl:
                return entry['data']
    return None


def cache_set(key: str, data):
    with _lock:
        _cache[key] = {'data': data, 'ts': datetime.utcnow()}


# ── Models ─────────────────────────────────────────────────────────────────────
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
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
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.String(500))


class WatchlistItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    ticker = db.Column(db.String(10), nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'ticker'),)


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


def get_quote(ticker: str) -> dict | None:
    ticker = ticker.upper().strip()
    cached = cache_get(f'q:{ticker}', 45)
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


def get_chart_data(ticker: str, period: str = '1mo') -> dict | None:
    period_to_interval = {
        '1d': '5m', '5d': '15m', '1mo': '1d', '3mo': '1d',
        '6mo': '1d', '1y': '1wk', '2y': '1wk', '5y': '1mo', 'max': '1mo',
    }
    interval = period_to_interval.get(period, '1d')
    key = f'chart:{ticker}:{period}'
    cached = cache_get(key, 300)
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
    cached = cache_get(f'news:{ticker}', 600)
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
    cached = cache_get('indices', 60)
    if cached:
        return cached
    result = []
    for ticker, name in INDEX_TICKERS.items():
        q = get_quote(ticker)
        if q:
            result.append({'ticker': ticker, 'name': name,
                           'price': q['price'], 'change': q['change'],
                           'change_pct': q['change_pct']})
    cache_set('indices', result)
    return result


def get_sector_performance() -> list:
    cached = cache_get('sectors', 300)
    if cached:
        return cached
    result = []
    for ticker, name in SECTOR_ETFS.items():
        q = get_quote(ticker)
        if q:
            result.append({'ticker': ticker, 'name': name,
                           'change_pct': q['change_pct'], 'price': q['price'],
                           'change': q['change']})
    result.sort(key=lambda x: x['change_pct'], reverse=True)
    cache_set('sectors', result)
    return result


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
            return redirect(request.args.get('next') or url_for('dashboard'))
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
    ticker = request.form.get('ticker', '').upper().strip()
    try:
        shares = float(request.form.get('shares', 0))
        avg_cost = float(request.form.get('avg_cost', 0))
        if shares <= 0 or avg_cost <= 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('Enter valid positive values for shares and cost.', 'error')
        return redirect(url_for('portfolio'))
    if not ticker:
        flash('Ticker symbol required.', 'error')
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


@app.route('/watchlist')
@login_required
def watchlist():
    return render_template('watchlist.html')


@app.route('/watchlist/add', methods=['POST'])
@login_required
def add_watchlist():
    ticker = request.form.get('ticker', '').upper().strip()
    if not ticker:
        flash('Ticker symbol required.', 'error')
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
    return redirect(request.referrer or url_for('watchlist'))


@login_required
def remove_watchlist(item_id):
    item = WatchlistItem.query.filter_by(id=item_id,
                                         user_id=current_user.id).first_or_404()
    ticker = item.ticker
    db.session.delete(item)
    db.session.commit()
    flash(f'Removed {ticker} from watchlist.', 'info')
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
def api_quote(ticker):
    data = get_quote(ticker.upper())
    if not data:
        return jsonify({'error': 'Ticker not found'}), 404
    return jsonify(data)


@app.route('/api/chart/<ticker>')
@login_required
def api_chart(ticker):
    period = request.args.get('period', '1mo')
    if period not in ('1d', '5d', '1mo', '3mo', '6mo', '1y', '2y', '5y', 'max'):
        period = '1mo'
    data = get_chart_data(ticker.upper(), period)
    if not data:
        return jsonify({'error': 'No chart data available'}), 404
    return jsonify(data)


@app.route('/api/news/<ticker>')
@login_required
def api_news(ticker):
    return jsonify(get_news(ticker.upper()))


@app.route('/api/portfolio/data')
@login_required
def api_portfolio_data():
    positions = Position.query.filter_by(user_id=current_user.id).all()
    rows = []
    total_value = total_cost = 0.0
    for pos in positions:
        q = get_quote(pos.ticker)
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
    total_gain = total_value - total_cost
    total_gain_pct = (total_gain / total_cost * 100) if total_cost else 0
    return jsonify({
        'positions': rows,
        'total_value': round(total_value, 2),
        'total_cost': round(total_cost, 2),
        'total_gain': round(total_gain, 2),
        'total_gain_pct': round(total_gain_pct, 2),
    })


@app.route('/api/watchlist/data')
@login_required
def api_watchlist_data():
    items = WatchlistItem.query.filter_by(user_id=current_user.id).all()
    result = []
    for item in items:
        q = get_quote(item.ticker)
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
            })
    return jsonify(result)


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
def api_movers():
    cached = cache_get('movers', 180)
    if cached:
        return jsonify(cached)
    quotes = []
    for ticker in POPULAR_TICKERS:
        q = get_quote(ticker)
        if q:
            quotes.append({'ticker': q['ticker'], 'name': q['name'],
                           'price': q['price'], 'change': q['change'],
                           'change_pct': q['change_pct'],
                           'volume': q.get('volume', 0)})
    gainers = sorted(quotes, key=lambda x: x['change_pct'], reverse=True)[:6]
    losers = sorted(quotes, key=lambda x: x['change_pct'])[:6]
    data = {'gainers': gainers, 'losers': losers}
    cache_set('movers', data)
    return jsonify(data)


@app.route('/api/dashboard/summary')
@login_required
def api_dashboard_summary():
    indices = get_indices()
    positions = Position.query.filter_by(user_id=current_user.id).all()
    portfolio_value = portfolio_day_gain = 0.0
    for pos in positions:
        q = cache_get(f'q:{pos.ticker}', 300) or get_quote(pos.ticker)
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


@app.route('/api/search')
@login_required
def api_search():
    q_str = request.args.get('q', '').strip().upper()
    if not q_str:
        return jsonify([])
    cached = cache_get(f'search:{q_str}', 300)
    if cached is not None:
        return jsonify(cached)
    results = []
    # Direct ticker lookup
    q = get_quote(q_str)
    if q:
        results.append({'ticker': q['ticker'], 'name': q['name'],
                        'price': q['price'], 'change_pct': q['change_pct'],
                        'sector': q.get('sector', ''), 'exchange': q.get('exchange', '')})
    # Search cached popular tickers
    for ticker in POPULAR_TICKERS:
        if ticker == q_str:
            continue
        cached_q = cache_get(f'q:{ticker}', 3600)
        if cached_q and (q_str in cached_q['ticker'] or
                         q_str.lower() in cached_q.get('name', '').lower()):
            if not any(r['ticker'] == cached_q['ticker'] for r in results):
                results.append({'ticker': cached_q['ticker'], 'name': cached_q['name'],
                                'price': cached_q['price'],
                                'change_pct': cached_q['change_pct'],
                                'sector': cached_q.get('sector', ''),
                                'exchange': cached_q.get('exchange', '')})
    cache_set(f'search:{q_str}', results[:10])
    return jsonify(results[:10])


# ── Init ───────────────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
