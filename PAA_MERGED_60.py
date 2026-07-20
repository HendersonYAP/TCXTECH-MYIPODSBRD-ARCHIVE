import pandas as pd
import yfinance as yf
import numpy as np
import os
import random
import threading
import time as time_module
from datetime import datetime, time
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import matplotlib
matplotlib.use('TkAgg')
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

# =============================================================================
# LOCAL DISK CACHE — all expensive network calls persist to disk so subsequent
# launches are instant even without an internet connection.
#
# Cache layout (all in paa_cache/ next to the script):
#   prices/<TICKER>.parquet  — adjusted close history     (TTL: 1 day)
#   divs/<TICKER>.parquet    — dividend series             (TTL: 7 days)
#   live/prices.json         — live prices per ticker      (TTL: 5 min)
#   live/fx.json             — USDMYR rate                 (TTL: 5 min)
#   live/eps.json            — trailing EPS per ticker     (TTL: 24 hr)
#   live/indices.json        — Bursa market indices        (TTL: 5 min)
#   teet_cache.csv           — MYIPO+ Google Sheet         (SHA-256 delta)
#   ledger_<kid>.csv             — per-K-ID ledger sheet/CSV   (SHA-256 delta)
# =============================================================================

_CACHE_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'paa_cache')
_CACHE_PRICES_DIR = os.path.join(_CACHE_DIR, 'prices')
_CACHE_DIVS_DIR   = os.path.join(_CACHE_DIR, 'divs')
_CACHE_LIVE_DIR   = os.path.join(_CACHE_DIR, 'live')

_TTL_PRICE  = 86_400        # 1 day
_TTL_DIV    = 7 * 86_400    # 7 days
_TTL_LIVE   = 300           # 5 min
_TTL_EPS    = 86_400        # 24 hr

for _d in (_CACHE_DIR, _CACHE_PRICES_DIR, _CACHE_DIVS_DIR, _CACHE_LIVE_DIR):
    os.makedirs(_d, exist_ok=True)


def _cache_fresh(path: str, ttl: int) -> bool:
    try:    return (time_module.time() - os.path.getmtime(path)) < ttl
    except: return False

def _cache_rparquet(path: str):
    try:    return pd.read_parquet(path)
    except: return None

def _cache_wparquet(path: str, df):
    try:    df.to_parquet(path)
    except: pass

def _cache_rjson(path: str) -> dict:
    import json as _j
    try:
        with open(path, 'r', encoding='utf-8') as f: return _j.load(f)
    except: return {}

def _cache_wjson(path: str, data: dict):
    import json as _j
    try:
        with open(path, 'w', encoding='utf-8') as f: _j.dump(data, f)
    except: pass

def _pticker(ticker: str) -> str:
    return os.path.join(_CACHE_PRICES_DIR,
                        ticker.replace('^','_').replace('/','_') + '.parquet')

def _dticker(ticker: str) -> str:
    return os.path.join(_CACHE_DIVS_DIR,
                        ticker.replace('^','_').replace('/','_') + '.parquet')

def _ljson(key: str) -> str:
    # Symbols can carry characters that are awkward in filenames (^, =, /),
    # e.g. 'USDMYR=X'. Normalise so every cache key maps to a safe path.
    safe = ''.join(ch if (ch.isalnum() or ch in '_-.') else '_' for ch in key)
    return os.path.join(_CACHE_LIVE_DIR, f'{safe}.json')


def cache_get_prices(ticker: str, start_str: str = None) -> 'pd.Series | None':
    """Return cached daily Close series, or None if stale/missing."""
    p = _pticker(ticker)
    if not _cache_fresh(p, _TTL_PRICE): return None
    df = _cache_rparquet(p)
    if df is None or df.empty: return None
    s = df['Close'] if 'Close' in df.columns else df.iloc[:, 0]
    if start_str:
        try: s = s[s.index >= pd.Timestamp(start_str)]
        except: pass
    return s if not s.empty else None

def cache_set_prices(ticker: str, series: 'pd.Series'):
    if series is None or series.empty: return
    _cache_wparquet(_pticker(ticker), series.rename('Close').to_frame())

def cache_get_divs(ticker: str) -> 'pd.Series | None':
    p = _dticker(ticker)
    if not _cache_fresh(p, _TTL_DIV): return None
    df = _cache_rparquet(p)
    if df is None or df.empty: return None
    col = 'Dividends' if 'Dividends' in df.columns else df.columns[0]
    s = df[col]
    return s if not s.empty else None

def cache_set_divs(ticker: str, series: 'pd.Series'):
    if series is None or series.empty: return
    _cache_wparquet(_dticker(ticker), series.rename('Dividends').to_frame())

def cache_get_live(key: str, ttl: int = _TTL_LIVE):
    p = _ljson(key)
    if not _cache_fresh(p, ttl): return None
    return _cache_rjson(p).get('v')

def cache_set_live(key: str, value):
    _cache_wjson(_ljson(key), {'v': value, 't': time_module.time()})

def cache_get_live_dict(key: str, ttl: int = _TTL_LIVE) -> dict:
    p = _ljson(key)
    if not _cache_fresh(p, ttl): return {}
    return _cache_rjson(p)

def cache_set_live_dict(key: str, data: dict):
    _cache_wjson(_ljson(key), data)

# =============================================================================
# ONLINE DATABASE
# =============================================================================
ONLINE_DB_URL = (
    'https://docs.google.com/spreadsheets/d/e/'
    '2PACX-1vTZQwnk6JF3NazIiGxXumkHio3_8q6NbB1P5KxNyCYBlA0BCszcv12ahR7hb-3hZYqzFN4FxR3GiVsg'
    '/pub?gid=147724768&single=true&output=csv'
)
ONLINE_TIMEOUT   = 10          # seconds before giving up
LOCAL_CACHE_NAME = 'teet_cache.csv'   # saved alongside the local CSV (was TSV, now CSV)

def fetch_online_db(log_cb=None):
    """
    Download the Google Sheets CSV, logging download speed (KB/s or MB/s).
    Returns (DataFrame, source_label) or (None, error_msg).
    """
    import urllib.request
    import io
    import time as _time
    def log(m):
        if log_cb: log_cb(m)
    try:
        log('🌐  Connecting to online database…')
        import ssl as _ssl
        try:
            _ctx = _ssl.create_default_context()
        except Exception:
            _ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
            _ctx.check_hostname = False; _ctx.verify_mode = _ssl.CERT_NONE
        req = urllib.request.Request(
            ONLINE_DB_URL,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        try:
            _open_resp = urllib.request.urlopen(req, timeout=ONLINE_TIMEOUT, context=_ctx)
        except Exception:
            _ctx2 = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
            _ctx2.check_hostname = False; _ctx2.verify_mode = _ssl.CERT_NONE
            _open_resp = urllib.request.urlopen(req, timeout=ONLINE_TIMEOUT, context=_ctx2)
        with _open_resp as resp:
            content_len = resp.headers.get('Content-Length')
            total_bytes = int(content_len) if content_len else None
            t_start   = _time.perf_counter()
            chunks    = []
            recv      = 0
            CHUNK_SZ  = 8192
            while True:
                chunk = resp.read(CHUNK_SZ)
                if not chunk:
                    break
                chunks.append(chunk)
                recv += len(chunk)
                elapsed = _time.perf_counter() - t_start or 0.001
                speed   = recv / elapsed          # bytes/s
                if speed >= 1_048_576:
                    speed_str = f'{speed/1_048_576:.1f} MB/s'
                elif speed >= 1024:
                    speed_str = f'{speed/1024:.0f} KB/s'
                else:
                    speed_str = f'{speed:.0f} B/s'
                if total_bytes:
                    pct = recv / total_bytes * 100
                    log(f'⬇  Downloading database… {recv/1024:.0f} KB / {total_bytes/1024:.0f} KB  ({pct:.0f}%)  @ {speed_str}')
                else:
                    log(f'⬇  Downloading database… {recv/1024:.0f} KB received  @ {speed_str}')
            raw_bytes = b''.join(chunks)
            elapsed   = _time.perf_counter() - t_start or 0.001
            speed     = len(raw_bytes) / elapsed
            if speed >= 1_048_576:
                final_speed = f'{speed/1_048_576:.1f} MB/s'
            elif speed >= 1024:
                final_speed = f'{speed/1024:.0f} KB/s'
            else:
                final_speed = f'{speed:.0f} B/s'
            log(f'✅  Database fetched: {len(raw_bytes)/1024:.1f} KB in {elapsed:.1f}s @ {final_speed}')
        df = pd.read_csv(
            io.BytesIO(raw_bytes),
            sep=',',
            dtype=str,
            keep_default_na=False,
            low_memory=False,
            encoding_errors='replace',
        )
        return df, 'online'
    except Exception as exc:
        log(f'⚠️  Online database unavailable ({exc}). Falling back to local file…')
        return None, str(exc)


def resolve_input(local_path, prefer_online=True, log_cb=None, cache_online=True):
    """
    Returns (DataFrame_or_path, source_label).
    - If prefer_online=True  → download online first, compare against the
      cached local copy:
        * If online content DIFFERS from the cache (or no cache exists yet) →
          treat online as the update, save it as the new local cache, and use it.
        * If online content is IDENTICAL to the cache → skip re-writing, just
          use the existing local cache file directly.
    - If prefer_online=False → local only, no online check at all.
    """
    import hashlib

    def log(m):
        if log_cb: log_cb(m)

    if prefer_online:
        df_online, src = fetch_online_db(log_cb=log_cb)
        if df_online is not None:
            cache_path = os.path.join(os.path.dirname(local_path), LOCAL_CACHE_NAME)

            # Hash the freshly-downloaded online data
            online_bytes = df_online.to_csv(index=False).encode('utf-8')
            online_hash  = hashlib.sha256(online_bytes).hexdigest()

            # Hash the existing local cache, if any
            local_hash = None
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, 'rb') as f:
                        local_hash = hashlib.sha256(f.read()).hexdigest()
                except Exception as e:
                    log(f'⚠️  Could not read existing cache for comparison: {e}')

            if local_hash == online_hash and local_hash is not None:
                # No update — local cache already matches online, just use it.
                log('✓  Online data unchanged from local cache — using local copy.')
                return cache_path, 'local'

            # Online differs (or no cache yet) — this IS the update, save & use it.
            if cache_online:
                try:
                    with open(cache_path, 'wb') as f:
                        f.write(online_bytes)
                    log(f'💾  Online data updated — cached to {cache_path}')
                except Exception as e:
                    log(f'⚠️  Could not write cache: {e}')
            return df_online, 'online'

    # Fall back to local file
    log(f'📂  Loading local file: {local_path}')
    return local_path, 'local'


# =============================================================================
# CONFIGURATION
# =============================================================================
DEFAULT_FOLDER       = r'C:\Users\User\Documents'
DEFAULT_INPUT_FILE   = os.path.join(DEFAULT_FOLDER, 'teet.csv')
DEFAULT_OUTPUT_CSV   = os.path.join(DEFAULT_FOLDER, 'MYIPO_Index_History.csv')
# (per-K-ID ledger cache path is derived at runtime — see _mf_load_csv)
START_DATE_STR     = (pd.Timestamp.now() - pd.DateOffset(years=7)).strftime('%Y-%m-%d')
MALAYSIA_TZ        = ZoneInfo('Asia/Kuala_Lumpur') if ZoneInfo else None
# NOTE: TODAY is intentionally NOT computed here as a module constant —
# it must be recomputed inside build_indices() on every rebuild so that
# new IPOs listed after the app first launched are correctly included.
# (Freezing it at import time caused post-launch IPOs to stay in "future" forever.)

# =============================================================================
# INDEX DEFINITIONS
# =============================================================================
INDEX_CONFIG = [
    ('MYIPO+',            'Quant1',  None,   False),
    ('MYIPO+ Momentum',   'Quant2',  None,   False),
    ('MYIPO+ Ace',        'Quant1',  'ACE',  False),
    ('MYIPO+ Main',       'Quant1',  'MAIN', False),
    ('MYIPO+ Dynamic365', 'Quant1',  None,   True ),
]

# =============================================================================
# Y-SERIES DEFINITIONS
# =============================================================================
YSERIES_CONFIG = [
    ('MYIPO+ Y2020', 'Quant1', 2020),
    ('MYIPO+ Y2021', 'Quant1', 2021),
    ('MYIPO+ Y2022', 'Quant1', 2022),
    ('MYIPO+ Y2023', 'Quant1', 2023),
    ('MYIPO+ Y2024', 'Quant1', 2024),
    ('MYIPO+ Y2025', 'Quant1', 2025),
    ('MYIPO+ Y2026', 'Quant1', 2026),
]

# =============================================================================
# TR-SERIES DEFINITIONS
# =============================================================================
# Covers both the base INDEX_CONFIG indices AND the Y-Series years, since
# every Quant1 fund needs a matching TR twin to compute its dividend-only
# return gap (TR includes dividends, Core/base does not).
TR_SERIES_CONFIG = (
    [(f'{c[0]} TR', c[1], c[2], c[3]) for c in INDEX_CONFIG] +
    [(f'{c[0]} TR', c[1], None, False) for c in YSERIES_CONFIG]
)

# ── MYIPO+ Fund Series (Mutual-Fund / Unit Trust type) ────────────────────────
# Every Quant1-based index (i.e. everything EXCEPT Momentum, which is Quant2)
# gets its own managed-fund twin: a unit trust that tracks that index's daily
# % change 1:1, launched at par RM0.2500/unit on the date of the first IPO
# listed in 2020, with 1,000 units in issue. Each fund pays its own quarterly
# cash distribution on 31 Mar / 30 Jun / 30 Sep / 31 Dec.
#
# DISTRIBUTION BASIS — dividend pass-through, not arbitrary NAV growth:
#   Each fund's underlying index has a TR (Total Return) twin which includes
#   dividend reinvestment. The gap between TR return and Core/base return
#   since the last payout IS the dividend income the basket actually earned
#   in that period. The fund distributes 100% of that dividend-only gap
#   (applied to NAV) each quarter — it does NOT distribute capital growth,
#   since this is a growth-focused fund. Capital appreciation stays in NAV.
#
# CASH FLOOR — growth-focused, dividend payout never erodes capital:
#   If cash-per-unit (cash / units_out) is below FUND_DIST_CASH_FLOOR_PER_UNIT
#   (RM0.0100) on a distribution date, that quarter's payout is skipped
#   entirely. The fund keeps running normally otherwise.
FUND_PAR_NAV      = 0.2500     # RM per unit at inception (same for all funds)
FUND_UNITS_ISSUED = 1000       # fixed units in issue (same for all funds)
FUND_DIST_MONTHS_DAYS = [(3, 31), (6, 30), (9, 30), (12, 31)]  # (month, day)
FUND_DIST_CASH_FLOOR_PER_UNIT = 0.0100  # RM/unit — skip payout if cash/unit below this

# TRANSACTION COSTS — one-directional, applied uniformly to every fund:
#   Buy-in (taking up a new IPO allocation) is FREE — RM0 cost, since this
#   models direct IPO subscription rather than a market purchase.
#   Sell/exit (e.g. a Dynamic365 stock leaving at D+365, or any other
#   disposal) incurs a FLAT RM2.01 per transaction — broker commission plus
#   regulatory/government tax — deducted from the sale proceeds before they
#   reach the fund's cash bucket. This applies regardless of trade size.
FUND_SELL_FEE_PER_TXN = 2.01    # RM, flat, deducted from every sell/exit proceeds

# One fund per Quant1 index — base index label, board filter & D365-flag are
# inherited so the fund's underlying basket exactly matches its index twin.
# Momentum (Quant2) is excluded — it doesn't get a fund.
FUND_CONFIG = [
    (f'{label} Fund', label, board_filter, use_d365)
    for (label, qty_col, board_filter, use_d365) in INDEX_CONFIG
    if qty_col == 'Quant1'
] + [
    (f'{label} Fund', label, None, False)
    for (label, qty_col, year) in YSERIES_CONFIG
    if qty_col == 'Quant1'
]

# Back-compat alias — the original single Core fund label, kept so any code
# that still references the old singular fund constants keeps working.
FUND_LABEL      = 'MYIPO+ Fund'
FUND_BASE_INDEX = 'MYIPO+'
FUND_LABELS     = [c[0] for c in FUND_CONFIG]   # all fund labels, in order

# =============================================================================
# VFUND / MODEL PORTFOLIO — standalone equal-weighted closed-end funds
# =============================================================================
# These differ from the index-twin funds above in three ways:
#   1. FIXED BASKET   — an explicit constituent list, not derived from an index.
#   2. EQUAL WEIGHT   — capital split 1/N at inception, then left to drift.
#                       (The index-twin funds are Quant1-weighted.)
#   3. CLOSED-END     — units are issued once at launch and never again. There
#                       is no cash-shortfall unit issuance, so NAV moves purely
#                       with the basket. That's what makes them a clean model
#                       portfolio rather than an open-ended vehicle.
# Inception: first trading day of 2026, at par.
VFUND_INCEPTION = '2026-01-01'   # snapped forward to the first trading day

VFUND_CONFIG = [
    {
        'label':     'MYIPO+ XaaS Fund',
        'ccy':       'MYR',
        'par':       0.2500,
        'suffix':    '.KL',
        'members':   ['0265', '0258', '0276', '5301', '0277', '0290',
                      '0358', '0311', '0376', '0465', '0281'],
        'note':      'Equal-weight XaaS basket, Bursa-listed',
    },
    {
        'label':     'MSCI-EW First Batch SC Fund',
        'ccy':       'USD',
        'par':       1.0000,
        'suffix':    '',
        # EWX (EM ex-China) removed: this is a SINGLE COUNTRY basket and
        # EWX is a regional fund, so it never belonged here. 20 members.
        'members':   ['EWA', 'EWC', 'EWD', 'EWG', 'EWH', 'EWI', 'EWJ',
                      'EWK', 'EWL', 'EWM', 'EWN', 'EWO', 'EWP', 'EWQ',
                      'EWS', 'EWT', 'EWU', 'EWW', 'EWY', 'EWZ'],
        'note':      'Equal-weight single-country MSCI fund-of-funds',
    },
    {
        'label':     'MSCI-EW ASEAN Fund',
        'ccy':       'USD',
        'par':       1.0000,
        'suffix':    '',
        'members':   ['EWM', 'EWS', 'EIDO', 'EPHE', 'THD'],
        'note':      'Equal-weight ASEAN single-country MSCI basket',
    },
    {
        'label':     'MSCI-EW Asia Dragon 4 Fund',
        'ccy':       'USD',
        'par':       1.0000,
        'suffix':    '',
        'members':   ['EWT', 'EWS', 'EWH', 'EWY'],
        'note':      'Equal-weight Taiwan / Singapore / Hong Kong / Korea',
    },
]
VFUND_LABELS = [c['label'] for c in VFUND_CONFIG]

# ── Unified fund metadata ────────────────────────────────────────────────
# FUND_LABELS (index-twin unit trusts) and VFUND_LABELS (equal-weight CEFs)
# are built by two different engines above, but from the UI's perspective
# they're the SAME class: NAV-denominated, unit-based, ledger-tracked funds
# (as opposed to base-100 price indices). Every display/dropdown/ledger
# element should key off this instead of hardcoding FUND_LABELS alone and
# the FUND_PAR_NAV/FUND_UNITS_ISSUED globals (which only cover the unit
# trusts) — that mismatch is what made the 4 VFund CEFs render as plain
# 2dp index rows with no units/ledger instead of 4dp fund NAVs.
ALL_FUND_LABELS = FUND_LABELS + VFUND_LABELS   # both classes, ledger-tracked

FUND_META = {}
for _lbl in FUND_LABELS:
    FUND_META[_lbl] = {'par': FUND_PAR_NAV, 'ccy': 'RM', 'units': FUND_UNITS_ISSUED,
                        'note': 'Unit trust, quarterly distribution'}
for _vf in VFUND_CONFIG:
    FUND_META[_vf['label']] = {'par':   _vf['par'],
                                'ccy':   'RM' if _vf['ccy'] == 'MYR' else '$',
                                'units': FUND_UNITS_ISSUED,
                                'note':  _vf['note']}

# =============================================================================
# TOP-10 SERIES DEFINITION
# =============================================================================
TOP10_LABEL      = 'MYIPO+ Top 10'
_TOP10_HISTORY   = []   # legacy alias — Top 10 in/out records
_TOP_SERIES_HISTORY = {}  # {N: [history contract records]} for Top 10/20/50/100
TOP_SERIES_LABELS = ['MYIPO+ Top 10', 'MYIPO+ Top 20', 'MYIPO+ Top 50', 'MYIPO+ Top 100']
# label -> N, derived rather than hand-written so the two can't drift apart
_TOP_LABEL_N = {lbl: int(lbl.rsplit(' ', 1)[1]) for lbl in TOP_SERIES_LABELS}

ALL_INDEX_LABELS = (
    [c[0] for c in INDEX_CONFIG] +
    [c[0] for c in YSERIES_CONFIG] +
    [c[0] for c in TR_SERIES_CONFIG] +
    TOP_SERIES_LABELS +
    FUND_LABELS +
    VFUND_LABELS
)

# ── Line style patterns per index group ──────────────────────────────────────
# Each family gets a distinct dash pattern so overlapping lines stay readable
# even when colours are similar.
#   Core      → solid          ————————
#   Y-Series  → dotted         ··········
#   TR-Series → dash-dot       —·—·—·—·
#   Top       → long dash      ——  ——  ——
#   Fund      → dashed         — — — —  (already on twin axis)
LINE_STYLE_MAP = {}
for _c in INDEX_CONFIG:
    LINE_STYLE_MAP[_c[0]] = '-'
for _c in YSERIES_CONFIG:
    LINE_STYLE_MAP[_c[0]] = ':'
for _c in TR_SERIES_CONFIG:
    LINE_STYLE_MAP[_c[0]] = '-.'
for _lbl in TOP_SERIES_LABELS:
    LINE_STYLE_MAP[_lbl] = (0, (6, 2))     # long dash
for _lbl in FUND_LABELS:
    LINE_STYLE_MAP[_lbl] = '--'
for _lbl in VFUND_LABELS:
    LINE_STYLE_MAP[_lbl] = (0, (4, 1, 1, 1))   # dash-dot-dot: EW CEF

COLORS = [
    '#00C9FF','#FF6B6B','#FFD93D','#6BCB77','#845EC2',
    '#F9A825','#00B4D8','#E76F51','#A8DADC','#457B9D',
    '#2EC4B6','#FF9F1C',
]
TR_COLORS = [
    '#7FECFF','#FFADAD','#FFEEAA','#B5F5C3','#C4A4E8',
]
FUND_COLORS = [
    '#00E5A0','#5EEAD4','#34D399','#A7F3D0','#6EE7B7',
    '#10B981','#2DD4BF','#99F6E4',
]
COLOR_MAP = {label: COLORS[i % len(COLORS)] for i, label in enumerate(ALL_INDEX_LABELS)}
for i, cfg in enumerate(TR_SERIES_CONFIG):
    COLOR_MAP[cfg[0]] = TR_COLORS[i % len(TR_COLORS)]
for i, label in enumerate(FUND_LABELS):
    COLOR_MAP[label] = FUND_COLORS[i % len(FUND_COLORS)]
COLOR_MAP[TOP10_LABEL] = '#FFD700'

# World indices are loaded lazily, so they aren't in ALL_INDEX_LABELS at import.
# Give them fixed colours rather than letting them collide with MYIPO+ series.
WORLD_COLORS = {
    'FBM KLCI':            '#00C9FF',
    'Hang Seng':           '#FF6B6B',
    'Straits Times':       '#F4A261',
    'Nikkei 225':          '#E76F51',
    'FTSE 100':            '#2A9D8F',
    'FTSE 250':            '#8AB17D',
    'KOSPI':               '#4ECDC4',
    'S&P 500':             '#45B7D1',
    'Nasdaq Composite':    '#96CEB4',
    'Dow Jones':           '#FFEAA7',
    'PHLX Semiconductor':  '#DDA0DD',
    'Russell 1000':        '#74B9FF',
    'Russell 2000':        '#A29BFE',
    'Russell 3000':        '#FD79A8',
}

# =============================================================================
# CORE ENGINE
# =============================================================================
def build_indices(input_file, log_cb=None, prefer_online=True, apply_sell_fee=True, drip_distributions=False):
    def log(msg):
        if log_cb: log_cb(msg)

    # Compute TODAY fresh on every build — not a module constant — so that
    # IPOs listed after the app first launched are correctly included when
    # the user refreshes. Using Malaysia time (UTC+8) so the cutoff aligns
    # with Bursa's trading calendar, not the user's local clock.
    _now_my = datetime.now(MALAYSIA_TZ) if MALAYSIA_TZ else datetime.now()
    TODAY   = pd.Timestamp(_now_my.date()).as_unit('s')
    # Also refresh the price-history start date — 7 years back from today
    _start_str = (pd.Timestamp.now() - pd.DateOffset(years=7)).strftime('%Y-%m-%d')

    # ── DATA SOURCE: try online Google Sheets first, fall back to local CSV.
    source_ref, source_label = resolve_input(
        input_file,
        prefer_online=prefer_online,
        log_cb=log_cb,
        cache_online=True,
    )

    if isinstance(source_ref, pd.DataFrame):
        raw_df = source_ref.copy()
        raw_df = raw_df.astype(str).replace({"nan": "", "None": ""})
    else:
        # ── CSV LOAD: read all as strings, then immediately filter to real .KL rows.
        #    The CSV may contain hundreds of thousands of corrupt/blank trailing rows
        #    (Excel export artefact) — filtering early avoids polluting date parsing.
        raw_df = pd.read_csv(
            source_ref,
            dtype=str,
            keep_default_na=False,
            low_memory=False,
            encoding_errors='replace',
        )

    log(f"📊  Source: {source_label.upper()} | {len(raw_df)} raw rows loaded.")

    # Rename first column → Symbol, keep only rows whose Symbol matches NNNN.KL
    raw_df = raw_df.rename(columns={raw_df.columns[0]: 'Symbol'})
    raw_df['Symbol'] = raw_df['Symbol'].str.strip()
    raw_df = raw_df[raw_df['Symbol'].str.match(r'^\d{4}\.KL$', na=False)].copy()

    if 'Name' not in raw_df.columns:
        raw_df['Name'] = raw_df['Symbol'].str.replace('.KL', '', regex=False)

    # Select only needed columns
    needed = ['Symbol', 'Name', 'Board', 'Trade Date', 'D-1', 'D+365',
              'Purchase Price', 'Quant1', 'Quant2']
    df = raw_df[[c for c in needed if c in raw_df.columns]].copy()

    # ── PURCHASE PRICE: strip "RM" prefix
    df['Purchase Price'] = (
        df['Purchase Price']
        .str.replace('RM', '', case=False, regex=False)
        .str.strip()
    )

    # ── DATE PARSING: CSV uses DD/MM/YYYY (Malaysian format) for all historical
    #    dates.  Some future IPO rows may use M/D/YYYY (US format from Excel).
    #    Parse primary format first, then fall back for any remaining NaTs.
    #    Convert to datetime64[s] to match yfinance index resolution exactly,
    #    so searchsorted and label alignment never misfire.
    def _parse_my_dates(series):
        s = series.str.strip()
        # The CSV (both local teet.csv and Google Sheets export) uses M/D/YYYY
        # throughout — first segment is always month (1-12), second is day (1-31).
        # Primary: M/D/YYYY  (e.g. 3/11/2026 = March 11, not November 3)
        parsed = pd.to_datetime(s, format='%m/%d/%Y', errors='coerce')
        # Fallback: DD/MM/YYYY for any rows that didn't parse above
        still_nat = parsed.isna() & s.str.len().gt(0)
        if still_nat.any():
            fallback = pd.to_datetime(s[still_nat], format='%d/%m/%Y', errors='coerce')
            parsed = parsed.copy()
            parsed[still_nat] = fallback
        return parsed.dt.as_unit('s')   # align with yfinance datetime64[s]

    for col in ['Trade Date', 'D-1', 'D+365']:
        if col in df.columns:
            df[col] = _parse_my_dates(df[col])

    df['Purchase Price'] = pd.to_numeric(df['Purchase Price'], errors='coerce')
    df['Quant1']         = pd.to_numeric(df['Quant1'], errors='coerce').fillna(0).astype(int)
    df['Quant2']         = pd.to_numeric(df['Quant2'], errors='coerce').fillna(0).astype(int)
    df['Board']          = df['Board'].str.strip().str.upper()

    # ── D-1 / Trade Date integrity: D-1 must always be before Trade Date.
    #    Two kinds of errors found in the CSV:
    #      (a) gap == 1 day  → fields are genuinely swapped; swap them back.
    #      (b) gap  > 1 day  → D-1 is a wrong date entirely; derive it as
    #                           Trade Date − 1 calendar day instead of swapping.
    bad_d1_mask = df['D-1'] > df['Trade Date']
    if bad_d1_mask.any():
        # (a) exactly 1-day gap → fields are genuinely swapped; swap them back.
        swap_mask = bad_d1_mask & (
            (df['D-1'] - df['Trade Date']).dt.days.fillna(0).abs() == 1
        )
        if swap_mask.any():
            df.loc[swap_mask, ['D-1', 'Trade Date']] = (
                df.loc[swap_mask, ['Trade Date', 'D-1']].values
            )
        # (b) larger gap → derive D-1 as the previous business day before Trade Date.
        #     Using calendar day -1 is wrong when Trade Date falls on a Monday
        #     (D-1 would land on Sunday — no market data, ipo_event_dt returns past time,
        #      silently dropping the IPO from the info bar).
        still_bad = df['D-1'] >= df['Trade Date']
        if still_bad.any():
            df.loc[still_bad, 'D-1'] = (
                df.loc[still_bad, 'Trade Date'] - pd.offsets.BDay(1)
            ).dt.as_unit('s')

    # Derive D-1 for any row where it is NaT (missing from the CSV entirely).
    # Without this, _ipo_event_dt returns None for the include_at check,
    # and if Trade Date is also future the IPO may still show — but if the
    # D-1 15:00/17:00 window is what the info bar keys on, a NaT D-1 means
    # it would never trigger "To D-1 inclusion". Derive it as the previous
    # business day so both countdown stages always work correctly.
    nat_d1_mask = df['D-1'].isna() & df['Trade Date'].notna()
    if nat_d1_mask.any():
        df.loc[nat_d1_mask, 'D-1'] = (
            df.loc[nat_d1_mask, 'Trade Date'] - pd.offsets.BDay(1)
        ).dt.as_unit('s')

    df_listed   = df[df['Trade Date'] <= TODAY].copy()

    # future_ipos: IPOs whose D0 Trade Date is in the future OR is today.
    # Include a 30-day look-ahead so upcoming IPOs in the next few weeks
    # are always captured in the info bar, not just imminent ones.
    # Use D-1 as the primary key — an IPO enters the info bar at D-1 17:00.
    THIRTY_DAYS_AHEAD = TODAY + pd.Timedelta(days=30)
    future_ipos = df[
        (df['Trade Date'] > (TODAY - pd.Timedelta(days=1))) &
        (df['Trade Date'] <= THIRTY_DAYS_AHEAD)
    ].copy().sort_values(['D-1', 'Trade Date'])
    log(f"📅  Future IPOs in 30-day window: {len(future_ipos)} rows.")
    tickers_listed = df_listed['Symbol'].unique().tolist()

    # ── VFund constituents ────────────────────────────────────────────────
    # These are hand-picked baskets, not IPO-sheet rows, so they'd never be
    # fetched otherwise. Merge them into the same download + cache path
    # rather than bolting on a second fetcher.
    _vf_syms = []
    for _vf in VFUND_CONFIG:
        for _m in _vf['members']:
            _s = f"{_m}{_vf['suffix']}"
            if _s not in tickers_listed and _s not in _vf_syms:
                _vf_syms.append(_s)
    if _vf_syms:
        tickers_listed = tickers_listed + _vf_syms
        log(f"➕  VFund constituents added to price fetch: {len(_vf_syms)} extra tickers.")

    log(f"⬇  Downloading price history for {len(tickers_listed)} tickers...")

    # ── Check disk cache first — only download tickers whose cache is stale ──
    cached_parts  = {}   # sym → Series (from disk)
    stale_tickers = []
    for sym in tickers_listed:
        cached = cache_get_prices(sym, _start_str)
        if cached is not None:
            cached_parts[sym] = cached
        else:
            stale_tickers.append(sym)

    n_cached = len(cached_parts)
    n_stale  = len(stale_tickers)
    log(f"   Cache: {n_cached} tickers fresh from disk, {n_stale} need downloading.")

    CHUNK  = 25
    chunks = [stale_tickers[i:i+CHUNK] for i in range(0, len(stale_tickers), CHUNK)]

    fresh_parts = {}   # sym → Series (freshly downloaded)
    if chunks:
        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
        import io as _io, contextlib as _ctx

        def _dl_chunk(idx_chunk):
            idx, chunk = idx_chunk
            _sink = _io.StringIO()
            with _ctx.redirect_stdout(_sink), _ctx.redirect_stderr(_sink):
                raw = yf.download(chunk, start=_start_str,
                                  auto_adjust=True, progress=False)['Close']
            if isinstance(raw, pd.Series):
                raw = raw.to_frame(name=chunk[0])
            return idx, raw

        log(f"   Launching {len(chunks)} parallel download chunk(s) for {n_stale} stale tickers…")
        with _TPE(max_workers=min(len(chunks), 8)) as _ex:
            futs = {_ex.submit(_dl_chunk, (i, c)): i for i, c in enumerate(chunks)}
            for fut in _ac(futs):
                idx, raw = fut.result()
                for col in raw.columns:
                    s = raw[col].dropna()
                    if not s.empty:
                        fresh_parts[str(col)] = s
                        cache_set_prices(str(col), s)   # persist to disk
                log(f"   Chunk {idx+1}/{len(chunks)} done.")

    # Merge cached + fresh into a single hist_data DataFrame
    all_series = {}
    for sym in tickers_listed:
        if sym in fresh_parts:
            all_series[sym] = fresh_parts[sym]
        elif sym in cached_parts:
            all_series[sym] = cached_parts[sym]
    if all_series:
        hist_data = pd.DataFrame(all_series).ffill()
    else:
        hist_data = pd.DataFrame()
    hist_data.columns = [str(c) for c in hist_data.columns]
    log(f"✅  Price data ready ({hist_data.shape[1]} tickers, {len(hist_data)} days).")

    short_name_map = df.drop_duplicates('Symbol').set_index('Symbol')['Name'].fillna('').to_dict()
    short_name_map = {k: (v if v else k) for k, v in short_name_map.items()}

    # ------------------------------------------------------------------
    # BUILD TOTAL-RETURN PRICE MATRIX
    # ------------------------------------------------------------------
    log("==> Building TR price matrix (dividend reinvestment)...")
    tr_hist = hist_data.copy()

    syms_needing_divs = [s for s in tickers_listed if s in hist_data.columns]

    # Check div cache first — only fetch stale dividend series
    divs_map: dict = {}
    stale_div_syms  = []
    for sym in syms_needing_divs:
        cached_div = cache_get_divs(sym)
        if cached_div is not None:
            divs_map[sym] = cached_div
        else:
            stale_div_syms.append(sym)

    log(f"   Div cache: {len(divs_map)} fresh, {len(stale_div_syms)} need fetching.")

    if stale_div_syms:
        from concurrent.futures import ThreadPoolExecutor as _TPEDIV, as_completed as _acdiv

        def _fetch_divs(sym):
            try:
                divs = yf.Ticker(sym).dividends
                if divs is None or divs.empty:
                    return sym, None
                if divs.index.tz is not None:
                    divs.index = divs.index.tz_localize(None)
                divs = divs[divs.index >= hist_data.index[0]]
                if not divs.empty:
                    cache_set_divs(sym, divs)   # persist to disk
                return sym, divs if not divs.empty else None
            except Exception:
                return sym, None

        CHUNK_DIV = 25
        n_div_chunks = -(-len(stale_div_syms) // CHUNK_DIV)
        with _TPEDIV(max_workers=min(20, len(stale_div_syms) or 1)) as _ex:
            futs = {_ex.submit(_fetch_divs, sym): sym for sym in stale_div_syms}
            done = 0
            for fut in _acdiv(futs):
                sym, divs = fut.result()
                divs_map[sym] = divs
                done += 1
                if done % CHUNK_DIV == 0 or done == len(stale_div_syms):
                    log(f"   TR dividends: {done}/{len(stale_div_syms)} fetched…")

    # Apply dividend adjustment factors to build the TR price matrix
    for sym, divs in divs_map.items():
        if divs is None:
            continue
        try:
            price_col = hist_data[sym].copy()
            drf = pd.Series(1.0, index=hist_data.index)
            for ex_date, div_amt in divs.items():
                pos = hist_data.index.searchsorted(ex_date, side='left')
                if pos == 0 or pos >= len(hist_data.index):
                    continue
                prev_price = price_col.iloc[pos - 1]
                if pd.isna(prev_price) or prev_price <= 0:
                    continue
                drf.iloc[pos:] *= (prev_price + div_amt) / prev_price
            tr_hist[sym] = (price_col * drf).round(6)
        except Exception:
            pass

    log("✅  TR price matrix ready.")

    all_frames       = {}
    weights_snapshot = {}
    board_map        = df_listed.set_index('Symbol')['Board'].to_dict()
    name_map         = df_listed.set_index('Symbol')['Name'].fillna('').to_dict()
    # Store full df_listed so PTS can detect non-MYIPO+ constituents (Quant1==0)
    # and use them as prediction market candidates
    self_df_listed   = df_listed.copy()

    idx_arr = hist_data.index

    def _snap_date(dt):
        pos = idx_arr.searchsorted(dt, side='right') - 1
        return idx_arr[pos] if pos >= 0 else None

    def _build_holdings(subset, qty_col, use_d365=False, price_data=None):
        px = price_data if price_data is not None else hist_data
        syms = subset['Symbol'].unique()
        hv        = pd.DataFrame(0.0, index=idx_arr, columns=syms)
        exit_mask = pd.DataFrame(False, index=idx_arr, columns=syms)

        for _, row in subset.iterrows():
            sym       = row['Symbol']
            qty       = row[qty_col]
            entry_dt  = row['Trade Date']          # D0 — first trading day
            d_plus365 = row.get('D+365', pd.NaT)
            offer_px  = row['Purchase Price']      # D-1 17:00 inclusion price

            if sym not in px.columns or pd.isna(offer_px) or qty <= 0:
                continue

            # D0 position in the trading calendar
            start_pos = idx_arr.searchsorted(entry_dt, side='left')
            if start_pos >= len(idx_arr):
                continue

            col_pos = hv.columns.get_loc(sym)

            # ── D-1 row: last trading day before D0
            #    This is the inclusion moment (17:00 close equivalent).
            #    Seeded with offer price so pct_change D-1→D0 = IPO first-day return.
            d_minus1_pos = start_pos - 1
            if d_minus1_pos >= 0:
                # Only write if this stock hasn't been placed here already
                # (guard against duplicate rows in the CSV for the same symbol)
                if hv.iloc[d_minus1_pos, col_pos] == 0.0:
                    hv.iloc[d_minus1_pos, col_pos] = offer_px * qty
            else:
                # D0 is the very first row of the calendar — seed D0 itself
                hv.iloc[0, col_pos] = offer_px * qty

            # ── D0 onward: live market prices
            if use_d365 and not pd.isna(d_plus365):
                end_pos = idx_arr.searchsorted(d_plus365, side='right')
                in_win  = px[sym].iloc[start_pos:end_pos]
                if not in_win.empty:
                    hv.iloc[start_pos:end_pos, col_pos] = (in_win * qty).values
                if end_pos < len(idx_arr):
                    exit_mask.iloc[end_pos, col_pos] = True
                hv.iloc[end_pos:, col_pos] = 0.0
            else:
                prices = px[sym].iloc[start_pos:]
                if not prices.empty:
                    hv.iloc[start_pos:, col_pos] = (prices * qty).values

        active = hv.sum(axis=1) > 0
        return hv[active], exit_mask[active]

    def _calc_index(holdings_val, exit_mask=None):
        daily_total = holdings_val.sum(axis=1)
        weights     = holdings_val.div(daily_total, axis=0)

        stock_returns = holdings_val.pct_change()

        # On the D-1 inclusion row, a stock transitions from 0 → offer_price×qty.
        # pct_change gives inf or a huge number there.  Zero it — no return is
        # earned at inclusion; the first-day pop is captured on D0 (offer→market).
        # Also zero any exit-day returns (handled via exit_mask below).
        entered_today = (holdings_val.shift(1) == 0.0) & (holdings_val > 0.0)
        stock_returns[entered_today] = 0.0
        stock_returns = stock_returns.fillna(0.0).replace([np.inf, -np.inf], 0.0)

        if exit_mask is not None:
            aligned_mask = exit_mask.reindex_like(stock_returns).fillna(False)
            stock_returns[aligned_mask] = 0.0

        # weights.shift(1): on D0, the weight is the D-1 (offer-price) weight →
        # correctly sizes each stock's first-day contribution by its inclusion value.
        index_daily_return = (stock_returns * weights.shift(1)).sum(axis=1)

        factors    = (1 + index_daily_return.values)
        factors[0] = 1.0
        index_values = np.cumprod(factors) * 100

        index_final = pd.Series(index_values, index=holdings_val.index)
        return index_final, weights

    # ------------------------------------------------------------------
    # ORIGINAL INDEX LOOP
    # ------------------------------------------------------------------
    index_holdings_val = {}   # label -> daily holdings-value DataFrame (for fund unit logic)
    index_exit_mask     = {}   # label -> daily exit-event DataFrame (for fund unit logic)

    for label, qty_col, board_filter, use_d365 in INDEX_CONFIG:
        log(f"   Building {label}...")
        subset = df_listed.copy()
        if board_filter is not None:
            subset = subset[subset['Board'] == board_filter]
        subset = subset[subset[qty_col] > 0].sort_values('Trade Date')
        if subset.empty: continue

        holdings_val, exit_mask   = _build_holdings(subset, qty_col, use_d365)
        index_final, weights      = _calc_index(holdings_val, exit_mask if use_d365 else None)

        index_holdings_val[label] = holdings_val
        index_exit_mask[label]    = exit_mask

        all_frames[label] = pd.DataFrame({
            label:            index_final.round(2),
            f'{label} Chg%': (index_final.pct_change() * 100).round(2)
        })
        weights_snapshot[label] = weights.iloc[-1][weights.iloc[-1] > 0].sort_values(ascending=False)

    # ------------------------------------------------------------------
    # Y-SERIES INDEX LOOP
    # ------------------------------------------------------------------
    for label, qty_col, year in YSERIES_CONFIG:
        log(f"   Building {label}...")
        subset = df_listed[df_listed['Trade Date'].dt.year == year].copy()
        subset = subset[subset[qty_col] > 0].sort_values('Trade Date')
        if subset.empty:
            log(f"   ⚠️  {label}: no IPOs found, skipped.")
            continue

        holdings_val, exit_mask = _build_holdings(subset, qty_col, use_d365=False)
        if holdings_val.empty:
            log(f"   ⚠️  {label}: holdings empty, skipped.")
            continue

        index_final, weights = _calc_index(holdings_val)

        index_holdings_val[label] = holdings_val
        index_exit_mask[label]    = exit_mask

        all_frames[label] = pd.DataFrame({
            label:            index_final.round(2),
            f'{label} Chg%': (index_final.pct_change() * 100).round(2)
        })
        weights_snapshot[label] = weights.iloc[-1][weights.iloc[-1] > 0].sort_values(ascending=False)
        log(f"   ✅ {label}: {len(subset)} IPOs | latest = {index_final.iloc[-1]:.2f}")

    # ------------------------------------------------------------------
    # TR-SERIES INDEX LOOP
    # ------------------------------------------------------------------
    log("==> Building TR-Series (Total Return) indices...")
    for label, qty_col, board_filter, use_d365 in TR_SERIES_CONFIG:
        log(f"   Building {label}...")
        subset = df_listed.copy()
        if board_filter is not None:
            subset = subset[subset['Board'] == board_filter]
        subset = subset[subset[qty_col] > 0].sort_values('Trade Date')
        if subset.empty:
            log(f"   ⚠️  {label}: no constituents, skipped.")
            continue

        holdings_val, exit_mask = _build_holdings(
            subset, qty_col, use_d365, price_data=tr_hist
        )
        if holdings_val.empty:
            log(f"   ⚠️  {label}: holdings empty, skipped.")
            continue

        index_final, weights = _calc_index(
            holdings_val, exit_mask if use_d365 else None
        )

        all_frames[label] = pd.DataFrame({
            label:            index_final.round(2),
            f'{label} Chg%': (index_final.pct_change() * 100).round(2)
        })
        core_label = label[:-3]
        weights_snapshot[label] = (
            weights_snapshot[core_label]
            if core_label in weights_snapshot
            else weights.iloc[-1][weights.iloc[-1] > 0].sort_values(ascending=False)
        )
        log(f"   ✅ {label}: latest = {index_final.iloc[-1]:.2f}")

    # ------------------------------------------------------------------
    # MYIPO+ FUND SERIES (Mutual Fund / Unit Trust type)
    # One fund per Quant1-based index — Core, Ace, Main, Dynamic365, and
    # every Y-Series year. Momentum (Quant2) is excluded by FUND_CONFIG.
    #
    # UNIT-TRUST MECHANICS (not a simple NAV-tracker):
    #   - Fund launches with FUND_UNITS_ISSUED units at par NAV (RM0.2500),
    #     on the same D-1 launch date as its underlying index.
    #   - Each day, NAV per unit moves with the *value-weighted* return of
    #     whatever the fund currently holds (mirrors the index's stock
    #     selection/weighting, but the fund tracks RM cash flows directly
    #     rather than rebasing to 100).
    #   - When the underlying index ADMITS a new constituent (entry event):
    #     the fund needs RM to buy in at that stock's inclusion value.
    #       1) Use available cash (built up from prior exits) first.
    #       2) If cash is insufficient, issue new units at the CURRENT NAV
    #          to cover the shortfall — this is real unit dilution/creation,
    #          mirroring real unit-trust subscription mechanics.
    #   - When the underlying index DROPS a constituent (exit event, e.g.
    #     Dynamic365's D+365 rolling exit): the fund sells that position,
    #     proceeds go to the cash bucket for future buy-ins.
    #   - Quarterly distributions (31 Mar / 30 Jun / 30 Sep / 31 Dec) pay
    #     out 100% of the DIVIDEND-ONLY return gap since the last payout —
    #     i.e. (TR index return − Core/base index return) over that window,
    #     applied to NAV. This is growth-focused: capital appreciation is
    #     never distributed, only the dividend income the basket actually
    #     earned. Skipped entirely if cash-per-unit is below the floor
    #     (FUND_DIST_CASH_FLOOR_PER_UNIT), so payouts never erode capital.
    # ------------------------------------------------------------------
    all_fund_dist_logs  = {}   # fund_label -> dist_log list
    all_fund_unit_logs  = {}   # fund_label -> unit creation/redemption log

    fee_note = f"RM{FUND_SELL_FEE_PER_TXN:.2f}/sell" if apply_sell_fee else "OFF (frictionless)"
    log(f"   Fund sell-transaction fee: {fee_note}")

    for fund_label, base_label, board_filter, use_d365 in FUND_CONFIG:
        log(f"   Building {fund_label}...")
        fund_dist_log = []   # list of (date, nav_before, dist_per_unit, nav_after)
        fund_unit_log = []   # (date, event, symbol, amount_rm, units_issued, units_outstanding, cash_after, nav_after)
        try:
            if base_label in all_frames and base_label in index_holdings_val:
                holdings_val = index_holdings_val[base_label]   # daily RM value per symbol (qty x price)
                exit_mask    = index_exit_mask.get(base_label)
                calendar     = holdings_val.index

                # TR twin (dividend-reinvested) — the return gap between this
                # and the base/Core index series is the dividend-only return,
                # which drives the distribution amount below.
                tr_label  = f'{base_label} TR'
                tr_series = all_frames[tr_label][tr_label] if tr_label in all_frames else None
                core_series = all_frames[base_label][base_label]

                # Launch date: the fund's basis must be IDENTICAL to the
                # index's actual inception. index_holdings_val[label] is
                # already trimmed by _build_holdings to start at its first
                # active row (the true D-1/inclusion row, or D0 itself in
                # the edge case where the very first IPO's D0 lands on the
                # first row of the whole price calendar) — so the fund's
                # launch date is simply that same first row, not a
                # re-derived guess that can land one row too early.
                launch_date = calendar[0]

                sub_hv     = holdings_val.loc[launch_date:]
                sub_dates  = sub_hv.index

                # Per-symbol entry/exit detection from the daily value matrix
                entered = (sub_hv.shift(1).fillna(0.0) == 0.0) & (sub_hv > 0.0)
                exited  = (sub_hv.shift(1).fillna(0.0) > 0.0) & (sub_hv == 0.0)
                if exit_mask is not None:
                    exit_aligned = exit_mask.reindex_like(sub_hv).fillna(False)
                else:
                    exit_aligned = exited

                # ── Daily simulation ────────────────────────────────────────
                units_out   = float(FUND_UNITS_ISSUED)
                # Seed cash with the par capital actually raised at launch
                # (1,000 units x RM0.2500 = RM250 raised, available to deploy
                # into the first constituent(s) before any new units are needed).
                cash        = FUND_UNITS_ISSUED * FUND_PAR_NAV
                nav         = FUND_PAR_NAV
                dist_dates_seen = set()

                # Reference points for the dividend-gap calc: TR/Core index
                # values as of the last distribution (or launch, initially).
                # Looked up by date from the full (non-truncated) series so
                # they're available even before sub_dates' own first row.
                tr_ref   = tr_series.loc[launch_date] if tr_series is not None and launch_date in tr_series.index else None
                core_ref = core_series.loc[launch_date] if launch_date in core_series.index else None

                nav_values  = []
                units_track = []
                aum_values  = []
                cash_values = []

                for i, dt in enumerate(sub_dates):
                    row_val   = sub_hv.iloc[i]
                    total_val = row_val.sum()   # market value of all current holdings (RM)

                    # ── Handle entries: new constituent needs buy-in RM ──────
                    entered_today = entered.iloc[i]
                    entered_syms  = entered_today[entered_today].index.tolist()
                    for sym in entered_syms:
                        buy_in_value = row_val[sym]   # RM needed to take this position
                        if buy_in_value <= 0:
                            continue
                        from_cash  = min(cash, buy_in_value)
                        shortfall  = buy_in_value - from_cash
                        cash      -= from_cash
                        units_issued = 0.0
                        if shortfall > 1e-9:
                            # Issue new units at current NAV to cover the gap
                            units_issued = shortfall / nav if nav > 0 else 0.0
                            units_out   += units_issued
                        # Log the SHORTFALL (RM actually funded by issuing new
                        # units), not from_cash — from_cash is the portion paid
                        # from existing cash on hand and isn't what triggered
                        # the unit issuance. Without this, a buy-in funded
                        # ENTIRELY by new units (cash already at zero, so
                        # from_cash=0) would show RM0.00 even though a real
                        # cash shortfall occurred and units were genuinely issued.
                        fund_unit_log.append((dt, 'entry', sym, shortfall, units_issued, units_out, cash, nav))

                    # ── Handle exits: liquidate to cash (net of sell fee) ────
                    exited_today = exit_aligned.iloc[i] if i < len(exit_aligned) else None
                    if exited_today is not None:
                        exited_syms = exited_today[exited_today].index.tolist()
                        # Use yesterday's value for the exiting position (today's row is already 0)
                        if i > 0:
                            prev_row = sub_hv.iloc[i-1]
                            for sym in exited_syms:
                                gross_proceeds = prev_row.get(sym, 0.0)
                                if gross_proceeds > 0:
                                    # Flat RM2.01 broker commission + tax per sell
                                    # transaction, deducted from proceeds before
                                    # they reach the fund's cash bucket. Buy-ins
                                    # (IPO allocation) remain free — only sells
                                    # are charged. Net proceeds floored at 0 so
                                    # a tiny position can't go cash-negative.
                                    # Optional — gated by the apply_sell_fee toggle.
                                    fee = FUND_SELL_FEE_PER_TXN if apply_sell_fee else 0.0
                                    net_proceeds = max(0.0, gross_proceeds - fee)
                                    cash += net_proceeds
                                    fund_unit_log.append((dt, 'exit', sym, -net_proceeds, 0.0, units_out, cash, nav))

                    # ── NAV update: (holdings value + cash) / units outstanding ──
                    total_assets = total_val + cash
                    nav = total_assets / units_out if units_out > 0 else nav

                    # ── Quarterly distribution ────────────────────────────────
                    # Distribution = 100% of the DIVIDEND-ONLY return gap since
                    # the last payout: (TR index return − Core index return)
                    # over that window. Capital growth is never distributed —
                    # this is a growth-focused fund.
                    #
                    # Two modes, controlled by drip_distributions:
                    #   CASH (default): pays out as cash, reducing NAV. Skipped
                    #     entirely if cash-per-unit is below the floor, so
                    #     payouts never erode the fund's capital base.
                    #   DRIP (reinvest): instead of paying cash, issues new
                    #     units to existing unitholders worth the distribution
                    #     amount, valued at the CURRENT NAV (scrip dividend).
                    #     NAV itself is unchanged — the value stays inside the
                    #     fund as additional units rather than leaving as cash,
                    #     so the cash floor check doesn't apply in this mode.
                    is_dist_date = (dt.month, dt.day) in FUND_DIST_MONTHS_DAYS
                    key = (dt.year, dt.month, dt.day)
                    if (is_dist_date and key not in dist_dates_seen and dt > launch_date
                            and tr_ref is not None and core_ref is not None
                            and dt in tr_series.index and dt in core_series.index):
                        tr_now   = tr_series.loc[dt]
                        core_now = core_series.loc[dt]
                        tr_ret   = (tr_now   / tr_ref   - 1.0) if tr_ref   else 0.0
                        core_ret = (core_now / core_ref - 1.0) if core_ref else 0.0
                        dividend_gap_pct = tr_ret - core_ret   # dividend-only contribution

                        if drip_distributions:
                            if dividend_gap_pct > 0:
                                dist_per_unit = nav * dividend_gap_pct
                                # Total distribution value across all units,
                                # reinvested as new units AT CURRENT NAV.
                                dist_total_value = dist_per_unit * units_out
                                new_units = dist_total_value / nav if nav > 0 else 0.0
                                units_out += new_units
                                # NAV unchanged — value stayed inside the fund.
                                fund_dist_log.append((dt, nav, dist_per_unit, nav))
                                fund_unit_log.append((dt, 'drip', f'{fund_label}',
                                                      0.0, new_units, units_out, cash, nav))
                        else:
                            cash_per_unit = cash / units_out if units_out > 0 else 0.0
                            if dividend_gap_pct > 0 and cash_per_unit >= FUND_DIST_CASH_FLOOR_PER_UNIT:
                                dist_per_unit = nav * dividend_gap_pct
                                nav_before    = nav
                                nav          -= dist_per_unit
                                # Distribution is only deducted from the cash bucket
                                # if cash actually covers it. If cash is short, the
                                # NAV markdown still applies but cash is left as-is
                                # (no artificial floor-to-zero masking a shortfall).
                                dist_total = dist_per_unit * units_out
                                if cash >= dist_total:
                                    cash -= dist_total
                                fund_dist_log.append((dt, nav_before, dist_per_unit, nav))
                            # else: skipped (no dividend gap, or cash below floor) —
                            # NAV is left untouched, capital growth is retained.

                        # Reset the reference points to today regardless of
                        # whether a payout fired, so next quarter's gap is
                        # measured from here.
                        tr_ref, core_ref = tr_now, core_now
                        dist_dates_seen.add(key)

                    nav_values.append(nav)
                    units_track.append(units_out)
                    aum_values.append(nav * units_out)
                    cash_values.append(cash)

                nav_series   = pd.Series(nav_values,   index=sub_dates)
                units_series = pd.Series(units_track,  index=sub_dates)
                aum_series   = pd.Series(aum_values,   index=sub_dates)

                # Reindex onto the full calendar — flat (ffill) before launch.
                # IMPORTANT: launch_date itself already holds the genuine
                # par-NAV seed value from the simulation (reindex placed it
                # correctly) — only rows STRICTLY BEFORE launch_date should
                # be wiped to NaN. pandas .loc[:launch_date] is INCLUSIVE,
                # so using it here would erase that real first-day value too
                # (the off-by-one bug that showed the fund's NAV starting
                # one day later than the index it's supposed to match).
                nav_full   = nav_series.reindex(idx_arr)
                units_full = units_series.reindex(idx_arr)
                aum_full   = aum_series.reindex(idx_arr)
                pre_launch_mask = idx_arr < launch_date
                nav_full.loc[pre_launch_mask]   = np.nan
                units_full.loc[pre_launch_mask] = np.nan
                aum_full.loc[pre_launch_mask]   = np.nan
                nav_full   = nav_full.ffill()
                units_full = units_full.ffill().fillna(FUND_UNITS_ISSUED)
                aum_full   = aum_full.ffill()

                # ── Cumulative distributions per unit, and Total Return NAV ──
                # CASH mode: the fund pays real quarterly cash distributions,
                # which permanently reduce NAV. Comparing NAV-only return
                # against a price-only index (zero dividend concept) always
                # understates the fund by however much was paid out, so
                # Total Return = NAV + cumulative distributions paid per
                # unit (matches the TR-Series convention) for a fair
                # apples-to-apples comparison.
                # DRIP mode: distributions are reinvested as new units, NAV
                # is never reduced — the value already stays inside NAV, so
                # CumDist is tracked for visibility/reporting only and is
                # NOT added again on top of NAV (that would double-count it).
                dist_per_day = pd.Series(0.0, index=idx_arr)
                if fund_dist_log:
                    for d, _, amt, _ in fund_dist_log:
                        if d in dist_per_day.index:
                            dist_per_day.loc[d] += amt
                cum_dist_full = dist_per_day.cumsum()
                cum_dist_full = cum_dist_full.where(~pre_launch_mask, 0.0)
                total_return_nav = nav_full if drip_distributions else (nav_full + cum_dist_full)

                fund_frame = pd.DataFrame({
                    fund_label:                    nav_full.round(4),        # NAV per unit (RM)
                    f'{fund_label} Chg%':         (nav_full.pct_change() * 100).round(2),
                    f'{fund_label} AUM':           aum_full.round(2),        # total fund size (RM)
                    f'{fund_label} Units':         units_full.round(2),      # units outstanding
                    f'{fund_label} CumDist':       cum_dist_full.round(4),   # cumulative distributions/unit (RM)
                    f'{fund_label} TotalReturn':   total_return_nav.round(4),# NAV + cum. distributions (RM)
                })
                all_frames[fund_label] = fund_frame
                all_fund_dist_logs[fund_label] = fund_dist_log
                all_fund_unit_logs[fund_label] = fund_unit_log

                n_creations = sum(1 for e in fund_unit_log if e[1] == 'entry' and e[4] > 0)
                log(f"   ✅ {fund_label}: launched {launch_date.date()} @ RM{FUND_PAR_NAV:.4f}, "
                    f"latest NAV = RM{nav_full.iloc[-1]:.4f}, "
                    f"units {FUND_UNITS_ISSUED:.0f}→{units_full.iloc[-1]:.2f} "
                    f"({n_creations} unit-creation events), "
                    f"{len(fund_dist_log)} distributions paid.")
            else:
                log(f"   ⚠️  {fund_label}: base index '{base_label}' not available, skipped.")
        except Exception as e:
            log(f"   ⚠️  {fund_label}: error — {e}")

    # Back-compat: expose the Core fund's dist log under the old singular name too
    fund_dist_log = all_fund_dist_logs.get(FUND_LABEL, [])

    # ------------------------------------------------------------------
    # VFUND / MODEL PORTFOLIO — standalone equal-weight closed-end funds
    # ------------------------------------------------------------------
    # Deliberately NOT run through the index-twin engine above: these have a
    # fixed basket (no Quant1 weighting), issue units once (no cash-shortfall
    # creation), and hold non-Bursa tickers. Bending the open-ended engine to
    # cover that would have made both harder to reason about.
    log("   Building VFund / Model Portfolio series...")
    for vf in VFUND_CONFIG:
        label = vf['label']
        try:
            syms = [f"{m}{vf['suffix']}" for m in vf['members']]
            have = [s for s in syms if s in hist_data.columns]
            missing = [s for s in syms if s not in hist_data.columns]

            if not have:
                log(f"   ⚠️  {label}: none of its {len(syms)} constituents have "
                    f"price data — skipped.")
                continue

            # Inception = first trading day on/after the configured date
            incept_target = pd.Timestamp(VFUND_INCEPTION)
            pos = idx_arr.searchsorted(incept_target, side='left')
            if pos >= len(idx_arr):
                log(f"   ⚠️  {label}: inception {VFUND_INCEPTION} is beyond the "
                    f"price calendar — skipped.")
                continue
            launch = idx_arr[pos]

            px = hist_data.loc[launch:, have].ffill()
            # A constituent with no price at launch can't be bought at launch
            first_row = px.iloc[0]
            tradable  = [s for s in have if first_row.get(s, 0) > 0]
            if not tradable:
                log(f"   ⚠️  {label}: no constituent priced on {launch.date()} — skipped.")
                continue
            px = px[tradable].ffill().bfill()

            par        = float(vf['par'])
            units_out  = float(FUND_UNITS_ISSUED)
            capital    = units_out * par            # raised once, at launch
            n          = len(tradable)
            per_name   = capital / n                # equal weight: 1/N of capital
            entry_px   = px.iloc[0]
            shares     = (per_name / entry_px)      # fixed share counts — CEF

            # NAV = basket value / units. No issuance, so it moves purely
            # with the constituents.
            basket_val = (px * shares).sum(axis=1)
            nav        = basket_val / units_out

            nav_full = nav.reindex(idx_arr)
            nav_full.loc[idx_arr < launch] = np.nan
            nav_full = nav_full.ffill()

            all_frames[label] = pd.DataFrame({
                label:            nav_full.round(4),
                f'{label} Chg%': (nav_full.pct_change() * 100).round(2),
            })
            weights_snapshot[label] = pd.Series(1.0 / n, index=tradable)

            note = f"{n} constituents @ {100.0/n:.2f}% each"
            if missing:
                note += f" ({len(missing)} unpriced: {', '.join(m[:6] for m in missing[:4])}" \
                        + ("…" if len(missing) > 4 else "") + ")"
            log(f"   ✅ {label}: launched {launch.date()} at "
                f"{vf['ccy']} {par:.4f} · {note} · "
                f"latest NAV {nav_full.iloc[-1]:.4f}")
        except Exception as e:
            log(f"   ⚠️  {label}: error — {e}")

    # ------------------------------------------------------------------
    # MYIPO+ TOP SERIES — Top 10 / 20 / 50 / 100
    # Monthly rebalance on the 21st (snap forward to next trading day).
    # Score = Quant1 x rebalance price. Equal weight 1/N. Chain-linked.
    # Universal history contract per rebalance:
    #   {'date', 'constituents', 'weights', 'prices', 'scores', 'in', 'out'}
    # ------------------------------------------------------------------
    def _build_top_series(top_n: int):
        """Build one Top-N index. Returns (label, frame, w_snap, history) or None."""
        label = f'MYIPO+ Top {top_n}'
        universe = df_listed[df_listed['Quant1'] > 0].copy()
        universe = universe.drop_duplicates('Symbol').sort_values('Trade Date')

        min_universe = top_n + 1
        if len(universe) < min_universe:
            log(f"   ⚠️  {label}: needs {min_universe}+ IPOs (have {len(universe)}), skipped.")
            return None

        activation_date = universe.iloc[min_universe - 1]['Trade Date']

        def _next_td(target_dt):
            pos = idx_arr.searchsorted(target_dt, side='left')
            return idx_arr[pos] if pos < len(idx_arr) else None

        cal_21sts = pd.date_range(start=pd.Timestamp('2020-11-21'), end=TODAY,
                                  freq='MS').map(lambda d: d.replace(day=21))
        rebalance_dates = []
        for cal_dt in cal_21sts:
            snapped = _next_td(cal_dt)
            if snapped is not None and snapped >= activation_date:
                rebalance_dates.append(snapped)
        rebalance_dates = sorted(set(rebalance_dates))
        if not rebalance_dates:
            log(f"   ⚠️  {label}: no valid rebalance dates, skipped.")
            return None

        first_rb     = rebalance_dates[0]
        tdays        = idx_arr[idx_arr >= first_rb]
        index_values = pd.Series(np.nan, index=tdays)
        index_level  = 100.0
        history      = []
        prev_members = set()

        for rb_idx, rb_date in enumerate(rebalance_dates):
            next_rb = rebalance_dates[rb_idx + 1] if rb_idx + 1 < len(rebalance_dates) \
                      else tdays[-1] + pd.Timedelta(days=1)
            period_days = tdays[(tdays >= rb_date) & (tdays < next_rb)]
            if len(period_days) == 0:
                continue

            eligible = universe[universe['Trade Date'] <= rb_date].copy()
            eligible = eligible[eligible['Symbol'].isin(hist_data.columns)]
            if eligible.empty:
                continue
            rb_snap = _snap_date(rb_date)
            if rb_snap is None:
                continue
            eligible['rb_price'] = eligible['Symbol'].map(
                lambda s: hist_data.loc[rb_snap, s]
                if s in hist_data.columns and rb_snap in hist_data.index else np.nan)
            eligible = eligible.dropna(subset=['rb_price'])
            eligible = eligible[eligible['rb_price'] > 0]
            if eligible.empty:
                continue

            eligible['score'] = eligible['Quant1'] * eligible['rb_price']
            top_sel = eligible.nlargest(top_n, 'score')
            syms    = top_sel['Symbol'].tolist()
            if not syms:
                continue

            # ── Universal history contract record ──────────────────────────
            cur_members = set(syms)
            n_actual    = len(syms)
            eq_w        = round(1.0 / n_actual, 6) if n_actual else 0
            history.append({
                'date':         rb_date,
                'constituents': sorted(cur_members),
                'weights':      {s: eq_w for s in syms},
                'prices':       {s: float(top_sel[top_sel['Symbol'] == s]['rb_price'].iloc[0])
                                 for s in syms},
                'scores':       {s: float(top_sel[top_sel['Symbol'] == s]['score'].iloc[0])
                                 for s in syms},
                'in':           sorted(cur_members - prev_members),
                'out':          sorted(prev_members - cur_members),
            })
            prev_members = cur_members

            avail_syms = [s for s in syms if s in hist_data.columns]
            px_period  = hist_data.loc[period_days, avail_syms].ffill().bfill()
            if px_period.empty:
                continue
            period_prices = px_period.values
            first_prices  = period_prices[0, :]
            valid_mask    = first_prices > 0
            if valid_mask.sum() == 0:
                continue
            units     = np.where(valid_mask, 1.0 / (first_prices * valid_mask.sum()), 0.0)
            port_vals = period_prices.dot(units)
            port_norm = port_vals / port_vals[0] * index_level
            index_values.loc[period_days] = port_norm
            index_level = port_norm[-1]

        index_values = index_values.dropna()
        if index_values.empty:
            log(f"   ⚠️  {label}: empty after calculation, skipped.")
            return None

        frame = pd.DataFrame({
            label:           index_values.round(2),
            f'{label} Chg%': (index_values.pct_change() * 100).round(2),
        })
        # Latest membership -> weights snapshot
        last_members = history[-1]['constituents'] if history else []
        w_snap = (pd.Series(1.0 / len(last_members), index=last_members)
                  if last_members else pd.Series(dtype=float))
        log(f"   ✅ {label}: {len(rebalance_dates)} rebalances | "
            f"{len(index_values)} days | latest = {index_values.iloc[-1]:.2f}")
        return label, frame, w_snap, history

    global _TOP_SERIES_HISTORY
    _TOP_SERIES_HISTORY = {}
    for _top_n in (10, 20, 50, 100):
        log(f"   Building MYIPO+ Top {_top_n}...")
        try:
            result_ts = _build_top_series(_top_n)
            if result_ts:
                ts_label, ts_frame, ts_wsnap, ts_hist = result_ts
                all_frames[ts_label]       = ts_frame
                weights_snapshot[ts_label] = ts_wsnap
                _TOP_SERIES_HISTORY[_top_n] = ts_hist
                if _top_n == 10:
                    global _TOP10_HISTORY
                    _TOP10_HISTORY = [
                        {'date': r['date'], 'members': r['constituents'],
                         'in': r['in'], 'out': r['out']}
                        for r in ts_hist
                    ]
        except Exception as e:
            log(f"   ⚠️  MYIPO+ Top {_top_n}: error — {e}")

    output_df = pd.concat(all_frames.values(), axis=1, sort=True).dropna(how='all').sort_index()
    stock_price_df = hist_data.copy()

    delist_candidates = df_listed[df_listed['Quant1'] > 0].copy()
    delist_candidates = delist_candidates.dropna(subset=['D+365'])
    delist_candidates = delist_candidates[delist_candidates['D+365'] >= (TODAY - pd.DateOffset(days=7))]
    delist_candidates = delist_candidates.sort_values('D+365')

    # Stash fund distribution history in weights_snapshot under reserved keys
    # (keeps the public return signature stable across the app).
    # '__FUND_DIST_LOG__' = Core fund's log only, kept for backward compat.
    # '__ALL_FUND_DIST_LOGS__' = dict of every fund's log, keyed by fund label.
    # '__ALL_FUND_UNIT_LOGS__' = dict of every fund's unit creation/redemption log.
    weights_snapshot['__FUND_DIST_LOG__']     = fund_dist_log
    weights_snapshot['__ALL_FUND_DIST_LOGS__'] = all_fund_dist_logs
    weights_snapshot['__ALL_FUND_UNIT_LOGS__'] = all_fund_unit_logs

    return all_frames, output_df, weights_snapshot, board_map, short_name_map, future_ipos, stock_price_df, delist_candidates, self_df_listed


# =============================================================================
# GUI APPLICATION
# =============================================================================
class MYIPOApp(tk.Tk):
    def __init__(self, preloaded=None, csv_path=None):
        super().__init__()
        self.title("MYIPO Index Dashboard")
        self.geometry("1280x800")
        self.configure(bg='#0d1117')
        self.resizable(True, True)

        self.all_frames       = {}
        self.output_df        = None
        self.weights_snapshot = {}
        self.board_map        = {}
        self.short_name_map   = {}
        self.future_ipos      = pd.DataFrame()
        self.stock_price_df   = pd.DataFrame()
        self.delist_candidates = pd.DataFrame()
        self.df_listed_all    = pd.DataFrame()   # full listed universe incl. non-MYIPO+ stocks
        self.stock_symbol_var = tk.StringVar(value='')
        self.input_file      = tk.StringVar(value=csv_path or DEFAULT_INPUT_FILE)
        self.output_file     = tk.StringVar(value=DEFAULT_OUTPUT_CSV)
        self.prefer_online   = tk.BooleanVar(value=True)
        self.apply_sell_fee  = tk.BooleanVar(value=True)   # optional RM2.01/sell fee on fund exits
        self.drip_distributions = tk.BooleanVar(value=False)   # reinvest distributions as units instead of cash
        self._data_source    = 'local'   # updated after each load

        # NEWPOR (MYFOLIO/LottoStock) data — preloaded once at SplashScreen
        # startup, same pattern as the MYIPO index data itself, so opening
        # MYFOLIO or LottoStock is instant instead of triggering its own
        # fresh network fetch each time.
        self.newpor_rows       = None   # raw ledger rows from _mf_load_csv
        self.newpor_portfolios = None   # pre-built dict from _mf_build_portfolios
        self.newpor_validation = None   # _mf_validate_ledger result

        self.period_var  = tk.StringVar(value='All')
        self.PERIODS     = ['1D', '5D', '1M', '3M', '6M', 'YTD', '1Y', '3Y', '5Y', 'All']

        self.check_vars  = {lbl: tk.BooleanVar(value=False) for lbl in ALL_INDEX_LABELS}
        for lbl in [c[0] for c in INDEX_CONFIG]:
            self.check_vars[lbl].set(True)

        self.show_datapoints_var = tk.BooleanVar(value=False)
        self.chart_mode_var      = tk.StringVar(value='Level')   # Level | Monthly % | Rebased
        self.log_scale_var       = tk.BooleanVar(value=False)

        self._build_ui()

        if preloaded is not None:
            if len(preloaded) == 9:
                frames, df, wsnap, bmap, snames, future_ipos, stock_price_df, delist_candidates, df_listed_all = preloaded
            elif len(preloaded) == 8:
                frames, df, wsnap, bmap, snames, future_ipos, stock_price_df, delist_candidates = preloaded
                df_listed_all = pd.DataFrame()
            elif len(preloaded) == 7:
                frames, df, wsnap, bmap, snames, future_ipos, stock_price_df = preloaded
                delist_candidates = pd.DataFrame(); df_listed_all = pd.DataFrame()
            elif len(preloaded) == 6:
                frames, df, wsnap, bmap, snames, future_ipos = preloaded
                stock_price_df = pd.DataFrame(); delist_candidates = pd.DataFrame(); df_listed_all = pd.DataFrame()
            else:
                frames, df, wsnap, bmap, snames = preloaded
                future_ipos = pd.DataFrame(); stock_price_df = pd.DataFrame()
                delist_candidates = pd.DataFrame(); df_listed_all = pd.DataFrame()
            self.all_frames        = frames
            self.output_df         = df
            self.weights_snapshot  = wsnap
            self.board_map         = bmap
            self.short_name_map    = snames
            self.future_ipos       = future_ipos
            self.stock_price_df    = stock_price_df
            self.delist_candidates = delist_candidates
            self.df_listed_all     = df_listed_all
            self.delist_candidates = delist_candidates
            self.after(100, self._on_load_done)

    def _build_ui(self):
        top = tk.Frame(self, bg='#0d1117', pady=6)
        top.pack(fill='x', padx=10)

        tk.Label(top, text="MYIPO Index Dashboard",
                 bg='#0d1117', fg='#00C9FF',
                 font=('Segoe UI', 16, 'bold')).pack(side='left')

        tk.Label(top, text=f"  {datetime.now().strftime('%d %b %Y')}",
                 bg='#0d1117', fg='#888', font=('Segoe UI', 10)).pack(side='left')

        tk.Button(top, text='⟳  Refresh Data', command=self._run_refresh,
                  bg='#00C9FF', fg='#0d1117', font=('Segoe UI', 10, 'bold'),
                  relief='flat', padx=12, pady=4, cursor='hand2').pack(side='right', padx=4)

        tk.Button(top, text='💼  Portfolio (SCA)', command=self._open_sca,
                  bg='#00b4d8', fg='#0d1117', font=('Segoe UI', 10, 'bold'),
                  relief='flat', padx=12, pady=4, cursor='hand2').pack(side='right', padx=4)

        tk.Button(top, text='🎲  LottoStock', command=self._open_lottostock,
                  bg='#f0c040', fg='#0d1117', font=('Segoe UI', 10, 'bold'),
                  relief='flat', padx=12, pady=4, cursor='hand2').pack(side='right', padx=4)



        tk.Button(top, text='💾  Export CSV', command=self._export_csv,
                  bg='#238636', fg='white', font=('Segoe UI', 10, 'bold'),
                  relief='flat', padx=12, pady=4, cursor='hand2').pack(side='right', padx=4)

        tk.Button(top, text='⚙️  Settings', command=self._open_settings,
                  bg='#21262d', fg='#cdd9e5', font=('Segoe UI', 10, 'bold'),
                  relief='flat', padx=12, pady=4, cursor='hand2').pack(side='right', padx=4)

        tk.Checkbutton(
            top, text='☁️  Online DB', variable=self.prefer_online,
            bg='#0d1117', fg='#cdd9e5', selectcolor='#0d1117',
            activebackground='#0d1117', activeforeground='#00C9FF',
            font=('Segoe UI', 9), relief='flat', cursor='hand2'
        ).pack(side='right', padx=8)

        self._source_label_var = tk.StringVar(value='source: —')
        tk.Label(top, textvariable=self._source_label_var,
                 bg='#0d1117', fg='#555', font=('Segoe UI', 8)).pack(side='right', padx=4)

        tk.Button(top, text='📂  Input File', command=self._pick_file,
                  bg='#333', fg='white', font=('Segoe UI', 10),
                  relief='flat', padx=10, pady=4, cursor='hand2').pack(side='right', padx=4)

        tk.Checkbutton(
            top, text='Show Data Points', variable=self.show_datapoints_var,
            command=self._redraw_chart,
            bg='#0d1117', fg='#cdd9e5', selectcolor='#0d1117',
            activebackground='#0d1117', activeforeground='#00C9FF',
            font=('Segoe UI', 9), relief='flat', cursor='hand2'
        ).pack(side='right', padx=8)

        main = tk.Frame(self, bg='#0d1117')
        main.pack(fill='both', expand=True, padx=10, pady=(0, 6))

        sidebar_outer = tk.Frame(main, bg='#161b22', width=285, relief='flat')
        sidebar_outer.pack(side='left', fill='y', padx=(0, 8))
        sidebar_outer.pack_propagate(False)

        top_block = tk.Frame(sidebar_outer, bg='#161b22')
        top_block.pack(fill='x', side='top')

        tk.Label(top_block, text="Indices", bg='#161b22', fg='#cdd9e5',
                 font=('Segoe UI', 11, 'bold')).pack(anchor='w', padx=10, pady=(10, 4))

        self._accordion_open = {}

        def _make_section(parent, title, labels, accent='#555'):
            self._accordion_open[title] = tk.BooleanVar(value=True)
            hdr = tk.Frame(parent, bg='#1e252d', cursor='hand2')
            hdr.pack(fill='x', padx=4, pady=(3, 0))
            arrow_var = tk.StringVar(value='▾')
            tk.Label(hdr, textvariable=arrow_var,
                     bg='#1e252d', fg=accent,
                     font=('Segoe UI', 8)).pack(side='left', padx=(6, 2))
            tk.Label(hdr, text=title, bg='#1e252d', fg=accent,
                     font=('Segoe UI', 8, 'bold')).pack(side='left')
            body = tk.Frame(parent, bg='#161b22')
            body.pack(fill='x', padx=4)
            for lbl in labels:
                self._make_checkbox(body, lbl)
            def _toggle(event=None, av=arrow_var, t=title, b=body, p=parent):
                if self._accordion_open[t].get():
                    b.pack_forget()
                    av.set('▸')
                    self._accordion_open[t].set(False)
                else:
                    b.pack(fill='x', padx=4)
                    av.set('▾')
                    self._accordion_open[t].set(True)
            for w in [hdr] + list(hdr.winfo_children()):
                w.bind('<Button-1>', _toggle)
            return body

        _make_section(top_block, 'Core',
                      [c[0] for c in INDEX_CONFIG], accent='#00C9FF')
        _make_section(top_block, 'Y-Series',
                      [c[0] for c in YSERIES_CONFIG], accent='#888')
        _make_section(top_block, 'TR-Series (Total Return)',
                      [c[0] for c in TR_SERIES_CONFIG], accent='#7FECFF')
        _make_section(top_block, 'Top Series (Monthly Rebalanced)',
                      TOP_SERIES_LABELS, accent='#FFD700')

        # ── World (Performance) — history is fetched on demand ────────────────
        w_hdr = tk.Frame(top_block, bg='#161b22')
        w_hdr.pack(fill='x', padx=6, pady=(6, 0))
        tk.Label(w_hdr, text='▸ World (Performance)', font=('Segoe UI', 8, 'bold'),
                 fg='#FF6B6B', bg='#161b22').pack(side='left')
        tk.Button(w_hdr, text='🌏 Load', font=('Segoe UI', 7),
                  bg='#21262d', fg='#FF6B6B', relief='flat', bd=0,
                  cursor='hand2', padx=6,
                  command=self._open_world_chart).pack(side='right')
        self._world_section = tk.Frame(top_block, bg='#161b22')
        self._world_section.pack(fill='x')
        tk.Label(self._world_section,
                 text='   KLCI · Hang Seng · KOSPI · S&P · Nasdaq…\n'
                      '   Rebased to 100 for comparison.',
                 font=('Segoe UI', 7), fg='#555', bg='#161b22',
                 justify='left').pack(anchor='w', padx=(18, 6))

        fee_row = tk.Frame(top_block, bg='#161b22')
        fee_row.pack(fill='x', padx=10, pady=(8, 2))
        tk.Checkbutton(
            fee_row, text='💸  Apply RM2.01 sell fee (fund exits)',
            variable=self.apply_sell_fee, command=self._run_refresh,
            bg='#161b22', fg='#cdd9e5', selectcolor='#161b22',
            activebackground='#161b22', activeforeground='#00E5A0',
            font=('Segoe UI', 8), relief='flat', cursor='hand2'
        ).pack(anchor='w')

        _make_section(top_block, 'VFund / Model Portfolio (Unit Trust, Quarterly Dist.)',
                      FUND_LABELS, accent='#00E5A0')
        _make_section(top_block, 'VFund — Equal-Weight CEF (from 2026)',
                      VFUND_LABELS, accent='#C77DFF')

        btn_row = tk.Frame(top_block, bg='#161b22')
        btn_row.pack(fill='x', padx=8, pady=6)
        for _txt, _cmd, _fg in [
            ('All',      self._select_all,     '#cdd9e5'),
            ('None',     self._deselect_all,    '#cdd9e5'),
            ('Core',     self._select_core,     '#cdd9e5'),
            ('Y-Series', self._select_yseries,  '#cdd9e5'),
            ('TR',       self._select_trseries, '#7FECFF'),
            ('Top10',    self._select_top10,    '#FFD700'),
            ('World',    self._open_world_chart, '#FF6B6B'),
            ('VFund',    self._select_fund,     '#00E5A0'),
            ('EW-CEF',   self._select_vfund,    '#C77DFF'),
        ]:
            tk.Button(btn_row, text=_txt, command=_cmd,
                      bg='#21262d', fg=_fg, relief='flat',
                      font=('Segoe UI', 8), cursor='hand2',
                      padx=2, pady=2
                      ).pack(side='left', expand=True, fill='x', padx=1)

        bottom_block = tk.Frame(sidebar_outer, bg='#161b22')
        bottom_block.pack(fill='both', expand=True, side='bottom')

        tk.Label(bottom_block, text="Period", bg='#161b22', fg='#cdd9e5',
                 font=('Segoe UI', 11, 'bold')).pack(anchor='w', padx=10, pady=(6, 2))
        period_frame = tk.Frame(bottom_block, bg='#161b22')
        period_frame.pack(fill='x', padx=8)
        for i, p in enumerate(self.PERIODS):
            tk.Radiobutton(
                period_frame, text=p, variable=self.period_var, value=p,
                command=self._redraw_chart,
                bg='#161b22', fg='#cdd9e5', selectcolor='#00C9FF',
                activebackground='#161b22', activeforeground='#00C9FF',
                font=('Segoe UI', 9), indicatoron=False,
                relief='flat', bd=0, padx=8, pady=3, cursor='hand2'
            ).grid(row=i // 5, column=i % 5, sticky='ew', padx=2, pady=2)
        for c in range(5):
            period_frame.columnconfigure(c, weight=1)

        tk.Label(bottom_block, text="Summary", bg='#161b22', fg='#cdd9e5',
                 font=('Segoe UI', 11, 'bold')).pack(anchor='w', padx=10, pady=(10, 2))

        self.stats_tree = ttk.Treeview(
            bottom_block, columns=('Index', 'Last', 'Chg%', 'Ret%', 'Units'),
            show='headings', height=18
        )
        style = ttk.Style()
        style.theme_use('default')
        style.configure('Treeview',
                        background='#161b22', foreground='#cdd9e5',
                        fieldbackground='#161b22', rowheight=22,
                        font=('Segoe UI', 8))
        style.configure('Treeview.Heading',
                        background='#21262d', foreground='#cdd9e5',
                        font=('Segoe UI', 8, 'bold'), relief='flat')
        style.map('Treeview', background=[('selected', '#2d333b')])

        for col, w, anchor in [('Index', 100, 'w'), ('Last', 56, 'e'), ('Chg%', 50, 'e'),
                                ('Ret%', 50, 'e'), ('Units', 62, 'e')]:
            self.stats_tree.heading(col, text=col)
            self.stats_tree.column(col, width=w, anchor=anchor, stretch=False)
        self.stats_tree.pack(fill='both', expand=True, padx=8, pady=(0, 8))

        right = tk.Frame(main, bg='#0d1117')
        right.pack(side='left', fill='both', expand=True)

        info_bar = tk.Frame(right, bg='#101820')
        info_bar.pack(fill='x', pady=(0, 6))

        self._show_ipo_var    = tk.BooleanVar(value=True)
        self._show_delist_var = tk.BooleanVar(value=True)

        tk.Checkbutton(
            info_bar, text='🆕 IPO', variable=self._show_ipo_var,
            command=self._update_future_header,
            bg='#101820', fg='#3FB950', selectcolor='#101820',
            activebackground='#101820', activeforeground='#3FB950',
            font=('Segoe UI', 8, 'bold'), relief='flat', cursor='hand2'
        ).pack(side='left', padx=(6, 0))

        tk.Checkbutton(
            info_bar, text='🔴 Delist', variable=self._show_delist_var,
            command=self._update_future_header,
            bg='#101820', fg='#F85149', selectcolor='#101820',
            activebackground='#101820', activeforeground='#F85149',
            font=('Segoe UI', 8, 'bold'), relief='flat', cursor='hand2'
        ).pack(side='left', padx=(4, 8))

        tk.Frame(info_bar, bg='#30363D', width=1).pack(side='left', fill='y', pady=4)

        self.future_header_var = tk.StringVar(value='Loading events…')
        self.future_header = tk.Label(
            info_bar, textvariable=self.future_header_var,
            bg='#101820', fg='#cdd9e5', anchor='w', padx=8,
            font=('Segoe UI', 9), relief='flat'
        )
        self.future_header.pack(side='left', fill='x', expand=True)
        self._future_headline_full  = ''
        self._future_headline_pos   = 0
        self._future_headline_width = 190
        self._future_item_idx       = 0
        self._future_item_signature = ''
        self._future_tick_count     = 0
        self.after(1000, self._tick_future_header)

        # ── Bursa Market Indices Ticker ───────────────────────────────────────
        self._build_market_ticker(right)

        self.notebook = ttk.Notebook(right)
        style.configure('TNotebook', background='#0d1117', borderwidth=0)
        style.configure('TNotebook.Tab', background='#21262d', foreground='#cdd9e5',
                        font=('Segoe UI', 9), padding=[10, 4])
        style.map('TNotebook.Tab', background=[('selected', '#161b22')],
                  foreground=[('selected', '#00C9FF')])
        self.notebook.pack(fill='both', expand=True)

        self.tab_line  = tk.Frame(self.notebook, bg='#0d1117')
        self.notebook.add(self.tab_line, text='📈  Line Chart')
        self._build_line_tab()

        self.tab_bar   = tk.Frame(self.notebook, bg='#0d1117')
        self.notebook.add(self.tab_bar, text='📊  Returns Bar')
        self._build_bar_tab()

        self.tab_table = tk.Frame(self.notebook, bg='#0d1117')
        self.notebook.add(self.tab_table, text='🗃️  Data Table')
        self._build_table_tab()

        self.tab_stock = tk.Frame(self.notebook, bg='#0d1117')
        self.notebook.add(self.tab_stock, text='📌  Stock Perf')
        self._build_stock_tab()

        self.tab_comp = tk.Frame(self.notebook, bg='#0d1117')
        self.notebook.add(self.tab_comp, text='🧩  Composition')
        self._build_comp_tab()

        self.tab_fund_events = tk.Frame(self.notebook, bg='#0d1117')
        self.notebook.add(self.tab_fund_events, text='💰  VFund Events')
        self._build_fund_events_tab()

        self.tab_fund_ledger = tk.Frame(self.notebook, bg='#0d1117')
        self.notebook.add(self.tab_fund_ledger, text='📒  VFund Ledger')
        self._build_fund_ledger_tab()

        self.tab_pts = tk.Frame(self.notebook, bg='#0d1117')
        self.notebook.add(self.tab_pts, text='🎯  PTS')

        self.tab_zdws = tk.Frame(self.notebook, bg='#0d1117')
        self.notebook.add(self.tab_zdws, text='⚡  ZDWS')
        self.tab_zdos = tk.Frame(self.notebook, bg='#0d1117')
        self.notebook.add(self.tab_zdos, text='📐  ZDOS')

        self.status_var = tk.StringVar(value='Ready — load a CSV to begin.')
        status_bar = tk.Label(self, textvariable=self.status_var,
                              bg='#161b22', fg='#888', font=('Segoe UI', 9),
                              anchor='w', padx=10)
        status_bar.pack(fill='x', side='bottom')

        self.notebook.bind('<<NotebookTabChanged>>', lambda e: self._redraw_chart())

    # ── MYFOLIO launcher ──────────────────────────────────────────────────────
    def _open_myfolio(self):
        """MYFOLIO is now merged into SCA Tracker for a unified portfolio view."""
        self._open_sca()

    def _open_lottostock(self):
        """Open LottoStock, linked live to this K-ID's SWAY1-MYR holdings."""
        has_src = sca_has_source(load_app_settings())
        LottoStockWindow(self,
                          preloaded_rows=self.newpor_rows if has_src else None,
                          preloaded_portfolios=self.newpor_portfolios if has_src else None,
                          preloaded_validation=self.newpor_validation if has_src else None)

    def _open_sca(self):
        """Open the SCA · NEWPOR TRACKER portfolio dashboard.
        The splash preloads the ledger before we know whose K-ID is active, so
        only hand that preload over once the user has confirmed their source —
        otherwise SCA would render demo rows under their name."""
        has_src = sca_has_source(load_app_settings())
        SCAWindow(self,
                  preloaded_rows=self.newpor_rows if has_src else None,
                  preloaded_portfolios=self.newpor_portfolios if has_src else None)

    def _open_settings(self):
        """Settings — K-ID account, cache management."""
        win = tk.Toplevel(self)
        win.title("Settings")
        win.configure(bg='#0d1117')
        win.geometry("440x480")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        tk.Label(win, text="⚙ SETTINGS", font=("Segoe UI", 13, "bold"),
                 fg='#cdd9e5', bg='#0d1117').pack(pady=(18, 10))

        settings = load_app_settings()

        # ── K-ID account ──────────────────────────────────────────────────────
        box = tk.Frame(win, bg='#161b22', highlightbackground='#30363D',
                       highlightthickness=1, padx=16, pady=12)
        box.pack(fill='x', padx=20)
        tk.Label(box, text="🆔  K-ID ACCOUNT", font=("Segoe UI", 9, "bold"),
                 fg='#58A6FF', bg='#161b22').pack(anchor='w')

        acc = kid_find_account(settings, CURRENT_KID) if CURRENT_KID else None
        who = CURRENT_KID or 'guest (no account)'
        tk.Label(box, text=f"Signed in as:  {who}",
                 font=("Segoe UI", 10, "bold"), fg='#cdd9e5',
                 bg='#161b22').pack(anchor='w', pady=(6, 0))
        if acc:
            tk.Label(box, text=f"K-ID created {acc.get('created', '—')}",
                     font=("Segoe UI", 7), fg='#555', bg='#161b22').pack(anchor='w')

        n_acc = len(settings.get('kid_accounts', []))
        tk.Label(box,
                 text=f"{n_acc} K-ID{'s' if n_acc != 1 else ''} on this device. "
                      f"Each has its own portfolio, PTS wallet and warrant book.",
                 font=("Segoe UI", 8), fg='#888', bg='#161b22',
                 wraplength=380, justify='left').pack(anchor='w', pady=(6, 0))

        def _sign_out():
            if not messagebox.askyesno(
                    "Sign Out",
                    "Sign out and return to the K-ID screen?\n\n"
                    "Unsaved chart selections will be lost.",
                    parent=win):
                return
            win.destroy()
            self.destroy()
            AppLockScreen().mainloop()

        def _change_pin():
            if not CURRENT_KID:
                messagebox.showinfo("K-ID",
                    "You're signed in as guest — create a K-ID first.", parent=win)
                return
            self._open_change_pin_dialog(parent=win)

        btn_row = tk.Frame(box, bg='#161b22')
        btn_row.pack(fill='x', pady=(10, 0))
        tk.Button(btn_row, text="🚪  Sign Out", font=("Segoe UI", 8, 'bold'),
                  bg='#21262d', fg='#58A6FF', relief='flat', padx=12, pady=4,
                  cursor='hand2', command=_sign_out).pack(side='left')
        tk.Button(btn_row, text="🔑  Change PIN", font=("Segoe UI", 8),
                  bg='#21262d', fg='#888', relief='flat', padx=12, pady=4,
                  cursor='hand2', command=_change_pin).pack(side='left', padx=(6, 0))

        # ── Cache info ────────────────────────────────────────────────────────
        cache_box = tk.Frame(win, bg='#161b22', highlightbackground='#30363D',
                             highlightthickness=1, padx=16, pady=12)
        cache_box.pack(fill='x', padx=20, pady=(10, 0))
        tk.Label(cache_box, text="💾  LOCAL CACHE  (paa_cache/ folder)",
                 font=("Segoe UI", 9, "bold"), fg='#3FB950', bg='#161b22').pack(anchor='w')
        try:
            total_sz = sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _, fs in os.walk(_CACHE_DIR) for f in fs
            )
            n_files  = sum(len(fs) for _, _, fs in os.walk(_CACHE_DIR))
            size_str = f'{total_sz/1024/1024:.1f} MB' if total_sz >= 1024*1024 else f'{total_sz/1024:.0f} KB'
            tk.Label(cache_box, text=f"{n_files} cached files · {size_str}",
                     font=("Segoe UI", 8), fg='#cdd9e5', bg='#161b22').pack(anchor='w', pady=(4, 0))
        except Exception:
            tk.Label(cache_box, text="Cache folder not found yet.",
                     font=("Segoe UI", 8), fg='#888', bg='#161b22').pack(anchor='w', pady=(4, 0))
        def _clear_cache():
            import shutil
            try:
                shutil.rmtree(_CACHE_DIR, ignore_errors=True)
                os.makedirs(_CACHE_DIR, exist_ok=True)
                messagebox.showinfo("Cache Cleared",
                    "Local cache cleared.\nAll data will be re-downloaded on next load.",
                    parent=win)
                win.destroy()
            except Exception as e:
                messagebox.showerror("Error", str(e), parent=win)
        tk.Button(cache_box, text="🗑  Clear All Cache", font=("Segoe UI", 8),
                  bg='#21262d', fg='#F85149', relief='flat', padx=10, pady=4,
                  cursor='hand2', command=_clear_cache).pack(anchor='w', pady=(6, 0))
        tk.Label(cache_box,
                 text="Prices, dividends and EPS only — your ledger and K-ID are untouched.",
                 font=("Segoe UI", 7), fg='#555', bg='#161b22',
                 wraplength=380, justify='left').pack(anchor='w', pady=(4, 0))

        tk.Button(win, text="Close", font=("Segoe UI", 10, "bold"),
                  bg='#21262d', fg='#cdd9e5', relief='flat', padx=20, pady=6,
                  cursor='hand2', command=win.destroy).pack(pady=(14, 0))

    def _open_change_pin_dialog(self, parent=None):
        """Change the signed-in K-ID's PIN."""
        win = tk.Toplevel(self)
        win.title("Change PIN")
        win.configure(bg='#0d1117')
        win.geometry("360x300")
        win.resizable(False, False)
        win.transient(parent or self)
        win.grab_set()

        tk.Label(win, text="🔑  CHANGE PIN", font=("Segoe UI", 12, "bold"),
                 fg='#58A6FF', bg='#0d1117').pack(pady=(18, 2))
        tk.Label(win, text=f"K-ID: {CURRENT_KID}", font=("Segoe UI", 8),
                 fg='#888', bg='#0d1117').pack(pady=(0, 14))

        old_var, new_var, cf_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for lbl, var in [("Current PIN", old_var), ("New PIN (4–8 digits)", new_var),
                         ("Confirm new PIN", cf_var)]:
            tk.Label(win, text=lbl, font=("Segoe UI", 8), fg='#8b949e',
                     bg='#0d1117').pack()
            tk.Entry(win, textvariable=var, show="•", justify="center",
                     font=("Segoe UI", 13), bg='#161b22', fg='#cdd9e5',
                     insertbackground='#cdd9e5', relief="flat",
                     width=12).pack(pady=(2, 8), ipady=4)

        err_var = tk.StringVar()
        tk.Label(win, textvariable=err_var, font=("Segoe UI", 8),
                 fg='#F85149', bg='#0d1117', wraplength=300).pack()

        def _apply():
            s = load_app_settings()
            if not kid_verify(s, CURRENT_KID, old_var.get()):
                err_var.set("Current PIN is wrong."); return
            n = new_var.get()
            if not (n.isdigit() and 4 <= len(n) <= 8):
                err_var.set("New PIN must be 4–8 digits."); return
            if n != cf_var.get():
                err_var.set("New PINs don't match."); return
            acc = kid_find_account(s, CURRENT_KID)
            acc['pin_hash'] = kid_hash_pin(CURRENT_KID, n)
            save_app_settings(s)
            win.destroy()
            messagebox.showinfo("K-ID", "PIN updated.", parent=self)

        tk.Button(win, text="Update PIN", font=("Segoe UI", 10, "bold"),
                  bg='#58A6FF', fg='#0d1117', relief='flat', padx=20, pady=6,
                  cursor='hand2', command=_apply).pack(pady=(10, 0))

    def _make_checkbox(self, parent, lbl):
        color = COLOR_MAP[lbl]
        row = tk.Frame(parent, bg='#161b22')
        row.pack(fill='x', padx=6, pady=1)
        tk.Label(row, text='●', bg='#161b22', fg=color,
                 font=('Segoe UI', 10)).pack(side='left')
        tk.Checkbutton(
            row, text=lbl, variable=self.check_vars[lbl],
            command=self._redraw_chart,
            bg='#161b22', fg='#cdd9e5', selectcolor='#161b22',
            activebackground='#161b22', activeforeground='#00C9FF',
            font=('Segoe UI', 8), anchor='w', relief='flat', cursor='hand2'
        ).pack(side='left', fill='x', expand=True)

    # ── Bursa Market Indices Ticker ───────────────────────────────────────────
    # KLCI + FBM EMAS + FBM70 + FBM Small Cap + FBM ACE
    # Market ticker strip config:
    # '^KLSE' fetched from Yahoo Finance (works fine)
    # Internal MYIPO+ keys read directly from self.all_frames (no yfinance needed)
    BURSA_INDICES = [
        ('^KLSE',           'KLCI',        'yahoo'),
        ('MYIPO+',          'MYIPO+',      'internal'),
        ('MYIPO+ Main',     'Main',        'internal'),
        ('MYIPO+ Ace',      'Ace',         'internal'),
        ('MYIPO+ Momentum', 'Momentum',    'internal'),
        ('MYIPO+ Top 10',   'Top 10',      'internal'),
        ('MYIPO+ Top 20',   'Top 20',      'internal'),
        ('MYIPO+ Top 50',   'Top 50',      'internal'),
        ('MYIPO+ Top 100',  'Top 100',     'internal'),
    ]

    def _build_market_ticker(self, parent):
        """Scrolling marquee ticker board — KLCI from Yahoo, MYIPO+ from all_frames.

        Rendered on a Canvas so the whole strip can slide. The item list is
        drawn twice back-to-back, so as copy A leaves the far edge copy B is
        already filling the near edge — that gives a seamless loop with no
        visible jump at the wrap point.
        """
        panel = tk.Frame(parent, bg='#0d1117',
                         highlightbackground='#21262d', highlightthickness=1)
        panel.pack(fill='x', pady=(0, 2))

        # ── Controls (right) ──────────────────────────────────────────────────
        ctl = tk.Frame(panel, bg='#0d1117')
        ctl.pack(side='right', padx=4)
        tk.Button(ctl, text='↻', font=('Segoe UI', 8), bg='#0d1117', fg='#444',
                  relief='flat', cursor='hand2', bd=0,
                  command=self._refresh_market_ticker).pack(side='right', padx=(2, 2))
        self._mkt_dir_btn = tk.Button(
            ctl, text='→', font=('Segoe UI', 8, 'bold'), bg='#0d1117', fg='#555',
            relief='flat', cursor='hand2', bd=0, width=2,
            command=self._toggle_marquee_dir)
        self._mkt_dir_btn.pack(side='right')
        self._mkt_play_btn = tk.Button(
            ctl, text='⏸', font=('Segoe UI', 8), bg='#0d1117', fg='#555',
            relief='flat', cursor='hand2', bd=0, width=2,
            command=self._toggle_marquee_run)
        self._mkt_play_btn.pack(side='right')
        self._mkt_spd_btn = tk.Button(
            ctl, text='1x', font=('Segoe UI', 7), bg='#0d1117', fg='#555',
            relief='flat', cursor='hand2', bd=0, width=3,
            command=self._cycle_marquee_speed)
        self._mkt_spd_btn.pack(side='right')
        self._mkt_time_var = tk.StringVar(value='')
        tk.Label(ctl, textvariable=self._mkt_time_var, font=('Segoe UI', 7),
                 fg='#555', bg='#0d1117').pack(side='right', padx=(0, 6))

        # ── Marquee canvas (fills the rest) ───────────────────────────────────
        self._mkt_canvas = tk.Canvas(panel, bg='#0d1117', height=22,
                                     highlightthickness=0, bd=0)
        self._mkt_canvas.pack(side='left', fill='x', expand=True)

        # Marquee state
        self._mkt_data      = {}      # sym -> (last, prev)
        self._mkt_offset    = 0.0
        self._mkt_dir       = 1       # +1 = content travels left→right, -1 = right→left
        self._mkt_running   = True
        self._mkt_speed     = 3.5     # px/frame @30fps — full loop ~30s
        self._mkt_span      = 1       # width of one full copy of the list
        self._mkt_hitboxes  = []      # (x1, x2, callback) in content coords
        self._mkt_anim_job  = None

        self._mkt_canvas.bind('<Enter>', lambda e: self._pause_marquee(True))
        self._mkt_canvas.bind('<Leave>', lambda e: self._pause_marquee(False))
        self._mkt_canvas.bind('<Button-1>', self._on_marquee_click)
        self._mkt_canvas.bind('<Motion>',   self._on_marquee_motion)
        self._mkt_canvas.bind('<Configure>', lambda e: self._draw_marquee())

        self.after(500, self._refresh_market_ticker)
        self._animate_marquee()

    # ── Marquee controls ─────────────────────────────────────────────────────
    def _toggle_marquee_dir(self):
        self._mkt_dir = -self._mkt_dir
        self._mkt_dir_btn.configure(text='→' if self._mkt_dir > 0 else '←')

    def _cycle_marquee_speed(self):
        """Cycle 1x / 2x / 0.5x — the board is long, so pace is a preference."""
        steps = [(3.5, '1x'), (7.0, '2x'), (1.75, '½x')]
        self._mkt_spd_i = (getattr(self, '_mkt_spd_i', 0) + 1) % len(steps)
        self._mkt_speed, lbl = steps[self._mkt_spd_i]
        self._mkt_spd_btn.configure(text=lbl)

    def _toggle_marquee_run(self):
        self._mkt_running = not self._mkt_running
        self._mkt_play_btn.configure(text='⏸' if self._mkt_running else '▶')

    def _pause_marquee(self, hovering: bool):
        """Hover pauses the scroll so a value can actually be read / clicked."""
        self._mkt_hover = hovering

    def _animate_marquee(self):
        try:
            if (self._mkt_running and not getattr(self, '_mkt_hover', False)
                    and self._mkt_span > 1):
                self._mkt_offset = (self._mkt_offset + self._mkt_dir * self._mkt_speed) \
                                   % self._mkt_span
                self._position_marquee()
            self._mkt_anim_job = self.after(33, self._animate_marquee)   # ~30 fps
        except tk.TclError:
            pass   # window closed

    def _position_marquee(self):
        """Slide both copies to match the current offset.
        Canvas has no absolute 'move group to x', so we track where each copy
        currently sits and apply the delta."""
        c = self._mkt_canvas
        try:
            want_a = -self._mkt_offset
            want_b = self._mkt_span - self._mkt_offset
            cur_a  = getattr(self, '_mkt_xa', 0.0)
            cur_b  = getattr(self, '_mkt_xb', float(self._mkt_span))
            c.move('grpA', want_a - cur_a, 0)
            c.move('grpB', want_b - cur_b, 0)
            self._mkt_xa, self._mkt_xb = want_a, want_b
        except tk.TclError:
            pass

    def _draw_marquee(self):
        """Render the ticker content twice, back to back, for a seamless loop."""
        c = getattr(self, '_mkt_canvas', None)
        if c is None:
            return
        c.delete('all')
        self._mkt_hitboxes = []

        F_HDR = ('Segoe UI', 6, 'bold')
        F_LBL = ('Segoe UI', 7, 'bold')
        F_VAL = ('Segoe UI', 8, 'bold')
        F_CHG = ('Segoe UI', 8)
        GAP   = 20
        y     = 11

        def _fmt(sym, last):
            """FX wants 4dp; index levels want 2dp with thousands separators."""
            if sym.startswith('FX:'):
                return f'{last:,.4f}'
            return f'{last:,.2f}'

        def _draw_copy(x0, tag):
            x = x0
            for si, (header, items) in enumerate(self._ticker_sections()):
                # Section header
                iid = c.create_text(x, y, text=header, font=F_HDR, fill='#484f58',
                                    anchor='w', tags=(tag,))
                x = c.bbox(iid)[2] + 10

                for sym, label in items:
                    last, prev = self._mkt_data.get(sym, (None, None))
                    top_n    = _TOP_LABEL_N.get(sym)      # None unless a Top-N
                    is_top   = top_n is not None
                    x_start  = x

                    iid = c.create_text(x, y, text=label, font=F_LBL,
                                        fill='#D29922' if is_top else '#888',
                                        anchor='w', tags=(tag,))
                    x = c.bbox(iid)[2] + 5

                    iid = c.create_text(x, y,
                                        text=_fmt(sym, last) if last is not None else '—',
                                        font=F_VAL, fill='#cdd9e5', anchor='w',
                                        tags=(tag,))
                    x = c.bbox(iid)[2] + 4

                    if last is not None and prev:
                        chg = last - prev
                        pct = chg / prev * 100 if prev else 0
                        sgn = '+' if chg >= 0 else ''
                        col = '#3FB950' if chg >= 0 else '#F85149'
                        arw = '▲' if chg > 0 else ('▼' if chg < 0 else '•')
                        txt = (f'{arw} {sgn}{pct:.2f}%' if sym.startswith('FX:')
                               else f'{arw} {sgn}{chg:,.2f} ({sgn}{pct:.2f}%)')
                        iid = c.create_text(x, y, text=txt, font=F_CHG, fill=col,
                                            anchor='w', tags=(tag,))
                        x = c.bbox(iid)[2]

                    if is_top:
                        # default arg binds n now, not at click time
                        self._mkt_hitboxes.append(
                            (x_start, x, lambda n=top_n: self._open_top10_board(n)))

                    x += GAP
                    c.create_text(x - GAP / 2, y, text='·', font=F_CHG,
                                  fill='#30363d', anchor='center', tags=(tag,))

                # Divider between sections
                iid = c.create_text(x, y, text='|', font=F_LBL, fill='#21262d',
                                    anchor='w', tags=(tag,))
                x = c.bbox(iid)[2] + GAP
            return x - x0

        span = _draw_copy(0, 'grpA')
        self._mkt_span = max(span, 1)
        _draw_copy(self._mkt_span, 'grpB')
        # Copies were just drawn at their nominal spots — resync the tracker
        # before positioning, or the first move() would be off by the old delta.
        self._mkt_xa, self._mkt_xb = 0.0, float(self._mkt_span)
        self._position_marquee()

    def _marquee_content_x(self, event_x):
        """Map a click on the canvas back to a position in the content."""
        return (event_x + self._mkt_offset) % self._mkt_span

    def _on_marquee_click(self, event):
        cx = self._marquee_content_x(event.x)
        for x1, x2, cb in self._mkt_hitboxes:
            if x1 <= cx <= x2:
                cb()
                return

    def _on_marquee_motion(self, event):
        cx = self._marquee_content_x(event.x)
        over = any(x1 <= cx <= x2 for x1, x2, _ in self._mkt_hitboxes)
        self._mkt_canvas.configure(cursor='hand2' if over else '')

    def _ticker_sections(self):
        """What the marquee shows, grouped. Each section is (header, [(sym, label)]).
        BURSA_INDICES stays untouched — PTS still iterates it — so the world
        block is layered on here rather than mixed into it.
        """
        # An internal series absent from all_frames was never built — e.g.
        # Top 100 stays gated until 101 MYIPO+ IPOs exist. Showing a permanent
        # 'Top 100 —' would just be noise, so those are dropped entirely.
        frames = getattr(self, 'all_frames', {}) or {}
        my = [(s, l) for s, l, src_kind in self.BURSA_INDICES
              if src_kind != 'internal' or s in frames]
        secs = [('🇲🇾 MALAYSIA', my)]
        world = [(s, l) for s, l, _ in self.WORLD_INDICES]
        if world:
            secs.append(('🌏 WORLD', world))
        # Derive FX from the currencies actually in use — add an index and
        # its FX leg follows automatically.
        ccys = sorted({c for _, _, c in self.WORLD_INDICES if c != 'MYR'})
        secs.append(('💱 FX', [(f'FX:{c}', f'{c}MYR') for c in ccys]))
        return secs

    def _refresh_market_ticker(self):
        """Refresh the marquee — internal MYIPO+ series are free, KLCI /
        world indices / FX are fetched in parallel with a 5-min disk cache."""
        import threading as _th

        def _worker():
            import yfinance as _yf
            from concurrent.futures import ThreadPoolExecutor, as_completed
            results = {}

            # ── Internal MYIPO+ series — instant, no network needed ───────────
            for sym, label, source in self.BURSA_INDICES:
                if source != 'internal':
                    continue
                try:
                    df = self.all_frames.get(sym)
                    if df is None or df.empty:
                        continue
                    if sym in df.columns:
                        s = df[sym].dropna()
                    else:
                        num_cols = df.select_dtypes('number').columns
                        if len(num_cols) == 0:
                            continue
                        s = df[num_cols[0]].dropna()
                    if len(s) < 1:
                        continue
                    last = float(s.iloc[-1])
                    prev = float(s.iloc[-2]) if len(s) >= 2 else last
                    results[sym] = (last, prev)
                except Exception:
                    pass

            # ── Yahoo symbols: KLCI + every world index, fetched together ─────
            def _fetch(sym):
                key = f'tick_{sym.replace("^", "")}'
                cached = cache_get_live_dict(key, _TTL_LIVE)
                if cached:
                    return sym, cached.get('last'), cached.get('prev')
                try:
                    t    = _yf.Ticker(sym)
                    fi   = t.fast_info
                    last = getattr(fi, 'last_price', None) or getattr(fi, 'lastPrice', None)
                    prev = getattr(fi, 'previous_close', None) or getattr(fi, 'previousClose', None)
                    if last is None or prev is None:
                        h = t.history(period='2d')
                        if not h.empty:
                            last = float(h['Close'].iloc[-1])
                            prev = float(h['Close'].iloc[-2]) if len(h) > 1 else last
                    if last:
                        last = float(last)
                        prev = float(prev) if prev else last
                        cache_set_live_dict(key, {'last': last, 'prev': prev})
                        return sym, last, prev
                except Exception:
                    pass
                return sym, None, None

            yahoo_syms = ['^KLSE'] + [s for s, _, _ in self.WORLD_INDICES]
            try:
                with ThreadPoolExecutor(max_workers=min(8, len(yahoo_syms))) as ex:
                    for fut in as_completed([ex.submit(_fetch, s) for s in yahoo_syms]):
                        sym, last, prev = fut.result()
                        if last is not None:
                            results[sym] = (last, prev)
            except Exception:
                pass
            # Keep the KLCI cache key the ZDWS level lookup also reads
            if '^KLSE' in results:
                l, p = results['^KLSE']
                cache_set_live_dict('market_indices_klci',
                                    {'^KLSE': {'last': l, 'prev': p}})

            # ── FX pairs — one per currency in WORLD_INDICES ──────────────────
            for ccy in sorted({c for _, _, c in self.WORLD_INDICES if c != 'MYR'}):
                sym, last, prev = _fetch(f'{ccy}MYR=X')
                if last is not None:
                    results[f'FX:{ccy}'] = (last, prev)

            self.after(0, lambda: self._apply_market_data(results))

        _th.Thread(target=_worker, daemon=True).start()

    def _apply_market_data(self, results):
        """Store fresh values and redraw the marquee content."""
        from datetime import datetime as _DT
        self._mkt_data = dict(results or {})
        self._draw_marquee()
        self._mkt_time_var.set(_DT.now().strftime('%H:%M'))

    def _open_top10_board(self, top_n: int = 10):
        """Historical in/out + weight board for any MYIPO+ Top-N series."""
        win = tk.Toplevel(self)
        win.title(f'MYIPO+ Top Series — Historical Board')
        win.configure(bg='#0d1117')
        win.geometry('820x680')
        win.transient(self)

        tk.Label(win, text='📋  MYIPO+ TOP SERIES — REBALANCE HISTORY',
                 font=('Segoe UI', 12, 'bold'), fg='#D29922',
                 bg='#0d1117').pack(pady=(14, 2))
        tk.Label(win, text='Monthly rebalance (21st) · Score = Quant1 × price · Equal weight 1/N',
                 font=('Segoe UI', 8), fg='#8b949e', bg='#0d1117').pack(pady=(0, 4))

        # ── N selector ──
        sel_row = tk.Frame(win, bg='#0d1117')
        sel_row.pack(pady=(0, 8))
        n_var = tk.IntVar(value=top_n)
        for n in (10, 20, 50, 100):
            avail = n in _TOP_SERIES_HISTORY and bool(_TOP_SERIES_HISTORY[n])
            tk.Radiobutton(sel_row, text=f'Top {n}', variable=n_var, value=n,
                           command=lambda: _rebuild(),
                           state='normal' if avail else 'disabled',
                           font=('Segoe UI', 9, 'bold'),
                           bg='#0d1117', fg='#D29922' if avail else '#444',
                           selectcolor='#0d1117', activebackground='#0d1117',
                           activeforeground='#FFD700', indicatoron=False,
                           relief='flat', padx=14, pady=4,
                           cursor='hand2' if avail else 'arrow').pack(side='left', padx=3)

        # ── Scrollable body ──
        wrap = tk.Frame(win, bg='#0d1117')
        wrap.pack(fill='both', expand=True)
        canvas = tk.Canvas(wrap, bg='#0d1117', highlightthickness=0)
        vsb    = ttk.Scrollbar(wrap, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True, padx=(14, 0))
        body = tk.Frame(canvas, bg='#0d1117')
        canvas.create_window((0, 0), window=body, anchor='nw', width=760)
        body.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))

        name_map = getattr(self, 'short_name_map', {})

        def _rebuild():
            for w in body.winfo_children():
                w.destroy()
            n    = n_var.get()
            hist = list(reversed(_TOP_SERIES_HISTORY.get(n, [])))
            if not hist:
                tk.Label(body, text=f'No history for Top {n}.\n'
                                    f'Needs {n+1}+ MYIPO+ IPOs listed to activate.',
                         font=('Segoe UI', 10), fg='#8b949e', bg='#0d1117',
                         justify='center').pack(pady=40)
                return
            for i, rec in enumerate(hist):
                date_str   = pd.Timestamp(rec['date']).strftime('%d %b %Y')
                is_current = (i == 0)
                card = tk.Frame(body, bg='#161b22',
                                highlightbackground='#D29922' if is_current else '#21262d',
                                highlightthickness=1, padx=12, pady=10)
                card.pack(fill='x', pady=4)
                title = f'🔄 Rebalance — {date_str}' + ('   ● CURRENT' if is_current else '')
                w_val = rec['weights'][rec['constituents'][0]] * 100 if rec['constituents'] else 0
                tk.Label(card, text=f'{title}    ·    {len(rec["constituents"])} members @ {w_val:.2f}% each',
                         font=('Segoe UI', 9, 'bold'),
                         fg='#D29922' if is_current else '#cdd9e5',
                         bg='#161b22', anchor='w').pack(fill='x')
                if rec['in']:
                    in_names = ', '.join(f"{s.replace('.KL','')} ({name_map.get(s, '')[:12]})"
                                         for s in rec['in'])
                    tk.Label(card, text=f'➕ IN:  {in_names}',
                             font=('Segoe UI', 8), fg='#3FB950', bg='#161b22',
                             wraplength=700, justify='left', anchor='w').pack(fill='x', pady=(4, 0))
                if rec['out']:
                    out_names = ', '.join(f"{s.replace('.KL','')} ({name_map.get(s, '')[:12]})"
                                          for s in rec['out'])
                    tk.Label(card, text=f'➖ OUT: {out_names}',
                             font=('Segoe UI', 8), fg='#F85149', bg='#161b22',
                             wraplength=700, justify='left', anchor='w').pack(fill='x', pady=(2, 0))
                mem_str = ', '.join(s.replace('.KL', '') for s in rec['constituents'])
                tk.Label(card, text=f'Members: {mem_str}',
                         font=('Segoe UI', 7), fg='#8b949e', bg='#161b22',
                         wraplength=700, justify='left', anchor='w').pack(fill='x', pady=(4, 0))

        _rebuild()

    # Charted alongside the world set, but deliberately not in WORLD_INDICES:
    # that list drives the marquee's World block and ZDWS FX handling, and
    # KLCI is neither foreign nor FX-converted.
    CHART_EXTRA = [('^KLSE', 'FBM KLCI', 'MYR')]

    def _chart_world_set(self):
        return list(self.CHART_EXTRA) + list(self.WORLD_INDICES)

    def _load_world_history(self, symbols=None, log_cb=None):
        """Fetch daily history for world indices and rebase each to 100 at its
        first date, so they share an axis with the MYIPO+ series.

        Rebasing is what makes this a PERFORMANCE chart: raw levels are
        ~24,000 (Hang Seng) vs ~5,700 (S&P) vs ~177 (MYIPO+). Plotted raw,
        everything but Hang Seng is a flat line on the floor. Cached to
        parquet (1-day TTL) like every other price series.
        """
        def log(m):
            if log_cb: log_cb(m)
        world  = self._chart_world_set()
        syms   = symbols or [s for s, _, _ in world]
        loaded = 0
        for sym in syms:
            label = next((l for s, l, _ in world if s == sym), sym)
            if label in self.all_frames:
                continue
            try:
                cached = cache_get_prices(sym)   # returns a Series
                if cached is not None and not cached.empty:
                    s = cached.dropna()
                else:
                    import yfinance as _yf
                    h = _yf.Ticker(sym).history(period='5y', interval='1d')
                    if h.empty:
                        log(f'   \u26a0\ufe0f  {label}: no history returned.')
                        continue
                    s = h['Close'].dropna()
                    if s.index.tz is not None:
                        s.index = s.index.tz_localize(None)
                    cache_set_prices(sym, s.to_frame(label))
                if s.empty:
                    continue
                base = float(s.iloc[0])
                if base <= 0:
                    continue
                reb = (s / base * 100).round(2)
                self.all_frames[label] = pd.DataFrame({
                    label:            reb,
                    f'{label} Chg%': (reb.pct_change() * 100).round(2),
                })
                COLOR_MAP.setdefault(label, WORLD_COLORS.get(label, '#8b949e'))
                LINE_STYLE_MAP.setdefault(label, (0, (2, 1)))
                loaded += 1
            except Exception as e:
                log(f'   \u26a0\ufe0f  {label}: {e}')
        if loaded:
            log(f'   \u2705 World: {loaded} series ready (rebased to 100).')
        return loaded

    def _open_world_chart(self):
        """Load world history on demand, then chart it."""
        self.status_var.set('\u2b07  Loading world index history\u2026')

        def _worker():
            self._load_world_history(
                log_cb=lambda m: self.after(0, lambda msg=m: self.status_var.set(msg)))
            self.after(0, _done)

        def _done():
            labels = [l for _, l, _ in self._chart_world_set() if l in self.all_frames]
            if not labels:
                self.status_var.set('\u26a0\ufe0f  No world index history available.')
                return
            self._rebuild_world_section(labels)
            self._deselect_all()
            for lbl in labels:
                if lbl in self.check_vars:
                    self.check_vars[lbl].set(True)
            # Rebased is the only honest way to compare across markets
            if hasattr(self, 'chart_mode_var'):
                self.chart_mode_var.set('Rebased')
            self._redraw_chart()
            self.status_var.set(f'\u2705  {len(labels)} world indices charted (rebased to 100).')

        import threading as _th
        _th.Thread(target=_worker, daemon=True).start()

    def _rebuild_world_section(self, labels):
        """Populate the sidebar World block once history exists."""
        holder = getattr(self, '_world_section', None)
        if holder is None:
            return
        for w in holder.winfo_children():
            w.destroy()
        for lbl in labels:
            var = self.check_vars.get(lbl)
            if var is None:
                var = tk.BooleanVar(value=False)
                self.check_vars[lbl] = var
            row = tk.Frame(holder, bg='#161b22')
            row.pack(fill='x', padx=(18, 6))
            tk.Label(row, text='\u25cf', fg=COLOR_MAP.get(lbl, '#8b949e'),
                     bg='#161b22', font=('Segoe UI', 7)).pack(side='left')
            tk.Checkbutton(row, text=lbl, variable=var,
                           command=self._redraw_chart,
                           bg='#161b22', fg='#cdd9e5', selectcolor='#161b22',
                           activebackground='#161b22', activeforeground='#00C9FF',
                           font=('Segoe UI', 8), relief='flat',
                           cursor='hand2', anchor='w').pack(side='left', fill='x')

    def _build_line_tab(self):
        # ── Chart mode controls ───────────────────────────────────────────────
        ctrl = tk.Frame(self.tab_line, bg='#0d1117')
        ctrl.pack(fill='x', padx=6, pady=(4, 0))

        tk.Label(ctrl, text='View:', bg='#0d1117', fg='#888',
                 font=('Segoe UI', 8, 'bold')).pack(side='left', padx=(0, 4))
        for mode, tip in [('Level',     'Index level (base 100)'),
                          ('Monthly %', 'Month-on-month % change'),
                          ('Rebased',   'Rebased to 100 at period start')]:
            tk.Radiobutton(ctrl, text=mode, variable=self.chart_mode_var,
                           value=mode, command=self._redraw_chart,
                           bg='#0d1117', fg='#cdd9e5', selectcolor='#161b22',
                           activebackground='#0d1117', activeforeground='#00C9FF',
                           font=('Segoe UI', 8), relief='flat', cursor='hand2',
                           indicatoron=False, padx=8, pady=2).pack(side='left', padx=1)

        tk.Frame(ctrl, bg='#21262d', width=1).pack(side='left', fill='y', padx=8, pady=2)

        tk.Checkbutton(ctrl, text='📈 Log Scale', variable=self.log_scale_var,
                       command=self._redraw_chart,
                       bg='#0d1117', fg='#cdd9e5', selectcolor='#0d1117',
                       activebackground='#0d1117', activeforeground='#FFD700',
                       font=('Segoe UI', 8), relief='flat',
                       cursor='hand2').pack(side='left')

        tk.Label(ctrl, text='  ·  Line pattern: Core ——  Y-Series ·····  '
                            'TR —·—·  Top ——  ——  Fund — — —',
                 bg='#0d1117', fg='#555',
                 font=('Segoe UI', 7)).pack(side='left', padx=(10, 0))

        tk.Label(self.tab_line, text='💡 Click anywhere on the chart to inspect a datapoint\'s date & value',
                 bg='#0d1117', fg='#666', font=('Segoe UI', 8)).pack(anchor='w', padx=4, pady=(2, 0))
        self.fig_line = Figure(figsize=(10, 5.5), facecolor='#0d1117')
        self.ax_line  = self.fig_line.add_subplot(111)
        self._style_ax(self.ax_line)
        self.canvas_line = FigureCanvasTkAgg(self.fig_line, self.tab_line)
        self.canvas_line.get_tk_widget().pack(fill='both', expand=True)
        tb = NavigationToolbar2Tk(self.canvas_line, self.tab_line)
        tb.config(background='#161b22')
        tb.update()

        # ── Datapoint inspector (Google-Finance style floating value box) ────
        # Click anywhere on the chart to show the nearest datapoint's date +
        # value, for every currently-plotted series.
        self._line_hover_series = {}   # label -> {series, color, is_fund}, refreshed each draw
        self._line_hover_artists = []  # crosshair line/dot/annotation, cleared each click
        self._ax_line_fund = None      # set when a fund twin-axis exists, cleared otherwise

        # Bind BOTH the matplotlib event AND a raw Tkinter click on the canvas
        # widget itself. matplotlib's mpl_connect can occasionally be a no-op
        # in some backend/Tk combinations; binding directly to the underlying
        # Tk widget guarantees the click is received, and we convert pixel
        # coordinates to data coordinates ourselves as the reliable path.
        self.canvas_line.mpl_connect('button_press_event', self._on_line_click)
        self.canvas_line.get_tk_widget().bind('<Button-1>', self._on_line_tk_click, add='+')

    def _on_line_tk_click(self, tk_event):
        """Tkinter-native fallback click handler — converts widget pixel
        coordinates to matplotlib data coordinates manually, guaranteed to
        fire regardless of matplotlib's own event-routing quirks."""
        try:
            widget = self.canvas_line.get_tk_widget()
            # Tk's y-origin is top-left; matplotlib's figure y-origin is
            # bottom-left, so flip y relative to the widget height.
            x_px = tk_event.x
            y_px = widget.winfo_height() - tk_event.y

            # Try every axes that currently has data — ax_line first, then
            # the fund twin-axis if present — and use whichever one's click
            # actually lands inside its data area.
            for ax_candidate in [self.ax_line, self._ax_line_fund]:
                if ax_candidate is None:
                    continue
                try:
                    inv = ax_candidate.transData.inverted()
                    xdata, ydata = inv.transform((x_px, y_px))
                    xlim = ax_candidate.get_xlim()
                    ylim = ax_candidate.get_ylim()
                    if xlim[0] <= xdata <= xlim[1] and min(ylim) <= ydata <= max(ylim):
                        self._show_line_inspector(xdata)
                        return
                except Exception:
                    continue
        except Exception:
            pass

    def _clear_line_hover(self):
        for artist in self._line_hover_artists:
            try: artist.remove()
            except Exception: pass
        self._line_hover_artists = []
        if hasattr(self, 'canvas_line'):
            self.canvas_line.draw_idle()

    def _on_line_click(self, event):
        if event.button != 1 or event.xdata is None:
            return
        if event.inaxes not in (self.ax_line, self._ax_line_fund):
            return
        self._show_line_inspector(event.xdata)

    def _show_line_inspector(self, xdata):
        """Core datapoint-matching + rendering logic, called from either the
        matplotlib event path or the Tkinter-native fallback path."""
        if not self._line_hover_series:
            return

        import matplotlib.dates as _mdates
        try:
            hover_dt = _mdates.num2date(xdata)
            if hover_dt.tzinfo is not None:
                hover_dt = hover_dt.replace(tzinfo=None)
        except Exception:
            return

        # Find the closest datapoint, across every plotted series, to the click's x position
        best = None  # (distance_seconds, label, date, value, color, is_fund)
        for lbl, info in self._line_hover_series.items():
            series = info['series']
            if series is None or series.empty:
                continue
            idx_arr = series.index
            try:
                pos = idx_arr.searchsorted(hover_dt)
            except Exception:
                continue
            candidates = [p for p in (pos - 1, pos) if 0 <= p < len(idx_arr)]
            for p in candidates:
                d = idx_arr[p]
                try:
                    d_py = d.to_pydatetime()
                    if d_py.tzinfo is not None:
                        d_py = d_py.replace(tzinfo=None)
                    dist = abs((d_py - hover_dt).total_seconds())
                except Exception:
                    continue
                if best is None or dist < best[0]:
                    best = (dist, lbl, d, series.iloc[p], info['color'], info['is_fund'])

        self._clear_line_hover()
        if best is None:
            return

        _, lbl, d, v, color, is_fund = best
        target_ax = self._ax_line_fund if (is_fund and getattr(self, '_ax_line_fund', None) is not None) else self.ax_line

        # Vertical dashed crosshair line at the matched date
        vline = self.ax_line.axvline(d, color='#888', linewidth=0.7, linestyle='--', zorder=3)
        self._line_hover_artists.append(vline)

        # Dot marker on the matched series at its actual value
        dot, = target_ax.plot([d], [v], 'o', color=color, markersize=7,
                              markeredgecolor='#0d1117', markeredgewidth=1, zorder=6)
        self._line_hover_artists.append(dot)

        # Floating box — date + value, Google-Finance style
        ccy_pfx   = FUND_META.get(lbl, {}).get('ccy', 'RM')
        value_txt = f'{ccy_pfx}{v:.4f}' if is_fund else f'{v:.2f}'
        date_txt  = d.strftime('%a, %d %b %Y')
        ann = target_ax.annotate(
            f'{value_txt}\n{lbl}\n{date_txt}',
            xy=(d, v), xytext=(12, 12), textcoords='offset points',
            fontsize=7.5, color='#0d1117', ha='left', va='bottom', zorder=7,
            bbox=dict(boxstyle='round,pad=0.4', fc='#cdd9e5', ec=color, lw=1.2, alpha=0.95),
        )
        self._line_hover_artists.append(ann)

        self.canvas_line.draw_idle()

    def _build_bar_tab(self):
        self.fig_bar = Figure(figsize=(10, 5.5), facecolor='#0d1117')
        self.ax_bar  = self.fig_bar.add_subplot(111)
        self._style_ax(self.ax_bar)
        self.canvas_bar = FigureCanvasTkAgg(self.fig_bar, self.tab_bar)
        self.canvas_bar.get_tk_widget().pack(fill='both', expand=True)

    def _build_table_tab(self):
        frame = tk.Frame(self.tab_table, bg='#0d1117')
        frame.pack(fill='both', expand=True)

        # Columns are rebuilt dynamically in _set_table_columns(), linked to
        # whichever indices/funds are checked on the right-side sidebar.
        self.data_tree = ttk.Treeview(frame, columns=['Date'], show='headings')
        self.data_tree.heading('Date', text='Date')
        self.data_tree.column('Date', width=90, anchor='w', stretch=False)

        vsb = ttk.Scrollbar(frame, orient='vertical',   command=self.data_tree.yview)
        hsb = ttk.Scrollbar(frame, orient='horizontal', command=self.data_tree.xview)
        self.data_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.data_tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    def _build_stock_tab(self):
        ctrl = tk.Frame(self.tab_stock, bg='#0d1117')
        ctrl.pack(fill='x', padx=10, pady=(8, 4))

        tk.Label(ctrl, text='Stock:', bg='#0d1117', fg='#cdd9e5',
                 font=('Segoe UI', 9)).pack(side='left')

        self.stock_symbol_var = tk.StringVar(value='')
        self.stock_cb = ttk.Combobox(ctrl, textvariable=self.stock_symbol_var,
                                     values=[], state='readonly', width=34)
        self.stock_cb.pack(side='left', padx=(4, 12))
        self.stock_cb.bind('<<ComboboxSelected>>', lambda e: self._draw_stock_performance())

        tk.Label(ctrl, text='Uses sidebar period: 1D / 5D / 1M / 3M / 6M / YTD / 1Y / 3Y / 5Y / All',
                 bg='#0d1117', fg='#888', font=('Segoe UI', 8)).pack(side='left')

        self.stock_summary_var = tk.StringVar(value='Select a stock after data loads.')
        tk.Label(self.tab_stock, textvariable=self.stock_summary_var,
                 bg='#101820', fg='#cdd9e5', anchor='w', padx=10, pady=5,
                 font=('Segoe UI', 9)).pack(fill='x', padx=10, pady=(0, 4))

        self.fig_stock = Figure(figsize=(10, 5.5), facecolor='#0d1117')
        self.ax_stock  = self.fig_stock.add_subplot(111)
        self._style_ax(self.ax_stock)
        self.canvas_stock = FigureCanvasTkAgg(self.fig_stock, self.tab_stock)
        self.canvas_stock.get_tk_widget().pack(fill='both', expand=True)

    @staticmethod
    def _style_ax(ax):
        ax.set_facecolor('#0d1117')
        ax.tick_params(colors='#888', labelsize=8)
        ax.spines['bottom'].set_color('#333')
        ax.spines['left'].set_color('#333')
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.yaxis.label.set_color('#888')
        ax.xaxis.label.set_color('#888')
        ax.grid(True, color='#1e2530', linewidth=0.5, linestyle='--')

    def _pick_file(self):
        path = filedialog.askopenfilename(
            title='Select portfolio CSV',
            filetypes=[('CSV files', '*.csv'), ('All files', '*.*')]
        )
        if path:
            self.input_file.set(path)
            self.status_var.set(f'File selected: {path}')

    def _show_loader(self):
        self._loader = tk.Toplevel(self)
        self._loader.title('')
        self._loader.configure(bg='#0d1117')
        self._loader.resizable(False, False)
        self._loader.grab_set()
        self._loader.protocol('WM_DELETE_WINDOW', lambda: None)

        self.update_idletasks()
        w, h = 460, 260
        x = self.winfo_x() + (self.winfo_width()  - w) // 2
        y = self.winfo_y() + (self.winfo_height() - h) // 2
        self._loader.geometry(f'{w}x{h}+{x}+{y}')

        tk.Label(self._loader, text='MYIPO Index Dashboard',
                 bg='#0d1117', fg='#00C9FF',
                 font=('Segoe UI', 13, 'bold')).pack(pady=(28, 4))
        tk.Label(self._loader, text='Building indices — please wait…',
                 bg='#0d1117', fg='#888', font=('Segoe UI', 9)).pack()

        self._spinner_chars = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏']
        self._spinner_idx   = 0
        self._spinner_var   = tk.StringVar(value='⠋')
        tk.Label(self._loader, textvariable=self._spinner_var,
                 bg='#0d1117', fg='#00C9FF', font=('Segoe UI', 22)).pack(pady=(10, 4))

        self._loader_stage_var = tk.StringVar(value='Initialising…')
        tk.Label(self._loader, textvariable=self._loader_stage_var,
                 bg='#0d1117', fg='#cdd9e5', font=('Segoe UI', 10, 'bold')).pack()

        self._loader_detail_var = tk.StringVar(value='')
        tk.Label(self._loader, textvariable=self._loader_detail_var,
                 bg='#0d1117', fg='#666', font=('Segoe UI', 8)).pack(pady=(2, 0))

        self._loader_progress = ttk.Progressbar(self._loader, mode='indeterminate', length=360)
        self._loader_progress.pack(pady=(16, 0))
        self._loader_progress.start(12)
        self._animate_spinner()

    def _animate_spinner(self):
        if not hasattr(self, '_loader') or not self._loader.winfo_exists():
            return
        self._spinner_var.set(self._spinner_chars[self._spinner_idx % len(self._spinner_chars)])
        self._spinner_idx += 1
        self._loader.after(80, self._animate_spinner)

    def _loader_update(self, msg):
        self.status_var.set(msg)
        msg_l = msg.strip()
        if msg_l.startswith('📊  Source: ONLINE'):
            self._data_source = 'online'
        elif msg_l.startswith('📊  Source: LOCAL'):
            self._data_source = 'local'
        if not hasattr(self, '_loader') or not self._loader.winfo_exists():
            return
        if 'chunk' in msg_l.lower() or 'tickers' in msg_l.lower():
            self._loader_stage_var.set('⬇  Downloading price data…')
            self._loader_detail_var.set(msg_l)
        elif 'TR dividends' in msg_l:
            self._loader_stage_var.set('⬇  Downloading dividend data (TR)…')
            self._loader_detail_var.set(msg_l)
        elif 'TR price matrix' in msg_l or 'TR-Series' in msg_l:
            self._loader_stage_var.set(msg_l[:55])
            self._loader_detail_var.set('')
        elif msg_l.startswith('Building') or 'Building' in msg_l:
            self._loader_stage_var.set(msg_l)
            self._loader_detail_var.set('')
        elif msg_l.startswith('✅') and 'Y20' in msg_l:
            self._loader_stage_var.set(msg_l[:55])
            self._loader_detail_var.set('')
        elif msg_l.startswith('✅'):
            self._loader_stage_var.set(msg_l)
            self._loader_detail_var.set('')
        elif msg_l.startswith('⚠️'):
            self._loader_detail_var.set(msg_l)
        else:
            self._loader_stage_var.set(msg_l)
            self._loader_detail_var.set('')

    def _hide_loader(self):
        if hasattr(self, '_loader') and self._loader.winfo_exists():
            self._loader_progress.stop()
            self._loader.grab_release()
            self._loader.destroy()

    def _run_refresh(self):
        path = self.input_file.get()
        if not os.path.exists(path):
            messagebox.showerror('File Not Found', f'Cannot find:\n{path}')
            return
        self.status_var.set('⏳  Loading data — please wait...')
        self.update_idletasks()
        self._show_loader()
        threading.Thread(target=self._load_thread, daemon=True).start()

    def _load_thread(self):
        try:
            result = build_indices(
                self.input_file.get(),
                log_cb=lambda m: self.after(0, lambda msg=m: self._loader_update(msg)),
                prefer_online=self.prefer_online.get(),
                apply_sell_fee=self.apply_sell_fee.get(),
                drip_distributions=self.drip_distributions.get(),
            )
            frames, df, wsnap, bmap, snames, future_ipos, stock_price_df, delist_candidates, *_rest = result
            self.all_frames        = frames
            self.output_df         = df
            self.weights_snapshot  = wsnap
            self.board_map         = bmap
            self.short_name_map    = snames
            self.future_ipos       = future_ipos
            self.stock_price_df    = stock_price_df
            self.delist_candidates = delist_candidates
            self.df_listed_all     = _rest[0] if _rest else pd.DataFrame()
            self.after(0, self._on_load_done)
        except Exception as exc:
            # ── FIX 4: capture exception correctly for Python 3.14 ──
            err_msg = str(exc)
            self.after(0, self._hide_loader)
            self.after(0, lambda msg=err_msg: messagebox.showerror('Error', msg))
            self.after(0, lambda msg=err_msg: self.status_var.set(f'❌  Error: {msg}'))

    def _on_load_done(self):
        self._hide_loader()
        src = getattr(self, '_data_source', 'local')
        src_icon = '☁️' if src == 'online' else '📂'
        if hasattr(self, '_source_label_var'):
            self._source_label_var.set(f'source: {src_icon} {src}')
        self.status_var.set(
            f'✅  Data loaded ({src_icon} {src}) — {len(self.all_frames)} indices built. '            f' Last update: {datetime.now().strftime("%H:%M:%S")}'        )
        self._populate_table()
        self._update_future_header()
        self._refresh_stock_list()
        self._draw_composition()
        self._refresh_fund_events_dropdown()
        self._draw_fund_events()
        self._draw_fund_ledger()
        self._redraw_chart()

    def _export_csv(self):
        if self.output_df is None:
            messagebox.showwarning('No Data', 'Please refresh data first.')
            return
        path = filedialog.asksaveasfilename(
            defaultextension='.csv',
            initialfile='MYIPO_Index_History.csv',
            filetypes=[('CSV', '*.csv')]
        )
        if path:
            out = self.output_df.copy()
            out.index = out.index.strftime('%d/%m/%Y')
            out.to_csv(path)
            self.status_var.set(f'💾  Exported to {path}')

    def _now_myt(self):
        return datetime.now(MALAYSIA_TZ) if MALAYSIA_TZ else datetime.now()

    def _ipo_event_dt(self, date_value, hour, minute=0):
        if pd.isna(date_value):
            return None
        base = pd.Timestamp(date_value).date()
        return datetime.combine(base, time(hour, minute), tzinfo=MALAYSIA_TZ)

    @staticmethod
    def _fmt_countdown(delta):
        total = max(0, int(delta.total_seconds()))
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        if days:
            return f'{days}d {hours:02d}h {mins:02d}m {secs:02d}s'
        return f'{hours:02d}h {mins:02d}m {secs:02d}s'

    def _tick_future_header(self):
        self._future_tick_count = getattr(self, '_future_tick_count', 0) + 1
        if self._future_tick_count % 10 == 0:
            self._future_item_idx = getattr(self, '_future_item_idx', 0) + 1
        self._update_future_header()
        self.after(1000, self._tick_future_header)

    def _update_future_header(self):
        if not hasattr(self, 'future_header_var'):
            return

        show_ipo    = getattr(self, '_show_ipo_var',    None)
        show_delist = getattr(self, '_show_delist_var', None)
        ipo_on    = show_ipo.get()    if show_ipo    else True
        delist_on = show_delist.get() if show_delist else True

        now  = self._now_myt()
        rows = []
        signatures = []

        if ipo_on and self.future_ipos is not None and not self.future_ipos.empty:
            _n_future = len(self.future_ipos)
            _n_shown  = 0
            for _, r in self.future_ipos.iterrows():
                trade_dt = r.get('Trade Date')
                d1_dt    = r.get('D-1')
                include_at     = self._ipo_event_dt(d1_dt,    17, 0)
                first_trade_at = self._ipo_event_dt(trade_dt,  9, 0)

                if include_at is not None and now < include_at:
                    stage     = 'To D-1 inclusion'
                    countdown = self._fmt_countdown(include_at - now)
                elif first_trade_at is not None and now < first_trade_at:
                    stage     = 'To D0 first trade'
                    countdown = self._fmt_countdown(first_trade_at - now)
                else:
                    # Log why this IPO was skipped so discrepancies are visible
                    sym = str(r.get('Symbol', '')).strip()
                    if hasattr(self, 'status_var'):
                        pass  # too noisy for status bar
                    continue

                trade_txt = trade_dt.strftime('%d %b %Y') if hasattr(trade_dt, 'strftime') and pd.notna(trade_dt) else 'TBA'
                d1_txt    = d1_dt.strftime('%d %b %Y')    if hasattr(d1_dt,    'strftime') and pd.notna(d1_dt)    else 'TBA'
                raw_sym = str(r.get('Symbol', '')).strip()
                sym     = raw_sym if raw_sym.endswith('.KL') else (raw_sym + '.KL' if raw_sym else '')
                name    = str(r.get('Name', '')).strip() or raw_sym.replace('.KL', '')
                board   = str(r.get('Board', '')).strip().upper()
                px      = r.get('Purchase Price')
                px_txt  = f'RM{px:.2f}' if pd.notna(px) else 'RM—'

                inclusions = ['MYIPO+']
                if pd.notna(r.get('Quant2')) and r.get('Quant2', 0) > 0:
                    inclusions.append('Momentum')
                if board == 'ACE':
                    inclusions.append('Ace')
                elif board == 'MAIN':
                    inclusions.append('Main')
                inclusions.append('Dynamic365')
                if pd.notna(trade_dt):
                    inclusions.append(f'Y{trade_dt.year}')

                headline = (
                    f'🆕 IPO  {sym}({name}) | D-1:{d1_txt} 17:00 MYT / D0:{trade_txt} 09:00 MYT | '
                    f'Incl:{"/".join(inclusions)} | {stage}: {countdown} | {board} | {px_txt}'
                )
                rows.append(headline)
                signatures.append(f'IPO|{sym}|{d1_txt}|{trade_txt}')
                if len(rows) >= 12:
                    break

        if delist_on and hasattr(self, 'delist_candidates') and self.delist_candidates is not None and not self.delist_candidates.empty:
            for _, r in self.delist_candidates.iterrows():
                d365_dt  = r.get('D+365')
                if pd.isna(d365_dt):
                    continue
                delist_at = self._ipo_event_dt(d365_dt, 17, 0)
                if delist_at is None:
                    continue

                raw_sym  = str(r.get('Symbol', '')).strip()
                sym      = raw_sym if raw_sym.endswith('.KL') else (raw_sym + '.KL' if raw_sym else '')
                name     = str(r.get('Name', '')).strip() or raw_sym.replace('.KL', '')
                board    = str(r.get('Board', '')).strip().upper()
                d365_txt = pd.Timestamp(d365_dt).strftime('%d %b %Y')

                if now < delist_at:
                    stage     = 'To D365 delist'
                    countdown = self._fmt_countdown(delist_at - now)
                else:
                    elapsed   = self._fmt_countdown(now - delist_at)
                    stage     = 'Delisted'
                    countdown = f'{elapsed} ago'

                headline = (
                    f'🔴 D365  {sym}({name}) | D+365:{d365_txt} 17:00 MYT | '
                    f'Dynamic365 {stage}: {countdown} | {board}'
                )
                rows.append(headline)
                signatures.append(f'D365|{sym}|{d365_txt}')
                if len(rows) >= 20:
                    break

        if not rows:
            if not ipo_on and not delist_on:
                self.future_header_var.set('— info bar hidden (both IPO and Delist are off) —')
            elif not ipo_on:
                self.future_header_var.set('🔴 Delist: no upcoming D+365 events in window.')
            elif not delist_on:
                self.future_header_var.set('🆕 IPO: no upcoming D-1/D0 events in database.')
            else:
                self.future_header_var.set('No upcoming IPO or D365 delist events in database.')
            return

        signature = '||'.join(signatures)
        if signature != getattr(self, '_future_item_signature', ''):
            self._future_item_signature = signature
            self._future_item_idx       = 0
            self._future_tick_count     = 0

        idx = getattr(self, '_future_item_idx', 0) % len(rows)
        self.future_header_var.set(f'Event {idx + 1}/{len(rows)}: {rows[idx]}')

    def _get_period_mask(self, index):
        p   = self.period_var.get()
        end = index[-1]
        if p == '1D':
            start = index[-2] if len(index) >= 2 else index[0]
        elif p == '5D':
            start = index[-6] if len(index) >= 6 else index[0]
        elif p == '1M':  start = end - pd.DateOffset(months=1)
        elif p == '3M':  start = end - pd.DateOffset(months=3)
        elif p == '6M':  start = end - pd.DateOffset(months=6)
        elif p == 'YTD': start = pd.Timestamp(end.year, 1, 1)
        elif p == '1Y':  start = end - pd.DateOffset(years=1)
        elif p == '3Y':  start = end - pd.DateOffset(years=3)
        elif p == '5Y':  start = end - pd.DateOffset(years=5)
        else:            start = index[0]
        return (index >= start) & (index <= end)

    def _selected_labels(self):
        return [lbl for lbl in ALL_INDEX_LABELS if self.check_vars[lbl].get() and lbl in self.all_frames]

    def _redraw_chart(self):
        if not self.all_frames:
            return
        tab = self.notebook.index(self.notebook.select())
        if tab == 0:   self._draw_line()
        elif tab == 1: self._draw_bar()
        elif tab == 2: self._populate_table()
        elif tab == 3: self._draw_stock_performance()
        elif tab == 4: self._draw_composition()
        elif tab == 5: self._draw_fund_events()
        elif tab == 6: self._draw_fund_ledger()
        elif tab == 7:
            self._build_pts_tab()
        elif tab == 8:
            self._build_zdws_tab()
        elif tab == 9:
            self._build_zdos_tab()
        self._update_stats()

    def _refresh_stock_list(self):
        if not hasattr(self, 'stock_cb'):
            return
        if self.stock_price_df is None or self.stock_price_df.empty:
            self.stock_cb['values'] = []
            self.stock_summary_var.set('No stock price data loaded.')
            return

        symbols = [c for c in self.stock_price_df.columns if self.stock_price_df[c].dropna().shape[0] >= 2]
        display = []
        for sym in symbols:
            name      = self.short_name_map.get(sym, sym)
            clean_sym = sym.replace('.KL', '')
            display.append(f'{clean_sym} — {name}')

        self._stock_display_to_symbol = dict(zip(display, symbols))
        self.stock_cb['values'] = display
        if display and not self.stock_symbol_var.get():
            self.stock_symbol_var.set(display[0])
        self._draw_stock_performance()

    @staticmethod
    def _pick_annotation_indices(n_points, max_labels=12):
        if n_points <= max_labels:
            return list(range(n_points))
        step = max(1, n_points // max_labels)
        indices = list(range(0, n_points, step))
        if (n_points - 1) not in indices:
            indices.append(n_points - 1)
        return indices

    def _draw_stock_performance(self):
        if not hasattr(self, 'ax_stock'):
            return

        ax = self.ax_stock
        ax.cla()
        self._style_ax(ax)

        if self.stock_price_df is None or self.stock_price_df.empty:
            ax.set_title('No stock price data loaded', color='#888')
            self.canvas_stock.draw()
            return

        display = self.stock_symbol_var.get()
        sym     = getattr(self, '_stock_display_to_symbol', {}).get(display, display)
        if not sym or sym not in self.stock_price_df.columns:
            ax.set_title('Select a stock', color='#888')
            self.canvas_stock.draw()
            return

        prices = self.stock_price_df[sym].dropna()
        if len(prices) < 2:
            ax.set_title(f'{sym} has insufficient price data', color='#888')
            self.canvas_stock.draw()
            return

        mask = self._get_period_mask(prices.index)
        s    = prices[mask].dropna()
        if len(s) < 2:
            s = prices.tail(2)

        ret     = (s.iloc[-1] / s.iloc[0] - 1) * 100
        chg_abs = s.iloc[-1] - s.iloc[0]
        last    = s.iloc[-1]
        period  = self.period_var.get()
        name    = self.short_name_map.get(sym, sym)

        ax.plot(s.index, s.values, color='#00C9FF', linewidth=1.8, label='Adjusted Close Price')

        if self.show_datapoints_var.get():
            idxs = self._pick_annotation_indices(len(s))
            dates  = s.index[idxs]
            values = s.values[idxs]
            ax.scatter(dates, values, color='#00C9FF', s=18, zorder=5)
            for d, v in zip(dates, values):
                ax.annotate(
                    f'{v:.3f}',
                    xy=(d, v), xytext=(0, 6), textcoords='offset points',
                    ha='center', va='bottom', fontsize=6, color='#cdd9e5',
                    bbox=dict(boxstyle='round,pad=0.15', fc='#161b22', ec='none', alpha=0.7)
                )

        ax.set_title(f'{sym.replace(".KL", "")} — {name}  [{period}]',
                     color='#cdd9e5', fontsize=11, pad=10)
        ccy_pfx = self._price_ccy_prefix(sym)
        ax.set_ylabel(f'Price ({ccy_pfx.strip()})', color='#888')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d\n%Y'))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        self.fig_stock.autofmt_xdate(rotation=30, ha='right')
        ax.legend(fontsize=7, loc='upper left',
                  facecolor='#161b22', edgecolor='#333',
                  labelcolor='#cdd9e5', framealpha=0.8)

        start_txt = s.index[0].strftime('%d %b %Y')
        end_txt   = s.index[-1].strftime('%d %b %Y')
        self.stock_summary_var.set(
            f'{sym.replace(".KL", "")} | {period} | {start_txt} → {end_txt} | '
            f'Price: {ccy_pfx}{last:.3f} | Change: {ccy_pfx}{chg_abs:+.3f} | Return: {ret:+.2f}%'
        )

        self.fig_stock.tight_layout()
        self.canvas_stock.draw()

    def _draw_line(self):
        ax = self.ax_line
        ax.cla()
        self._style_ax(ax)
        # Remove any stale twin axis from a previous draw
        if hasattr(self, '_ax_line_fund') and self._ax_line_fund is not None:
            try: self._ax_line_fund.remove()
            except Exception: pass
            self._ax_line_fund = None

        # Reset hover lookup table — repopulated below as each series is drawn
        self._line_hover_series = {}
        self._clear_line_hover()

        selected = self._selected_labels()
        period   = self.period_var.get()

        if not selected:
            ax.set_title('No indices selected', color='#888')
            self.canvas_line.draw()
            return

        # Funds are priced in RM (NAV per unit, launched at RM0.2500) — an
        # entirely different scale to the base-100 indices. Split them out
        # and plot funds on a secondary right-hand axis, dashed, in RM.
        # VFunds are NAV-denominated (RM0.2500 / USD1.0000 at par), not
        # base-100 index levels — so they belong on the fund axis. Left on the
        # index axis they'd be flat lines pinned to the floor.
        _nav_labels  = set(FUND_LABELS) | set(VFUND_LABELS)
        index_labels = [lbl for lbl in selected if lbl not in _nav_labels]
        fund_labels  = [lbl for lbl in selected if lbl in _nav_labels]

        mode      = self.chart_mode_var.get()
        use_log   = self.log_scale_var.get()

        def _transform(s):
            """Apply the active chart mode to a series."""
            if mode == 'Monthly %':
                # Month-end resample → month-on-month % change
                m = s.resample('ME').last()
                return (m.pct_change() * 100).dropna()
            if mode == 'Rebased':
                # Rebase to 100 at the first point of the visible period
                base = s.iloc[0]
                return (s / base * 100) if base else s
            return s

        any_index_plotted = False
        for lbl in index_labels:
            series = self.all_frames[lbl][lbl].dropna()
            if series.empty: continue
            mask = self._get_period_mask(series.index)
            s    = series[mask]
            if s.empty: continue
            s = _transform(s)
            if s.empty: continue
            ls = LINE_STYLE_MAP.get(lbl, '-')
            if mode == 'Monthly %':
                # Bar-like step plot reads better for discrete monthly ratios
                ax.plot(s.index, s.values, label=lbl, color=COLOR_MAP[lbl],
                        linewidth=1.4, linestyle=ls, marker='o', markersize=3,
                        drawstyle='steps-mid')
            else:
                ax.plot(s.index, s.values, label=lbl,
                        color=COLOR_MAP[lbl], linewidth=1.5, linestyle=ls)
            any_index_plotted = True
            self._line_hover_series[lbl] = {'series': s, 'color': COLOR_MAP[lbl], 'is_fund': False}

            if self.show_datapoints_var.get():
                idxs   = self._pick_annotation_indices(len(s))
                dates  = s.index[idxs]
                values = s.values[idxs]
                ax.scatter(dates, values, color=COLOR_MAP[lbl], s=14, zorder=5)
                for d, v in zip(dates, values):
                    ax.annotate(
                        f'{v:+.1f}%' if mode == 'Monthly %' else f'{v:.1f}',
                        xy=(d, v), xytext=(0, 5), textcoords='offset points',
                        ha='center', va='bottom', fontsize=5.5, color='#cdd9e5',
                        bbox=dict(boxstyle='round,pad=0.12', fc='#161b22', ec='none', alpha=0.65)
                    )

        if any_index_plotted:
            if mode == 'Monthly %':
                ax.axhline(0, color='#555', linewidth=1.0, linestyle='-')
                ax.set_ylabel('Monthly Change (%)', color='#888')
                ax.set_title('Month-on-Month % Change', color='#cdd9e5', fontsize=10)
            elif mode == 'Rebased':
                ax.axhline(100, color='#333', linewidth=0.8, linestyle='--')
                ax.set_ylabel('Rebased to 100 at period start', color='#888')
            else:
                ax.axhline(100, color='#333', linewidth=0.8, linestyle='--')
                ax.set_ylabel('Index Value (Base 100)', color='#888')
            # Log scale — only valid for positive-value modes
            if use_log and mode != 'Monthly %':
                try:
                    ax.set_yscale('log')
                    ax.set_ylabel(ax.get_ylabel() + '  [log]', color='#888')
                except Exception:
                    pass

        # ── Fund NAVs on a secondary right-hand axis (RM scale) ──────────────
        ax_fund = None
        if fund_labels:
            ax_fund = ax.twinx()
            self._ax_line_fund = ax_fund
            ax_fund.set_facecolor('none')
            ax_fund.tick_params(colors='#00E5A0', labelsize=8)
            ax_fund.spines['right'].set_color('#00E5A0')
            ax_fund.spines['top'].set_visible(False)
            ax_fund.spines['bottom'].set_visible(False)
            ax_fund.spines['left'].set_visible(False)
            ax_fund.yaxis.label.set_color('#00E5A0')

            any_fund_plotted = False
            _fund_pars_seen = {}   # (ccy, par) -> True, for the reference line(s) below
            for lbl in fund_labels:
                series = self.all_frames[lbl][lbl].dropna()   # NAV per unit, native ccy
                if series.empty: continue
                mask = self._get_period_mask(series.index)
                s    = series[mask]
                if s.empty: continue
                s = _transform(s)
                if s.empty: continue
                ls = LINE_STYLE_MAP.get(lbl, '--')
                fmeta   = FUND_META.get(lbl, {'ccy': 'RM', 'par': FUND_PAR_NAV})
                fund_ccy = {'RM': 'MYR', '$': 'USD'}.get(fmeta['ccy'], fmeta['ccy'])
                _fund_pars_seen[(fund_ccy, fmeta['par'])] = True
                unit_lbl = '(MoM %)' if mode == 'Monthly %' else \
                           '(rebased)' if mode == 'Rebased' else f'({fund_ccy} NAV)'
                if mode == 'Monthly %':
                    ax_fund.plot(s.index, s.values, label=f'{lbl}  {unit_lbl}',
                                 color=COLOR_MAP[lbl], linewidth=1.4, linestyle=ls,
                                 marker='D', markersize=3, drawstyle='steps-mid')
                else:
                    ax_fund.plot(s.index, s.values, label=f'{lbl}  {unit_lbl}',
                                 color=COLOR_MAP[lbl], linewidth=1.5, linestyle=ls)
                any_fund_plotted = True
                self._line_hover_series[lbl] = {'series': s, 'color': COLOR_MAP[lbl], 'is_fund': True}

                if self.show_datapoints_var.get():
                    idxs   = self._pick_annotation_indices(len(s))
                    dates  = s.index[idxs]
                    values = s.values[idxs]
                    ax_fund.scatter(dates, values, color=COLOR_MAP[lbl], s=14,
                                    zorder=5, marker='D')
                    for d, v in zip(dates, values):
                        ax_fund.annotate(
                            f'{v:.4f}',
                            xy=(d, v), xytext=(0, -10), textcoords='offset points',
                            ha='center', va='top', fontsize=5.5, color='#00E5A0',
                            bbox=dict(boxstyle='round,pad=0.12', fc='#161b22', ec='none', alpha=0.65)
                        )

            if any_fund_plotted:
                if mode == 'Monthly %':
                    ax_fund.axhline(0, color='#00E5A0', linewidth=0.8, linestyle=':')
                    ax_fund.set_ylabel('Fund MoM Change (%)', color='#00E5A0')
                elif mode == 'Rebased':
                    ax_fund.axhline(100, color='#00E5A0', linewidth=0.8, linestyle=':')
                    ax_fund.set_ylabel('Fund (rebased to 100)', color='#00E5A0')
                else:
                    # One faint par-reference line per distinct (ccy, par) among
                    # the funds actually plotted — a single global RM0.2500
                    # line was meaningless once a USD fund (par 1.0000) shares
                    # this axis.
                    for _ccy, _par in _fund_pars_seen:
                        ax_fund.axhline(_par, color='#00E5A0', linewidth=0.8, linestyle=':')
                    ccys_plotted = {c for c, _ in _fund_pars_seen}
                    if len(ccys_plotted) == 1:
                        ax_fund.set_ylabel(f'Fund NAV ({next(iter(ccys_plotted))} per unit)',
                                           color='#00E5A0')
                    else:
                        ax_fund.set_ylabel('Fund NAV (native currency per unit)', color='#00E5A0')
                    if use_log:
                        try: ax_fund.set_yscale('log')
                        except Exception: pass

        # ── Bound the x-axis to what's actually plotted ─────────────────────────
        # Relying on implicit autoscale left the chart stretched to the full
        # 'All'-period calendar (back to the oldest Core index date) even when
        # the only selected series was a young VFund CEF — mostly blank chart
        # before its inception. Explicitly following the plotted data's own
        # date span fixes that for any young series, not just VFund.
        _all_plotted_idx = [info['series'].index for info in self._line_hover_series.values()
                            if not info['series'].empty]
        if _all_plotted_idx:
            _xmin = min(idx.min() for idx in _all_plotted_idx)
            _xmax = max(idx.max() for idx in _all_plotted_idx)
            if _xmin < _xmax:
                _pad = (_xmax - _xmin) * 0.02
                ax.set_xlim(_xmin - _pad, _xmax + _pad)

        # ── Title reflects what's actually shown ──────────────────────────────
        mode_tag = {'Monthly %': 'Month-on-Month %',
                    'Rebased':   'Rebased to 100',
                    'Level':     'Cumulative (Base 100)'}.get(mode, mode)
        log_tag  = '  [log]' if (use_log and mode != 'Monthly %') else ''
        if index_labels and fund_labels:
            title = f'MYIPO Index vs Fund — {mode_tag}  [{period}]{log_tag}'
        elif fund_labels:
            title = f'MYIPO+ Fund Series — {mode_tag}  [{period}]{log_tag}'
        else:
            title = f'MYIPO Index — {mode_tag}  [{period}]{log_tag}'
        ax.set_title(title, color='#cdd9e5', fontsize=11, pad=10)

        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %y'))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        self.fig_line.autofmt_xdate(rotation=30, ha='right')

        # Combined legend across both axes
        handles1, labels1 = ax.get_legend_handles_labels()
        if ax_fund is not None:
            handles2, labels2 = ax_fund.get_legend_handles_labels()
            handles1 += handles2; labels1 += labels2
        ax.legend(handles1, labels1, fontsize=7, loc='upper left',
                  facecolor='#161b22', edgecolor='#333',
                  labelcolor='#cdd9e5', framealpha=0.8)

        self.fig_line.tight_layout()
        self.canvas_line.draw()

    def _draw_bar(self):
        ax = self.ax_bar
        ax.cla()
        self._style_ax(ax)

        selected = self._selected_labels()
        if not selected:
            ax.set_title('No indices selected', color='#888')
            self.canvas_bar.draw()
            return

        labels, returns, colors = [], [], []
        for lbl in selected:
            is_fund = lbl in FUND_LABELS
            if is_fund:
                # Total Return = NAV + cumulative distributions paid per unit.
                # Comparing NAV-only return against a price-only index always
                # understates the fund, since the fund pays real cash
                # distributions the index has no concept of. Total Return is
                # the fair, standard convention for this comparison.
                tr_col = f'{lbl} TotalReturn'
                series = self.all_frames[lbl].get(tr_col, pd.Series(dtype=float)).dropna()
                if series.empty:
                    series = self.all_frames[lbl][lbl].dropna()   # fallback if column missing
            else:
                series = self.all_frames[lbl][lbl].dropna()
            if series.empty: continue
            mask = self._get_period_mask(series.index)
            s    = series[mask]
            if len(s) < 2: continue
            ret = (s.iloc[-1] / s.iloc[0] - 1) * 100
            labels.append(lbl.replace('MYIPO+ ', '').replace('MYIPO+', 'All'))
            returns.append(ret)
            colors.append(COLOR_MAP[lbl])

        if not labels:
            self.canvas_bar.draw()
            return

        x    = range(len(labels))
        bars = ax.bar(x, returns, color=colors, width=0.6, edgecolor='none')
        ax.axhline(0, color='#555', linewidth=0.8)

        for bar, val in zip(bars, returns):
            label_y  = bar.get_height() + (0.5 if val >= 0 else -1.5)
            va_align = 'bottom' if val >= 0 else 'top'
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                label_y,
                f'{val:+.1f}%',
                ha='center', va=va_align,
                color='#cdd9e5', fontsize=7,
                bbox=dict(boxstyle='round,pad=0.15', fc='#161b22', ec='none', alpha=0.7)
            )

        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=30, ha='right', fontsize=7)
        any_fund_shown = any(lbl in FUND_LABELS for lbl in selected)
        title = f'Total Return % — {self.period_var.get()}'
        if any_fund_shown:
            title += '  (Fund: NAV + distributions)'
        ax.set_title(title, color='#cdd9e5', fontsize=11, pad=10)
        ax.set_ylabel('Return (%)', color='#888')
        self.fig_bar.tight_layout()
        self.canvas_bar.draw()

    def _build_comp_tab(self):
        ctrl = tk.Frame(self.tab_comp, bg='#0d1117')
        ctrl.pack(fill='x', padx=10, pady=(8, 4))

        tk.Label(ctrl, text='Index:', bg='#0d1117', fg='#cdd9e5',
                 font=('Segoe UI', 9)).pack(side='left')

        self.comp_index_var = tk.StringVar(value=ALL_INDEX_LABELS[0])
        self.comp_index_cb  = ttk.Combobox(ctrl, textvariable=self.comp_index_var,
                                            values=ALL_INDEX_LABELS, state='readonly', width=22)
        self.comp_index_cb.pack(side='left', padx=(4, 16))
        self.comp_index_cb.bind('<<ComboboxSelected>>', lambda e: self._draw_composition())

        tk.Label(ctrl, text='Top N:', bg='#0d1117', fg='#cdd9e5',
                 font=('Segoe UI', 9)).pack(side='left')
        self.comp_topn_var = tk.StringVar(value='20')
        for n in ['10', '20', '50', 'All']:
            tk.Radiobutton(
                ctrl, text=n, variable=self.comp_topn_var, value=n,
                command=self._draw_composition,
                bg='#0d1117', fg='#cdd9e5', selectcolor='#00C9FF',
                activebackground='#0d1117', font=('Segoe UI', 9),
                indicatoron=False, relief='flat', padx=8, pady=2, cursor='hand2'
            ).pack(side='left', padx=2)

        body = tk.Frame(self.tab_comp, bg='#0d1117')
        body.pack(fill='both', expand=True, padx=10, pady=(0, 6))

        left = tk.Frame(body, bg='#161b22', width=420)
        left.pack(side='left', fill='y', padx=(0, 8))
        left.pack_propagate(False)

        tk.Label(left, text='Constituent Weights', bg='#161b22', fg='#cdd9e5',
                 font=('Segoe UI', 10, 'bold')).pack(anchor='w', padx=8, pady=(8, 2))

        cols = ('#', 'Symbol', 'Name', 'Board', 'Weight %', 'Cum %')
        self.comp_tree = ttk.Treeview(left, columns=cols, show='headings')
        for col, w in [('#', 28), ('Symbol', 68), ('Name', 130), ('Board', 46), ('Weight %', 62), ('Cum %', 52)]:
            self.comp_tree.heading(col, text=col)
            self.comp_tree.column(col, width=w,
                                  anchor='e' if col in ('Weight %', 'Cum %') else 'w',
                                  stretch=False)
        vsb = ttk.Scrollbar(left, orient='vertical', command=self.comp_tree.yview)
        self.comp_tree.configure(yscrollcommand=vsb.set)
        self.comp_tree.pack(side='left', fill='both', expand=True, padx=(6, 0), pady=(0, 6))
        vsb.pack(side='left', fill='y', pady=(0, 6))

        self.comp_tree.tag_configure('MAIN',  foreground='#00C9FF')
        self.comp_tree.tag_configure('ACE',   foreground='#FFD93D')
        self.comp_tree.tag_configure('OTHER', foreground='#aaa')
        # WC Inspector — double-click a constituent for weight-change history
        self.comp_tree.bind('<Double-1>', self._open_wc_inspector)

        self.comp_summary_var = tk.StringVar(value='')
        tk.Label(left, textvariable=self.comp_summary_var, bg='#161b22', fg='#888',
                 font=('Segoe UI', 8), wraplength=280, justify='left').pack(anchor='w', padx=8, pady=(0, 6))

        right = tk.Frame(body, bg='#0d1117')
        right.pack(side='left', fill='both', expand=True)

        self.fig_donut = Figure(figsize=(7, 3.2), facecolor='#0d1117')
        self.ax_donut  = self.fig_donut.add_subplot(121)
        self.ax_board  = self.fig_donut.add_subplot(122)
        self.canvas_donut = FigureCanvasTkAgg(self.fig_donut, right)
        self.canvas_donut.get_tk_widget().pack(fill='both', expand=True)

        self.fig_wbar = Figure(figsize=(7, 3.0), facecolor='#0d1117')
        self.ax_wbar  = self.fig_wbar.add_subplot(111)
        self.canvas_wbar = FigureCanvasTkAgg(self.fig_wbar, right)
        self.canvas_wbar.get_tk_widget().pack(fill='both', expand=True)

    def _open_wc_inspector(self, event=None):
        """WC Inspector — weight-change history for the double-clicked constituent.
        Shows per-rebalance membership, weight, price, and score across all
        Top-N series the symbol has appeared in."""
        sel = self.comp_tree.selection()
        if not sel:
            return
        vals = self.comp_tree.item(sel[0], 'values')
        if not vals or len(vals) < 2:
            return
        # Columns: ('#', 'Symbol', 'Name', 'Board', 'Weight %', 'Cum %')
        # vals[1] is already the full symbol (e.g. '0282.KL') as stored in history
        sym       = str(vals[1]).strip()
        sym_short = sym.replace('.KL', '')
        name      = self.short_name_map.get(sym, sym_short)

        win = tk.Toplevel(self)
        win.title(f'WC Inspector — {sym_short}')
        win.configure(bg='#0d1117')
        win.geometry('720x600')
        win.transient(self)

        tk.Label(win, text=f'🔍  WC INSPECTOR — {sym_short}',
                 font=('Segoe UI', 13, 'bold'), fg='#00E5A0', bg='#0d1117').pack(pady=(14, 0))
        tk.Label(win, text=name, font=('Segoe UI', 9), fg='#8b949e',
                 bg='#0d1117').pack(pady=(0, 10))

        nb = ttk.Notebook(win)
        nb.pack(fill='both', expand=True, padx=12, pady=(0, 12))

        found_any = False
        for n in (10, 20, 50, 100):
            hist = _TOP_SERIES_HISTORY.get(n, [])
            if not hist:
                continue
            # Records where this symbol appears or transitions
            records = []
            for rec in hist:
                in_now  = sym in rec['constituents']
                was_in  = sym in rec.get('in', [])
                was_out = sym in rec.get('out', [])
                if in_now or was_in or was_out:
                    records.append((rec, in_now, was_in, was_out))
            if not records:
                continue
            found_any = True

            tab = tk.Frame(nb, bg='#0d1117')
            nb.add(tab, text=f'Top {n}')

            cols = ('Date', 'Status', 'Weight %', 'Price (RM)', 'Score')
            tree = ttk.Treeview(tab, columns=cols, show='headings', height=18)
            cw   = {'Date': 100, 'Status': 90, 'Weight %': 80,
                    'Price (RM)': 90, 'Score': 90}
            for col in cols:
                tree.heading(col, text=col)
                tree.column(col, width=cw.get(col, 80), anchor='center')
            vsb = ttk.Scrollbar(tab, orient='vertical', command=tree.yview)
            tree.configure(yscrollcommand=vsb.set)
            tree.pack(side='left', fill='both', expand=True, padx=(8, 0), pady=8)
            vsb.pack(side='left', fill='y', pady=8)
            tree.tag_configure('in',   foreground='#3FB950')
            tree.tag_configure('out',  foreground='#F85149')
            tree.tag_configure('hold', foreground='#cdd9e5')

            n_periods_in = 0
            for rec, in_now, was_in, was_out in reversed(records):
                date_str = pd.Timestamp(rec['date']).strftime('%d %b %Y')
                if was_in:
                    status, tag = '➕ ENTERED', 'in'
                elif was_out:
                    status, tag = '➖ EXITED', 'out'
                elif in_now:
                    status, tag = '● HELD', 'hold'
                else:
                    continue
                w  = rec['weights'].get(sym)
                px = rec['prices'].get(sym)
                sc = rec['scores'].get(sym)
                if in_now:
                    n_periods_in += 1
                tree.insert('', 'end', tags=(tag,), values=(
                    date_str, status,
                    f'{w*100:.2f}%' if w else '—',
                    f'{px:.4f}'     if px else '—',
                    f'{sc:,.2f}'    if sc else '—'))

            tk.Label(tab, text=f'{n_periods_in} rebalance period(s) as member',
                     font=('Segoe UI', 8), fg='#8b949e', bg='#0d1117').pack(pady=(0, 6))

        if not found_any:
            tk.Label(win, text=f'{sym_short} has never appeared in any Top series.',
                     font=('Segoe UI', 10), fg='#8b949e', bg='#0d1117').pack(pady=30)

    def _draw_composition(self):
        if not self.weights_snapshot:
            return

        lbl    = self.comp_index_var.get()
        if lbl not in self.weights_snapshot:
            return

        raw    = self.weights_snapshot[lbl]
        topn_s = self.comp_topn_var.get()
        topn   = len(raw) if topn_s == 'All' else int(topn_s)
        topn   = min(topn, len(raw))

        top    = raw.iloc[:topn]
        rest_w = raw.iloc[topn:].sum()
        total  = raw.sum()

        for row in self.comp_tree.get_children():
            self.comp_tree.delete(row)

        cum = 0.0
        for rank, (sym, w) in enumerate(top.items(), 1):
            pct   = w / total * 100
            cum  += pct
            board = self.board_map.get(sym, '—')
            name  = self.short_name_map.get(sym, sym)
            name  = name[:22] + '…' if len(name) > 22 else name
            tag   = board if board in ('MAIN', 'ACE') else 'OTHER'
            self.comp_tree.insert('', 'end',
                values=(rank, sym, name, board, f'{pct:.2f}%', f'{cum:.1f}%'),
                tags=(tag,))

        top_pct  = top.sum()  / total * 100
        rest_pct = rest_w     / total * 100
        n_total  = len(raw)
        self.comp_summary_var.set(
            f'Showing Top {topn} of {n_total} constituents\n'
            f'Top {topn} weight: {top_pct:.1f}%  |  Rest: {rest_pct:.1f}%'
        )

        ax = self.ax_donut
        ax.cla()
        ax.set_facecolor('#0d1117')

        sizes   = [top_pct, rest_pct] if rest_pct > 0 else [top_pct]
        clabels = [f'Top {topn}\n{top_pct:.1f}%', f'Rest\n{rest_pct:.1f}%'] if rest_pct > 0 else [f'Top {topn}\n100%']
        colors  = ['#00C9FF', '#2d333b'][:len(sizes)]
        wedges, _ = ax.pie(sizes, colors=colors, startangle=90,
                           wedgeprops=dict(width=0.45, edgecolor='#0d1117', linewidth=1.5))
        ax.set_title(f'{lbl}\nConcentration', color='#cdd9e5', fontsize=8, pad=4)

        for wedge, clabel in zip(wedges, clabels):
            angle = (wedge.theta2 + wedge.theta1) / 2
            x = np.cos(np.radians(angle)) * 0.72
            y = np.sin(np.radians(angle)) * 0.72
            ax.text(x, y, clabel, ha='center', va='center',
                    color='#cdd9e5', fontsize=7)

        ax2 = self.ax_board
        ax2.cla()
        ax2.set_facecolor('#0d1117')
        ax2.tick_params(colors='#888', labelsize=7)
        for sp in ax2.spines.values(): sp.set_color('#333')
        ax2.spines['top'].set_visible(False)
        ax2.spines['right'].set_visible(False)

        board_weights = {}
        for sym, w in raw.items():
            b = self.board_map.get(sym, 'OTHER')
            board_weights[b] = board_weights.get(b, 0) + w / total * 100

        bnames  = list(board_weights.keys())
        bvals   = list(board_weights.values())
        bcolors = ['#00C9FF' if b == 'MAIN' else '#FFD93D' if b == 'ACE' else '#845EC2' for b in bnames]

        bars = ax2.bar(bnames, bvals, color=bcolors, edgecolor='none', width=0.5)
        for bar, val in zip(bars, bvals):
            ax2.text(bar.get_x() + bar.get_width() / 2,
                     bar.get_height() + 0.5,
                     f'{val:.1f}%', ha='center', va='bottom',
                     color='#cdd9e5', fontsize=7)
        ax2.set_title('Board Breakdown', color='#cdd9e5', fontsize=8, pad=4)
        ax2.set_ylabel('Weight %', color='#888', fontsize=7)
        ax2.set_ylim(0, max(bvals) * 1.2 if bvals else 1)

        self.fig_donut.tight_layout(pad=1.2)
        self.canvas_donut.draw()

        ax3 = self.ax_wbar
        ax3.cla()
        ax3.set_facecolor('#0d1117')
        ax3.tick_params(colors='#888', labelsize=7)
        for sp in ax3.spines.values(): sp.set_color('#333')
        ax3.spines['top'].set_visible(False)
        ax3.spines['right'].set_visible(False)

        bar_labels = []
        for s in top.index:
            name = self.short_name_map.get(s, s.replace('.KL', ''))
            name = name[:20] + '…' if len(name) > 20 else name
            bar_labels.append(name)
        wvals  = (top / total * 100).values
        bclrs  = ['#00C9FF' if self.board_map.get(s, '') == 'MAIN'
                  else '#FFD93D' if self.board_map.get(s, '') == 'ACE'
                  else '#845EC2' for s in top.index]

        y_pos = range(len(bar_labels))
        ax3.barh(list(y_pos), wvals, color=bclrs, edgecolor='none', height=0.7)
        ax3.set_yticks(list(y_pos))
        ax3.set_yticklabels(bar_labels, fontsize=6)
        ax3.invert_yaxis()
        ax3.set_xlabel('Weight %', color='#888', fontsize=7)
        ax3.set_title(f'Top {topn} Constituent Weights', color='#cdd9e5', fontsize=8, pad=4)

        for i, v in enumerate(wvals):
            ax3.text(v + 0.05, i, f'{v:.2f}%', va='center', color='#cdd9e5', fontsize=6)

        from matplotlib.patches import Patch
        legend_els = [Patch(color='#00C9FF', label='MAIN'),
                      Patch(color='#FFD93D', label='ACE'),
                      Patch(color='#845EC2', label='Other')]
        ax3.legend(handles=legend_els, fontsize=6, loc='lower right',
                   facecolor='#161b22', edgecolor='#333', labelcolor='#cdd9e5')

        self.fig_wbar.tight_layout(pad=1.2)
        self.canvas_wbar.draw()

    # =========================================================================
    # FUND EVENTS TAB — distributions paid + units issued from cash shortfall,
    # shown chronologically per fund, translated to the actual listing/event date.
    # =========================================================================
    def _build_fund_events_tab(self):
        ctrl = tk.Frame(self.tab_fund_events, bg='#0d1117')
        ctrl.pack(fill='x', padx=10, pady=(8, 4))

        tk.Label(ctrl, text='Fund:', bg='#0d1117', fg='#cdd9e5',
                 font=('Segoe UI', 9)).pack(side='left')
        self.fund_events_var = tk.StringVar(value='')
        self.fund_events_cb = ttk.Combobox(ctrl, textvariable=self.fund_events_var,
                                           state='readonly', width=28, font=('Segoe UI', 9))
        self.fund_events_cb.pack(side='left', padx=(6, 16))
        self.fund_events_cb.bind('<<ComboboxSelected>>', lambda e: self._draw_fund_events())

        tk.Label(ctrl, text='Type:', bg='#0d1117', fg='#cdd9e5',
                 font=('Segoe UI', 9)).pack(side='left')
        self.fund_events_type_var = tk.StringVar(value='All')
        type_cb = ttk.Combobox(ctrl, textvariable=self.fund_events_type_var, state='readonly',
                               values=['All', 'Distribution', 'Unit Issued (cash shortfall)',
                                       'Unit Issued (DRIP reinvest)'],
                               width=26, font=('Segoe UI', 9))
        type_cb.pack(side='left', padx=(6, 16))
        type_cb.bind('<<ComboboxSelected>>', lambda e: self._draw_fund_events())

        # Per-fund DRIP toggle — distributions issue new units (scrip dividend)
        # instead of paying cash, valued at that day's NAV.
        tk.Checkbutton(
            ctrl, text='🔁 Reinvest distributions as units (DRIP) — applies on next rebuild',
            variable=self.drip_distributions, command=self._run_refresh,
            bg='#0d1117', fg='#cdd9e5', selectcolor='#0d1117',
            activebackground='#0d1117', activeforeground='#00E5A0',
            font=('Segoe UI', 9), relief='flat', cursor='hand2'
        ).pack(side='left', padx=(0, 8))

        self.fund_events_summary_var = tk.StringVar(value='')
        tk.Label(self.tab_fund_events, textvariable=self.fund_events_summary_var,
                 bg='#0d1117', fg='#888', font=('Segoe UI', 8),
                 anchor='w', justify='left').pack(fill='x', padx=10, pady=(0, 4))

        frame = tk.Frame(self.tab_fund_events, bg='#0d1117')
        frame.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        cols = ('Date', 'Type', 'Symbol/Detail', 'Amount (RM)', 'Units', 'Units Outstanding After')
        self.fund_events_tree = ttk.Treeview(frame, columns=cols, show='headings')
        cw = {'Date': 90, 'Type': 170, 'Symbol/Detail': 110, 'Amount (RM)': 110,
              'Units': 100, 'Units Outstanding After': 150}
        for col in cols:
            self.fund_events_tree.heading(col, text=col)
            self.fund_events_tree.column(col, width=cw.get(col, 100),
                                         anchor='w' if col in ('Date', 'Type', 'Symbol/Detail') else 'e',
                                         stretch=False)
        vsb = ttk.Scrollbar(frame, orient='vertical', command=self.fund_events_tree.yview)
        hsb = ttk.Scrollbar(frame, orient='horizontal', command=self.fund_events_tree.xview)
        self.fund_events_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.fund_events_tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)

        self.fund_events_tree.tag_configure('dist',  foreground='#3FB950')
        self.fund_events_tree.tag_configure('issue', foreground='#F0883E')
        self.fund_events_tree.tag_configure('drip',  foreground='#00E5A0')

    def _refresh_fund_events_dropdown(self):
        if not hasattr(self, 'fund_events_cb'):
            return
        self.fund_events_cb['values'] = ALL_FUND_LABELS
        if not self.fund_events_var.get() and ALL_FUND_LABELS:
            self.fund_events_var.set(ALL_FUND_LABELS[0])
        # Also refresh the Fund Ledger dropdown
        if hasattr(self, '_ledger_fund_cb'):
            self._ledger_fund_cb['values'] = ALL_FUND_LABELS
            if not self._ledger_fund_var.get() and ALL_FUND_LABELS:
                self._ledger_fund_var.set(ALL_FUND_LABELS[0])

    def _draw_fund_events(self):
        if not hasattr(self, 'fund_events_tree'):
            return
        for row in self.fund_events_tree.get_children():
            self.fund_events_tree.delete(row)

        lbl = self.fund_events_var.get()
        if not lbl or not self.weights_snapshot:
            self.fund_events_summary_var.set('No fund selected.')
            return

        dist_logs = self.weights_snapshot.get('__ALL_FUND_DIST_LOGS__', {})
        unit_logs = self.weights_snapshot.get('__ALL_FUND_UNIT_LOGS__', {})
        dist_log  = dist_logs.get(lbl, [])
        unit_log  = unit_logs.get(lbl, [])
        type_filter = self.fund_events_type_var.get()

        # Build a unified, chronologically-sorted event list. Each event is
        # translated to the actual date it happened on (distribution date,
        # or the constituent's listing/entry date for unit-issuance events
        # triggered by a cash shortfall on that IPO's inclusion).
        #
        # Sign convention: Amount is from the FUND'S cash perspective.
        #   Distribution   -> cash OUT (paid to unitholders)   -> negative
        #   Unit Issued    -> capital deployed to buy the IPO  -> negative
        #                      (the shortfall RM that new units had to cover)
        #   DRIP reinvest  -> no cash moves at all (value stays in NAV as
        #                      extra units) -> shown as 0.0000, not cash out
        events = []   # (date, type, detail, amount, units, units_after)

        # entry events are split into REAL unit-issuance ('entry' in unit_log)
        # vs DRIP-driven issuance ('drip' in unit_log) — DRIP events come from
        # the distribution loop, not a cash-shortfall buy-in, and must not be
        # double-counted as a Distribution here (handled separately below).
        drip_dates = {dt for dt, ev_type, *_ in unit_log if ev_type == 'drip'}

        for dt, nav_before, dist_per_unit, nav_after in dist_log:
            is_drip = dt in drip_dates
            label   = 'Distribution (DRIP reinvest)' if is_drip else 'Distribution'
            if type_filter not in ('All', label):
                continue
            amt = 0.0 if is_drip else -dist_per_unit   # DRIP moves no cash; cash payout is an outflow
            events.append((dt, label, f'NAV {nav_before:.4f}→{nav_after:.4f}',
                           amt, None, None))

        for dt, ev_type, sym, shortfall_amt, units_issued, units_after, *rest in unit_log:
            if ev_type == 'entry' and units_issued > 0:
                if type_filter not in ('All', 'Unit Issued (cash shortfall)'):
                    continue
                # Translated to the actual day the new constituent listed
                # in (its D-1/entry date) — the moment the fund needed RM
                # to buy in and cash on hand wasn't enough, qualifying it
                # for new-unit issuance rather than a cash purchase.
                # Negative = capital deployed (cash out) to fund the buy-in.
                events.append((dt, 'Unit Issued (cash shortfall)', sym,
                               -shortfall_amt if shortfall_amt else 0.0, units_issued, units_after))
            elif ev_type == 'drip':
                if type_filter not in ('All', 'Unit Issued (DRIP reinvest)'):
                    continue
                events.append((dt, 'Unit Issued (DRIP reinvest)', sym,
                               0.0, units_issued, units_after))

        events.sort(key=lambda e: e[0])

        n_dist = sum(1 for e in events if 'Distribution' in e[1])
        n_issue = sum(1 for e in events if 'Unit Issued' in e[1])
        total_dist = sum(abs(e[3]) for e in events if 'Distribution' in e[1] and e[3])
        total_units_issued = sum(e[4] for e in events if e[4])

        for dt, etype, detail, amount, units, units_after in events:
            date_s = dt.strftime('%d/%m/%Y') if hasattr(dt, 'strftime') else str(dt)
            tag = 'drip' if 'DRIP' in etype else ('dist' if 'Distribution' in etype else 'issue')
            self.fund_events_tree.insert('', 'end', tags=(tag,),
                values=(date_s, etype, detail,
                        f'{amount:+,.4f}' if amount is not None else '—',
                        _fmt_units(units) if units is not None else '—',
                        f'{units_after:,.2f}' if units_after is not None else '—'))

        _ccy_pfx = FUND_META.get(lbl, {}).get('ccy', 'RM')
        self.fund_events_summary_var.set(
            f'{lbl}  ·  {n_dist} distribution(s) — {_ccy_pfx}{total_dist:.4f}/unit paid out as cash  ·  '
            f'{n_issue} unit-issuance event(s) totalling {total_units_issued:,.2f} new units '
            f'(cash insufficient to buy in at that IPO\'s listing date)'
        )

    # =========================================================================
    # FUND LEDGER TAB — Statement of Financial Position (SOFP) + account book
    # =========================================================================
    def _build_fund_ledger_tab(self):
        tab = self.tab_fund_ledger

        # ── Controls row ─────────────────────────────────────────────────────
        ctrl = tk.Frame(tab, bg='#0d1117')
        ctrl.pack(fill='x', padx=10, pady=(8, 4))

        tk.Label(ctrl, text='Fund:', bg='#0d1117', fg='#cdd9e5',
                 font=('Segoe UI', 9)).pack(side='left')
        self._ledger_fund_var = tk.StringVar(value='')
        self._ledger_fund_cb  = ttk.Combobox(ctrl, textvariable=self._ledger_fund_var,
                                              state='readonly', width=28,
                                              font=('Segoe UI', 9))
        self._ledger_fund_cb.pack(side='left', padx=(6, 16))
        self._ledger_fund_cb.bind('<<ComboboxSelected>>', lambda e: self._draw_fund_ledger())

        tk.Label(ctrl, text='Show:', bg='#0d1117', fg='#cdd9e5',
                 font=('Segoe UI', 9)).pack(side='left')
        self._ledger_filter_var = tk.StringVar(value='All')
        for opt in ('All', 'Cash flows only', 'NAV events only'):
            tk.Radiobutton(ctrl, text=opt, variable=self._ledger_filter_var,
                           value=opt, command=self._draw_fund_ledger,
                           bg='#0d1117', fg='#cdd9e5', selectcolor='#0d1117',
                           activebackground='#0d1117', activeforeground='#00E5A0',
                           font=('Segoe UI', 9), relief='flat',
                           cursor='hand2').pack(side='left', padx=(4, 0))

        # ── SOFP summary cards ────────────────────────────────────────────────
        # Field specs: (base_name, has_ccy). Field-name Label widgets are kept
        # so _draw_fund_ledger can swap the '(RM)'/'($)' suffix per selected
        # fund instead of it being baked in as static text at build time.
        sofp_frame = tk.Frame(tab, bg='#0d1117')
        sofp_frame.pack(fill='x', padx=10, pady=(4, 6))

        self._sofp_field_lbls = {}   # base_name -> name Label widget (ccy-suffixed ones only)

        def _sofp_card(parent, header, header_col, fields, val_fg_rule):
            card = tk.Frame(parent, bg='#161b22',
                            highlightbackground=header_col, highlightthickness=1,
                            padx=14, pady=8)
            card.pack(side='left', fill='x', expand=True, padx=4)
            tk.Label(card, text=header, font=('Segoe UI', 9, 'bold'),
                     fg=header_col, bg='#161b22').pack(anchor='w')
            var_dict = {}
            for base, has_ccy, width in fields:
                r = tk.Frame(card, bg='#161b22'); r.pack(fill='x', pady=1)
                name_lbl = tk.Label(r, text=f'{base} (RM)' if has_ccy else base,
                                     font=('Segoe UI', 8), fg='#888',
                                     bg='#161b22', width=width, anchor='w')
                name_lbl.pack(side='left')
                if has_ccy:
                    self._sofp_field_lbls[base] = name_lbl
                v = tk.StringVar(value='—')
                tk.Label(r, textvariable=v, font=('Segoe UI', 8, 'bold'),
                         fg=val_fg_rule(base), bg='#161b22', anchor='e').pack(side='right')
                var_dict[base] = v
            return var_dict

        self._sofp_asset_vars = _sofp_card(
            sofp_frame, 'ASSETS', '#3FB950',
            [('Holdings Value', True, 24), ('Cash on Hand', True, 24), ('Total Assets', True, 24)],
            lambda base: '#3FB950' if 'Total' in base else '#cdd9e5')

        self._sofp_equity_vars = _sofp_card(
            sofp_frame, 'UNIT CAPITAL (EQUITY)', '#58A6FF',
            [('Units Outstanding', False, 28), ('NAV per Unit', True, 28), ('Total NAV', True, 28),
             ('Cum. Distributions/Unit', True, 28), ('Total Return/Unit', True, 28)],
            lambda base: '#58A6FF' if 'Return' in base else '#cdd9e5')

        self._sofp_perf_vars = _sofp_card(
            sofp_frame, 'PERFORMANCE', '#D29922',
            [('Inception Date', False, 26), ('Par NAV', True, 26), ('NAV Return %', False, 26),
             ('Total Return % (incl. dist)', False, 26), ('N Distributions', False, 26),
             ('N Unit Issuances', False, 26)],
            lambda base: '#D29922' if 'Return' in base else '#cdd9e5')

        # ── Ledger table ──────────────────────────────────────────────────────
        tk.Label(tab, text='ACCOUNT BOOK  —  Chronological Statement',
                 font=('Segoe UI', 9, 'bold'), fg='#cdd9e5',
                 bg='#0d1117', anchor='w', padx=10).pack(fill='x', pady=(4, 0))
        self._ledger_summary_var = tk.StringVar(value='')
        tk.Label(tab, textvariable=self._ledger_summary_var,
                 font=('Segoe UI', 8), fg='#888', bg='#0d1117',
                 anchor='w', padx=10).pack(fill='x')

        frame = tk.Frame(tab, bg='#0d1117')
        frame.pack(fill='both', expand=True, padx=10, pady=(2, 8))

        # (base column name, has_ccy) — headers get the right currency suffix
        # applied on each draw.
        self._ledger_col_specs = [
            ('Date', False), ('Type', False), ('Description', False),
            ('Debit', True), ('Credit', True), ('Cash Balance', True),
            ('Units Out', False), ('NAV', True), ('Total Assets', True),
        ]
        cols = [c for c, _ in self._ledger_col_specs]
        self._ledger_tree = ttk.Treeview(frame, columns=cols, show='headings')
        cw = {'Date': 90, 'Type': 160, 'Description': 160,
              'Debit': 100, 'Credit': 100,
              'Cash Balance': 120, 'Units Out': 100,
              'NAV': 90, 'Total Assets': 120}
        for col in cols:
            self._ledger_tree.heading(col, text=col)
            self._ledger_tree.column(
                col, width=cw.get(col, 90),
                anchor='w' if col in ('Date', 'Type', 'Description') else 'e',
                stretch=False)
        vsb = ttk.Scrollbar(frame, orient='vertical',   command=self._ledger_tree.yview)
        hsb = ttk.Scrollbar(frame, orient='horizontal', command=self._ledger_tree.xview)
        self._ledger_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._ledger_tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)

        # Row colours: cash-in = green, cash-out = red, NAV-only = dim
        self._ledger_tree.tag_configure('credit',  foreground='#3FB950')  # cash in
        self._ledger_tree.tag_configure('debit',   foreground='#F85149')  # cash out
        self._ledger_tree.tag_configure('neutral', foreground='#8B949E')  # no cash move
        self._ledger_tree.tag_configure('section', foreground='#D29922',
                                        font=('Segoe UI', 8, 'bold'))

    def _draw_fund_ledger(self):
        if not hasattr(self, '_ledger_tree'):
            return
        for row in self._ledger_tree.get_children():
            self._ledger_tree.delete(row)

        lbl = self._ledger_fund_var.get()
        if not lbl or not self.weights_snapshot:
            self._ledger_summary_var.set('No fund selected.')
            return

        dist_logs = self.weights_snapshot.get('__ALL_FUND_DIST_LOGS__', {})
        unit_logs = self.weights_snapshot.get('__ALL_FUND_UNIT_LOGS__', {})
        frames    = self.all_frames
        dist_log  = dist_logs.get(lbl, [])
        unit_log  = unit_logs.get(lbl, [])

        # Per-fund par/ccy/units — unit trusts and VFund CEFs both come
        # through here now, so nothing below may assume the RM0.2500/1000-unit
        # unit-trust globals apply to every label.
        meta   = FUND_META.get(lbl, {'par': FUND_PAR_NAV, 'ccy': 'RM',
                                      'units': FUND_UNITS_ISSUED, 'note': ''})
        par    = meta['par']
        ccy    = meta['ccy']
        units0 = meta['units']
        is_vfund = lbl in VFUND_LABELS
        _incept_ts = None   # fund's actual launch date, for the opening ledger row

        # Swap the '(RM)'/'($)' suffix on every ccy-tagged field-name label to
        # match this fund's real currency (previously static text baked in at
        # build time, so every fund — including the USD VFund CEFs — showed
        # '(RM)' regardless of what it actually traded in).
        for base, name_lbl in self._sofp_field_lbls.items():
            name_lbl.config(text=f'{base} ({ccy})')
        if hasattr(self, '_ledger_tree'):
            for base, has_ccy in self._ledger_col_specs:
                self._ledger_tree.heading(base, text=f'{base} ({ccy})' if has_ccy else base)

        # ── Update SOFP cards ─────────────────────────────────────────────────
        if lbl in frames:
            fund_df   = frames[lbl]
            nav_series = fund_df[lbl].dropna()
            cum_dist   = fund_df.get(f'{lbl} CumDist', None)
            tr_series  = fund_df.get(f'{lbl} TotalReturn', None)
            aum_series = fund_df.get(f'{lbl} AUM', None)
            units_s    = fund_df.get(f'{lbl} Units', None)

            nav_last   = nav_series.iloc[-1] if not nav_series.empty else 0.0
            cd_last    = cum_dist.iloc[-1]  if cum_dist  is not None and not cum_dist.empty  else 0.0
            tr_last    = tr_series.iloc[-1] if tr_series is not None and not tr_series.empty else nav_last
            units_last = units_s.iloc[-1]   if units_s   is not None and not units_s.empty   else units0
            # VFund CEFs carry no AUM column (never needed one — units are
            # fixed at launch and never diluted) — AUM = NAV/unit × units
            # holds exactly, so derive it rather than defaulting to 0.
            aum_last   = (aum_series.iloc[-1] if aum_series is not None and not aum_series.empty
                          else nav_last * units_last)

            # Rough holdings value = AUM - cash (estimate from last cash in unit_log).
            # VFund CEFs are fully invested at launch and never hold a cash
            # bucket, so unit_log is empty and last_cash correctly stays 0.
            last_cash = 0.0
            if unit_log:
                # Last logged event carries cash_after at index 6
                last_entry = unit_log[-1]
                if len(last_entry) >= 7:
                    last_cash = last_entry[6]
            holdings_val = max(0.0, aum_last - last_cash)

            nav_ret_pct = (nav_last / par - 1) * 100
            tr_ret_pct  = (tr_last  / par - 1) * 100
            inception_date = nav_series.index[0].strftime('%d %b %Y') if not nav_series.empty else '—'
            _incept_ts = nav_series.index[0] if not nav_series.empty else None
            n_dist   = len(dist_log)
            n_issue  = sum(1 for e in unit_log if len(e) > 1 and e[1] == 'entry' and e[4] > 0)

            self._sofp_asset_vars['Holdings Value'].set(f'{ccy}{holdings_val:,.4f}')
            self._sofp_asset_vars['Cash on Hand'].set(f'{ccy}{last_cash:,.4f}')
            self._sofp_asset_vars['Total Assets'].set(f'{ccy}{aum_last:,.4f}')
            self._sofp_equity_vars['Units Outstanding'].set(f'{units_last:,.2f}')
            self._sofp_equity_vars['NAV per Unit'].set(f'{ccy}{nav_last:,.4f}')
            self._sofp_equity_vars['Total NAV'].set(f'{ccy}{aum_last:,.4f}')
            self._sofp_equity_vars['Cum. Distributions/Unit'].set(f'{ccy}{cd_last:,.4f}')
            self._sofp_equity_vars['Total Return/Unit'].set(f'{ccy}{tr_last:,.4f}')
            self._sofp_perf_vars['Inception Date'].set(inception_date)
            self._sofp_perf_vars['Par NAV'].set(f'{ccy}{par:.4f}')
            self._sofp_perf_vars['NAV Return %'].set(f'{nav_ret_pct:+.2f}%')
            self._sofp_perf_vars['Total Return % (incl. dist)'].set(f'{tr_ret_pct:+.2f}%')
            self._sofp_perf_vars['N Distributions'].set(str(n_dist))
            self._sofp_perf_vars['N Unit Issuances'].set(str(n_issue))

        # ── Build chronological account-book events ───────────────────────────
        # Sign convention — from the fund's cash account perspective:
        #   CREDIT (cash IN):  unit issuance proceeds, exit/sale proceeds
        #   DEBIT  (cash OUT): buy-in purchase payments, distributions paid
        #   NEUTRAL:           DRIP unit issuance (no cash moves), NAV revaluation
        #
        # Each row shows: Date | Type | Description | Debit | Credit |
        #                 Running Cash | Units Out | NAV | Total Assets

        events = []   # (date, type, desc, debit, credit, cash_after, units_out, nav_after)
        filt = self._ledger_filter_var.get()

        for entry in unit_log:
            if len(entry) < 8:
                continue
            dt, ev_type, sym, amount, units_issued, units_out, cash_after, nav_after = entry

            if ev_type == 'entry':
                # buy-in: new units issued to cover shortfall (CREDIT cash in from
                # investors), then immediately deployed to buy the IPO (DEBIT cash out)
                units_rm = abs(amount)   # shortfall = new units × NAV at issuance
                # Credit: unit issuance proceeds (cash raised by issuing new units)
                if filt in ('All', 'Cash flows only'):
                    events.append((dt, 'Unit Issued', f'New units for {sym} buy-in',
                                   0.0, units_rm, cash_after, units_out, nav_after))
                    # Debit: buy-in payment — cost of acquiring the IPO position
                    buy_in_total = units_rm   # shortfall = the unfunded portion of buy-in
                    events.append((dt, 'Buy-in (IPO)', f'Acquired {sym}',
                                   buy_in_total, 0.0, cash_after, units_out, nav_after))

            elif ev_type == 'exit':
                net_proc = abs(amount)
                if filt in ('All', 'Cash flows only'):
                    events.append((dt, 'Exit (D+365 Sale)', f'Disposed {sym}',
                                   0.0, net_proc, cash_after, units_out, nav_after))

            elif ev_type == 'drip':
                if filt in ('All', 'NAV events only'):
                    events.append((dt, 'DRIP Reinvest', f'{units_issued:,.4f} units issued',
                                   0.0, 0.0, cash_after, units_out, nav_after))

        for dt, nav_before, dist_per_unit, nav_after in dist_log:
            # Find matching units_out from unit_log at this date (for cash calc)
            units_at_dist = units0
            cash_at_dist  = 0.0
            for entry in unit_log:
                if len(entry) >= 8 and entry[0] <= dt:
                    units_at_dist = entry[5]
                    cash_at_dist  = entry[6]
            dist_total = dist_per_unit * units_at_dist
            if filt in ('All', 'Cash flows only'):
                events.append((dt, 'Distribution Paid',
                               f'{ccy}{dist_per_unit:.4f}/unit × {units_at_dist:,.2f} units',
                               dist_total, 0.0, cash_at_dist, units_at_dist, nav_after))

        events.sort(key=lambda e: e[0])

        if filt in ('All', 'NAV events only'):
            # Opening entry — anchored to the fund's real inception date, not
            # events[0]. VFund CEFs raise capital once and never issue/redeem/
            # distribute again, so unit_log and dist_log are empty for them;
            # gating this on "if events" silently blanked their entire ledger.
            first_dt = events[0][0] if events else _incept_ts
            if first_dt is not None:
                if is_vfund:
                    note = meta.get('note', '')
                    desc = (f'VFund launch (equal-weight CEF) @ {ccy}{par:.4f} par · '
                            f'{units0} units' + (f' — {note}' if note else ''))
                else:
                    desc = f'Fund inception @ {ccy}{par:.4f} par · {units0} units'
                self._ledger_tree.insert('', 'end', tags=('section',),
                    values=(first_dt.strftime('%d/%m/%Y') if hasattr(first_dt, 'strftime') else str(first_dt),
                            '── OPENING ──', desc,
                            '', '', f'{units0 * par:,.4f}',
                            f'{units0:,.2f}', f'{par:.4f}',
                            f'{units0 * par:,.4f}'))

        total_debits = 0.0; total_credits = 0.0
        for dt, etype, desc, debit, credit, cash_after, units_out, nav_after in events:
            date_s = dt.strftime('%d/%m/%Y') if hasattr(dt, 'strftime') else str(dt)
            total_assets = nav_after * units_out
            if debit > 0 and credit == 0:
                tag = 'debit'
            elif credit > 0 and debit == 0:
                tag = 'credit'
            else:
                tag = 'neutral'
            total_debits  += debit
            total_credits += credit
            self._ledger_tree.insert('', 'end', tags=(tag,),
                values=(date_s, etype, desc,
                        f'{debit:,.4f}'   if debit  > 0 else '—',
                        f'{credit:,.4f}'  if credit > 0 else '—',
                        f'{cash_after:,.4f}', f'{units_out:,.2f}',
                        f'{nav_after:.4f}', f'{total_assets:,.4f}'))

        n_events = len(events)
        self._ledger_summary_var.set(
            f'{lbl}  ·  {n_events} ledger entries  ·  '
            f'Total debits: {ccy}{total_debits:,.4f}  ·  '
            f'Total credits: {ccy}{total_credits:,.4f}  ·  '
            f'Net cash flow: {ccy}{total_credits - total_debits:+,.4f}'
        )

    # =========================================================================
    # PTS — PredictTheShares Gamify Module
    # Virtual prediction-credit ecosystem per the PTS proposal.
    # All data persisted in paa_settings.json under "pts" key.
    # =========================================================================

    # ── Rank thresholds (accuracy % + minimum predictions) ───────────────────
    PTS_RANKS = [
        (80, 20, '🔮 Market Oracle',      '#FFD700'),
        (65, 10, '📊 Senior Analyst',     '#C0C0C0'),
        (50,  5, '🔭 Investment Explorer','#CD7F32'),
        (  0,  0, '📘 Beginner Analyst',  '#58A6FF'),
    ]
    PTS_STARTING_CREDITS = 10_000.0
    PTS_MIN_STAKE        = 10.0       # minimum credits per prediction
    PTS_TRADE_LOT        = 10_000.0   # lot size reference from proposal

    # ── Category definitions ──────────────────────────────────────────────────
    PTS_CATEGORIES = [
        'Stock Price Target',
        'Index Target',
        'IPO Performance',
        'Economic Forecast',
        'Custom',
    ]

    def _pts_load(self) -> dict:
        """Load THIS K-ID's PTS/ZDWS wallet from paa_settings.json.

        Storage: settings['pts_by_user'][username]  — one wallet per K-ID, so
        credits, predictions and warrants never leak between accounts.
        Legacy global settings['pts'] is migrated to the current user on first
        read, then left in place as a dormant backup.
        """
        s   = load_app_settings()
        key = (CURRENT_KID or '').strip().lower()   # '' = guest / legacy PIN mode

        by_user = s.setdefault('pts_by_user', {})

        if key not in by_user:
            legacy = s.get('pts')
            # Migrate the old global wallet to the first K-ID that signs in,
            # but only if it actually holds something worth keeping.
            if legacy and not s.get('pts_migrated') and (
                legacy.get('predictions') or legacy.get('history')
                or legacy.get('zdws_active') or legacy.get('zdws_history')
                or legacy.get('credits', 0) != _DEFAULT_SETTINGS['pts']['credits']
            ):
                by_user[key]      = dict(legacy)
                s['pts_migrated'] = True
                save_app_settings(s)
            else:
                by_user[key] = _pts_fresh_wallet()
                save_app_settings(s)

        pts = by_user[key]
        for k, v in _DEFAULT_SETTINGS['pts'].items():
            # deep-copy mutable defaults so wallets never share list objects
            pts.setdefault(k, _copy.deepcopy(v) if isinstance(v, (list, dict)) else v)
        return pts

    def _pts_save(self, pts: dict):
        """Persist THIS K-ID's PTS/ZDWS wallet."""
        s   = load_app_settings()
        key = (CURRENT_KID or '').strip().lower()
        s.setdefault('pts_by_user', {})[key] = pts
        save_app_settings(s)

    def _pts_rank(self, pts: dict) -> tuple:
        """Return (rank_label, rank_color) based on accuracy and prediction count."""
        total   = pts.get('total_made', 0)
        correct = pts.get('total_correct', 0)
        acc     = (correct / total * 100) if total else 0
        for min_acc, min_preds, label, color in self.PTS_RANKS:
            if acc >= min_acc and total >= min_preds:
                return label, color
        return self.PTS_RANKS[-1][2], self.PTS_RANKS[-1][3]

    # =========================================================================
    # ZDWS — Zero-Decay Warrant System
    # Bull/Bear warrants on internal indices. P/L = stake × (1 + |%move| × 10)
    # if direction correct, 0 if wrong. Zero time decay. Auto-settles when the
    # settlement date passes (checked on every PTS tab refresh).
    # =========================================================================

    # ── World indices tradeable via ZDWS ──────────────────────────────────────
    # Credits are MYR-denominated, so a foreign index bet is settled on the
    # MYR-converted move: total% = index% + FX%. That is what a Malaysian
    # investor actually experiences, and it makes an HKD and a USD position
    # comparable from the same wallet.
    WORLD_INDICES = [
        ('^HSI',  'Hang Seng',       'HKD'),
        ('^STI',  'Straits Times',   'SGD'),
        ('^N225', 'Nikkei 225',      'JPY'),
        ('^KS11', 'KOSPI',           'KRW'),
        ('^GSPC', 'S&P 500',         'USD'),
        ('^IXIC', 'Nasdaq Composite','USD'),
        ('^DJI',  'Dow Jones',       'USD'),
        ('^SOX',  'PHLX Semiconductor','USD'),
        ('^RUI',  'Russell 1000',    'USD'),
        ('^RUT',  'Russell 2000',    'USD'),
        ('^RUA',  'Russell 3000',    'USD'),
        ('^FTSE', 'FTSE 100',        'GBP'),
        ('^FTMC', 'FTSE 250',        'GBP'),
    ]
    SP500_CONFIG = [
        ('MMM', '3M'),
        ('AOS', 'A. O. Smith'),
        ('ABT', 'Abbott Laboratories'),
        ('ABBV', 'AbbVie'),
        ('ACN', 'Accenture'),
        ('ADBE', 'Adobe Inc.'),
        ('AMD', 'Advanced Micro Devices'),
        ('AES', 'AES Corporation'),
        ('AFL', 'Aflac'),
        ('A', 'Agilent Technologies'),
        ('APD', 'Air Products'),
        ('ABNB', 'Airbnb'),
        ('AKAM', 'Akamai Technologies'),
        ('ALB', 'Albemarle Corporation'),
        ('ARE', 'Alexandria Real Estate Equities'),
        ('ALGN', 'Align Technology'),
        ('ALLE', 'Allegion'),
        ('LNT', 'Alliant Energy'),
        ('ALL', 'Allstate'),
        ('GOOGL', 'Alphabet Inc. (Class A)'),
        ('GOOG', 'Alphabet Inc. (Class C)'),
        ('MO', 'Altria'),
        ('AMZN', 'Amazon'),
        ('AMCR', 'Amcor'),
        ('AEE', 'Ameren'),
        ('AEP', 'American Electric Power'),
        ('AXP', 'American Express'),
        ('AIG', 'American International Group'),
        ('AMT', 'American Tower'),
        ('AWK', 'American Water Works'),
        ('AMP', 'Ameriprise Financial'),
        ('AME', 'Ametek'),
        ('AMGN', 'Amgen'),
        ('APH', 'Amphenol'),
        ('ADI', 'Analog Devices'),
        ('AON', 'Aon plc'),
        ('APA', 'APA Corporation'),
        ('APO', 'Apollo Global Management'),
        ('AAPL', 'Apple Inc.'),
        ('AMAT', 'Applied Materials'),
        ('APP', 'AppLovin'),
        ('APTV', 'Aptiv'),
        ('ACGL', 'Arch Capital Group'),
        ('ADM', 'Archer Daniels Midland'),
        ('ARES', 'Ares Management'),
        ('ANET', 'Arista Networks'),
        ('AJG', 'Arthur J. Gallagher & Co.'),
        ('AIZ', 'Assurant'),
        ('T', 'AT&T'),
        ('ATO', 'Atmos Energy'),
        ('ADSK', 'Autodesk'),
        ('ADP', 'Automatic Data Processing'),
        ('AZO', 'AutoZone'),
        ('AVB', 'AvalonBay Communities'),
        ('AVY', 'Avery Dennison'),
        ('AXON', 'Axon Enterprise'),
        ('BKR', 'Baker Hughes'),
        ('BALL', 'Ball Corporation'),
        ('BAC', 'Bank of America'),
        ('BAX', 'Baxter International'),
        ('BDX', 'Becton Dickinson'),
        ('BRK-B', 'Berkshire Hathaway'),
        ('BBY', 'Best Buy'),
        ('TECH', 'Bio-Techne'),
        ('BIIB', 'Biogen'),
        ('BLK', 'BlackRock'),
        ('BX', 'Blackstone Inc.'),
        ('XYZ', 'Block, Inc.'),
        ('BNY', 'BNY Mellon'),
        ('BA', 'Boeing'),
        ('BKNG', 'Booking Holdings'),
        ('BSX', 'Boston Scientific'),
        ('BMY', 'Bristol Myers Squibb'),
        ('AVGO', 'Broadcom'),
        ('BR', 'Broadridge Financial Solutions'),
        ('BRO', 'Brown & Brown'),
        ('BF-B', 'Brown-Forman'),
        ('BLDR', 'Builders FirstSource'),
        ('BG', 'Bunge Global'),
        ('BXP', 'BXP, Inc.'),
        ('CHRW', 'C.H. Robinson'),
        ('CDNS', 'Cadence Design Systems'),
        ('CPT', 'Camden Property Trust'),
        ('CPB', 'Campbell\'s Company (The)'),
        ('COF', 'Capital One'),
        ('CAH', 'Cardinal Health'),
        ('CCL', 'Carnival Corporation'),
        ('CARR', 'Carrier Global'),
        ('CVNA', 'Carvana'),
        ('CASY', 'Casey\'s'),
        ('CAT', 'Caterpillar Inc.'),
        ('CBOE', 'Cboe Global Markets'),
        ('CBRE', 'CBRE Group'),
        ('CDW', 'CDW Corporation'),
        ('COR', 'Cencora'),
        ('CNC', 'Centene Corporation'),
        ('CNP', 'CenterPoint Energy'),
        ('CF', 'CF Industries'),
        ('CRL', 'Charles River Laboratories'),
        ('SCHW', 'Charles Schwab Corporation'),
        ('CHTR', 'Charter Communications'),
        ('CVX', 'Chevron Corporation'),
        ('CMG', 'Chipotle Mexican Grill'),
        ('CB', 'Chubb Limited'),
        ('CHD', 'Church & Dwight'),
        ('CIEN', 'Ciena'),
        ('CI', 'Cigna'),
        ('CINF', 'Cincinnati Financial'),
        ('CTAS', 'Cintas'),
        ('CSCO', 'Cisco'),
        ('C', 'Citigroup'),
        ('CFG', 'Citizens Financial Group'),
        ('CLX', 'Clorox'),
        ('CME', 'CME Group'),
        ('CMS', 'CMS Energy'),
        ('KO', 'Coca-Cola Company (The)'),
        ('CTSH', 'Cognizant'),
        ('COHR', 'Coherent Corp.'),
        ('COIN', 'Coinbase'),
        ('CL', 'Colgate-Palmolive'),
        ('CMCSA', 'Comcast'),
        ('FIX', 'Comfort Systems USA'),
        ('CAG', 'Conagra Brands'),
        ('COP', 'ConocoPhillips'),
        ('ED', 'Consolidated Edison'),
        ('STZ', 'Constellation Brands'),
        ('CEG', 'Constellation Energy'),
        ('COO', 'Cooper Companies (The)'),
        ('CPRT', 'Copart'),
        ('GLW', 'Corning Inc.'),
        ('CPAY', 'Corpay'),
        ('CTVA', 'Corteva'),
        ('CSGP', 'CoStar Group'),
        ('COST', 'Costco'),
        ('CRH', 'CRH plc'),
        ('CRWD', 'CrowdStrike'),
        ('CCI', 'Crown Castle'),
        ('CSX', 'CSX Corporation'),
        ('CMI', 'Cummins'),
        ('CVS', 'CVS Health'),
        ('DHR', 'Danaher Corporation'),
        ('DRI', 'Darden Restaurants'),
        ('DDOG', 'Datadog'),
        ('DVA', 'DaVita'),
        ('DECK', 'Deckers Brands'),
        ('DE', 'Deere & Company'),
        ('DELL', 'Dell Technologies'),
        ('DAL', 'Delta Air Lines'),
        ('DVN', 'Devon Energy'),
        ('DXCM', 'Dexcom'),
        ('FANG', 'Diamondback Energy'),
        ('DLR', 'Digital Realty'),
        ('DG', 'Dollar General'),
        ('DLTR', 'Dollar Tree'),
        ('D', 'Dominion Energy'),
        ('DPZ', 'Domino\'s'),
        ('DASH', 'DoorDash'),
        ('DOV', 'Dover Corporation'),
        ('DOW', 'Dow Inc.'),
        ('DHI', 'D. R. Horton'),
        ('DTE', 'DTE Energy'),
        ('DUK', 'Duke Energy'),
        ('DD', 'DuPont'),
        ('ETN', 'Eaton Corporation'),
        ('EBAY', 'eBay Inc.'),
        ('SATS', 'EchoStar'),
        ('ECL', 'Ecolab'),
        ('EIX', 'Edison International'),
        ('EW', 'Edwards Lifesciences'),
        ('EA', 'Electronic Arts'),
        ('ELV', 'Elevance Health'),
        ('EME', 'Emcor'),
        ('EMR', 'Emerson Electric'),
        ('ETR', 'Entergy'),
        ('EOG', 'EOG Resources'),
        ('EPAM', 'EPAM Systems'),
        ('EQT', 'EQT Corporation'),
        ('EFX', 'Equifax'),
        ('EQIX', 'Equinix'),
        ('EQR', 'Equity Residential'),
        ('ERIE', 'Erie Indemnity'),
        ('ESS', 'Essex Property Trust'),
        ('EL', 'Estee Lauder Companies (The)'),
        ('EG', 'Everest Group'),
        ('EVRG', 'Evergy'),
        ('ES', 'Eversource Energy'),
        ('EXC', 'Exelon'),
        ('EXE', 'Expand Energy'),
        ('EXPE', 'Expedia Group'),
        ('EXPD', 'Expeditors International'),
        ('EXR', 'Extra Space Storage'),
        ('XOM', 'ExxonMobil'),
        ('FFIV', 'F5, Inc.'),
        ('FDS', 'FactSet'),
        ('FICO', 'Fair Isaac'),
        ('FAST', 'Fastenal'),
        ('FRT', 'Federal Realty Investment Trust'),
        ('FDX', 'FedEx'),
        ('FIS', 'Fidelity National Information Services'),
        ('FITB', 'Fifth Third Bancorp'),
        ('FSLR', 'First Solar'),
        ('FE', 'FirstEnergy'),
        ('FISV', 'Fiserv'),
        ('F', 'Ford Motor Company'),
        ('FTNT', 'Fortinet'),
        ('FTV', 'Fortive'),
        ('FOXA', 'Fox Corporation (Class A)'),
        ('FOX', 'Fox Corporation (Class B)'),
        ('BEN', 'Franklin Resources'),
        ('FCX', 'Freeport-McMoRan'),
        ('GRMN', 'Garmin'),
        ('IT', 'Gartner'),
        ('GE', 'GE Aerospace'),
        ('GEHC', 'GE HealthCare'),
        ('GEV', 'GE Vernova'),
        ('GEN', 'Gen Digital'),
        ('GNRC', 'Generac'),
        ('GD', 'General Dynamics'),
        ('GIS', 'General Mills'),
        ('GM', 'General Motors'),
        ('GPC', 'Genuine Parts Company'),
        ('GILD', 'Gilead Sciences'),
        ('GPN', 'Global Payments'),
        ('GL', 'Globe Life'),
        ('GDDY', 'GoDaddy'),
        ('GS', 'Goldman Sachs'),
        ('HAL', 'Halliburton'),
        ('HIG', 'Hartford (The)'),
        ('HAS', 'Hasbro'),
        ('HCA', 'HCA Healthcare'),
        ('DOC', 'Healthpeak Properties'),
        ('HSIC', 'Henry Schein'),
        ('HSY', 'Hershey Company (The)'),
        ('HPE', 'Hewlett Packard Enterprise'),
        ('HLT', 'Hilton Worldwide'),
        ('HD', 'Home Depot (The)'),
        ('HON', 'Honeywell'),
        ('HRL', 'Hormel Foods'),
        ('HST', 'Host Hotels & Resorts'),
        ('HWM', 'Howmet Aerospace'),
        ('HPQ', 'HP Inc.'),
        ('HUBB', 'Hubbell Incorporated'),
        ('HUM', 'Humana'),
        ('HBAN', 'Huntington Bancshares'),
        ('HII', 'Huntington Ingalls Industries'),
        ('IBM', 'IBM'),
        ('IEX', 'IDEX Corporation'),
        ('IDXX', 'Idexx Laboratories'),
        ('ITW', 'Illinois Tool Works'),
        ('INCY', 'Incyte'),
        ('IR', 'Ingersoll Rand'),
        ('PODD', 'Insulet Corporation'),
        ('INTC', 'Intel'),
        ('IBKR', 'Interactive Brokers'),
        ('ICE', 'Intercontinental Exchange'),
        ('IFF', 'International Flavors & Fragrances'),
        ('IP', 'International Paper'),
        ('INTU', 'Intuit'),
        ('ISRG', 'Intuitive Surgical'),
        ('IVZ', 'Invesco'),
        ('INVH', 'Invitation Homes'),
        ('IQV', 'IQVIA'),
        ('IRM', 'Iron Mountain'),
        ('JBHT', 'J.B. Hunt'),
        ('JBL', 'Jabil'),
        ('JKHY', 'Jack Henry & Associates'),
        ('J', 'Jacobs Solutions'),
        ('JNJ', 'Johnson & Johnson'),
        ('JCI', 'Johnson Controls'),
        ('JPM', 'JPMorgan Chase'),
        ('KVUE', 'Kenvue'),
        ('KDP', 'Keurig Dr Pepper'),
        ('KEY', 'KeyCorp'),
        ('KEYS', 'Keysight Technologies'),
        ('KMB', 'Kimberly-Clark'),
        ('KIM', 'Kimco Realty'),
        ('KMI', 'Kinder Morgan'),
        ('KKR', 'KKR & Co.'),
        ('KLAC', 'KLA Corporation'),
        ('KHC', 'Kraft Heinz'),
        ('KR', 'Kroger'),
        ('LHX', 'L3Harris'),
        ('LH', 'Labcorp'),
        ('LRCX', 'Lam Research'),
        ('LVS', 'Las Vegas Sands'),
        ('LDOS', 'Leidos'),
        ('LEN', 'Lennar'),
        ('LII', 'Lennox International'),
        ('LLY', 'Lilly (Eli)'),
        ('LIN', 'Linde plc'),
        ('LYV', 'Live Nation Entertainment'),
        ('LMT', 'Lockheed Martin'),
        ('L', 'Loews Corporation'),
        ('LOW', 'Lowe\'s'),
        ('LULU', 'Lululemon Athletica'),
        ('LITE', 'Lumentum'),
        ('LYB', 'LyondellBasell'),
        ('MTB', 'M&T Bank'),
        ('MPC', 'Marathon Petroleum'),
        ('MAR', 'Marriott International'),
        ('MRSH', 'Marsh McLennan'),
        ('MLM', 'Martin Marietta Materials'),
        ('MAS', 'Masco'),
        ('MA', 'Mastercard'),
        ('MKC', 'McCormick & Company'),
        ('MCD', 'McDonald\'s'),
        ('MCK', 'McKesson Corporation'),
        ('MDT', 'Medtronic'),
        ('MRK', 'Merck & Co.'),
        ('META', 'Meta Platforms'),
        ('MET', 'MetLife'),
        ('MTD', 'Mettler Toledo'),
        ('MGM', 'MGM Resorts'),
        ('MCHP', 'Microchip Technology'),
        ('MU', 'Micron Technology'),
        ('MSFT', 'Microsoft'),
        ('MAA', 'Mid-America Apartment Communities'),
        ('MRNA', 'Moderna'),
        ('TAP', 'Molson Coors Beverage Company'),
        ('MDLZ', 'Mondelez International'),
        ('MPWR', 'Monolithic Power Systems'),
        ('MNST', 'Monster Beverage'),
        ('MCO', 'Moody\'s Corporation'),
        ('MS', 'Morgan Stanley'),
        ('MOS', 'Mosaic Company (The)'),
        ('MSI', 'Motorola Solutions'),
        ('MSCI', 'MSCI Inc.'),
        ('NDAQ', 'Nasdaq, Inc.'),
        ('NTAP', 'NetApp'),
        ('NFLX', 'Netflix'),
        ('NEM', 'Newmont'),
        ('NWSA', 'News Corp (Class A)'),
        ('NWS', 'News Corp (Class B)'),
        ('NEE', 'NextEra Energy'),
        ('NKE', 'Nike, Inc.'),
        ('NI', 'NiSource'),
        ('NDSN', 'Nordson Corporation'),
        ('NSC', 'Norfolk Southern'),
        ('NTRS', 'Northern Trust'),
        ('NOC', 'Northrop Grumman'),
        ('NCLH', 'Norwegian Cruise Line Holdings'),
        ('NRG', 'NRG Energy'),
        ('NUE', 'Nucor'),
        ('NVDA', 'Nvidia'),
        ('NVR', 'NVR, Inc.'),
        ('NXPI', 'NXP Semiconductors'),
        ('ORLY', 'O\'Reilly Automotive'),
        ('OXY', 'Occidental Petroleum'),
        ('ODFL', 'Old Dominion'),
        ('OMC', 'Omnicom Group'),
        ('ON', 'ON Semiconductor'),
        ('OKE', 'Oneok'),
        ('ORCL', 'Oracle Corporation'),
        ('OTIS', 'Otis Worldwide'),
        ('PCAR', 'Paccar'),
        ('PKG', 'Packaging Corporation of America'),
        ('PLTR', 'Palantir Technologies'),
        ('PANW', 'Palo Alto Networks'),
        ('PSKY', 'Paramount Skydance Corporation'),
        ('PH', 'Parker Hannifin'),
        ('PAYX', 'Paychex'),
        ('PYPL', 'PayPal'),
        ('PNR', 'Pentair'),
        ('PEP', 'PepsiCo'),
        ('PFE', 'Pfizer'),
        ('PCG', 'PG&E Corporation'),
        ('PM', 'Philip Morris International'),
        ('PSX', 'Phillips 66'),
        ('PNW', 'Pinnacle West Capital'),
        ('PNC', 'PNC Financial Services'),
        ('POOL', 'Pool Corporation'),
        ('PPG', 'PPG Industries'),
        ('PPL', 'PPL Corporation'),
        ('PFG', 'Principal Financial Group'),
        ('PG', 'Procter & Gamble'),
        ('PGR', 'Progressive Corporation'),
        ('PLD', 'Prologis'),
        ('PRU', 'Prudential Financial'),
        ('PEG', 'Public Service Enterprise Group'),
        ('PTC', 'PTC Inc.'),
        ('PSA', 'Public Storage'),
        ('PHM', 'PulteGroup'),
        ('PWR', 'Quanta Services'),
        ('QCOM', 'Qualcomm'),
        ('DGX', 'Quest Diagnostics'),
        ('Q', 'Qnity Electronics'),
        ('RL', 'Ralph Lauren Corporation'),
        ('RJF', 'Raymond James Financial'),
        ('RTX', 'RTX Corporation'),
        ('O', 'Realty Income'),
        ('REG', 'Regency Centers'),
        ('REGN', 'Regeneron Pharmaceuticals'),
        ('RF', 'Regions Financial Corporation'),
        ('RSG', 'Republic Services'),
        ('RMD', 'ResMed'),
        ('RVTY', 'Revvity'),
        ('HOOD', 'Robinhood Markets'),
        ('ROK', 'Rockwell Automation'),
        ('ROL', 'Rollins, Inc.'),
        ('ROP', 'Roper Technologies'),
        ('ROST', 'Ross Stores'),
        ('RCL', 'Royal Caribbean Group'),
        ('SPGI', 'S&P Global'),
        ('CRM', 'Salesforce'),
        ('SNDK', 'Sandisk'),
        ('SBAC', 'SBA Communications'),
        ('SLB', 'Schlumberger'),
        ('STX', 'Seagate Technology'),
        ('SRE', 'Sempra'),
        ('NOW', 'ServiceNow'),
        ('SHW', 'Sherwin-Williams'),
        ('SPG', 'Simon Property Group'),
        ('SWKS', 'Skyworks Solutions'),
        ('SJM', 'J.M. Smucker Company (The)'),
        ('SW', 'Smurfit Westrock'),
        ('SNA', 'Snap-on'),
        ('SOLV', 'Solventum'),
        ('SO', 'Southern Company'),
        ('LUV', 'Southwest Airlines'),
        ('SWK', 'Stanley Black & Decker'),
        ('SBUX', 'Starbucks'),
        ('STT', 'State Street Corporation'),
        ('STLD', 'Steel Dynamics'),
        ('STE', 'Steris'),
        ('SYK', 'Stryker Corporation'),
        ('SMCI', 'Supermicro'),
        ('SYF', 'Synchrony Financial'),
        ('SNPS', 'Synopsys'),
        ('SYY', 'Sysco'),
        ('TMUS', 'T-Mobile US'),
        ('TROW', 'T. Rowe Price'),
        ('TTWO', 'Take-Two Interactive'),
        ('TPR', 'Tapestry, Inc.'),
        ('TRGP', 'Targa Resources'),
        ('TGT', 'Target Corporation'),
        ('TEL', 'TE Connectivity'),
        ('TDY', 'Teledyne Technologies'),
        ('TER', 'Teradyne'),
        ('TSLA', 'Tesla, Inc.'),
        ('TXN', 'Texas Instruments'),
        ('TPL', 'Texas Pacific Land Corporation'),
        ('TXT', 'Textron'),
        ('TMO', 'Thermo Fisher Scientific'),
        ('TJX', 'TJX Companies'),
        ('TKO', 'TKO Group Holdings'),
        ('TTD', 'Trade Desk (The)'),
        ('TSCO', 'Tractor Supply'),
        ('TT', 'Trane Technologies'),
        ('TDG', 'TransDigm Group'),
        ('TRV', 'Travelers Companies (The)'),
        ('TRMB', 'Trimble Inc.'),
        ('TFC', 'Truist Financial'),
        ('TYL', 'Tyler Technologies'),
        ('TSN', 'Tyson Foods'),
        ('USB', 'U.S. Bancorp'),
        ('UBER', 'Uber'),
        ('UDR', 'UDR, Inc.'),
        ('ULTA', 'Ulta Beauty'),
        ('UNP', 'Union Pacific Corporation'),
        ('UAL', 'United Airlines Holdings'),
        ('UPS', 'United Parcel Service'),
        ('URI', 'United Rentals'),
        ('UNH', 'UnitedHealth Group'),
        ('UHS', 'Universal Health Services'),
        ('VLO', 'Valero Energy'),
        ('VEEV', 'Veeva Systems'),
        ('VTR', 'Ventas'),
        ('VLTO', 'Veralto'),
        ('VRSN', 'Verisign'),
        ('VRSK', 'Verisk Analytics'),
        ('VZ', 'Verizon'),
        ('VRTX', 'Vertex Pharmaceuticals'),
        ('VRT', 'Vertiv'),
        ('VTRS', 'Viatris'),
        ('VICI', 'Vici Properties'),
        ('V', 'Visa Inc.'),
        ('VST', 'Vistra Corp.'),
        ('VMC', 'Vulcan Materials Company'),
        ('WRB', 'W. R. Berkley Corporation'),
        ('GWW', 'W. W. Grainger'),
        ('WAB', 'Wabtec'),
        ('WMT', 'Walmart'),
        ('DIS', 'Walt Disney Company (The)'),
        ('WBD', 'Warner Bros. Discovery'),
        ('WM', 'Waste Management'),
        ('WAT', 'Waters Corporation'),
        ('WEC', 'WEC Energy Group'),
        ('WFC', 'Wells Fargo'),
        ('WELL', 'Welltower'),
        ('WST', 'West Pharmaceutical Services'),
        ('WDC', 'Western Digital'),
        ('WY', 'Weyerhaeuser'),
        ('WSM', 'Williams-Sonoma, Inc.'),
        ('WMB', 'Williams Companies'),
        ('WTW', 'Willis Towers Watson'),
        ('WDAY', 'Workday, Inc.'),
        ('WYNN', 'Wynn Resorts'),
        ('XEL', 'Xcel Energy'),
        ('XYL', 'Xylem Inc.'),
        ('YUM', 'Yum! Brands'),
        ('ZBRA', 'Zebra Technologies'),
        ('ZBH', 'Zimmer Biomet'),
        ('ZTS', 'Zoetis'),
    ]

    # ── SPDR Select Sector ETFs — the 11 S&P 500 sector SPDRs, all USD ──────
    SPDR_SELECT_ETFS = [
        ('XLK',  'Technology Select Sector SPDR'),
        ('XLF',  'Financial Select Sector SPDR'),
        ('XLE',  'Energy Select Sector SPDR'),
        ('XLV',  'Health Care Select Sector SPDR'),
        ('XLI',  'Industrial Select Sector SPDR'),
        ('XLY',  'Consumer Discretionary Select Sector SPDR'),
        ('XLP',  'Consumer Staples Select Sector SPDR'),
        ('XLU',  'Utilities Select Sector SPDR'),
        ('XLB',  'Materials Select Sector SPDR'),
        ('XLRE', 'Real Estate Select Sector SPDR'),
        ('XLC',  'Communication Services Select Sector SPDR'),
    ]

    # ── MSCI ETFs — iShares MSCI single-country/regional funds, all USD ─────
    # Union of the constituents already used across VFUND_CONFIG's 3 MSCI-EW
    # funds, plus the 3 broad/flagship MSCI funds (EAFE, EM, ACWI) that
    # weren't already members of a VFund basket.
    MSCI_ETF_CONFIG = [
        ('EWA',  'iShares MSCI Australia'),
        ('EWC',  'iShares MSCI Canada'),
        ('EWD',  'iShares MSCI Sweden'),
        ('EWG',  'iShares MSCI Germany'),
        ('EWH',  'iShares MSCI Hong Kong'),
        ('EWI',  'iShares MSCI Italy'),
        ('EWJ',  'iShares MSCI Japan'),
        ('EWK',  'iShares MSCI Belgium'),
        ('EWL',  'iShares MSCI Switzerland'),
        ('EWM',  'iShares MSCI Malaysia'),
        ('EWN',  'iShares MSCI Netherlands'),
        ('EWO',  'iShares MSCI Austria'),
        ('EWP',  'iShares MSCI Spain'),
        ('EWQ',  'iShares MSCI France'),
        ('EWS',  'iShares MSCI Singapore'),
        ('EWT',  'iShares MSCI Taiwan'),
        ('EWU',  'iShares MSCI United Kingdom'),
        ('EWW',  'iShares MSCI Mexico'),
        ('EWY',  'iShares MSCI South Korea'),
        ('EWZ',  'iShares MSCI Brazil'),
        ('EIDO', 'iShares MSCI Indonesia'),
        ('EPHE', 'iShares MSCI Philippines'),
        ('THD',  'iShares MSCI Thailand'),
        ('EFA',  'iShares MSCI EAFE'),
        ('EEM',  'iShares MSCI Emerging Markets'),
        ('ACWI', 'iShares MSCI ACWI'),
    ]

    # ── FX pairs — currencies already used for MYR settlement conversion,
    # now directly playable as their own underlying (level = XXXMYR=X rate).
    FX_PAIRS = [
        ('USD', 'US Dollar'),
        ('JPY', 'Japanese Yen'),
        ('GBP', 'British Pound'),
        ('EUR', 'Euro'),
        ('SGD', 'Singapore Dollar'),
        ('HKD', 'Hong Kong Dollar'),
        ('KRW', 'South Korean Won'),
        ('CNY', 'Chinese Yuan'),
        ('AUD', 'Australian Dollar'),
        ('THB', 'Thai Baht'),
        ('IDR', 'Indonesian Rupiah'),
        ('PHP', 'Philippine Peso'),
        ('TWD', 'Taiwan Dollar'),
    ]

    def _zdws_fx_to_myr(self, ccy: str):
        """FX rate converting `ccy` into MYR. 1.0 for MYR itself.
        Shares the marquee's FX fetch so both aren't hitting Yahoo separately."""
        ccy = (ccy or 'MYR').upper()
        if ccy == 'MYR':
            return 1.0
        # 1. Marquee's in-memory value
        v = (getattr(self, '_mkt_data', {}) or {}).get(f'FX:{ccy}', (None, None))[0]
        if v:
            return float(v)
        # 2. Marquee's disk cache
        cached = cache_get_live_dict(f'tick_{ccy}MYR=X', _TTL_LIVE)
        if cached and cached.get('last'):
            return float(cached['last'])
        # 3. Cold path
        try:
            import yfinance as _yf
            t  = _yf.Ticker(f'{ccy}MYR=X')
            fi = t.fast_info
            r  = getattr(fi, 'last_price', None) or getattr(fi, 'lastPrice', None)
            if r is None:
                h = t.history(period='5d')
                if not h.empty:
                    r = float(h['Close'].iloc[-1])
            if r:
                cache_set_live_dict(f'tick_{ccy}MYR=X',
                                    {'last': float(r), 'prev': float(r)})
                return float(r)
        except Exception:
            pass
        return None

    def _zdws_world_level(self, symbol: str):
        """Latest level for a world index.
        Shares the marquee's cache (tick_*) so the ticker refresh and ZDWS
        don't each fetch the same symbol — one network call serves both.
        """
        # 1. Whatever the marquee last fetched, in memory
        v = (getattr(self, '_mkt_data', {}) or {}).get(symbol, (None, None))[0]
        if v:
            return float(v)
        # 2. The marquee's disk cache (5-min TTL)
        cached = cache_get_live_dict(f'tick_{symbol.replace("^", "")}', _TTL_LIVE)
        if cached and cached.get('last'):
            return float(cached['last'])
        # 3. Cold path — nothing cached yet, fetch it ourselves
        try:
            import yfinance as _yf
            t  = _yf.Ticker(symbol)
            fi = t.fast_info
            last = getattr(fi, 'last_price', None) or getattr(fi, 'lastPrice', None)
            prev = getattr(fi, 'previous_close', None) or getattr(fi, 'previousClose', None)
            if last is None:
                h = t.history(period='5d')
                if not h.empty:
                    last = float(h['Close'].iloc[-1])
                    prev = float(h['Close'].iloc[-2]) if len(h) > 1 else last
            if last:
                last = float(last)
                cache_set_live_dict(f'tick_{symbol.replace("^", "")}',
                                    {'last': last,
                                     'prev': float(prev) if prev else last})
                return last
        except Exception:
            pass
        return None

    def _zdws_cached_level_only(self, symbol: str):
        """Same lookup as _zdws_world_level but WITHOUT the cold single-ticker
        fetch fallback — used for large universes (S&P 500) where checking
        500 tickers one-at-a-time on every tab build would hang the UI.
        Returns None if nothing's cached yet; see _zdws_warm_batch_cache to
        populate the cache in one batched call instead."""
        v = (getattr(self, '_mkt_data', {}) or {}).get(symbol, (None, None))[0]
        if v:
            return float(v)
        cached = cache_get_live_dict(f'tick_{symbol.replace("^", "")}', _TTL_LIVE)
        if cached and cached.get('last'):
            return float(cached['last'])
        return None

    def _zdws_warm_batch_cache(self, symbols: list) -> int:
        """Batch-fetch a list of tickers in ONE yfinance call and write each
        into the same tick_* cache _zdws_world_level/_zdws_cached_level_only
        read from. This is what makes a 500+ ticker universe (S&P 500)
        practical — one network round trip instead of 500. Returns how many
        symbols were successfully cached."""
        if not symbols:
            return 0
        try:
            import yfinance as _yf
            data = _yf.download(symbols, period='5d', group_by='ticker',
                                progress=False, threads=True)
        except Exception as e:
            log(f"   ⚠️  Batch price warm failed: {e}")
            return 0
        n_cached = 0
        for sym in symbols:
            try:
                if len(symbols) == 1:
                    closes = data['Close'].dropna()
                else:
                    closes = data[sym]['Close'].dropna()
                if closes.empty:
                    continue
                last = float(closes.iloc[-1])
                prev = float(closes.iloc[-2]) if len(closes) > 1 else last
                cache_set_live_dict(f'tick_{sym.replace("^", "")}',
                                    {'last': last, 'prev': prev})
                n_cached += 1
            except Exception:
                continue
        return n_cached

    def _zdws_warm_us_universe(self, rebuild_tab=None):
        """Batch-warm the S&P 500 + SPDR + MSCI ticker cache in one shot,
        then optionally rebuild the calling tab so the newly-cached symbols
        appear in the underlying dropdown immediately."""
        tickers = ([t for t, _ in self.SP500_CONFIG] +
                  [t for t, _ in self.SPDR_SELECT_ETFS] +
                  [t for t, _ in self.MSCI_ETF_CONFIG])
        n = self._zdws_warm_batch_cache(tickers)
        messagebox.showinfo('US Universe',
            f'Loaded prices for {n}/{len(tickers)} US stocks & ETFs.\n'
            f'They\'ll now show up under US-Stock / US-ETF in the underlying list.',
            parent=self)
        if rebuild_tab:
            rebuild_tab()

    def _zdws_current_level(self, underlying: str):
        """Return current level for an underlying — index, KLCI, or individual stock."""
        try:
            if underlying == '^KLSE':
                # 1. Live ticker strip label
                v = (getattr(self, '_mkt_data', {}) or {}).get('^KLSE', (None, None))[0]
                if v:
                    return float(v)
                # 2. Disk cache fallback (5-min TTL, written by the ticker refresh)
                cached = cache_get_live_dict('market_indices_klci', _TTL_LIVE)
                if cached and '^KLSE' in cached:
                    v = cached['^KLSE'].get('last')
                    if v:
                        return float(v)
                return None
            # World index (Hang Seng, S&P 500, Nasdaq, Dow, Russell…)
            if underlying in {s for s, _, _ in self.WORLD_INDICES}:
                return self._zdws_world_level(underlying)
            # Internal index?
            df_i = self.all_frames.get(underlying)
            if df_i is not None and not df_i.empty:
                s = df_i[underlying].dropna() if underlying in df_i.columns else \
                    df_i.select_dtypes('number').iloc[:, 0].dropna()
                if not s.empty:
                    return float(s.iloc[-1])
            # Individual stock — look up latest price in stock_price_df
            spdf = getattr(self, 'stock_price_df', None)
            if spdf is not None and not spdf.empty and underlying in spdf.columns:
                col = spdf[underlying].dropna()
                if not col.empty:
                    return float(col.iloc[-1])
            # FX pair — level IS the XXXMYR=X rate itself (reuses the same
            # fetch/cache _zdws_fx_to_myr already uses for settlement conversion)
            if underlying in {c for c, _ in self.FX_PAIRS}:
                return self._zdws_fx_to_myr(underlying)
            # Generic bare US ticker (S&P 500 stock, SPDR/MSCI ETF, or any
            # future addition not covered above) — _zdws_world_level works
            # for any yfinance symbol, not just the World Indices it was
            # originally written for.
            return self._zdws_world_level(underlying)
        except Exception:
            return None

    def _zdws_move_pct(self, w: dict, cur_level: float):
        """% move a MYR wallet actually felt on this position.
        MYR underlying  -> plain index move.
        Foreign         -> index move compounded with the FX move.
        Returns None if the FX leg can't be priced right now."""
        entry = w.get('entry') or 0
        if not entry or cur_level is None:
            return None
        ccy = w.get('ccy', 'MYR')
        if ccy == 'MYR':
            return (cur_level / entry - 1) * 100
        entry_fx = w.get('entry_fx')
        cur_fx   = self._zdws_fx_to_myr(ccy)
        if not entry_fx or not cur_fx:
            return None
        return ((cur_level * cur_fx) / (entry * entry_fx) - 1) * 100

    def _zdws_auto_settle(self, pts: dict) -> list:
        """Settle all ZDWS positions whose settlement date has passed.
        Returns list of settlement result messages."""
        import datetime as _dt
        today    = _dt.date.today()
        messages = []
        still_open = []
        for w in pts.get('zdws_active', []):
            try:
                settle_dt = _dt.datetime.strptime(w['settle_date'], '%d/%m/%Y').date()
            except Exception:
                still_open.append(w); continue
            if settle_dt > today:
                still_open.append(w); continue
            # Settlement due — get current level
            cur = self._zdws_current_level(w['underlying'])
            move_pct = self._zdws_move_pct(w, cur)
            if move_pct is None:
                still_open.append(w); continue   # can't price it yet, keep open
            direction_ok = (move_pct >= 0) if w['side'] == 'BULL' else (move_pct <= 0)
            if direction_ok:
                payout = round(w['stake'] * (1 + abs(move_pct) / 100 * 10), 2)
                pts['credits'] += payout
                w['payout'] = payout
                w['result'] = 'WIN'
                messages.append(
                    f"🟢 ZDWS WIN: {w['side']} {w['underlying']} "
                    f"{move_pct:+.2f}% → +{payout:,.0f} credits")
            else:
                w['payout'] = 0.0
                w['result'] = 'LOSS'
                messages.append(
                    f"🔴 ZDWS LOSS: {w['side']} {w['underlying']} "
                    f"{move_pct:+.2f}% → -{w['stake']:,.0f} credits")
            w['settled_level'] = cur
            w['move_pct']      = round(move_pct, 4)
            pts['zdws_history'].append(w)
        pts['zdws_active'] = still_open
        return messages

    def _zdws_open_position(self, underlying: str, side: str, stake: float,
                             settle_date_str: str) -> bool:
        """Open a new ZDWS position. Returns True on success."""
        pts = self._pts_load()
        if stake < self.PTS_MIN_STAKE or stake > pts['credits']:
            return False
        entry = self._zdws_current_level(underlying)
        if entry is None:
            return False
        # Credits are MYR. For a foreign underlying we snapshot the FX rate at
        # entry so settlement can measure the move the wallet actually felt:
        #   total% = index% + FX%   (MYR-composite, not quanto)
        ccy      = self._zdws_ccy(underlying)
        entry_fx = self._zdws_fx_to_myr(ccy)
        if entry_fx is None:
            return False          # no FX -> can't price it honestly, refuse
        import datetime as _dt
        pts['credits'] -= stake
        pts['zdws_active'].append({
            'id':          len(pts['zdws_active']) + len(pts['zdws_history']) + 1,
            'underlying':  underlying,
            'side':        side,          # 'BULL' or 'BEAR'
            'stake':       stake,
            'entry':       entry,
            'ccy':         ccy,
            'entry_fx':    entry_fx,
            'opened':      _dt.date.today().strftime('%d/%m/%Y'),
            'settle_date': settle_date_str,
            'result':      None,
            'payout':      0.0,
        })
        self._pts_save(pts)
        return True

    def _zdws_market_close(self, warrant_id: int):
        """Close a ZDWS position early at current market level (before settlement)."""
        pts = self._pts_load()
        pos = next((w for w in pts['zdws_active'] if w['id'] == warrant_id), None)
        if not pos:
            return
        cur      = self._zdws_current_level(pos['underlying'])
        move_pct = self._zdws_move_pct(pos, cur)
        if move_pct is None:
            messagebox.showwarning('ZDWS',
                'Cannot price this position right now (level or FX unavailable).\n'
                'Try again in a moment.', parent=self)
            return
        direction_ok = (move_pct >= 0) if pos['side'] == 'BULL' else (move_pct <= 0)
        if direction_ok:
            payout = round(pos['stake'] * (1 + abs(move_pct) / 100 * 10), 2)
            pts['credits'] += payout
            pos['result'] = 'WIN (early close)'
            pos['payout'] = payout
            msg = f'🟢 Closed early: {move_pct:+.2f}% in your favour → +{payout:,.0f} credits'
        else:
            # Early close on a losing position: refund 50% of stake
            refund = round(pos['stake'] * 0.5, 2)
            pts['credits'] += refund
            pos['result'] = 'LOSS (early close, 50% refund)'
            pos['payout'] = refund
            msg = f'🔴 Closed early against you ({move_pct:+.2f}%) → 50% refund: +{refund:,.0f} credits'
        pos['settled_level'] = cur
        pos['move_pct']      = round(move_pct, 4)
        pts['zdws_active']   = [w for w in pts['zdws_active'] if w['id'] != warrant_id]
        pts['zdws_history'].append(pos)
        self._pts_save(pts)
        messagebox.showinfo('ZDWS', msg, parent=self)
        self._build_pts_tab()

    # =========================================================================
    # ZDOS — Zero-Decay Option System
    # Same Θ=0 philosophy as ZDWS (pricing has no time-decay component — value
    # is always a direct function of current distance-to-strike, never a
    # function of time-to-settlement) but structured as real strikes instead
    # of a plain directional bet:
    #   - AMERICAN-STYLE PRICING:  mark-to-model value at any moment depends
    #     only on where the underlying sits relative to the strike right now.
    #   - EUROPEAN-STYLE SETTLEMENT: the binding result is only ever
    #     determined on the settlement date snapshot (auto-settle), same
    #     mechanism as ZDWS. Early close (below) is a convenience cash-out
    #     at the current mark, not an exercise.
    #   - 4 position types per tier: LONG_CALL / SHORT_CALL / LONG_PUT /
    #     SHORT_PUT — "C/S or S/C" — Call-or-Put, Long-or-Short.
    #   - 10 TIERS: each tier fixes BOTH the strike's %-distance from spot
    #     AND its win multiplier, for the long (buyer) and short (seller)
    #     side of that same strike.
    # NOTE: the multiplier curve below is a first-pass, deliberately kept in
    # one flat table so it's trivial to retune without touching any logic —
    # calibrate mult_long/mult_short once real usage data exists.
    # =========================================================================
    ZDOS_TIERS = [
        # tier, pct OTM from spot, mult if LONG (buyer) side wins, mult if SHORT (seller) side wins
        {'tier': 1,  'pct': 1.0,  'mult_long': 3.0,  'mult_short': 1.30},
        {'tier': 2,  'pct': 2.0,  'mult_long': 4.5,  'mult_short': 1.25},
        {'tier': 3,  'pct': 3.0,  'mult_long': 6.0,  'mult_short': 1.20},
        {'tier': 4,  'pct': 4.0,  'mult_long': 7.5,  'mult_short': 1.15},
        {'tier': 5,  'pct': 5.0,  'mult_long': 9.0,  'mult_short': 1.12},
        {'tier': 6,  'pct': 6.0,  'mult_long': 11.0, 'mult_short': 1.10},
        {'tier': 7,  'pct': 7.0,  'mult_long': 13.0, 'mult_short': 1.08},
        {'tier': 8,  'pct': 8.0,  'mult_long': 15.5, 'mult_short': 1.06},
        {'tier': 9,  'pct': 9.0,  'mult_long': 18.0, 'mult_short': 1.04},
        {'tier': 10, 'pct': 10.0, 'mult_long': 21.0, 'mult_short': 1.02},
    ]
    ZDOS_POSTYPES = ['LONG_CALL', 'SHORT_CALL', 'LONG_PUT', 'SHORT_PUT']

    # ── Multi-leg strategy templates ────────────────────────────────────────
    # Each leg is (postype, tier_offset) — tier_offset is relative to a
    # user-chosen base tier, clamped into 1..10. A strategy is just N ordinary
    # single-leg ZDOS positions opened together under one strategy_id, each
    # settled/closed with the exact same per-leg logic as a standalone
    # position — nothing new needed there, only the combined open/grouping.
    #
    # NOTE on Covered Call / Covered Put: a real covered call is long the
    # underlying + short a call, and a covered put is short the underlying +
    # short a put. ZDOS has no underlying-ownership leg (it's option-only,
    # credit-settled), so there's no way to build the actual covered
    # position here. What's offered instead is the INCOME leg alone (short
    # call / short put) — same direction of bet a covered position expresses,
    # but without the underlying leg's capped-loss/breakeven-shift behaviour.
    # Flagged in the UI so it's never mistaken for the real thing.
    ZDOS_STRATEGIES = {
        'Single Leg':        None,   # manual — uses the position+tier picker directly
        'Covered Call (income leg only)': [('SHORT_CALL', 0)],
        'Covered Put (income leg only)':  [('SHORT_PUT', 0)],
        'Bull Call Spread':  [('LONG_CALL', 0), ('SHORT_CALL', 2)],
        'Bear Put Spread':   [('LONG_PUT', 0), ('SHORT_PUT', 2)],
        'Straddle (long)':   [('LONG_CALL', 0), ('LONG_PUT', 0)],
        'Strangle (long)':   [('LONG_CALL', 2), ('LONG_PUT', 2)],
        'Iron Condor':       [('SHORT_CALL', 0), ('LONG_CALL', 2),
                               ('SHORT_PUT', 0), ('LONG_PUT', 2)],
    }

    def _zdos_strategy_legs(self, strategy: str, base_tier: int) -> list:
        """Resolve a strategy template into concrete (postype, tier) legs,
        clamping tier offsets into the valid 1..10 range."""
        tmpl = self.ZDOS_STRATEGIES.get(strategy)
        if not tmpl:
            return []
        n = len(self.ZDOS_TIERS)
        return [(pt, min(max(base_tier + off, 1), n)) for pt, off in tmpl]

    def _zdos_tier(self, tier_num: int) -> dict:
        return next((t for t in self.ZDOS_TIERS if t['tier'] == tier_num), self.ZDOS_TIERS[0])

    def _zdos_strike_level(self, entry: float, tier_num: int, postype: str) -> float:
        """Strike level in the underlying's own units (not %). Calls strike
        above spot, Puts strike below — same distance regardless of long/short,
        since long/short is just which side of that strike you're betting on."""
        pct = self._zdos_tier(tier_num)['pct']
        is_call = 'CALL' in postype
        return entry * (1 + pct / 100) if is_call else entry * (1 - pct / 100)

    def _zdos_is_winning(self, pos: dict, move_pct) -> bool:
        """Win condition at the current move_pct (MYR-composite, same as ZDWS).
        move_pct > 0 means the underlying is above entry; the tier's pct is
        the strike distance in the same terms."""
        if move_pct is None:
            return False
        pct = self._zdos_tier(pos['tier'])['pct']
        pt  = pos['postype']
        if pt == 'LONG_CALL':   return move_pct > pct
        if pt == 'SHORT_CALL':  return move_pct <= pct
        if pt == 'LONG_PUT':    return move_pct < -pct
        if pt == 'SHORT_PUT':   return move_pct >= -pct
        return False

    def _zdos_multiplier(self, pos: dict) -> float:
        t = self._zdos_tier(pos['tier'])
        return t['mult_long'] if pos['postype'].startswith('LONG') else t['mult_short']

    def _zdos_auto_settle(self, pts: dict) -> list:
        """Settle all ZDOS positions whose settlement date has passed.
        European-style: this is the only point a position's result is ever
        finalised (barring a voluntary early close)."""
        import datetime as _dt
        today      = _dt.date.today()
        messages   = []
        still_open = []
        for o in pts.get('zdos_active', []):
            try:
                settle_dt = _dt.datetime.strptime(o['settle_date'], '%d/%m/%Y').date()
            except Exception:
                still_open.append(o); continue
            if settle_dt > today:
                still_open.append(o); continue
            cur      = self._zdws_current_level(o['underlying'])
            move_pct = self._zdws_move_pct(o, cur)
            if move_pct is None:
                still_open.append(o); continue   # can't price it yet, keep open
            if self._zdos_is_winning(o, move_pct):
                payout = round(o['stake'] * self._zdos_multiplier(o), 2)
                pts['credits'] += payout
                o['payout'] = payout
                o['result'] = 'WIN'
                messages.append(
                    f"📐 ZDOS WIN: {o['postype']} T{o['tier']} {o['underlying']} "
                    f"{move_pct:+.2f}% → +{payout:,.0f} credits")
            else:
                o['payout'] = 0.0
                o['result'] = 'LOSS'
                messages.append(
                    f"🔴 ZDOS LOSS: {o['postype']} T{o['tier']} {o['underlying']} "
                    f"{move_pct:+.2f}% → -{o['stake']:,.0f} credits")
            o['settled_level'] = cur
            o['move_pct']      = round(move_pct, 4)
            pts['zdos_history'].append(o)
        pts['zdos_active'] = still_open
        return messages

    def _zdos_open_position(self, underlying: str, postype: str, tier_num: int,
                             stake: float, settle_date_str: str,
                             pts: dict = None, strategy: str = None,
                             strategy_id: str = None) -> bool:
        """Open a new ZDOS position. Returns True on success.
        pts: pass an already-loaded wallet to open as one leg of a multi-leg
        strategy without it being independently loaded/saved (caller saves
        once after all legs are appended) — see _zdos_open_strategy."""
        owns_pts = pts is None
        if owns_pts:
            pts = self._pts_load()
        if stake < self.PTS_MIN_STAKE or stake > pts['credits']:
            return False
        entry = self._zdws_current_level(underlying)
        if entry is None:
            return False
        ccy      = self._zdws_ccy(underlying)
        entry_fx = self._zdws_fx_to_myr(ccy)
        if entry_fx is None:
            return False
        import datetime as _dt
        pts['credits'] -= stake
        pts['zdos_active'].append({
            'id':          len(pts['zdos_active']) + len(pts['zdos_history']) + 1,
            'underlying':  underlying,
            'postype':     postype,       # LONG_CALL / SHORT_CALL / LONG_PUT / SHORT_PUT
            'tier':        tier_num,
            'strike':      self._zdos_strike_level(entry, tier_num, postype),
            'stake':       stake,
            'entry':       entry,
            'ccy':         ccy,
            'entry_fx':    entry_fx,
            'opened':      _dt.date.today().strftime('%d/%m/%Y'),
            'settle_date': settle_date_str,
            'result':      None,
            'payout':      0.0,
            'strategy':    strategy,      # e.g. 'Iron Condor', or None for a plain single leg
            'strategy_id': strategy_id,   # groups legs of the same strategy together
        })
        if owns_pts:
            self._pts_save(pts)
        return True

    def _zdos_open_strategy(self, underlying: str, strategy: str, base_tier: int,
                             total_stake: float, settle_date_str: str) -> tuple:
        """Open every leg of a named multi-leg strategy as one atomic trade,
        stake split evenly across legs. Returns (ok: bool, message: str)."""
        legs = self._zdos_strategy_legs(strategy, base_tier)
        if not legs:
            return False, 'Unknown strategy or no legs to open.'
        leg_stake = round(total_stake / len(legs), 2)
        if leg_stake < self.PTS_MIN_STAKE:
            return False, (f'Stake too small once split across {len(legs)} legs — '
                           f'each leg needs at least {self.PTS_MIN_STAKE:,.0f} credits '
                           f'(={self.PTS_MIN_STAKE * len(legs):,.0f} total minimum).')
        pts = self._pts_load()
        if total_stake > pts['credits']:
            return False, 'Insufficient credits for the full strategy.'
        import datetime as _dt, uuid as _uuid
        sid = _uuid.uuid4().hex[:8]
        for postype, tier in legs:
            ok = self._zdos_open_position(underlying, postype, tier, leg_stake,
                                          settle_date_str, pts=pts,
                                          strategy=strategy, strategy_id=sid)
            if not ok:
                return False, f'Failed opening the {postype.replace("_"," ").title()} leg.'
        self._pts_save(pts)
        leg_desc = ', '.join(f'{pt.replace("_"," ").title()} T{t}' for pt, t in legs)
        return True, f'{strategy} written: {leg_desc}  ·  {leg_stake:,.0f} credits/leg'

    def _zdos_market_close(self, position_id: int, silent: bool = False):
        """Close a ZDOS position early at its current mark-to-model value.
        Θ=0 pricing means the mark is the same win/lose test used at
        settlement, evaluated right now instead of waiting — full multiplier
        if currently winning, flat 50% stake refund if not (same early-close
        convention as ZDWS, for consistency across both desks).
        silent=True skips the popup/tab-rebuild and just returns the result
        message — used when closing every leg of a strategy at once so the
        person gets one combined summary instead of N popups."""
        pts = self._pts_load()
        pos = next((o for o in pts['zdos_active'] if o['id'] == position_id), None)
        if not pos:
            return None
        cur      = self._zdws_current_level(pos['underlying'])
        move_pct = self._zdws_move_pct(pos, cur)
        if move_pct is None:
            msg = 'Cannot price this position right now (level or FX unavailable).'
            if not silent:
                messagebox.showwarning('ZDOS', msg + '\nTry again in a moment.', parent=self)
            return msg
        if self._zdos_is_winning(pos, move_pct):
            payout = round(pos['stake'] * self._zdos_multiplier(pos), 2)
            pts['credits'] += payout
            pos['result'] = 'WIN (early close)'
            pos['payout'] = payout
            msg = f'📐 Closed early ITM: {move_pct:+.2f}% → +{payout:,.0f} credits'
        else:
            refund = round(pos['stake'] * 0.5, 2)
            pts['credits'] += refund
            pos['result'] = 'LOSS (early close, 50% refund)'
            pos['payout'] = refund
            msg = f'🔴 Closed early OTM ({move_pct:+.2f}%) → 50% refund: +{refund:,.0f} credits'
        pos['settled_level'] = cur
        pos['move_pct']      = round(move_pct, 4)
        pts['zdos_active']   = [o for o in pts['zdos_active'] if o['id'] != position_id]
        pts['zdos_history'].append(pos)
        self._pts_save(pts)
        if not silent:
            messagebox.showinfo('ZDOS', msg, parent=self)
            self._build_zdos_tab()
        return msg

    def _zdos_close_strategy(self, position_ids: list):
        """Close every leg of a strategy bundle in one action, then show one
        combined summary instead of a popup per leg."""
        msgs = [self._zdos_market_close(pid, silent=True) for pid in position_ids]
        summary = '\n'.join(m for m in msgs if m)
        messagebox.showinfo('ZDOS', f'Strategy closed — {len(position_ids)} legs:\n\n{summary}',
                            parent=self)
        self._build_zdos_tab()

    def _pts_check_expired(self, pts: dict) -> list:
        """Return list of open predictions whose deadline has passed."""
        import datetime as _dt
        today   = _dt.date.today()
        expired = []
        for p in pts.get('predictions', []):
            if p.get('status') != 'open':
                continue
            try:
                dl = _dt.datetime.strptime(p['deadline'], '%d/%m/%Y').date()
                if dl <= today:
                    expired.append(p)
            except Exception:
                pass
        return expired

    def _build_pts_tab(self):
        """Build (or rebuild) the PTS tab content."""
        import datetime as _dt
        tab = self.tab_pts
        for w in tab.winfo_children():
            w.destroy()

        pts = self._pts_load()

        BG   = '#0d1117'; PANEL = '#161b22'
        TEAL = '#00E5A0'; GOLD  = '#FFD700'
        FG   = '#cdd9e5'; DIM   = '#8b949e'
        RED  = '#F85149'; GREEN = '#3FB950'
        FONT = ('Segoe UI', 9)

        # ── Header strip ──────────────────────────────────────────────────────
        hdr = tk.Frame(tab, bg=BG)
        hdr.pack(fill='x', padx=12, pady=(10, 6))

        tk.Label(hdr, text='🎯  PredictTheShares',
                 font=('Segoe UI', 14, 'bold'), fg=TEAL, bg=BG).pack(side='left')
        tk.Label(hdr, text='  Virtual Prediction Market',
                 font=('Segoe UI', 10), fg=DIM, bg=BG).pack(side='left')
        tk.Label(hdr, text=f'  🆔 {CURRENT_KID or "guest"}',
                 font=('Segoe UI', 8, 'bold'), fg='#58A6FF', bg=BG).pack(side='left')
        # Refresh button — rebuilds tab + regenerates markets with live data
        tk.Button(hdr, text='↻ Refresh', font=('Segoe UI', 8, 'bold'),
                  bg='#21262d', fg=TEAL, relief='flat', padx=10, pady=3,
                  cursor='hand2',
                  command=self._build_pts_tab).pack(side='left', padx=(14, 0))

        # Credits + rank on the right
        rank_label, rank_color = self._pts_rank(pts)
        credits = pts.get('credits', self.PTS_STARTING_CREDITS)
        info = tk.Frame(hdr, bg=BG)
        info.pack(side='right')
        tk.Label(info, text=rank_label, font=('Segoe UI', 9, 'bold'),
                 fg=rank_color, bg=BG).pack(side='right', padx=(8, 0))
        self._pts_credits_var = tk.StringVar(value=f'💰 {credits:,.0f} PTS Credits')
        tk.Label(info, textvariable=self._pts_credits_var,
                 font=('Segoe UI', 10, 'bold'), fg=GOLD, bg=BG).pack(side='right', padx=(0, 12))

        # ── Expired predictions banner (auto-resolve prompt) ──────────────────
        expired = self._pts_check_expired(pts)
        if expired:
            exp_banner = tk.Frame(tab, bg='#2a1a0a',
                                  highlightbackground='#D29922', highlightthickness=1,
                                  padx=12, pady=8)
            exp_banner.pack(fill='x', padx=12, pady=(0, 6))
            tk.Label(exp_banner,
                     text=f'⏰  {len(expired)} prediction{"s" if len(expired)!=1 else ""} '
                          f'past deadline — resolve now to claim results. '
                          f'(Select in Active Predictions → Mark as Resolved)',
                     font=('Segoe UI', 8, 'bold'), fg='#D29922', bg='#2a1a0a',
                     wraplength=900, justify='left').pack(anchor='w')

        # ── Stats row ─────────────────────────────────────────────────────────
        stats = tk.Frame(tab, bg=PANEL, highlightbackground='#21262d', highlightthickness=1)
        stats.pack(fill='x', padx=12, pady=(0, 8))

        total   = pts.get('total_made', 0)
        correct = pts.get('total_correct', 0)
        active  = len([p for p in pts.get('predictions', []) if p.get('status') == 'open'])
        acc     = (correct / total * 100) if total else 0.0
        won_cred = sum(p.get('payout', 0) for p in pts.get('history', []) if p.get('correct'))
        lost_cred= sum(p.get('stake', 0)  for p in pts.get('history', []) if not p.get('correct'))

        for label, val, col in [
            ('Active Predictions', str(active),          TEAL),
            ('Total Made',         str(total),            FG),
            ('Correct',            str(correct),          GREEN),
            ('Accuracy',           f'{acc:.1f}%',         GREEN if acc >= 50 else RED),
            ('Credits Won',        f'{won_cred:,.0f}',    GREEN),
            ('Credits Lost',       f'{lost_cred:,.0f}',   RED),
        ]:
            c = tk.Frame(stats, bg=PANEL, padx=16, pady=8)
            c.pack(side='left')
            tk.Label(c, text=label, font=('Segoe UI', 7), fg=DIM, bg=PANEL).pack()
            tk.Label(c, text=val,   font=('Segoe UI', 11, 'bold'), fg=col, bg=PANEL).pack()

        # ── Main body: left panel (make prediction) + right panel (active) ───
        body = tk.Frame(tab, bg=BG)
        body.pack(fill='both', expand=True, padx=12, pady=(0, 8))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)   # critical — without this, grid children have zero height

        # ── LEFT: New Prediction form ─────────────────────────────────────────
        left = tk.Frame(body, bg=PANEL, highlightbackground='#21262d', highlightthickness=1,
                        padx=14, pady=10)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 6))

        tk.Label(left, text='➕  NEW PREDICTION',
                 font=('Segoe UI', 9, 'bold'), fg=TEAL, bg=PANEL).pack(anchor='w', pady=(0, 4))

        # Auto-generate market suggestions — wrapped in try/except so a crash
        # (e.g. data not yet loaded) shows a helpful message instead of blank panel
        try:
            markets = self._pts_generate_markets()
        except Exception as _e:
            markets = []
            tk.Label(left, text=f'⚠ Market generation error:\n{_e}',
                     font=('Segoe UI', 8), fg=RED, bg=PANEL,
                     wraplength=280, justify='left').pack(anchor='w', pady=4)
        self._pts_markets = markets

        tk.Label(left, text='Select Market', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')

        # ── Category filter — jump straight to ZeroDecay Warrants etc. ────────
        categories = ['ALL'] + sorted({m['category'] for m in markets})
        cat_filter_var = tk.StringVar(value='ALL')

        def _labels_for_category(cat):
            out = []
            for i, m in enumerate(markets):
                if cat != 'ALL' and m['category'] != cat:
                    continue
                short_q = m['question'][:52] + '…' if len(m['question']) > 52 else m['question']
                # Special icon for ZeroDecay so it stands out
                ico = '⚡ ' if m['category'] == 'ZeroDecay Warrant' else ''
                out.append((i, f"{ico}[{m['category'][:8]}] {short_q}"))
            return out

        cat_row = tk.Frame(left, bg=PANEL)
        cat_row.pack(fill='x', pady=(2, 2))
        tk.Label(cat_row, text='Filter:', font=('Segoe UI', 7), fg=DIM,
                 bg=PANEL).pack(side='left', padx=(0, 4))
        cat_cb = ttk.Combobox(cat_row, textvariable=cat_filter_var, values=categories,
                              state='readonly', font=('Segoe UI', 7), width=22)
        cat_cb.pack(side='left', fill='x', expand=True)

        # Market picker — shows category + truncated question
        self._pts_filtered_idx = [i for i, _ in _labels_for_category('ALL')]
        mkt_labels = [lbl for _, lbl in _labels_for_category('ALL')]

        mkt_var = tk.StringVar()
        mkt_cb  = ttk.Combobox(left, textvariable=mkt_var, values=mkt_labels,
                                state='readonly', font=('Segoe UI', 8), width=36)
        mkt_cb.pack(fill='x', pady=(2, 4))
        if mkt_labels:
            mkt_cb.current(0)

        def _on_cat_filter(*_):
            pairs = _labels_for_category(cat_filter_var.get())
            self._pts_filtered_idx = [i for i, _ in pairs]
            mkt_cb['values'] = [lbl for _, lbl in pairs]
            if pairs:
                mkt_cb.current(0)
                _update_market_ui()
        cat_cb.bind('<<ComboboxSelected>>', _on_cat_filter)

        # Context label — shows supporting data for the selected market
        ctx_var = tk.StringVar(value='')
        tk.Label(left, textvariable=ctx_var, font=('Segoe UI', 7),
                 fg='#D29922', bg=PANEL, wraplength=280,
                 justify='left').pack(anchor='w', pady=(0, 6))

        # Full question display (read-only, auto-filled from selection)
        tk.Label(left, text='Question', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')
        q_var = tk.StringVar()
        q_lbl = tk.Label(left, textvariable=q_var, font=('Segoe UI', 8),
                         fg=FG, bg=PANEL, wraplength=280,
                         justify='left', anchor='w')
        q_lbl.pack(fill='x', pady=(0, 6))

        # Position selector — changes dynamically based on market type
        tk.Label(left, text='Your Position', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')
        pos_frame = tk.Frame(left, bg=PANEL)
        pos_frame.pack(fill='x', pady=(2, 8))
        pos_var = tk.StringVar(value='YES')
        pos_btns = []   # rebuilt on market change

        def _render_question(m, level_opt=None):
            """Resolve {level} and {deadline} placeholders into the live question.
            {deadline} always reflects the settlement period currently selected,
            so the question can never show a stale hardcoded date."""
            q = m['question']
            if '{level}' in q:
                opt = level_opt if level_opt in m.get('options', []) else \
                      (m.get('options') or ['—'])[-1]
                q = q.replace('{level}', opt)
            if '{deadline}' in q:
                # Late-bound: the settlement picker is built further down, so
                # during the first paint fall back to a neutral word.
                d = None
                try:
                    d = _settle_map.get(period_var.get())
                except NameError:
                    pass
                q = q.replace('{deadline}',
                              d.strftime('%d/%m/%Y') if d else 'settlement date')
            return q

        def _update_market_ui(*_):
            fi = mkt_cb.current()
            if fi < 0 or fi >= len(self._pts_filtered_idx):
                return
            sel_idx = self._pts_filtered_idx[fi]
            if sel_idx < 0 or sel_idx >= len(markets):
                return
            m = markets[sel_idx]
            ctx_var.set(m.get('context', ''))
            # Rebuild position buttons first so pos_var holds a valid option
            for w in pos_frame.winfo_children():
                w.destroy()
            opts = m['options']
            pos_var.set(m.get('suggested_position') or opts[0])
            for opt in opts:
                col = GREEN if opt == 'YES' else RED if opt == 'NO' else TEAL
                rb = tk.Radiobutton(pos_frame, text=opt, variable=pos_var,
                                    value=opt, command=lambda: _update_q_text(),
                                    bg=PANEL, fg=col, selectcolor=PANEL,
                                    activebackground=PANEL, activeforeground=col,
                                    font=('Segoe UI', 8, 'bold'), relief='flat',
                                    cursor='hand2', indicatoron=False,
                                    padx=8, pady=3)
                rb.pack(side='left', padx=(0, 3))
            q_var.set(_render_question(m, pos_var.get()))

        def _update_q_text(*_):
            fi = mkt_cb.current()
            if fi < 0 or fi >= len(self._pts_filtered_idx): return
            sel_idx = self._pts_filtered_idx[fi]
            if sel_idx < 0 or sel_idx >= len(markets): return
            q_var.set(_render_question(markets[sel_idx], pos_var.get()))

        mkt_cb.bind('<<ComboboxSelected>>', _update_market_ui)
        pos_var.trace_add('write', _update_q_text)
        if markets:
            _update_market_ui()

        # Stake
        tk.Label(left, text='Stake (PTS Credits)', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')
        stake_var = tk.StringVar(value='1000')
        tk.Entry(left, textvariable=stake_var, font=FONT,
                 bg='#0d1117', fg=GOLD, insertbackground=GOLD,
                 relief='flat', highlightbackground='#30363d',
                 highlightthickness=1).pack(fill='x', pady=(2, 2))

        # Implied odds + wallet pct
        odds_var = tk.StringVar(value='')
        tk.Label(left, textvariable=odds_var, font=('Segoe UI', 7, 'bold'),
                 fg=TEAL, bg=PANEL).pack(anchor='w', pady=(0, 4))

        def _preview_odds(*_):
            try:
                s = float(stake_var.get())
                c = pts.get('credits', 1)
                pct = min(100, s / c * 100) if c else 0
                odds_var.set(f'{s:,.0f} credits  ·  {pct:.1f}% of wallet  ·  '
                             f'Win payout: {s*1.8:,.0f} credits (1.8×)')
            except Exception:
                odds_var.set('')
        stake_var.trace_add('write', _preview_odds)
        _preview_odds()

        # ── Resolution period picker ───────────────────────────────────────
        tk.Label(left, text='Resolution Period', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')

        _settle_dates = self._pts_settlement_dates()
        _settle_labels = [lbl for lbl, _ in _settle_dates]
        _settle_map    = {lbl: d for lbl, d in _settle_dates}

        period_var = tk.StringVar()
        period_cb  = ttk.Combobox(left, textvariable=period_var,
                                   values=_settle_labels,
                                   state='readonly', font=('Segoe UI', 8), width=36)
        period_cb.pack(fill='x', pady=(2, 2))
        # Default to first quarterly settlement ≥ 7 days out
        _default_idx = 0
        for i, (lbl, _) in enumerate(_settle_dates):
            if 'Quarterly' in lbl:
                _default_idx = i
                break
        if _settle_labels:
            period_cb.current(_default_idx)

        # Show the resolved date + days-to-settlement
        settle_info_var = tk.StringVar(value='')
        tk.Label(left, textvariable=settle_info_var,
                 font=('Segoe UI', 7), fg='#D29922', bg=PANEL).pack(anchor='w', pady=(0, 6))

        def _update_settle_info(*_):
            lbl = period_var.get()
            d   = _settle_map.get(lbl)
            if d:
                days = (d - _dt.date.today()).days
                settle_info_var.set(
                    f'Settles: {d.strftime("%A, %d %b %Y")}  ·  {days} days from today')
            else:
                settle_info_var.set('')
            # Question embeds the settlement date — re-render it
            _update_q_text()

        period_cb.bind('<<ComboboxSelected>>', _update_settle_info)
        if _settle_labels:
            _update_settle_info()

        def _submit_prediction():
            question = q_var.get().strip()
            if not question or question == '—':
                messagebox.showwarning('PTS', 'Please select a market first.', parent=self)
                return
            try:
                stake = float(stake_var.get())
            except ValueError:
                messagebox.showwarning('PTS', 'Invalid stake amount.', parent=self)
                return
            if stake < self.PTS_MIN_STAKE:
                messagebox.showwarning('PTS',
                    f'Minimum stake is {self.PTS_MIN_STAKE:,.0f} credits.', parent=self)
                return
            pts2 = self._pts_load()
            if stake > pts2['credits']:
                messagebox.showwarning('PTS',
                    f'Insufficient credits.\nYou have {pts2["credits"]:,.0f} PTS.', parent=self)
                return
            lbl_sel  = period_var.get()
            deadline = _settle_map.get(lbl_sel)
            if deadline is None:
                messagebox.showwarning('PTS', 'Please select a resolution period.', parent=self)
                return
            days_to = (deadline - _dt.date.today()).days
            if days_to < 7:
                messagebox.showwarning('PTS',
                    f'Minimum resolution period is 7 days.\n'
                    f'Selected date is only {days_to} day(s) away.', parent=self)
                return
            fi = mkt_cb.current()
            sel_idx = self._pts_filtered_idx[fi] if 0 <= fi < len(self._pts_filtered_idx) else -1
            cat = markets[sel_idx]['category'] if 0 <= sel_idx < len(markets) else 'Custom'

            pred = {
                'id':         len(pts2['predictions']) + len(pts2['history']) + 1,
                'category':   cat,
                'question':   question,
                'type':       markets[sel_idx]['type'] if 0 <= sel_idx < len(markets) else 'YES/NO',
                'position':   pos_var.get(),
                'stake':      stake,
                'deadline':   deadline.strftime('%d/%m/%Y'),
                'period_lbl': lbl_sel,
                'created':    _dt.date.today().strftime('%d/%m/%Y'),
                'status':     'open',
                'payout':     0.0,
                'correct':    None,
                'source':     markets[sel_idx].get('source', '') if 0 <= sel_idx < len(markets) else '',
            }
            pts2['predictions'].append(pred)
            pts2['credits']    -= stake
            pts2['total_made'] += 1
            self._pts_save(pts2)
            messagebox.showinfo('PTS',
                f'🎯 Prediction submitted!\n\n'
                f'"{question}"\n\n'
                f'Position: {pred["position"]}  |  Stake: {stake:,.0f} credits\n'
                f'Deadline: {pred["deadline"]}\n'
                f'Remaining credits: {pts2["credits"]:,.0f}',
                parent=self)
            self._build_pts_tab()

        tk.Button(left, text='🎯  Submit Prediction',
                  font=('Segoe UI', 10, 'bold'),
                  bg=TEAL, fg='#000', relief='flat',
                  padx=12, pady=8, cursor='hand2',
                  command=_submit_prediction).pack(fill='x', pady=(4, 0))

        # ── RIGHT: Active predictions + history ───────────────────────────────
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky='nsew', padx=(6, 0))
        right.rowconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        # Active predictions treeview
        act_lbl = tk.Frame(right, bg=BG)
        act_lbl.grid(row=0, column=0, sticky='new')
        tk.Label(act_lbl, text='📋  ACTIVE PREDICTIONS',
                 font=('Segoe UI', 9, 'bold'), fg=TEAL, bg=BG).pack(side='left', pady=(0, 4))

        act_frame = tk.Frame(right, bg=BG)
        act_frame.grid(row=0, column=0, sticky='nsew', pady=(22, 4))
        act_frame.rowconfigure(0, weight=1); act_frame.columnconfigure(0, weight=1)

        act_cols = ('ID', 'Category', 'Question', 'Position', 'Stake', 'Deadline', 'Status')
        act_tree = ttk.Treeview(act_frame, columns=act_cols, show='headings', height=8)
        act_cw   = {'ID': 40, 'Category': 110, 'Question': 220, 'Position': 60,
                    'Stake': 80, 'Deadline': 90, 'Status': 70}
        for col in act_cols:
            act_tree.heading(col, text=col)
            act_tree.column(col, width=act_cw.get(col, 80),
                            anchor='w' if col in ('Category', 'Question') else 'center',
                            stretch=col == 'Question')
        vsb = ttk.Scrollbar(act_frame, orient='vertical', command=act_tree.yview)
        act_tree.configure(yscrollcommand=vsb.set)
        act_tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        act_tree.tag_configure('yes',    foreground=GREEN)
        act_tree.tag_configure('no',     foreground=RED)
        act_tree.tag_configure('closed', foreground=DIM)

        open_preds = [p for p in pts.get('predictions', []) if p.get('status') == 'open']
        for p in sorted(open_preds, key=lambda x: x.get('deadline', ''), reverse=False):
            tag = 'yes' if p['position'] == 'YES' else 'no'
            act_tree.insert('', 'end', tags=(tag,),
                values=(p['id'], p['category'],
                        p['question'][:50] + '…' if len(p['question']) > 50 else p['question'],
                        p['position'], f"{p['stake']:,.0f}", p['deadline'], '🟢 Open'))

        if not open_preds:
            act_tree.insert('', 'end', values=('', '', 'No active predictions yet.', '', '', '', ''))

        # Resolve button
        def _resolve_selected():
            sel = act_tree.selection()
            if not sel: return
            vals = act_tree.item(sel[0], 'values')
            pred_id = int(vals[0]) if vals[0] else None
            if pred_id is None: return
            pts2 = self._pts_load()
            pred = next((p for p in pts2['predictions'] if p['id'] == pred_id), None)
            if not pred: return
            correct = messagebox.askyesno(
                'Resolve Prediction',
                f'Prediction #{pred_id}:\n\n"{pred["question"]}"\n\n'
                f'Your position was: {pred["position"]}\n\n'
                f'Was your prediction CORRECT?',
                parent=self)
            stake = pred['stake']
            if correct:
                payout = round(stake * 1.8, 2)   # 1.8× return on win
                pred['payout']  = payout
                pred['correct'] = True
                pts2['credits'] += payout
                pts2['total_correct'] += 1
                msg = f'✅ Correct! You win {payout:,.0f} credits!'
                # ── Zero-balance rescue: if wallet was depleted (below min stake)
                #    and you solved this question correctly, get a 10,000 bonus ──
                if pts2['credits'] - payout < self.PTS_MIN_STAKE:
                    pts2['credits'] += self.PTS_STARTING_CREDITS
                    msg += (f'\n\n🎁 ZERO-BALANCE RESCUE!\n'
                            f'Your wallet was depleted — correct answer earns '
                            f'+{self.PTS_STARTING_CREDITS:,.0f} bonus credits!')
            else:
                pred['payout']  = 0.0
                pred['correct'] = False
                msg = f'❌ Incorrect. You lose {stake:,.0f} credits.'
            pred['status'] = 'resolved'
            pts2['predictions'] = [p for p in pts2['predictions'] if p['id'] != pred_id]
            pts2['history'].append(pred)
            self._pts_save(pts2)
            messagebox.showinfo('PTS', msg, parent=self)
            self._build_pts_tab()

        tk.Button(right, text='✔  Mark Selected as Resolved',
                  font=('Segoe UI', 8, 'bold'), bg='#21262d', fg=TEAL,
                  relief='flat', padx=10, pady=4, cursor='hand2',
                  command=_resolve_selected).grid(row=0, column=0, sticky='se', pady=(0, 4))

        # History treeview
        hist_lbl = tk.Frame(right, bg=BG)
        hist_lbl.grid(row=1, column=0, sticky='new')
        tk.Label(hist_lbl, text='🏆  PREDICTION HISTORY & LEADERBOARD',
                 font=('Segoe UI', 9, 'bold'), fg=GOLD, bg=BG).pack(side='left', pady=(4, 4))

        hist_frame = tk.Frame(right, bg=BG)
        hist_frame.grid(row=1, column=0, sticky='nsew', pady=(26, 0))
        hist_frame.rowconfigure(0, weight=1); hist_frame.columnconfigure(0, weight=1)

        hist_cols = ('ID', 'Category', 'Question', 'Pos', 'Stake', 'Payout', 'Result', 'Deadline')
        hist_tree = ttk.Treeview(hist_frame, columns=hist_cols, show='headings', height=7)
        hist_cw   = {'ID': 36, 'Category': 100, 'Question': 200, 'Pos': 50,
                     'Stake': 75, 'Payout': 80, 'Result': 70, 'Deadline': 90}
        for col in hist_cols:
            hist_tree.heading(col, text=col)
            hist_tree.column(col, width=hist_cw.get(col, 80),
                             anchor='w' if col in ('Category', 'Question') else 'center',
                             stretch=col == 'Question')
        vsb2 = ttk.Scrollbar(hist_frame, orient='vertical', command=hist_tree.yview)
        hist_tree.configure(yscrollcommand=vsb2.set)
        hist_tree.grid(row=0, column=0, sticky='nsew')
        vsb2.grid(row=0, column=1, sticky='ns')
        hist_tree.tag_configure('win',  foreground=GREEN)
        hist_tree.tag_configure('loss', foreground=RED)

        history = sorted(pts.get('history', []), key=lambda x: x.get('deadline', ''), reverse=True)
        for p in history:
            tag = 'win' if p.get('correct') else 'loss'
            result_txt = f"✓ +{p['payout']:,.0f}" if p.get('correct') else f"✗ −{p['stake']:,.0f}"
            hist_tree.insert('', 'end', tags=(tag,),
                values=(p['id'], p['category'],
                        p['question'][:48] + '…' if len(p['question']) > 48 else p['question'],
                        p['position'], f"{p['stake']:,.0f}",
                        f"{p.get('payout', 0):,.0f}",
                        result_txt, p['deadline']))

        if not history:
            hist_tree.insert('', 'end',
                values=('', '', 'No resolved predictions yet.', '', '', '', '', ''))

        # Leaderboard summary footer
        footer = tk.Frame(tab, bg='#161b22',
                          highlightbackground='#21262d', highlightthickness=1)
        footer.pack(fill='x', padx=12, pady=(0, 8))
        rank_label2, rank_color2 = self._pts_rank(pts)
        net = won_cred - lost_cred
        net_col = GREEN if net >= 0 else RED
        for txt, col in [
            (f'Rank: {rank_label2}', rank_color2),
            (f'Accuracy: {acc:.1f}%  ({correct}/{total})', GREEN if acc >= 50 else RED),
            (f'Net P/L: {net:+,.0f} credits', net_col),
            (f'Wallet: {credits:,.0f} credits', GOLD),
        ]:
            tk.Label(footer, text=txt, font=('Segoe UI', 8, 'bold'),
                     fg=col, bg='#161b22', padx=16, pady=6).pack(side='left')

        # ── Daily Free Top-Up ─────────────────────────────────────────────────
        import datetime as _dt2
        today_str    = _dt2.date.today().strftime('%Y-%m-%d')
        last_claimed = pts.get('daily_topup_date', '')
        already_done = (last_claimed == today_str)

        def _daily_topup():
            pts2 = self._pts_load()
            ts   = _dt2.date.today().strftime('%Y-%m-%d')
            if pts2.get('daily_topup_date', '') == ts:
                messagebox.showinfo('PTS',
                    'You already collected today\'s free credits!\n'
                    'Come back tomorrow for another 10,000 PTS.',
                    parent=self)
                return
            pts2['credits']         += self.PTS_STARTING_CREDITS
            pts2['daily_topup_date'] = ts
            self._pts_save(pts2)
            messagebox.showinfo('PTS',
                f'🎁  +{self.PTS_STARTING_CREDITS:,.0f} PTS Credits added!\n'
                f'New balance: {pts2["credits"]:,.0f} credits.\n\n'
                f'Next free top-up available tomorrow.',
                parent=self)
            self._build_pts_tab()

        topup_txt = '🎁  Collect Daily Free 10,000 Credits' if not already_done \
                    else f'✓  Daily credits claimed (next: tomorrow)'
        topup_col = TEAL if not already_done else DIM
        tk.Button(footer,
                  text=topup_txt,
                  font=('Segoe UI', 8, 'bold'),
                  bg='#0d2a1a' if not already_done else '#161b22',
                  fg=topup_col,
                  relief='flat', padx=12, pady=6,
                  cursor='hand2' if not already_done else 'arrow',
                  state='normal' if not already_done else 'disabled',
                  command=_daily_topup).pack(side='left', padx=(8, 0))

    def _pts_settlement_dates(self, include_daily: bool = False):
        """Generate valid settlement dates.
        Rules:
          Daily    — next 5 trading days (ZDWS only, min 1 day out)
          Weekly   — every Friday (min 7 days from today)
          Monthly  — last Friday of each month
          Quarterly— last Friday of Mar, Jun, Sep, Dec
          Year-end — 31 Dec (or last trading Friday before it)

        include_daily=True adds same-week daily settlements — used by ZDWS,
        which has no 7-day minimum since warrants are pure directional bets
        with no time decay. PTS predictions keep the 7-day floor.
        """
        import datetime as _dt2
        today   = _dt2.date.today()
        min_dt  = today + _dt2.timedelta(days=7)
        year    = today.year
        dates   = {}   # label → date

        # ── Daily (ZDWS only) — next 5 weekdays, from tomorrow ────────────
        if include_daily:
            d = today + _dt2.timedelta(days=1)
            added = 0
            while added < 5:
                if d.weekday() < 5:   # Mon-Fri only (Bursa trading days)
                    dates[f'Daily  — {d.strftime("%a %d %b %Y")}'] = d
                    added += 1
                d += _dt2.timedelta(days=1)

        # ── Weekly Fridays (next 8 weeks) ────────────────────────────────
        d = min_dt
        # advance to next Friday on or after min_dt
        d += _dt2.timedelta(days=(4 - d.weekday()) % 7)
        for _ in range(8):
            dates[f'Weekly  — Fri {d.strftime("%d %b %Y")}'] = d
            d += _dt2.timedelta(weeks=1)

        # ── Monthly: last Friday of each month for next 12 months ────────
        for m_offset in range(1, 13):
            mo = today.month + m_offset
            yr = year + (mo - 1) // 12
            mo = ((mo - 1) % 12) + 1
            # last day of month
            import calendar as _cal
            last_day = _dt2.date(yr, mo, _cal.monthrange(yr, mo)[1])
            # walk back to Friday
            last_fri = last_day - _dt2.timedelta(days=(last_day.weekday() - 4) % 7)
            if last_fri >= min_dt:
                lbl = f'Monthly  — {last_fri.strftime("%b %Y")} ({last_fri.strftime("%d %b")})'
                dates.setdefault(lbl, last_fri)

        # ── Quarterly: last Friday of Mar/Jun/Sep/Dec ────────────────────
        for qm in [3, 6, 9, 12]:
            for yr in [year, year + 1]:
                import calendar as _cal
                last_day = _dt2.date(yr, qm, _cal.monthrange(yr, qm)[1])
                last_fri = last_day - _dt2.timedelta(days=(last_day.weekday() - 4) % 7)
                if last_fri >= min_dt:
                    qname = {3: 'Q1', 6: 'Q2', 9: 'Q3', 12: 'Q4'}[qm]
                    lbl = f'Quarterly — {qname} {yr} ({last_fri.strftime("%d %b %Y")})'
                    dates.setdefault(lbl, last_fri)

        # ── Year-end ─────────────────────────────────────────────────────
        for yr in [year, year + 1]:
            eoy = _dt2.date(yr, 12, 31)
            # if 31 Dec is not a Friday, use last Friday of Dec
            eoy_fri = eoy - _dt2.timedelta(days=(eoy.weekday() - 4) % 7)
            if eoy_fri >= min_dt:
                dates.setdefault(f'Year-End  — {yr} ({eoy_fri.strftime("%d %b %Y")})', eoy_fri)

        # Sort by date, return as ordered list of (label, date) tuples
        return sorted(dates.items(), key=lambda x: x[1])


    def _infer_ccy_from_symbol(self, sym: str) -> str:
        """Currency from ticker convention alone, no lookup table needed:
        '.KL' suffix = Bursa Malaysia = MYR; bare ticker (no suffix) = USD.
        Same convention VFUND_CONFIG uses ('.KL' vs '' suffix per fund).
        World indices (^HSI, ^N225 etc.) aren't exchange-suffixed this way,
        so those still need the explicit WORLD_INDICES table — this is the
        fallback for everything else (stocks, VFund CEFs and their
        constituents, any future ZDOS underlying)."""
        s = (sym or '').upper()
        if s.endswith('.KL'):
            return 'MYR'
        return 'USD'

    _VFUND_CCY = {c['label']: c['ccy'] for c in VFUND_CONFIG}   # ISO codes: MYR/USD

    def _price_ccy_prefix(self, sym: str) -> str:
        """Display prefix for a price figure, keyed off the symbol's actual
        currency (not an RM default). Used anywhere a raw ticker/underlying's
        price is shown outside the fund-specific tabs (e.g. Stock Perf),
        which previously hardcoded 'RM' for every symbol including US-listed
        ETFs like EWW, EWA, EWM etc."""
        ccy = self._zdws_ccy(sym)
        return {'MYR': 'RM', 'USD': '$'}.get(ccy, f'{ccy} ')

    def _zdws_ccy(self, underlying: str) -> str:
        """Native currency of an underlying, checked in priority order:
        1) World indices — explicit table (^HSI etc. carry no exchange suffix).
        2) FX pairs — 'MYR', since the pair's own level (XXXMYR=X) already IS
           the MYR value; treating it as foreign would double-apply the FX
           conversion in _zdws_move_pct.
        3) VFund CEFs — real per-fund currency from VFUND_CONFIG (mixed
           MYR/USD baskets, e.g. MYIPO+ XaaS Fund is MYR, the MSCI-EW ones
           are USD).
        4) KLCI + the internal MYIPO+/Top-Series index family and unit-trust
           FUND_LABELS — always MYR (built from Bursa-listed constituents),
           even though the label itself carries no '.KL' suffix.
        5) Raw ticker symbols (individual stocks, LottoStock, VFund
           constituents, S&P 500/SPDR/MSCI) — inferred from suffix: '.KL' =
           MYR, bare = USD.
        """
        for sym, _, ccy in self.WORLD_INDICES:
            if sym == underlying:
                return ccy
        if underlying in {c for c, _ in self.FX_PAIRS}:
            return 'MYR'
        if underlying in self._VFUND_CCY:
            return self._VFUND_CCY[underlying]
        if underlying == '^KLSE' or underlying in ALL_INDEX_LABELS:
            return 'MYR'
        return self._infer_ccy_from_symbol(underlying)

    def _zdws_underlyings(self) -> list:
        """All underlyings tradeable via ZDWS/ZDOS.
        Returns list of (key, display, level, group) tuples covering:
          - FBM KLCI (Bursa benchmark)
          - MYIPO+ headline indices (MYIPO+, Main, Ace, Momentum)
          - Top series (Top 10/20/50/100)
          - World indices (Hang Seng, S&P 500, Nasdaq, Dow, Russell 1K/2K/3K)
          - Every individual MYIPO+ stock (Quant1 > 0) with a live price
          - The 33 LottoStock Bursa blue chips
          - S&P 500 constituents, group 'US-Stock' (USD) — cache-only, see
            _zdws_warm_batch_cache to populate before these appear
          - SPDR Select Sector + MSCI ETFs, group 'US-ETF' (USD)
          - FX pairs, group 'FX' (level = XXXMYR=X rate)
        """
        out = []

        # ── KLCI — Malaysia's headline benchmark index ────────────────────────
        klci_lvl = self._zdws_current_level('^KLSE')
        if klci_lvl:
            out.append(('^KLSE', 'FBM KLCI (Bursa Benchmark)', klci_lvl, 'Index'))

        # ── MYIPO+ indices ────────────────────────────────────────────────────
        for key, disp in [('MYIPO+', 'MYIPO+ Index'),
                          ('MYIPO+ Main', 'MYIPO+ Main Board'),
                          ('MYIPO+ Ace', 'MYIPO+ ACE Market'),
                          ('MYIPO+ Momentum', 'MYIPO+ Momentum')]:
            if hasattr(self, 'all_frames') and key in self.all_frames:
                lvl = self._zdws_current_level(key)
                if lvl:
                    out.append((key, disp, lvl, 'Index'))

        # ── World indices — settled on the MYR-converted move ─────────────────
        for sym, disp, ccy in self.WORLD_INDICES:
            lvl = self._zdws_world_level(sym)
            if lvl:
                out.append((sym, f'{disp} ({ccy})', lvl, 'World'))

        # ── Top series ────────────────────────────────────────────────────────
        for lbl in TOP_SERIES_LABELS:
            if hasattr(self, 'all_frames') and lbl in self.all_frames:
                lvl = self._zdws_current_level(lbl)
                if lvl:
                    out.append((lbl, lbl, lvl, 'Top Series'))

        # ── Individual MYIPO+ stocks (Quant1 > 0) ─────────────────────────────
        try:
            dfl = getattr(self, 'df_listed_all', None)
            spdf = getattr(self, 'stock_price_df', None)
            if dfl is not None and not dfl.empty and spdf is not None and not spdf.empty:
                myipo_stocks = dfl[dfl['Quant1'] > 0].drop_duplicates('Symbol')
                name_map = getattr(self, 'short_name_map', {})
                for _, row in myipo_stocks.iterrows():
                    sym = row['Symbol']
                    if sym not in spdf.columns:
                        continue
                    lvl = self._zdws_current_level(sym)
                    if not lvl or lvl <= 0:
                        continue
                    short = sym.replace('.KL', '')
                    nm    = name_map.get(sym, '')
                    disp  = f'{short} · {nm[:18]}' if nm else short
                    out.append((sym, disp, lvl, 'Stock'))
        except Exception:
            pass

        # ── LottoStock 33 Bursa blue chips ────────────────────────────────────
        # Same names the lottery draws from, so a LottoStock pick can be
        # hedged or doubled down on here.
        seen = {k for k, _, _, _ in out}
        for code, name in LOTTO_STOCK_LIST:
            sym = f'{code}.KL'
            if sym in seen:
                continue          # already listed as a MYIPO+ constituent
            lvl = self._zdws_current_level(sym)
            if not lvl:
                lvl = lotto_fetch_live_price(code, is_etf=False)
            if lvl and lvl > 0:
                out.append((sym, f'{code} · {name[:18]}', lvl, 'LottoStock 33'))

        # ── SPDR Select Sector + MSCI ETFs, group 'US-ETF' ─────────────────────
        # Small lists (37 total) — safe to resolve eagerly like World Indices,
        # each hits the shared tick_* cache after its first fetch.
        for sym, name in self.SPDR_SELECT_ETFS + self.MSCI_ETF_CONFIG:
            lvl = self._zdws_world_level(sym)
            if lvl and lvl > 0:
                out.append((sym, f'{sym} · {name}', lvl, 'US-ETF'))

        # ── FX pairs, group 'FX' — level is the XXXMYR=X rate itself ───────────
        for code, name in self.FX_PAIRS:
            lvl = self._zdws_fx_to_myr(code)
            if lvl and lvl > 0:
                out.append((code, f'{code}/MYR · {name}', lvl, 'FX'))

        # ── S&P 500 constituents, group 'US-Stock' ──────────────────────────────
        # 503 tickers — cache-ONLY lookup here (no per-symbol cold fetch, that
        # would hang the UI). Call _zdws_warm_batch_cache([t for t,_ in
        # SP500_CONFIG]) once (e.g. via the "Load US Universe" button) to
        # populate the cache in a single batched network call; whatever's
        # cached shows up here, everything else just waits its turn.
        for sym, name in self.SP500_CONFIG:
            lvl = self._zdws_cached_level_only(sym)
            if lvl and lvl > 0:
                out.append((sym, f'{sym} · {name}', lvl, 'US-Stock'))

        return out

    def _build_zdws_tab(self):
        """⚡ ZDWS — Zero-Decay Warrant System trading desk."""
        import datetime as _dt
        tab = self.tab_zdws
        for w in tab.winfo_children():
            w.destroy()

        BG   = '#0d1117'; PANEL = '#161b22'
        TEAL = '#00E5A0'; GOLD  = '#FFD700'
        FG   = '#cdd9e5'; DIM   = '#8b949e'
        RED  = '#F85149'; GREEN = '#3FB950'
        BLUE = '#58A6FF'
        FONT = ('Segoe UI', 9)

        pts = self._pts_load()
        # Auto-settle any matured warrants on tab open
        zdws_msgs = self._zdws_auto_settle(pts)
        if zdws_msgs:
            self._pts_save(pts)
            pts = self._pts_load()

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(tab, bg=BG)
        hdr.pack(fill='x', padx=12, pady=(10, 6))
        tk.Label(hdr, text='⚡  ZDWS', font=('Segoe UI', 14, 'bold'),
                 fg=BLUE, bg=BG).pack(side='left')
        tk.Label(hdr, text='  Zero-Decay Warrant System  ·  Θ = 0  ·  Credits in MYR · foreign assets settle MYR-composite',
                 font=('Segoe UI', 9), fg=DIM, bg=BG).pack(side='left')
        tk.Label(hdr, text=f'  🆔 {CURRENT_KID or "guest"}',
                 font=('Segoe UI', 8, 'bold'), fg='#58A6FF', bg=BG).pack(side='left')
        tk.Button(hdr, text='↻ Refresh', font=('Segoe UI', 8, 'bold'),
                  bg='#21262d', fg=BLUE, relief='flat', padx=10, pady=3,
                  cursor='hand2', command=self._build_zdws_tab).pack(side='left', padx=(14, 0))
        tk.Button(hdr, text='📥 Load US Universe', font=('Segoe UI', 8, 'bold'),
                  bg='#21262d', fg='#D29922', relief='flat', padx=10, pady=3,
                  cursor='hand2',
                  command=lambda: self._zdws_warm_us_universe(self._build_zdws_tab)
                  ).pack(side='left', padx=(6, 0))
        tk.Label(hdr, text=f'  💰 {pts.get("credits", 0):,.0f} PTS Credits',
                 font=('Segoe UI', 10, 'bold'), fg=GOLD, bg=BG).pack(side='right')

        # ── Settlement banner ─────────────────────────────────────────────────
        if zdws_msgs:
            ban = tk.Frame(tab, bg='#0a1a2a', highlightbackground=BLUE,
                           highlightthickness=1, padx=12, pady=8)
            ban.pack(fill='x', padx=12, pady=(0, 6))
            tk.Label(ban, text='⚡ AUTO-SETTLEMENT RESULTS', font=('Segoe UI', 8, 'bold'),
                     fg=BLUE, bg='#0a1a2a').pack(anchor='w')
            for m in zdws_msgs[:6]:
                tk.Label(ban, text=m, font=('Segoe UI', 8), fg=FG,
                         bg='#0a1a2a').pack(anchor='w')

        # ── Stats strip ───────────────────────────────────────────────────────
        zh      = pts.get('zdws_history', [])
        za      = pts.get('zdws_active', [])
        n_win   = len([w for w in zh if w.get('result', '').startswith('WIN')])
        n_tot   = len(zh)
        win_pct = (n_win / n_tot * 100) if n_tot else 0.0
        pnl     = sum(w.get('payout', 0) - w.get('stake', 0) for w in zh)
        staked  = sum(w.get('stake', 0) for w in za)

        stats = tk.Frame(tab, bg=PANEL, highlightbackground='#21262d', highlightthickness=1)
        stats.pack(fill='x', padx=12, pady=(0, 8))
        for lbl, val, col in [
            ('Open Warrants', f'{len(za)}',        BLUE),
            ('Credits at Risk', f'{staked:,.0f}',  GOLD),
            ('Settled',        f'{n_tot}',        FG),
            ('Win Rate',       f'{win_pct:.1f}%', GREEN if win_pct >= 50 else RED),
            ('Net P/L',        f'{pnl:+,.0f}',    GREEN if pnl >= 0 else RED),
        ]:
            cell = tk.Frame(stats, bg=PANEL)
            cell.pack(side='left', expand=True, fill='x', pady=6)
            tk.Label(cell, text=lbl, font=('Segoe UI', 7), fg=DIM, bg=PANEL).pack()
            tk.Label(cell, text=val, font=('Segoe UI', 12, 'bold'),
                     fg=col, bg=PANEL).pack()

        # ── Body: mint panel (left) | boards (right) ──────────────────────────
        body = tk.Frame(tab, bg=BG)
        body.pack(fill='both', expand=True, padx=12, pady=(0, 10))
        body.columnconfigure(0, weight=0, minsize=310)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # ── LEFT: Mint a warrant (scrollable — same reasoning as ZDOS) ────────
        left_outer = tk.Frame(body, bg=PANEL, highlightbackground='#21262d',
                              highlightthickness=1)
        left_outer.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
        left_outer.rowconfigure(0, weight=1)
        left_outer.columnconfigure(0, weight=1)

        left_canvas = tk.Canvas(left_outer, bg=PANEL, highlightthickness=0)
        left_vsb    = ttk.Scrollbar(left_outer, orient='vertical', command=left_canvas.yview)
        left_canvas.configure(yscrollcommand=left_vsb.set)
        left_canvas.grid(row=0, column=0, sticky='nsew')
        left_vsb.grid(row=0, column=1, sticky='ns')

        left = tk.Frame(left_canvas, bg=PANEL, padx=12, pady=10)
        left_win = left_canvas.create_window((0, 0), window=left, anchor='nw')
        left.bind('<Configure>',
                 lambda e: left_canvas.configure(scrollregion=left_canvas.bbox('all')))
        left_canvas.bind('<Configure>',
                         lambda e: left_canvas.itemconfig(left_win, width=e.width))

        def _left_wheel(e):
            left_canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units')
        left_canvas.bind('<Enter>', lambda e: left_canvas.bind_all('<MouseWheel>', _left_wheel))
        left_canvas.bind('<Leave>', lambda e: left_canvas.unbind_all('<MouseWheel>'))

        tk.Label(left, text='⚡  MINT WARRANT', font=('Segoe UI', 10, 'bold'),
                 fg=BLUE, bg=PANEL).pack(anchor='w', pady=(0, 8))

        unders = self._zdws_underlyings()
        if not unders:
            tk.Label(left, text='No MYIPO universe available yet.\nLoad data first.',
                     font=FONT, fg=DIM, bg=PANEL, justify='left').pack(anchor='w')
            return

        # ── Group filter — Index / Top Series / Stock / All ───────────────────
        groups = ['All'] + sorted({g for _, _, _, g in unders},
                                  key=lambda x: {'Index': 0, 'World': 1, 'Top Series': 2, 'Stock': 3, 'US-Stock': 4, 'US-ETF': 5, 'FX': 6, 'LottoStock 33': 7}.get(x, 9))
        grp_var = tk.StringVar(value='All')
        grp_row = tk.Frame(left, bg=PANEL)
        grp_row.pack(fill='x', pady=(0, 4))
        tk.Label(grp_row, text='Type:', font=('Segoe UI', 7), fg=DIM,
                 bg=PANEL).pack(side='left', padx=(0, 4))
        grp_cb = ttk.Combobox(grp_row, textvariable=grp_var, values=groups,
                              state='readonly', font=('Segoe UI', 7), width=14)
        grp_cb.pack(side='left', fill='x', expand=True)
        cnt_var = tk.StringVar(value=f'{len(unders)} tradeable')
        tk.Label(grp_row, textvariable=cnt_var, font=('Segoe UI', 7), fg=DIM,
                 bg=PANEL).pack(side='left', padx=(6, 0))

        tk.Label(left, text='Underlying', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')

        # Filtered index list — maps combobox position → unders index
        self._zdws_filtered = list(range(len(unders)))
        u_var = tk.StringVar()
        u_cb  = ttk.Combobox(left, textvariable=u_var, state='readonly',
                             font=('Segoe UI', 8), width=32)
        u_cb.pack(fill='x', pady=(2, 6))

        def _apply_group_filter(*_):
            g = grp_var.get()
            self._zdws_filtered = [i for i, (_, _, _, grp) in enumerate(unders)
                                   if g == 'All' or grp == g]
            u_cb['values'] = [f'{unders[i][1]}  —  {unders[i][2]:,.2f}'
                              for i in self._zdws_filtered]
            cnt_var.set(f'{len(self._zdws_filtered)} tradeable')
            if self._zdws_filtered:
                u_cb.current(0)
                _refresh_ctx()
        grp_cb.bind('<<ComboboxSelected>>', _apply_group_filter)

        ctx_var = tk.StringVar()
        tk.Label(left, textvariable=ctx_var, font=('Segoe UI', 7), fg=GOLD,
                 bg=PANEL, wraplength=280, justify='left').pack(anchor='w', pady=(0, 6))

        # Side selector
        tk.Label(left, text='Direction', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')
        side_var = tk.StringVar(value='BULL')
        side_row = tk.Frame(left, bg=PANEL)
        side_row.pack(fill='x', pady=(2, 8))
        for txt, val, col in [('🔼  BULL (Long)', 'BULL', GREEN),
                              ('🔽  BEAR (Short)', 'BEAR', RED)]:
            tk.Radiobutton(side_row, text=txt, variable=side_var, value=val,
                           bg=PANEL, fg=col, selectcolor=PANEL,
                           activebackground=PANEL, activeforeground=col,
                           font=('Segoe UI', 8, 'bold'), relief='flat',
                           cursor='hand2', indicatoron=False,
                           padx=10, pady=4).pack(side='left', expand=True, fill='x', padx=1)

        # Stake
        tk.Label(left, text='Stake (PTS Credits)', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')
        stake_var = tk.StringVar(value='1000')
        tk.Entry(left, textvariable=stake_var, font=FONT, bg='#0d1117', fg=FG,
                 insertbackground=TEAL, relief='flat',
                 highlightbackground='#30363d', highlightthickness=1).pack(fill='x', pady=(2, 2))
        stake_info = tk.StringVar()
        tk.Label(left, textvariable=stake_info, font=('Segoe UI', 7), fg=TEAL,
                 bg=PANEL).pack(anchor='w', pady=(0, 6))

        # Horizon
        tk.Label(left, text='Settlement Horizon', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')
        settle_pairs  = self._pts_settlement_dates(include_daily=True)
        settle_labels = [l for l, _ in settle_pairs]
        settle_map    = dict(settle_pairs)
        h_var = tk.StringVar()
        h_cb  = ttk.Combobox(left, textvariable=h_var, values=settle_labels,
                             state='readonly', font=('Segoe UI', 8), width=32)
        h_cb.pack(fill='x', pady=(2, 2))
        if settle_labels:
            h_cb.current(0)
        h_info = tk.StringVar()
        tk.Label(left, textvariable=h_info, font=('Segoe UI', 7), fg='#D29922',
                 bg=PANEL).pack(anchor='w', pady=(0, 8))

        def _refresh_ctx(*_):
            fi = u_cb.current()
            if 0 <= fi < len(self._zdws_filtered):
                i = self._zdws_filtered[fi]
                key, disp, lvl, grp = unders[i]
                ctx_var.set(f'[{grp}]  Entry P₀ = {lvl:,.2f}   ·   Θ = 0 (no time decay)\n'
                            f'Payout if correct: stake × (1 + |%move| × 10)')
            try:
                st = float(stake_var.get())
                bal = pts.get('credits', 0)
                stake_info.set(f'{st:,.0f} credits  ·  {st/bal*100:.1f}% of wallet'
                               if bal else f'{st:,.0f} credits')
            except ValueError:
                stake_info.set('Invalid stake')
            d = settle_map.get(h_var.get())
            if d:
                h_info.set(f'Settles {d.strftime("%A, %d %b %Y")} '
                           f'· {(d - _dt.date.today()).days} days')

        u_cb.bind('<<ComboboxSelected>>', _refresh_ctx)
        h_cb.bind('<<ComboboxSelected>>', _refresh_ctx)
        stake_var.trace_add('write', _refresh_ctx)
        _apply_group_filter()   # populate list + select first

        def _mint():
            fi = u_cb.current()
            if not (0 <= fi < len(self._zdws_filtered)):
                return
            i = self._zdws_filtered[fi]
            key = unders[i][0]
            try:
                stake = float(stake_var.get())
            except ValueError:
                messagebox.showwarning('ZDWS', 'Invalid stake.', parent=self); return
            if stake < self.PTS_MIN_STAKE:
                messagebox.showwarning('ZDWS',
                    f'Minimum stake is {self.PTS_MIN_STAKE:,.0f} credits.', parent=self); return
            d = settle_map.get(h_var.get())
            if d is None:
                messagebox.showwarning('ZDWS', 'Pick a settlement horizon.', parent=self); return
            ok = self._zdws_open_position(key, side_var.get(), stake,
                                          d.strftime('%d/%m/%Y'))
            if ok:
                messagebox.showinfo('ZDWS',
                    f'⚡ Warrant minted!\n\n{side_var.get()} on {unders[i][1]}\n'
                    f'Entry: {unders[i][2]:,.2f}\nStake: {stake:,.0f} credits\n'
                    f'Settles: {d.strftime("%d/%m/%Y")}', parent=self)
                self._build_zdws_tab()
            else:
                messagebox.showwarning('ZDWS',
                    'Could not mint — check stake vs balance.', parent=self)

        tk.Button(left, text='⚡  MINT WARRANT', command=_mint,
                  font=('Segoe UI', 11, 'bold'), bg=BLUE, fg='#0d1117',
                  relief='flat', padx=10, pady=8, cursor='hand2').pack(fill='x')

        # ── RIGHT: Active board + history ─────────────────────────────────────
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky='nsew')
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=2)
        right.columnconfigure(0, weight=1)

        # Active warrants
        act = tk.Frame(right, bg=PANEL, highlightbackground=BLUE, highlightthickness=1)
        act.grid(row=0, column=0, sticky='nsew', pady=(0, 6))
        tk.Label(act, text=f'⚡  ACTIVE WARRANTS ({len(za)})',
                 font=('Segoe UI', 9, 'bold'), fg=BLUE,
                 bg=PANEL).pack(anchor='w', padx=8, pady=(6, 4))

        if not za:
            tk.Label(act, text='No open warrants. Mint one to start.',
                     font=FONT, fg=DIM, bg=PANEL).pack(pady=20)
        else:
            for w_pos in za:
                cur = self._zdws_current_level(w_pos['underlying'])
                mv  = self._zdws_move_pct(w_pos, cur)
                row = tk.Frame(act, bg='#0d1117', highlightbackground='#21262d',
                               highlightthickness=1, padx=8, pady=5)
                row.pack(fill='x', padx=8, pady=2)
                side_col = GREEN if w_pos['side'] == 'BULL' else RED
                ico      = '🔼' if w_pos['side'] == 'BULL' else '🔽'
                tk.Label(row, text=f"{ico} {w_pos['side']}", font=('Segoe UI', 8, 'bold'),
                         fg=side_col, bg='#0d1117', width=9,
                         anchor='w').pack(side='left')
                tk.Label(row, text=w_pos['underlying'], font=('Segoe UI', 8, 'bold'),
                         fg=FG, bg='#0d1117', width=16, anchor='w').pack(side='left')
                tk.Label(row, text=f"P₀ {w_pos['entry']:,.2f}", font=('Segoe UI', 7),
                         fg=DIM, bg='#0d1117').pack(side='left', padx=3)
                if mv is not None:
                    win = (mv >= 0) if w_pos['side'] == 'BULL' else (mv <= 0)
                    est = w_pos['stake'] * (1 + abs(mv) / 100 * 10) if win else 0
                    ccy = w_pos.get('ccy', 'MYR')
                    tk.Label(row, text=f"Pₜ {cur:,.2f}", font=('Segoe UI', 7),
                             fg=DIM, bg='#0d1117').pack(side='left', padx=3)
                    if ccy != 'MYR':
                        tk.Label(row, text=f'{ccy}→MYR', font=('Segoe UI', 6),
                                 fg='#D29922', bg='#0d1117').pack(side='left')
                    tk.Label(row, text=f'{mv:+.2f}%', font=('Segoe UI', 8, 'bold'),
                             fg=GREEN if win else RED, bg='#0d1117').pack(side='left', padx=3)
                    tk.Label(row, text=f'{est - w_pos["stake"]:+,.0f} cr',
                             font=('Segoe UI', 8, 'bold'),
                             fg=GREEN if win else RED, bg='#0d1117').pack(side='left', padx=3)
                tk.Label(row, text=f"⏱ {w_pos['settle_date']}", font=('Segoe UI', 7),
                         fg='#D29922', bg='#0d1117').pack(side='left', padx=3)
                tk.Button(row, text='✖ Close', font=('Segoe UI', 7), bg='#21262d',
                          fg=RED, relief='flat', padx=6, pady=1, cursor='hand2',
                          command=lambda wid=w_pos['id']: self._zdws_market_close(wid)
                          ).pack(side='right')

        # History
        hist_f = tk.Frame(right, bg=PANEL, highlightbackground='#21262d',
                          highlightthickness=1)
        hist_f.grid(row=1, column=0, sticky='nsew')
        tk.Label(hist_f, text='📜  SETTLED WARRANTS', font=('Segoe UI', 9, 'bold'),
                 fg=DIM, bg=PANEL).pack(anchor='w', padx=8, pady=(6, 4))
        cols = ('Side', 'Underlying', 'P₀', 'Settled', 'Move %', 'Stake', 'Payout', 'Result')
        htree = ttk.Treeview(hist_f, columns=cols, show='headings', height=7)
        for c, wdt in zip(cols, (55, 130, 70, 70, 65, 65, 70, 130)):
            htree.heading(c, text=c)
            htree.column(c, width=wdt, anchor='center')
        htree.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        htree.tag_configure('win',  foreground=GREEN)
        htree.tag_configure('loss', foreground=RED)
        if not zh:
            htree.insert('', 'end', values=('—',) * 8)
        for w_h in reversed(zh[-40:]):
            res = w_h.get('result', '—')
            htree.insert('', 'end',
                         tags=('win' if res.startswith('WIN') else 'loss',),
                         values=(w_h['side'], w_h['underlying'],
                                 f"{w_h['entry']:,.2f}",
                                 f"{w_h.get('settled_level', 0):,.2f}",
                                 f"{w_h.get('move_pct', 0):+.2f}%",
                                 f"{w_h['stake']:,.0f}",
                                 f"{w_h.get('payout', 0):,.0f}", res))

    def _build_zdos_tab(self):
        """📐 ZDOS — Zero-Decay Option System trading desk."""
        import datetime as _dt
        tab = self.tab_zdos
        for w in tab.winfo_children():
            w.destroy()

        BG   = '#0d1117'; PANEL = '#161b22'
        GOLD = '#FFD700'; PURP  = '#C77DFF'
        FG   = '#cdd9e5'; DIM   = '#8b949e'
        RED  = '#F85149'; GREEN = '#3FB950'
        FONT = ('Segoe UI', 9)

        pts = self._pts_load()
        zdos_msgs = self._zdos_auto_settle(pts)
        if zdos_msgs:
            self._pts_save(pts)
            pts = self._pts_load()

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(tab, bg=BG)
        hdr.pack(fill='x', padx=12, pady=(10, 6))
        tk.Label(hdr, text='📐  ZDOS', font=('Segoe UI', 14, 'bold'),
                 fg=PURP, bg=BG).pack(side='left')
        tk.Label(hdr, text='  Zero-Decay Option System  ·  Θ = 0  ·  American pricing, European settlement',
                 font=('Segoe UI', 9), fg=DIM, bg=BG).pack(side='left')
        tk.Label(hdr, text=f'  🆔 {CURRENT_KID or "guest"}',
                 font=('Segoe UI', 8, 'bold'), fg='#58A6FF', bg=BG).pack(side='left')
        tk.Button(hdr, text='↻ Refresh', font=('Segoe UI', 8, 'bold'),
                  bg='#21262d', fg=PURP, relief='flat', padx=10, pady=3,
                  cursor='hand2', command=self._build_zdos_tab).pack(side='left', padx=(14, 0))
        tk.Button(hdr, text='📥 Load US Universe', font=('Segoe UI', 8, 'bold'),
                  bg='#21262d', fg='#D29922', relief='flat', padx=10, pady=3,
                  cursor='hand2',
                  command=lambda: self._zdws_warm_us_universe(self._build_zdos_tab)
                  ).pack(side='left', padx=(6, 0))
        tk.Label(hdr, text=f'  💰 {pts.get("credits", 0):,.0f} PTS Credits',
                 font=('Segoe UI', 10, 'bold'), fg=GOLD, bg=BG).pack(side='right')

        if zdos_msgs:
            ban = tk.Frame(tab, bg='#2a0a1a', highlightbackground=PURP,
                           highlightthickness=1, padx=12, pady=8)
            ban.pack(fill='x', padx=12, pady=(0, 6))
            tk.Label(ban, text='📐 AUTO-SETTLEMENT RESULTS', font=('Segoe UI', 8, 'bold'),
                     fg=PURP, bg='#2a0a1a').pack(anchor='w')
            for m in zdos_msgs[:6]:
                tk.Label(ban, text=m, font=('Segoe UI', 8), fg=FG,
                         bg='#2a0a1a').pack(anchor='w')

        # ── Stats strip ───────────────────────────────────────────────────────
        oh      = pts.get('zdos_history', [])
        oa      = pts.get('zdos_active', [])
        n_win   = len([o for o in oh if o.get('result', '').startswith('WIN')])
        n_tot   = len(oh)
        win_pct = (n_win / n_tot * 100) if n_tot else 0.0
        pnl     = sum(o.get('payout', 0) - o.get('stake', 0) for o in oh)
        staked  = sum(o.get('stake', 0) for o in oa)

        stats = tk.Frame(tab, bg=PANEL, highlightbackground='#21262d', highlightthickness=1)
        stats.pack(fill='x', padx=12, pady=(0, 8))
        for lbl, val, col in [
            ('Open Options',   f'{len(oa)}',       PURP),
            ('Credits at Risk', f'{staked:,.0f}',  GOLD),
            ('Settled',        f'{n_tot}',        FG),
            ('Win Rate',       f'{win_pct:.1f}%', GREEN if win_pct >= 50 else RED),
            ('Net P/L',        f'{pnl:+,.0f}',    GREEN if pnl >= 0 else RED),
        ]:
            cell = tk.Frame(stats, bg=PANEL)
            cell.pack(side='left', expand=True, fill='x', pady=6)
            tk.Label(cell, text=lbl, font=('Segoe UI', 7), fg=DIM, bg=PANEL).pack()
            tk.Label(cell, text=val, font=('Segoe UI', 12, 'bold'),
                     fg=col, bg=PANEL).pack()

        # ── Body: mint panel (left) | boards (right) ──────────────────────────
        body = tk.Frame(tab, bg=BG)
        body.pack(fill='both', expand=True, padx=12, pady=(0, 10))
        body.columnconfigure(0, weight=0, minsize=320)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # Mint panel is scrollable — it's grown taller than the tab area
        # (Strategy selector + Position + Tier + Stake + Settlement Date +
        # Write button), and without this the Write button falls below the
        # visible fold with no way to reach it. Standard Canvas+Scrollbar
        # wrapper; 'left' below is still just an ordinary Frame everything
        # else packs into.
        left_outer = tk.Frame(body, bg=PANEL, highlightbackground='#21262d',
                              highlightthickness=1)
        left_outer.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
        left_outer.rowconfigure(0, weight=1)
        left_outer.columnconfigure(0, weight=1)

        left_canvas = tk.Canvas(left_outer, bg=PANEL, highlightthickness=0)
        left_vsb    = ttk.Scrollbar(left_outer, orient='vertical', command=left_canvas.yview)
        left_canvas.configure(yscrollcommand=left_vsb.set)
        left_canvas.grid(row=0, column=0, sticky='nsew')
        left_vsb.grid(row=0, column=1, sticky='ns')

        left = tk.Frame(left_canvas, bg=PANEL, padx=12, pady=10)
        left_win = left_canvas.create_window((0, 0), window=left, anchor='nw')
        left.bind('<Configure>',
                 lambda e: left_canvas.configure(scrollregion=left_canvas.bbox('all')))
        left_canvas.bind('<Configure>',
                         lambda e: left_canvas.itemconfig(left_win, width=e.width))

        def _left_wheel(e):
            left_canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units')
        left_canvas.bind('<Enter>', lambda e: left_canvas.bind_all('<MouseWheel>', _left_wheel))
        left_canvas.bind('<Leave>', lambda e: left_canvas.unbind_all('<MouseWheel>'))

        tk.Label(left, text='📐  WRITE OPTION', font=('Segoe UI', 10, 'bold'),
                 fg=PURP, bg=PANEL).pack(anchor='w', pady=(0, 8))

        unders = self._zdws_underlyings()   # same universe as ZDWS — currency-aware now
        if not unders:
            tk.Label(left, text='No MYIPO universe available yet.\nLoad data first.',
                     font=FONT, fg=DIM, bg=PANEL, justify='left').pack(anchor='w')
            return

        groups = ['All'] + sorted({g for _, _, _, g in unders},
                                  key=lambda x: {'Index': 0, 'World': 1, 'Top Series': 2, 'Stock': 3, 'US-Stock': 4, 'US-ETF': 5, 'FX': 6, 'LottoStock 33': 7}.get(x, 9))
        grp_var = tk.StringVar(value='All')
        grp_row = tk.Frame(left, bg=PANEL)
        grp_row.pack(fill='x', pady=(0, 4))
        tk.Label(grp_row, text='Type:', font=('Segoe UI', 7), fg=DIM,
                 bg=PANEL).pack(side='left', padx=(0, 4))
        grp_cb = ttk.Combobox(grp_row, textvariable=grp_var, values=groups,
                              state='readonly', font=('Segoe UI', 7), width=14)
        grp_cb.pack(side='left', fill='x', expand=True)
        cnt_var = tk.StringVar(value=f'{len(unders)} tradeable')
        tk.Label(grp_row, textvariable=cnt_var, font=('Segoe UI', 7), fg=DIM,
                 bg=PANEL).pack(side='left', padx=(6, 0))

        tk.Label(left, text='Underlying', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')
        self._zdos_filtered = list(range(len(unders)))
        u_var = tk.StringVar()
        u_cb  = ttk.Combobox(left, textvariable=u_var, state='readonly',
                             font=('Segoe UI', 8), width=32)
        u_cb.pack(fill='x', pady=(2, 6))

        def _apply_group_filter(*_):
            g = grp_var.get()
            self._zdos_filtered = [i for i, (_, _, _, grp) in enumerate(unders)
                                   if g == 'All' or grp == g]
            u_cb['values'] = [f'{unders[i][1]}  —  {unders[i][2]:,.2f}'
                              for i in self._zdos_filtered]
            cnt_var.set(f'{len(self._zdos_filtered)} tradeable')
            if self._zdos_filtered:
                u_cb.current(0)
                _refresh_ctx()
        grp_cb.bind('<<ComboboxSelected>>', _apply_group_filter)

        # Strategy — Single Leg (manual) or a named multi-leg template
        tk.Label(left, text='Strategy', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')
        strat_var = tk.StringVar(value='Single Leg')
        strat_cb  = ttk.Combobox(left, textvariable=strat_var,
                                 values=list(self.ZDOS_STRATEGIES.keys()),
                                 state='readonly', font=('Segoe UI', 8), width=32)
        strat_cb.pack(fill='x', pady=(2, 6))

        # Position type — C/S or S/C: Call-or-Put, Long-or-Short (Single Leg only)
        tk.Label(left, text='Position (Single Leg only)', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')
        pos_var = tk.StringVar(value='LONG_CALL')
        pos_row1 = tk.Frame(left, bg=PANEL); pos_row1.pack(fill='x', pady=(2, 1))
        pos_row2 = tk.Frame(left, bg=PANEL); pos_row2.pack(fill='x', pady=(1, 8))
        for row, opts in [(pos_row1, [('🔼 Long Call', 'LONG_CALL', GREEN),
                                       ('🔽 Short Call', 'SHORT_CALL', RED)]),
                          (pos_row2, [('🔽 Long Put', 'LONG_PUT', GREEN),
                                       ('🔼 Short Put', 'SHORT_PUT', RED)])]:
            for txt, val, col in opts:
                tk.Radiobutton(row, text=txt, variable=pos_var, value=val,
                               bg=PANEL, fg=col, selectcolor=PANEL,
                               activebackground=PANEL, activeforeground=col,
                               font=('Segoe UI', 8, 'bold'), relief='flat',
                               cursor='hand2', indicatoron=False,
                               padx=8, pady=4).pack(side='left', expand=True, fill='x', padx=1)

        # Tier — sets strike distance % AND the win multiplier together
        tk.Label(left, text='Tier (strike distance)', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')
        tier_var = tk.IntVar(value=1)
        tier_labels = [f"T{t['tier']}  ·  {t['pct']:.0f}% OTM  ·  ×{t['mult_long']:.1f} long / "
                       f"×{t['mult_short']:.2f} short" for t in self.ZDOS_TIERS]
        tier_cb = ttk.Combobox(left, values=tier_labels, state='readonly',
                               font=('Segoe UI', 8), width=32)
        tier_cb.current(0)
        tier_cb.pack(fill='x', pady=(2, 6))

        ctx_var = tk.StringVar()
        tk.Label(left, textvariable=ctx_var, font=('Segoe UI', 7), fg=GOLD,
                 bg=PANEL, wraplength=290, justify='left').pack(anchor='w', pady=(0, 6))

        tk.Label(left, text='Stake (PTS Credits)', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')
        stake_var = tk.StringVar(value='1000')
        tk.Entry(left, textvariable=stake_var, font=FONT, bg='#0d1117', fg=FG,
                 insertbackground=PURP, relief='flat',
                 highlightbackground='#30363d', highlightthickness=1).pack(fill='x', pady=(2, 2))
        stake_info = tk.StringVar()
        tk.Label(left, textvariable=stake_info, font=('Segoe UI', 7), fg=PURP,
                 bg=PANEL).pack(anchor='w', pady=(0, 6))

        tk.Label(left, text='Settlement Date', font=FONT, fg=DIM, bg=PANEL).pack(anchor='w')
        settle_pairs  = self._pts_settlement_dates(include_daily=True)
        settle_labels = [l for l, _ in settle_pairs]
        settle_map    = dict(settle_pairs)
        h_var = tk.StringVar()
        h_cb  = ttk.Combobox(left, textvariable=h_var, values=settle_labels,
                             state='readonly', font=('Segoe UI', 8), width=32)
        h_cb.pack(fill='x', pady=(2, 2))
        if settle_labels:
            h_cb.current(0)
        h_info = tk.StringVar()
        tk.Label(left, textvariable=h_info, font=('Segoe UI', 7), fg='#D29922',
                 bg=PANEL).pack(anchor='w', pady=(0, 8))

        write_btn_var = tk.StringVar(value='📐  WRITE OPTION')

        def _toggle_strategy_mode(*_):
            """Single Leg: position buttons are live, button says WRITE OPTION.
            Named strategy: position buttons are ignored (legs are derived
            from the template), so disable them and relabel the button so
            it's clear a multi-leg trade is about to be written."""
            is_single = strat_var.get() == 'Single Leg'
            state = 'normal' if is_single else 'disabled'
            for _rb_row in (pos_row1, pos_row2):
                for _rb in _rb_row.winfo_children():
                    _rb.config(state=state)
            write_btn_var.set('📐  WRITE OPTION' if is_single else
                              f'📎  WRITE STRATEGY — {strat_var.get()}')

        def _refresh_ctx(*_):
            fi = u_cb.current()
            ti = tier_cb.current()
            strat = strat_var.get()
            if 0 <= fi < len(self._zdos_filtered) and ti >= 0:
                i    = self._zdos_filtered[fi]
                key, disp, lvl, grp = unders[i]
                t    = self.ZDOS_TIERS[ti]
                if strat == 'Single Leg':
                    pt   = pos_var.get()
                    strike = self._zdos_strike_level(lvl, t['tier'], pt)
                    mult   = t['mult_long'] if pt.startswith('LONG') else t['mult_short']
                    ctx_var.set(f'[{grp}]  Spot {lvl:,.2f}  →  Strike {strike:,.2f} '
                                f'({t["pct"]:.0f}% away)   ·   Θ = 0\n'
                                f'{pt.replace("_", " ").title()}  ·  win pays stake × {mult:.2f}, '
                                f'loss forfeits stake')
                else:
                    legs = self._zdos_strategy_legs(strat, t['tier'])
                    lines = []
                    for pt, tn in legs:
                        tt = self._zdos_tier(tn)
                        strike = self._zdos_strike_level(lvl, tn, pt)
                        mult   = tt['mult_long'] if pt.startswith('LONG') else tt['mult_short']
                        lines.append(f'{pt.replace("_"," ").title()} T{tn} @ {strike:,.2f} '
                                    f'(×{mult:.2f})')
                    ctx_var.set(f'[{grp}]  Spot {lvl:,.2f}  ·  {len(legs)} legs, '
                                f'stake split evenly  ·  Θ = 0\n' + '  |  '.join(lines))
            try:
                st  = float(stake_var.get())
                bal = pts.get('credits', 0)
                stake_info.set(f'{st:,.0f} credits  ·  {st/bal*100:.1f}% of wallet'
                               if bal else f'{st:,.0f} credits')
            except ValueError:
                stake_info.set('Invalid stake')
            d = settle_map.get(h_var.get())
            if d:
                h_info.set(f'Settles {d.strftime("%A, %d %b %Y")} '
                           f'· {(d - _dt.date.today()).days} days')

        u_cb.bind('<<ComboboxSelected>>', _refresh_ctx)
        tier_cb.bind('<<ComboboxSelected>>', _refresh_ctx)
        h_cb.bind('<<ComboboxSelected>>', _refresh_ctx)
        strat_cb.bind('<<ComboboxSelected>>', lambda e: (_toggle_strategy_mode(), _refresh_ctx()))
        for _rb_row in (pos_row1, pos_row2):
            for _rb in _rb_row.winfo_children():
                _rb.bind('<Button-1>', lambda e: left.after(10, _refresh_ctx))
        stake_var.trace_add('write', _refresh_ctx)
        _toggle_strategy_mode()
        _apply_group_filter()

        def _write():
            fi = u_cb.current()
            ti = tier_cb.current()
            if not (0 <= fi < len(self._zdos_filtered)) or ti < 0:
                return
            i   = self._zdos_filtered[fi]
            key = unders[i][0]
            try:
                stake = float(stake_var.get())
            except ValueError:
                messagebox.showwarning('ZDOS', 'Invalid stake.', parent=self); return
            if stake < self.PTS_MIN_STAKE:
                messagebox.showwarning('ZDOS',
                    f'Minimum stake is {self.PTS_MIN_STAKE:,.0f} credits.', parent=self); return
            d = settle_map.get(h_var.get())
            if d is None:
                messagebox.showwarning('ZDOS', 'Pick a settlement date.', parent=self); return
            t = self.ZDOS_TIERS[ti]['tier']
            strat = strat_var.get()
            if strat == 'Single Leg':
                ok = self._zdos_open_position(key, pos_var.get(), t, stake,
                                              d.strftime('%d/%m/%Y'))
                ok_msg  = (f'📐 Option written!\n\n{pos_var.get().replace("_"," ").title()} '
                          f'T{t} on {unders[i][1]}\nEntry: {unders[i][2]:,.2f}\n'
                          f'Stake: {stake:,.0f} credits\nSettles: {d.strftime("%d/%m/%Y")}')
                fail_msg = 'Could not write — check stake vs balance.'
            else:
                ok, resp = self._zdos_open_strategy(key, strat, t, stake, d.strftime('%d/%m/%Y'))
                ok_msg   = f'📐 {resp}\n\non {unders[i][1]}\nSettles: {d.strftime("%d/%m/%Y")}'
                fail_msg = resp
            if ok:
                messagebox.showinfo('ZDOS', ok_msg, parent=self)
                self._build_zdos_tab()
            else:
                messagebox.showwarning('ZDOS', fail_msg, parent=self)

        tk.Button(left, textvariable=write_btn_var, command=_write,
                  font=('Segoe UI', 11, 'bold'), bg=PURP, fg='#0d1117',
                  relief='flat', padx=10, pady=8, cursor='hand2').pack(fill='x')

        # ── RIGHT: Active board + history ─────────────────────────────────────
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky='nsew')
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=2)
        right.columnconfigure(0, weight=1)

        act = tk.Frame(right, bg=PANEL, highlightbackground=PURP, highlightthickness=1)
        act.grid(row=0, column=0, sticky='nsew', pady=(0, 6))
        tk.Label(act, text=f'📐  ACTIVE OPTIONS ({len(oa)})',
                 font=('Segoe UI', 9, 'bold'), fg=PURP,
                 bg=PANEL).pack(anchor='w', padx=8, pady=(6, 4))

        if not oa:
            tk.Label(act, text='No open options. Write one to start.',
                     font=FONT, fg=DIM, bg=PANEL).pack(pady=20)
        else:
            def _render_leg_row(parent, o_pos, indent=0):
                cur = self._zdws_current_level(o_pos['underlying'])
                mv  = self._zdws_move_pct(o_pos, cur)
                winning = self._zdos_is_winning(o_pos, mv) if mv is not None else None
                row = tk.Frame(parent, bg='#0d1117', highlightbackground='#21262d',
                               highlightthickness=1)
                row.pack(fill='x', padx=(8 + indent, 8), pady=2)
                col = GREEN if winning else (RED if winning is False else DIM)
                tk.Label(row, text=f"{o_pos['postype'].replace('_',' ').title()}  T{o_pos['tier']}  "
                                    f"{o_pos['underlying']}", font=('Segoe UI', 8, 'bold'),
                         fg=col, bg='#0d1117').pack(side='left', padx=6, pady=4)
                mv_txt = f'{mv:+.2f}%' if mv is not None else '—'
                tk.Label(row, text=f'{mv_txt}  ·  stake {o_pos["stake"]:,.0f}  ·  '
                                    f'settles {o_pos["settle_date"]}',
                         font=('Segoe UI', 7), fg=DIM, bg='#0d1117').pack(side='left', padx=6)
                tk.Button(row, text='Close now', font=('Segoe UI', 7),
                          bg='#21262d', fg=FG, relief='flat', padx=6,
                          cursor='hand2',
                          command=lambda pid=o_pos['id']: self._zdos_market_close(pid)
                          ).pack(side='right', padx=6, pady=3)

            # Split standalone legs from strategy bundles (grouped by strategy_id)
            standalone = [o for o in oa if not o.get('strategy_id')]
            bundles    = {}
            for o in oa:
                sid = o.get('strategy_id')
                if sid:
                    bundles.setdefault(sid, []).append(o)

            for sid, legs in bundles.items():
                strat_name = legs[0].get('strategy') or 'Strategy'
                total_stake = sum(l['stake'] for l in legs)
                grp = tk.Frame(act, bg='#161b22', highlightbackground=GOLD, highlightthickness=1)
                grp.pack(fill='x', padx=8, pady=(4, 2))
                hdr_row = tk.Frame(grp, bg='#161b22'); hdr_row.pack(fill='x', padx=4, pady=(4, 2))
                tk.Label(hdr_row, text=f'📎 {strat_name}  ·  {legs[0]["underlying"]}  ·  '
                                        f'{len(legs)} legs  ·  {total_stake:,.0f} total stake',
                         font=('Segoe UI', 8, 'bold'), fg=GOLD, bg='#161b22').pack(side='left')
                tk.Button(hdr_row, text='Close all legs', font=('Segoe UI', 7),
                          bg='#21262d', fg=FG, relief='flat', padx=6, cursor='hand2',
                          command=lambda ids=[l['id'] for l in legs]:
                              self._zdos_close_strategy(ids)
                          ).pack(side='right')
                for leg in legs:
                    _render_leg_row(grp, leg, indent=8)

            for o_pos in standalone:
                _render_leg_row(act, o_pos)

        hist_f = tk.Frame(right, bg=PANEL, highlightbackground='#21262d',
                          highlightthickness=1)
        hist_f.grid(row=1, column=0, sticky='nsew')
        tk.Label(hist_f, text='📜  SETTLED OPTIONS', font=('Segoe UI', 9, 'bold'),
                 fg=DIM, bg=PANEL).pack(anchor='w', padx=8, pady=(6, 4))
        cols = ('Position', 'Tier', 'Underlying', 'Strategy', 'P₀', 'Settled', 'Move %', 'Stake', 'Payout', 'Result')
        htree = ttk.Treeview(hist_f, columns=cols, show='headings', height=7)
        for c, wdt in zip(cols, (85, 35, 100, 100, 60, 60, 55, 55, 60, 120)):
            htree.heading(c, text=c)
            htree.column(c, width=wdt, anchor='center')
        htree.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        htree.tag_configure('win',  foreground=GREEN)
        htree.tag_configure('loss', foreground=RED)
        if not oh:
            htree.insert('', 'end', values=('—',) * 10)
        for o_h in reversed(oh[-40:]):
            res = o_h.get('result', '—')
            htree.insert('', 'end',
                         tags=('win' if res.startswith('WIN') else 'loss',),
                         values=(o_h['postype'].replace('_', ' ').title(), o_h['tier'],
                                 o_h['underlying'], o_h.get('strategy') or '—',
                                 f"{o_h['entry']:,.2f}",
                                 f"{o_h.get('settled_level', 0):,.2f}",
                                 f"{o_h.get('move_pct', 0):+.2f}%",
                                 f"{o_h['stake']:,.0f}",
                                 f"{o_h.get('payout', 0):,.0f}", res))

    def _pts_generate_markets(self) -> list:
        """Auto-generate prediction markets from all MYIPO+ data sources.
        Each source is wrapped in its own try/except — one failure never
        prevents others from generating markets."""
        import datetime as _dt
        markets = []
        today = _dt.date.today()
        # {deadline} is a placeholder — substituted at display time with the
        # settlement date the user actually selects, so the question never
        # shows a stale hardcoded date.
        yr_end = '{deadline}'

        # ── 1. KLCI ──────────────────────────────────────────────────────────
        try:
            klci_last = self._zdws_current_level('^KLSE')
            if klci_last and klci_last > 0:
                if True:
                    base   = round(klci_last / 50) * 50
                    levels = [base - 100, base - 50, base, base + 50, base + 100, base + 150]
                    markets.append({'category': 'Index Target',
                        'question': f'Will KLCI close ABOVE {{level}} points by {yr_end}?',
                        'type': 'Multi-Level', 'options': [f'Above {l:,.0f}' for l in levels],
                        'source': 'KLCI live', 'context': f'Current: {klci_last:,.2f}',
                        'meta': {'levels': levels, 'current': klci_last}})
                    nr = (int(klci_last // 100) + 1) * 100
                    markets.append({'category': 'Index Target',
                        'question': f'Will KLCI break {nr:,.0f} before {yr_end}?',
                        'type': 'YES/NO', 'options': ['YES', 'NO'],
                        'source': 'KLCI live',
                        'context': f'Current: {klci_last:,.2f} → gap: {nr - klci_last:+.2f} pts',
                        'suggested_position': 'YES' if klci_last > nr * 0.97 else 'NO'})
        except Exception:
            pass

        # ── 2. Other Bursa indices ────────────────────────────────────────────
        for sym, label, source in getattr(self, 'BURSA_INDICES', []):
            if sym == '^KLSE' or source == 'internal':
                continue   # KLCI handled above; internal indices in section 3
            try:
                lv = (getattr(self, '_mkt_data', {}) or {}).get(sym, (None, None))[0]
                if not lv or lv <= 0: continue
                base   = round(lv / 10) * 10
                levels = [base - 20, base, base + 20, base + 40]
                markets.append({'category': 'Index Target',
                    'question': f'Will {label} close above {{level}} by {yr_end}?',
                    'type': 'Multi-Level', 'options': [f'Above {l:,.0f}' for l in levels],
                    'source': f'{label} live', 'context': f'Current {label}: {lv:,.2f}',
                    'meta': {'levels': levels, 'current': lv}})
            except Exception:
                pass

        # ── 3. MYIPO+ named index markets (MYIPO+, Main, Ace) ────────────────
        # These are the three headline indices directly playable in PTS.
        # Both YES/NO (beat today's level?) and multi-level range markets.
        INDEX_PLAY = [
            ('MYIPO+',          'MYIPO+ Index'),
            ('MYIPO+ Main',     'MYIPO+ Main Board'),
            ('MYIPO+ Ace',      'MYIPO+ ACE Market'),
        ]
        for idx_key, idx_label in INDEX_PLAY:
            try:
                if not hasattr(self, 'all_frames') or idx_key not in self.all_frames:
                    continue
                s = self.all_frames[idx_key].dropna()
                if hasattr(s, 'squeeze'):
                    s = s.squeeze()
                if s.empty: continue
                cur = float(s.iloc[-1])
                if cur <= 0: continue

                # Day change
                prev_val = float(s.iloc[-2]) if len(s) >= 2 else cur
                day_chg  = (cur / prev_val - 1) * 100 if prev_val else 0

                # YTD
                mask  = s.index >= pd.Timestamp(f'{today.year}-01-01')
                yr_s  = float(s[mask].iloc[0]) if mask.any() else cur
                ytd   = (cur / yr_s - 1) * 100 if yr_s else 0

                ctx = (f'Current: {cur:,.2f} | Day: {day_chg:+.2f}% | '
                       f'YTD: {ytd:+.1f}%')

                # YES/NO: will it be higher than today by each settlement?
                markets.append({
                    'category':  'Index Target',
                    'question':  f'Will {idx_label} close HIGHER than {cur:,.2f} by {{level}}?',
                    'type':      'Multi-Level',
                    'options':   ['Yes — will be higher',
                                  'No — will be lower'],
                    'source':    f'{idx_key} internal index',
                    'context':   ctx,
                    'suggested_position': 'Yes — will be higher' if ytd >= 0 else 'No — will be lower',
                })

                # Price-range prediction
                lvls = sorted(set(round(cur * m, 2)
                               for m in [0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20]))
                markets.append({
                    'category':  'Index Target',
                    'question':  f'Where will {idx_label} close by {yr_end}?',
                    'type':      'Multi-Level',
                    'options':   [f'{l:,.2f}' for l in lvls],
                    'source':    f'{idx_key} internal index',
                    'context':   ctx,
                    'meta':      {'levels': lvls, 'current': cur},
                })

            except Exception:
                pass
            try:
                if not hasattr(self, 'all_frames') or idx_name not in self.all_frames: continue
                s = self.all_frames[idx_name].dropna()
                if s.empty: continue
                cur = float(s.iloc[-1])
                if cur <= 0: continue
                mask = s.index >= pd.Timestamp(f'{today.year}-01-01')
                yr_s = float(s[mask].iloc[0]) if mask.any() else cur
                ytd  = (cur / yr_s - 1) * 100 if yr_s else 0
                lvls = sorted(set(round(cur * m, 2) for m in [0.90, 0.95, 1.00, 1.05, 1.10, 1.20]))
                markets.append({'category': 'Index Target',
                    'question': f'Will {idx_name} reach {{level}} by {yr_end}?',
                    'type': 'Multi-Level', 'options': [f'{l:,.2f}' for l in lvls],
                    'source': f'{idx_name} series',
                    'context': f'Current: {cur:,.2f} | YTD: {ytd:+.1f}%',
                    'meta': {'levels': lvls, 'current': cur}})
            except Exception:
                pass

        # ── 4. Future IPO listings ────────────────────────────────────────────
        try:
            fipos = getattr(self, 'future_ipos', None)
            if fipos is not None and not fipos.empty:
                for _, row in fipos.iterrows():
                    try:
                        sym = str(row.get('Symbol', '')).strip()
                        name = str(row.get('Name', '')).strip() or sym.replace('.KL', '')
                        board = str(row.get('Board', '')).strip()
                        px = row.get('Purchase Price'); d0 = row.get('Trade Date')
                        if not sym or pd.isna(px): continue
                        px = float(px)
                        d0s = pd.Timestamp(d0).strftime('%d/%m/%Y') if pd.notna(d0) else '—'
                        try: d30 = (pd.Timestamp(d0) + pd.DateOffset(months=1)).strftime('%d/%m/%Y')
                        except: d30 = d0s
                        markets.append({'category': 'IPO Performance',
                            'question': f'Will {sym} ({name}) trade ABOVE RM{px:.2f} after 1 month ({d30})?',
                            'type': 'YES/NO', 'options': ['YES', 'NO'],
                            'source': f'IPO database ({board})',
                            'context': f'IPO: RM{px:.2f} | Listing: {d0s} | Board: {board}',
                            'suggested_position': 'YES' if board in ('ACE', 'MAIN') else None})
                        markets.append({'category': 'IPO Performance',
                            'question': f'Will {sym} ({name}) reach RM{px*2:.2f} (2× IPO) in 6 months?',
                            'type': 'YES/NO', 'options': ['YES', 'NO'],
                            'source': f'IPO database ({board})',
                            'context': f'Target: RM{px*2:.2f} | IPO: RM{px:.2f}',
                            'suggested_position': 'NO'})
                    except Exception:
                        pass
        except Exception:
            pass

        # ── 5. D+365 delist candidates ────────────────────────────────────────
        try:
            dc = getattr(self, 'delist_candidates', None)
            if dc is not None and not dc.empty:
                for _, row in dc.iterrows():
                    try:
                        sym = str(row.get('Symbol', '')).strip()
                        name = str(row.get('Name', '')).strip() or sym.replace('.KL', '')
                        d365 = row.get('D+365'); px = row.get('Purchase Price')
                        if pd.isna(d365) or pd.isna(px): continue
                        d365s = pd.Timestamp(d365).strftime('%d/%m/%Y')
                        px = float(px)
                        markets.append({'category': 'Stock Price Target',
                            'question': f'Will {sym} ({name}) close ABOVE RM{px:.2f} on D+365 ({d365s})?',
                            'type': 'YES/NO', 'options': ['YES', 'NO'],
                            'source': 'MYIPO+ D+365 delist',
                            'context': f'D+365: {d365s} | IPO: RM{px:.2f}',
                            'suggested_position': None})
                    except Exception:
                        pass
        except Exception:
            pass

        # ── 6. Non-MYIPO+ constituents (Quant1==0 in teet.csv) ───────────────
        try:
            dla = getattr(self, 'df_listed_all', None)
            if dla is not None and not dla.empty and 'Quant1' in dla.columns:
                for _, row in dla[dla['Quant1'] == 0].iterrows():
                    try:
                        sym = str(row.get('Symbol', '')).strip()
                        name = str(row.get('Name', '')).strip() or sym.replace('.KL', '')
                        board = str(row.get('Board', '')).strip()
                        px = row.get('Purchase Price'); d0 = row.get('Trade Date')
                        if not sym: continue
                        has_px = pd.notna(px)
                        d0s = pd.Timestamp(d0).strftime('%d/%m/%Y') if pd.notna(d0) else '—'
                        ctx = f'Board: {board} | Listed: {d0s}'
                        if has_px:
                            p = float(px); ctx += f' | IPO: RM{p:.2f}'
                            markets.append({'category': 'Stock Price Target',
                                'question': f'Will {sym} ({name}) trade ABOVE RM{p:.2f} by {yr_end}?',
                                'type': 'YES/NO', 'options': ['YES', 'NO'],
                                'source': f'teet.csv non-constituent ({board})',
                                'context': ctx, 'suggested_position': None})
                            lvls = [round(p * m, 4) for m in [0.80, 1.00, 1.20, 1.50, 2.00]]
                            markets.append({'category': 'Stock Price Target',
                                'question': f'Where will {sym} ({name}) trade by {yr_end}?',
                                'type': 'Multi-Level', 'options': [f'RM{l:.4f}' for l in lvls],
                                'source': f'teet.csv non-constituent ({board})',
                                'context': ctx, 'meta': {'levels': lvls, 'ipo_px': p}})
                        else:
                            markets.append({'category': 'Stock Price Target',
                                'question': f'Will {sym} ({name}) be higher than today in 3 months?',
                                'type': 'YES/NO', 'options': ['YES', 'NO'],
                                'source': f'teet.csv non-constituent ({board})',
                                'context': ctx, 'suggested_position': None})
                    except Exception:
                        pass
        except Exception:
            pass

        # ── 7. LottoStock 33 (Bursa blue-chip) ───────────────────────────────
        for sym_code, sym_name in LOTTO_STOCK_LIST:
            try:
                ticker = f'{sym_code}.KL' if not sym_code.endswith('.KL') else sym_code
                lp = _MF_PRICE_CACHE.get(ticker)
                if lp is None:
                    cp = cache_get_live(f'px_{ticker}', _TTL_LIVE)
                    lp = float(cp) if cp else None
                markets.append({'category': 'Stock Price Target',
                    'question': f'Will {ticker} ({sym_name}) close HIGHER by {yr_end}?',
                    'type': 'YES/NO', 'options': ['YES', 'NO'],
                    'source': 'LottoStock (Bursa blue-chip)',
                    'context': f'Live: RM{lp:.4f}' if lp else 'Price not loaded',
                    'suggested_position': None})
                if lp and lp > 0:
                    lvls = sorted(set(round(lp * m, 4) for m in [0.80, 0.90, 1.00, 1.10, 1.20, 1.50]))
                    markets.append({'category': 'Stock Price Target',
                        'question': f'What price will {ticker} ({sym_name}) reach by {yr_end}?',
                        'type': 'Multi-Level', 'options': [f'RM{l:.4f}' for l in lvls],
                        'source': 'LottoStock (Bursa blue-chip)',
                        'context': f'Current: RM{lp:.4f}',
                        'meta': {'levels': lvls, 'current': lp}})
            except Exception:
                pass

        return markets

    def _populate_table(self):
        for row in self.data_tree.get_children():
            self.data_tree.delete(row)
        if self.output_df is None: return

        # Linked to the right-side checkboxes — only show currently-selected
        # indices/funds, same set the Line Chart / Returns Bar use.
        selected = self._selected_labels()
        if not selected:
            return

        # Linked to the Period selector — same date window as every other tab.
        df = self.output_df.copy()
        mask = self._get_period_mask(df.index)
        df = df[mask]

        val_cols = [lbl for lbl in selected if lbl in df.columns]
        if not val_cols:
            return

        # Rebuild the column set on the treeview so only checked series show.
        self._set_table_columns(val_cols)

        for dt, row in df[val_cols].iloc[::-1].iterrows():
            date_str = dt.strftime('%d/%m/%Y') if hasattr(dt, 'strftime') else str(dt)
            vals = []
            for lbl, v in row.items():
                if pd.isna(v):
                    vals.append('')
                elif lbl in ALL_FUND_LABELS:
                    vals.append(f'{v:.4f}')   # fund NAV (unit trust or VFund CEF), 4dp
                else:
                    vals.append(f'{v:.2f}')   # index value, base-100, 2dp
            self.data_tree.insert('', 'end', values=[date_str] + vals)

    def _set_table_columns(self, val_cols):
        """Rebuild Data Table columns to match the currently-selected labels."""
        cols = ['Date'] + val_cols
        if list(self.data_tree['columns']) == cols:
            return   # already matches — avoid unnecessary rebuild/flicker
        self.data_tree['columns'] = cols
        for col in cols:
            self.data_tree.heading(col, text=col.replace('MYIPO+ ', ''))
            self.data_tree.column(col, width=90, anchor='e', stretch=False)
        self.data_tree.column('Date', width=90, anchor='w')

    def _update_stats(self):
        for row in self.stats_tree.get_children():
            self.stats_tree.delete(row)
        if not self.all_frames: return

        period = self.period_var.get()
        try:
            self.stats_tree.heading('Ret%', text=f'{period}%')
        except Exception:
            pass

        for lbl in self._selected_labels():
            series = self.all_frames[lbl][lbl].dropna()
            if series.empty:
                continue

            last = series.iloc[-1]

            chg  = self.all_frames[lbl].get(f'{lbl} Chg%', pd.Series(dtype=float)).dropna()
            chg1 = chg.iloc[-1] if not chg.empty else 0.0

            is_fund = lbl in ALL_FUND_LABELS
            # Ret% uses Total Return (NAV + cumulative distributions) for
            # funds, so it's a fair comparison against price-only indices —
            # NAV itself (last_disp below) still shows the actual per-unit
            # price, just the % return column accounts for payouts received.
            # (VFund CEFs have no TotalReturn/Units columns — no distributions,
            # fixed units — so these lookups just no-op and fall back cleanly.)
            ret_series = series
            if is_fund:
                tr_series_fund = self.all_frames[lbl].get(f'{lbl} TotalReturn', pd.Series(dtype=float)).dropna()
                if not tr_series_fund.empty:
                    ret_series = tr_series_fund

            mask = self._get_period_mask(ret_series.index)
            s    = ret_series[mask].dropna()
            if len(s) < 2:
                s = ret_series.tail(2)
            period_ret = (s.iloc[-1] / s.iloc[0] - 1) * 100 if len(s) >= 2 and s.iloc[0] != 0 else 0.0

            tag = 'pos' if period_ret >= 0 else 'neg'
            ccy_pfx   = FUND_META.get(lbl, {}).get('ccy', 'RM')
            last_disp = f'{ccy_pfx}{last:.4f}' if is_fund else f'{last:.2f}'

            units_disp = '—'
            if is_fund:
                units_series = self.all_frames[lbl].get(f'{lbl} Units', pd.Series(dtype=float)).dropna()
                if not units_series.empty:
                    units_disp = f'{units_series.iloc[-1]:,.0f}'
                elif lbl in VFUND_LABELS:
                    units_disp = f'{FUND_META[lbl]["units"]:,.0f}'   # fixed at launch, never changes

            self.stats_tree.insert('', 'end',
                values=(lbl.replace('MYIPO+ ', ''), last_disp,
                        f'{chg1:+.2f}', f'{period_ret:+.1f}%', units_disp),
                tags=(tag,))

        self.stats_tree.tag_configure('pos', foreground='#6BCB77')
        self.stats_tree.tag_configure('neg', foreground='#FF6B6B')

    def _select_all(self):
        for v in self.check_vars.values(): v.set(True)
        self._redraw_chart()

    def _deselect_all(self):
        for v in self.check_vars.values(): v.set(False)
        self._redraw_chart()

    def _select_core(self):
        self._deselect_all()
        for lbl in [c[0] for c in INDEX_CONFIG]:
            self.check_vars[lbl].set(True)
        self._redraw_chart()

    def _select_yseries(self):
        self._deselect_all()
        for lbl in [c[0] for c in YSERIES_CONFIG]:
            self.check_vars[lbl].set(True)
        self._redraw_chart()

    def _select_trseries(self):
        self._deselect_all()
        for lbl in [c[0] for c in TR_SERIES_CONFIG]:
            self.check_vars[lbl].set(True)
        self._redraw_chart()

    def _select_top10(self):
        self._deselect_all()
        for lbl in TOP_SERIES_LABELS:
            if lbl in self.check_vars:
                self.check_vars[lbl].set(True)
        self._redraw_chart()

    def _select_fund(self):
        self._deselect_all()
        for lbl in FUND_LABELS:
            if lbl in self.check_vars:
                self.check_vars[lbl].set(True)
        self._redraw_chart()

    def _select_vfund(self):
        """Select the equal-weight CEF model portfolios."""
        self._deselect_all()
        for lbl in VFUND_LABELS:
            if lbl in self.check_vars:
                self.check_vars[lbl].set(True)
        self._redraw_chart()


# =============================================================================
# SPLASH SCREEN
# =============================================================================
# =============================================================================
# MYFOLIO — PORTFOLIO COMMAND CENTER  (formerly NEWPOR.py)
# Merged as a Toplevel window launched from MYIPOApp.
# Separate Google Sheets CSV from PAA's IPO database.
# USD and MYR portfolios are shown in separate tabs.
# =============================================================================

# ── Colour palette (shared with MYFOLIO) ─────────────────────────────────────
_MF_BG      = "#0D1117"
_MF_PANEL   = "#161B22"
_MF_CARD    = "#1C2128"
_MF_BORDER  = "#30363D"
_MF_ACCENT  = "#58A6FF"
_MF_GREEN   = "#3FB950"
_MF_RED     = "#F85149"
_MF_YELLOW  = "#D29922"
_MF_PURPLE  = "#BC8CFF"
_MF_ORANGE  = "#F0883E"
_MF_TEXT    = "#E6EDF3"
_MF_SUBTEXT = "#8B949E"

_MF_FONT_H1 = ("Consolas", 18, "bold")
_MF_FONT_H2 = ("Consolas", 13, "bold")
_MF_FONT_H3 = ("Consolas", 11, "bold")
_MF_FONT_SM = ("Consolas", 9)
_MF_FONT_MD = ("Consolas", 10)
_MF_FONT_LG = ("Consolas", 14, "bold")

# ── MYFOLIO-specific Google Sheets CSV (DIFFERENT from PAA's IPO database) ───
# NOTE: there is no built-in portfolio URL any more.
# Each K-ID connects their own Google Sheet or CSV via SCA -> Source.
# A hardcoded sheet here would mean every user on the machine saw
# whoever set it up, so the concept of a shared "demo" sheet is gone.

# ── Transaction-ledger schema column names (after _mf_norm) ──────────────────
# Headers: Date | Transaction Type | AssetCode | Currency | Unit | Value | Amount
# "Value" = price per unit; "Amount" = total MV of the row (Unit × Value)
# Cash asset code: USD-CASH  (or MYR-CASH for MYR side)
# Transaction Types: Deposit, Withdraw, Buy, Sell, Fees, Dividend (future)
_NEWPOR_CCY_MAP = {
    "USD": ("SWAY1", "SWAY"),
    "MYR": ("SWAY1-MYR", "SWAY"),
    "HKD": ("SWAY1-HKD", "SWAY"),
    "SGD": ("SWAY1-SGD", "SWAY"),
}

# ── Supported markets ────────────────────────────────────────────────────────
# ccy -> (market code, yfinance suffix, display label, symbol prefix)
# The suffix matters: a Hong Kong ticker is 0700.HK and a Singapore one is
# D05.SI. Tagging them 'US' (as the old `"MY" if ccy=="MYR" else "US"` rule
# did) would send a bare '0700' to Yahoo and silently return nothing.
MARKET_SPEC = {
    "MYR": {"market": "MY", "suffix": ".KL", "label": "MYR Position", "sym": "RM"},
    "USD": {"market": "US", "suffix": "",    "label": "USD Position", "sym": "$"},
    "HKD": {"market": "HK", "suffix": ".HK", "label": "HKD Position", "sym": "HK$"},
    "SGD": {"market": "SG", "suffix": ".SI", "label": "SGD Position", "sym": "S$"},
}
# market code -> ccy, for reverse lookups
_MARKET_TO_CCY = {v["market"]: k for k, v in MARKET_SPEC.items()}


def ccy_symbol(ccy: str) -> str:
    return MARKET_SPEC.get((ccy or "").upper(), {}).get("sym", (ccy or "") + " ")

# ── Price cache (shared with existing yf calls) ───────────────────────────────
_MF_PRICE_CACHE: dict = {}

def _mf_ticker_for_market(ticker: str, market: str) -> str:
    """Append the exchange suffix Yahoo expects for a given market.
    MY -> .KL, HK -> .HK, SG -> .SI, US -> bare.
    """
    ticker = (ticker or "").upper().strip()
    mkt    = (market or "").upper()
    ccy    = _MARKET_TO_CCY.get(mkt)
    suffix = MARKET_SPEC.get(ccy, {}).get("suffix", "") if ccy else ""
    if suffix and not ticker.endswith(suffix):
        return f"{ticker}{suffix}"
    return ticker

def _mf_as_price(value):
    try:
        p = float(value)
        return p if p > 0 else None
    except (TypeError, ValueError):
        return None

def _mf_get_price(ticker: str, market: str, fallback=None):
    lookup = _mf_ticker_for_market(ticker, market)
    # 1. In-memory session cache
    if lookup in _MF_PRICE_CACHE:
        return _MF_PRICE_CACHE[lookup]
    # 2. Disk live-price cache (5-min TTL) — avoids re-fetching on re-open
    cached_p = cache_get_live(f'px_{lookup}', _TTL_LIVE)
    if cached_p is not None:
        _MF_PRICE_CACHE[lookup] = float(cached_p)
        return float(cached_p)
    # 3. Network fetch
    try:
        obj  = yf.Ticker(lookup)
        fast = obj.fast_info
        for field in ("last_price", "lastPrice", "regular_market_price",
                      "previous_close", "previousClose"):
            p = _mf_as_price(getattr(fast, field, None))
            if p:
                _MF_PRICE_CACHE[lookup] = p
                cache_set_live(f'px_{lookup}', p)
                return p
        hist = obj.history(period="5d", interval="1d")
        if hist is not None and not hist.empty and "Close" in hist:
            close = hist["Close"].dropna()
            if not close.empty:
                p = _mf_as_price(close.iloc[-1])
                if p:
                    _MF_PRICE_CACHE[lookup] = p
                    cache_set_live(f'px_{lookup}', p)
                    return p
        info = obj.info
        for field in ("currentPrice", "regularMarketPrice",
                      "previousClose", "navPrice"):
            p = _mf_as_price(info.get(field))
            if p:
                _MF_PRICE_CACHE[lookup] = p
                cache_set_live(f'px_{lookup}', p)
                return p
    except Exception:
        pass
    return fallback

# ── Historical daily closing-price series — the actual basis for a genuine
#    day-by-day market value curve (units held that day x THAT DAY'S close),
#    not just a single live-price dot at today. Cached per ticker so the
#    chart doesn't re-fetch on every redraw/period change.
_MF_HIST_PRICE_CACHE: dict = {}
_MF_DIV_CACHE: dict = {}   # ticker (with .KL) -> pd.Series of dividends (full history)
_MF_EPS_CACHE: dict = {}   # ticker -> float EPS

def _preload_div_cache_from_disk():
    """Load all fresh div parquet files from disk into _MF_DIV_CACHE at startup.
    This ensures the dividend estimator popup hits the in-memory cache immediately
    without needing to read parquet files on every popup open."""
    try:
        if not os.path.exists(_CACHE_DIVS_DIR):
            return
        for fname in os.listdir(_CACHE_DIVS_DIR):
            if not fname.endswith('.parquet'):
                continue
            fpath = os.path.join(_CACHE_DIVS_DIR, fname)
            if not _cache_fresh(fpath, _TTL_DIV):
                continue   # stale — skip
            try:
                df = _cache_rparquet(fpath)
                if df is None or df.empty:
                    continue
                col = 'Dividends' if 'Dividends' in df.columns else df.columns[0]
                s   = df[col]
                if s.empty:
                    continue
                # Reconstruct ticker key: filename is e.g. '5326.KL.parquet' or 'VNQ.parquet'
                ticker_key = fname.replace('.parquet', '')
                _MF_DIV_CACHE[ticker_key] = s
            except Exception:
                pass
    except Exception:
        pass

_preload_div_cache_from_disk()

def _mf_get_price_history(ticker: str, market: str, start_date=None):
    """
    Returns a pandas Series of daily closing prices for the given ticker.
    Cache hierarchy:
      1. In-memory (_MF_HIST_PRICE_CACHE) — instant, session-scoped
      2. Disk parquet (paa_cache/prices/)  — instant, persists across restarts
      3. yfinance network fetch            — slow, updates disk + memory cache
    """
    lookup = _mf_ticker_for_market(ticker, market)

    # 1. In-memory cache
    if lookup in _MF_HIST_PRICE_CACHE:
        full_series = _MF_HIST_PRICE_CACHE[lookup]
    else:
        # 2. Disk cache (1-day TTL)
        disk_series = cache_get_prices(lookup)
        if disk_series is not None:
            full_series = disk_series
        else:
            # 3. Network fetch
            try:
                obj  = yf.Ticker(lookup)
                hist = obj.history(period="max", interval="1d", auto_adjust=True)
                if hist is None or hist.empty or "Close" not in hist:
                    full_series = pd.Series(dtype=float)
                else:
                    close = hist["Close"].dropna()
                    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
                    full_series = close
                    cache_set_prices(lookup, full_series)   # persist to disk
            except Exception:
                full_series = pd.Series(dtype=float)
        _MF_HIST_PRICE_CACHE[lookup] = full_series   # promote to memory

    if start_date is not None and not full_series.empty:
        return full_series[full_series.index >= pd.Timestamp(start_date)]
    return full_series

def _mf_fmt_price(v)  -> str: return f"{v:,.4f}"  if v is not None else "N/A"
def _mf_fmt_money(v)  -> str: return f"{v:,.2f}"  if v is not None else "N/A"
def _mf_fmt_signed(v) -> str: return f"{v:+,.2f}" if v is not None else "N/A"
def _mf_fmt_pct(v)    -> str: return f"{v:+.2f}%" if v is not None else "N/A"

def _fmt_units(v) -> str:
    """Format unit/share quantities with trailing-zero trimming.
    Shows up to 4 decimal places but drops trailing zeros:
      10.0000 → '10'
       4.3900 → '4.39'
       2.3200 → '2.32'
       0.8600 → '0.86'
       1.2345 → '1.2345'
    Integers with no fractional part show no decimal point at all.
    """
    if v is None:
        return "N/A"
    # Format to 4dp then strip trailing zeros and unnecessary decimal point
    s = f"{v:,.4f}"
    # Strip trailing zeros after decimal, then the decimal point if nothing left
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s

def _port_display_name(port_key: str) -> str:
    """Internal portfolio key -> human label. Driven by MARKET_SPEC so adding
    a currency doesn't need this function touched."""
    for _ccy, (_code, _plat) in _NEWPOR_CCY_MAP.items():
        if _code == port_key:
            return MARKET_SPEC.get(_ccy, {}).get("label", f"{_ccy} Position")
    return port_key


def _port_ccy(port_key: str) -> str:
    """Internal portfolio key -> its currency code."""
    for _ccy, (_code, _plat) in _NEWPOR_CCY_MAP.items():
        if _code == port_key:
            return _ccy
    return "MYR"

# ── CSV loader ────────────────────────────────────────────────────────────────
import csv, io
from urllib.request import urlopen as _mf_urlopen
from urllib.error   import URLError as _mf_URLError

def _mf_norm(k: str) -> str:
    return k.lower().replace(" ", "").replace("_", "").strip()

# ── Normalised column keys for the NEWPOR transaction ledger ─────────────────
_NC_DATE   = "date"
_NC_TYPE   = "transactiontype"   # Deposit / Buy / Sell / Fees / Withdraw / Dividend
_NC_ASSET  = "assetcode"
_NC_CCY    = "currency"
_NC_UNIT   = "unit"
_NC_VALUE  = "value"             # price per unit (may have $, RM prefix)
_NC_AMOUNT = "amount"            # total amount    (may have $, RM prefix / negative)

_CASH_ASSETS = {"USD-CASH", "MYR-CASH", "SGD-CASH"}  # cash instrument codes

def _mf_strip_currency(s: str) -> float:
    """
    Parse '$1.23', '-$0.74', 'RM1.32', '-RM0.50', '1.23', '' → float.
    Returns 0.0 on failure.
    """
    try:
        cleaned = s.replace("RM", "").replace("$", "").replace(",", "").strip()
        return float(cleaned)
    except (ValueError, AttributeError):
        return 0.0

_NEWPOR_CACHE_PREFIX = 'ledger'   # per-K-ID cache: ledger_<kid>.csv next to teet.csv

# One-time purge of caches written by the old append-only delta-sync.
# That version could silently drop a mid-sheet insert and duplicate another
# row, so any file it produced is untrustworthy. Bump this string to force
# a clean re-download for everyone.
_LEDGER_CACHE_EPOCH = '2026-07-v2-fullhash'


def _purge_stale_ledger_caches():
    """Delete ledger caches written before the delta-sync fix, once."""
    try:
        marker = os.path.join(_CACHE_DIR, '.ledger_epoch')
        if os.path.exists(marker):
            with open(marker, 'r', encoding='utf-8') as f:
                if f.read().strip() == _LEDGER_CACHE_EPOCH:
                    return                      # already purged
        folder = os.path.dirname(os.path.abspath(DEFAULT_INPUT_FILE))
        removed = 0
        if os.path.isdir(folder):
            for fn in os.listdir(folder):
                if fn.startswith(_NEWPOR_CACHE_PREFIX + '_') and fn.endswith('.csv'):
                    try:
                        os.remove(os.path.join(folder, fn)); removed += 1
                    except Exception:
                        pass
                # legacy shared file from before per-K-ID caches
                elif fn in ('MYBook.csv', 'newpor.csv'):
                    try:
                        os.remove(os.path.join(folder, fn)); removed += 1
                    except Exception:
                        pass
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(marker, 'w', encoding='utf-8') as f:
            f.write(_LEDGER_CACHE_EPOCH)
        if removed:
            print(f'[cache] Purged {removed} stale ledger cache file(s) — '
                  f'they were written by the old delta-sync and may be wrong.')
    except Exception:
        pass


_purge_stale_ledger_caches()

def _mf_fetch_online_chunked(url: str, log_cb=None, chunk_size: int = 200):
    """
    Download the ledger CSV. Tries 4 methods in order until one works:
    1. requests library (best Windows SSL support, handles certs automatically)
    2. urllib + certifi CA bundle
    3. urllib + unverified SSL
    4. urllib bare (no context)
    """
    def log(m):
        if log_cb: log_cb(m)

    log('🌐  Connecting to your ledger…')
    raw_bytes  = None
    last_error = None
    hdrs       = {'User-Agent': 'Mozilla/5.0'}

    # Method 1: requests (handles Windows certs best)
    try:
        import requests as _req
        r = _req.get(url, headers=hdrs, timeout=20)
        r.raise_for_status()
        raw_bytes = r.content
        log(f'✅  Ledger: {len(raw_bytes)/1024:.1f} KB via requests')
    except ImportError:
        pass
    except Exception as e:
        last_error = e
        log(f'   requests failed ({type(e).__name__}: {e})')

    # Method 2: urllib + certifi
    if raw_bytes is None:
        try:
            import ssl, urllib.request
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                ctx = ssl.create_default_context()
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
                raw_bytes = r.read()
            log(f'✅  Ledger: {len(raw_bytes)/1024:.1f} KB via urllib+certifi')
        except Exception as e:
            last_error = e
            log(f'   urllib+certifi failed ({type(e).__name__}: {e})')

    # Method 3: urllib unverified SSL
    if raw_bytes is None:
        try:
            import ssl, urllib.request
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
                raw_bytes = r.read()
            log(f'✅  Ledger: {len(raw_bytes)/1024:.1f} KB via unverified SSL')
        except Exception as e:
            last_error = e
            log(f'   unverified SSL failed ({type(e).__name__}: {e})')

    # Method 4: bare urllib
    if raw_bytes is None:
        try:
            import urllib.request
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=20) as r:
                raw_bytes = r.read()
            log(f'✅  Ledger: {len(raw_bytes)/1024:.1f} KB via bare urllib')
        except Exception as e:
            last_error = e
            log(f'   bare urllib failed ({type(e).__name__}: {e})')

    if raw_bytes is None:
        err_str = f'{type(last_error).__name__}: {last_error}'
        hint = ''
        if '403' in err_str or 'Forbidden' in err_str:
            hint = (
                '\n\nHTTP 403 — Sheet not published.\n'
                'In your Google Sheet:\n'
                '  File → Share → Publish to web\n'
                '  → Sheet1 → CSV → Publish'
            )
        raise Exception(f'Ledger download failed (all methods).\n{err_str}{hint}')

    raw   = raw_bytes.decode('utf-8-sig')
    lines = raw.splitlines()
    if not lines:
        return raw, 0
    return '\n'.join(lines), len(lines) - 1


def _mf_delta_sync(online_text: str, cache_path: str, log_cb=None) -> tuple[str, bool]:
    """
    Sync the ledger cache against the online sheet.

    The cache is only reused when it is provably identical to the sheet:
    we hash the FULL body of each and compare. Anything else — an appended
    row, an edited cell, a deleted row, or a row inserted in the middle —
    triggers a full rewrite.

    Why not append-only deltas:
      The old version assumed rows are only ever added at the bottom, and
      spot-checked just the first 10 rows to confirm. Insert a forgotten
      trade in date order below row 10 and the check passes, so it appends
      online_body[n_cached:] — the LAST row of the sheet — while silently
      dropping the row you actually inserted. The cache then holds a
      duplicate and is missing a trade, and every position from that date
      onward is wrong. A ledger is small (a few hundred rows / tens of KB);
      the append optimisation saved microseconds and cost correctness.

    Returns (merged_text: str, cache_was_updated: bool).
    """
    import hashlib

    def log(m):
        if log_cb: log_cb(m)

    online_lines = online_text.splitlines()
    if not online_lines:
        return online_text, False

    online_body = online_lines[1:]   # data rows only

    # Read existing cache
    cached_lines: list[str] = []
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r', encoding='utf-8-sig') as f:
                cached_lines = f.read().splitlines()
        except Exception:
            cached_lines = []
    cached_body = cached_lines[1:] if cached_lines else []

    n_cached, n_online = len(cached_body), len(online_body)

    def _digest(body):
        # Ignore trailing blank lines and stray \r so cosmetic differences
        # don't force a pointless rewrite.
        norm = [ln.rstrip('\r') for ln in body if ln.strip()]
        return hashlib.sha256('\n'.join(norm).encode('utf-8')).hexdigest()

    if cached_body and _digest(online_body) == _digest(cached_body):
        log(f'✓  Ledger unchanged ({n_cached} rows) — using local cache.')
        return online_text, False

    # Something differs — say what, then rewrite in full.
    if not cached_body:
        log(f'📋  Ledger first sync: {n_online} rows.')
    elif n_online > n_cached:
        log(f'➕  Ledger changed: {n_cached} → {n_online} rows '
            f'(+{n_online - n_cached}) — refreshing cache.')
    elif n_online < n_cached:
        log(f'➖  Ledger changed: {n_cached} → {n_online} rows '
            f'({n_online - n_cached}) — refreshing cache.')
    else:
        log(f'✏️  Ledger edited ({n_online} rows, content changed) — refreshing cache.')

    try:
        with open(cache_path, 'wb') as f:
            f.write(online_text.encode('utf-8'))
        log(f'💾  Ledger cache updated — {n_online} rows.')
    except Exception as e:
        log(f'⚠️  Could not write ledger cache: {e}')
    return online_text, True


def _prefetch_sca_dividends(portfolios: dict, log_cb=None, chunk_size: int = 5):
    """
    Pre-fetch dividend history and EPS for every SCA holding in chunks.
    Uses _mf_ticker_for_market() so MY holdings get .KL and US ETFs stay
    as-is (VNQ, SMH etc. — not VNQ.KL which gives 404 on Yahoo Finance).
    """
    def log(m):
        if log_cb: log_cb(m)

    # Collect unique (ticker, yf_lookup, market) tuples from all portfolios
    seen    = set()
    entries = []   # (ticker_key, yf_lookup)
    for port in portfolios.values():
        for ticker, item in port.get('holdings', {}).items():
            if item.get('_is_cash') or ticker in seen:
                continue
            seen.add(ticker)
            mkt    = item.get('market', 'MY')
            lookup = _mf_ticker_for_market(ticker, mkt)
            entries.append((ticker, lookup))

    if not entries:
        return

    log(f'⬇  Pre-fetching div/EPS for {len(entries)} holdings…')

    from concurrent.futures import ThreadPoolExecutor, as_completed as _ac

    def _fetch_one_div(ticker_key, lookup):
        # Skip if both caches are already fresh
        div_fresh = cache_get_divs(lookup) is not None
        eps_fresh = cache_get_live(f'eps_{lookup}', _TTL_EPS) is not None
        if div_fresh and eps_fresh:
            return ticker_key, 'cached'

        try:
            import yfinance as _yf
            yf_t = _yf.Ticker(lookup)

            # Dividends
            if not div_fresh:
                try:
                    divs = yf_t.dividends
                    if divs is not None and not divs.empty:
                        if divs.index.tz is not None:
                            divs.index = divs.index.tz_localize(None)
                        _MF_DIV_CACHE[lookup] = divs
                        cache_set_divs(lookup, divs)
                except Exception:
                    pass

            # EPS
            if not eps_fresh:
                try:
                    fi  = yf_t.fast_info
                    eps = None
                    for attr in ('trailing_eps', 'trailingEps'):
                        v = getattr(fi, attr, None)
                        if v is not None:
                            eps = float(v); break
                    if eps is None:
                        info = yf_t.info
                        eps  = float(info.get('trailingEps') or
                                     info.get('epsTrailingTwelveMonths') or 0.0)
                    if eps is not None:
                        cache_set_live(f'eps_{lookup}', eps)
                except Exception:
                    pass

            return ticker_key, 'fetched'
        except Exception as e:
            return ticker_key, f'error: {e}'

    total   = len(entries)
    fetched = cached = errors = 0
    chunks  = [entries[i:i+chunk_size] for i in range(0, total, chunk_size)]

    for ci, chunk in enumerate(chunks, 1):
        log(f'   Div/EPS chunk {ci}/{len(chunks)} ({len(chunk)} holdings)…')
        with ThreadPoolExecutor(max_workers=chunk_size) as ex:
            futs = {ex.submit(_fetch_one_div, t, lu): t for t, lu in chunk}
            for fut in _ac(futs):
                try:
                    _, status = fut.result()
                    if status == 'cached':   cached  += 1
                    elif status == 'fetched': fetched += 1
                    else:                    errors  += 1
                except Exception:
                    errors += 1

    log(f'✅  Div/EPS: {fetched} fetched, {cached} from cache, {errors} skipped')


def _mf_parse_rows(raw_text: str, log_cb=None) -> list:
    """Parse ledger CSV text into normalised row dicts.
    Shared by the online-sheet path and the imported-CSV path."""
    def log(m):
        if log_cb: log_cb(m)

    reader = csv.DictReader(io.StringIO(raw_text))
    rows = []
    if reader.fieldnames:
        normed = [_mf_norm(k) for k in reader.fieldnames if k]
        log(f'📋  Ledger columns: {normed}')
        expected = {_NC_DATE, _NC_TYPE, _NC_ASSET, _NC_CCY, _NC_UNIT, _NC_VALUE, _NC_AMOUNT}
        missing  = expected - set(normed)
        if missing:
            log(f'⚠️  Ledger missing columns: {sorted(missing)} — check sheet headers')
    for row in reader:
        norm = {_mf_norm(k): v.strip() for k, v in row.items() if k}
        if not norm.get(_NC_ASSET) and not norm.get(_NC_TYPE):
            continue
        rows.append(norm)
    return rows


def _mf_load_csv(url: str = None, log_cb=None,
                 prefer_online: bool = True,
                 local_file: str = None,
                 force_online: bool = False) -> list:
    """
    Load the NEWPOR transaction ledger. Mirrors how teet.csv works.

    url=None (default) resolves the SIGNED-IN USER's own source:
      · mode 'sheet' → their Google Sheet CSV-export URL
      · mode 'csv'   → their imported local CSV (no network)
    Pass an explicit url to override.

    Each K-ID gets its own local cache file (ledger_<kid>.csv) so accounts
    never read each other's portfolio.

    Cache strategy:
      1. local_file < 5 min old         → use as-is, zero network.
      2. prefer_online=True             → download sheet, sync.
      3. Network unreachable            → use local_file regardless of age.

    force_online=True skips step 1 entirely. The TTL exists to make OPENING
    SCA instant — it should never gate a background refresh, or a trade you
    just entered stays invisible until the TTL lapses. Auto-refresh and the
    manual ↻ both pass force_online=True.
    """
    def log(m):
        if log_cb: log_cb(m)

    # ── Resolve the active user's source ─────────────────────────────────────
    src = None
    if url is None:
        try:
            src = sca_get_source(load_app_settings())
        except Exception:
            src = None
        if src and src.get('mode') == 'csv' and src.get('csv_path'):
            # Imported CSV — read straight off disk, never touch the network
            p = src['csv_path']
            if not os.path.exists(p):
                raise FileNotFoundError(
                    f'Your imported CSV is gone: {p}\n'
                    f'Reconnect it via SCA → Source.')
            log(f'📂  Reading imported CSV: {os.path.basename(p)}')
            with open(p, 'r', encoding='utf-8-sig') as f:
                return _mf_parse_rows(f.read(), log_cb=log_cb)
        url = (src or {}).get('url')
        if not url:
            raise ValueError(
                'No portfolio connected for this K-ID.\n'
                'Open SCA → Source to link your Google Sheet or import a CSV.')

    # Derive local file path — per-user so accounts don't collide
    if local_file is None:
        teet_dir = os.path.dirname(os.path.abspath(DEFAULT_INPUT_FILE))
        who = (CURRENT_KID or '').strip().lower()
        name = f'{_NEWPOR_CACHE_PREFIX}_{who or "guest"}.csv'
        local_file = os.path.join(teet_dir, name)

    _NEWPOR_TTL = 300   # 5 min — skip network if file is this fresh

    raw_text = None

    # ── 1. Fast path: local file fresh enough ────────────────────────────────
    # Skipped when force_online — a background refresh must actually go and
    # look, otherwise new rows can't be detected.
    if not force_online and os.path.exists(local_file):
        try:
            age = time_module.time() - os.path.getmtime(local_file)
            if age < _NEWPOR_TTL:
                log(f'📂  Ledger cache {age:.0f}s old — using local copy (no network).')
                with open(local_file, 'r', encoding='utf-8-sig') as f:
                    raw_text = f.read()
        except Exception:
            pass

    # ── 2. Online fetch + delta sync ─────────────────────────────────────────
    if raw_text is None and prefer_online:
        fetch_error = None
        try:
            online_text, row_count = _mf_fetch_online_chunked(url, log_cb=log_cb)
            raw_text, updated = _mf_delta_sync(online_text, local_file, log_cb=log_cb)
            try:
                os.utime(local_file, None)
            except Exception:
                pass
            if updated:
                log(f'💾  Ledger cache updated → {local_file}')
            fetch_error = None
        except Exception as e:
            fetch_error = e
            log(f'⚠️  Ledger online fetch failed: {type(e).__name__}: {e}')
            raw_text = None

    # ── 3. Offline fallback ───────────────────────────────────────────────────
    if raw_text is None:
        if os.path.exists(local_file):
            log(f'📂  Offline — reading cached ledger from {local_file}')
            with open(local_file, 'r', encoding='utf-8-sig') as f:
                raw_text = f.read()
        else:
            raise _mf_URLError(
                f'No cached ledger at {local_file}.\n'
                f'Connect to the internet at least once so the app can download\n'
                f'your ledger sheet and create the local copy.\n'
                f'URL: {url}')

    rows = _mf_parse_rows(raw_text, log_cb=log_cb)
    log(f'✓  NEWPOR loaded: {len(rows)} transactions from {os.path.basename(local_file)}')
    return rows

def _mf_build_portfolios(rows: list) -> dict:
    """
    Convert NEWPOR transaction ledger rows into the standard portfolio dict
    consumed by MYFOLIOWindow / MFChartWindow.

    One portfolio per currency bucket:
        USD  → SWAY1   [SWAY]   (US market, USD)
        MYR  → SWAY1-MYR [SWAY] (MY market, MYR, .KL tickers)

    Cash rows tracked as a synthetic holding so balance is visible.
    Holdings per ticker:
        market, currency, shares (net units), avg_cost (weighted),
        cost_value (total cost at time of buy), realised_pl, last_date
    """
    # bucket rows by currency
    ccy_rows: dict = {}
    for row in rows:
        ccy = row.get(_NC_CCY, "USD").upper().strip() or "USD"
        ccy_rows.setdefault(ccy, []).append(row)

    portfolios: dict = {}

    for ccy, txns in ccy_rows.items():
        code, platform = _NEWPOR_CCY_MAP.get(ccy, (f"SWAY1-{ccy}", "SWAY"))
        market         = MARKET_SPEC.get(ccy, {}).get("market", "US")

        holdings: dict = {}
        cash_balance    = 0.0
        total_deposited = 0.0
        total_withdrawn = 0.0
        total_fees      = 0.0
        realised_pl     = 0.0

        for row in txns:
            ttype  = row.get(_NC_TYPE,  "").strip().lower()
            asset  = row.get(_NC_ASSET, "").upper().strip()
            date_s = row.get(_NC_DATE,  "")
            unit   = _mf_strip_currency(row.get(_NC_UNIT,   "0"))
            price  = _mf_strip_currency(row.get(_NC_VALUE,  "0"))
            amount = _mf_strip_currency(row.get(_NC_AMOUNT, "0"))

            if not asset:
                continue

            # ── Cash flow events ──────────────────────────────────────────────
            if asset in _CASH_ASSETS or "cash" in asset.lower():
                if ttype == "deposit":
                    cash_balance    += amount
                    total_deposited += amount
                elif ttype in ("withdraw", "widthdraw"):  # typo in source data
                    cash_balance    += amount             # amount already negative
                    total_withdrawn += abs(amount)
                elif ttype == "fees":
                    cash_balance    += amount             # amount already negative
                    total_fees      += abs(amount)
                continue

            # Infer per-ticker market: .KL suffix always MY/MYR regardless of bucket
            t_market   = "MY" if asset.endswith(".KL") else market
            t_currency = "MYR" if t_market == "MY" else ccy

            # ── Buy ───────────────────────────────────────────────────────────
            if ttype == "buy":
                cash_balance -= abs(amount)
                if asset not in holdings:
                    holdings[asset] = {
                        "market": t_market, "currency": t_currency,
                        "shares": 0.0, "avg_cost": 0.0,
                        "cost_value": 0.0, "realised_pl": 0.0,
                        "date": date_s, "transactions": [],
                    }
                h = holdings[asset]
                old_s = h["shares"]; new_s = old_s + unit
                if new_s > 0:
                    h["avg_cost"] = ((old_s * h["avg_cost"]) + (unit * price)) / new_s
                h["shares"]     = new_s
                h["cost_value"] += abs(amount)
                h["date"]        = date_s
                h["transactions"].append(("buy", date_s, unit, price, abs(amount)))

            # ── Sell ──────────────────────────────────────────────────────────
            elif ttype == "sell":
                cash_balance += abs(amount)
                if asset in holdings:
                    h          = holdings[asset]
                    sold_units = abs(unit)
                    cost_basis = h["avg_cost"] * sold_units
                    proceeds   = abs(amount)
                    rpl        = proceeds - cost_basis
                    h["realised_pl"] += rpl
                    realised_pl      += rpl
                    h["shares"]      -= sold_units
                    h["cost_value"]  -= cost_basis
                    h["date"]         = date_s
                    h["transactions"].append(("sell", date_s, -sold_units, price, proceeds))
                    if h["shares"] <= 1e-8:
                        del holdings[asset]

            # ── Dividend ──────────────────────────────────────────────────────
            elif ttype in ("dividend", "div"):
                cash_balance += abs(amount)

        # Synthetic CASH holding so balance shows in holdings table
        if abs(cash_balance) > 0.0001:
            holdings[f"{ccy}-CASH"] = {
                "market": market, "currency": ccy,
                "shares": cash_balance, "avg_cost": 1.0,
                "cost_value": cash_balance, "realised_pl": 0.0,
                "date": "", "transactions": [],
                "_is_cash": True,
            }

        portfolios[code] = {
            "code":             code,
            "platform":         platform,
            "markets":          [market],
            "currencies":       [ccy],
            "base_currency":    ccy,
            "holdings":         holdings,
            "_total_deposited": total_deposited,
            "_total_withdrawn": total_withdrawn,
            "_total_fees":      total_fees,
            "_realised_pl":     realised_pl,
        }

    return portfolios

def _mf_validate_ledger(rows: list) -> dict:
    """
    Validates the NEWPOR transaction ledger for both:
      1) SCHEMA  — required columns must exist and have a recognizable header
      2) ROW-LEVEL — every Buy/Sell row must have valid numeric Unit/Value/
         Amount, every row must have a recognized Transaction Type, and
         AssetCode must be non-empty.

    Returns a dict:
        {
          "ok": bool,
          "errors": [str, ...],     # blocking problems
          "warnings": [str, ...],   # non-blocking but worth flagging
          "row_count": int,
          "checked_at": datetime,
        }
    """
    import datetime as _dt
    errors   = []
    warnings = []

    REQUIRED_COLS = {_NC_DATE, _NC_TYPE, _NC_ASSET, _NC_CCY, _NC_UNIT, _NC_VALUE, _NC_AMOUNT}
    KNOWN_TYPES   = {"deposit", "withdraw", "widthdraw", "buy", "sell", "fees", "dividend", "div"}

    if not rows:
        # A brand-new / not-yet-used sheet with zero transaction rows is a
        # perfectly valid fresh-setup state — there's simply nothing entered
        # yet. This should NOT lock the app; only genuinely malformed or
        # unreachable data should. The lock applies once real data exists
        # and fails validation, not before any data has been entered.
        return {"ok": True, "errors": [], "warnings": [],
                "row_count": 0, "checked_at": _dt.datetime.now()}

    # ── 1) SCHEMA CHECK — required columns present in at least one row ──────
    seen_cols = set()
    for row in rows:
        seen_cols |= set(row.keys())
    missing_cols = REQUIRED_COLS - seen_cols
    if missing_cols:
        errors.append(f"Missing required column(s): {', '.join(sorted(missing_cols))}")

    # ── 2) ROW-LEVEL CHECK — every row's data must be internally consistent ─
    bad_rows = []
    for i, row in enumerate(rows, start=2):  # start=2: row 1 is the header
        ttype = row.get(_NC_TYPE, "").strip().lower()
        asset = row.get(_NC_ASSET, "").strip()

        if not asset:
            bad_rows.append(f"Row {i}: missing AssetCode")
            continue
        if not ttype:
            bad_rows.append(f"Row {i} ({asset}): missing Transaction Type")
            continue
        if ttype not in KNOWN_TYPES:
            bad_rows.append(f"Row {i} ({asset}): unrecognized Transaction Type '{ttype}'")
            continue

        is_cash = asset.upper() in _CASH_ASSETS or "cash" in asset.lower()
        if ttype in ("buy", "sell") and not is_cash:
            unit_raw   = row.get(_NC_UNIT,   "")
            value_raw  = row.get(_NC_VALUE,  "")
            amount_raw = row.get(_NC_AMOUNT, "")
            unit   = _mf_strip_currency(unit_raw)
            value  = _mf_strip_currency(value_raw)
            amount = _mf_strip_currency(amount_raw)
            if unit_raw.strip() and unit == 0.0 and unit_raw.strip() not in ("0", "0.0", "0.0000"):
                bad_rows.append(f"Row {i} ({asset}): Unit '{unit_raw}' is not a valid number")
            if value_raw.strip() and value == 0.0 and value_raw.strip() not in ("0", "0.0"):
                bad_rows.append(f"Row {i} ({asset}): Value '{value_raw}' is not a valid number")
            if amount_raw.strip() and amount == 0.0 and amount_raw.strip() not in ("0", "0.0"):
                bad_rows.append(f"Row {i} ({asset}): Amount '{amount_raw}' is not a valid number")
            # Sanity: Unit x Value should roughly equal Amount (within 5% or RM/USD0.05,
            # whichever is larger — small rounding in the source sheet is expected)
            expected = abs(unit) * value
            actual   = abs(amount)
            tolerance = max(0.05, expected * 0.05)
            if expected > 0 and abs(expected - actual) > tolerance:
                warnings.append(
                    f"Row {i} ({asset}): Unit×Value ({expected:.4f}) doesn't match "
                    f"Amount ({actual:.4f}) — possible data entry error"
                )

    if bad_rows:
        errors.append(f"{len(bad_rows)} row(s) failed validation:")
        errors.extend(bad_rows[:15])   # cap detail list so the dialog stays readable
        if len(bad_rows) > 15:
            errors.append(f"...and {len(bad_rows) - 15} more.")

    ok = not errors
    return {
        "ok": ok, "errors": errors, "warnings": warnings,
        "row_count": len(rows), "checked_at": _dt.datetime.now(),
    }



# ── MYFOLIO Chart Window (Toplevel) ───────────────────────────────────────────
class MFChartWindow(tk.Toplevel):
    _PAL = [
        "#58A6FF","#3FB950","#BC8CFF","#D29922","#F0883E",
        "#F85149","#79C0FF","#56D364","#E3B341","#FF7B72",
        "#D2A8FF","#FFA657","#8B949E","#2EA043","#1F6FEB",
    ]

    def __init__(self, parent, portfolio: dict, all_portfolios: dict):
        super().__init__(parent)
        self.title(f"Charts  —  {portfolio['code']}  [{portfolio['platform']}]")
        self.configure(bg=_MF_BG)
        self.geometry("980x660"); self.minsize(740, 520)
        self.grab_set()
        self._port = portfolio
        self._all  = all_portfolios

        try:
            import matplotlib as _mpl
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg as _FC
            from matplotlib.figure import Figure as _Fig
            import numpy as _np
            self._FC = _FC; self._Fig = _Fig; self._np = _np
            self._has_mpl = True
        except ImportError:
            self._has_mpl = False

        if not self._has_mpl:
            tk.Label(self, text="matplotlib / numpy not installed.\n\npip install matplotlib numpy",
                     font=_MF_FONT_MD, bg=_MF_BG, fg=_MF_RED, justify="center").pack(expand=True)
            return

        tab_bar = tk.Frame(self, bg=_MF_PANEL, height=42)
        tab_bar.pack(fill="x"); tab_bar.pack_propagate(False)
        self._canvas_frame = tk.Frame(self, bg=_MF_BG)
        self._canvas_frame.pack(fill="both", expand=True)

        self._tab_btns = {}
        tabs = [
            ("TP vs TA", self._draw_tp_ta),
            ("TP Pie",   self._draw_tp_pie),
            ("SP Pie",   self._draw_sp_pie),
            ("P/L Bar",  self._draw_pl_bar),
            ("P/L %",    self._draw_pl_pct),
        ]
        for label, fn in tabs:
            btn = tk.Button(tab_bar, text=label, font=_MF_FONT_SM,
                            bg=_MF_CARD, fg=_MF_TEXT, relief="flat", bd=0, padx=14, pady=8,
                            command=lambda f=fn, l=label: self._switch(l, f))
            btn.pack(side="left", padx=2, pady=4)
            self._tab_btns[label] = btn
        self._switch("TP vs TA", self._draw_tp_ta)

    def _switch(self, label, draw_fn):
        for lbl, btn in self._tab_btns.items():
            btn.configure(bg=_MF_ACCENT if lbl == label else _MF_CARD,
                          fg=_MF_BG     if lbl == label else _MF_TEXT)
        for w in self._canvas_frame.winfo_children(): w.destroy()
        draw_fn()

    def _new_fig(self, w=9, h=5.4):
        return self._Fig(figsize=(w, h), facecolor=_MF_BG)

    def _embed(self, fig):
        c = self._FC(fig, master=self._canvas_frame)
        c.draw(); c.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=4)

    def _ax_dark(self, ax):
        ax.set_facecolor(_MF_BG); ax.spines[:].set_color(_MF_BORDER)
        ax.tick_params(colors=_MF_TEXT, labelsize=8)
        ax.yaxis.label.set_color(_MF_SUBTEXT); ax.xaxis.label.set_color(_MF_SUBTEXT)
        ax.title.set_color(_MF_TEXT)

    def _no_data(self, ax, msg="No priced holdings to display"):
        ax.text(0.5, 0.5, msg, ha="center", va="center",
                color=_MF_SUBTEXT, fontsize=11, transform=ax.transAxes)
        ax.axis("off")

    def _holdings_rows(self, port=None):
        if port is None: port = self._port
        rows = []
        for ticker, item in port["holdings"].items():
            if item.get("_is_cash"): continue   # skip cash pseudo-holding in charts
            price = _mf_get_price(ticker, item["market"])
            if price is None: continue
            costv = item["cost_value"]; vt = item["shares"] * price
            pl = vt - costv; pl_pct = (pl / costv * 100) if costv else 0.0
            rows.append(dict(ticker=ticker, currency=item["currency"],
                             shares=item["shares"], avg_cost=item["avg_cost"],
                             price=price, cost_total=costv, value_total=vt,
                             pl=pl, pl_pct=pl_pct))
        return rows

    def _draw_tp_ta(self):
        rows = self._holdings_rows(); fig = self._new_fig(); ax = fig.add_subplot(111)
        self._ax_dark(ax)
        if not rows:
            self._no_data(ax)
        else:
            labels  = [r["ticker"] for r in rows]
            ta_vals = [r["cost_total"] for r in rows]
            tp_vals = [r["value_total"] for r in rows]
            x = self._np.arange(len(labels)); w = 0.38
            ax.bar(x - w/2, ta_vals, w, label="TA (Cost Value)", color=_MF_SUBTEXT, edgecolor=_MF_BG, linewidth=0.5)
            ax.bar(x + w/2, tp_vals, w,
                   color=[_MF_GREEN if tp >= ta else _MF_RED for tp, ta in zip(tp_vals, ta_vals)],
                   label="TP (Market Value)", edgecolor=_MF_BG, linewidth=0.5)
            ax.set_xticks(x); ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8, color=_MF_TEXT)
            ax.set_ylabel("Value", fontsize=9); ax.axhline(0, color=_MF_BORDER, linewidth=0.5)
            for xi, (ta, tp) in enumerate(zip(ta_vals, tp_vals)):
                ax.text(xi - w/2, ta * 1.01, f"{ta:,.0f}", ha="center", va="bottom", fontsize=6, color=_MF_SUBTEXT)
                ax.text(xi + w/2, tp * 1.01, f"{tp:,.0f}", ha="center", va="bottom", fontsize=6,
                        color=_MF_GREEN if tp >= ta else _MF_RED)
            ax.legend(facecolor=_MF_PANEL, edgecolor=_MF_BORDER, labelcolor=_MF_TEXT, fontsize=8)
            ttl_ta = sum(ta_vals); ttl_tp = sum(tp_vals); pl = ttl_tp - ttl_ta
            pct = (pl / ttl_ta * 100) if ttl_ta else 0.0
            ax.annotate(f"Total Cost: {ttl_ta:,.2f}   Market Value: {ttl_tp:,.2f}   P/L: {pl:+,.2f} ({pct:+.2f}%)",
                        xy=(0.5, 1.03), xycoords="axes fraction", ha="center", fontsize=8,
                        color=_MF_GREEN if pl >= 0 else _MF_RED)
        ax.set_title("Total Portfolio vs Total Allocated (CostV)", fontsize=11, pad=24)
        fig.tight_layout(); self._embed(fig)

    def _draw_tp_pie(self):
        rows = self._holdings_rows(); fig = self._new_fig(); ax = fig.add_subplot(111)
        ax.set_facecolor(_MF_BG)
        if not rows:
            self._no_data(ax)
        else:
            sizes  = [r["value_total"] for r in rows]
            colors = [self._PAL[i % len(self._PAL)] for i in range(len(rows))]
            wedges, _, autotexts = ax.pie(sizes, colors=colors, autopct="%1.1f%%",
                                          startangle=140, pctdistance=0.80,
                                          wedgeprops=dict(linewidth=1.5, edgecolor=_MF_BG))
            for at in autotexts: at.set_color(_MF_BG); at.set_fontsize(8); at.set_fontweight("bold")
            ax.legend(wedges,
                      [f"{r['ticker']}  {r['currency']} {r['value_total']:,.2f}" for r in rows],
                      loc="center left", bbox_to_anchor=(1.0, 0.5),
                      fontsize=8, facecolor=_MF_PANEL, edgecolor=_MF_BORDER, labelcolor=_MF_TEXT)
            ax.annotate(f"Total: {sum(sizes):,.2f}", xy=(0.5, -0.08), xycoords="axes fraction",
                        ha="center", fontsize=9, color=_MF_ACCENT)
        ax.set_title("Portfolio Allocation by Current Market Value", fontsize=11, pad=12)
        fig.tight_layout(); self._embed(fig)

    def _draw_sp_pie(self):
        plat_val: dict = {}
        for port in self._all.values():
            plat = port.get("platform") or "Unknown"
            for ticker, item in port["holdings"].items():
                price = _mf_get_price(ticker, item["market"])
                val   = item["shares"] * (price if price is not None else item["avg_cost"])
                plat_val[plat] = plat_val.get(plat, 0.0) + val
        fig = self._new_fig(); ax = fig.add_subplot(111); ax.set_facecolor(_MF_BG)
        if not plat_val or all(v == 0 for v in plat_val.values()):
            self._no_data(ax, "No data across portfolios")
        else:
            labels = list(plat_val.keys()); sizes = list(plat_val.values())
            colors = [self._PAL[i % len(self._PAL)] for i in range(len(labels))]
            wedges, _, autotexts = ax.pie(sizes, colors=colors, autopct="%1.1f%%",
                                          startangle=140, pctdistance=0.80,
                                          wedgeprops=dict(linewidth=1.5, edgecolor=_MF_BG))
            for at in autotexts: at.set_color(_MF_BG); at.set_fontsize(8); at.set_fontweight("bold")
            ax.legend(wedges, [f"{l}  {v:,.2f}" for l, v in zip(labels, sizes)],
                      loc="center left", bbox_to_anchor=(1.0, 0.5),
                      fontsize=8, facecolor=_MF_PANEL, edgecolor=_MF_BORDER, labelcolor=_MF_TEXT)
            ax.annotate(f"Grand Total (all platforms): {sum(sizes):,.2f}",
                        xy=(0.5, -0.08), xycoords="axes fraction", ha="center", fontsize=9, color=_MF_ACCENT)
        ax.set_title("Allocation by Platform — All Portfolios", fontsize=11, pad=12)
        fig.tight_layout(); self._embed(fig)

    def _draw_pl_bar(self):
        rows = self._holdings_rows(); fig = self._new_fig(); ax = fig.add_subplot(111)
        self._ax_dark(ax)
        if not rows:
            self._no_data(ax)
        else:
            labels = [r["ticker"] for r in rows]; vals = [r["pl"] for r in rows]; pcts = [r["pl_pct"] for r in rows]
            clrs = [_MF_GREEN if v >= 0 else _MF_RED for v in vals]
            x = self._np.arange(len(labels)); bars = ax.bar(x, vals, color=clrs, edgecolor=_MF_BG, linewidth=0.5)
            ax.set_xticks(x); ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8, color=_MF_TEXT)
            ax.axhline(0, color=_MF_BORDER, linewidth=0.8); ax.set_ylabel("Unrealised P/L", fontsize=9)
            mx = max((abs(v) for v in vals), default=1)
            for bar, v, p in zip(bars, vals, pcts):
                off = mx * 0.03
                ypos = (bar.get_height() + off if v >= 0 else bar.get_height() - off * 2)
                ax.text(bar.get_x() + bar.get_width() / 2, ypos,
                        f"{v:+,.2f}\n({p:+.1f}%)",
                        ha="center", va="bottom", fontsize=6, color=_MF_GREEN if v >= 0 else _MF_RED)
        ax.set_title("Unrealised P/L per Holding", fontsize=11, pad=12)
        fig.tight_layout(); self._embed(fig)

    def _draw_pl_pct(self):
        rows = sorted(self._holdings_rows(), key=lambda r: r["pl_pct"])
        fig  = self._new_fig(); ax = fig.add_subplot(111); self._ax_dark(ax)
        if not rows:
            self._no_data(ax)
        else:
            labels = [r["ticker"] for r in rows]; pcts = [r["pl_pct"] for r in rows]
            clrs = [_MF_GREEN if p >= 0 else _MF_RED for p in pcts]
            y = self._np.arange(len(labels)); bars = ax.barh(y, pcts, color=clrs, edgecolor=_MF_BG, linewidth=0.5)
            ax.set_yticks(y); ax.set_yticklabels(labels, fontsize=8, color=_MF_TEXT)
            ax.axvline(0, color=_MF_BORDER, linewidth=0.8); ax.set_xlabel("P/L %", fontsize=9)
            for bar, p in zip(bars, pcts):
                xpos = p + (0.4 if p >= 0 else -0.4)
                ax.text(xpos, bar.get_y() + bar.get_height() / 2,
                        f"{p:+.2f}%", ha="left" if p >= 0 else "right",
                        va="center", fontsize=7, color=_MF_GREEN if p >= 0 else _MF_RED)
        ax.set_title("Unrealised Return % per Holding (sorted)", fontsize=11, pad=12)
        fig.tight_layout(); self._embed(fig)


# ── MYFOLIO Main Window — 3-view system ─────────────────────────────────────
class MYFOLIOWindow(tk.Toplevel):
    """
    MYFOLIO Portfolio Command Center.
    Layout mirrors MYIPO+ Dashboard exactly:
      - #0d1117 bg, #161b22 sidebar, Segoe UI fonts
      - ttk.Notebook tab bar
      - Sidebar: portfolio list + currency toggle + period + summary treeview
    Tabs: 📈 Line Chart | 📊 Holdings Bar | 🗃️ Data Table | 💰 Total P/L | 🧩 Composition
    """
    PERIODS = ['1D', '5D', '1M', '3M', '6M', 'YTD', '1Y', 'All']

    def __init__(self, parent, preloaded_rows=None, preloaded_portfolios=None):
        super().__init__(parent)
        self.title("MYFOLIO — Portfolio Command Center")
        self.geometry("1300x820"); self.minsize(1060, 680)
        self.configure(bg='#0d1117')
        self.portfolios: dict = {}
        self.active_code      = tk.StringVar(value="")
        self._loading         = False
        self._status_var      = tk.StringVar(value="")
        self._cur_ccy         = "USD"
        self._period_var      = tk.StringVar(value="All")
        self._total_ccy_var   = tk.StringVar(value="Combined")
        self._build_ui()

        if preloaded_portfolios:
            # Instant open — data was already fetched/cached at SplashScreen
            # startup, same pattern as MYIPO's own preload. No network wait.
            # Only used when the preload actually produced real data — a
            # falsy value here (None, or {} from a failed preload) falls
            # through to a normal live fetch instead of opening empty.
            import datetime as _dt
            self.portfolios = preloaded_portfolios
            now = _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self._status_var.set(f'✓ Preloaded  ({len(preloaded_portfolios)} portfolios)  —  {now}')
            self._rebuild_list()
            self._refresh_detail()
        else:
            self._load_async()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Top bar — same colours/fonts as MYIPOApp
        top = tk.Frame(self, bg='#0d1117', pady=6)
        top.pack(fill='x', padx=10)
        tk.Label(top, text="MYFOLIO", bg='#0d1117', fg='#58A6FF',
                 font=('Segoe UI', 16, 'bold')).pack(side='left')
        tk.Label(top, text="  Portfolio Command Center",
                 bg='#0d1117', fg='#888', font=('Segoe UI', 10)).pack(side='left')
        tk.Label(top, text=" │ SCA-NEWPOR",
                 bg='#0d1117', fg='#D29922', font=('Segoe UI', 8)).pack(side='left')
        self._clock_var = tk.StringVar(value="")
        tk.Label(top, textvariable=self._clock_var,
                 bg='#0d1117', fg='#444', font=('Segoe UI', 8)).pack(side='right', padx=8)
        tk.Button(top, text='⟳  Refresh', command=self._load_async,
                  bg='#58A6FF', fg='#0d1117', font=('Segoe UI', 10, 'bold'),
                  relief='flat', padx=12, pady=4, cursor='hand2').pack(side='right', padx=4)
        self.status_lbl = tk.Label(top, textvariable=self._status_var,
                                   bg='#0d1117', fg='#D29922', font=('Segoe UI', 8))
        self.status_lbl.pack(side='right', padx=12)
        self._tick_clock()

        # Main body
        main = tk.Frame(self, bg='#0d1117')
        main.pack(fill='both', expand=True, padx=10, pady=(0, 4))

        # ── Sidebar ──────────────────────────────────────────────────────────
        sb = tk.Frame(main, bg='#161b22', width=275, relief='flat')
        sb.pack(side='left', fill='y', padx=(0, 8))
        sb.pack_propagate(False)

        sb_top = tk.Frame(sb, bg='#161b22'); sb_top.pack(fill='x', side='top')

        tk.Label(sb_top, text="Portfolios", bg='#161b22', fg='#cdd9e5',
                 font=('Segoe UI', 11, 'bold')).pack(anchor='w', padx=10, pady=(10, 4))

        # USD / MYR currency selector
        ccy_row = tk.Frame(sb_top, bg='#161b22'); ccy_row.pack(fill='x', padx=8, pady=(0, 4))
        self._ccy_btns = {}
        for ccy, label in [("USD", "● USD"), ("MYR", "● MYR")]:
            is_active = (ccy == "USD")
            btn = tk.Button(ccy_row, text=label,
                            bg='#58A6FF' if is_active else '#21262d',
                            fg='#0d1117' if is_active else '#cdd9e5',
                            font=('Segoe UI', 8, 'bold'), relief='flat',
                            padx=6, pady=3, cursor='hand2',
                            command=lambda c=ccy: self._switch_currency(c))
            btn.pack(side='left', expand=True, fill='x', padx=1)
            self._ccy_btns[ccy] = btn

        self.portfolio_list = tk.Listbox(
            sb_top, bg='#161b22', fg='#cdd9e5',
            selectbackground='#2d333b', selectforeground='#58A6FF',
            font=('Segoe UI', 9), relief='flat', bd=0,
            activestyle='none', height=5)
        self.portfolio_list.pack(fill='x', padx=8, pady=(0, 6))
        self.portfolio_list.bind('<<ListboxSelect>>', self._on_select)

        # Period selector — grid, mirrors MYIPO+
        sb_bot = tk.Frame(sb, bg='#161b22'); sb_bot.pack(fill='both', expand=True, side='bottom')
        tk.Label(sb_bot, text="Period", bg='#161b22', fg='#cdd9e5',
                 font=('Segoe UI', 11, 'bold')).pack(anchor='w', padx=10, pady=(6, 2))
        pf = tk.Frame(sb_bot, bg='#161b22'); pf.pack(fill='x', padx=8)
        for i, p in enumerate(self.PERIODS):
            tk.Radiobutton(
                pf, text=p, variable=self._period_var, value=p,
                command=self._on_period_change,
                bg='#161b22', fg='#cdd9e5', selectcolor='#58A6FF',
                activebackground='#161b22', activeforeground='#58A6FF',
                font=('Segoe UI', 9), indicatoron=False,
                relief='flat', bd=0, padx=6, pady=3, cursor='hand2'
            ).grid(row=i // 4, column=i % 4, sticky='ew', padx=2, pady=2)
        for c in range(4): pf.columnconfigure(c, weight=1)

        # Summary treeview
        tk.Label(sb_bot, text="Summary", bg='#161b22', fg='#cdd9e5',
                 font=('Segoe UI', 11, 'bold')).pack(anchor='w', padx=10, pady=(10, 2))
        self.summary_tree = ttk.Treeview(
            sb_bot, columns=('Ticker', 'Units', 'Cost', 'MktVal', 'P/L%'),
            show='headings', height=20)
        _sty = ttk.Style()
        _sty.theme_use('default')
        _sty.configure('Treeview',
                       background='#161b22', foreground='#cdd9e5',
                       fieldbackground='#161b22', rowheight=22,
                       font=('Segoe UI', 8))
        _sty.configure('Treeview.Heading',
                       background='#21262d', foreground='#cdd9e5',
                       font=('Segoe UI', 8, 'bold'), relief='flat')
        _sty.map('Treeview', background=[('selected', '#2d333b')])
        for col, w, anc in [('Ticker', 76, 'w'), ('Units', 64, 'e'), ('Cost', 56, 'e'),
                             ('MktVal', 56, 'e'), ('P/L%', 52, 'e')]:
            self.summary_tree.heading(col, text=col)
            self.summary_tree.column(col, width=w, anchor=anc, stretch=False)
        self.summary_tree.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self.summary_tree.tag_configure('gain', foreground='#3FB950')
        self.summary_tree.tag_configure('loss', foreground='#F85149')

        # ── Right panel ───────────────────────────────────────────────────────
        right = tk.Frame(main, bg='#0d1117')
        right.pack(side='left', fill='both', expand=True)

        # Info strip (mirrors MYIPO+ future_header)
        self._info_var = tk.StringVar(value="MYFOLIO  —  Select a portfolio.")
        tk.Label(right, textvariable=self._info_var,
                 bg='#101820', fg='#cdd9e5', anchor='w', padx=10, pady=5,
                 font=('Segoe UI', 9), relief='flat').pack(fill='x', pady=(0, 4))

        # Notebook
        self.nb = ttk.Notebook(right)
        _sty.configure('TNotebook', background='#0d1117', borderwidth=0)
        _sty.configure('TNotebook.Tab', background='#21262d', foreground='#cdd9e5',
                       font=('Segoe UI', 9), padding=[10, 4])
        _sty.map('TNotebook.Tab',
                 background=[('selected', '#161b22')],
                 foreground=[('selected', '#00C9FF')])
        self.nb.pack(fill='both', expand=True)

        self.tab_line  = tk.Frame(self.nb, bg='#0d1117')
        self.nb.add(self.tab_line,  text='📈  Line Chart')
        self._build_line_tab()

        self.tab_bar   = tk.Frame(self.nb, bg='#0d1117')
        self.nb.add(self.tab_bar,   text='📊  Holdings Bar')
        self._build_bar_tab()

        self.tab_table = tk.Frame(self.nb, bg='#0d1117')
        self.nb.add(self.tab_table, text='🗃️  Data Table')
        self._build_table_tab()

        self.tab_total = tk.Frame(self.nb, bg='#0d1117')
        self.nb.add(self.tab_total, text='💰  Total P/L')
        self._build_total_tab()

        self.tab_comp  = tk.Frame(self.nb, bg='#0d1117')
        self.nb.add(self.tab_comp,  text='🧩  Composition')
        self._build_comp_tab()

        self.nb.bind('<<NotebookTabChanged>>', lambda e: self._redraw_active_tab())

        # Status bar
        tk.Label(self, textvariable=self._status_var,
                 bg='#161b22', fg='#888', font=('Segoe UI', 9),
                 anchor='w', padx=10).pack(fill='x', side='bottom')

    # ── Tab builders ──────────────────────────────────────────────────────────
    def _make_fig(self):
        try:
            from matplotlib.figure import Figure as _Fig
            return _Fig(figsize=(10, 5.5), facecolor='#0d1117')
        except ImportError:
            return None

    def _style_ax(self, ax):
        ax.set_facecolor('#0d1117')
        ax.tick_params(colors='#888', labelsize=8)
        ax.spines['bottom'].set_color('#333'); ax.spines['left'].set_color('#333')
        ax.spines['top'].set_visible(False);  ax.spines['right'].set_visible(False)
        ax.yaxis.label.set_color('#888');     ax.xaxis.label.set_color('#888')
        ax.grid(True, color='#1e2530', linewidth=0.5, linestyle='--')

    def _embed_fig(self, fig, parent):
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg as _FC
            c = _FC(fig, master=parent); c.draw()
            c.get_tk_widget().pack(fill='both', expand=True)
            return c
        except Exception:
            return None

    def _build_line_tab(self):
        self._fig_line = self._make_fig()
        if self._fig_line:
            self._ax_line = self._fig_line.add_subplot(111)
            self._style_ax(self._ax_line)
            self._canvas_line = self._embed_fig(self._fig_line, self.tab_line)

    def _build_bar_tab(self):
        self._fig_bar = self._make_fig()
        if self._fig_bar:
            self._ax_bar = self._fig_bar.add_subplot(111)
            self._style_ax(self._ax_bar)
            self._canvas_bar = self._embed_fig(self._fig_bar, self.tab_bar)

    def _build_table_tab(self):
        tk.Label(self.tab_table, text='Transaction history with running unit totals (cumulative, like the chart)',
                 bg='#0d1117', fg='#666', font=('Segoe UI', 8)).pack(anchor='w', padx=4, pady=(2, 0))
        frame = tk.Frame(self.tab_table, bg='#0d1117')
        frame.pack(fill='both', expand=True)
        hold_cols = ('Date','Ticker','Type','Units','Price','Amount',
                     'Running Units','Running Cost')
        self.hold_tree = ttk.Treeview(frame, columns=hold_cols, show='headings')
        cw = {'Date':88,'Ticker':85,'Type':56,'Units':78,'Price':78,
              'Amount':85,'Running Units':100,'Running Cost':100}
        for col in hold_cols:
            self.hold_tree.heading(col, text=col)
            self.hold_tree.column(col, width=cw.get(col,80),
                                  anchor='w' if col in ('Date','Ticker','Type') else 'e',
                                  stretch=False)
        vsb = ttk.Scrollbar(frame, orient='vertical',   command=self.hold_tree.yview)
        hsb = ttk.Scrollbar(frame, orient='horizontal', command=self.hold_tree.xview)
        self.hold_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.hold_tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)
        self.hold_tree.tag_configure('gain', foreground='#3FB950')
        self.hold_tree.tag_configure('loss', foreground='#F85149')
        self.hold_tree.tag_configure('buy',  foreground='#58A6FF')
        self.hold_tree.tag_configure('sell', foreground='#F0883E')

    def _build_total_tab(self):
        cbar = tk.Frame(self.tab_total, bg='#161b22', height=38)
        cbar.pack(fill='x', padx=10, pady=(8,4)); cbar.pack_propagate(False)
        tk.Label(cbar, text='Show:', bg='#161b22', fg='#cdd9e5',
                 font=('Segoe UI', 9)).pack(side='left', padx=10)
        self._tot_ccy_btns = {}
        for opt in ('Combined', 'USD', 'MYR'):
            is_def = (opt == 'Combined')
            btn = tk.Button(cbar, text=opt,
                            bg='#00C9FF' if is_def else '#21262d',
                            fg='#0d1117' if is_def else '#cdd9e5',
                            font=('Segoe UI', 8, 'bold'), relief='flat',
                            padx=10, pady=3, cursor='hand2',
                            command=lambda o=opt: self._set_total_ccy(o))
            btn.pack(side='left', padx=3)
            self._tot_ccy_btns[opt] = btn
        frame = tk.Frame(self.tab_total, bg='#0d1117')
        frame.pack(fill='both', expand=True, padx=10, pady=4)
        tot_cols = ('Portfolio','Ccy','Type','Cost','Mkt Value',
                    'P/L','P/L %','Real P/L')
        self.total_tree = ttk.Treeview(frame, columns=tot_cols, show='headings')
        tw = {'Portfolio':130,'Ccy':60,'Type':90,'Cost':100,'Mkt Value':100,
              'P/L':100,'P/L %':80,'Real P/L':100}
        for col in tot_cols:
            self.total_tree.heading(col, text=col)
            self.total_tree.column(col, width=tw.get(col,80),
                                   anchor='w' if col in ('Portfolio','Type') else 'e',
                                   stretch=False)
        vsb2 = ttk.Scrollbar(frame, orient='vertical',   command=self.total_tree.yview)
        hsb2 = ttk.Scrollbar(frame, orient='horizontal', command=self.total_tree.xview)
        self.total_tree.configure(yscrollcommand=vsb2.set, xscrollcommand=hsb2.set)
        self.total_tree.grid(row=0, column=0, sticky='nsew')
        vsb2.grid(row=0, column=1, sticky='ns')
        hsb2.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)
        self.total_tree.tag_configure('gain',    foreground='#3FB950')
        self.total_tree.tag_configure('loss',    foreground='#F85149')
        self.total_tree.tag_configure('section', foreground='#8B949E', background='#21262d')
        self.total_tree.tag_configure('grand',   foreground='#00C9FF', background='#1a2030')

    def _build_comp_tab(self):
        self._fig_comp = self._make_fig()
        if self._fig_comp:
            self._ax_comp = self._fig_comp.add_subplot(111)
            self._ax_comp.set_facecolor('#0d1117')
            self._canvas_comp = self._embed_fig(self._fig_comp, self.tab_comp)

    # ── Clock ─────────────────────────────────────────────────────────────────
    def _tick_clock(self):
        import datetime as _dt
        self._clock_var.set(_dt.datetime.now().strftime('%Y-%m-%d  %H:%M:%S'))
        self.after(1000, self._tick_clock)

    # ── Currency switch ───────────────────────────────────────────────────────
    def _switch_currency(self, ccy: str):
        self._cur_ccy = ccy
        for c, btn in self._ccy_btns.items():
            active = (c == ccy)
            btn.configure(bg='#58A6FF' if active else '#21262d',
                          fg='#0d1117' if active else '#cdd9e5')
        self._rebuild_list(); self._refresh_detail()

    def _portfolios_for_currency(self, ccy: str) -> dict:
        if not self.portfolios:
            return {}
        return {code: port for code, port in self.portfolios.items()
                if port.get('base_currency', 'USD').upper() == ccy.upper()}

    # ── Data loading ──────────────────────────────────────────────────────────
    def _load_async(self):
        if self._loading: return
        self._loading = True
        _MF_PRICE_CACHE.clear()
        self._status_var.set('⟳ Syncing NEWPOR database…')
        import threading as _thr
        _thr.Thread(target=self._fetch_thread, daemon=True).start()

    def _fetch_thread(self):
        def _log(msg):
            self.after(0, lambda: self._status_var.set(msg))
        try:
            rows  = _mf_load_csv(log_cb=_log)
            ports = _mf_build_portfolios(rows)
            self.after(0, lambda: self._on_loaded(ports, len(rows)))
        except _mf_URLError as exc:
            self.after(0, lambda: self._on_error(f'Network error: {exc.reason}'))
        except Exception as exc:
            import traceback as _tb
            tb_text = _tb.format_exc()
            self.after(0, lambda: self._on_error(f'{exc}\n\n{tb_text}'))

    def _on_loaded(self, portfolios: dict, row_count: int = None):
        import datetime as _dt
        self._loading = False
        if portfolios is None:
            # Defensive: should never happen since _mf_build_portfolios always
            # returns a dict, but guard against it rather than crashing the
            # Tkinter callback if it ever does (e.g. a stale/partial build).
            rc = f' (fetched {row_count} rows)' if row_count is not None else ''
            self._on_error(f'Portfolio build returned no data (internal error){rc}.')
            return
        self.portfolios = portfolios
        now = _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._status_var.set(f'✓ Synced  ({len(portfolios)} portfolios)  —  {now}')
        self._rebuild_list(); self._refresh_detail()

    def _on_error(self, msg: str):
        self._loading = False; self._status_var.set('✗ Load failed')
        from tkinter import messagebox as _mb
        _mb.showerror('NEWPOR Load Error', f'Could not load portfolio data:\n\n{msg}')

    # ── Sidebar list ──────────────────────────────────────────────────────────
    def _rebuild_list(self):
        self.portfolio_list.delete(0, 'end')
        visible = self._portfolios_for_currency(self._cur_ccy)
        codes   = sorted(visible.keys())
        for code in codes:
            self.portfolio_list.insert('end', f'{code}  [{visible[code]["platform"]}]')
        active = self.active_code.get()
        if active not in visible and codes:
            active = codes[0]; self.active_code.set(active)
        for idx, code in enumerate(codes):
            if code == active:
                self.portfolio_list.selection_set(idx)
                self.portfolio_list.activate(idx); break

    def _on_select(self, _e=None):
        sel = self.portfolio_list.curselection()
        if not sel: return
        code = self.portfolio_list.get(sel[0]).split(' ', 1)[0]
        self.active_code.set(code); self._refresh_detail()

    def _on_period_change(self):
        self._redraw_active_tab()

    # ── Master refresh ────────────────────────────────────────────────────────
    def _refresh_detail(self):
        code = self.active_code.get()
        if not code or not self.portfolios or code not in self.portfolios: return
        port = self.portfolios[code]
        ccy0 = port['base_currency']
        n_h  = sum(1 for h in port['holdings'].values() if not h.get('_is_cash'))
        invested = sum(h['cost_value'] for h in port['holdings'].values()
                        if not h.get('_is_cash'))
        rpl  = port.get('_realised_pl', 0.0)
        self._info_var.set(
            f"Portfolio: {code}  [{port['platform']}]  │  {ccy0}  "
            f"│  Holdings: {n_h}  │  Invested: {ccy0} {invested:,.2f}  "
            f"│  Real P/L: {ccy0} {rpl:+,.2f}"
        )
        self._refresh_summary(port)
        self._redraw_active_tab()

    def _redraw_active_tab(self):
        code = self.active_code.get()
        if not code or not self.portfolios or code not in self.portfolios: return
        port = self.portfolios[code]
        tab  = self.nb.tab(self.nb.select(), 'text').strip()
        if   'Line'         in tab: self._draw_line_chart(port)
        elif 'Holdings Bar' in tab: self._draw_bar_chart(port)
        elif 'Data Table'   in tab: self._draw_table(port)
        elif 'Total'        in tab: self._draw_total()
        elif 'Composition'  in tab: self._draw_comp(port)

    # ── Summary treeview ──────────────────────────────────────────────────────
    def _refresh_summary(self, port: dict):
        for row in self.summary_tree.get_children(): self.summary_tree.delete(row)
        for ticker, item in sorted(port['holdings'].items()):
            if item.get('_is_cash'):
                continue   # cash position trimmed from SCA views
            units  = item['shares']
            costv  = item['cost_value']
            price  = _mf_get_price(ticker, item['market'])
            mv     = item['shares'] * price if price else None
            pl_pct = ((mv - costv) / costv * 100) if mv and costv else None
            tag    = 'gain' if pl_pct is not None and pl_pct >= 0 else 'loss'
            self.summary_tree.insert('', 'end', tags=(tag,),
                values=(ticker, _fmt_units(units), f'{costv:,.2f}',
                        f'{mv:,.2f}' if mv else 'N/A',
                        f'{pl_pct:+.2f}%' if pl_pct is not None else 'N/A'))

    # ── Tab 1: Line Chart ─────────────────────────────────────────────────────
    def _draw_line_chart(self, port: dict):
        if not hasattr(self, '_fig_line') or not self._fig_line: return
        import datetime as _dt
        from collections import defaultdict
        ax = self._ax_line; ax.clear(); self._style_ax(ax)
        ccy0 = port['base_currency']

        all_txns = []
        for ticker, item in port['holdings'].items():
            if item.get('_is_cash'): continue
            for txn in item.get('transactions', []):
                try:
                    d = _dt.datetime.strptime(txn[1], '%m/%d/%Y').date()
                    all_txns.append((d, ticker, txn[0], txn[2], txn[3], txn[4]))
                except ValueError:
                    pass
        all_txns.sort(key=lambda x: x[0])

        if not all_txns:
            ax.text(0.5, 0.5, 'No transaction history.', ha='center', va='center',
                    color='#888', fontsize=12, transform=ax.transAxes)
            self._canvas_line.draw(); return

        earliest_date = all_txns[0][0]
        today = _dt.date.today()

        # ── Replay transactions day-by-day across the FULL calendar range,
        #    tracking units held and cost basis per ticker on every single
        #    day (not just transaction dates) — this is what lets us apply
        #    each day's ACTUAL historical closing price below.
        full_calendar = pd.date_range(earliest_date, today, freq='D')
        units_by_ticker: dict = defaultdict(lambda: pd.Series(0.0, index=full_calendar))
        cost_s:   dict = defaultdict(float)
        shares_s: dict = defaultdict(float)
        running_cost = 0.0
        snap_cost = []   # total cost basis, tracked per calendar day
        txn_idx = 0
        all_tickers = set(t[1] for t in all_txns)

        daily_units_snapshot: dict = {t: [] for t in all_tickers}
        for day in full_calendar:
            day_date = day.date()
            while txn_idx < len(all_txns) and all_txns[txn_idx][0] <= day_date:
                _, ticker, ttype, unit, price, amount = all_txns[txn_idx]
                if ttype == 'buy':
                    old_s = shares_s[ticker]; new_s = old_s + unit
                    shares_s[ticker] = new_s; cost_s[ticker] += amount
                    running_cost += amount
                elif ttype == 'sell':
                    sold = abs(unit)
                    if shares_s[ticker] > 0:
                        cb = (cost_s[ticker] / shares_s[ticker]) * sold
                        cost_s[ticker] -= cb; running_cost -= cb
                    shares_s[ticker] -= sold
                    if shares_s[ticker] <= 1e-8:
                        shares_s[ticker] = 0.0; cost_s[ticker] = 0.0
                txn_idx += 1
            for t in all_tickers:
                daily_units_snapshot[t].append(shares_s[t])
            snap_cost.append(running_cost)

        # ── Apply each ticker's ACTUAL historical closing price on each day.
        #    yfinance only returns prices for real trading days, so we
        #    reindex onto the full daily calendar and forward-fill (a stock's
        #    value over a weekend/holiday is its last real close, same
        #    convention the rest of the app already uses for index pricing).
        daily_mv_total = pd.Series(0.0, index=full_calendar)
        for ticker in all_tickers:
            mkt = 'MY' if ticker.endswith('.KL') else 'US'
            hist = _mf_get_price_history(ticker, mkt, start_date=earliest_date)
            units_series = pd.Series(daily_units_snapshot[ticker], index=full_calendar)
            if hist.empty:
                # No historical data available for this ticker — fall back to
                # today's live price applied flat across its held days, so it
                # still contributes to the curve rather than vanishing silently.
                live_p = _mf_get_price(ticker, mkt)
                if live_p:
                    daily_mv_total += units_series * live_p
                continue
            price_on_day = hist.reindex(full_calendar, method='ffill')
            price_on_day = price_on_day.bfill()   # cover days before the ticker's own history starts
            daily_mv_total += units_series * price_on_day.fillna(0.0)

        snap_dates_dt = list(full_calendar)
        snap_mv = daily_mv_total.tolist()

        # Period filter — applied to BOTH the cost line and the market-value line
        period = self._period_var.get()
        cutoff = {'1D': today-_dt.timedelta(days=1),
                  '5D': today-_dt.timedelta(days=5),
                  '1M': today-_dt.timedelta(days=30),
                  '3M': today-_dt.timedelta(days=91),
                  '6M': today-_dt.timedelta(days=182),
                  'YTD': _dt.date(today.year, 1, 1),
                  '1Y': today-_dt.timedelta(days=365)}.get(period)
        if cutoff:
            cutoff_ts = pd.Timestamp(cutoff)
            keep = [d >= cutoff_ts for d in full_calendar]
            snap_dates_dt = [d for d, k in zip(full_calendar, keep) if k]
            snap_cost      = [v for v, k in zip(snap_cost, keep) if k]
            snap_mv        = [v for v, k in zip(snap_mv, keep) if k]

        total_units_now = sum(shares_s.values())

        import matplotlib.dates as mdates
        if snap_dates_dt:
            ax.step(snap_dates_dt, snap_cost, where='post', color='#8B949E',
                    linewidth=1.6, label='Total Cost (TC)', linestyle='--', zorder=3)
            ax.fill_between(snap_dates_dt, snap_cost, alpha=0.05, color='#8B949E', step='post')

            # ── The actual day-by-day Current Value curve — units held each
            #    day x THAT DAY'S real closing price. This is the key fix:
            #    a genuine historical market-value line, not a single dot.
            mv_color = '#3FB950' if (snap_mv[-1] >= snap_cost[-1]) else '#F85149'
            ax.plot(snap_dates_dt, snap_mv, color=mv_color, linewidth=1.8,
                    label='Current Value (daily close x units held)', zorder=4)
            ax.fill_between(snap_dates_dt, snap_mv, snap_cost,
                            where=[mv >= c for mv, c in zip(snap_mv, snap_cost)],
                            color='#3FB950', alpha=0.08, interpolate=True, zorder=2)
            ax.fill_between(snap_dates_dt, snap_mv, snap_cost,
                            where=[mv < c for mv, c in zip(snap_mv, snap_cost)],
                            color='#F85149', alpha=0.08, interpolate=True, zorder=2)

        if snap_mv and snap_cost:
            today_dt   = snap_dates_dt[-1]
            current_value = snap_mv[-1]
            tc_today      = snap_cost[-1]
            pl_today  = current_value - tc_today
            pl_pct    = (pl_today / tc_today * 100) if tc_today else 0.0
            dot_color = '#3FB950' if pl_today >= 0 else '#F85149'
            ax.plot([today_dt], [current_value], 'o', color=dot_color, markersize=9, zorder=5)
            ax.annotate(
                f'TC: {ccy0} {tc_today:,.2f}\n'
                f'Current: {ccy0} {current_value:,.2f}\n'
                f'P/L: {pl_today:+,.2f} ({pl_pct:+.1f}%)\n'
                f'Units: {total_units_now:,.4f}',
                xy=(today_dt, current_value), xytext=(-150, 14),
                textcoords='offset points', color=dot_color, fontsize=8,
                ha='left', va='bottom',
                bbox=dict(boxstyle='round,pad=0.4', fc='#161b22', ec=dot_color, lw=1.2, alpha=0.92),
                arrowprops=dict(arrowstyle='->', color=dot_color, lw=1))

        ax.set_title(f'Portfolio History — {port["code"]}  [{ccy0}]  [{period}]  '
                     f'·  Total Cost vs Current', color='#cdd9e5', fontsize=11, pad=12)
        ax.set_ylabel(f'Value ({ccy0})', fontsize=9)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d %b'))
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
        self._fig_line.autofmt_xdate(rotation=30)
        ax.legend(facecolor='#161b22', edgecolor='#30363D', labelcolor='#cdd9e5', fontsize=8)
        self._fig_line.tight_layout()
        self._canvas_line.draw()

    # ── Tab 2: Holdings Bar Chart ─────────────────────────────────────────────
    def _draw_bar_chart(self, port: dict):
        if not hasattr(self, '_fig_bar') or not self._fig_bar: return
        try: import numpy as _np
        except ImportError: return
        ax = self._ax_bar; ax.clear(); self._style_ax(ax)
        ccy0 = port['base_currency']
        rows = []
        for ticker, item in port['holdings'].items():
            if item.get('_is_cash'): continue
            p = _mf_get_price(ticker, item['market'])
            if p is None: continue
            costv = item['cost_value']; mv = item['shares'] * p
            pl    = mv - costv; pl_pct = (pl / costv * 100) if costv else 0.0
            rows.append((ticker, costv, mv, pl, pl_pct))
        if not rows:
            ax.text(0.5, 0.5, 'No priced holdings.', ha='center', va='center',
                    color='#888', fontsize=12, transform=ax.transAxes)
            self._canvas_bar.draw(); return
        rows.sort(key=lambda r: r[2], reverse=True)
        labels = [r[0] for r in rows]; costs = [r[1] for r in rows]
        mvs = [r[2] for r in rows]; pls = [r[3] for r in rows]
        x = _np.arange(len(labels)); w = 0.38
        ax.bar(x-w/2, costs, w, label='Cost Value', color='#30363D',
               edgecolor='#0d1117', linewidth=0.5)
        ax.bar(x+w/2, mvs, w,
               color=['#3FB950' if mv >= c else '#F85149' for mv, c in zip(mvs, costs)],
               label='Market Value', edgecolor='#0d1117', linewidth=0.5)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=30, ha='right', fontsize=8, color='#cdd9e5')
        ax.axhline(0, color='#333', linewidth=0.5)
        ax.set_ylabel(f'Value ({ccy0})', fontsize=9)
        for xi, (c, mv, pl, plp) in enumerate(zip(costs, mvs, pls, [r[4] for r in rows])):
            col = '#3FB950' if pl >= 0 else '#F85149'
            ax.text(xi+w/2, mv*1.01, f'{plp:+.1f}%',
                    ha='center', va='bottom', fontsize=6, color=col)
        ax.set_title(f'Holdings — Cost vs Market Value  [{ccy0}]',
                     color='#cdd9e5', fontsize=11, pad=12)
        ax.legend(facecolor='#161b22', edgecolor='#30363D', labelcolor='#cdd9e5', fontsize=8)
        self._fig_bar.tight_layout(); self._canvas_bar.draw()

    # ── Tab 3: Data Table ─────────────────────────────────────────────────────
    def _draw_table(self, port: dict):
        for row in self.hold_tree.get_children(): self.hold_tree.delete(row)

        # Replay every transaction chronologically, per ticker, tracking a
        # running cumulative unit total and cost — mirrors the same logic
        # used to build the line chart's cost-history step series, just
        # surfaced here as a full row-by-row ledger instead of a plot.
        all_txns = []
        for ticker, item in port['holdings'].items():
            if item.get('_is_cash'):
                continue
            for txn in item.get('transactions', []):
                ttype, date_s, unit, price, amount = txn
                all_txns.append((date_s, ticker, ttype, unit, price, amount))

        # Sort chronologically (date string is MM/DD/YYYY from the ledger)
        import datetime as _dt
        def _parse_date(s):
            try: return _dt.datetime.strptime(s, '%m/%d/%Y')
            except ValueError: return _dt.datetime.min
        all_txns.sort(key=lambda t: _parse_date(t[0]))

        if not all_txns:
            return

        running_units: dict = {}   # ticker -> cumulative units
        running_cost:  dict = {}   # ticker -> cumulative cost
        for date_s, ticker, ttype, unit, price, amount in all_txns:
            ru = running_units.get(ticker, 0.0)
            rc = running_cost.get(ticker, 0.0)
            if ttype == 'buy':
                ru += unit
                rc += amount
            elif ttype == 'sell':
                sold = abs(unit)
                if ru > 0:
                    cb = (rc / ru) * sold
                    rc -= cb
                ru -= sold
                if ru <= 1e-8:
                    ru = 0.0; rc = 0.0
            running_units[ticker] = ru
            running_cost[ticker]  = rc

            tag = 'buy' if ttype == 'buy' else 'sell'
            self.hold_tree.insert('', 'end', tags=(tag,),
                values=(date_s, ticker, ttype.upper(),
                        f'{unit:+,.4f}', _mf_fmt_price(price),
                        _mf_fmt_signed(amount if ttype == 'buy' else -abs(amount)),
                        f'{ru:,.4f}', _mf_fmt_money(rc)))

    # ── Tab 4: Total P/L ──────────────────────────────────────────────────────
    def _set_total_ccy(self, opt: str):
        self._total_ccy_var.set(opt)
        for o, btn in self._tot_ccy_btns.items():
            active = (o == opt)
            btn.configure(bg='#00C9FF' if active else '#21262d',
                          fg='#0d1117' if active else '#cdd9e5')
        self._draw_total()

    def _draw_total(self):
        for row in self.total_tree.get_children(): self.total_tree.delete(row)
        if not self.portfolios: return
        mode = self._total_ccy_var.get()
        grand: dict = {}
        for code, port in sorted(self.portfolios.items()):
            ccy0 = port['base_currency']
            if mode != 'Combined' and mode != ccy0: continue
            self.total_tree.insert('', 'end', tags=('section',),
                values=(code, ccy0, '— PORTFOLIO —', '', '', '', '', ''))
            port_cost = 0.0; port_mv = 0.0
            for ticker, item in sorted(port['holdings'].items()):
                if item.get('_is_cash'):
                    continue   # cash position trimmed from SCA views
                costv    = item['cost_value']; units = item['shares']
                currency = item['currency']
                price  = _mf_get_price(ticker, item['market'])
                mv     = units * price if price else None
                pl     = mv - costv if mv is not None else None
                pl_pct = (pl / costv * 100) if pl is not None and costv else None
                tag    = 'gain' if pl is not None and pl >= 0 else 'loss'
                self.total_tree.insert('', 'end', tags=(tag,),
                    values=(ticker, currency, 'ETF/Stock',
                            _mf_fmt_money(costv), _mf_fmt_money(mv),
                            _mf_fmt_signed(pl) if pl is not None else 'N/A',
                            _mf_fmt_pct(pl_pct) if pl_pct is not None else 'N/A',
                            '—'))
                port_cost += costv
                if mv is not None: port_mv += mv
            rpl  = port.get('_realised_pl', 0.0)
            port_pl  = port_mv - port_cost
            port_pct = (port_pl / port_cost * 100) if port_cost else 0.0
            tag  = 'gain' if port_pl >= 0 else 'loss'
            self.total_tree.insert('', 'end', tags=(tag,),
                values=(f'  ↳ SUBTOTAL', ccy0, '',
                        _mf_fmt_money(port_cost), _mf_fmt_money(port_mv),
                        f'{port_pl:+,.2f}', f'{port_pct:+.2f}%', f'{rpl:+,.2f}'))
            if ccy0 not in grand:
                grand[ccy0] = {'cost':0,'mv':0,'rpl':0}
            grand[ccy0]['cost'] += port_cost; grand[ccy0]['mv']  += port_mv
            grand[ccy0]['rpl']  += rpl
        self.total_tree.insert('', 'end', tags=('section',),
            values=('', '', '', '', '', '', '', ''))
        for ccy, g in sorted(grand.items()):
            gpl  = g['mv'] - g['cost']
            gpct = (gpl / g['cost'] * 100) if g['cost'] else 0.0
            self.total_tree.insert('', 'end', tags=('grand',),
                values=(f'▶ GRAND TOTAL', ccy, '',
                        _mf_fmt_money(g['cost']), _mf_fmt_money(g['mv']),
                        f'{gpl:+,.2f}', f'{gpct:+.2f}%', f"{g['rpl']:+,.2f}"))

    # ── Tab 5: Composition Pie ────────────────────────────────────────────────
    def _draw_comp(self, port: dict):
        if not hasattr(self, '_fig_comp') or not self._fig_comp: return
        _PAL = ['#58A6FF','#3FB950','#BC8CFF','#D29922','#F0883E',
                '#F85149','#79C0FF','#56D364','#E3B341','#FF7B72',
                '#D2A8FF','#FFA657','#8B949E','#2EA043','#1F6FEB']
        ax = self._ax_comp; ax.clear(); ax.set_facecolor('#0d1117')
        ccy0 = port['base_currency']
        rows = []
        for ticker, item in port['holdings'].items():
            if item.get('_is_cash'):
                continue   # cash position trimmed from SCA views
            p  = _mf_get_price(ticker, item['market'])
            mv = item['shares'] * p if p else item['cost_value']
            rows.append((ticker, mv, False))
        rows = [(t, v, c) for t, v, c in rows if v > 0]
        if not rows:
            ax.text(0.5, 0.5, 'No data.', ha='center', va='center',
                    color='#888', fontsize=12, transform=ax.transAxes)
            self._canvas_comp.draw(); return
        labels = [r[0] for r in rows]; sizes = [r[1] for r in rows]
        colors = ['#D29922' if r[2] else _PAL[i % len(_PAL)] for i, r in enumerate(rows)]
        wedges, _, autotexts = ax.pie(
            sizes, colors=colors, autopct='%1.1f%%', startangle=140,
            pctdistance=0.80, wedgeprops=dict(linewidth=1.5, edgecolor='#0d1117'))
        for at in autotexts:
            at.set_color('#0d1117'); at.set_fontsize(8); at.set_fontweight('bold')
        ax.legend(wedges, [f'{l}  {ccy0} {v:,.2f}' for l, v in zip(labels, sizes)],
                  loc='center left', bbox_to_anchor=(1.0, 0.5), fontsize=8,
                  facecolor='#161b22', edgecolor='#30363D', labelcolor='#cdd9e5')
        ax.annotate(f'Total: {ccy0} {sum(sizes):,.2f}',
                    xy=(0.5, -0.06), xycoords='axes fraction',
                    ha='center', fontsize=9, color='#58A6FF')
        ax.set_title(f'Portfolio Composition — {port["code"]}',
                     color='#cdd9e5', fontsize=11, pad=12)
        self._fig_comp.tight_layout(); self._canvas_comp.draw()


# =============================================================================
# LOTTOSTOCK — Bursa Malaysia Stock Lottery  (formerly stock_lottery.py)
# Linked to MYFOLIO: owned-stock tuning & "Road to 100" head-start units are
# pulled LIVE from the SWAY1-MYR portfolio (built from the NEWPOR transaction
# ledger). Confirmed lottery buys feed back in as new in-memory Buy rows on
# that same ledger, so MYFOLIO's holdings view reflects lottery activity too.
# =============================================================================

LOTTO_STOCK_LIST = [
    ("1015", "AMBANK"), ("1023", "CIMB"), ("1082", "HLFG"), ("1155", "MAYBANK"),
    ("1066", "RHBBANK"), ("4197", "SIMEDARBY"), ("4065", "PPB"), ("3816", "MISC"),
    ("3182", "GENTING"), ("2445", "KLK"), ("1961", "IOI"), ("1818", "BURSA"),
    ("1295", "PBBANK"), ("5151", "HEXTARGLB"), ("4707", "NESTLEMY"), ("4715", "GENTINGMY"),
    ("4677", "YTL"), ("4863", "TM"), ("6012", "MAXIS"), ("6033", "PETRONASGAS"),
    ("6947", "CELCOMDIGI"), ("6742", "YTLPWR"), ("7084", "QL"), ("5211", "SUNWAY"),
    ("5183", "PETRONASCHEM"), ("5225", "IHH"), ("5347", "TENAGA"), ("5819", "HLBANK"),
    ("5681", "PETRONASDAG"), ("8869", "PRESSMETAL"), ("5285", "SDGUT"), ("5296", "MRDIY"),
    ("5326", "99SMART"),
]

LOTTO_RETAIL     = {"7084", "5326", "5296"}
LOTTO_HEALTHCARE = {"5225"}

# ── Stashaway ETF Explorer — supported ETF universe ──────────────────────────
# These trade in USD via Up-CGS-CIMB / StashAway with a $1.99 order fee.
# Minimum recommended starting amount: RM50 / ~USD12 per buy.
LOTTO_ETF_LIST = [
    ("SMH",   "VanEck Semiconductor"),
    ("IVV",   "iShares S&P 500"),
    ("GLDM",  "SPDR Gold MiniShares"),
    ("QQQ",   "Invesco QQQ (Nasdaq-100)"),
    ("SIVR",  "Aberdeen Silver ETF"),
    # SPDR Select Series (all except XLRE)
    ("XLB",   "SPDR Materials"),
    ("XLC",   "SPDR Communication"),
    ("XLE",   "SPDR Energy"),
    ("XLF",   "SPDR Financials"),
    ("XLI",   "SPDR Industrials"),
    ("XLK",   "SPDR Technology"),
    ("XLP",   "SPDR Consumer Staples"),
    ("XLU",   "SPDR Utilities"),
    ("XLV",   "SPDR Health Care"),
    ("XLY",   "SPDR Consumer Discret"),
    # Other supported ETFs
    ("VYM",   "Vanguard High Dividend"),
    ("FBTC",  "Fidelity Bitcoin"),
    ("GRID",  "iShares Clean Energy Infra"),
    ("VEU",   "Vanguard FTSE All-World ex-US"),
    ("COPX",  "Global X Copper Miners"),
    ("PPA",   "Invesco Aerospace & Defense"),
    ("DXJ",   "WisdomTree Japan Hedged"),
    ("FLTW",  "Franklin FTSE Taiwan"),
    ("CIBR",  "First Trust Cybersecurity"),
    ("ARKX",  "ARK Space Exploration"),
    ("SPEM",  "SPDR Emerging Markets"),
    ("PPH",   "VanEck Pharma"),
    ("ICLN",  "iShares Global Clean Energy"),
    ("FETH",  "Fidelity Ethereum"),
    ("BOTZ",  "Global X Robotics & AI"),
    ("AAXJ",  "iShares Asia ex-Japan"),
    ("VGK",   "Vanguard FTSE Europe"),
    ("BBJP",  "JPMorgan BetaBuilders Japan"),
    ("CWB",   "SPDR Convertible Securities"),
    ("IGV",   "iShares Expanded Tech Software"),
    ("RSP",   "Invesco S&P 500 Equal Weight"),
    ("VNQ",   "Vanguard Real Estate"),
    ("FIW",   "First Trust Water"),
    ("FLIN",  "Franklin FTSE India"),
    ("IJR",   "iShares S&P Small-Cap 600"),
    ("ESGE",  "iShares MSCI EM ESG"),
    ("ESPO",  "VanEck Video Gaming & eSports"),
    ("VNM",   "VanEck Vietnam"),
    ("IBB",   "iShares Biotech"),
    ("DIA",   "SPDR Dow Jones Industrial"),
    ("ARKK",  "ARK Innovation"),
    ("VNQI",  "Vanguard Intl Real Estate"),
    ("IXJ",   "iShares Global Healthcare"),
    ("EIDO",  "iShares MSCI Indonesia"),
    ("EVX",   "VanEck Environmental Svcs"),
    ("ARKG",  "ARK Genomic Revolution"),
    ("ESGU",  "iShares MSCI USA ESG"),
    ("EWC",   "iShares MSCI Canada"),
    ("SKYY",  "First Trust Cloud Computing"),
    ("FDN",   "First Trust Dow Jones Internet"),
    ("IHI",   "iShares US Medical Devices"),
    ("EWA",   "iShares MSCI Australia"),
    ("BNDX",  "Vanguard Total Intl Bond"),
    ("FINX",  "Global X FinTech"),
    ("SHY",   "iShares 1-3Y Treasury"),
    ("SCHR",  "Schwab 5-10Y Treasury"),
    ("EMB",   "iShares JP Morgan EM Bond"),
    ("IGOV",  "iShares Intl Treasury"),
    ("LQD",   "iShares iBoxx IG Corp Bond"),
    ("TLH",   "iShares 10-20Y Treasury"),
    ("WIP",   "SPDR TIPS Intl"),
    ("BGRN",  "iShares USD Green Bond"),
    ("INDY",  "iShares MSCI India"),
]

LOTTO_ETF_FEE_USD   = 1.99   # per-order fee charged by StashAway/Up-CGS-CIMB
LOTTO_ETF_MIN_USD   = 12.00  # recommended minimum buy (~RM50)
LOTTO_ETF_MIN_PRICE = 1.99   # reject orders where price < fee (auto-reject)

# Road-To targets per mode
LOTTO_ROAD_BURSA = [100, 200, 500, 1000]   # units (whole Bursa shares)
LOTTO_ROAD_ETF   = [1, 2, 5, 10, 20]       # units (fractional ETF units)

# Theme — matches the rest of the merged app
LOTTO_BG     = "#0a0c10"
LOTTO_PANEL  = "#13151c"
LOTTO_GOLD   = "#f0c040"
LOTTO_GREEN  = "#40f090"
LOTTO_TEAL   = "#40f0b0"
LOTTO_PURPLE = "#c080ff"
LOTTO_GREY   = "#888888"
LOTTO_DGREY  = "#444444"
LOTTO_RED    = "#ff5050"
LOTTO_FG     = "#ffffff"


# ──────────────────────────────────────────────────────────────────
# MYFOLIO LINKAGE — live owned-stock / unit data from SWAY1-MYR
# ──────────────────────────────────────────────────────────────────

def lotto_derive_owned_from_portfolios(ports: dict):
    """
    Derive owned/units for BOTH modes from the portfolios dict.
    Returns (bursa_owned, bursa_units, etf_owned, etf_units).

    Bursa: from SWAY1-MYR — filtered to LOTTO_STOCK_LIST 33 codes.
    ETF:   from SWAY1     — filtered to LOTTO_ETF_LIST codes.
    """
    if not ports:
        return set(), {}, set(), {}

    # ── Bursa side (MYR, .KL tickers) ────────────────────────────────────────
    bursa_owned = set()
    bursa_units = {}
    myr_port = ports.get("SWAY1-MYR")
    if myr_port:
        lotto_codes = {code for code, _ in LOTTO_STOCK_LIST}
        for ticker, item in myr_port["holdings"].items():
            if item.get("_is_cash"):
                continue
            code = ticker[:-3] if ticker.upper().endswith(".KL") else ticker
            if code not in lotto_codes:
                continue
            if item["shares"] > 0:
                bursa_owned.add(code)
                bursa_units[code] = item["shares"]

    # ── ETF side (USD, no suffix) ─────────────────────────────────────────────
    etf_owned = set()
    etf_units = {}
    usd_port = ports.get("SWAY1")
    if usd_port:
        etf_codes = {code for code, _ in LOTTO_ETF_LIST}
        for ticker, item in usd_port["holdings"].items():
            if item.get("_is_cash"):
                continue
            code = ticker.upper().strip()
            if code not in etf_codes:
                continue
            if item["shares"] > 0:
                etf_owned.add(code)
                etf_units[code] = item["shares"]

    return bursa_owned, bursa_units, etf_owned, etf_units


def lotto_pull_sca_state(log_cb=None):
    """
    Fetch the live MYR portfolio (SWAY1-MYR) from MYFOLIO's own data path
    and derive (owned_set, starting_units) for the lottery, matched by bare
    4-digit code (MYFOLIO stores tickers as 'CODE.KL').

    Returns (owned: set[str], units: dict[str,float], portfolio_dict_or_None,
             validation: dict). `validation` is the result of _mf_validate_ledger
    — LottoStock is locked unless validation["ok"] is True.
    """
    try:
        rows = _mf_load_csv(log_cb=log_cb)
    except Exception as e:
        validation = {"ok": False, "errors": [f"Could not reach Google Sheet: {e}"],
                      "warnings": [], "row_count": 0, "checked_at": None}
        return set(), {}, None, validation

    validation = _mf_validate_ledger(rows)
    if not validation["ok"]:
        return set(), {}, None, validation

    try:
        ports = _mf_build_portfolios(rows)
    except Exception as e:
        validation["ok"] = False
        validation["errors"].append(f"Sheet passed validation but failed to process: {e}")
        return set(), {}, None, validation

    if ports is None:
        # Defensive: _mf_build_portfolios always returns a dict by design,
        # but guard against None here too rather than crashing the worker
        # thread if it ever happens.
        validation["ok"] = False
        validation["errors"].append("Portfolio build returned no data (internal error).")
        return set(), {}, None, validation

    bursa_owned, bursa_units, etf_owned, etf_units = lotto_derive_owned_from_portfolios(ports)
    return bursa_owned, bursa_units, etf_owned, etf_units, ports, validation


def lotto_record_etf_buy_to_portfolios(portfolios: dict, code: str,
                                        units: float, price_usd: float,
                                        date_str: str):
    """
    Append a confirmed ETF lottery buy as a new in-memory Buy entry onto
    the SWAY1 (USD) portfolio, mirroring lotto_record_buy_to_myfolio for Bursa.
    ticker stored as bare code (e.g. 'VNQ') matching SWAY1 key convention.
    """
    if not portfolios or "SWAY1" not in portfolios:
        return
    port     = portfolios["SWAY1"]
    ticker   = code.upper()
    amount   = round(units * price_usd, 4)
    holdings = port["holdings"]

    if ticker in holdings and not holdings[ticker].get("_is_cash"):
        h = holdings[ticker]
        old_s = h["shares"]; new_s = old_s + units
        if new_s > 0:
            h["avg_cost"] = round(
                (old_s * h["avg_cost"] + units * price_usd) / new_s, 6)
        h["shares"]     = new_s
        h["cost_value"] = round(h["shares"] * h["avg_cost"], 4)
    else:
        holdings[ticker] = {
            "shares":     units,
            "avg_cost":   price_usd,
            "cost_value": amount,
            "market":     "US",
            "_is_cash":   False,
        }

    port["total_cost"] = sum(
        h["cost_value"] for h in holdings.values() if not h.get("_is_cash"))


def lotto_record_buy_to_myfolio(portfolios: dict, code: str, units: float,
                                 price: float, date_str: str):
    """
    Append a confirmed lottery buy as a new in-memory Buy entry directly onto
    the SWAY1-MYR portfolio dict (same shape _mf_build_portfolios produces),
    so MYFOLIO's holdings/total/history views reflect it immediately without
    needing a refresh from the live Google Sheet.

    This mutates `portfolios` in place. ticker stored as 'CODE.KL' to match
    MYFOLIO's existing key convention.
    """
    if not portfolios or "SWAY1-MYR" not in portfolios:
        return
    port    = portfolios["SWAY1-MYR"]
    ticker  = f"{code}.KL"
    amount  = round(units * price, 4)
    holdings = port["holdings"]

    if ticker in holdings and not holdings[ticker].get("_is_cash"):
        h = holdings[ticker]
        old_s = h["shares"]; new_s = old_s + units
        if new_s > 0:
            h["avg_cost"] = ((old_s * h["avg_cost"]) + (units * price)) / new_s
        h["shares"]      = new_s
        h["cost_value"] += amount
        h["date"]         = date_str
        h["transactions"].append(("buy", date_str, units, price, amount))
    else:
        holdings[ticker] = {
            "market": "MY", "currency": "MYR",
            "shares": units, "avg_cost": price,
            "cost_value": amount, "realised_pl": 0.0,
            "date": date_str, "transactions": [("buy", date_str, units, price, amount)],
        }


# ──────────────────────────────────────────────────────────────────
# SCORING HELPERS
# ──────────────────────────────────────────────────────────────────

def lotto_rand_pickmax_for(code: str, owned: set, units_map: dict) -> int:
    """Owned stocks (from the hardcoded 33 only) get a BONUS PickMax ceiling
    — higher than the base 99 — so they appear more often in draws, reflecting
    the extra conviction of already holding them.

    Bonus formula: base 99 + up to 99 extra based on units held.
      0 units on record  → base 99 (no bonus, just as if unowned)
      1 unit held        → 99 + 50  = 149
      5 units held       → 99 + 99  = 198  (capped at 198)
      10+ units held     → capped at 198

    Unowned stocks stay at randint(1, 99) — no change.
    Only codes that are in LOTTO_STOCK_LIST are ever tagged as owned,
    so IPO stocks in MYFOLIO never accidentally affect this calculation.
    """
    if code not in owned:
        return random.randint(1, 99)
    u = units_map.get(code, 0)
    if u <= 0:
        return random.randint(1, 99)
    # Bonus: scale from +1 to +99 based on units held, capped at 99 bonus
    bonus = min(99, int(u * 10))
    cap   = 99 + bonus   # range: 100 to 198
    return random.randint(99, cap)   # always at least 99 (same as unowned max)


def lotto_get_extra_buy(code: str, owned: set) -> tuple:
    """Returns (total_extra, breakdown_list) for the buy-extra-units rule.
    The +0.01 owned bonus only applies to the hardcoded 33 stocks — IPO
    holdings in MYFOLIO are filtered out of `owned` before this is called."""
    total = 0.0
    breakdown = []
    if code in owned:
        total += 0.01
        breakdown.append(("Owned", 0.01))
    if code in LOTTO_RETAIL:
        total += 0.02
        breakdown.append(("Retail", 0.02))
    if code in LOTTO_HEALTHCARE:
        total += 0.02
        breakdown.append(("Healthcare", 0.02))
    return total, breakdown


def lotto_fetch_live_price(code: str, is_etf: bool = False):
    """Fetch live price via yfinance.
    Bursa: appends .KL  (e.g. 1818 → 1818.KL, returns RM price)
    ETF:   bare symbol  (e.g. VNQ → VNQ,       returns USD price)
    """
    symbol = code if is_etf else f"{code}.KL"
    try:
        t    = yf.Ticker(symbol)
        fast = t.fast_info
        price = (getattr(fast, 'last_price', None) or
                 getattr(fast, 'lastPrice', None))
        if price is None:
            hist = t.history(period="2d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
        return float(price) if price else None
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────
# LOTTOSTOCK WINDOW (Toplevel, launched from MYIPOApp)
# ──────────────────────────────────────────────────────────────────

class LottoStockWindow(tk.Toplevel):
    def __init__(self, parent, preloaded_rows=None, preloaded_portfolios=None,
                 preloaded_validation=None):
        super().__init__(parent)
        self.title("LottoStock — Bursa Malaysia & StashAway ETF Explorer")
        self.geometry("1040x920")
        self.configure(bg=LOTTO_BG)
        self.minsize(860, 720)

        # ── Mode: 'bursa' or 'etf' ──
        self._mode = tk.StringVar(value='bursa')

        # ── MYFOLIO-linked state ──
        self.owned          = set()    # Bursa 33 — codes owned in SWAY1-MYR
        self.starting_units = {}       # Bursa — {code: units} from SWAY1-MYR
        self.etf_owned      = set()    # ETF — codes owned in SWAY1 (USD)
        self.etf_units      = {}       # ETF — {code: units} from SWAY1
        self.sca_portfolios = None   # raw dict from _mf_build_portfolios
        self._sca_loading = False
        self._validation = None          # last _mf_validate_ledger result
        self._locked = True              # locked until validation passes

        # ── Lottery state ──
        self.stocks = []
        self.spinning = False
        self.top_n = tk.StringVar(value="1 — Auto Pick")
        self.top_stocks = []

        self.next_bonus = {}
        self.tx_history = []
        self._pending_buy = None          # winner awaiting inline confirm/skip
        self._road_switch_pending = None  # road-to-100 target switch awaiting inline confirm

        # Road to 100
        self.road_target = None
        self.road_log = []
        self.road_price = None
        self.road_price_loading = False
        self._road_buy_armed = False      # inline two-step confirm state

        self._build_lock_screen()
        self._build_ui()
        self._show_lock_screen()   # hide the game UI until validation passes

        if preloaded_validation is not None:
            # Instant open — data + validation already done at SplashScreen
            # startup, same pattern as MYIPO's own preload. No network wait,
            # no re-validation needed.
            b_own, b_u, e_own, e_u = lotto_derive_owned_from_portfolios(preloaded_portfolios)
            self._on_sca_synced(b_own, b_u, e_own, e_u,
                                    preloaded_portfolios, preloaded_validation, initial=True)
        else:
            self._refresh_sca_link(initial=True)

    # ──────────────────────────────────────────────────────────
    # UI CONSTRUCTION
    # ──────────────────────────────────────────────────────────

    # ──────────────────────────────────────────────────────────
    # APPLOCK — data-validation lock screen
    # LottoStock refuses to run (no draws, no buys) until the NEWPOR
    # Google Sheet passes both schema and row-level validation.
    # ──────────────────────────────────────────────────────────

    def _build_lock_screen(self):
        self._lock_frame = tk.Frame(self, bg=LOTTO_BG)

        center = tk.Frame(self._lock_frame, bg=LOTTO_BG)
        center.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(center, text="🔒", font=("Segoe UI", 48), bg=LOTTO_BG, fg=LOTTO_RED).pack()
        tk.Label(center, text="LOTTOSTOCK LOCKED", font=("Impact", 22),
                 fg=LOTTO_RED, bg=LOTTO_BG).pack(pady=(6, 2))
        tk.Label(center, text="Database validation failed — fix the NEWPOR sheet and resync.",
                 font=("Courier New", 10), fg=LOTTO_GREY, bg=LOTTO_BG).pack(pady=(0, 16))

        self._lock_detail = tk.Frame(center, bg=LOTTO_PANEL, highlightbackground=LOTTO_RED,
                                      highlightthickness=1, padx=20, pady=16)
        self._lock_detail.pack(fill="x")
        self._lock_text_var = tk.StringVar(value="Checking database…")
        tk.Label(self._lock_detail, textvariable=self._lock_text_var, font=("Courier New", 9),
                 fg="#ffaaaa", bg=LOTTO_PANEL, justify="left", anchor="w",
                 wraplength=560).pack(fill="x")

        tk.Button(center, text="↻ Re-check Database", font=("Courier New", 10, "bold"),
                  bg=LOTTO_GOLD, fg="#000", relief="flat", padx=16, pady=8, cursor="hand2",
                  command=lambda: self._refresh_sca_link(initial=False)
                  ).pack(pady=(16, 0))

    def _show_lock_screen(self):
        self._locked = True
        self._outer.pack_forget()
        self._lock_frame.pack(fill="both", expand=True)

    def _hide_lock_screen(self):
        self._locked = False
        self._lock_frame.pack_forget()
        self._outer.pack(fill="both", expand=True)

    def _update_lock_text(self, validation: dict):
        if validation is None:
            self._lock_text_var.set("No response from database check.")
            return
        lines = []
        if validation.get("errors"):
            lines.append("ERRORS:")
            lines.extend(f"  • {e}" for e in validation["errors"])
        if validation.get("warnings"):
            lines.append("")
            lines.append("WARNINGS:")
            lines.extend(f"  • {w}" for w in validation["warnings"][:5])
        self._lock_text_var.set("\n".join(lines) if lines else "Unknown validation failure.")

    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Lotto.TCombobox", fieldbackground=LOTTO_PANEL, background=LOTTO_PANEL,
                         foreground=LOTTO_GOLD, arrowcolor=LOTTO_GOLD)

        self._outer = tk.Frame(self, bg=LOTTO_BG)
        self._outer.pack(fill="both", expand=True)
        outer = self._outer
        canvas = tk.Canvas(outer, bg=LOTTO_BG, highlightthickness=0)
        vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self.body = tk.Frame(canvas, bg=LOTTO_BG)
        canvas.create_window((0, 0), window=self.body, anchor="nw", width=1000)
        self.body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        # ── Header ──
        hdr = tk.Frame(self.body, bg=LOTTO_BG)
        hdr.pack(fill="x", pady=(18, 4))
        tk.Label(hdr, text="LOTTOSTOCK", font=("Impact", 36),
                 fg=LOTTO_GOLD, bg=LOTTO_BG).pack()
        tk.Label(hdr, text="PickMax Randomizer · Highest Score Wins", font=("Courier New", 9),
                 fg=LOTTO_GREY, bg=LOTTO_BG).pack()

        # ── Mode switcher ──────────────────────────────────────────────────────
        mode_row = tk.Frame(self.body, bg=LOTTO_BG)
        mode_row.pack(pady=(10, 4))
        tk.Label(mode_row, text="Exchange:", font=("Courier New", 9),
                 fg=LOTTO_GREY, bg=LOTTO_BG).pack(side="left", padx=(0, 8))
        for label, val, col in [
            ("🏦  Bursa Malaysia (Up-CGS-CIMB)", "bursa", LOTTO_GOLD),
            ("🌐  StashAway ETF Explorer (USD)", "etf",   "#58A6FF"),
        ]:
            tk.Radiobutton(mode_row, text=label, variable=self._mode, value=val,
                           command=self._on_mode_change,
                           font=("Courier New", 9, "bold"), bg=LOTTO_BG, fg=col,
                           selectcolor=LOTTO_BG, activebackground=LOTTO_BG,
                           activeforeground=col, relief="flat",
                           cursor="hand2").pack(side="left", padx=8)

        # ── ETF info banner (shown only in ETF mode) ───────────────────────────
        self._etf_banner = tk.Frame(self.body, bg="#0a1a2a",
                                    highlightbackground="#58A6FF", highlightthickness=1,
                                    padx=14, pady=8)
        tk.Label(self._etf_banner,
                 text=f"⚠  StashAway ETF Explorer — via Up-CGS-CIMB  |  "
                      f"Order fee: ${LOTTO_ETF_FEE_USD:.2f}/trade  |  "
                      f"Min. recommended: RM50 / ~${LOTTO_ETF_MIN_USD:.0f} USD per buy  |  "
                      f"Orders with price < ${LOTTO_ETF_MIN_PRICE:.2f} are auto-rejected",
                 font=("Courier New", 8), fg="#58A6FF", bg="#0a1a2a",
                 wraplength=900, justify="left").pack(anchor="w")

        # ── Road-To selector ───────────────────────────────────────────────────
        self._road_sel_row = tk.Frame(self.body, bg=LOTTO_BG)
        self._road_sel_row.pack(pady=(6, 0))
        road_sel_row = self._road_sel_row
        tk.Label(road_sel_row, text="🛣️ Road To:", font=("Courier New", 9, "bold"),
                 fg=LOTTO_GOLD, bg=LOTTO_BG).pack(side="left", padx=(0, 8))
        self._road_target_var = tk.IntVar(value=100)
        self._road_btns = {}
        for n in LOTTO_ROAD_BURSA:
            b = tk.Radiobutton(road_sel_row, text=f"{n} units",
                               variable=self._road_target_var, value=n,
                               command=self._on_road_target_change,
                               font=("Courier New", 8, "bold"),
                               bg=LOTTO_BG, fg=LOTTO_GOLD, selectcolor=LOTTO_BG,
                               activebackground=LOTTO_BG, activeforeground=LOTTO_GOLD,
                               relief="flat", cursor="hand2", indicatoron=False,
                               padx=10, pady=4)
            b.pack(side="left", padx=2)
            self._road_btns[n] = b

        # ── MYFOLIO link status ──
        link_row = tk.Frame(self.body, bg=LOTTO_BG)
        link_row.pack(pady=(10, 0))
        self.sca_link_var = tk.StringVar(value="🔗 SCA: connecting…")
        tk.Label(link_row, textvariable=self.sca_link_var, font=("Courier New", 9),
                 fg="#58A6FF", bg=LOTTO_BG).pack(side="left", padx=6)
        tk.Button(link_row, text="↻ Resync SCA", font=("Courier New", 8),
                  bg=LOTTO_PANEL, fg="#58A6FF", relief="flat", bd=1, cursor="hand2",
                  command=lambda: self._refresh_sca_link(initial=False)).pack(side="left", padx=6)

        # ── Legend ──
        legend = tk.Frame(self.body, bg=LOTTO_BG)
        legend.pack(pady=(12, 16))
        for text, color in [("● Owned (SCA) → +0.01", LOTTO_GREEN), ("● Retail → +0.02", "#40c0f0"),
                             ("● Healthcare → +0.02", "#f040a0")]:
            tk.Label(legend, text=text, font=("Courier New", 9), fg=color, bg=LOTTO_BG).pack(side="left", padx=10)

        # ── Controls ──
        ctrl = tk.Frame(self.body, bg=LOTTO_BG)
        ctrl.pack(pady=(0, 12))
        self.draw_btn = tk.Button(ctrl, text="🎲 DRAW LOTTERY", font=("Impact", 14),
                                   bg=LOTTO_GOLD, fg="#000", activebackground="#c08000",
                                   relief="flat", padx=30, pady=10, cursor="hand2",
                                   command=self.handle_draw)
        self.draw_btn.pack(side="left", padx=10)

        dropdown_frame = tk.Frame(ctrl, bg=LOTTO_BG)
        dropdown_frame.pack(side="left", padx=10)
        tk.Label(dropdown_frame, text="SHOW TOP", font=("Courier New", 8), fg=LOTTO_DGREY, bg=LOTTO_BG).pack()
        self.top_dropdown = ttk.Combobox(dropdown_frame, textvariable=self.top_n, state="readonly",
                                          values=["1 — Auto Pick", "3", "5", "10"], width=14,
                                          font=("Courier New", 10), style="Lotto.TCombobox")
        self.top_dropdown.pack()
        self.top_dropdown.bind("<<ComboboxSelected>>", lambda e: self._reset_top_display())

        # ── Next draw bonus banner ──
        self.bonus_banner = tk.Label(self.body, text="", font=("Courier New", 9),
                                      fg=LOTTO_PURPLE, bg=LOTTO_BG, wraplength=880, justify="left")
        self.bonus_banner.pack(pady=(0, 8))

        # ── Road to 100 ──
        self.road_section = tk.Frame(self.body, bg=LOTTO_BG)
        self.road_section.pack(fill="x", padx=20, pady=(4, 16))
        self._build_road_section()

        # ── Top picks ──
        self.top_section = tk.Frame(self.body, bg=LOTTO_BG)
        self.top_section.pack(fill="x", padx=20, pady=(0, 16))

        # ── Transaction history ──
        self.tx_section = tk.Frame(self.body, bg=LOTTO_BG)
        self.tx_section.pack(fill="x", padx=20, pady=(0, 16))

        # ── Full table ──
        table_label_frame = tk.Frame(self.body, bg=LOTTO_BG)
        table_label_frame.pack(fill="x", padx=20)
        self.table_title = tk.Label(table_label_frame, text="STOCK POOL", font=("Courier New", 10, "bold"),
                                     fg=LOTTO_GOLD, bg=LOTTO_BG, anchor="w")
        self.table_title.pack(fill="x", pady=(0, 6))

        table_frame = tk.Frame(self.body, bg=LOTTO_BG)
        table_frame.pack(fill="both", expand=True, padx=20, pady=(0, 30))

        cols = ("rank", "code", "name", "pickmax", "extra", "score")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=16,
                                  style="Lotto.Treeview")
        headers = {"rank": "Rank", "code": "Code", "name": "Name",
                   "pickmax": "PickMax", "extra": "Buy Extra", "score": "Score"}
        widths = {"rank": 50, "code": 70, "name": 140, "pickmax": 80, "extra": 220, "score": 100}
        for c in cols:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="center" if c != "name" and c != "extra" else "w")

        style.configure("Lotto.Treeview", background=LOTTO_PANEL, fieldbackground=LOTTO_PANEL,
                         foreground=LOTTO_FG, rowheight=26, font=("Courier New", 9))
        style.configure("Lotto.Treeview.Heading", background="#000", foreground=LOTTO_DGREY,
                         font=("Courier New", 8, "bold"))
        style.map("Lotto.Treeview", background=[("selected", "#222")])

        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("owned", foreground=LOTTO_GREEN)

        self._reset_pool()

    # ──────────────────────────────────────────────────────────
    # MYFOLIO LINK
    # ──────────────────────────────────────────────────────────

    def _refresh_sca_link(self, initial=False):
        if self._sca_loading:
            return
        self._sca_loading = True
        self.sca_link_var.set("🔗 MYFOLIO: syncing…")
        if hasattr(self, '_lock_text_var'):
            self._lock_text_var.set("Checking database…")

        def _log(msg):
            self.after(0, lambda: (
                self.sca_link_var.set(f"🔗 MYFOLIO: {msg}"),
                self._lock_text_var.set(msg) if hasattr(self, '_lock_text_var') else None,
            ))

        def worker():
            b_own, b_u, e_own, e_u, ports, validation = lotto_pull_sca_state(log_cb=_log)
            self.after(0, lambda: self._on_sca_synced(b_own, b_u, e_own, e_u, ports, validation, initial))

        threading.Thread(target=worker, daemon=True).start()

    def _on_sca_synced(self, bursa_owned, bursa_units, etf_owned, etf_units,
                              ports, validation, initial):
        self._sca_loading = False
        self._validation = validation

        if not validation or not validation.get("ok"):
            self._update_lock_text(validation)
            self._show_lock_screen()
            self.sca_link_var.set("🔗 SCA: ❌ validation failed — locked")
            return

        # Apply synced data for both modes
        self.owned              = bursa_owned
        self.starting_units     = bursa_units
        self.etf_owned          = etf_owned
        self.etf_units          = etf_units
        self.sca_portfolios = ports
        self._hide_lock_screen()

        n_bursa = len(bursa_owned)
        n_etf   = len(etf_owned)
        link_txt = (f"🔗 SCA: linked — "
                    f"{n_bursa} Bursa stock{'s' if n_bursa!=1 else ''} · "
                    f"{n_etf} ETF{'s' if n_etf!=1 else ''} owned")
        self.sca_link_var.set(link_txt)
        if validation.get("warnings"):
            self.sca_link_var.set(link_txt + f"  ⚠ {len(validation['warnings'])} warning(s)")

        if not initial or not self.stocks:
            # Initial open: self.stocks is empty — must populate from LOTTO_STOCK_LIST.
            # Re-sync: reset pool to refresh pickmax values with the new owned set.
            # (Previously initial=True always skipped this, leaving self.stocks=[]
            # so _refresh_table had nothing to render and owned highlighting never showed.)
            self._reset_pool()
        else:
            # Re-sync mid-session with existing draw state: just update pickmax per
            # stock to reflect the new owned set, preserving scores/ranks already drawn.
            for s in self.stocks:
                s["pickmax"] = lotto_rand_pickmax_for(s["code"], self.owned, self.starting_units)
            self._refresh_table()
        self._build_road_section()

    # ──────────────────────────────────────────────────────────
    # ROAD TO 100 SECTION
    # ──────────────────────────────────────────────────────────

    def _build_road_section(self):
        for w in self.road_section.winfo_children():
            w.destroy()

        goal    = self._current_road_goal() if hasattr(self, '_road_target_var') else 100
        mode    = self._mode.get() if hasattr(self, '_mode') else 'bursa'
        is_etf  = (mode == 'etf')
        ccy_pfx = '$' if is_etf else 'RM'
        goal_lbl = f'{goal} unit{"s" if goal != 1 else ""}'

        # ── Inline target-switch prompt ───────────────────────────────────────
        if self._road_switch_pending:
            switch_card = tk.Frame(self.road_section, bg="#1f1a0f",
                                   highlightbackground=LOTTO_GOLD, highlightthickness=1,
                                   padx=16, pady=12)
            switch_card.pack(fill="x", pady=(0, 10))
            cur_name = self.road_target["name"] if self.road_target else "—"
            new_code = self._road_switch_pending["code"]
            new_name = self._road_switch_pending["name"]
            tk.Label(switch_card,
                     text=f"🔀 New draw winner differs from your Road to {goal_lbl} target",
                     font=("Courier New", 9, "bold"), fg=LOTTO_GOLD,
                     bg="#1f1a0f", anchor="w").pack(fill="x")
            tk.Label(switch_card,
                     text=f"You haven't bought into {cur_name} yet. Switch target to "
                          f"{new_code} · {new_name}?",
                     font=("Courier New", 8), fg="#ccc", bg="#1f1a0f",
                     wraplength=880, justify="left", anchor="w").pack(fill="x", pady=(2, 8))
            sw_btn_row = tk.Frame(switch_card, bg="#1f1a0f")
            sw_btn_row.pack(fill="x")
            tk.Button(sw_btn_row, text=f"✅ Switch to {new_code}",
                      font=("Courier New", 9, "bold"), bg=LOTTO_GOLD, fg="#000",
                      relief="flat", padx=10, pady=6, cursor="hand2",
                      command=self._accept_road_switch).pack(side="left", expand=True, fill="x", padx=(0, 4))
            tk.Button(sw_btn_row, text=f"✖ Keep {cur_name}", font=("Courier New", 9),
                      bg="#1a1a1a", fg=LOTTO_GREY, relief="flat", padx=10, pady=6, cursor="hand2",
                      command=self._decline_road_switch).pack(side="left", expand=True, fill="x", padx=(4, 0))

        if not self.road_target:
            return

        locked     = len(self.road_log) > 0
        title_text = f"🛣️ ROAD TO {goal_lbl.upper()} " + ("🔒" if locked else "🟡 PARTIAL MATCH")
        tk.Label(self.road_section, text=title_text, font=("Courier New", 10, "bold"),
                 fg=LOTTO_GOLD, bg=LOTTO_BG, anchor="w").pack(fill="x", pady=(0, 6))

        border_color = LOTTO_TEAL if locked else LOTTO_GOLD
        card = tk.Frame(self.road_section, bg=LOTTO_PANEL, highlightbackground=border_color,
                         highlightthickness=1, padx=20, pady=16)
        card.pack(fill="x")

        top_row = tk.Frame(card, bg=LOTTO_PANEL)
        top_row.pack(fill="x")

        left = tk.Frame(top_row, bg=LOTTO_PANEL)
        left.pack(side="left")
        tk.Label(left, text=self.road_target["code"], font=("Courier New", 24, "bold"),
                 fg=LOTTO_GOLD, bg=LOTTO_PANEL).pack(anchor="w")
        tk.Label(left, text=self.road_target["name"], font=("Courier New", 10),
                 fg=LOTTO_GREY, bg=LOTTO_PANEL).pack(anchor="w")

        right = tk.Frame(top_row, bg=LOTTO_PANEL)
        right.pack(side="right")
        if self.road_price_loading:
            tk.Label(right, text="fetching price…", font=("Courier New", 9, "italic"),
                     fg=LOTTO_GREY, bg=LOTTO_PANEL).pack(anchor="e")
        elif self.road_price is not None:
            tk.Label(right, text="LIVE PRICE", font=("Courier New", 8),
                     fg=LOTTO_DGREY, bg=LOTTO_PANEL).pack(anchor="e")
            price_str = f"${self.road_price:.4f}" if is_etf else f"RM{self.road_price:.3f}"
            tk.Label(right, text=price_str, font=("Courier New", 16, "bold"),
                     fg=LOTTO_TEAL, bg=LOTTO_PANEL).pack(anchor="e")
        else:
            tk.Label(right, text="price unavailable", font=("Courier New", 9, "italic"),
                     fg=LOTTO_GREY, bg=LOTTO_PANEL).pack(anchor="e")
        tk.Button(right, text="↻ refresh", font=("Courier New", 8), bg=LOTTO_PANEL, fg=LOTTO_GREY,
                  relief="flat", bd=1, cursor="hand2",
                  command=self.refresh_road_price).pack(anchor="e", pady=(4, 0))

        # Progress
        head_start  = self.starting_units.get(self.road_target["code"], 0)
        total_units = head_start + sum(l["units"] for l in self.road_log)
        total_saved = sum(l.get("amountMYR", l.get("amountUSD", 0)) for l in self.road_log)

        prog_frame = tk.Frame(card, bg=LOTTO_PANEL)
        prog_frame.pack(fill="x", pady=(14, 4))
        lbl_row = tk.Frame(prog_frame, bg=LOTTO_PANEL)
        lbl_row.pack(fill="x")
        units_fmt = f"{total_units:.4f}" if is_etf else f"{total_units:.4f}"
        tk.Label(lbl_row, text=f"{units_fmt} / {goal_lbl}", font=("Courier New", 10),
                 fg="#ccc", bg=LOTTO_PANEL).pack(side="left")
        saved_str = f"{ccy_pfx}{total_saved:.2f} saved"
        if is_etf and total_saved > 0:
            saved_str += f"  (incl. ${LOTTO_ETF_FEE_USD:.2f}/trade fee)"
        tk.Label(lbl_row, text=saved_str, font=("Courier New", 10),
                 fg="#ccc", bg=LOTTO_PANEL).pack(side="right")

        if head_start > 0:
            tk.Label(prog_frame,
                     text=f"🎁 Head start: {head_start:.4f} units (live from SCA)",
                     font=("Courier New", 8), fg=LOTTO_GOLD,
                     bg=LOTTO_PANEL, anchor="w").pack(fill="x", pady=(4, 0))

        bar_bg = tk.Frame(prog_frame, bg="#1a1a1a", height=10)
        bar_bg.pack(fill="x", pady=(6, 0))
        pct = min(100, total_units / goal * 100) if goal else 0
        bar_fg = tk.Frame(bar_bg, bg=LOTTO_GREEN, height=10,
                          width=int(8.6 * pct))
        bar_fg.place(x=0, y=0, relheight=1)

        if is_etf:
            note = (f"🔒 ETF target locked — ${LOTTO_ETF_FEE_USD:.2f} fee/trade · min ${LOTTO_ETF_MIN_USD:.0f} recommended."
                    if locked else
                    f"🟡 ETF partial match — ${LOTTO_ETF_FEE_USD:.2f} order fee applies · orders below ${LOTTO_ETF_MIN_PRICE:.2f} are rejected.")
        else:
            note = ("🔒 Target locked. Cannot change until a new transaction record is needed."
                    if locked else
                    "🟡 Partial match — not bought yet. Each draw may offer a new candidate "
                    "until you save toward it to lock it in.")
        tk.Label(card, text=note, font=("Courier New", 8), fg=LOTTO_GREY, bg=LOTTO_PANEL,
                 wraplength=880, justify="left", anchor="w").pack(fill="x", pady=(10, 12))

        if is_etf:
            btn_label = f"💰 Buy (min ${LOTTO_ETF_MIN_USD:.0f} USD)"
        else:
            btn_label = "💰 Buy 1.5 Units" if (self.road_price and self.road_price > 10) else "💰 Save RM10"
        if not locked:
            btn_label += " & Lock Target"

        if self._road_buy_armed:
            # ── Inline confirmation step ──────────────────────────────────────
            price = self.road_price
            if is_etf:
                # ETF mode — USD price + $1.99 fee gate
                if price is None:
                    confirm_msg = "Price still loading…"
                    units = amount = 0
                elif price < LOTTO_ETF_MIN_PRICE:
                    confirm_msg = (f"❌ Auto-rejected: price ${price:.4f} < ${LOTTO_ETF_MIN_PRICE:.2f} minimum. "
                                   f"The ${LOTTO_ETF_FEE_USD:.2f} order fee would exceed the trade value.")
                    units = amount = 0
                else:
                    amount = LOTTO_ETF_MIN_USD   # default ~$12 USD
                    units  = round((amount - LOTTO_ETF_FEE_USD) / price, 4)
                    confirm_msg = (f"Buy ${amount:.2f} USD of {self.road_target['code']} "
                                   f"≈ {units:.4f} units @ ${price:.4f} "
                                   f"(incl. ${LOTTO_ETF_FEE_USD:.2f} order fee)")
            else:
                if price and price > 10:
                    units, amount = 1.5, round(1.5 * price, 2)
                    confirm_msg = f"Price RM{price:.3f} > RM10 — capped at 1.5 units (≈RM{amount:.2f})"
                elif price:
                    units, amount = round(10 / price, 4), 10.0
                    confirm_msg = f"Confirm RM10 contribution (≈+{units:.4f} units @ RM{price:.3f})"
                else:
                    confirm_msg = "Price still loading…"
                    units = amount = 0

            confirm_box = tk.Frame(card, bg="#0f1f18", highlightbackground=LOTTO_TEAL,
                                   highlightthickness=1, padx=12, pady=10)
            confirm_box.pack(fill="x")
            reject_mode = is_etf and price is not None and price < LOTTO_ETF_MIN_PRICE
            tk.Label(confirm_box, text=confirm_msg, font=("Courier New", 9),
                     fg=LOTTO_RED if reject_mode else "#ccc",
                     bg="#0f1f18", wraplength=880, justify="left").pack(fill="x", pady=(0, 8))
            cbtn_row = tk.Frame(confirm_box, bg="#0f1f18")
            cbtn_row.pack(fill="x")
            if not reject_mode:
                tk.Button(cbtn_row, text="✅ Confirm Contribution",
                          font=("Courier New", 9, "bold"),
                          bg=LOTTO_GREEN, fg="#000", relief="flat", padx=10, pady=6,
                          cursor="hand2",
                          command=self.open_road_confirm).pack(side="left", expand=True,
                                                               fill="x", padx=(0, 4))
            tk.Button(cbtn_row,
                      text="✖ Reject (price too low)" if reject_mode else "✖ Cancel",
                      font=("Courier New", 9), bg="#1a1a1a", fg=LOTTO_GREY,
                      relief="flat", padx=10, pady=6, cursor="hand2",
                      command=self._cancel_road_buy).pack(side="left", expand=True,
                                                          fill="x", padx=(4, 0))
        else:
            tk.Button(card, text=btn_label, font=("Courier New", 11, "bold"),
                      bg=LOTTO_GREEN, fg="#000", relief="flat", padx=16, pady=8,
                      cursor="hand2", command=self._arm_road_buy).pack(fill="x")

        # Road log
        if self.road_log:
            log_frame = tk.Frame(self.road_section, bg=LOTTO_BG)
            log_frame.pack(fill="x", pady=(8, 0))
            for entry in self.road_log[:6]:
                row = tk.Frame(log_frame, bg="#1a1422",
                               highlightbackground=LOTTO_PURPLE,
                               highlightthickness=1, padx=10, pady=4)
                row.pack(fill="x", pady=2)
                tag = " (capped)" if entry.get("capped") else ""
                amt_key = "amountUSD" if is_etf else "amountMYR"
                amt_val = entry.get(amt_key, entry.get("amountMYR", 0))
                px_key  = "priceAtBuyUSD" if is_etf else "priceAtBuy"
                px_val  = entry.get(px_key, entry.get("priceAtBuy", 0))
                tk.Label(row, text=f"{ccy_pfx}{amt_val:.2f}",
                         font=("Courier New", 9, "bold"),
                         fg=LOTTO_GOLD, bg="#1a1422").pack(side="left")
                tk.Label(row,
                         text=f"+{entry['units']:.4f} units @ {ccy_pfx}{px_val:.4f}{tag}",
                         font=("Courier New", 9), fg="#ccc",
                         bg="#1a1422").pack(side="left", padx=12)
                tk.Label(row, text=entry["date"], font=("Courier New", 8),
                         fg=LOTTO_DGREY, bg="#1a1422").pack(side="right")

    def refresh_road_price(self):
        if not self.road_target:
            return
        self.road_price_loading = True
        self._build_road_section()
        is_etf = (self._mode.get() == 'etf') if hasattr(self, '_mode') else False

        def worker():
            price = lotto_fetch_live_price(self.road_target["code"], is_etf=is_etf)
            self.road_price = price
            self.road_price_loading = False
            self.after(0, self._build_road_section)

        threading.Thread(target=worker, daemon=True).start()

    def set_candidate_target(self, code, name):
        """Set the Road-To target. Only accepts codes valid for the current mode."""
        mode = self._mode.get() if hasattr(self, '_mode') else 'bursa'
        if mode == 'etf':
            valid = {c for c, _ in LOTTO_ETF_LIST}
        else:
            valid = {c for c, _ in LOTTO_STOCK_LIST}
        if code not in valid:
            return   # silently ignore cross-mode candidate
        self.road_target  = {"code": code, "name": name}
        self.road_price   = None
        self.road_price_loading = False
        self.refresh_road_price()

    def _arm_road_buy(self):
        """Step 1 — show the inline confirmation box instead of a popup."""
        if self._locked:
            return
        if not self.road_target or self.road_price is None:
            return
        self._road_buy_armed = True
        self._build_road_section()

    def _cancel_road_buy(self):
        self._road_buy_armed = False
        self._build_road_section()

    def open_road_confirm(self):
        """Step 2 — executes the contribution after the inline confirm click."""
        if self._locked:
            return
        if not self.road_target:
            return
        price = self.road_price
        if price is None:
            return

        mode   = self._mode.get() if hasattr(self, '_mode') else 'bursa'
        is_etf = (mode == 'etf')
        date_str = datetime.now().strftime("%m/%d/%Y")

        if is_etf:
            # ETF: $12 USD buy, $1.99 fee deducted, price gate enforced
            if price < LOTTO_ETF_MIN_PRICE:
                self._road_buy_armed = False
                self._build_road_section()
                return
            amount = LOTTO_ETF_MIN_USD
            units  = round((amount - LOTTO_ETF_FEE_USD) / price, 4)
            self.road_log.insert(0, {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "amountUSD": amount,
                "units": units,
                "priceAtBuyUSD": price,
                "capped": False,
            })
            lotto_record_etf_buy_to_portfolios(
                self.sca_portfolios, self.road_target["code"], units, price, date_str)
            self.etf_units[self.road_target["code"]] = \
                self.etf_units.get(self.road_target["code"], 0) + units
        else:
            if price > 10:
                units = 1.5
                amount = round(1.5 * price, 2)
            else:
                units = round(10 / price, 4)
                amount = 10.0
            self.road_log.insert(0, {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "amountMYR": amount,
                "units": units,
                "priceAtBuy": price,
                "capped": price > 10,
            })
            lotto_record_buy_to_myfolio(
                self.sca_portfolios, self.road_target["code"], units, price, date_str)
            self.starting_units[self.road_target["code"]] = \
                self.starting_units.get(self.road_target["code"], 0) + units

        self._road_buy_armed = False
        self._build_road_section()

    # ──────────────────────────────────────────────────────────
    # LOTTERY LOGIC
    # ──────────────────────────────────────────────────────────

    def _on_mode_change(self):
        """Switch between Bursa Malaysia and StashAway ETF mode."""
        mode = self._mode.get()
        if mode == 'etf':
            self._etf_banner.pack(fill='x', padx=20, pady=(0, 4))
        else:
            self._etf_banner.pack_forget()
        # Rebuild Road-To buttons
        for w in self._road_sel_row.winfo_children():
            if isinstance(w, tk.Radiobutton):
                w.destroy()
        self._road_btns.clear()
        targets = LOTTO_ROAD_ETF if mode == 'etf' else LOTTO_ROAD_BURSA
        for n in targets:
            lbl = f'{n} unit{"s" if n != 1 else ""}'
            b = tk.Radiobutton(self._road_sel_row, text=lbl,
                               variable=self._road_target_var, value=n,
                               command=self._on_road_target_change,
                               font=("Courier New", 8, "bold"),
                               bg=LOTTO_BG, fg=LOTTO_GOLD, selectcolor=LOTTO_BG,
                               activebackground=LOTTO_BG, activeforeground=LOTTO_GOLD,
                               relief="flat", cursor="hand2", indicatoron=False,
                               padx=10, pady=4)
            b.pack(side="left", padx=2)
            self._road_btns[n] = b
        self._road_target_var.set(targets[0])
        # Clear ALL draw state — mode switch starts completely fresh
        self.road_target          = None
        self.road_log             = []
        self.road_price           = None
        self.road_price_loading   = False
        self._road_buy_armed      = False
        self._road_switch_pending = None
        self.top_stocks           = []      # clear previous draw results
        self._pending_buy         = None    # clear buy confirmation
        self.spinning             = False
        self._reset_pool()
        self._refresh_top_section()   # clears old draw result (top_stocks=[])
        self._build_road_section()

    def _on_road_target_change(self):
        self.road_target = None
        self.road_log    = []
        self._build_road_section()

    def _current_road_goal(self) -> int:
        return self._road_target_var.get()

    def _reset_pool(self):
        """Populate self.stocks from the active mode's list."""
        mode = self._mode.get() if hasattr(self, '_mode') else 'bursa'
        if mode == 'etf':
            # ETF mode: owned bonus from SWAY1 USD portfolio
            etf_owned = getattr(self, 'etf_owned', set())
            etf_units = getattr(self, 'etf_units', {})
            self.stocks = [
                {"code": c, "name": n,
                 "pickmax": lotto_rand_pickmax_for(c, etf_owned, etf_units),
                 "score": None, "rank": None}
                for c, n in LOTTO_ETF_LIST
            ]
        else:
            self.stocks = [
                {"code": c, "name": n,
                 "pickmax": lotto_rand_pickmax_for(c, self.owned, self.starting_units),
                 "score": None, "rank": None}
                for c, n in LOTTO_STOCK_LIST
            ]
        self._refresh_table()

    def _selected_top_n(self):
        v = self.top_n.get()
        return 1 if "Auto" in v else int(v)

    def handle_draw(self):
        if self._locked:
            messagebox.showwarning("LottoStock Locked",
                                    "Database validation has not passed. Fix the NEWPOR sheet and resync first.")
            return
        if self.spinning:
            return
        self.spinning = True
        self.draw_btn.config(text="⟳ DRAWING...", state="disabled")
        threading.Thread(target=self._spin_animation, daemon=True).start()

    def _spin_animation(self):
        # Mode-aware: draw from the active list with the matching owned/units
        mode = self._mode.get() if hasattr(self, '_mode') else 'bursa'
        if mode == 'etf':
            draw_list    = LOTTO_ETF_LIST
            active_owned = getattr(self, 'etf_owned', set())
            active_units = getattr(self, 'etf_units', {})
        else:
            draw_list    = LOTTO_STOCK_LIST
            active_owned = self.owned
            active_units = self.starting_units

        ticks = 18
        for i in range(ticks):
            for s in self.stocks:
                s["pickmax"] = lotto_rand_pickmax_for(s["code"], active_owned, active_units)
                s["score"] = round(random.random() * s["pickmax"], 2)
            self.after(0, self._refresh_table)
            time_module.sleep(0.07)

        final = []
        for code, name in draw_list:
            pm    = lotto_rand_pickmax_for(code, active_owned, active_units)
            base  = round(random.random() * pm, 4)
            bonus = self.next_bonus.get(code, 0)
            final.append({"code": code, "name": name, "pickmax": pm,
                           "score": round(base + bonus, 4), "rank": None})

        sorted_final = sorted(final, key=lambda s: -s["score"])
        for i, s in enumerate(sorted_final):
            s["rank"] = i + 1
        self.stocks = sorted_final

        n = self._selected_top_n()
        self.top_stocks = sorted_final[:n]

        self.after(0, self._on_draw_complete)

    def _on_draw_complete(self):
        self.spinning = False
        self.draw_btn.config(text="🎲 DRAW LOTTERY", state="normal")

        winner = self.top_stocks[0] if self.top_stocks else None

        # Road to 100: candidate logic (unlocked until first real tx).
        # No popup — if the winner differs from the current target, show an
        # inline switch-target banner instead of an askyesno dialog.
        self._road_switch_pending = None
        locked = len(self.road_log) > 0
        if not locked and winner:
            if self.road_target is None:
                self.set_candidate_target(winner["code"], winner["name"])
            elif winner["code"] != self.road_target["code"]:
                self._road_switch_pending = {"code": winner["code"], "name": winner["name"]}

        # Pending buy-confirmation state lives on the card itself now —
        # no popup. Top-1 auto mode arms the pending-confirm flag on the
        # winner so the card renders its inline confirm/skip buttons.
        self._pending_buy = winner if (self._selected_top_n() == 1 and winner) else None

        self._refresh_table()
        self._refresh_top_section()
        self._refresh_tx_section()
        self._refresh_bonus_banner()
        self._build_road_section()
        self.table_title.config(text="ALL STOCKS")

    def _reset_top_display(self):
        self.top_stocks = []
        self._pending_buy = None
        self._refresh_top_section()

    # ──────────────────────────────────────────────────────────
    # INLINE TRANSACTION CONFIRMATION (no popups — integrated in window)
    # ──────────────────────────────────────────────────────────

    def _confirm_transaction(self, stock):
        code = stock["code"]
        self.next_bonus[code] = self.next_bonus.get(code, 0) + 0.01
        mode   = self._mode.get() if hasattr(self, '_mode') else 'bursa'
        is_etf = (mode == 'etf')
        if is_etf:
            self.etf_owned.add(code)
        else:
            self.owned.add(code)
        self.tx_history.insert(0, {
            "code": code, "name": stock["name"],
            "time": datetime.now().strftime("%H:%M:%S"), "bonus": 0.01,
        })

        # Feed back into the correct portfolio — Bursa → SWAY1-MYR, ETF → SWAY1.
        price = lotto_fetch_live_price(code, is_etf=is_etf)
        date_str = datetime.now().strftime("%m/%d/%Y")
        if is_etf:
            # ETF: nominal buy = min recommended $12 minus $1.99 fee
            if price is None:
                price = 1.00
            units = round((LOTTO_ETF_MIN_USD - LOTTO_ETF_FEE_USD) / price, 4) \
                    if price >= LOTTO_ETF_MIN_PRICE else 0
            if units > 0:
                lotto_record_etf_buy_to_portfolios(
                    self.sca_portfolios, code, units, price, date_str)
                self.etf_units[code] = self.etf_units.get(code, 0) + units
        else:
            if price is None:
                price = 1.00
            units = 1.0
            lotto_record_buy_to_myfolio(
                self.sca_portfolios, code, units, price, date_str)
            self.starting_units[code] = self.starting_units.get(code, 0) + units

        if self._pending_buy and self._pending_buy["code"] == code:
            self._pending_buy = None
        self._refresh_top_section()
        self._refresh_tx_section()
        self._refresh_bonus_banner()

    def _skip_transaction(self, stock):
        """Dismiss the pending inline confirmation without recording a buy."""
        if self._pending_buy and self._pending_buy["code"] == stock["code"]:
            self._pending_buy = None
        self._refresh_top_section()

    def mark_buy(self, stock):
        """Arms the inline confirm/skip state on a multi-pick card."""
        self._pending_buy = stock
        self._refresh_top_section()

    def _accept_road_switch(self):
        if self._road_switch_pending:
            self.set_candidate_target(self._road_switch_pending["code"], self._road_switch_pending["name"])
        self._road_switch_pending = None
        self._build_road_section()

    def _decline_road_switch(self):
        self._road_switch_pending = None
        self._build_road_section()

    # ──────────────────────────────────────────────────────────
    # RENDER HELPERS
    # ──────────────────────────────────────────────────────────

    def _refresh_bonus_banner(self):
        if not self.next_bonus:
            self.bonus_banner.config(text="")
            return
        parts = []
        name_map = dict(LOTTO_STOCK_LIST)
        for code, amt in self.next_bonus.items():
            parts.append(f"{name_map.get(code, code)} +{amt:.2f}")
        self.bonus_banner.config(text="🔮 Next Draw Boosts: " + "   ".join(parts))

    def _refresh_top_section(self):
        for w in self.top_section.winfo_children():
            w.destroy()
        if not self.top_stocks:
            return

        n = len(self.top_stocks)
        tk.Label(self.top_section, text=f"🏆 TOP {n} PICK{'S' if n > 1 else ''}",
                 font=("Courier New", 10, "bold"), fg=LOTTO_GOLD, bg=LOTTO_BG, anchor="w").pack(fill="x", pady=(0, 6))

        grid = tk.Frame(self.top_section, bg=LOTTO_BG)
        grid.pack(fill="x")

        for idx, s in enumerate(self.top_stocks):
            total, breakdown = lotto_get_extra_buy(s["code"], self.owned)
            is_pending = self._pending_buy is not None and self._pending_buy["code"] == s["code"]
            card_border = LOTTO_TEAL if is_pending else (LOTTO_GOLD if s["rank"] == 1 else "#333")
            card = tk.Frame(grid, bg=LOTTO_PANEL, highlightbackground=card_border,
                             highlightthickness=2 if is_pending else 1, padx=14, pady=12)
            if n == 1:
                card.pack(fill="x")
            else:
                card.grid(row=idx // 4, column=idx % 4, padx=6, pady=6, sticky="nsew")
                grid.grid_columnconfigure(idx % 4, weight=1)

            tk.Label(card, text=f"#{s['rank']}", font=("Courier New", 8), fg=LOTTO_DGREY, bg=LOTTO_PANEL,
                     anchor="e").pack(fill="x")
            tk.Label(card, text=s["code"], font=("Courier New", 26 if n == 1 else 18, "bold"),
                     fg=LOTTO_GOLD, bg=LOTTO_PANEL).pack(anchor="center" if n == 1 else "w")
            tk.Label(card, text=s["name"], font=("Courier New", 11 if n == 1 else 9),
                     fg=LOTTO_FG if n == 1 else LOTTO_GREY, bg=LOTTO_PANEL).pack(anchor="center" if n == 1 else "w")
            tk.Label(card, text=f"PickMax: {s['pickmax']}", font=("Courier New", 8),
                     fg=LOTTO_DGREY, bg=LOTTO_PANEL).pack(anchor="center" if n == 1 else "w", pady=(2, 4))
            tk.Label(card, text=f"{s['score']:.4f}", font=("Courier New", 20 if n == 1 else 14, "bold"),
                     fg=LOTTO_GREEN, bg=LOTTO_PANEL).pack(anchor="center" if n == 1 else "w")

            if total > 0:
                bd_txt = "  ".join(f"+{amt:.2f} {label}" for label, amt in breakdown)
                tk.Label(card, text=f"Buy Extra: +{total:.2f}", font=("Courier New", 10, "bold"),
                         fg=LOTTO_TEAL, bg=LOTTO_PANEL).pack(anchor="center" if n == 1 else "w", pady=(8, 0))
                tk.Label(card, text=bd_txt, font=("Courier New", 8),
                         fg=LOTTO_GOLD, bg=LOTTO_PANEL).pack(anchor="center" if n == 1 else "w")

            if is_pending:
                # ── Inline confirmation panel — replaces the old popup ──────
                confirm_box = tk.Frame(card, bg="#0f1f18", highlightbackground=LOTTO_TEAL,
                                       highlightthickness=1, padx=10, pady=10)
                confirm_box.pack(fill="x", pady=(12, 0))
                extra_txt = ""
                if total > 0:
                    parts = ", ".join(f"+{amt:.2f} {label}" for label, amt in breakdown)
                    extra_txt = f"  ({parts})"
                tk.Label(confirm_box, text="Did you complete this purchase?",
                         font=("Courier New", 9, "bold"), fg=LOTTO_TEAL, bg="#0f1f18",
                         justify="center" if n == 1 else "left").pack(fill="x")
                tk.Label(confirm_box,
                         text=f"✅ Yes → +0.01 boost next draw, Buy recorded to MYFOLIO{extra_txt}",
                         font=("Courier New", 8), fg="#ccc", bg="#0f1f18", wraplength=560,
                         justify="center" if n == 1 else "left").pack(fill="x", pady=(2, 8))
                btn_row = tk.Frame(confirm_box, bg="#0f1f18")
                btn_row.pack(fill="x")
                tk.Button(btn_row, text="✅ Confirm Buy", font=("Courier New", 9, "bold"),
                          bg=LOTTO_GREEN, fg="#000", relief="flat", padx=10, pady=6, cursor="hand2",
                          command=lambda st=s: self._confirm_transaction(st)).pack(side="left", expand=True, fill="x", padx=(0, 4))
                tk.Button(btn_row, text="✖ Skip", font=("Courier New", 9), bg="#1a1a1a",
                          fg=LOTTO_GREY, relief="flat", padx=10, pady=6, cursor="hand2",
                          command=lambda st=s: self._skip_transaction(st)).pack(side="left", expand=True, fill="x", padx=(4, 0))
            elif n == 1:
                plan = f"📋 {s['name']} ({s['code']})"
                plan += f" — buy with +{total:.2f} extra units" if total > 0 else " — standard buy"
                tk.Label(card, text=plan, font=("Courier New", 9), fg="#ccc", bg=LOTTO_PANEL,
                         wraplength=600, justify="center").pack(pady=(14, 0))
            else:
                tk.Button(card, text="Mark as Bought", font=("Courier New", 8), bg="#1a1a1a",
                          fg=LOTTO_GOLD, relief="flat", cursor="hand2",
                          command=lambda st=s: self.mark_buy(st)).pack(fill="x", pady=(8, 0))

    def _refresh_tx_section(self):
        for w in self.tx_section.winfo_children():
            w.destroy()
        if not self.tx_history:
            return
        tk.Label(self.tx_section, text="📒 TRANSACTION HISTORY  (also recorded to MYFOLIO)",
                 font=("Courier New", 10, "bold"),
                 fg=LOTTO_GOLD, bg=LOTTO_BG, anchor="w").pack(fill="x", pady=(0, 6))
        for t in self.tx_history[:8]:
            row = tk.Frame(self.tx_section, bg="#1a1422", highlightbackground=LOTTO_PURPLE,
                            highlightthickness=1, padx=10, pady=4)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=t["code"], font=("Courier New", 9, "bold"), fg=LOTTO_GOLD, bg="#1a1422").pack(side="left")
            tk.Label(row, text=t["name"], font=("Courier New", 9), fg="#ccc", bg="#1a1422").pack(side="left", padx=10)
            tk.Label(row, text=f"+{t['bonus']:.2f} boost", font=("Courier New", 9), fg=LOTTO_PURPLE,
                     bg="#1a1422").pack(side="left")
            tk.Label(row, text=t["time"], font=("Courier New", 8), fg=LOTTO_DGREY, bg="#1a1422").pack(side="right")

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        mode         = self._mode.get() if hasattr(self, '_mode') else 'bursa'
        active_owned = getattr(self, 'etf_owned', set()) if mode == 'etf' else self.owned
        for s in self.stocks:
            total, breakdown = lotto_get_extra_buy(s["code"], active_owned)
            extra_txt = "+{:.2f} ({})".format(
                total, ", ".join(f"+{amt:.2f}{l[0]}" for l, amt in [(b[0], b[1]) for b in breakdown])
            ) if total > 0 else "—"
            score_txt = f"{s['score']:.4f}" if s["score"] is not None else "—"
            rank_txt  = f"#{s['rank']}" if s["rank"] else "—"
            tags      = ("owned",) if s["code"] in active_owned else ()
            self.tree.insert("", "end", values=(rank_txt, s["code"], s["name"],
                                                s["pickmax"], extra_txt, score_txt), tags=tags)

# =============================================================================
# SCA · NEWPOR TRACKER — Portfolio tracker window
# Mirrors the HTML SCA tracker UI, built as a native Python/Tkinter Toplevel.
# Reuses _mf_load_csv / _mf_build_portfolios / _mf_get_price / _mf_get_price_history
# — the same data stack that MYFOLIO already uses.
# =============================================================================
class SCAWindow(tk.Toplevel):
    # ── Colour scheme (matches the HTML tracker's dark palette) ──────────────
    BG       = '#0a0e1a'
    PANEL    = '#0d1424'
    BORDER   = '#1a2035'
    TEAL     = '#00b4d8'
    GREEN    = '#00e676'
    YELLOW   = '#ffd600'
    RED      = '#ff1744'
    DIM      = '#4a5568'
    FG       = '#e2e8f0'
    FG2      = '#a0aec0'
    GOLD     = '#ffd166'
    FONT     = ('Courier New', 9)
    FONT_B   = ('Courier New', 9, 'bold')
    FONT_H   = ('Courier New', 11, 'bold')

    def __init__(self, parent, preloaded_rows=None, preloaded_portfolios=None):
        super().__init__(parent)
        self.title('SCA · NEWPOR TRACKER')
        self.geometry('1380x760')
        self.configure(bg=self.BG)
        self.minsize(1100, 600)

        self._rows         = preloaded_rows
        self._portfolios   = preloaded_portfolios or {}
        self._loading      = False
        self._auto_after   = None
        self._auto_secs    = 60
        self._view_var     = tk.StringVar(value='BOTH')   # MY / US / BOTH
        self._tab_var      = tk.StringVar(value='ALL')
        self._status_var   = tk.StringVar(value='Ready')
        self._sheet_ok     = False
        self._fx_ok        = False
        self._usdmyr       = None
        self._fx_rates     = {'MYR': 1.0}   # ccy -> MYR rate; filled on price fetch
        self._tick_count   = 0
        self._last_price_data = {}
        self._last_hist_data  = {}

        self._build_ui()
        self._schedule_auto()

        # ── First-run gate ────────────────────────────────────────────────────
        # A K-ID with no saved source must set one up before anything loads,
        # otherwise they'd silently be looking at demo data and think it's
        # their own. Blocks until saved; closing the dialog closes SCA.
        if not sca_has_source(load_app_settings()):
            self.after(120, self._force_source_setup)
            return

        if preloaded_portfolios:
            # Hot path: data already preloaded at SplashScreen — instant render
            self._apply_portfolios(preloaded_portfolios)
            self._fetch_live_prices()
        else:
            # Cold open: try disk cache first for instant render,
            # then refresh from network in background
            self._load_from_cache_then_refresh()

    def _force_source_setup(self):
        """First SCA open for this K-ID — make them point at their own data."""
        who = CURRENT_KID or 'guest'
        messagebox.showinfo(
            'SCA — Welcome',
            f'Hi {who} — let\'s connect your portfolio.\n\n'
            f'SCA reads your transaction ledger from either:\n'
            f'  · your own Google Sheet, or\n'
            f'  · a CSV file on this device\n\n'
            f'Nothing is uploaded anywhere — your ledger stays yours.',
            parent=self)
        saved = self._open_source_dialog(first_run=True)
        if not saved:
            # They backed out — close SCA rather than show someone else's data
            self.destroy()

    def _load_from_cache_then_refresh(self):
        """Load this K-ID's cached ledger for instant display, then refresh."""
        def _worker():
            try:
                # local-only read — no network, uses this K-ID's cache
                rows  = _mf_load_csv(prefer_online=False)
                ports = _mf_build_portfolios(rows)
                if ports:
                    self._rows = rows
                    self.after(0, lambda: self._on_cache_loaded(ports))
            except Exception:
                pass
            # Kick off background refresh (live prices + online sheet check)
            self.after(0, lambda: self.after(50, self._refresh))

        import threading as _th
        _th.Thread(target=_worker, daemon=True).start()

    def _on_cache_loaded(self, ports):
        """Cache data arrived — render immediately before live prices load."""
        self._apply_portfolios(ports)
        self._set_indicator(sheet=True)
        self._fetch_live_prices()

    # ─────────────────────────────────────────────────────────────────────────
    # UI BUILD
    # ─────────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Header stays fixed at top (not scrolled)
        self._build_header()

        # Everything below goes into a scrollable canvas
        outer = tk.Frame(self, bg=self.BG)
        outer.pack(fill='both', expand=True)
        self._scroll_canvas = tk.Canvas(outer, bg=self.BG, highlightthickness=0)
        vbar = ttk.Scrollbar(outer, orient='vertical',
                             command=self._scroll_canvas.yview)
        self._scroll_canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side='right', fill='y')
        self._scroll_canvas.pack(side='left', fill='both', expand=True)

        self._scroll_body = tk.Frame(self._scroll_canvas, bg=self.BG)
        self._scroll_canvas.create_window((0, 0), window=self._scroll_body,
                                          anchor='nw')
        self._scroll_body.bind('<Configure>',
            lambda e: self._scroll_canvas.configure(
                scrollregion=self._scroll_canvas.bbox('all')))
        self._scroll_canvas.bind_all('<MouseWheel>',
            lambda e: self._scroll_canvas.yview_scroll(
                int(-e.delta / 120), 'units'))

        self._build_error_banner()
        self._build_summary_cards()
        self._build_tabs()
        self._build_holdings_table()
        self._build_portfolio_chart()
        self._build_weight_section()
        self._build_footer()

    def _scroll_frame(self):
        """All content widgets below the header should use _scroll_body as parent."""
        return self._scroll_body

    def _build_portfolio_chart(self):
        """Portfolio value chart — Total Value vs Net Deposits, like t17/t18.
        Shown below the holdings table, with period selectors and a hover
        tooltip showing total returns, total value, and net deposits on
        the nearest date to the mouse cursor."""
        panel = tk.Frame(self._scroll_body, bg=self.PANEL, highlightbackground=self.BORDER,
                         highlightthickness=1)
        panel.pack(fill='x', padx=14, pady=(6, 0))

        # Title + period selectors
        hdr = tk.Frame(panel, bg=self.PANEL)
        hdr.pack(fill='x', padx=12, pady=(8, 4))
        tk.Label(hdr, text='Portfolio value', font=('Segoe UI', 10, 'bold'),
                 fg=self.FG, bg=self.PANEL).pack(side='left')

        period_row = tk.Frame(panel, bg=self.PANEL)
        period_row.pack(fill='x', padx=12, pady=(0, 4))

        # Legend
        tk.Label(period_row, text='─○─ Total value', font=self.FONT,
                 fg='#4a90d9', bg=self.PANEL).pack(side='left', padx=(0, 12))
        tk.Label(period_row, text='─○─ Net deposits', font=self.FONT,
                 fg=self.DIM, bg=self.PANEL).pack(side='left', padx=(0, 20))

        # Period buttons
        self._pchart_period_var = tk.StringVar(value='Since 1st deposit')
        for label in ('1M', '3M', '6M', 'YTD', '1Y', 'Since 1st deposit'):
            active = label == 'Since 1st deposit'
            btn = tk.Radiobutton(
                period_row, text=label, variable=self._pchart_period_var,
                value=label, command=self._redraw_portfolio_chart,
                font=self.FONT_B, relief='flat', cursor='hand2',
                bg=self.TEAL if active else self.PANEL,
                fg='#000' if active else self.FG2,
                selectcolor=self.TEAL, activebackground=self.PANEL,
                activeforeground=self.TEAL, indicatoron=False, padx=8, pady=3)
            btn.pack(side='right', padx=2)

        # Matplotlib chart
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        self._pchart_fig  = Figure(figsize=(12, 2.6), facecolor=self.PANEL)
        self._pchart_ax   = self._pchart_fig.add_subplot(111)
        self._pchart_fig.subplots_adjust(left=0.06, right=0.98, top=0.92, bottom=0.12)
        self._pchart_canvas = FigureCanvasTkAgg(self._pchart_fig, panel)
        self._pchart_canvas.get_tk_widget().pack(fill='x', padx=6, pady=(0, 6))

        # Hover state for tooltip (t18 style)
        self._pchart_hover_artists = []
        self._pchart_data = {}   # filled by _redraw_portfolio_chart
        self._pchart_canvas.mpl_connect('motion_notify_event', self._on_pchart_hover)
        self._pchart_canvas.mpl_connect('figure_leave_event',
                                         lambda e: self._clear_pchart_hover())
        self._style_pchart_ax()

    def _style_pchart_ax(self):
        ax = self._pchart_ax
        ax.set_facecolor(self.PANEL)
        ax.tick_params(colors=self.DIM, labelsize=7)
        for sp in ax.spines.values():
            sp.set_color(self.BORDER)

    def _redraw_portfolio_chart(self):
        """Rebuild the Total Value vs Net Deposits lines for the chosen period."""
        import datetime as _dt
        ax = self._pchart_ax
        ax.cla(); self._style_pchart_ax()
        self._clear_pchart_hover()
        self._pchart_data = {}

        if not self._portfolios:
            self._pchart_canvas.draw(); return

        # Collect all transactions across both portfolios chronologically
        all_txns = []
        for port_key, port in self._portfolios.items():
            ccy = port.get('base_currency', 'USD')
            prefix = 'RM' if ccy == 'MYR' else '$'
            for ticker, item in port.get('holdings', {}).items():
                for txn in item.get('transactions', []):
                    ttype, date_s, unit, price, amount = txn
                    try:
                        d = _dt.datetime.strptime(date_s, '%m/%d/%Y').date()
                        all_txns.append((d, ttype, amount, ccy))
                    except ValueError:
                        pass
            # Also include deposit transactions from ledger if available
            for txn in port.get('_deposits', []):
                try:
                    d = _dt.datetime.strptime(txn[0], '%m/%d/%Y').date()
                    all_txns.append((d, 'deposit', txn[1], ccy))
                except Exception:
                    pass

        if not all_txns:
            ax.text(0.5, 0.5, 'No transaction history', ha='center', va='center',
                    color=self.DIM, fontsize=9, transform=ax.transAxes)
            self._pchart_canvas.draw(); return

        all_txns.sort(key=lambda t: t[0])
        earliest = all_txns[0][0]
        today = _dt.date.today()
        import pandas as pd

        # Period cutoff
        period = self._pchart_period_var.get()
        cutoff_map = {
            '1M': today - _dt.timedelta(days=30),
            '3M': today - _dt.timedelta(days=91),
            '6M': today - _dt.timedelta(days=182),
            'YTD': _dt.date(today.year, 1, 1),
            '1Y': today - _dt.timedelta(days=365),
            'Since 1st deposit': earliest,
        }
        cutoff = cutoff_map.get(period, earliest)

        # Build daily net-deposit and total-cost-basis series
        full_cal = pd.date_range(earliest, today, freq='D')
        net_dep  = pd.Series(0.0, index=full_cal)
        net_cost = pd.Series(0.0, index=full_cal)

        running_dep  = 0.0
        running_cost = 0.0
        from collections import defaultdict
        shares_s: dict = defaultdict(float)
        cost_s:   dict = defaultdict(float)
        txn_idx = 0

        for day in full_cal:
            day_date = day.date()
            while txn_idx < len(all_txns) and all_txns[txn_idx][0] <= day_date:
                d, ttype, amount, ccy = all_txns[txn_idx]
                if ttype in ('deposit', 'Deposit'):
                    running_dep += abs(amount)
                elif ttype in ('withdraw', 'Withdraw', 'Widthdraw'):
                    running_dep -= abs(amount)
                elif ttype in ('buy', 'Buy'):
                    running_cost += abs(amount)
                elif ttype in ('sell', 'Sell'):
                    running_cost -= abs(amount)
                txn_idx += 1
            net_dep.loc[day]  = running_dep
            net_cost.loc[day] = running_cost

        # Total Value = live prices × units held per day, using the same
        # _mf_get_price_history infrastructure already built for MYFOLIO
        total_mv = pd.Series(0.0, index=full_cal)
        price_data = getattr(self, '_last_price_data', {})
        for port_key, port in self._portfolios.items():
            for ticker, item in port.get('holdings', {}).items():
                if item.get('_is_cash'): continue
                mkt = item.get('market', 'MY')
                # Always fetch full history for the portfolio chart — the initial
                # _last_hist_data only has 3 months (optimised for the holdings table).
                hist = _mf_get_price_history(ticker, mkt, start_date=earliest)
                if hist is None or hist.empty:
                    p = price_data.get(ticker)
                    if p:
                        total_mv += item['shares'] * p
                    continue
                # Reindex onto full daily calendar with forward-fill
                ph = hist.reindex(full_cal, method='ffill').bfill()
                total_mv += item['shares'] * ph.fillna(0.0)

        # Apply period cutoff
        cutoff_ts = pd.Timestamp(cutoff)
        mv_plot  = total_mv[total_mv.index >= cutoff_ts]
        dep_plot = net_dep[net_dep.index >= cutoff_ts]

        # Store for hover lookup
        self._pchart_data = {
            'dates': mv_plot.index, 'mv': mv_plot.values, 'dep': dep_plot.values
        }

        import matplotlib.dates as mdates
        ax.plot(mv_plot.index, mv_plot.values, color='#4a90d9', linewidth=1.5,
                label='Total value', zorder=4)
        ax.plot(dep_plot.index, dep_plot.values, color=self.DIM, linewidth=1.2,
                linestyle='--', label='Net deposits', zorder=3)
        ax.fill_between(mv_plot.index, mv_plot.values, dep_plot.values,
                        where=(mv_plot.values >= dep_plot.values),
                        color='#4a90d9', alpha=0.08, interpolate=True)
        ax.fill_between(mv_plot.index, mv_plot.values, dep_plot.values,
                        where=(mv_plot.values < dep_plot.values),
                        color=self.RED, alpha=0.08, interpolate=True)

        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        self._pchart_fig.autofmt_xdate(rotation=0, ha='center')
        self._pchart_canvas.draw()

    def _clear_pchart_hover(self):
        for a in self._pchart_hover_artists:
            try: a.remove()
            except Exception: pass
        self._pchart_hover_artists = []
        if hasattr(self, '_pchart_canvas'):
            self._pchart_canvas.draw_idle()

    def _on_pchart_hover(self, event):
        """t18-style hover tooltip: vertical crosshair + floating box showing
        date, total returns after fees, Total value, and Net deposits."""
        d = self._pchart_data
        if not d or event.xdata is None or event.inaxes != self._pchart_ax:
            self._clear_pchart_hover()
            return

        import matplotlib.dates as mdates
        try:
            hover_dt = mdates.num2date(event.xdata)
            if hover_dt.tzinfo:
                hover_dt = hover_dt.replace(tzinfo=None)
        except Exception:
            return

        import pandas as pd
        idx_arr = d['dates']
        pos = idx_arr.searchsorted(pd.Timestamp(hover_dt))
        pos = min(max(0, pos), len(idx_arr) - 1)

        dt_val  = idx_arr[pos]
        mv_val  = d['mv'][pos]
        dep_val = d['dep'][pos]
        ret_val = mv_val - dep_val

        self._clear_pchart_hover()
        ax = self._pchart_ax
        vline = ax.axvline(dt_val, color=self.DIM, linewidth=0.8,
                           linestyle='--', zorder=5)
        self._pchart_hover_artists.append(vline)

        ret_color = '#4a90d9' if ret_val >= 0 else self.RED
        prefix = 'RM'
        ann = ax.annotate(
            f'{dt_val.strftime("%d %b %Y")}\n'
            f'Total returns after fees\n'
            f'{prefix}{ret_val:+,.2f}\n\n'
            f'○ {prefix}{mv_val:,.2f}\n'
            f'○ {prefix}{dep_val:,.2f}',
            xy=(dt_val, mv_val),
            xytext=(12, 0), textcoords='offset points',
            fontsize=7.5, color=self.FG, ha='left', va='center', zorder=7,
            bbox=dict(boxstyle='round,pad=0.5', fc='#1a2035', ec=self.BORDER,
                      lw=1, alpha=0.95),
        )
        self._pchart_hover_artists.append(ann)
        # Coloured return value line inside the annotation — override just that text
        # (matplotlib doesn't support inline multi-colour, so we add a separate
        # annotation offset to highlight the return value in the correct colour)
        ann2 = ax.annotate(
            f'{prefix}{ret_val:+,.2f}',
            xy=(dt_val, mv_val),
            xytext=(12 + 0.5, -14), textcoords='offset points',
            fontsize=8, fontweight='bold', color=ret_color,
            ha='left', va='center', zorder=8,
        )
        self._pchart_hover_artists.append(ann2)
        self._pchart_canvas.draw_idle()

    def _view_options(self):
        """Filter buttons, driven by what's actually in the ledger.
        A MYR+USD user shouldn't be staring at empty HKD/SGD buttons, and an
        HK/SG user must not be stuck with only MYR/USD."""
        held = {_port_ccy(pk) for pk in (self._portfolios or {})}
        opts = [(c, MARKET_SPEC[c]['market'])
                for c in ('MYR', 'USD', 'HKD', 'SGD') if c in held]
        if not opts:                       # nothing loaded yet
            opts = [('MYR', 'MY'), ('USD', 'US')]
        if len(opts) > 1:
            opts.append(('⇄ ALL', 'BOTH'))
        return opts

    def _rebuild_view_buttons(self):
        """Redraw the filter row after portfolios change (e.g. first HK buy)."""
        row = getattr(self, '_view_btn_row', None)
        if row is None:
            return
        for b in getattr(self, '_view_btns', {}).values():
            b.destroy()
        self._view_btns = {}
        valid = {v for _, v in self._view_options()}
        if self._view_var.get() not in valid:
            self._view_var.set('BOTH' if 'BOTH' in valid else next(iter(valid)))
        for label, val in self._view_options():
            btn = tk.Radiobutton(row, text=label, variable=self._view_var, value=val,
                                 command=self._on_view_change, font=self.FONT_B,
                                 relief='flat', cursor='hand2', bg='#1a2035',
                                 fg=self.FG, selectcolor='#00b4d8',
                                 activebackground='#1a2035', activeforeground=self.TEAL,
                                 indicatoron=False, padx=10, pady=4)
            btn.pack(side='left', padx=2)
            self._view_btns[val] = btn

    def _build_header(self):
        hdr = tk.Frame(self, bg=self.BG)
        hdr.pack(fill='x', padx=14, pady=(10, 4))

        # Title
        tk.Label(hdr, text='SCA', font=('Courier New', 14, 'bold'),
                 fg=self.TEAL, bg=self.BG).pack(side='left')
        tk.Label(hdr, text=' · ', font=('Courier New', 14),
                 fg=self.DIM, bg=self.BG).pack(side='left')
        tk.Label(hdr, text='NEWPOR TRACKER', font=('Courier New', 14, 'bold'),
                 fg=self.FG, bg=self.BG).pack(side='left')

        # Whose portfolio is loaded
        self._src_var = tk.StringVar(value='')
        tk.Label(hdr, textvariable=self._src_var, font=('Courier New', 8),
                 fg=self.GOLD if hasattr(self, 'GOLD') else '#FFD700',
                 bg=self.BG).pack(side='left', padx=(10, 0))
        self._refresh_source_label()

        # Status indicators
        self._sheet_ind = tk.Label(hdr, text='● Sheet', font=self.FONT,
                                    fg=self.DIM, bg=self.BG)
        self._sheet_ind.pack(side='left', padx=(20, 4))
        self._fx_ind = tk.Label(hdr, text='● USDMYR', font=self.FONT,
                                 fg=self.DIM, bg=self.BG)
        self._fx_ind.pack(side='left', padx=(0, 8))
        self._time_var = tk.StringVar(value='')
        tk.Label(hdr, textvariable=self._time_var, font=self.FONT,
                 fg=self.FG2, bg=self.BG).pack(side='left', padx=(0, 6))
        self._auto_var = tk.StringVar(value=f'· Auto {self._auto_secs}s')
        tk.Label(hdr, textvariable=self._auto_var, font=self.FONT,
                 fg=self.DIM, bg=self.BG).pack(side='left')

        # Right controls
        ctrl = tk.Frame(hdr, bg=self.BG)
        ctrl.pack(side='right')

        self._view_btn_row = ctrl
        self._view_btns    = {}
        self._rebuild_view_buttons()

        tk.Button(ctrl, text='📂 Source', font=self.FONT, bg='#1a2035',
                  fg='#FFD700', relief='flat', padx=8, pady=4, cursor='hand2',
                  command=self._open_source_dialog).pack(side='left', padx=(6, 2))
        tk.Button(ctrl, text='↻', font=self.FONT_B, bg='#1a2035', fg=self.TEAL,
                  relief='flat', padx=8, pady=4, cursor='hand2',
                  command=self._refresh).pack(side='left', padx=(6, 2))

    def _refresh_source_label(self):
        try:
            src = sca_get_source(load_app_settings())
            who = CURRENT_KID or 'guest'
            icon = {'sheet': '📗', 'csv': '📄'}.get(src.get('mode'), '●')
            self._src_var.set(f'{icon} {who} · {src.get("label", "")}')
        except Exception:
            self._src_var.set('')

    def _open_source_dialog(self, first_run: bool = False) -> bool:
        """Link a Google Sheet or import a CSV as this K-ID's portfolio source.
        Returns True if a source was saved. On first_run the dialog is modal
        and has no Cancel — the caller closes SCA if it returns False."""
        settings = load_app_settings()
        cur      = sca_get_source(settings)
        who      = CURRENT_KID or ''
        self._src_saved = False

        win = tk.Toplevel(self)
        win.title('SCA — Connect Your Portfolio' if first_run else 'SCA — Portfolio Source')
        win.configure(bg=self.BG)
        win.geometry('620x520')
        win.transient(self)
        win.grab_set()
        if first_run:
            # X on the window means "I don't want to pick" -> caller closes SCA
            win.protocol('WM_DELETE_WINDOW', win.destroy)

        tk.Label(win,
                 text='🔗  CONNECT YOUR PORTFOLIO' if first_run else '📂  PORTFOLIO SOURCE',
                 font=('Courier New', 12, 'bold'),
                 fg=self.TEAL, bg=self.BG).pack(pady=(14, 2))
        tk.Label(win, text=f'K-ID: {who or "guest (not signed in)"}',
                 font=('Courier New', 8), fg=self.DIM, bg=self.BG).pack()
        tk.Label(win,
                 text='Your ledger stays on your own sheet — nothing is uploaded anywhere.',
                 font=('Courier New', 7), fg=self.DIM, bg=self.BG).pack(pady=(2, 10))

        mode_var = tk.StringVar(value=cur.get('mode') or 'sheet')

        # ── Option 1: Google Sheet ────────────────────────────────────────────
        c1 = tk.Frame(win, bg=self.PANEL, highlightbackground=self.BORDER,
                      highlightthickness=1, padx=12, pady=10)
        c1.pack(fill='x', padx=16, pady=4)
        tk.Radiobutton(c1, text='📗  Link my Google Sheet', variable=mode_var,
                       value='sheet', font=('Courier New', 9, 'bold'),
                       bg=self.PANEL, fg=self.TEAL, selectcolor=self.PANEL,
                       activebackground=self.PANEL, activeforeground=self.TEAL,
                       relief='flat', cursor='hand2').pack(anchor='w')
        tk.Label(c1, text='Paste any Sheets URL — /edit, /pubhtml or a CSV link all work.',
                 font=('Courier New', 7), fg=self.DIM, bg=self.PANEL).pack(anchor='w')
        url_var = tk.StringVar(value=cur.get('url', '') if cur.get('mode') == 'sheet' else '')
        tk.Entry(c1, textvariable=url_var, font=('Courier New', 8), bg='#0d1117',
                 fg=self.FG, insertbackground=self.TEAL, relief='flat',
                 highlightbackground='#30363d', highlightthickness=1
                 ).pack(fill='x', pady=(6, 4), ipady=4)
        tk.Label(c1,
                 text='Sheet must be shared: File → Share → Publish to web → CSV,\n'
                      'or set link-sharing to "Anyone with the link".',
                 font=('Courier New', 7), fg=self.DIM, bg=self.PANEL,
                 justify='left').pack(anchor='w')

        # ── Option 2: CSV import ──────────────────────────────────────────────
        c2 = tk.Frame(win, bg=self.PANEL, highlightbackground=self.BORDER,
                      highlightthickness=1, padx=12, pady=10)
        c2.pack(fill='x', padx=16, pady=4)
        tk.Radiobutton(c2, text='📄  Import a CSV file', variable=mode_var,
                       value='csv', font=('Courier New', 9, 'bold'),
                       bg=self.PANEL, fg='#FFD700', selectcolor=self.PANEL,
                       activebackground=self.PANEL, activeforeground='#FFD700',
                       relief='flat', cursor='hand2').pack(anchor='w')
        tk.Label(c2, text='Offline — read straight off disk, never uploaded.',
                 font=('Courier New', 7), fg=self.DIM, bg=self.PANEL).pack(anchor='w')
        csv_var = tk.StringVar(value=cur.get('csv_path', '') if cur.get('mode') == 'csv' else '')
        row2 = tk.Frame(c2, bg=self.PANEL)
        row2.pack(fill='x', pady=(6, 0))
        tk.Entry(row2, textvariable=csv_var, font=('Courier New', 8), bg='#0d1117',
                 fg=self.FG, insertbackground=self.TEAL, relief='flat',
                 highlightbackground='#30363d', highlightthickness=1
                 ).pack(side='left', fill='x', expand=True, ipady=4)

        def _browse():
            p = filedialog.askopenfilename(
                title='Select portfolio CSV',
                filetypes=[('CSV files', '*.csv'), ('All files', '*.*')])
            if p:
                csv_var.set(p)
                mode_var.set('csv')
        tk.Button(row2, text='Browse…', command=_browse, font=('Courier New', 8),
                  bg='#1a2035', fg=self.FG2, relief='flat', padx=8,
                  cursor='hand2').pack(side='left', padx=(4, 0))

        # ── Required columns hint ─────────────────────────────────────────────
        tk.Label(win,
                 text='Required columns:  Date · Transaction Type · AssetCode · '
                      'Currency · Unit · Value · Amount',
                 font=('Courier New', 7), fg=self.DIM, bg=self.BG,
                 wraplength=560, justify='left').pack(pady=(8, 0), padx=16, anchor='w')

        status_var = tk.StringVar(value='')
        tk.Label(win, textvariable=status_var, font=('Courier New', 8),
                 fg='#FFD700', bg=self.BG, wraplength=560,
                 justify='left').pack(pady=(4, 0), padx=16, anchor='w')

        def _save_and_load():
            m = mode_var.get()
            if m == 'sheet':
                u = url_var.get().strip()
                if not u:
                    status_var.set('⚠️  Paste a Google Sheets URL first.'); return
                norm = sca_normalise_sheet_url(u)
                if 'docs.google.com' not in norm:
                    status_var.set("⚠️  That doesn't look like a Google Sheets URL."); return
                status_var.set('⏳  Testing sheet…')
                win.update_idletasks()
                try:
                    txt, _ = _mf_fetch_online_chunked(norm)
                    rows   = _mf_parse_rows(txt)
                    if not rows:
                        status_var.set('⚠️  Sheet loaded but has no transaction rows.'); return
                    v = _mf_validate_ledger(rows)
                    if not v.get('ok'):
                        status_var.set('⚠️  ' + '; '.join(v.get('errors', ['Invalid ledger'])[:2]))
                        return
                    sca_save_source(settings, who, 'sheet', url=norm,
                                    label=f'My Sheet ({len(rows)} txns)')
                except Exception as e:
                    status_var.set(f'❌  Could not read sheet:\n{type(e).__name__}: {e}')
                    return
            elif m == 'csv':
                p = csv_var.get().strip()
                if not p or not os.path.exists(p):
                    status_var.set('⚠️  Pick a CSV file that exists.'); return
                try:
                    with open(p, 'r', encoding='utf-8-sig') as f:
                        rows = _mf_parse_rows(f.read())
                    if not rows:
                        status_var.set('⚠️  CSV has no transaction rows.'); return
                    v = _mf_validate_ledger(rows)
                    if not v.get('ok'):
                        status_var.set('⚠️  ' + '; '.join(v.get('errors', ['Invalid ledger'])[:2]))
                        return
                    sca_save_source(settings, who, 'csv', csv_path=p,
                                    label=f'{os.path.basename(p)} ({len(rows)} txns)')
                except Exception as e:
                    status_var.set(f'❌  Could not read CSV: {e}'); return
            else:
                status_var.set('⚠️  Pick a Google Sheet or a CSV file.'); return

            self._src_saved = True
            self._refresh_source_label()
            win.destroy()
            if first_run:
                # First run — nothing is loaded yet, so kick off the initial load
                self._load_from_cache_then_refresh()
            else:
                messagebox.showinfo('SCA', 'Portfolio source saved. Reloading…', parent=self)
                self._refresh()

        btns = tk.Frame(win, bg=self.BG)
        btns.pack(fill='x', padx=16, pady=12)
        tk.Button(btns, text='💾  Save & Load', command=_save_and_load,
                  font=('Courier New', 10, 'bold'), bg=self.TEAL, fg='#06121f',
                  relief='flat', padx=14, pady=7, cursor='hand2'
                  ).pack(side='left', expand=True, fill='x', padx=(0, 4))
        tk.Button(btns,
                  text='Not now (close SCA)' if first_run else 'Cancel',
                  command=win.destroy,
                  font=('Courier New', 9), bg='#1a2035', fg=self.FG2,
                  relief='flat', padx=14, pady=7, cursor='hand2'
                  ).pack(side='left', expand=True, fill='x', padx=(4, 0))

        # Block until they decide, so the caller knows whether to close SCA
        self.wait_window(win)
        return self._src_saved

    def _build_error_banner(self):
        self._banner_frame = tk.Frame(self._scroll_body, bg='#1a0a0a',
                                      highlightbackground=self.RED, highlightthickness=1)
        self._banner_msg = tk.Label(self._banner_frame, text='', font=self.FONT,
                                    fg=self.RED, bg='#1a0a0a', anchor='w', wraplength=1300,
                                    justify='left')
        self._banner_msg.pack(fill='x', padx=12, pady=(6, 2))
        self._banner_sub = tk.Label(self._banner_frame, text='', font=('Courier New', 8),
                                    fg=self.FG2, bg='#1a0a0a', anchor='w', wraplength=1300)
        self._banner_sub.pack(fill='x', padx=12, pady=(0, 6))

    CCY_ACCENT = {'MYR': '#00b4d8', 'USD': '#00e676',
                  'HKD': '#ff6b6b', 'SGD': '#ffd166'}

    def _build_summary_cards(self):
        self._cards_row = tk.Frame(self._scroll_body, bg=self.BG)
        self._cards_row.pack(fill='x', padx=14, pady=(4, 6))
        self._ccy_cards = {}          # ccy -> card frame
        self._rebuild_summary_cards()
        # Back-compat aliases — older code paths still reach for these
        self._my_card = self._ccy_cards.get('MYR')
        self._us_card = self._ccy_cards.get('USD')

    def _rebuild_summary_cards(self):
        """One card per currency actually held. Rebuilt when the ledger
        gains a new market, so a first HK buy grows an HKD card."""
        for w in self._cards_row.winfo_children():
            w.destroy()
        self._ccy_cards = {}
        held = [c for c in ('MYR', 'USD', 'HKD', 'SGD')
                if _NEWPOR_CCY_MAP.get(c, ('',))[0] in (self._portfolios or {})]
        if not held:
            held = ['MYR', 'USD']     # nothing loaded yet
        for i, ccy in enumerate(held):
            self._cards_row.columnconfigure(i, weight=1)
            card = self._make_summary_card(
                self._cards_row, f'{ccy} POSITION',
                self.CCY_ACCENT.get(ccy, self.TEAL), ccy)
            card.grid(row=0, column=i, sticky='nsew',
                      padx=(0 if i == 0 else 6, 0 if i == len(held) - 1 else 6))
            self._ccy_cards[ccy] = card
        self._my_card = self._ccy_cards.get('MYR')
        self._us_card = self._ccy_cards.get('USD')

    def _make_summary_card(self, parent, title, accent, ccy):
        frame = tk.Frame(parent, bg=self.PANEL, highlightbackground=accent,
                         highlightthickness=1, padx=16, pady=12)
        tk.Label(frame, text=title, font=self.FONT_B, fg=accent, bg=self.PANEL,
                 anchor='w').pack(fill='x', pady=(0, 8))
        rows = {}
        for field in ('Value', 'Cost', 'P/L', 'Return'):
            r = tk.Frame(frame, bg=self.PANEL)
            r.pack(fill='x', pady=1)
            tk.Label(r, text=field, font=self.FONT, fg=self.FG2, bg=self.PANEL,
                     width=10, anchor='w').pack(side='left')
            var = tk.StringVar(value=f'{ccy} 0.00' if field != 'Return' else '+0.00%')
            lbl = tk.Label(r, textvariable=var, font=self.FONT_B,
                           fg=accent if field == 'Value' else self.FG,
                           bg=self.PANEL, anchor='e')
            lbl.pack(side='right')
            rows[field] = (var, lbl)
        pos_var = tk.StringVar(value='0 positions')
        tk.Label(frame, textvariable=pos_var, font=self.FONT,
                 fg=self.DIM, bg=self.PANEL, anchor='w').pack(fill='x', pady=(6, 0))
        frame._rows = rows
        frame._pos_var = pos_var
        frame._ccy = ccy
        return frame

    def _build_tabs(self):
        tab_row = tk.Frame(self._scroll_body, bg=self.BG)
        tab_row.pack(fill='x', padx=14, pady=(0, 4))
        self._tab_btns = {}
        for label, val in [('⊕ ALL', 'ALL'), ('MYR Position', 'MY'), ('USD Position', 'US')]:
            btn = tk.Radiobutton(tab_row, text=label, variable=self._tab_var, value=val,
                                  command=self._on_tab_change,
                                  font=self.FONT_B, relief='flat', cursor='hand2',
                                  bg=self.PANEL, fg=self.FG,
                                  selectcolor=self.TEAL, activebackground=self.PANEL,
                                  activeforeground=self.TEAL,
                                  indicatoron=False, padx=14, pady=6)
            btn.pack(side='left', padx=(0, 4))
            self._tab_btns[val] = btn

        # ── Composition pie toggle ────────────────────────────────────────────
        self._pie_open = tk.BooleanVar(value=False)
        self._pie_btn = tk.Button(
            tab_row, text='🥧  Composition', font=self.FONT_B,
            bg=self.PANEL, fg=self.GOLD,
            relief='flat', cursor='hand2', padx=14, pady=6,
            command=self._toggle_pie)
        self._pie_btn.pack(side='left', padx=(10, 0))

        # Panel lives directly under the row, collapsed by default
        self._pie_panel = tk.Frame(self._scroll_body, bg=self.BG)
        self._pie_canvas = None

    def _toggle_pie(self):
        """Show/hide the composition pie. Built lazily — a matplotlib canvas
        that nobody opens is pure startup cost."""
        opening = not self._pie_open.get()
        self._pie_open.set(opening)
        if opening:
            anchor = getattr(self, '_holdings_anchor', None)
            if anchor is not None and anchor.winfo_exists():
                self._pie_panel.pack(fill='x', padx=14, pady=(0, 6), before=anchor)
            else:
                self._pie_panel.pack(fill='x', padx=14, pady=(0, 6))
            self._pie_btn.configure(text='🥧  Composition  ▲')
            self._draw_pie()
        else:
            self._pie_panel.pack_forget()
            self._pie_btn.configure(text='🥧  Composition')

    def _draw_pie(self):
        """Pie of current holdings by market value, honouring the active tab.
        USD holdings are converted to MYR so one pie can show both sides —
        adding a $ slice to an RM slice would otherwise be meaningless."""
        if not self._pie_open.get():
            return
        try:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        except Exception:
            return

        for w in self._pie_panel.winfo_children():
            w.destroy()
        self._pie_canvas = None

        tab   = self._tab_var.get()
        rows  = []          # (ticker, myr_value)
        for pk, port in (self._portfolios or {}).items():
            ccy = _port_ccy(pk)
            mkt = MARKET_SPEC.get(ccy, {}).get('market', 'US')
            if tab != 'ALL' and tab != mkt:
                continue
            fx = (self._fx_rates or {}).get(ccy)
            if not fx:
                continue                # no rate -> can't mix into a MYR pie
            for tkr, item in port.get('holdings', {}).items():
                if item.get('_is_cash'):
                    continue
                sh = item.get('shares', 0)
                if sh <= 0:
                    continue
                px = (self._last_price_data or {}).get(tkr)
                if not px:
                    continue
                short = tkr
                for _suf in ('.KL', '.HK', '.SI'):
                    short = short.replace(_suf, '')
                rows.append((short, sh * px * fx))

        if not rows:
            tk.Label(self._pie_panel,
                     text='No priced holdings to chart yet.',
                     font=self.FONT, fg=self.DIM, bg=self.BG).pack(pady=10)
            return

        rows.sort(key=lambda r: -r[1])
        total = sum(v for _, v in rows)

        # Long tails make an unreadable pie — keep the top 12, group the rest.
        TOP = 12
        if len(rows) > TOP:
            head, tail = rows[:TOP], rows[TOP:]
            head.append((f'Others ({len(tail)})', sum(v for _, v in tail)))
            rows = head

        labels = [f'{t}  {v/total*100:.2f}%' for t, v in rows]
        vals   = [v for _, v in rows]

        fig = Figure(figsize=(7.2, 3.0), dpi=100, facecolor=self.BG)
        ax  = fig.add_subplot(111)
        ax.set_facecolor(self.BG)
        try:
            import matplotlib.cm as cm
            import numpy as _np
            colors = cm.get_cmap('tab20')(_np.linspace(0, 1, len(vals)))
        except Exception:
            colors = None

        wedges, _ = ax.pie(vals, startangle=90, colors=colors,
                           wedgeprops=dict(width=0.42, edgecolor=self.BG,
                                           linewidth=1.2))
        ax.axis('equal')

        _c = _MARKET_TO_CCY.get(tab)
        ccy_note = ('MYR' if _c == 'MYR' else
                    f'{_c}→MYR' if _c else 'MYR (all converted)')
        ax.text(0, 0.06, f'RM{total:,.2f}', ha='center', va='center',
                fontsize=12, fontweight='bold', color=self.FG)
        ax.text(0, -0.14, f'{len(rows)} slices · {ccy_note}', ha='center',
                va='center', fontsize=7, color=self.DIM)

        ax.legend(wedges, labels, loc='center left',
                  bbox_to_anchor=(0.98, 0.5), fontsize=6.5, ncol=2,
                  frameon=False, labelcolor=self.FG2)
        fig.subplots_adjust(left=0.02, right=0.58, top=0.98, bottom=0.02)

        self._pie_canvas = FigureCanvasTkAgg(fig, master=self._pie_panel)
        self._pie_canvas.draw()
        self._pie_canvas.get_tk_widget().pack(fill='x')

    _SORT_ARROWS = {None: '', False: '  ▲', True: '  ▼'}

    @staticmethod
    def _sort_key(val: str):
        """Turn a displayed cell back into something sortable.

        Cells are pre-formatted strings ('RM0.7403', '+1.30%', '18.53%', '—'),
        so a plain string sort puts RM10 before RM9 and '-5%' next to '-0.1%'.
        Strip the decoration and sort on the number underneath; anything that
        isn't numeric falls back to case-insensitive text.
        """
        s = (val or '').strip()
        if s in ('—', '', '…'):
            return (2, 0.0, '')          # blanks always sort last
        cleaned = (s.replace('RM', '').replace('$', '')
                    .replace('%', '').replace(',', '').replace('+', '').strip())
        try:
            return (0, float(cleaned), '')
        except ValueError:
            return (1, 0.0, s.lower())

    def _sort_holdings(self, col: str):
        """Sort the holdings table by a column. Click again to flip direction."""
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col  = col
            # Numbers are most useful biggest-first; text reads better A→Z.
            self._sort_desc = col not in ('TICKER', 'PORTFOLIO', 'CCY')

        idx  = self._hold_tree['columns'].index(col)
        rows = [(self._hold_tree.set(k, col), k)
                for k in self._hold_tree.get_children('')]
        rows.sort(key=lambda r: self._sort_key(r[0]), reverse=self._sort_desc)
        for pos, (_, k) in enumerate(rows):
            self._hold_tree.move(k, '', pos)

        # Arrow on the active column only
        for c in self._hold_tree['columns']:
            self._hold_tree.heading(
                c, text=c + (self._SORT_ARROWS[self._sort_desc] if c == col else ''))

    def _reapply_sort(self):
        """Re-sort after a data refresh so the chosen order survives."""
        if self._sort_col:
            col, desc = self._sort_col, self._sort_desc
            self._sort_col = None          # force _sort_holdings to keep `desc`
            self._sort_desc = not desc
            self._sort_holdings(col)

    def _build_holdings_table(self):
        # Anchor so the pie panel can be inserted directly above this block
        self._holdings_anchor = tk.Frame(self._scroll_body, bg=self.BG, height=0)
        self._holdings_anchor.pack(fill='x')
        hdr = tk.Frame(self._scroll_body, bg=self.BG)
        hdr.pack(fill='x', padx=14)
        tk.Label(hdr, text='HOLDINGS', font=self.FONT_B, fg=self.FG2, bg=self.BG,
                 anchor='w').pack(side='left')
        self._pos_total_var = tk.StringVar(value='0 positions')
        tk.Label(hdr, textvariable=self._pos_total_var, font=self.FONT,
                 fg=self.DIM, bg=self.BG, anchor='e').pack(side='right')

        frame = tk.Frame(self._scroll_body, bg=self.BG)
        frame.pack(fill='both', expand=True, padx=14, pady=(2, 0))

        cols = ('TICKER','PORTFOLIO','CCY','QTY','AVG COST','LIVE PRICE',
                'DAY Δ','VALUE','P/L','P/L %','WEIGHT','1Y TREND')
        self._hold_tree = ttk.Treeview(frame, columns=cols, show='headings', height=12)
        cw = {'TICKER':88,'PORTFOLIO':110,'CCY':50,'QTY':80,'AVG COST':90,
              'LIVE PRICE':90,'DAY Δ':80,'VALUE':110,'P/L':100,'P/L %':70,
              'WEIGHT':70,'1Y TREND':80}
        for col in cols:
            self._hold_tree.heading(
                col, text=col,
                command=lambda c=col: self._sort_holdings(c))
            self._hold_tree.column(col, width=cw.get(col, 80),
                                   anchor='w' if col in ('TICKER','PORTFOLIO') else 'e',
                                   stretch=False)
        self._sort_col     = None
        self._sort_desc    = False

        sty = ttk.Style(self)
        sty.configure('SCA.Treeview', background=self.PANEL, fieldbackground=self.PANEL,
                       foreground=self.FG, rowheight=24, font=self.FONT)
        sty.configure('SCA.Treeview.Heading', background=self.BG, foreground=self.DIM,
                       font=('Courier New', 8, 'bold'), relief='flat')
        sty.map('SCA.Treeview', background=[('selected', '#1a2a3a')])
        self._hold_tree.configure(style='SCA.Treeview')

        vsb = ttk.Scrollbar(frame, orient='vertical', command=self._hold_tree.yview)
        hsb = ttk.Scrollbar(frame, orient='horizontal', command=self._hold_tree.xview)
        self._hold_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._hold_tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)

        self._hold_tree.tag_configure('gain',    foreground=self.GREEN)
        self._hold_tree.tag_configure('loss',    foreground=self.RED)
        self._hold_tree.tag_configure('neutral', foreground=self.FG)
        self._hold_tree.tag_configure('my',      foreground=self.TEAL)
        self._hold_tree.tag_configure('us',      foreground=self.GREEN)

        self._hold_tree.bind('<ButtonRelease-1>', self._on_row_click)
        self._selected_ticker = None

    def _refresh_today_activity(self):
        """Populate Today's Activity from raw NEWPOR rows.
        Shows every Buy and Sell transacted today with ticker, units, price, amount.
        """
        import datetime as _dt
        today = _dt.date.today()
        # Google Sheets CSV exports dates WITHOUT leading zeros: 7/13/2026 not 07/13/2026
        # Cover all realistic format variants so nothing slips through
        today_fmts = {
            today.strftime('%m/%d/%Y'),                          # 07/13/2026
            f'{today.month}/{today.day}/{today.year}',           # 7/13/2026 (no leading zeros)
            today.strftime('%d/%m/%Y'),                          # 13/07/2026
            f'{today.day}/{today.month}/{today.year}',           # 13/7/2026
            today.strftime('%Y-%m-%d'),                          # 2026-07-13
            today.strftime('%m-%d-%Y'),                          # 07-13-2026
        }

        if not hasattr(self, '_act_buy_frame'):
            return

        raw_rows = getattr(self, '_rows', None) or []

        buys  = []
        sells = []

        for row in raw_rows:
            date_s = row.get(_NC_DATE, '').strip()
            if not date_s:
                continue
            # Fast check against known format strings first
            if date_s not in today_fmts:
                # Slow fallback: try parsing and compare date object
                matched = False
                for fmt in ('%m/%d/%Y', '%d/%m/%Y', '%Y-%m-%d', '%m-%d-%Y', '%d-%m-%Y'):
                    try:
                        if _dt.datetime.strptime(date_s, fmt).date() == today:
                            matched = True
                            today_fmts.add(date_s)   # cache for remaining rows
                            break
                    except ValueError:
                        continue
                if not matched:
                    continue
            ttype  = row.get(_NC_TYPE,   '').strip().lower()
            asset  = row.get(_NC_ASSET,  '').strip()
            ccy    = row.get(_NC_CCY,    '').strip().upper()
            unit   = _mf_strip_currency(row.get(_NC_UNIT,   '0'))
            price  = _mf_strip_currency(row.get(_NC_VALUE,  '0'))
            amount = _mf_strip_currency(row.get(_NC_AMOUNT, '0'))
            prefix = 'RM' if ccy == 'MYR' else '$'

            if ttype in ('buy',):
                buys.append((asset, unit, price, amount, prefix))
            elif ttype in ('sell',):
                sells.append((asset, unit, price, amount, prefix))

        # Clear and rebuild buy panel
        for w in self._act_buy_frame.winfo_children():
            w.destroy()
        if buys:
            total_in = 0.0
            pfx0 = buys[0][4]
            for asset, unit, price, amount, pfx in buys:
                r = tk.Frame(self._act_buy_frame, bg='#0a1a0a')
                r.pack(fill='x', pady=1)
                tk.Label(r, text=asset, font=('Courier New', 8, 'bold'),
                         fg='#3FB950', bg='#0a1a0a', width=12, anchor='w').pack(side='left')
                qty_str = _fmt_units(abs(unit)) if pfx == 'RM' else f'{abs(unit):,.4f}'
                tk.Label(r, text=f'{qty_str} u @ {pfx}{price:,.4f}',
                         font=('Courier New', 7), fg=self.FG2,
                         bg='#0a1a0a').pack(side='left', padx=4)
                tk.Label(r, text=f'{pfx}{abs(amount):,.2f}',
                         font=('Courier New', 8, 'bold'),
                         fg='#3FB950', bg='#0a1a0a', anchor='e').pack(side='right')
                total_in += abs(amount)
            self._act_buy_total.configure(
                text=f'Total in:  {pfx0}{total_in:,.2f}  ({len(buys)} trade{"s" if len(buys)!=1 else ""})')
        else:
            tk.Label(self._act_buy_frame, text='No buys today',
                     font=('Courier New', 8), fg=self.DIM,
                     bg='#0a1a0a').pack(anchor='w')
            self._act_buy_total.configure(text='')

        # Clear and rebuild sell panel
        for w in self._act_sell_frame.winfo_children():
            w.destroy()
        if sells:
            total_out = 0.0
            pfx0 = sells[0][4]
            for asset, unit, price, amount, pfx in sells:
                r = tk.Frame(self._act_sell_frame, bg='#1a0a0a')
                r.pack(fill='x', pady=1)
                tk.Label(r, text=asset, font=('Courier New', 8, 'bold'),
                         fg='#F85149', bg='#1a0a0a', width=12, anchor='w').pack(side='left')
                qty_str = _fmt_units(abs(unit)) if pfx == 'RM' else f'{abs(unit):,.4f}'
                tk.Label(r, text=f'{qty_str} u @ {pfx}{price:,.4f}',
                         font=('Courier New', 7), fg=self.FG2,
                         bg='#1a0a0a').pack(side='left', padx=4)
                tk.Label(r, text=f'{pfx}{abs(amount):,.2f}',
                         font=('Courier New', 8, 'bold'),
                         fg='#F85149', bg='#1a0a0a', anchor='e').pack(side='right')
                total_out += abs(amount)
            self._act_sell_total.configure(
                text=f'Total out: {pfx0}{total_out:,.2f}  ({len(sells)} trade{"s" if len(sells)!=1 else ""})')
        else:
            tk.Label(self._act_sell_frame, text='No sells today',
                     font=('Courier New', 8), fg=self.DIM,
                     bg='#1a0a0a').pack(anchor='w')
            self._act_sell_total.configure(text='')

    def _build_weight_section(self):
        self._weight_frame = tk.Frame(self._scroll_body, bg=self.PANEL, highlightbackground=self.BORDER,
                                      highlightthickness=1, pady=6)
        self._weight_frame.pack(fill='x', padx=14, pady=(6, 0))
        tk.Label(self._weight_frame, text='WEIGHT DISTRIBUTION  (within each portfolio)',
                 font=self.FONT, fg=self.FG2, bg=self.PANEL, anchor='w',
                 padx=12).pack(fill='x')
        self._weight_bar_frame = tk.Frame(self._weight_frame, bg=self.PANEL)
        self._weight_bar_frame.pack(fill='x', padx=12, pady=(4, 4))

    def _build_footer(self):
        # ── Today's Activity — Buy/Sell Direction ────────────────────────────
        import datetime as _dt
        today_str = _dt.date.today().strftime('%d %b %Y')

        act_sep = tk.Frame(self._scroll_body, bg=self.BORDER, height=1)
        act_sep.pack(fill='x', padx=14, pady=(10, 0))

        act_hdr = tk.Frame(self._scroll_body, bg=self.BG)
        act_hdr.pack(fill='x', padx=14, pady=(6, 2))
        tk.Label(act_hdr, text='📈  TODAY\'S ACTIVITY',
                 font=('Courier New', 9, 'bold'), fg=self.TEAL,
                 bg=self.BG).pack(side='left')
        tk.Label(act_hdr, text=f'  {today_str}',
                 font=('Courier New', 8), fg=self.DIM,
                 bg=self.BG).pack(side='left')

        # Two columns: BUY side (left, green) | SELL side (right, red)
        act_body = tk.Frame(self._scroll_body, bg=self.BG)
        act_body.pack(fill='x', padx=14, pady=(0, 6))
        act_body.columnconfigure(0, weight=1)
        act_body.columnconfigure(1, weight=1)

        # BUY panel
        buy_panel = tk.Frame(act_body, bg='#0a1a0a',
                             highlightbackground='#3FB950', highlightthickness=1,
                             padx=10, pady=8)
        buy_panel.grid(row=0, column=0, sticky='nsew', padx=(0, 4))
        tk.Label(buy_panel, text='▲  BUY / IN',
                 font=('Courier New', 8, 'bold'), fg='#3FB950',
                 bg='#0a1a0a').pack(anchor='w', pady=(0, 4))
        self._act_buy_frame = tk.Frame(buy_panel, bg='#0a1a0a')
        self._act_buy_frame.pack(fill='x')
        self._act_buy_total = tk.Label(buy_panel, text='',
                                        font=('Courier New', 8, 'bold'),
                                        fg='#3FB950', bg='#0a1a0a', anchor='w')
        self._act_buy_total.pack(fill='x', pady=(4, 0))

        # SELL panel
        sell_panel = tk.Frame(act_body, bg='#1a0a0a',
                              highlightbackground='#F85149', highlightthickness=1,
                              padx=10, pady=8)
        sell_panel.grid(row=0, column=1, sticky='nsew', padx=(4, 0))
        tk.Label(sell_panel, text='▼  SELL / OUT',
                 font=('Courier New', 8, 'bold'), fg='#F85149',
                 bg='#1a0a0a').pack(anchor='w', pady=(0, 4))
        self._act_sell_frame = tk.Frame(sell_panel, bg='#1a0a0a')
        self._act_sell_frame.pack(fill='x')
        self._act_sell_total = tk.Label(sell_panel, text='',
                                         font=('Courier New', 8, 'bold'),
                                         fg='#F85149', bg='#1a0a0a', anchor='w')
        self._act_sell_total.pack(fill='x', pady=(4, 0))

        # ── ANALYTICS — external workbook link ────────────────────────────────
        # The in-app analytics charts were removed: the data lives in the
        # source sheet and is far more flexible to slice there. This panel
        # opens the workbook directly instead of half-rebuilding it in Tk.
        sep = tk.Frame(self._scroll_body, bg=self.BORDER, height=1)
        sep.pack(fill='x', padx=14, pady=(10, 0))

        an_hdr = tk.Frame(self._scroll_body, bg=self.BG)
        an_hdr.pack(fill='x', padx=14, pady=(6, 2))
        tk.Label(an_hdr, text='📊  ANALYTICS', font=('Courier New', 9, 'bold'),
                 fg=self.TEAL, bg=self.BG).pack(side='left')
        tk.Label(an_hdr, text='  ·  opens in your browser',
                 font=('Courier New', 8), fg=self.DIM, bg=self.BG).pack(side='left')

        an_card = tk.Frame(self._scroll_body, bg=self.PANEL,
                           highlightbackground=self.BORDER, highlightthickness=1,
                           padx=14, pady=12)
        an_card.pack(fill='x', padx=14, pady=(0, 8))

        tk.Label(an_card,
                 text='Charts, pivots and P/L breakdowns live in your own workbook.',
                 font=('Courier New', 8), fg=self.FG, bg=self.PANEL,
                 anchor='w').pack(fill='x')
        tk.Label(an_card,
                 text='Every transaction shown here is sourced from that sheet — '
                      'slice it there with full pivot/chart tooling.',
                 font=('Courier New', 7), fg=self.DIM, bg=self.PANEL,
                 anchor='w', wraplength=880, justify='left').pack(fill='x', pady=(2, 10))

        def _open_sheet():
            import webbrowser
            src_cfg = sca_get_source(load_app_settings())
            u = src_cfg.get('url', '')
            if not u:
                messagebox.showinfo(
                    'SCA',
                    'No Google Sheet connected.\n\n'
                    'If you imported a CSV there is no web page to open —\n'
                    'connect a Sheet via 📂 Source to use this.',
                    parent=self)
                return
            # Turn the CSV-export URL back into something a human can read
            page = u.replace('/export?format=csv&gid=', '/edit#gid=')
            page = page.replace('/pub?gid=0&single=true&output=csv', '/pubhtml')
            try:
                webbrowser.open_new_tab(page)
            except Exception as e:
                messagebox.showwarning('SCA', f'Could not open browser:\n{e}\n\n{page}',
                                       parent=self)

        def _copy_url():
            src_cfg = sca_get_source(load_app_settings())
            u = src_cfg.get('url') or src_cfg.get('csv_path') or ''
            if not u:
                messagebox.showinfo('SCA', 'Nothing connected yet.', parent=self)
                return
            try:
                self.clipboard_clear()
                self.clipboard_append(u)
                messagebox.showinfo('SCA', 'Link copied to clipboard.', parent=self)
            except Exception:
                pass

        btn_row = tk.Frame(an_card, bg=self.PANEL)
        btn_row.pack(fill='x')
        tk.Button(btn_row, text='📗  Open My Workbook', command=_open_sheet,
                  font=('Courier New', 9, 'bold'), bg=self.TEAL, fg='#06121f',
                  relief='flat', padx=14, pady=7,
                  cursor='hand2').pack(side='left')
        tk.Button(btn_row, text='🔗  Copy Link', command=_copy_url,
                  font=('Courier New', 8), bg='#1a2035', fg=self.FG2,
                  relief='flat', padx=12, pady=7,
                  cursor='hand2').pack(side='left', padx=(6, 0))

        # Footer
        footer = tk.Frame(self._scroll_body, bg=self.BG)
        footer.pack(fill='x', padx=14, pady=(6, 8))
        tk.Label(footer,
                 text='Source: PAA_MERGED NEWPOR Google Sheet · FX: Yahoo Finance USDMYR=X · '
                      'Auto-refresh 60s',
                 font=('Courier New', 7), fg=self.DIM, bg=self.BG).pack()
        tk.Label(footer,
                 text='GS = Google Sheet · Click any row in Holdings to expand 1Y chart',
                 font=('Courier New', 7), fg=self.DIM, bg=self.BG).pack()

    def _an_refresh_portfolio_dropdown(self):
        """No-op — in-app analytics were replaced by the workbook link."""
        return

    def _an_redraw(self):
        """No-op — in-app analytics were replaced by the workbook link."""
        return

    def _refresh(self, force_online: bool = True):
        """Reload the ledger. force_online=True (default) bypasses the 5-min
        cache TTL so a trade entered seconds ago actually shows up — the TTL
        is only meant to make the initial open instant."""
        if self._loading:
            return
        self._loading = True
        self._set_indicator(sheet=None, fx=None)
        threading.Thread(target=self._load_worker,
                         args=(force_online,), daemon=True).start()

    def _load_worker(self, force_online: bool = True):
        try:
            n_before = len(self._rows or [])
            rows  = _mf_load_csv(force_online=force_online)
            ports = _mf_build_portfolios(rows)
            valid = _mf_validate_ledger(rows)
            n_after = len(rows)
            self._rows = rows
            self.after(0, lambda: self._on_sheet_loaded(ports, valid,
                                                        n_before, n_after))
        except Exception as e:
            self.after(0, lambda: self._on_sheet_error(str(e)))

    def _on_sheet_loaded(self, ports, valid, n_before=0, n_after=0):
        self._sheet_ok = True
        self._set_indicator(sheet=True)
        if not valid.get('ok'):
            self._show_banner(f'Sheet loaded but validation failed: {valid["errors"][0] if valid["errors"] else "unknown"}',
                              sub='Fix your ledger sheet and reload.')
        else:
            self._hide_banner()
        self._apply_portfolios(ports)
        self._fetch_live_prices()
        # Tell the user when a refresh actually brought something in — a
        # silent update looks identical to a broken one.
        if n_before and n_after != n_before:
            d = n_after - n_before
            self._flash_status(
                f'🔄 Ledger updated: {d:+d} transaction{"s" if abs(d) != 1 else ""} '
                f'({n_before} → {n_after})')

    def _flash_status(self, msg: str, secs: int = 6):
        """Briefly surface a message in the status line, then restore it."""
        try:
            self._status_var.set(msg)
            if getattr(self, '_flash_after', None):
                self.after_cancel(self._flash_after)
            self._flash_after = self.after(secs * 1000, self._update_time)
        except Exception:
            pass

    def _on_sheet_error(self, msg):
        self._sheet_ok = False
        self._set_indicator(sheet=False)
        self._show_banner(f'✕ Cannot load NEWPOR sheet: {msg}',
                          sub='Make sure your Google Sheet is published via File → Share → Publish to web → CSV.\n'
                              'Then connect to the internet and reopen SCA so the local cache can be created.')
        self._loading = False

    def _apply_portfolios(self, ports):
        self._portfolios = ports or {}
        self._rebuild_view_buttons()   # a new currency should surface its filter
        self._rebuild_summary_cards()
        self._update_time()
        if hasattr(self, '_an_port_cb'):
            self._an_refresh_portfolio_dropdown()
        self._render_from_cache()
        # Refresh today's activity as soon as rows are available
        self.after(0, self._refresh_today_activity)
        # Paint the table immediately using whatever prices we already have on
        # disk (from last session's _MF_PRICE_CACHE / cache_get_live). Cells
        # with no cached price show '…' as a placeholder. The background price
        # worker will update them as live prices arrive — no more blank table
        # for 10-15s while the fetch runs.
        self._render_from_cache()

    def _render_from_cache(self):
        """Populate the holdings table instantly using disk-cached prices.
        Columns that need live data show '…' until _on_prices_loaded fires."""
        price_data = {}
        for port in self._portfolios.values():
            for ticker, item in port.get('holdings', {}).items():
                if item.get('_is_cash'):
                    continue
                mkt    = item.get('market', 'MY')
                lookup = _mf_ticker_for_market(ticker, mkt)
                # Check in-memory cache first, then disk
                p = _MF_PRICE_CACHE.get(lookup)
                if p is None:
                    p = cache_get_live(f'px_{lookup}', _TTL_LIVE)
                    if p is not None:
                        p = float(p)
                        _MF_PRICE_CACHE[lookup] = p
                if p:
                    price_data[ticker] = p
        # Render with whatever prices we have — missing ones show '…'
        self._render_all(price_data, {}, placeholder=True)
        if self._portfolios:
            n_pos = sum(
                1 for port in self._portfolios.values()
                for item in port.get('holdings', {}).values()
                if not item.get('_is_cash')
            )
            n_priced = len(price_data)
            if n_priced == 0:
                self._sheet_ind.configure(text='✓ Sheet (loading prices…)', fg='#D29922')
            else:
                self._sheet_ind.configure(text=f'✓ Sheet ({n_priced}/{n_pos} prices cached)', fg=self.GREEN)
        if price_data:
            self._set_indicator(sheet=True)

    def _fetch_live_prices(self):
        threading.Thread(target=self._price_worker, daemon=True).start()

    def _price_worker(self):
        import yfinance as _yf
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # ── FX: one rate per non-MYR currency actually held ───────────────
        # Only fetch what the ledger needs — a MYR-only portfolio shouldn't
        # be pulling GBP rates.
        held_ccys = {_port_ccy(pk) for pk in (self._portfolios or {})}
        need_fx   = sorted(c for c in held_ccys if c != 'MYR')
        self._fx_rates = {'MYR': 1.0}

        def _fx_one(ccy):
            key = f'fx_{ccy.lower()}myr'
            cached = cache_get_live(key, _TTL_LIVE)
            if cached is not None:
                return ccy, float(cached)
            try:
                t  = _yf.Ticker(f'{ccy}MYR=X')
                fi = t.fast_info
                r  = getattr(fi, 'last_price', None) or getattr(fi, 'lastPrice', None)
                if not r:
                    h = t.history(period='2d')
                    r = float(h['Close'].iloc[-1]) if not h.empty else None
                if r:
                    cache_set_live(key, float(r))
                    return ccy, float(r)
            except Exception:
                pass
            return ccy, None

        try:
            if need_fx:
                with ThreadPoolExecutor(max_workers=min(4, len(need_fx))) as ex:
                    for fut in as_completed([ex.submit(_fx_one, c) for c in need_fx]):
                        ccy, rate = fut.result()
                        if rate:
                            self._fx_rates[ccy] = rate
            fx_ok = all(c in self._fx_rates for c in need_fx)
        except Exception:
            fx_ok = False
        # Back-compat: plenty of call sites still read _usdmyr
        self._usdmyr = self._fx_rates.get('USD')

        # Collect unique tickers across all portfolios
        tickers_to_fetch = {}   # ticker -> market
        for port in self._portfolios.values():
            for ticker, item in port.get('holdings', {}).items():
                if not item.get('_is_cash') and ticker not in tickers_to_fetch:
                    tickers_to_fetch[ticker] = item.get('market', 'MY')

        # ── OPTIMIZED STRATEGY ────────────────────────────────────────────────
        # SCA's holdings table only needs:
        #   • Live price      → fast_info (instant, already cached by _mf_get_price)
        #   • Day Δ           → last 2 closing prices → period="5d" is enough
        #   • 1Y TREND spark  → last ~20 trading days  → period="3mo" is enough
        # Full history (period="max") is only needed for:
        #   • Portfolio value chart   (lazy, drawn after initial render)
        #   • Analytics Line Chart    (lazy, on tab click)
        #   • Position Detail popup   (on-demand, when you click a row)
        # So the initial load fetches period="3mo" for all tickers in PARALLEL,
        # which is ~10× faster than serial period="max" downloads.
        # Full history is fetched on demand and cached in _MF_HIST_PRICE_CACHE.

        price_data   = {}
        history_data = {}

        def _fetch_one(ticker, mkt):
            """Fetch 3mo price + dividend data for one holding.
            Cache hierarchy (fastest to slowest):
              1. In-memory _MF_PRICE_CACHE / _MF_DIV_CACHE   — instant
              2. Disk: live/px_<T>.json (5-min TTL)            — instant
              3. Disk: prices/<T>.parquet (1-day TTL)           — instant
              4. Disk: divs/<T>.parquet  (7-day TTL)            — instant
              5. Network: yf.history(3mo, actions=True)         — slow, last resort
            Only reaches the network if both price AND dividend caches are stale.
            """
            lookup = f'{ticker}.KL' if (mkt == 'MY' and not ticker.endswith('.KL')) else ticker

            # ── Price: check memory → live-price disk cache → history disk cache ──
            p = _MF_PRICE_CACHE.get(lookup)
            if p is None:
                p = cache_get_live(f'px_{lookup}', _TTL_LIVE)
                if p is not None:
                    p = float(p)
                    _MF_PRICE_CACHE[lookup] = p

            # ── History: check memory → disk parquet ──────────────────────────────
            close_series = None
            if lookup in _MF_HIST_PRICE_CACHE:
                close_series = _MF_HIST_PRICE_CACHE[lookup]
            else:
                disk_prices = cache_get_prices(lookup)
                if disk_prices is not None:
                    close_series = disk_prices
                    _MF_HIST_PRICE_CACHE[lookup] = close_series

            # ── Dividends: check memory → disk parquet ────────────────────────────
            div_series = None
            if lookup in _MF_DIV_CACHE:
                div_series = _MF_DIV_CACHE[lookup]
            else:
                disk_divs = cache_get_divs(lookup)
                if disk_divs is not None:
                    div_series = disk_divs
                    _MF_DIV_CACHE[lookup] = div_series

            # ── If we have fresh price AND fresh 3mo+ history, skip network ────────
            need_price  = p is None
            need_hist   = close_series is None or len(close_series) < 2
            need_divs   = div_series is None

            if not need_price and not need_hist and not need_divs:
                # Everything cached — return instantly with no network call
                tail = close_series.iloc[-65:] if close_series is not None else pd.Series(dtype=float)
                return ticker, p, tail

            # ── Network fetch: only if something is stale ─────────────────────────
            try:
                obj  = _yf.Ticker(lookup)
                hist = obj.history(period='3mo', interval='1d',
                                   auto_adjust=True, actions=True)

                if hist is not None and not hist.empty:
                    # Price
                    if need_price and 'Close' in hist:
                        p = float(hist['Close'].dropna().iloc[-1])
                        _MF_PRICE_CACHE[lookup] = p
                        cache_set_live(f'px_{lookup}', p)

                    # History (3mo close series)
                    if 'Close' in hist:
                        c = hist['Close'].dropna()
                        c.index = pd.to_datetime(c.index).tz_localize(None).normalize()
                        if close_series is None:
                            close_series = c
                            _MF_HIST_PRICE_CACHE[lookup] = c
                            cache_set_prices(lookup, c)   # persist full 3mo to disk
                        else:
                            # Merge new data onto existing (extend without full replace)
                            merged = pd.concat([close_series, c]).sort_index()
                            merged = merged[~merged.index.duplicated(keep='last')]
                            _MF_HIST_PRICE_CACHE[lookup] = merged
                            cache_set_prices(lookup, merged)
                            close_series = merged

                    # Dividends — persist to disk so next session skips this fetch
                    if need_divs and 'Dividends' in hist:
                        d = hist['Dividends']
                        d = d[d > 0].copy()
                        if not d.empty:
                            d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
                            _MF_DIV_CACHE[lookup] = d
                            cache_set_divs(lookup, d)   # persist to disk (7-day TTL)
                            div_series = d

            except Exception:
                pass

            tail = close_series.iloc[-65:] if close_series is not None and len(close_series) >= 2 \
                   else pd.Series(dtype=float)
            return ticker, p, tail

        # Parallel fetch — all tickers simultaneously
        n_cached_hits = 0
        with ThreadPoolExecutor(max_workers=min(12, len(tickers_to_fetch) or 1)) as ex:
            futures = {ex.submit(_fetch_one, t, m): t
                       for t, m in tickers_to_fetch.items()}
            for fut in as_completed(futures):
                try:
                    ticker, p, hist = fut.result()
                    if p:
                        price_data[ticker] = p
                    if hist is not None and len(hist) >= 2:
                        history_data[ticker] = hist
                except Exception:
                    pass

        self.after(0, lambda: self._on_prices_loaded(price_data, history_data, fx_ok))

    def _on_prices_loaded(self, price_data, history_data, fx_ok):
        self._fx_ok = fx_ok
        self._set_indicator(fx=fx_ok)
        self._loading = False
        self._last_price_data = price_data
        self._last_hist_data  = history_data
        self._render_all(price_data, history_data)
        self._redraw_portfolio_chart()
        self._an_redraw()
        self._refresh_today_activity()
        self._draw_pie()          # no-op while collapsed
        self._update_time()

    # ─────────────────────────────────────────────────────────────────────────
    # RENDERING
    # ─────────────────────────────────────────────────────────────────────────
    def _render_all(self, price_data, history_data, placeholder=False):
        """Render holdings table and summary cards.
        placeholder=True: show '…' for live-price cells instead of N/A,
        signalling that prices are loading in the background rather than absent.
        """
        tab  = self._tab_var.get()
        view = self._view_var.get()
        pend = '…' if placeholder else 'N/A'   # pending vs truly unavailable

        # Update every currency card that exists
        for _ccy, _card in (self._ccy_cards or {}).items():
            _key = _NEWPOR_CCY_MAP.get(_ccy, ('',))[0]
            self._update_card(_card, self._portfolios.get(_key, {}), _ccy,
                              price_data,
                              (self._fx_rates or {}).get(_ccy),
                              placeholder=placeholder)

        # Show only the cards the view asks for
        _want = None if view == 'BOTH' else _MARKET_TO_CCY.get(view)
        for _ccy, _card in (self._ccy_cards or {}).items():
            if _want is None or _ccy == _want:
                _card.grid()
            else:
                _card.grid_remove()

        # Populate holdings table
        for row in self._hold_tree.get_children():
            self._hold_tree.delete(row)

        all_rows = []
        ports_to_show = {}
        for _ccy in ('MYR', 'USD', 'HKD', 'SGD'):
            _key = _NEWPOR_CCY_MAP.get(_ccy, ('',))[0]
            if _key not in (self._portfolios or {}):
                continue
            if tab != 'ALL' and tab != MARKET_SPEC[_ccy]['market']:
                continue
            ports_to_show[MARKET_SPEC[_ccy]['label']] = (
                _key, _ccy, self.CCY_ACCENT.get(_ccy, self.TEAL))

        total_positions = 0
        for port_label, (port_key, ccy, accent) in ports_to_show.items():
            port = self._portfolios.get(port_key, {})
            holdings = port.get('holdings', {})
            port_cost  = sum(h['cost_value'] for h in holdings.values() if not h.get('_is_cash'))
            port_total_mv = 0.0
            for ticker, item in sorted(holdings.items()):
                if item.get('_is_cash'):
                    continue
                shares   = item['shares']
                avg_cost = item['avg_cost']
                cost_v   = item['cost_value']
                price    = price_data.get(ticker)
                mv       = shares * price if price else None
                pl       = (mv - cost_v) if mv is not None else None
                pl_pct   = (pl / cost_v * 100) if pl is not None and cost_v else None
                day_chg  = None
                if ticker in history_data:
                    h = history_data[ticker]
                    if len(h) >= 2:
                        day_chg = (h.iloc[-1] / h.iloc[-2] - 1) * 100
                weight = (mv / port_cost * 100) if mv and port_cost else (
                    # Cost-weight fallback when live price not yet available — local data
                    (cost_v / port_cost * 100) if placeholder and port_cost else None
                )
                if mv:
                    port_total_mv += mv
                trend = self._spark_text(history_data.get(ticker))
                tag_mv = ('gain' if pl and pl >= 0 else 'loss') if pl is not None else 'neutral'
                all_rows.append((ticker, port_label, ccy, shares, avg_cost, price,
                                 day_chg, mv, pl, pl_pct, weight, trend, tag_mv, accent))
                total_positions += 1

        for (ticker, port_label, ccy, shares, avg_cost, price, day_chg,
             mv, pl, pl_pct, weight, trend, tag_mv, accent) in all_rows:
            ccy_prefix = ccy_symbol(ccy)
            # Whole-lot markets (MY/HK/SG) trim trailing zeros; fractional
            # US holdings keep 4dp so 0.0131 doesn't collapse to 0.013.
            qty_str = f'{shares:,.4f}' if ccy == 'USD' else _fmt_units(shares)
            self._hold_tree.insert('', 'end', tags=(tag_mv,),
                values=(ticker, port_label, ccy,
                        qty_str,
                        f'{ccy_prefix}{avg_cost:,.4f}',
                        f'{ccy_prefix}{price:,.4f}' if price else pend,
                        f'{day_chg:+.2f}%' if day_chg is not None else '—',
                        f'{ccy_prefix}{mv:,.2f}' if mv else pend,
                        f'{ccy_prefix}{pl:+,.2f}' if pl is not None else pend,
                        f'{pl_pct:+.2f}%' if pl_pct is not None else pend,
                        f'{weight:.2f}%' if weight is not None else '—',
                        trend))

        self._pos_total_var.set(f'{total_positions} position{"s" if total_positions != 1 else ""}')
        self._reapply_sort()          # keep the user's chosen order across refreshes
        self._render_weight_bars(ports_to_show, price_data)

    def _update_card(self, card, port, ccy, price_data, usdmyr,
                     placeholder=False):
        """Update a portfolio summary card.
        placeholder=True: show '…' for value/P/L (not yet fetched) while
        still showing Cost and N positions which are local/instant.
        """
        prefix   = 'RM' if ccy == 'MYR' else '$'
        pend     = '…' if placeholder else f'{prefix}0.00'
        holdings = port.get('holdings', {})
        cost_total = sum(h['cost_value'] for h in holdings.values()
                         if not h.get('_is_cash'))
        mv_total = 0.0
        n_pos    = 0
        for ticker, item in holdings.items():
            if item.get('_is_cash'):
                continue
            p = price_data.get(ticker)
            if p:
                mv_total += item['shares'] * p
            n_pos += 1

        rows = card._rows

        # Cost is always available from the local ledger — show it immediately
        rows['Cost'][0].set(f'{prefix}{cost_total:,.2f}')

        if mv_total:
            pl      = mv_total - cost_total
            pl_pct  = (pl / cost_total * 100) if cost_total else 0.0
            pl_color = self.GREEN if pl >= 0 else self.RED
            rows['Value'][0].set(f'{prefix}{mv_total:,.2f}')
            rows['P/L'][0].set(f'{prefix}{pl:+,.2f}')
            rows['P/L'][1].configure(fg=pl_color)
            rows['Return'][0].set(f'{pl_pct:+.2f}%')
            rows['Return'][1].configure(fg=pl_color)
        else:
            # Live prices not available yet
            rows['Value'][0].set(pend)
            rows['P/L'][0].set(pend)
            rows['P/L'][1].configure(fg=self.DIM if placeholder else self.FG)
            rows['Return'][0].set(pend)
            rows['Return'][1].configure(fg=self.DIM if placeholder else self.FG)

        card._pos_var.set(f'{n_pos} position{"s" if n_pos != 1 else ""}')

    def _render_weight_bars(self, ports_to_show, price_data):
        for w in self._weight_bar_frame.winfo_children():
            w.destroy()
        for port_label, (port_key, ccy, accent) in ports_to_show.items():
            port = self._portfolios.get(port_key, {})
            holdings = port.get('holdings', {})
            mv_map = {}
            total_mv = 0.0
            for ticker, item in holdings.items():
                if item.get('_is_cash'):
                    continue
                p = price_data.get(ticker)
                if p:
                    mv = item['shares'] * p
                    mv_map[ticker] = mv
                    total_mv += mv
            if not total_mv:
                continue
            grp = tk.Frame(self._weight_bar_frame, bg=self.PANEL)
            grp.pack(fill='x', pady=2)
            tk.Label(grp, text=port_label, font=self.FONT, fg=accent, bg=self.PANEL,
                     width=20, anchor='w').pack(side='left')
            bar_outer = tk.Frame(grp, bg='#1a2035', height=16)
            bar_outer.pack(side='left', fill='x', expand=True, padx=(4, 0))
            bar_outer.pack_propagate(False)
            for ticker, mv in sorted(mv_map.items(), key=lambda x: -x[1])[:8]:
                pct = mv / total_mv
                seg = tk.Frame(bar_outer, bg=accent, width=int(pct * 900), height=16)
                seg.pack(side='left', fill='y')
                if pct > 0.06:
                    short = ticker.replace('.KL', '')[:6]
                    tk.Label(seg, text=short, font=('Courier New', 7),
                             fg='#000', bg=accent).place(relx=0.5, rely=0.5, anchor='center')

    def _spark_text(self, hist_series):
        """Tiny ASCII sparkline for the 1Y trend column."""
        if hist_series is None or len(hist_series) < 5:
            return '—'
        try:
            vals = hist_series.iloc[-20:].values   # last ~20 trading days
            lo, hi = vals.min(), vals.max()
            if hi == lo:
                return '────'
            chars = '▁▂▃▄▅▆▇█'
            spark = ''
            for v in vals[::4]:   # sample every 4th point -> ~5 chars
                idx = int((v - lo) / (hi - lo) * (len(chars) - 1))
                spark += chars[idx]
            last_ret = (vals[-1] / vals[0] - 1) * 100
            return spark + f' {last_ret:+.1f}%'
        except Exception:
            return '—'

    # ─────────────────────────────────────────────────────────────────────────
    # INTERACTION
    # ─────────────────────────────────────────────────────────────────────────
    def _on_view_change(self):
        price_data = {ticker: _mf_get_price(ticker, item.get('market', 'MY'))
                      for port in self._portfolios.values()
                      for ticker, item in port.get('holdings', {}).items()
                      if not item.get('_is_cash')}
        price_data = {k: v for k, v in price_data.items() if v}
        self._render_all(price_data, {})

    def _on_tab_change(self):
        self._on_view_change()
        self._draw_pie()          # no-op while collapsed

    def _on_row_click(self, event):
        sel = self._hold_tree.selection()
        if not sel:
            return
        vals = self._hold_tree.item(sel[0], 'values')
        if not vals:
            return
        ticker = vals[0]
        if ticker == self._selected_ticker:
            self._selected_ticker = None
            return
        self._selected_ticker = ticker
        self._open_stock_chart(ticker)

    def _open_stock_chart(self, ticker):
        """Position detail popup: ledger (all buy/sell entries for this stock)
        on top, price chart from buy-in date below — a complete per-holding
        account book as requested."""
        import datetime as _dt
        import pandas as pd

        # Find the stock across all portfolios
        item_info = None
        port_key_found = ''
        for port_key, port in self._portfolios.items():
            if ticker in port.get('holdings', {}):
                item_info = port['holdings'][ticker]
                port_key_found = port_key
                break

        if item_info is None:
            return

        txns     = item_info.get('transactions', [])
        ccy      = item_info.get('currency', 'MYR')
        mkt      = item_info.get('market', 'MY')
        prefix   = 'RM' if ccy == 'MYR' else '$'
        shares   = item_info.get('shares', 0.0)
        avg_cost = item_info.get('avg_cost', 0.0)
        cost_val = item_info.get('cost_value', 0.0)

        # Earliest buy-in date
        buy_dates = []
        for txn in txns:
            try: buy_dates.append(_dt.datetime.strptime(txn[1], '%m/%d/%Y').date())
            except ValueError: pass
        buy_in_date = min(buy_dates) if buy_dates else None

        # ── Popup window ──────────────────────────────────────────────────────
        pop = tk.Toplevel(self)
        pop.title(f'{ticker}  —  Position Detail')
        pop.configure(bg=self.BG)
        pop.geometry('920x780')
        pop.minsize(760, 500)

        # ── Fixed header (always visible, not scrolled) ───────────────────────
        hdr = tk.Frame(pop, bg=self.BG)
        hdr.pack(fill='x', padx=14, pady=(10, 4))
        tk.Label(hdr, text=ticker, font=('Courier New', 13, 'bold'),
                 fg=self.TEAL, bg=self.BG).pack(side='left')
        live_p = self._last_price_data.get(ticker)
        pl_now = (live_p - avg_cost) * shares if live_p else None
        pl_pct = (pl_now / cost_val * 100) if pl_now is not None and cost_val else None
        pl_col = self.GREEN if pl_now and pl_now >= 0 else self.RED
        info_parts = [
            f'{_port_display_name(port_key_found)}', f'{ccy}',
            f'Held: {shares:,.4f} units',
            f'Avg cost: {prefix}{avg_cost:,.4f}',
        ]
        if live_p:
            info_parts += [f'Live: {prefix}{live_p:,.4f}',
                           f'P/L: {prefix}{pl_now:+,.2f} ({pl_pct:+.2f}%)']
        tk.Label(hdr, text='  ·  '.join(info_parts),
                 font=('Courier New', 8), fg=self.FG2,
                 bg=self.BG).pack(side='left', padx=(10, 0))

        # ── Scrollable body — all sections below the header go in here ────────
        _outer = tk.Frame(pop, bg=self.BG)
        _outer.pack(fill='both', expand=True)
        _cv = tk.Canvas(_outer, bg=self.BG, highlightthickness=0)
        _vsb = ttk.Scrollbar(_outer, orient='vertical', command=_cv.yview)
        _cv.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side='right', fill='y')
        _cv.pack(side='left', fill='both', expand=True)
        body = tk.Frame(_cv, bg=self.BG)
        _cv_win = _cv.create_window((0, 0), window=body, anchor='nw')

        def _on_body_configure(e):
            _cv.configure(scrollregion=_cv.bbox('all'))
            # Keep body width matching the canvas width
            _cv.itemconfig(_cv_win, width=_cv.winfo_width())
        body.bind('<Configure>', _on_body_configure)
        _cv.bind('<Configure>', lambda e: _cv.itemconfig(_cv_win, width=e.width))

        # MouseWheel scrolling — bind on the canvas and propagate from child widgets
        def _on_mousewheel(e):
            _cv.yview_scroll(int(-e.delta / 120), 'units')
        _cv.bind('<MouseWheel>', _on_mousewheel)
        body.bind('<MouseWheel>', _on_mousewheel)
        # Windows uses <MouseWheel>, Linux uses <Button-4>/<Button-5>
        _cv.bind('<Button-4>', lambda e: _cv.yview_scroll(-1, 'units'))
        _cv.bind('<Button-5>', lambda e: _cv.yview_scroll( 1, 'units'))

        # ── LEDGER section ────────────────────────────────────────────────────
        lbl_frame = tk.Frame(body, bg=self.BG)
        lbl_frame.pack(fill='x', padx=14, pady=(6, 2))
        tk.Label(lbl_frame, text='📒  TRANSACTION LEDGER',
                 font=('Courier New', 9, 'bold'), fg=self.TEAL,
                 bg=self.BG).pack(side='left')

        ledger_frame = tk.Frame(body, bg=self.BG)
        ledger_frame.pack(fill='x', padx=14, pady=(0, 4))

        l_cols = ('Date', 'Type', 'Units', 'Price', 'Amount',
                  'Running Units', 'Running Cost', 'Avg Cost After')
        l_tree = ttk.Treeview(ledger_frame, columns=l_cols,
                               show='headings', style='SCA.Treeview', height=6)
        l_cw = {'Date': 88, 'Type': 56, 'Units': 88, 'Price': 90,
                'Amount': 95, 'Running Units': 105,
                'Running Cost': 105, 'Avg Cost After': 110}
        for col in l_cols:
            l_tree.heading(col, text=col)
            l_tree.column(col, width=l_cw.get(col, 85),
                          anchor='w' if col in ('Date', 'Type') else 'e',
                          stretch=False)
        lsb = ttk.Scrollbar(ledger_frame, orient='vertical', command=l_tree.yview)
        lhsb = ttk.Scrollbar(ledger_frame, orient='horizontal', command=l_tree.xview)
        l_tree.configure(yscrollcommand=lsb.set, xscrollcommand=lhsb.set)
        l_tree.grid(row=0, column=0, sticky='nsew')
        lsb.grid(row=0, column=1, sticky='ns')
        lhsb.grid(row=1, column=0, sticky='ew')
        ledger_frame.rowconfigure(0, weight=1); ledger_frame.columnconfigure(0, weight=1)
        l_tree.tag_configure('buy',  foreground='#58A6FF')
        l_tree.tag_configure('sell', foreground=self.RED)

        # Populate ledger — all buy/sell entries chronologically
        def _parse(s):
            try: return _dt.datetime.strptime(s, '%m/%d/%Y')
            except ValueError: return _dt.datetime.min

        sorted_txns = sorted(txns, key=lambda t: _parse(t[1]))
        run_units = 0.0; run_cost = 0.0; run_avg = 0.0
        total_bought = 0.0; total_sold = 0.0; total_realised = 0.0
        for txn in sorted_txns:
            ttype, date_s, unit, price, amount = txn
            if ttype == 'buy':
                new_units = run_units + abs(unit)
                if new_units > 0:
                    run_avg = (run_units * run_avg + abs(unit) * price) / new_units
                run_units = new_units
                run_cost += abs(amount)
                total_bought += abs(amount)
                tag = 'buy'
            elif ttype == 'sell':
                sold = abs(unit)
                cost_basis = run_avg * sold
                proceeds   = abs(amount)
                realised   = proceeds - cost_basis
                total_realised += realised
                run_units -= sold
                run_cost  -= cost_basis
                if run_units <= 1e-8:
                    run_units = 0.0; run_cost = 0.0; run_avg = 0.0
                total_sold += proceeds
                tag = 'sell'
            else:
                tag = 'buy'

            l_tree.insert('', 'end', tags=(tag,),
                values=(date_s, ttype.upper(),
                        f'{unit:+,.4f}',
                        f'{prefix}{price:,.4f}',
                        f'{prefix}{amount:+,.4f}',
                        _fmt_units(run_units),
                        f'{prefix}{run_cost:,.4f}',
                        f'{prefix}{run_avg:,.4f}'))

        # Summary strip below ledger
        summ = tk.Frame(body, bg='#0d1424')
        summ.pack(fill='x', padx=14, pady=(0, 4))
        summary_items = [
            ('Total Bought', f'{prefix}{total_bought:,.2f}'),
            ('Total Sold',   f'{prefix}{total_sold:,.2f}'),
            ('Realised P/L', f'{prefix}{total_realised:+,.2f}'),
            ('Current Units', _fmt_units(run_units)),
            ('Current Cost', f'{prefix}{run_cost:,.4f}'),
            ('Avg Cost',     f'{prefix}{run_avg:,.4f}'),
        ]
        for lbl_txt, val_txt in summary_items:
            col = self.DIM
            if 'P/L' in lbl_txt:
                col = self.GREEN if total_realised >= 0 else self.RED
            c = tk.Frame(summ, bg='#0d1424')
            c.pack(side='left', padx=12, pady=4)
            tk.Label(c, text=lbl_txt, font=('Courier New', 7),
                     fg=self.DIM, bg='#0d1424').pack()
            tk.Label(c, text=val_txt, font=('Courier New', 8, 'bold'),
                     fg=col, bg='#0d1424').pack()

        # ── PRICE CHART section ───────────────────────────────────────────────
        tk.Label(body, text='📈  PRICE CHART  (from buy-in date)',
                 font=('Courier New', 9, 'bold'), fg=self.TEAL,
                 bg=self.BG, anchor='w', padx=14).pack(fill='x', pady=(2, 0))

        stats_var = tk.StringVar(value='Loading price history…')
        tk.Label(body, textvariable=stats_var, font=('Courier New', 8),
                 fg=self.FG2, bg=self.BG, anchor='w', padx=14).pack(fill='x')

        chart_frame = tk.Frame(body, bg=self.BG)
        chart_frame.pack(fill='both', expand=True, padx=6, pady=(2, 8))
        fig = Figure(figsize=(11, 3.0), facecolor=self.BG)
        ax  = fig.add_subplot(111)
        ax.set_facecolor(self.BG)
        ax.tick_params(colors=self.DIM, labelsize=7)
        for sp in ax.spines.values(): sp.set_color(self.BORDER)
        ax.set_ylabel(f'Price ({ccy})', color=self.DIM, fontsize=8)
        fig.subplots_adjust(left=0.07, right=0.97, top=0.90, bottom=0.15)
        canvas = FigureCanvasTkAgg(fig, chart_frame)
        canvas.get_tk_widget().pack(fill='both', expand=True)

        def _draw_chart(hist):
            ax.cla()
            ax.set_facecolor(self.BG)
            ax.tick_params(colors=self.DIM, labelsize=7)
            for sp in ax.spines.values(): sp.set_color(self.BORDER)
            ax.set_ylabel(f'Price ({ccy})', color=self.DIM, fontsize=8)

            if hist is None or hist.empty:
                ax.text(0.5, 0.5, f'No price history for {ticker}',
                        ha='center', va='center', color=self.DIM,
                        fontsize=10, transform=ax.transAxes)
                canvas.draw(); return

            import matplotlib.dates as mdates
            plot_hist = hist
            if buy_in_date:
                mask = hist.index >= pd.Timestamp(buy_in_date)
                if mask.any():
                    plot_hist = hist[mask]

            ax.plot(plot_hist.index, plot_hist.values, color=self.TEAL,
                    linewidth=1.5, zorder=3)
            ax.fill_between(plot_hist.index, plot_hist.values, alpha=0.07,
                            color=self.TEAL)

            # Mark every buy and sell date on the chart
            for txn in sorted_txns:
                ttype, date_s, unit, price, amount = txn
                try:
                    d = _dt.datetime.strptime(date_s, '%m/%d/%Y').date()
                    ts = pd.Timestamp(d)
                    if ts in plot_hist.index or plot_hist.index.searchsorted(ts) < len(plot_hist):
                        pos = plot_hist.index.searchsorted(ts)
                        pos = min(pos, len(plot_hist) - 1)
                        y_val = plot_hist.iloc[pos]
                        marker_col = '#58A6FF' if ttype == 'buy' else self.RED
                        marker_sym = '^' if ttype == 'buy' else 'v'
                        ax.plot(plot_hist.index[pos], y_val, marker_sym,
                                color=marker_col, markersize=8, zorder=5)
                        ax.annotate(
                            f'{ttype[0].upper()} {prefix}{price:.3f}',
                            xy=(plot_hist.index[pos], y_val),
                            xytext=(0, 12 if ttype == 'buy' else -16),
                            textcoords='offset points',
                            ha='center', fontsize=6, color=marker_col, zorder=6)
                except (ValueError, IndexError):
                    pass

            # Avg cost horizontal line
            if avg_cost > 0:
                ax.axhline(avg_cost, color=self.YELLOW, linewidth=0.8,
                           linestyle=':', alpha=0.7,
                           label=f'Avg cost {prefix}{avg_cost:.4f}')

            ax.legend(facecolor='#161b22', edgecolor=self.BORDER,
                      labelcolor=self.FG, fontsize=7)
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
            fig.autofmt_xdate(rotation=30)

            current_price = plot_hist.iloc[-1] if not plot_hist.empty else None
            if current_price and avg_cost:
                chg_rm  = current_price - avg_cost
                chg_pct = (chg_rm / avg_cost * 100)
                stats_var.set(
                    f'{ticker}  ·  Buy-in: {buy_in_date or "N/A"}  '
                    f'·  Price: {prefix}{current_price:.4f}  '
                    f'·  vs Avg Cost: {prefix}{chg_rm:+.4f} ({chg_pct:+.2f}%)'
                )
            canvas.draw()

        def _fetch_and_draw():
            hist = _mf_get_price_history(ticker, mkt, start_date=buy_in_date)
            pop.after(0, lambda: _draw_chart(hist))

        if ticker in _MF_HIST_PRICE_CACHE:
            _draw_chart(_mf_get_price_history(ticker, mkt, start_date=buy_in_date))
        else:
            ax.text(0.5, 0.5, f'Fetching price history for {ticker}…',
                    ha='center', va='center', color=self.DIM,
                    fontsize=10, transform=ax.transAxes)
            canvas.draw()
            import threading as _threading
            _threading.Thread(target=_fetch_and_draw, daemon=True).start()

        # ── DIVIDEND ESTIMATOR section ────────────────────────────────────────
        # Formula: Estimated Div = (TTM_Div × 0.75) + (PAT × Policy_Rate) × 0.25
        # TTM_Div     = trailing 12-month dividends/share from yfinance
        # PAT         = EPS (profit after tax per share) from yfinance .info
        # Policy_Rate = manually set payout ratio % per ticker, persisted in paa_settings.json
        tk.Label(body, text='💰  DIVIDEND ESTIMATOR',
                 font=('Courier New', 9, 'bold'), fg=self.YELLOW,
                 bg=self.BG, anchor='w', padx=14).pack(fill='x', pady=(6, 0))

        div_panel = tk.Frame(body, bg='#0d1424', highlightbackground='#D29922',
                             highlightthickness=1)
        div_panel.pack(fill='x', padx=14, pady=(2, 8))

        # ── Row 1: inputs ────────────────────────────────────────────────────
        inp_row = tk.Frame(div_panel, bg='#0d1424')
        inp_row.pack(fill='x', padx=12, pady=(8, 4))

        # Load persisted policy rate for this ticker
        _settings     = load_app_settings()
        _div_rates    = _settings.get('div_policy_rates', {})
        _default_rate = _div_rates.get(ticker, 40.0)   # default 40%

        for lbl_txt in ('TTM Div/Share', 'PAT (EPS)', 'Policy Rate %'):
            c = tk.Frame(inp_row, bg='#0d1424')
            c.pack(side='left', padx=(0, 20))
            tk.Label(c, text=lbl_txt, font=('Courier New', 7),
                     fg=self.DIM, bg='#0d1424').pack(anchor='w')

        # Actual entry widgets — rebuild cleanly
        for w in inp_row.winfo_children(): w.destroy()

        ttm_var    = tk.StringVar(value='Fetching…')
        pat_var    = tk.StringVar(value='Fetching…')
        policy_var = tk.StringVar(value=f'{_default_rate:.1f}')

        for lbl_txt, var, editable in [
            ('TTM Div/Share', ttm_var, False),
            ('PAT / EPS',     pat_var, False),
            ('Policy Rate %', policy_var, True),
        ]:
            c = tk.Frame(inp_row, bg='#0d1424')
            c.pack(side='left', padx=(0, 20))
            tk.Label(c, text=lbl_txt, font=('Courier New', 7),
                     fg=self.DIM, bg='#0d1424').pack(anchor='w')
            if editable:
                e = tk.Entry(c, textvariable=var, font=('Courier New', 9, 'bold'),
                             bg='#1a2035', fg=self.YELLOW, insertbackground=self.YELLOW,
                             relief='flat', width=8)
                e.pack(anchor='w')
            else:
                tk.Label(c, textvariable=var, font=('Courier New', 9, 'bold'),
                         fg=self.FG, bg='#0d1424').pack(anchor='w')

        # ── Row 2: result ─────────────────────────────────────────────────────
        res_row = tk.Frame(div_panel, bg='#0d1424')
        res_row.pack(fill='x', padx=12, pady=(0, 4))

        est_dps_var   = tk.StringVar(value='—')
        est_total_var = tk.StringVar(value='—')
        est_yld_var   = tk.StringVar(value='—')

        for lbl_txt, var, col in [
            ('Est. Div/Share',     est_dps_var,   self.YELLOW),
            (f'Est. Annual Income ({shares:,.4f} units)', est_total_var, self.GREEN),
            ('Est. Yield (vs avg cost)', est_yld_var, self.TEAL),
        ]:
            c = tk.Frame(res_row, bg='#0d1424')
            c.pack(side='left', padx=(0, 24))
            tk.Label(c, text=lbl_txt, font=('Courier New', 7),
                     fg=self.DIM, bg='#0d1424').pack(anchor='w')
            tk.Label(c, textvariable=var, font=('Courier New', 11, 'bold'),
                     fg=col, bg='#0d1424').pack(anchor='w')

        formula_var = tk.StringVar(value='Formula: (TTM_Div × 0.75) + (PAT × Policy_Rate%) × 0.25')
        tk.Label(div_panel, textvariable=formula_var, font=('Courier New', 7),
                 fg=self.DIM, bg='#0d1424', anchor='w', padx=12).pack(fill='x', pady=(0, 6))

        def _calc_dividend(ttm_div, pat, policy_rate_pct):
            """Core formula: (TTM_Div × 0.75) + (PAT × PolicyRate) × 0.25"""
            try:
                p = float(policy_rate_pct) / 100.0
                est = (ttm_div * 0.75) + (pat * p) * 0.25
                return round(est, 4)
            except Exception:
                return None

        def _update_result(*_):
            try:
                ttm  = float(ttm_var.get())
                pat  = float(pat_var.get())
                rate = float(policy_var.get())
            except ValueError:
                est_dps_var.set('—')
                est_total_var.set('—')
                est_yld_var.set('—')
                return
            est_dps = _calc_dividend(ttm, pat, rate)
            if est_dps is None:
                est_dps_var.set('Error')
                return
            est_income = est_dps * shares
            est_yield  = (est_dps / avg_cost * 100) if avg_cost else 0.0
            est_dps_var.set(f'{prefix}{est_dps:.4f}')
            est_total_var.set(f'{prefix}{est_income:.4f}')
            est_yld_var.set(f'{est_yield:.2f}%')
            formula_var.set(
                f'({prefix}{ttm:.4f} × 0.75) + ({prefix}{pat:.4f} × {rate:.1f}%) × 0.25'
                f'  =  {prefix}{est_dps:.4f}/share'
            )
            # Persist the policy rate whenever the user changes it
            s = load_app_settings()
            dr = s.get('div_policy_rates', {})
            dr[ticker] = rate
            s['div_policy_rates'] = dr
            save_app_settings(s)

        policy_var.trace_add('write', _update_result)

        def _fetch_div_data():
            """Fetch TTM dividends and EPS in parallel — two independent
            network calls that previously ran sequentially are now fired at
            the same time, halving the wait. Also uses fast_info for EPS
            before falling back to the slower .info dict."""
            import yfinance as _yf
            import datetime as _dt
            from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac

            # Use market-aware symbol — MY stocks get .KL, US stocks stay as-is
            yf_symbol = _mf_ticker_for_market(ticker, mkt)
            yf_t = _yf.Ticker(yf_symbol)

            ttm_div    = 0.0
            div_history = []
            pat         = 0.0
            errors      = []

            def _get_dividends():
                try:
                    yf_sym = _mf_ticker_for_market(ticker, mkt)
                    divs   = None

                    # 1. In-memory — populated by preload at startup or prefetch
                    if yf_sym in _MF_DIV_CACHE:
                        divs = _MF_DIV_CACHE[yf_sym]

                    # 2. Disk parquet (7-day TTL)
                    if divs is None:
                        divs = cache_get_divs(yf_sym)
                        if divs is not None:
                            _MF_DIV_CACHE[yf_sym] = divs   # promote to memory

                    # 3. Live fetch — only if both caches missed
                    if divs is None:
                        raw = yf_t.dividends
                        if raw is not None and not raw.empty:
                            if raw.index.tz is not None:
                                raw.index = raw.index.tz_localize(None)
                            divs = raw
                            _MF_DIV_CACHE[yf_sym] = divs
                            cache_set_divs(yf_sym, divs)

                    if divs is None or divs.empty:
                        return 0.0, []

                    cutoff = pd.Timestamp.now(tz='UTC') - pd.DateOffset(years=1)
                    idx = divs.index
                    if idx.tz is None:
                        idx = idx.tz_localize('UTC')
                    ttm_divs = divs[idx >= cutoff]
                    history  = []
                    for ex_dt, dps in ttm_divs.items():
                        ex_date = ex_dt.tz_localize(None) if ex_dt.tzinfo is not None else ex_dt
                        history.append((ex_date.date(), float(dps)))
                    return float(ttm_divs.sum()), history
                except Exception:
                    return 0.0, []

            def _get_eps():
                try:
                    yf_sym = _mf_ticker_for_market(ticker, mkt)
                    # 1. Disk cache (24-hr TTL, written by prefetch)
                    cached_eps = cache_get_live(f'eps_{yf_sym}', _TTL_EPS)
                    if cached_eps is not None:
                        return float(cached_eps)
                    # 2. Live fetch
                    fi = yf_t.fast_info
                    for attr in ('trailing_eps', 'trailingEps'):
                        v = getattr(fi, attr, None)
                        if v is not None:
                            cache_set_live(f'eps_{yf_sym}', float(v))
                            return float(v)
                    info = yf_t.info
                    v = float(info.get('trailingEps') or
                              info.get('epsTrailingTwelveMonths') or 0.0)
                    cache_set_live(f'eps_{yf_sym}', v)
                    return v
                except Exception:
                    return 0.0


            # Fire both in parallel
            with _TPE(max_workers=2) as ex:
                f_div = ex.submit(_get_dividends)
                f_eps = ex.submit(_get_eps)
                ttm_div, div_history = f_div.result()
                pat                  = f_eps.result()

            pop.after(0, lambda: _apply_div_data(ttm_div, pat, div_history))

        def _apply_div_data(ttm_div, pat, div_history):
            ttm_var.set(f'{ttm_div:.4f}')
            pat_var.set(f'{pat:.4f}')
            _update_result()
            _build_actual_table(div_history)

        def _apply_div_error(msg):
            ttm_var.set('N/A')
            pat_var.set('N/A')
            formula_var.set(f'Could not fetch data: {msg[:60]}')

        # ── Actual 1-Year Dividend History ───────────────────────────────────
        # Hypothetical "buy and hold" note: if you'd held your CURRENT units
        # for the entire past 12 months, here's what you would have collected
        # at each real dividend payment. Simple: current_units × each real dps.
        import datetime as _dt
        tk.Label(div_panel,
                 text=f'📋  IF HELD {shares:,.4f} UNITS ALL YEAR  —  actual payments × current position (hypothetical)',
                 font=('Courier New', 7, 'bold'), fg=self.DIM,
                 bg='#0d1424', anchor='w', padx=12).pack(fill='x', pady=(4, 0))

        hist_frame = tk.Frame(div_panel, bg='#0d1424')
        hist_frame.pack(fill='x', padx=12, pady=(2, 4))

        hist_cols = ('Ex-Date', 'Div/Share', 'Units (current)', 'Would Receive')
        hist_tree = ttk.Treeview(hist_frame, columns=hist_cols,
                                  show='headings', style='SCA.Treeview', height=5)
        hist_cw = {'Ex-Date': 100, 'Div/Share': 100,
                   'Units (current)': 120, 'Would Receive': 110}
        for col in hist_cols:
            hist_tree.heading(col, text=col)
            hist_tree.column(col, width=hist_cw.get(col, 90), anchor='e', stretch=False)
        hist_tree.tag_configure('payment', foreground=self.GREEN)
        hist_tree.pack(fill='x')

        actual_total_var = tk.StringVar(value='—')
        tk.Label(div_panel, textvariable=actual_total_var,
                 font=('Courier New', 8, 'bold'), fg=self.GREEN,
                 bg='#0d1424', anchor='w', padx=12).pack(fill='x', pady=(2, 4))

        def _build_actual_table(div_history):
            for row in hist_tree.get_children():
                hist_tree.delete(row)
            if not div_history:
                hist_tree.insert('', 'end', values=('—', '—', _fmt_units(shares), '—'))
                actual_total_var.set('⚠  No dividends paid in the last 12 months.')
                return
            total = 0.0
            for ex_date, dps in sorted(div_history):
                would = shares * dps
                total += would
                hist_tree.insert('', 'end', tags=('payment',),
                    values=(ex_date.strftime('%d/%m/%Y'),
                            f'{prefix}{dps:.4f}',
                            _fmt_units(shares),
                            f'{prefix}{would:.4f}'))
            n = len(div_history)
            yoc = (total / cost_val * 100) if cost_val else 0.0
            actual_total_var.set(
                f'📌  {n} payment{"s" if n != 1 else ""}  ·  '
                f'{prefix}{total:.4f} total  '
                f'·  Yield on cost: {yoc:.2f}%  '
                f'(hypothetical — assumes {shares:,.4f} units held all 12 months)'
            )


        tk.Button(div_panel,
                  text='↻  Recalculate',
                  font=('Courier New', 8, 'bold'),
                  bg='#1a2035', fg=self.YELLOW, relief='flat',
                  padx=10, pady=3, cursor='hand2',
                  command=_update_result).pack(anchor='e', padx=12, pady=(0, 6))

        # Kick off background fetch
        _threading.Thread(target=_fetch_div_data, daemon=True).start()

    def _open_add_dialog(self):
        """Placeholder — manually add a transaction."""
        d = tk.Toplevel(self)
        d.title('Add Transaction')
        d.configure(bg=self.BG)
        d.geometry('380x200')
        d.resizable(False, False)
        tk.Label(d, text='Manual transaction entry\nwill be added in a future update.',
                 font=self.FONT, fg=self.FG2, bg=self.BG,
                 justify='center').pack(expand=True)
        tk.Button(d, text='Close', font=self.FONT_B, bg=self.PANEL, fg=self.FG,
                  relief='flat', padx=16, pady=6, cursor='hand2',
                  command=d.destroy).pack(pady=(0, 16))

    # ─────────────────────────────────────────────────────────────────────────
    # INDICATORS / STATUS HELPERS
    # ─────────────────────────────────────────────────────────────────────────
    def _set_indicator(self, sheet=None, fx=None):
        if sheet is True:
            self._sheet_ind.configure(text='✓ Sheet', fg=self.GREEN)
        elif sheet is False:
            self._sheet_ind.configure(text='✕ Sheet error', fg=self.RED)
        else:
            self._sheet_ind.configure(text='● Sheet', fg=self.DIM)

        if fx is True:
            # Show every rate in play — KRW/HKD are small numbers, so give the
            # sub-unit ones more precision than the 4dp that suits USD.
            parts = []
            for c, r in sorted((self._fx_rates or {}).items()):
                if c == 'MYR':
                    continue
                parts.append(f'{c} {r:.4f}' if r >= 0.1 else f'{c} {r:.6f}')
            self._fx_ind.configure(
                text='✓ ' + ' · '.join(parts) if parts else '✓ MYR only',
                fg=self.GREEN)
        elif fx is False:
            self._fx_ind.configure(text='✕ FX error', fg=self.RED)
        else:
            self._fx_ind.configure(text='● FX', fg=self.DIM)

    def _show_banner(self, msg, sub=''):
        self._banner_msg.configure(text=msg)
        self._banner_sub.configure(text=sub)
        self._banner_frame.pack(fill='x', padx=14, pady=(0, 6), before=self._my_card.master)

    def _hide_banner(self):
        self._banner_frame.pack_forget()

    def _update_time(self):
        from datetime import datetime as _DT
        self._time_var.set(_DT.now().strftime('%I:%M:%S %p'))

    def _schedule_auto(self):
        if self._auto_after:
            self.after_cancel(self._auto_after)
        self._auto_after = self.after(self._auto_secs * 1000, self._auto_tick)

    def _auto_tick(self):
        self._refresh()
        self._schedule_auto()


# =============================================================================
class SplashScreen(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('MYIPO+ — Loading')
        self.configure(bg='#0d1117')
        self.overrideredirect(True)

        W, H = 540, 560
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f'{W}x{H}+{(sw-W)//2}+{(sh-H)//2}')
        self.resizable(False, False)

        self._result            = None
        self._prefer_online     = tk.BooleanVar(value=True)
        self._apply_sell_fee    = tk.BooleanVar(value=True)
        self._newpor_rows       = None
        self._newpor_portfolios = None
        self._newpor_validation = None

        self._build_ui()

    def _build_ui(self):
        # ── Branding ────────────────────────────────────────────────────────
        tk.Label(self, text='MYIPO+', bg='#0d1117', fg='#00C9FF',
                 font=('Segoe UI', 38, 'bold')).pack(pady=(32, 0))
        tk.Label(self, text='Index Dashboard', bg='#0d1117', fg='#cdd9e5',
                 font=('Segoe UI', 13)).pack()
        tk.Label(self, text=f'by MYIPO Research  ·  {datetime.now().strftime("%d %b %Y")}',
                 bg='#0d1117', fg='#444', font=('Segoe UI', 8)).pack(pady=(2, 24))

        # ── File status strip (shows sizes once found on disk) ────────────
        self._file_status_var = tk.StringVar(value='')
        self._refresh_file_sizes()
        tk.Label(self, textvariable=self._file_status_var,
                 bg='#0d1117', fg='#58A6FF', font=('Segoe UI', 8)).pack()

        # ── Stage + detail labels ─────────────────────────────────────────
        self._stage_var  = tk.StringVar(value='Press  Load  to start.')
        self._detail_var = tk.StringVar(value='')
        tk.Label(self, textvariable=self._stage_var,
                 bg='#0d1117', fg='#cdd9e5',
                 font=('Segoe UI', 9, 'bold')).pack(pady=(12, 0))
        tk.Label(self, textvariable=self._detail_var,
                 bg='#0d1117', fg='#555', font=('Segoe UI', 8)).pack()

        # ── Progress bar ──────────────────────────────────────────────────
        _pbar_style = ttk.Style()
        _pbar_style.theme_use('default')
        _pbar_style.configure('Splash.Horizontal.TProgressbar',
                              troughcolor='#21262d', background='#3FB950',
                              bordercolor='#21262d', lightcolor='#3FB950',
                              darkcolor='#3FB950', thickness=18)
        self._pbar = ttk.Progressbar(self, mode='determinate', maximum=100,
                                     length=480, style='Splash.Horizontal.TProgressbar')
        self._pbar.pack(pady=(10, 0), padx=30)
        self._pct_var = tk.StringVar(value='')
        tk.Label(self, textvariable=self._pct_var,
                 bg='#0d1117', fg='#00C9FF',
                 font=('Segoe UI', 8, 'bold')).pack(pady=(2, 0))

        # ── Options ───────────────────────────────────────────────────────
        opt_row = tk.Frame(self, bg='#0d1117')
        opt_row.pack(pady=(14, 0))
        tk.Checkbutton(opt_row, text='☁️  Use Online Database (Google Sheets)',
                       variable=self._prefer_online,
                       bg='#0d1117', fg='#cdd9e5', selectcolor='#0d1117',
                       activebackground='#0d1117', activeforeground='#00C9FF',
                       font=('Segoe UI', 9), relief='flat', cursor='hand2').pack()
        tk.Label(opt_row, text='(falls back to local CSV if offline)',
                 bg='#0d1117', fg='#444', font=('Segoe UI', 7)).pack()

        fee_row = tk.Frame(self, bg='#0d1117')
        fee_row.pack(pady=(6, 0))
        tk.Checkbutton(fee_row, text='💸  Apply RM2.01 sell fee on fund exits',
                       variable=self._apply_sell_fee,
                       bg='#0d1117', fg='#cdd9e5', selectcolor='#0d1117',
                       activebackground='#0d1117', activeforeground='#00E5A0',
                       font=('Segoe UI', 9), relief='flat', cursor='hand2').pack()
        tk.Label(fee_row, text='(broker commission + tax; buy-ins always free)',
                 bg='#0d1117', fg='#444', font=('Segoe UI', 7)).pack()

        # ── Load button ───────────────────────────────────────────────────
        self._load_btn = tk.Button(
            self, text='⟳  Load Data', command=self._start_load,
            bg='#00C9FF', fg='#0d1117', font=('Segoe UI', 14, 'bold'),
            relief='flat', padx=36, pady=12, cursor='hand2')
        self._load_btn.pack(pady=(18, 0))

        self._spinner_chars = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏']
        self._spinner_idx   = 0
        self._spinner_var   = tk.StringVar(value='')
        tk.Label(self, textvariable=self._spinner_var,
                 bg='#0d1117', fg='#00C9FF',
                 font=('Segoe UI', 14)).pack(pady=(6, 20))

    def _refresh_file_sizes(self):
        """Show local file sizes for teet.csv and this K-ID's ledger cache."""
        parts = []
        try:
            sz = os.path.getsize(DEFAULT_INPUT_FILE)
            s  = f'{sz/1024:.0f} KB' if sz < 1024*1024 else f'{sz/1024/1024:.1f} MB'
            parts.append(f'teet.csv: {s}')
        except Exception:
            parts.append('teet.csv: not found')
        try:
            _who = (CURRENT_KID or 'guest').strip().lower()
            nf = os.path.join(os.path.dirname(DEFAULT_INPUT_FILE),
                              f'{_NEWPOR_CACHE_PREFIX}_{_who}.csv')
            if os.path.exists(nf):
                nz = os.path.getsize(nf)
                ns = f'{nz/1024:.0f} KB' if nz < 1024*1024 else f'{nz/1024/1024:.1f} MB'
                parts.append(f'ledger: {ns}')
            else:
                parts.append('ledger: will download')
        except Exception:
            pass
        self._file_status_var.set('  ·  '.join(parts))

    def _log(self, msg):
        msg_l = msg.strip()
        if msg_l.startswith('📊  Source: ONLINE'):
            self._detected_source = 'online'
        elif msg_l.startswith('📊  Source: LOCAL'):
            self._detected_source = 'local'

        # ── Determinate progress: advance a step counter against an estimated
        #    total stage count, so the bar fills smoothly 0->100% rather than
        #    bouncing indeterminately. The estimate doesn't need to be exact —
        #    it just needs to advance steadily and never overshoot 100 early.
        is_progress_event = (
            'chunk' in msg_l.lower() or 'Downloading chunk' in msg_l
            or 'TR dividends' in msg_l or 'TR price matrix' in msg_l
            or 'TR-Series' in msg_l or 'Top 10' in msg_l or 'Top10' in msg_l
            or 'Building' in msg_l or msg_l.startswith('✅')
        )
        if is_progress_event:
            self._progress_steps = getattr(self, '_progress_steps', 0) + 1
            pct = min(96, int(self._progress_steps / self._progress_estimate * 100))
            self._pbar['value'] = pct
            self._pct_var.set(f'{pct}%')

        if 'chunk' in msg_l.lower() or 'Downloading chunk' in msg_l:
            self._stage_var.set('⬇  Downloading price data…')
            self._detail_var.set(msg_l)
        elif 'TR dividends' in msg_l:
            self._stage_var.set('⬇  Downloading dividend data (TR)…')
            self._detail_var.set(msg_l)
        elif 'TR price matrix' in msg_l or 'TR-Series' in msg_l:
            self._stage_var.set(msg_l[:60])
            self._detail_var.set('')
        elif 'Top 10' in msg_l or 'Top10' in msg_l:
            self._stage_var.set(msg_l[:60])
            self._detail_var.set('')
        elif 'Building' in msg_l:
            self._stage_var.set(msg_l)
            self._detail_var.set('')
        elif msg_l.startswith('✅') and 'Y20' in msg_l:
            self._stage_var.set(msg_l[:60])
            self._detail_var.set('')
        elif msg_l.startswith('✅'):
            self._stage_var.set(msg_l)
            self._detail_var.set('')
        elif msg_l.startswith('⚠️'):
            self._detail_var.set(msg_l)
        else:
            self._stage_var.set(msg_l)
            self._detail_var.set('')

    def _animate_spinner(self):
        try:
            if not self.winfo_exists():
                return
            self._spinner_var.set(self._spinner_chars[self._spinner_idx % len(self._spinner_chars)])
            self._spinner_idx += 1
            self.after(80, self._animate_spinner)
        except tk.TclError:
            pass  # window was destroyed; ignore stale after-callbacks

    def _start_load(self):
        path = DEFAULT_INPUT_FILE
        # Show file sizes for both downloads
        self._refresh_file_sizes()
        self._load_btn.config(state='disabled', text='Loading…', bg='#333')
        self._progress_steps    = 0
        self._progress_estimate = 55
        self._pbar['value'] = 0
        self._pct_var.set('0%')
        self._animate_spinner()
        self._stage_var.set('⬇  Downloading teet.csv…')
        threading.Thread(target=self._load_thread,
                          args=(path, self._prefer_online.get(), self._apply_sell_fee.get()),
                          daemon=True).start()

    def _load_thread(self, path, prefer_online=True, apply_sell_fee=True):
        """
        Download teet.csv, this K-ID's ledger AND dividend/EPS data IN PARALLEL.
        Three tasks fire simultaneously — dashboard opens only when all three done.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed as _ac

        teet_result   = [None]
        teet_error    = [None]
        mybook_result = [None, None, None]  # [rows, validation, portfolios]
        mybook_error  = [None]

        def _fetch_teet():
            try:
                result = build_indices(
                    path,
                    log_cb=lambda m: self.after(0, lambda msg=m: self._log(f'[teet]  {msg}')),
                    prefer_online=prefer_online,
                    apply_sell_fee=apply_sell_fee,
                )
                teet_result[0] = result
            except Exception as e:
                teet_error[0] = str(e)

        def _fetch_mybook():
            # If this K-ID hasn't connected a portfolio yet, there is nothing
            # of theirs to fetch — SCA will run first-run setup on open.
            # Downloading the demo sheet here would just waste a request.
            if not sca_has_source(load_app_settings()):
                self.after(0, lambda: self._log(
                    '[Ledger] ⏭  No portfolio connected yet — '
                    'SCA will prompt on first open.'))
                return
            try:
                rows = _mf_load_csv(
                    log_cb=lambda m: self.after(0, lambda msg=m: self._log(f'[Ledger] {msg}')),
                    prefer_online=prefer_online,
                )
                val   = _mf_validate_ledger(rows)
                ports = _mf_build_portfolios(rows) if val["ok"] else {}
                mybook_result[0] = rows
                mybook_result[1] = val
                mybook_result[2] = ports
                self.after(0, lambda: self._log(
                    f'[Ledger] ✅  {len(rows)} transactions loaded.'))
                # ── After the ledger is parsed, pre-fetch dividends + EPS for all
                #    SCA holdings in chunks — same pattern as teet price chunks.
                #    Results go into _MF_DIV_CACHE and disk (divs/*.parquet +
                #    live/eps_*.json) so the dividend estimator popup is instant.
                if prefer_online and ports:
                    _prefetch_sca_dividends(ports,
                        log_cb=lambda m: self.after(0, lambda msg=m: self._log(f'[Div]    {msg}')))
            except Exception as e:
                mybook_error[0] = str(e)
                self.after(0, lambda msg=str(e):
                    self._log(f'[Ledger] ⚠️  {msg}'))

        # ── Fire both downloads simultaneously ────────────────────────────────
        self.after(0, lambda: self._stage_var.set(
            '⬇  Downloading teet.csv + your ledger in parallel…'))

        with ThreadPoolExecutor(max_workers=2) as ex:
            f_teet   = ex.submit(_fetch_teet)
            f_mybook = ex.submit(_fetch_mybook)
            for fut in _ac([f_teet, f_mybook]):
                fut.result()

        # ── Both done — check results ─────────────────────────────────────────
        if teet_error[0]:
            self.after(0, lambda msg=teet_error[0]: self._on_error(msg))
            return

        self._result      = teet_result[0]
        self._last_source = getattr(self, '_detected_source', 'local')

        self._newpor_rows       = mybook_result[0]
        self._newpor_validation = mybook_result[1] or {
            "ok": False, "errors": [mybook_error[0] or "Unknown"],
            "warnings": [], "row_count": 0, "checked_at": None}
        self._newpor_portfolios = mybook_result[2]

        self.after(0, self._launch_dashboard)

    def _on_error(self, msg):
        self._pbar['value'] = 0
        self._pct_var.set('')
        self._spinner_var.set('')
        self._load_btn.config(state='normal', text='⟳  Load Data', bg='#00C9FF')
        self._stage_var.set('❌  Error — see details below.')
        self._detail_var.set(msg[:80])
        messagebox.showerror('Load Error', msg)

    def _launch_dashboard(self):
        self._pbar['value'] = 100
        self._pct_var.set('100%')
        self._stage_var.set('✅  Done! Opening dashboard…')
        self.after(400, self._open_dashboard)

    def _open_dashboard(self):
        frames, df, wsnap, bmap, snames, future_ipos, stock_price_df, delist_candidates, *_rest = \
            self._result if len(self._result) == 9 else (*self._result, pd.DataFrame())
        df_listed_all = _rest[0] if _rest else pd.DataFrame()
        self.destroy()
        app = MYIPOApp(
            preloaded=(frames, df, wsnap, bmap, snames, future_ipos, stock_price_df,
                       delist_candidates, df_listed_all),
            csv_path=DEFAULT_INPUT_FILE,
        )
        app._data_source = getattr(self, '_last_source', 'local')
        if hasattr(app, '_source_label_var'):
            src = app._data_source
            app._source_label_var.set(f'source: {"☁️" if src == "online" else "📂"} {src}')
        # Keep the dashboard's fee toggle in sync with what was chosen at splash
        app.apply_sell_fee.set(self._apply_sell_fee.get())
        # Hand off preloaded NEWPOR data so MYFOLIO/LottoStock open instantly
        app.newpor_rows       = getattr(self, '_newpor_rows', None)
        app.newpor_portfolios = getattr(self, '_newpor_portfolios', None)
        app.newpor_validation = getattr(self, '_newpor_validation', None)
        app.mainloop()


# =============================================================================
# APPLOCK — optional PIN gate, persisted via local settings file
# Simple fixed-PIN screen that gates the whole app before SplashScreen loads.
# Not real authentication yet — just the UI/flow scaffold. Whether the lock
# is even shown at all is controlled by a persistent setting (paa_settings.json),
# toggleable from the lock screen itself or from the dashboard's Settings menu.
# =============================================================================
import json as _json

APPLOCK_PIN = "1234"   # retired — kept only so old imports don't break;
                       # K-ID sign-in replaced PIN-only access entirely.
_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'paa_settings.json')
_DEFAULT_SETTINGS = {
    # Retired: K-ID sign-in is now mandatory. Kept so older settings
    # files still load cleanly; the value is no longer read.
    "applock_enabled": True,
    # ── K-ID accounts ────────────────────────────────────────────────────────
    # Each account: {"username": str, "pin_hash": str, "created": "YYYY-MM-DD"}
    # PINs are stored as salted SHA-256 hashes, never plaintext.
    "kid_accounts":  [],
    "kid_last_user": "",
    # ── PTS/ZDWS wallets, keyed by K-ID username ('' = guest) ────────────────
    # {username: {credits, predictions[], history[], zdws_active[], ...}}
    "pts_by_user":   {},
    "pts_migrated":  False,   # True once the legacy global wallet was claimed
    # ── SCA portfolio source (per K-ID username) ─────────────────────────────
    # {username: {"mode": "sheet"|"csv", "url": str, "csv_path": str,
    #             "label": str, "saved": "YYYY-MM-DD HH:MM"}}
    "sca_sources":   {},
    "pts": {
        "credits":          10_000.0,
        "predictions":      [],
        "history":          [],
        "total_made":       0,
        "total_correct":    0,
        "daily_topup_date": "",   # 'YYYY-MM-DD' of last free daily claim
        "zdws_active":      [],   # open ZeroDecay warrant positions
        "zdws_history":     [],   # settled ZeroDecay warrants
        "zdos_active":      [],   # open ZeroDecay option positions
        "zdos_history":     [],   # settled ZeroDecay options
    }
}


# ── K-ID auth helpers ────────────────────────────────────────────────────────
import hashlib as _hashlib

def kid_hash_pin(username: str, pin: str) -> str:
    """Salted SHA-256 of a PIN. Salt = lowercased username, so the same PIN
    under different usernames produces different hashes."""
    salt = username.strip().lower()
    return _hashlib.sha256(f'{salt}::{pin}'.encode('utf-8')).hexdigest()


def kid_find_account(settings: dict, username: str):
    """Return the account dict for a username (case-insensitive), or None."""
    u = username.strip().lower()
    for acc in settings.get('kid_accounts', []):
        if acc.get('username', '').strip().lower() == u:
            return acc
    return None


def kid_verify(settings: dict, username: str, pin: str) -> bool:
    acc = kid_find_account(settings, username)
    if not acc:
        return False
    return acc.get('pin_hash') == kid_hash_pin(username, pin)


def kid_create(settings: dict, username: str, pin: str) -> tuple:
    """Create a new K-ID. Returns (ok: bool, message: str)."""
    u = username.strip()
    if len(u) < 3:
        return False, 'Username must be at least 3 characters.'
    if not u.replace('_', '').replace('-', '').isalnum():
        return False, 'Username may only contain letters, numbers, - and _.'
    if kid_find_account(settings, u):
        return False, f'K-ID "{u}" already exists.'
    if not (pin.isdigit() and 4 <= len(pin) <= 8):
        return False, 'PIN must be 4–8 digits.'
    import datetime as _d
    settings.setdefault('kid_accounts', []).append({
        'username': u,
        'pin_hash': kid_hash_pin(u, pin),
        'created':  _d.date.today().strftime('%Y-%m-%d'),
    })
    return True, f'K-ID "{u}" created.'


# ── SCA portfolio source (per K-ID) ──────────────────────────────────────────
# The signed-in user's own sheet/CSV. Falls back to the built-in demo sheet
# when a user hasn't configured one yet.
CURRENT_KID = ''   # set at login; '' = no account (legacy PIN mode)


def sca_normalise_sheet_url(url: str) -> str:
    """Accept any Google Sheets URL form and return a CSV-export URL.
    Handles: /pub?output=csv, /pubhtml, /edit#gid=N, and bare doc URLs."""
    u = (url or '').strip()
    if not u:
        return ''
    if 'output=csv' in u:
        return u
    # Published-to-web form: .../e/2PACX-.../pubhtml  ->  .../pub?output=csv
    if '/pubhtml' in u:
        return u.split('/pubhtml')[0] + '/pub?gid=0&single=true&output=csv'
    # Standard edit URL: .../d/<ID>/edit#gid=123  ->  .../d/<ID>/export?format=csv&gid=123
    import re as _re
    m = _re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', u)
    if m:
        doc_id = m.group(1)
        gid_m  = _re.search(r'[#&?]gid=(\d+)', u)
        gid    = gid_m.group(1) if gid_m else '0'
        return (f'https://docs.google.com/spreadsheets/d/{doc_id}'
                f'/export?format=csv&gid={gid}')
    return u   # not recognisable — hand back unchanged, let the fetch fail loudly


def sca_has_source(settings: dict, username: str = None) -> bool:
    """True if this K-ID has explicitly chosen a portfolio source.
    A missing entry means first run — SCA should force setup rather than
    quietly showing demo data as if it were theirs."""
    u = (username if username is not None else CURRENT_KID) or ''
    src = settings.get('sca_sources', {}).get(u)
    return bool(src and src.get('mode'))


# ── K-ID recovery — knowledge questions from the user's own ledger ───────────
# If someone forgets their PIN, they can prove ownership by answering questions
# only the portfolio owner would know: what they bought today, what they hold
# most of, and so on. Questions are generated live from their OWN sheet/CSV,
# so there is nothing extra to store and nothing to keep in sync.
#
# NOTE ON STRENGTH: this is deliberate, proportionate security for a local
# desktop app — it stops a housemate poking at your laptop. It is NOT strong
# auth: anyone who can already read your sheet can answer these. The real
# protection on the ledger is Google's own sharing settings / file permissions.

def kid_recovery_questions(rows: list, portfolios: dict, n: int = 3) -> list:
    """Build multiple-choice questions from a ledger.
    Returns [{'q': str, 'options': [str], 'answer': str}] — at most n of them.
    Returns [] when there isn't enough history to ask anything meaningful.
    """
    import random as _rnd
    import datetime as _d

    qs = []

    # Flatten non-cash holdings across every portfolio
    holds = []   # (ticker, units, port_key)
    for pk, port in (portfolios or {}).items():
        for tkr, item in port.get('holdings', {}).items():
            if item.get('_is_cash'):
                continue
            if item.get('shares', 0) > 0:
                holds.append((tkr, item['shares'], pk))

    def _decoys(correct, pool, k=3):
        """Wrong answers drawn from the user's own universe where possible —
        a decoy they've never heard of would give the answer away."""
        cand = [x for x in pool if x != correct]
        _rnd.shuffle(cand)
        out = cand[:k]
        if len(out) < k:
            extra = [c for c, _ in LOTTO_STOCK_LIST if c != correct and c not in out]
            _rnd.shuffle(extra)
            out += extra[:k - len(out)]
        return out

    # ── Q1: most-held by units ────────────────────────────────────────────────
    if len(holds) >= 4:
        by_units = sorted(holds, key=lambda x: -x[1])
        top = by_units[0][0]
        # Only ask if there's a clear winner — a near-tie is unfair
        if len(by_units) < 2 or by_units[0][1] > by_units[1][1] * 1.05:
            pool = [t for t, _, _ in by_units]
            opts = [top] + _decoys(top, pool)
            _rnd.shuffle(opts)
            qs.append({
                'q':       'Which holding do you own the MOST units of?',
                'options': opts,
                'answer':  top,
            })

    # ── Q2: what you bought today (or most recently) ──────────────────────────
    buys = []
    for r in (rows or []):
        if r.get(_NC_TYPE, '').strip().lower() != 'buy':
            continue
        ds = r.get(_NC_DATE, '').strip()
        d  = None
        for fmt in ('%m/%d/%Y', '%d/%m/%Y', '%Y-%m-%d', '%m-%d-%Y'):
            try:
                d = _d.datetime.strptime(ds, fmt).date(); break
            except ValueError:
                continue
        if d:
            buys.append((d, r.get(_NC_ASSET, '').strip().upper()))
    if len(buys) >= 4:
        buys.sort(key=lambda x: -x[0].toordinal())
        last_date, last_asset = buys[0]
        today = _d.date.today()
        label = ('buy TODAY' if last_date == today
                 else f'buy most recently ({last_date.strftime("%d %b %Y")})')
        pool  = [a for _, a in buys]
        opts  = [last_asset] + _decoys(last_asset, pool)
        _rnd.shuffle(opts)
        qs.append({
            'q':       f'Which asset did you {label}?',
            'options': opts,
            'answer':  last_asset,
        })

    # ── Q3: first asset ever bought ───────────────────────────────────────────
    if len(buys) >= 5:
        first_date, first_asset = min(buys, key=lambda x: x[0].toordinal())
        pool = [a for _, a in buys]
        opts = [first_asset] + _decoys(first_asset, pool)
        _rnd.shuffle(opts)
        qs.append({
            'q':       'Which asset did you buy FIRST, when you started?',
            'options': opts,
            'answer':  first_asset,
        })

    # ── Q4: position count (fallback / extra) ─────────────────────────────────
    if len(holds) >= 3:
        n_pos = len(holds)
        opts  = {str(n_pos)}
        for delta in (-3, -2, -1, 1, 2, 3, 5):
            if n_pos + delta > 0:
                opts.add(str(n_pos + delta))
        opts = list(opts)
        _rnd.shuffle(opts)
        opts = ([str(n_pos)] + [o for o in opts if o != str(n_pos)][:3])
        _rnd.shuffle(opts)
        qs.append({
            'q':       'How many separate positions do you currently hold?',
            'options': opts,
            'answer':  str(n_pos),
        })

    _rnd.shuffle(qs)
    return qs[:n]


def kid_load_recovery_data(settings: dict, username: str):
    """Load a user's ledger for recovery WITHOUT signing them in.
    Returns (rows, portfolios) or (None, None) if unreachable."""
    global CURRENT_KID
    src = settings.get('sca_sources', {}).get(username or '')
    if not src or not src.get('mode'):
        return None, None
    saved_kid = CURRENT_KID
    try:
        CURRENT_KID = username          # so per-user cache paths resolve
        if src['mode'] == 'csv' and src.get('csv_path'):
            if not os.path.exists(src['csv_path']):
                return None, None
            with open(src['csv_path'], 'r', encoding='utf-8-sig') as f:
                rows = _mf_parse_rows(f.read())
        else:
            rows = _mf_load_csv(url=src.get('url'))
        ports = _mf_build_portfolios(rows)
        return rows, ports
    except Exception:
        return None, None
    finally:
        CURRENT_KID = saved_kid


def kid_reset_pin(settings: dict, username: str, new_pin: str) -> tuple:
    """Set a new PIN after successful recovery. Returns (ok, message)."""
    if not (new_pin.isdigit() and 4 <= len(new_pin) <= 8):
        return False, 'PIN must be 4–8 digits.'
    acc = kid_find_account(settings, username)
    if not acc:
        return False, 'K-ID not found.'
    acc['pin_hash'] = kid_hash_pin(username, new_pin)
    save_app_settings(settings)
    return True, 'PIN reset. Sign in with your new PIN.'


def sca_get_source(settings: dict, username: str = None) -> dict:
    """Return the SCA source config for a user.
    Shape: {"mode": "sheet"|"csv"|"demo", "url": str, "csv_path": str, "label": str}
    """
    u = (username if username is not None else CURRENT_KID) or ''
    src = settings.get('sca_sources', {}).get(u)
    if src and (src.get('url') or src.get('csv_path')):
        return src
    return {'mode': '', 'url': '', 'csv_path': '',
            'label': 'No portfolio connected'}


def sca_save_source(settings: dict, username: str, mode: str,
                    url: str = '', csv_path: str = '', label: str = '') -> dict:
    """Persist a user's SCA source and return the saved record."""
    import datetime as _d
    rec = {
        'mode':     mode,
        'url':      sca_normalise_sheet_url(url) if mode == 'sheet' else '',
        'csv_path': csv_path if mode == 'csv' else '',
        'label':    label or ('My Google Sheet' if mode == 'sheet' else 'Imported CSV'),
        'saved':    _d.datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    settings.setdefault('sca_sources', {})[username or ''] = rec
    save_app_settings(settings)
    return rec


import copy as _copy


def _pts_fresh_wallet() -> dict:
    """A brand-new PTS/ZDWS wallet. Deep-copied so the mutable lists in
    _DEFAULT_SETTINGS are never shared between K-IDs."""
    return _copy.deepcopy(_DEFAULT_SETTINGS['pts'])


def load_app_settings() -> dict:
    if os.path.exists(_SETTINGS_PATH):
        try:
            with open(_SETTINGS_PATH, 'r', encoding='utf-8') as f:
                data = _json.load(f)
            merged = dict(_DEFAULT_SETTINGS)
            merged.update(data)
            return merged
        except Exception:
            pass
    return dict(_DEFAULT_SETTINGS)


def save_app_settings(settings: dict):
    """Persist app settings to disk, next to the script."""
    try:
        with open(_SETTINGS_PATH, 'w', encoding='utf-8') as f:
            _json.dump(settings, f, indent=2)
    except Exception:
        pass


class AppLockScreen(tk.Tk):
    """K-ID sign-in — username + PIN. Falls back to legacy PIN-only mode
    when no K-ID account exists yet."""

    def __init__(self):
        super().__init__()
        self.title("PAA — K-ID Sign In")
        self.configure(bg='#0d1117')
        self.geometry("440x560")
        self.resizable(False, False)
        self._settings = load_app_settings()
        self._user_var = tk.StringVar(value=self._settings.get('kid_last_user', ''))
        self._pin_var  = tk.StringVar(value="")
        self._mode = 'signin'   # 'signin' | 'create'
        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        for w in self.winfo_children():
            w.destroy()

        center = tk.Frame(self, bg='#0d1117')
        center.place(relx=0.5, rely=0.5, anchor="center")

        accounts = self._settings.get('kid_accounts', [])
        creating = (self._mode == 'create') or not accounts

        tk.Label(center, text="🆔", font=("Segoe UI", 38),
                 bg='#0d1117', fg='#58A6FF').pack()
        tk.Label(center, text="K-ID", font=("Segoe UI", 20, "bold"),
                 fg='#58A6FF', bg='#0d1117').pack(pady=(2, 0))
        tk.Label(center,
                 text="Create your K-ID" if creating else "Sign in to continue",
                 font=("Segoe UI", 9), fg='#888', bg='#0d1117').pack(pady=(2, 14))

        # Username
        tk.Label(center, text="Username", font=("Segoe UI", 8),
                 fg='#8b949e', bg='#0d1117', anchor='w').pack(fill='x')
        if accounts and not creating:
            names = [a['username'] for a in accounts]
            u_cb = ttk.Combobox(center, textvariable=self._user_var, values=names,
                                font=("Segoe UI", 12), width=18)
            u_cb.pack(pady=(2, 8), ipady=3)
            if self._user_var.get() not in names:
                self._user_var.set(names[0])
        else:
            tk.Entry(center, textvariable=self._user_var, justify="center",
                     font=("Segoe UI", 13), bg='#161b22', fg='#cdd9e5',
                     insertbackground='#cdd9e5', relief="flat",
                     width=18).pack(pady=(2, 8), ipady=5)

        # PIN
        tk.Label(center, text="PIN (4–8 digits)", font=("Segoe UI", 8),
                 fg='#8b949e', bg='#0d1117', anchor='w').pack(fill='x')
        pin_entry = tk.Entry(center, textvariable=self._pin_var, show="•",
                             justify="center", font=("Segoe UI", 16),
                             bg='#161b22', fg='#cdd9e5',
                             insertbackground='#cdd9e5', relief="flat", width=12)
        pin_entry.pack(pady=(2, 0), ipady=6)
        pin_entry.bind("<Return>",
                       lambda e: self._create() if creating else self._sign_in())
        pin_entry.focus_set()

        self._error_var = tk.StringVar(value="")
        tk.Label(center, textvariable=self._error_var, font=("Segoe UI", 8),
                 fg='#F85149', bg='#0d1117', wraplength=320,
                 justify='center').pack(pady=(8, 0))

        # Primary action
        tk.Button(center,
                  text="Create K-ID" if creating else "Sign In",
                  font=("Segoe UI", 11, "bold"), bg='#58A6FF', fg='#0d1117',
                  relief="flat", padx=24, pady=8, cursor="hand2",
                  command=self._create if creating else self._sign_in
                  ).pack(pady=(12, 0))

        # Mode switch
        if accounts:
            tk.Button(center,
                      text="← Back to sign in" if creating else "+ Create another K-ID",
                      font=("Segoe UI", 8), bg='#0d1117', fg='#58A6FF',
                      relief="flat", cursor="hand2", bd=0,
                      activebackground='#0d1117', activeforeground='#79c0ff',
                      command=self._toggle_mode).pack(pady=(8, 0))

        if accounts and not creating:
            tk.Button(center, text="Forgot your PIN?",
                      font=("Segoe UI", 8), bg='#0d1117', fg='#8b949e',
                      relief="flat", cursor="hand2", bd=0,
                      activebackground='#0d1117', activeforeground='#D29922',
                      command=self._open_recovery).pack(pady=(2, 0))

        if creating:
            tk.Label(center,
                     text="Your K-ID is local to this device.\n"
                          "It keeps your portfolio and PTS wallet separate\n"
                          "from anyone else using this app.",
                     font=("Segoe UI", 7), fg='#555', bg='#0d1117',
                     justify='center').pack(pady=(10, 0))

        # Security note
        box = tk.Frame(center, bg='#161b22', highlightbackground='#30363D',
                       highlightthickness=1, padx=14, pady=8)
        box.pack(pady=(18, 0), fill='x')
        tk.Label(box, text="🔒  Sign in is required",
                 font=("Segoe UI", 8, "bold"), fg='#888',
                 bg='#161b22').pack(anchor='w')
        tk.Label(box,
                 text="Your K-ID decides which portfolio, PTS wallet and\n"
                      "warrant book load. PINs are stored hashed, never in\n"
                      "plain text, and never leave this device.",
                 font=("Segoe UI", 7), fg='#555', bg='#161b22',
                 justify='left').pack(anchor='w', pady=(2, 0))

    def _open_recovery(self):
        """Forgot-PIN recovery — prove ownership by answering questions
        generated live from the user's own portfolio ledger."""
        user = self._user_var.get().strip()
        if not user or not kid_find_account(self._settings, user):
            self._error_var.set("Pick the K-ID you want to recover first.")
            return

        if not sca_has_source(self._settings, user):
            messagebox.showwarning(
                'K-ID Recovery',
                f'"{user}" has never connected a portfolio, so there are no\n'
                f'questions to verify against.\n\n'
                f'Recovery works by asking about your own transactions —\n'
                f'without a ledger there is nothing to ask.',
                parent=self)
            return

        win = tk.Toplevel(self)
        win.title('K-ID Recovery')
        win.configure(bg='#0d1117')
        win.geometry('460x520')
        win.transient(self)
        win.grab_set()

        tk.Label(win, text='🔓  K-ID RECOVERY', font=('Segoe UI', 13, 'bold'),
                 fg='#D29922', bg='#0d1117').pack(pady=(16, 2))
        tk.Label(win, text=f'Recovering: {user}', font=('Segoe UI', 9),
                 fg='#cdd9e5', bg='#0d1117').pack()

        status = tk.StringVar(value='⏳  Reading your ledger…')
        tk.Label(win, textvariable=status, font=('Segoe UI', 8),
                 fg='#8b949e', bg='#0d1117', wraplength=400,
                 justify='center').pack(pady=(10, 6))

        body = tk.Frame(win, bg='#0d1117')
        body.pack(fill='both', expand=True, padx=18)

        state = {'qs': [], 'vars': [], 'attempts': 0}

        def _build_quiz():
            rows, ports = kid_load_recovery_data(self._settings, user)
            if not rows or not ports:
                status.set('❌  Could not read your portfolio.\n'
                           'Recovery needs access to the sheet/CSV you connected.')
                return
            qs = kid_recovery_questions(rows, ports, n=3)
            if len(qs) < 2:
                status.set('❌  Not enough transaction history to verify you.\n'
                           'Recovery needs a few buys on record.')
                return
            state['qs'] = qs
            status.set(f'Answer all {len(qs)} questions about your own portfolio.')

            for i, q in enumerate(qs, 1):
                card = tk.Frame(body, bg='#161b22', highlightbackground='#30363D',
                                highlightthickness=1, padx=12, pady=8)
                card.pack(fill='x', pady=4)
                tk.Label(card, text=f'{i}.  {q["q"]}', font=('Segoe UI', 9, 'bold'),
                         fg='#cdd9e5', bg='#161b22', wraplength=380,
                         justify='left').pack(anchor='w')
                v = tk.StringVar(value='')
                state['vars'].append(v)
                opt_row = tk.Frame(card, bg='#161b22')
                opt_row.pack(fill='x', pady=(4, 0))
                for opt in q['options']:
                    tk.Radiobutton(opt_row, text=opt, variable=v, value=opt,
                                   bg='#161b22', fg='#8b949e', selectcolor='#161b22',
                                   activebackground='#161b22', activeforeground='#58A6FF',
                                   font=('Segoe UI', 8), relief='flat',
                                   cursor='hand2', indicatoron=False,
                                   padx=8, pady=3).pack(side='left', padx=2)

            tk.Button(win, text='✓  Verify', font=('Segoe UI', 10, 'bold'),
                      bg='#D29922', fg='#0d1117', relief='flat',
                      padx=20, pady=6, cursor='hand2',
                      command=_verify).pack(pady=(8, 12))

        def _verify():
            answers = [v.get() for v in state['vars']]
            if any(not a for a in answers):
                status.set('⚠️  Answer every question.')
                return
            correct = sum(1 for q, a in zip(state['qs'], answers) if a == q['answer'])
            if correct == len(state['qs']):
                win.destroy()
                self._open_reset_pin(user)
                return
            state['attempts'] += 1
            if state['attempts'] >= 3:
                messagebox.showerror(
                    'K-ID Recovery',
                    'Too many failed attempts.\n\n'
                    'If this is your K-ID, check your portfolio in the sheet\n'
                    'and try again later.',
                    parent=win)
                win.destroy()
                return
            status.set(f'❌  {correct}/{len(state["qs"])} correct — '
                       f'{3 - state["attempts"]} attempt(s) left.')

        # Load in the background so the dialog paints immediately
        def _bg():
            try:
                self.after(0, _build_quiz)
            except Exception:
                pass
        self.after(80, _bg)

    def _open_reset_pin(self, user: str):
        """Post-recovery — set a new PIN."""
        win = tk.Toplevel(self)
        win.title('Set New PIN')
        win.configure(bg='#0d1117')
        win.geometry('360x280')
        win.transient(self)
        win.grab_set()

        tk.Label(win, text='✅  IDENTITY CONFIRMED', font=('Segoe UI', 12, 'bold'),
                 fg='#3FB950', bg='#0d1117').pack(pady=(18, 2))
        tk.Label(win, text=f'Set a new PIN for "{user}"', font=('Segoe UI', 8),
                 fg='#888', bg='#0d1117').pack(pady=(0, 14))

        new_var, cf_var = tk.StringVar(), tk.StringVar()
        for lbl, var in [('New PIN (4–8 digits)', new_var), ('Confirm new PIN', cf_var)]:
            tk.Label(win, text=lbl, font=('Segoe UI', 8), fg='#8b949e',
                     bg='#0d1117').pack()
            tk.Entry(win, textvariable=var, show='•', justify='center',
                     font=('Segoe UI', 14), bg='#161b22', fg='#cdd9e5',
                     insertbackground='#cdd9e5', relief='flat',
                     width=12).pack(pady=(2, 8), ipady=4)

        err = tk.StringVar()
        tk.Label(win, textvariable=err, font=('Segoe UI', 8), fg='#F85149',
                 bg='#0d1117', wraplength=300).pack()

        def _apply():
            if new_var.get() != cf_var.get():
                err.set("PINs don't match."); return
            ok, msg = kid_reset_pin(self._settings, user, new_var.get())
            if not ok:
                err.set(msg); return
            win.destroy()
            self._settings = load_app_settings()
            self._pin_var.set('')
            self._error_var.set('')
            messagebox.showinfo('K-ID', msg, parent=self)

        tk.Button(win, text='Set PIN', font=('Segoe UI', 10, 'bold'),
                  bg='#3FB950', fg='#0d1117', relief='flat', padx=20, pady=6,
                  cursor='hand2', command=_apply).pack(pady=(10, 0))

    def _toggle_mode(self):
        self._mode = 'signin' if self._mode == 'create' else 'create'
        self._pin_var.set("")
        self._build_ui()

    # ── Actions ──────────────────────────────────────────────────────────────
    def _create(self):
        u, p = self._user_var.get(), self._pin_var.get()
        ok, msg = kid_create(self._settings, u, p)
        if not ok:
            self._error_var.set(msg)
            self._pin_var.set("")
            return
        self._settings['kid_last_user'] = u.strip()
        save_app_settings(self._settings)
        self._launch(u.strip())

    def _sign_in(self):
        u, p = self._user_var.get().strip(), self._pin_var.get()
        if not u:
            self._error_var.set("Pick a K-ID."); return
        if not p:
            self._error_var.set("Enter your PIN."); return
        if kid_verify(self._settings, u, p):
            self._settings['kid_last_user'] = u
            save_app_settings(self._settings)
            self._launch(u)
            return
        self._error_var.set("Wrong username or PIN.")
        self._pin_var.set("")

    def _launch(self, username: str):
        global CURRENT_KID
        CURRENT_KID = username
        self.destroy()
        SplashScreen().mainloop()


# =============================================================================
if __name__ == '__main__':
    # K-ID sign-in is mandatory — the account decides which portfolio,
    # PTS wallet and warrant book get loaded, so there is no bypass.
    AppLockScreen().mainloop()
