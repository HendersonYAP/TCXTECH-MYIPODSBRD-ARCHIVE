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
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

# =============================================================================
# ONLINE DATABASE
# =============================================================================
ONLINE_DB_URL = (
    'https://docs.google.com/spreadsheets/d/e/'
    '2PACX-1vTZQwnk6JF3NazIiGxXumkHio3_8q6NbB1P5KxNyCYBlA0BCszcv12ahR7hb-3hZYqzFN4FxR3GiVsg'
    '/pub?gid=147724768&single=true&output=tsv'
)
ONLINE_TIMEOUT   = 10          # seconds before giving up
LOCAL_CACHE_NAME = 'teet_cache.csv'   # saved alongside the local CSV

def fetch_online_db(log_cb=None):
    """
    Try to download the Google Sheets TSV.
    Returns (DataFrame, source_label) or (None, error_msg).
    """
    import urllib.request
    import io
    def log(m):
        if log_cb: log_cb(m)
    try:
        log('🌐  Connecting to online database…')
        req = urllib.request.Request(
            ONLINE_DB_URL,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=ONLINE_TIMEOUT) as resp:
            raw_bytes = resp.read()
        log('✅  Online database fetched successfully.')
        df = pd.read_csv(
            io.BytesIO(raw_bytes),
            sep='\t',
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
DEFAULT_FOLDER     = r'C:\Users\User\Documents'
DEFAULT_INPUT_FILE = os.path.join(DEFAULT_FOLDER, 'teet.csv')
DEFAULT_OUTPUT_CSV = os.path.join(DEFAULT_FOLDER, 'MYIPO_Index_History.csv')
START_DATE_STR     = (pd.Timestamp.now() - pd.DateOffset(years=7)).strftime('%Y-%m-%d')
MALAYSIA_TZ        = ZoneInfo('Asia/Kuala_Lumpur') if ZoneInfo else None
NOW_MY             = datetime.now(MALAYSIA_TZ) if MALAYSIA_TZ else datetime.now()
TODAY              = pd.Timestamp(NOW_MY.date()).as_unit('s')  # match yfinance datetime64[s]

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
# TOP-10 SERIES DEFINITION
# =============================================================================
TOP10_LABEL      = 'MYIPO+ Top 10'

ALL_INDEX_LABELS = (
    [c[0] for c in INDEX_CONFIG] +
    [c[0] for c in YSERIES_CONFIG] +
    [c[0] for c in TR_SERIES_CONFIG] +
    [TOP10_LABEL] +
    FUND_LABELS
)

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

# =============================================================================
# CORE ENGINE
# =============================================================================
def build_indices(input_file, log_cb=None, prefer_online=True):
    def log(msg):
        if log_cb: log_cb(msg)

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
        gap_days = (df.loc[bad_d1_mask, 'D-1'] - df.loc[bad_d1_mask, 'Trade Date']).dt.days
        # (a) exactly 1-day gap → swap
        swap_mask = bad_d1_mask & (
            (df['D-1'] - df['Trade Date']).dt.days.fillna(0).abs() == 1
        )
        if swap_mask.any():
            df.loc[swap_mask, ['D-1', 'Trade Date']] = (
                df.loc[swap_mask, ['Trade Date', 'D-1']].values
            )
        # (b) larger gap → derive D-1 from Trade Date
        still_bad = df['D-1'] >= df['Trade Date']
        if still_bad.any():
            df.loc[still_bad, 'D-1'] = (
                df.loc[still_bad, 'Trade Date'] - pd.Timedelta(days=1)
            )

    df_listed   = df[df['Trade Date'] <= TODAY].copy()

    # future_ipos: stocks where the D-1 inclusion (17:00) or D0 first trade
    # (09:00) has not yet happened.  Both are checked in real-time in
    # _update_future_header; here we just keep any row whose Trade Date
    # is today-or-future (covers the morning window between D-1 17:00
    # and D0 09:00 when D-1 was yesterday but trading hasn't started yet).
    future_ipos = df[df['Trade Date'] >= TODAY].copy().sort_values(['D-1', 'Trade Date'])
    tickers_listed = df_listed['Symbol'].unique().tolist()

    log(f"⬇  Downloading price history for {len(tickers_listed)} tickers...")

    CHUNK = 50
    chunks = [tickers_listed[i:i+CHUNK] for i in range(0, len(tickers_listed), CHUNK)]
    parts  = []
    for i, chunk in enumerate(chunks, 1):
        log(f"   Downloading chunk {i}/{len(chunks)} ({len(chunk)} tickers)...")
        import io, contextlib
        _sink = io.StringIO()
        with contextlib.redirect_stdout(_sink), contextlib.redirect_stderr(_sink):
            raw = yf.download(chunk, start=START_DATE_STR, auto_adjust=True, progress=False)['Close']
        if isinstance(raw, pd.Series):
            raw = raw.to_frame(name=chunk[0])
        parts.append(raw)
    # ── FIX 3: suppress deprecation warning from concat of DatetimeIndex
    hist_data = pd.concat(parts, axis=1, sort=True).ffill()
    hist_data.columns = [str(c) for c in hist_data.columns]
    log(f"✅  Price data downloaded ({hist_data.shape[1]} tickers, {len(hist_data)} days).")

    short_name_map = df.drop_duplicates('Symbol').set_index('Symbol')['Name'].fillna('').to_dict()
    short_name_map = {k: (v if v else k) for k, v in short_name_map.items()}

    # ------------------------------------------------------------------
    # BUILD TOTAL-RETURN PRICE MATRIX
    # ------------------------------------------------------------------
    log("==> Building TR price matrix (dividend reinvestment)...")
    tr_hist = hist_data.copy()

    CHUNK_DIV = 20
    n_div_chunks = -(-len(tickers_listed) // CHUNK_DIV)
    for ci, i in enumerate(range(0, len(tickers_listed), CHUNK_DIV), 1):
        chunk = tickers_listed[i:i + CHUNK_DIV]
        log(f"   TR dividends {ci}/{n_div_chunks} ({len(chunk)} tickers)...")
        for sym in chunk:
            if sym not in hist_data.columns:
                continue
            try:
                divs = yf.Ticker(sym).dividends
                if divs is None or divs.empty:
                    continue
                if divs.index.tz is not None:
                    divs.index = divs.index.tz_localize(None)
                divs = divs[divs.index >= hist_data.index[0]]
                if divs.empty:
                    continue
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

    for fund_label, base_label, board_filter, use_d365 in FUND_CONFIG:
        log(f"   Building {fund_label}...")
        fund_dist_log = []   # list of (date, nav_before, dist_per_unit, nav_after)
        fund_unit_log = []   # list of (date, event, symbol, cash_used, units_issued, units_outstanding)
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

                # Launch date: D-1 row where the first constituent value appears.
                row_has_any = (holdings_val.sum(axis=1) > 0)
                if row_has_any.any():
                    launch_pos  = row_has_any.values.argmax()
                    launch_pos  = max(0, launch_pos - 1)   # one row earlier = the D-1 seeding row
                    launch_date = calendar[launch_pos]
                else:
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
                        fund_unit_log.append((dt, 'entry', sym, from_cash, units_issued, units_out))

                    # ── Handle exits: liquidate to cash ──────────────────────
                    exited_today = exit_aligned.iloc[i] if i < len(exit_aligned) else None
                    if exited_today is not None:
                        exited_syms = exited_today[exited_today].index.tolist()
                        # Use yesterday's value for the exiting position (today's row is already 0)
                        if i > 0:
                            prev_row = sub_hv.iloc[i-1]
                            for sym in exited_syms:
                                proceeds = prev_row.get(sym, 0.0)
                                if proceeds > 0:
                                    cash += proceeds
                                    fund_unit_log.append((dt, 'exit', sym, -proceeds, 0.0, units_out))

                    # ── NAV update: (holdings value + cash) / units outstanding ──
                    total_assets = total_val + cash
                    nav = total_assets / units_out if units_out > 0 else nav

                    # ── Quarterly distribution ────────────────────────────────
                    # Distribution = 100% of the DIVIDEND-ONLY return gap since
                    # the last payout: (TR index return − Core index return)
                    # over that window, applied to NAV. Capital growth is never
                    # distributed — this is a growth-focused fund. Skipped
                    # entirely if cash-per-unit is below the floor, so payouts
                    # never erode the fund's capital base.
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

                # Reindex onto the full calendar — flat (ffill) before launch
                nav_full   = nav_series.reindex(idx_arr)
                units_full = units_series.reindex(idx_arr)
                aum_full   = aum_series.reindex(idx_arr)
                nav_full.loc[:launch_date]   = np.nan
                units_full.loc[:launch_date] = np.nan
                aum_full.loc[:launch_date]   = np.nan
                nav_full   = nav_full.ffill()
                units_full = units_full.ffill().fillna(FUND_UNITS_ISSUED)
                aum_full   = aum_full.ffill()

                fund_frame = pd.DataFrame({
                    fund_label:               nav_full.round(4),     # NAV per unit (RM)
                    f'{fund_label} Chg%':    (nav_full.pct_change() * 100).round(2),
                    f'{fund_label} AUM':      aum_full.round(2),     # total fund size (RM)
                    f'{fund_label} Units':    units_full.round(2),   # units outstanding
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
    # MYIPO+ TOP 10
    # ------------------------------------------------------------------
    log(f"   Building {TOP10_LABEL}...")
    try:
        top10_universe = df_listed[df_listed['Quant1'] > 0].copy()
        top10_universe = top10_universe.drop_duplicates('Symbol').sort_values('Trade Date')

        if len(top10_universe) < 11:
            log(f"   ⚠️  {TOP10_LABEL}: fewer than 11 MYIPO+ IPOs listed, skipped.")
        else:
            eleventh_list_date = top10_universe.iloc[10]['Trade Date']
            log(f"   ℹ️  {TOP10_LABEL}: 11th IPO listed on {eleventh_list_date.date()}")

            def _next_trading_day_on_or_after(target_dt):
                pos = idx_arr.searchsorted(target_dt, side='left')
                return idx_arr[pos] if pos < len(idx_arr) else None

            rb_start = pd.Timestamp('2020-11-21')
            cal_21sts = pd.date_range(
                start=rb_start,
                end=TODAY,
                freq='MS'
            ).map(lambda d: d.replace(day=21))

            rebalance_dates = []
            for cal_dt in cal_21sts:
                snapped = _next_trading_day_on_or_after(cal_dt)
                if snapped is not None and snapped >= eleventh_list_date:
                    rebalance_dates.append(snapped)

            rebalance_dates = sorted(set(rebalance_dates))

            if not rebalance_dates:
                log(f"   ⚠️  {TOP10_LABEL}: no valid rebalance dates found, skipped.")
            else:
                first_rb         = rebalance_dates[0]
                top10_tdays      = idx_arr[idx_arr >= first_rb]
                index_values     = pd.Series(np.nan, index=top10_tdays)
                index_level      = 100.0

                for rb_idx, rb_date in enumerate(rebalance_dates):
                    if rb_idx + 1 < len(rebalance_dates):
                        next_rb = rebalance_dates[rb_idx + 1]
                    else:
                        next_rb = top10_tdays[-1] + pd.Timedelta(days=1)

                    period_days = top10_tdays[
                        (top10_tdays >= rb_date) & (top10_tdays < next_rb)
                    ]
                    if len(period_days) == 0:
                        continue

                    eligible = top10_universe[
                        top10_universe['Trade Date'] <= rb_date
                    ].copy()
                    eligible = eligible[eligible['Symbol'].isin(hist_data.columns)]

                    if eligible.empty:
                        continue

                    rb_snap = _snap_date(rb_date)
                    if rb_snap is None:
                        continue

                    eligible = eligible.copy()
                    eligible['rb_price'] = eligible['Symbol'].map(
                        lambda s: hist_data.loc[rb_snap, s]
                        if s in hist_data.columns and rb_snap in hist_data.index
                        else np.nan
                    )
                    eligible = eligible.dropna(subset=['rb_price'])
                    eligible = eligible[eligible['rb_price'] > 0]

                    if eligible.empty:
                        continue

                    eligible['score'] = eligible['Quant1'] * eligible['rb_price']
                    top10_sel   = eligible.nlargest(10, 'score')
                    syms10      = top10_sel['Symbol'].tolist()

                    if not syms10:
                        continue

                    avail_syms  = [s for s in syms10 if s in hist_data.columns]
                    px_period   = hist_data.loc[period_days, avail_syms].ffill().bfill()

                    if px_period.empty:
                        continue

                    period_prices = px_period.values
                    first_prices  = period_prices[0, :]

                    valid_mask = first_prices > 0
                    if valid_mask.sum() == 0:
                        continue

                    units         = np.where(valid_mask, 1.0 / (first_prices * valid_mask.sum()), 0.0)
                    port_vals     = period_prices.dot(units)
                    port_norm     = port_vals / port_vals[0] * index_level

                    index_values.loc[period_days] = port_norm
                    index_level = port_norm[-1]

                index_values = index_values.dropna()

                if not index_values.empty:
                    all_frames[TOP10_LABEL] = pd.DataFrame({
                        TOP10_LABEL:            index_values.round(2),
                        f'{TOP10_LABEL} Chg%': (index_values.pct_change() * 100).round(2)
                    })
                    last_rb   = rebalance_dates[-1]
                    last_elig = top10_universe[
                        top10_universe['Trade Date'] <= last_rb
                    ].copy()
                    last_elig = last_elig[last_elig['Symbol'].isin(hist_data.columns)]
                    last_rb_snap = _snap_date(last_rb)
                    if last_rb_snap is not None and not last_elig.empty:
                        last_elig['rb_price'] = last_elig['Symbol'].map(
                            lambda s: hist_data.loc[last_rb_snap, s]
                            if s in hist_data.columns and last_rb_snap in hist_data.index
                            else np.nan
                        )
                        last_elig = last_elig.dropna(subset=['rb_price'])
                        last_elig = last_elig[last_elig['rb_price'] > 0]
                        last_elig['score'] = last_elig['Quant1'] * last_elig['rb_price']
                        last_top10 = last_elig.nlargest(10, 'score')['Symbol'].tolist()
                    else:
                        last_top10 = []
                    w_snap = (pd.Series(1.0 / len(last_top10), index=last_top10)
                              if last_top10 else pd.Series(dtype=float))
                    weights_snapshot[TOP10_LABEL] = w_snap
                    log(f"   ✅ {TOP10_LABEL}: {len(rebalance_dates)} rebalances | "
                        f"{len(index_values)} days | latest = {index_values.iloc[-1]:.2f}")
                else:
                    log(f"   ⚠️  {TOP10_LABEL}: index is empty after calculation, skipped.")
    except Exception as e:
        log(f"   ⚠️  {TOP10_LABEL}: error — {e}")

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

    return all_frames, output_df, weights_snapshot, board_map, short_name_map, future_ipos, stock_price_df, delist_candidates


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
        self.stock_symbol_var = tk.StringVar(value='')
        self.input_file      = tk.StringVar(value=csv_path or DEFAULT_INPUT_FILE)
        self.output_file     = tk.StringVar(value=DEFAULT_OUTPUT_CSV)
        self.prefer_online   = tk.BooleanVar(value=True)
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

        self._build_ui()

        if preloaded is not None:
            if len(preloaded) == 8:
                frames, df, wsnap, bmap, snames, future_ipos, stock_price_df, delist_candidates = preloaded
            elif len(preloaded) == 7:
                frames, df, wsnap, bmap, snames, future_ipos, stock_price_df = preloaded
                delist_candidates = pd.DataFrame()
            elif len(preloaded) == 6:
                frames, df, wsnap, bmap, snames, future_ipos = preloaded
                stock_price_df = pd.DataFrame()
                delist_candidates = pd.DataFrame()
            else:
                frames, df, wsnap, bmap, snames = preloaded
                future_ipos = pd.DataFrame()
                stock_price_df = pd.DataFrame()
                delist_candidates = pd.DataFrame()
            self.all_frames        = frames
            self.output_df         = df
            self.weights_snapshot  = wsnap
            self.board_map         = bmap
            self.short_name_map    = snames
            self.future_ipos       = future_ipos
            self.stock_price_df    = stock_price_df
            self.delist_candidates = delist_candidates
            self.after(100, self._on_load_done)

    def _build_ui(self):
        top = tk.Frame(self, bg='#0d1117', pady=6)
        top.pack(fill='x', padx=10)

        tk.Label(top, text="MYIPO Index Dashboard",
                 bg='#0d1117', fg='#00C9FF',
                 font=('Segoe UI', 16, 'bold')).pack(side='left')

        tk.Label(top, text=f"  {TODAY.strftime('%d %b %Y')}",
                 bg='#0d1117', fg='#888', font=('Segoe UI', 10)).pack(side='left')

        tk.Button(top, text='⟳  Refresh Data', command=self._run_refresh,
                  bg='#00C9FF', fg='#0d1117', font=('Segoe UI', 10, 'bold'),
                  relief='flat', padx=12, pady=4, cursor='hand2').pack(side='right', padx=4)

        tk.Button(top, text='💼  MYFOLIO', command=self._open_myfolio,
                  bg='#58A6FF', fg='#0d1117', font=('Segoe UI', 10, 'bold'),
                  relief='flat', padx=12, pady=4, cursor='hand2').pack(side='right', padx=4)

        tk.Button(top, text='🎲  LottoStock', command=self._open_lottostock,
                  bg='#f0c040', fg='#0d1117', font=('Segoe UI', 10, 'bold'),
                  relief='flat', padx=12, pady=4, cursor='hand2').pack(side='right', padx=4)

        tk.Button(top, text='💾  Export CSV', command=self._export_csv,
                  bg='#238636', fg='white', font=('Segoe UI', 10, 'bold'),
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
        _make_section(top_block, 'Top-10 (Monthly Rebalanced)',
                      [TOP10_LABEL], accent='#FFD700')
        _make_section(top_block, 'Fund (Unit Trust, Quarterly Dist.)',
                      FUND_LABELS, accent='#00E5A0')

        btn_row = tk.Frame(top_block, bg='#161b22')
        btn_row.pack(fill='x', padx=8, pady=6)
        for _txt, _cmd, _fg in [
            ('All',      self._select_all,     '#cdd9e5'),
            ('None',     self._deselect_all,    '#cdd9e5'),
            ('Core',     self._select_core,     '#cdd9e5'),
            ('Y-Series', self._select_yseries,  '#cdd9e5'),
            ('TR',       self._select_trseries, '#7FECFF'),
            ('Top10',    self._select_top10,    '#FFD700'),
            ('Fund',     self._select_fund,     '#00E5A0'),
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

        self.future_header_var = tk.StringVar(value='Next IPO: loading from database…')
        self.future_header = tk.Label(
            right, textvariable=self.future_header_var,
            bg='#101820', fg='#cdd9e5', anchor='w', padx=10, pady=5,
            font=('Segoe UI', 9), relief='flat'
        )
        self.future_header.pack(fill='x', pady=(0, 6))
        self._future_headline_full  = ''
        self._future_headline_pos   = 0
        self._future_headline_width = 190
        self._future_item_idx       = 0
        self._future_item_signature = ''
        self._future_tick_count     = 0
        self.after(1000, self._tick_future_header)

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

        self.status_var = tk.StringVar(value='Ready — load a CSV to begin.')
        status_bar = tk.Label(self, textvariable=self.status_var,
                              bg='#161b22', fg='#888', font=('Segoe UI', 9),
                              anchor='w', padx=10)
        status_bar.pack(fill='x', side='bottom')

        self.notebook.bind('<<NotebookTabChanged>>', lambda e: self._redraw_chart())

    # ── MYFOLIO launcher ──────────────────────────────────────────────────────
    def _open_myfolio(self):
        """Open the MYFOLIO Portfolio Command Center as a child window."""
        MYFOLIOWindow(self, preloaded_rows=self.newpor_rows,
                      preloaded_portfolios=self.newpor_portfolios)

    def _open_lottostock(self):
        """Open LottoStock, linked live to MYFOLIO's SWAY1-MYR holdings."""
        LottoStockWindow(self, preloaded_rows=self.newpor_rows,
                          preloaded_portfolios=self.newpor_portfolios,
                          preloaded_validation=self.newpor_validation)

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

    def _build_line_tab(self):
        self.fig_line = Figure(figsize=(10, 5.5), facecolor='#0d1117')
        self.ax_line  = self.fig_line.add_subplot(111)
        self._style_ax(self.ax_line)
        self.canvas_line = FigureCanvasTkAgg(self.fig_line, self.tab_line)
        self.canvas_line.get_tk_widget().pack(fill='both', expand=True)
        tb = NavigationToolbar2Tk(self.canvas_line, self.tab_line)
        tb.config(background='#161b22')
        tb.update()

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
            )
            frames, df, wsnap, bmap, snames, future_ipos, stock_price_df, delist_candidates = result
            self.all_frames        = frames
            self.output_df         = df
            self.weights_snapshot  = wsnap
            self.board_map         = bmap
            self.short_name_map    = snames
            self.future_ipos       = future_ipos
            self.stock_price_df    = stock_price_df
            self.delist_candidates = delist_candidates
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

        now  = self._now_myt()
        rows = []
        signatures = []

        if self.future_ipos is not None and not self.future_ipos.empty:
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

        if hasattr(self, 'delist_candidates') and self.delist_candidates is not None and not self.delist_candidates.empty:
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
            self.future_header_var.set('Next IPO: no live D-1/D0 countdown or D365 delist item in database.')
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
        ax.set_ylabel('Price (RM)', color='#888')
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
            f'Price: RM{last:.3f} | Change: RM{chg_abs:+.3f} | Return: {ret:+.2f}%'
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

        selected = self._selected_labels()
        period   = self.period_var.get()

        if not selected:
            ax.set_title('No indices selected', color='#888')
            self.canvas_line.draw()
            return

        # Funds are priced in RM (NAV per unit, launched at RM0.2500) — an
        # entirely different scale to the base-100 indices. Split them out
        # and plot funds on a secondary right-hand axis, dashed, in RM.
        index_labels = [lbl for lbl in selected if lbl not in FUND_LABELS]
        fund_labels  = [lbl for lbl in selected if lbl in FUND_LABELS]

        any_index_plotted = False
        for lbl in index_labels:
            series = self.all_frames[lbl][lbl].dropna()
            if series.empty: continue
            mask = self._get_period_mask(series.index)
            s    = series[mask]
            if s.empty: continue
            ax.plot(s.index, s.values, label=lbl,
                    color=COLOR_MAP[lbl], linewidth=1.5)
            any_index_plotted = True

            if self.show_datapoints_var.get():
                idxs   = self._pick_annotation_indices(len(s))
                dates  = s.index[idxs]
                values = s.values[idxs]
                ax.scatter(dates, values, color=COLOR_MAP[lbl], s=14, zorder=5)
                for d, v in zip(dates, values):
                    ax.annotate(
                        f'{v:.1f}',
                        xy=(d, v), xytext=(0, 5), textcoords='offset points',
                        ha='center', va='bottom', fontsize=5.5, color='#cdd9e5',
                        bbox=dict(boxstyle='round,pad=0.12', fc='#161b22', ec='none', alpha=0.65)
                    )

        if any_index_plotted:
            ax.axhline(100, color='#333', linewidth=0.8, linestyle='--')
            ax.set_ylabel('Index Value (Base 100)', color='#888')

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
            for lbl in fund_labels:
                series = self.all_frames[lbl][lbl].dropna()   # NAV per unit, RM
                if series.empty: continue
                mask = self._get_period_mask(series.index)
                s    = series[mask]
                if s.empty: continue
                ax_fund.plot(s.index, s.values, label=f'{lbl}  (RM NAV)',
                            color=COLOR_MAP[lbl], linewidth=1.5, linestyle='--')
                any_fund_plotted = True

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
                ax_fund.axhline(FUND_PAR_NAV, color='#00E5A0', linewidth=0.8, linestyle=':')
                ax_fund.set_ylabel('Fund NAV (RM per unit)', color='#00E5A0')

        # ── Title reflects what's actually shown ──────────────────────────────
        if index_labels and fund_labels:
            title = f'MYIPO Index (Base 100) vs Fund NAV (RM)  [{period}]'
        elif fund_labels:
            title = f'MYIPO+ Fund Series — NAV per Unit (RM)  [{period}]'
        else:
            title = f'MYIPO Index — Cumulative (Base 100)  [{period}]'
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
        ax.set_title(f'Total Return % — {self.period_var.get()}',
                     color='#cdd9e5', fontsize=11, pad=10)
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
                elif lbl in FUND_LABELS:
                    vals.append(f'{v:.4f}')   # fund NAV, RM, 4dp
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

            mask = self._get_period_mask(series.index)
            s    = series[mask].dropna()
            if len(s) < 2:
                s = series.tail(2)
            period_ret = (s.iloc[-1] / s.iloc[0] - 1) * 100 if len(s) >= 2 and s.iloc[0] != 0 else 0.0

            tag = 'pos' if period_ret >= 0 else 'neg'
            is_fund   = lbl in FUND_LABELS
            last_disp = f'RM{last:.4f}' if is_fund else f'{last:.2f}'

            units_disp = '—'
            if is_fund:
                units_series = self.all_frames[lbl].get(f'{lbl} Units', pd.Series(dtype=float)).dropna()
                if not units_series.empty:
                    units_disp = f'{units_series.iloc[-1]:,.0f}'

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
        if TOP10_LABEL in self.check_vars:
            self.check_vars[TOP10_LABEL].set(True)
        self._redraw_chart()

    def _select_fund(self):
        self._deselect_all()
        for lbl in FUND_LABELS:
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
MYFOLIO_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vQaZ4yBz0KhsA5Aj2sAehPd3nnMZjLF7RBs4bQHsxYdhrJW2NQ0u_kQc8_cXiDWxQMsRc2o-dvaSGlF"
    "/pub?gid=0&single=true&output=csv"
)

# ── Transaction-ledger schema column names (after _mf_norm) ──────────────────
# Headers: Date | Transaction Type | AssetCode | Currency | Unit | Value | Amount
# "Value" = price per unit; "Amount" = total MV of the row (Unit × Value)
# Cash asset code: USD-CASH  (or MYR-CASH for MYR side)
# Transaction Types: Deposit, Withdraw, Buy, Sell, Fees, Dividend (future)
_NEWPOR_CCY_MAP = {
    "USD": ("SWAY1", "SWAY"),
    "MYR": ("SWAY1-MYR", "SWAY"),
}

# ── Price cache (shared with existing yf calls) ───────────────────────────────
_MF_PRICE_CACHE: dict = {}

def _mf_ticker_for_market(ticker: str, market: str) -> str:
    ticker = ticker.upper().strip()
    if market.upper() == "MY" and not ticker.endswith(".KL"):
        return f"{ticker}.KL"
    return ticker

def _mf_as_price(value):
    try:
        p = float(value)
        return p if p > 0 else None
    except (TypeError, ValueError):
        return None

def _mf_get_price(ticker: str, market: str, fallback=None):
    lookup = _mf_ticker_for_market(ticker, market)
    if lookup in _MF_PRICE_CACHE:
        return _MF_PRICE_CACHE[lookup]
    try:
        obj = yf.Ticker(lookup)
        fast = obj.fast_info
        for field in ("last_price", "lastPrice", "regular_market_price",
                      "previous_close", "previousClose"):
            p = _mf_as_price(getattr(fast, field, None))
            if p:
                _MF_PRICE_CACHE[lookup] = p
                return p
        hist = obj.history(period="5d", interval="1d")
        if hist is not None and not hist.empty and "Close" in hist:
            close = hist["Close"].dropna()
            if not close.empty:
                p = _mf_as_price(close.iloc[-1])
                if p:
                    _MF_PRICE_CACHE[lookup] = p
                    return p
        info = obj.info
        for field in ("currentPrice", "regularMarketPrice",
                      "previousClose", "navPrice"):
            p = _mf_as_price(info.get(field))
            if p:
                _MF_PRICE_CACHE[lookup] = p
                return p
    except Exception:
        pass
    return fallback

def _mf_fmt_price(v)  -> str: return f"{v:,.4f}"  if v is not None else "N/A"
def _mf_fmt_money(v)  -> str: return f"{v:,.2f}"  if v is not None else "N/A"
def _mf_fmt_signed(v) -> str: return f"{v:+,.2f}" if v is not None else "N/A"
def _mf_fmt_pct(v)    -> str: return f"{v:+.2f}%" if v is not None else "N/A"

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

_NEWPOR_LOCAL_CACHE_NAME = 'newpor_cache.csv'   # saved alongside the script, like teet_cache.csv

def _mf_fetch_online_chunked(url: str = MYFOLIO_CSV_URL, log_cb=None, chunk_size: int = 200):
    """
    Download the NEWPOR transaction-ledger CSV in row-chunks, mirroring the
    same chunked-download pattern MYIPO uses for its price history (CHUNK=50
    tickers per yf.download batch). Here the 'chunk' is a slice of CSV lines
    rather than tickers, since NEWPOR is a flat row-based sheet, not a
    per-symbol price matrix — but the behaviour (logged progress per chunk,
    same network call shape) mirrors it directly.

    Returns (raw_csv_text: str, row_count: int) or raises on failure.
    """
    def log(m):
        if log_cb: log_cb(m)

    log('🌐  Connecting to NEWPOR database…')
    with _mf_urlopen(url, timeout=15) as resp:
        raw = resp.read().decode("utf-8-sig")

    lines = raw.splitlines()
    if not lines:
        return raw, 0
    header, body = lines[0], lines[1:]

    n_chunks = max(1, -(-len(body) // chunk_size))   # ceiling division
    log(f'⬇  Processing NEWPOR ledger in {n_chunks} chunk(s) ({len(body)} rows)...')
    rebuilt = [header]
    for i in range(0, len(body), chunk_size):
        chunk_num = i // chunk_size + 1
        chunk = body[i:i + chunk_size]
        log(f'   Chunk {chunk_num}/{n_chunks} ({len(chunk)} rows)...')
        rebuilt.extend(chunk)

    log('✅  NEWPOR database fetched successfully.')
    return "\n".join(rebuilt), len(body)


def _mf_load_csv(url: str = MYFOLIO_CSV_URL, log_cb=None, prefer_online: bool = True) -> list:
    """
    Download the NEWPOR transaction ledger CSV — chunked, with progress
    logging — and hash-compare against a local cache file, mirroring
    MYIPO's resolve_input()/fetch_online_db() pattern exactly:
        * Online differs from cache (or no cache yet) -> save as new cache, use it.
        * Online matches cache                         -> skip rewrite, use cache as-is.
        * Online unreachable                           -> fall back to whatever
                                                           cache exists, if any.
    Returns a list of normalised row dicts.
    """
    import hashlib

    def log(m):
        if log_cb: log_cb(m)

    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _NEWPOR_LOCAL_CACHE_NAME)

    raw_text = None
    if prefer_online:
        try:
            raw_text, row_count = _mf_fetch_online_chunked(url, log_cb=log_cb)
        except Exception as e:
            log(f'⚠️  NEWPOR online fetch failed ({e}). Falling back to local cache…')
            raw_text = None

    if raw_text is not None:
        online_bytes = raw_text.encode('utf-8')
        online_hash  = hashlib.sha256(online_bytes).hexdigest()

        local_hash = None
        if os.path.exists(cache_path):
            try:
                with open(cache_path, 'rb') as f:
                    local_hash = hashlib.sha256(f.read()).hexdigest()
            except Exception as e:
                log(f'⚠️  Could not read existing NEWPOR cache for comparison: {e}')

        if local_hash == online_hash and local_hash is not None:
            log('✓  NEWPOR data unchanged from local cache.')
        else:
            try:
                with open(cache_path, 'wb') as f:
                    f.write(online_bytes)
                log(f'💾  NEWPOR data updated — cached to {cache_path}')
            except Exception as e:
                log(f'⚠️  Could not write NEWPOR cache: {e}')
    else:
        # Online unavailable — fall back to the local cache if it exists.
        if os.path.exists(cache_path):
            log(f'📂  Loading NEWPOR from local cache: {cache_path}')
            with open(cache_path, 'r', encoding='utf-8-sig') as f:
                raw_text = f.read()
        else:
            raise _mf_URLError('NEWPOR online fetch failed and no local cache exists yet.')

    reader = csv.DictReader(io.StringIO(raw_text))
    rows = []
    for row in reader:
        norm = {_mf_norm(k): v.strip() for k, v in row.items() if k}
        # skip blank / trailing rows
        if not norm.get(_NC_ASSET) and not norm.get(_NC_TYPE):
            continue
        rows.append(norm)
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
        market         = "MY" if ccy == "MYR" else "US"

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
            sb_bot, columns=('Ticker', 'Cost', 'MktVal', 'P/L%'),
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
        for col, w, anc in [('Ticker', 88, 'w'), ('Cost', 60, 'e'),
                             ('MktVal', 60, 'e'), ('P/L%', 56, 'e')]:
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
        frame = tk.Frame(self.tab_table, bg='#0d1117')
        frame.pack(fill='both', expand=True)
        hold_cols = ('Ticker','Market','Ccy','Units','Avg Cost',
                     'Cost Value','Price','Mkt Value','P/L','P/L %','Date')
        self.hold_tree = ttk.Treeview(frame, columns=hold_cols, show='headings')
        cw = {'Ticker':85,'Market':55,'Ccy':48,'Units':72,'Avg Cost':80,
              'Cost Value':85,'Price':78,'Mkt Value':85,'P/L':82,'P/L %':66,'Date':88}
        for col in hold_cols:
            self.hold_tree.heading(col, text=col)
            self.hold_tree.column(col, width=cw.get(col,80),
                                  anchor='w' if col in ('Ticker','Date') else 'e',
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
            costv  = item['cost_value']
            price  = _mf_get_price(ticker, item['market'])
            mv     = item['shares'] * price if price else None
            pl_pct = ((mv - costv) / costv * 100) if mv and costv else None
            tag    = 'gain' if pl_pct is not None and pl_pct >= 0 else 'loss'
            self.summary_tree.insert('', 'end', tags=(tag,),
                values=(ticker, f'{costv:,.2f}',
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

        shares_s: dict = defaultdict(float)
        cost_s:   dict = defaultdict(float)
        snap_dates = []; snap_cost = []
        running_cost = 0.0; txn_idx = 0
        unique_dates = sorted(set(t[0] for t in all_txns))
        for snap_d in unique_dates:
            while txn_idx < len(all_txns) and all_txns[txn_idx][0] <= snap_d:
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
            snap_dates.append(snap_d); snap_cost.append(running_cost)

        # Period filter
        today  = _dt.date.today()
        period = self._period_var.get()
        cutoff = {'1D': today-_dt.timedelta(days=1),
                  '5D': today-_dt.timedelta(days=5),
                  '1M': today-_dt.timedelta(days=30),
                  '3M': today-_dt.timedelta(days=91),
                  '6M': today-_dt.timedelta(days=182),
                  'YTD': _dt.date(today.year, 1, 1),
                  '1Y': today-_dt.timedelta(days=365)}.get(period)
        if cutoff:
            pairs = [(d, v) for d, v in zip(snap_dates, snap_cost) if d >= cutoff]
            if pairs: snap_dates, snap_cost = zip(*pairs)
            else:     snap_dates, snap_cost = [], []

        live_mv = 0.0
        for ticker, shares in shares_s.items():
            if shares <= 0: continue
            mkt = 'MY' if ticker.endswith('.KL') else 'US'
            p   = _mf_get_price(ticker, mkt)
            if p: live_mv += shares * p

        import matplotlib.dates as mdates
        if snap_dates:
            dt_dates = [_dt.datetime.combine(d, _dt.time()) for d in snap_dates]
            ax.step(dt_dates, snap_cost, where='post', color='#8B949E',
                    linewidth=1.8, label='Invested Cost', linestyle='--')
            ax.fill_between(dt_dates, snap_cost, alpha=0.06, color='#8B949E', step='post')
            ax.plot(dt_dates, snap_cost, '.', color='#58A6FF', markersize=5, zorder=4)

        if live_mv > 0:
            today_dt  = _dt.datetime.combine(today, _dt.time())
            pl_today  = live_mv - running_cost
            dot_color = '#3FB950' if pl_today >= 0 else '#F85149'
            ax.plot([today_dt], [live_mv], 'o', color=dot_color, markersize=10,
                    label=f'Live MV  {ccy0} {live_mv:,.2f}', zorder=5)
            ax.annotate(f'{ccy0} {live_mv:,.2f}\n({pl_today:+,.2f})',
                        xy=(today_dt, live_mv), xytext=(-65, 20),
                        textcoords='offset points', color=dot_color, fontsize=8,
                        arrowprops=dict(arrowstyle='->', color=dot_color, lw=1))
            if snap_dates:
                ax.vlines(today_dt, running_cost, live_mv,
                          colors=dot_color, linewidth=1.4, linestyles=':')

        ax.set_title(f'Portfolio History — {port["code"]}  [{ccy0}]  [{period}]',
                     color='#cdd9e5', fontsize=11, pad=12)
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
        for ticker, item in sorted(port['holdings'].items()):
            if item.get('_is_cash'):
                continue   # cash position trimmed from SCA views
            units = item['shares']; avg_cost = item['avg_cost']; costv = item['cost_value']
            price = _mf_get_price(ticker, item['market'])
            mv    = units * price if price else None
            pl    = mv - costv if mv is not None else None
            pl_pct = (pl / costv * 100) if pl is not None and costv else None
            tag   = 'gain' if pl is not None and pl >= 0 else 'loss'
            self.hold_tree.insert('', 'end', tags=(tag,),
                values=(ticker, item['market'], item['currency'],
                        f'{units:,.4f}', f'{avg_cost:,.4f}',
                        _mf_fmt_money(costv), _mf_fmt_price(price),
                        _mf_fmt_money(mv),
                        _mf_fmt_signed(pl) if pl is not None else 'N/A',
                        _mf_fmt_pct(pl_pct) if pl_pct is not None else 'N/A',
                        item.get('date', '')))

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
    Derive (owned_set, starting_units) from an already-built portfolios dict
    (e.g. preloaded at SplashScreen startup) without re-fetching anything.
    Matched by bare 4-digit code (MYFOLIO stores tickers as 'CODE.KL').
    """
    if not ports:
        return set(), {}
    myr_port = ports.get("SWAY1-MYR")
    if not myr_port:
        return set(), {}

    owned = set()
    units = {}
    for ticker, item in myr_port["holdings"].items():
        if item.get("_is_cash"):
            continue
        code = ticker[:-3] if ticker.upper().endswith(".KL") else ticker
        if item["shares"] > 0:
            owned.add(code)
            units[code] = item["shares"]
    return owned, units


def lotto_pull_myfolio_state(log_cb=None):
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

    owned, units = lotto_derive_owned_from_portfolios(ports)
    return owned, units, ports, validation


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
    """Owned stocks get a tuned-down PickMax ceiling based on units already held
    (live from MYFOLIO). cap = 99 x (0.8 / owned_units), clamped to max 99.
    No units on record -> full 99."""
    if code not in owned:
        return random.randint(1, 99)
    u = units_map.get(code, 0)
    if u <= 0:
        return random.randint(1, 99)
    cap = min(99, 99 * (0.8 / u))
    cap_int = max(1, int(cap))
    return random.randint(1, cap_int)


def lotto_get_extra_buy(code: str, owned: set) -> tuple:
    """Returns (total_extra, breakdown_list) for the buy-extra-units rule."""
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


def lotto_fetch_live_price(code: str):
    """Fetch live price via yfinance for a Bursa Malaysia ticker (CODE.KL)."""
    symbol = f"{code}.KL"
    try:
        ticker = yf.Ticker(symbol)
        fast = ticker.fast_info
        price = fast.get("lastPrice") or fast.get("last_price")
        if price is None:
            hist = ticker.history(period="1d")
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
        self.title("LottoStock — Bursa Malaysia Stock Lottery")
        self.geometry("980x900")
        self.configure(bg=LOTTO_BG)
        self.minsize(820, 700)

        # ── MYFOLIO-linked state ──
        self.owned = set()
        self.starting_units = {}
        self.myfolio_portfolios = None   # raw dict from _mf_build_portfolios
        self._myfolio_loading = False
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
            owned, units = lotto_derive_owned_from_portfolios(preloaded_portfolios)
            self._on_myfolio_synced(owned, units, preloaded_portfolios,
                                    preloaded_validation, initial=True)
        else:
            self._refresh_myfolio_link(initial=True)

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
                  command=lambda: self._refresh_myfolio_link(initial=False)
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
        canvas.create_window((0, 0), window=self.body, anchor="nw", width=960)
        self.body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        # ── Header ──
        hdr = tk.Frame(self.body, bg=LOTTO_BG)
        hdr.pack(fill="x", pady=(24, 8))
        tk.Label(hdr, text="BURSA MALAYSIA", font=("Courier New", 10, "bold"),
                 fg=LOTTO_GOLD, bg=LOTTO_BG).pack()
        tk.Label(hdr, text="LOTTOSTOCK", font=("Impact", 36),
                 fg=LOTTO_GOLD, bg=LOTTO_BG).pack(pady=(4, 0))
        tk.Label(hdr, text="PickMax Randomizer · Highest Score Wins", font=("Courier New", 9),
                 fg=LOTTO_GREY, bg=LOTTO_BG).pack()

        # ── MYFOLIO link status ──
        link_row = tk.Frame(self.body, bg=LOTTO_BG)
        link_row.pack(pady=(10, 0))
        self.myfolio_link_var = tk.StringVar(value="🔗 MYFOLIO: connecting…")
        tk.Label(link_row, textvariable=self.myfolio_link_var, font=("Courier New", 9),
                 fg="#58A6FF", bg=LOTTO_BG).pack(side="left", padx=6)
        tk.Button(link_row, text="↻ Resync MYFOLIO", font=("Courier New", 8),
                  bg=LOTTO_PANEL, fg="#58A6FF", relief="flat", bd=1, cursor="hand2",
                  command=lambda: self._refresh_myfolio_link(initial=False)).pack(side="left", padx=6)

        # ── Legend ──
        legend = tk.Frame(self.body, bg=LOTTO_BG)
        legend.pack(pady=(12, 16))
        for text, color in [("● Owned (MYFOLIO) → +0.01", LOTTO_GREEN), ("● Retail → +0.02", "#40c0f0"),
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

    def _refresh_myfolio_link(self, initial=False):
        if self._myfolio_loading:
            return
        self._myfolio_loading = True
        self.myfolio_link_var.set("🔗 MYFOLIO: syncing…")
        if hasattr(self, '_lock_text_var'):
            self._lock_text_var.set("Checking database…")

        def _log(msg):
            self.after(0, lambda: (
                self.myfolio_link_var.set(f"🔗 MYFOLIO: {msg}"),
                self._lock_text_var.set(msg) if hasattr(self, '_lock_text_var') else None,
            ))

        def worker():
            owned, units, ports, validation = lotto_pull_myfolio_state(log_cb=_log)
            self.after(0, lambda: self._on_myfolio_synced(owned, units, ports, validation, initial))

        threading.Thread(target=worker, daemon=True).start()

    def _on_myfolio_synced(self, owned, units, ports, validation, initial):
        self._myfolio_loading = False
        self._validation = validation

        if not validation or not validation.get("ok"):
            # Validation failed — lock the app, no draws/buys allowed.
            self._update_lock_text(validation)
            self._show_lock_screen()
            self.myfolio_link_var.set("🔗 MYFOLIO: ❌ validation failed — locked")
            return

        # Validation passed — unlock and apply the synced data.
        self.owned = owned
        self.starting_units = units
        self.myfolio_portfolios = ports
        self._hide_lock_screen()

        if not owned:
            self.myfolio_link_var.set("🔗 MYFOLIO: connected — no MYR holdings found yet")
        else:
            self.myfolio_link_var.set(
                f"🔗 MYFOLIO: linked — {len(owned)} owned MYR stocks "
                f"({', '.join(sorted(owned)[:6])}{'…' if len(owned) > 6 else ''})"
            )
        if validation.get("warnings"):
            self.myfolio_link_var.set(
                self.myfolio_link_var.get() + f"  ⚠ {len(validation['warnings'])} warning(s)"
            )

        if not initial:
            self._reset_pool()
        self._refresh_table()
        self._build_road_section()

    # ──────────────────────────────────────────────────────────
    # ROAD TO 100 SECTION
    # ──────────────────────────────────────────────────────────

    def _build_road_section(self):
        for w in self.road_section.winfo_children():
            w.destroy()

        # ── Inline target-switch prompt — replaces the old askyesno popup ────
        if self._road_switch_pending:
            switch_card = tk.Frame(self.road_section, bg="#1f1a0f", highlightbackground=LOTTO_GOLD,
                                    highlightthickness=1, padx=16, pady=12)
            switch_card.pack(fill="x", pady=(0, 10))
            cur_name = self.road_target["name"] if self.road_target else "—"
            new_code = self._road_switch_pending["code"]
            new_name = self._road_switch_pending["name"]
            tk.Label(switch_card, text="🔀 New draw winner differs from your Road to 100 target",
                     font=("Courier New", 9, "bold"), fg=LOTTO_GOLD, bg="#1f1a0f", anchor="w").pack(fill="x")
            tk.Label(switch_card,
                     text=f"You haven't bought into {cur_name} yet. Switch target to "
                          f"{new_code} · {new_name}?",
                     font=("Courier New", 8), fg="#ccc", bg="#1f1a0f", wraplength=820,
                     justify="left", anchor="w").pack(fill="x", pady=(2, 8))
            sw_btn_row = tk.Frame(switch_card, bg="#1f1a0f")
            sw_btn_row.pack(fill="x")
            tk.Button(sw_btn_row, text=f"✅ Switch to {new_code}", font=("Courier New", 9, "bold"),
                      bg=LOTTO_GOLD, fg="#000", relief="flat", padx=10, pady=6, cursor="hand2",
                      command=self._accept_road_switch).pack(side="left", expand=True, fill="x", padx=(0, 4))
            tk.Button(sw_btn_row, text=f"✖ Keep {cur_name}", font=("Courier New", 9), bg="#1a1a1a",
                      fg=LOTTO_GREY, relief="flat", padx=10, pady=6, cursor="hand2",
                      command=self._decline_road_switch).pack(side="left", expand=True, fill="x", padx=(4, 0))

        if not self.road_target:
            return

        locked = len(self.road_log) > 0
        title_text = "🛣️ ROAD TO 100 UNITS " + ("🔒" if locked else "🟡 PARTIAL MATCH")
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
            tk.Label(right, text="LIVE PRICE", font=("Courier New", 8), fg=LOTTO_DGREY, bg=LOTTO_PANEL).pack(anchor="e")
            tk.Label(right, text=f"RM{self.road_price:.3f}", font=("Courier New", 16, "bold"),
                     fg=LOTTO_TEAL, bg=LOTTO_PANEL).pack(anchor="e")
        else:
            tk.Label(right, text="price unavailable", font=("Courier New", 9, "italic"),
                     fg=LOTTO_GREY, bg=LOTTO_PANEL).pack(anchor="e")
        tk.Button(right, text="↻ refresh", font=("Courier New", 8), bg=LOTTO_PANEL, fg=LOTTO_GREY,
                  relief="flat", bd=1, cursor="hand2",
                  command=self.refresh_road_price).pack(anchor="e", pady=(4, 0))

        # Progress — head start now pulled LIVE from MYFOLIO
        head_start = self.starting_units.get(self.road_target["code"], 0)
        total_units = head_start + sum(l["units"] for l in self.road_log)
        total_saved = sum(l["amountMYR"] for l in self.road_log)

        prog_frame = tk.Frame(card, bg=LOTTO_PANEL)
        prog_frame.pack(fill="x", pady=(14, 4))
        lbl_row = tk.Frame(prog_frame, bg=LOTTO_PANEL)
        lbl_row.pack(fill="x")
        tk.Label(lbl_row, text=f"{total_units:.4f} / 100 units", font=("Courier New", 10),
                 fg="#ccc", bg=LOTTO_PANEL).pack(side="left")
        tk.Label(lbl_row, text=f"RM{total_saved:.2f} saved", font=("Courier New", 10),
                 fg="#ccc", bg=LOTTO_PANEL).pack(side="right")

        if head_start > 0:
            tk.Label(prog_frame, text=f"🎁 Head start: {head_start:.4f} units (live from MYFOLIO SWAY1-MYR)",
                     font=("Courier New", 8), fg=LOTTO_GOLD, bg=LOTTO_PANEL, anchor="w").pack(fill="x", pady=(4, 0))

        bar_bg = tk.Frame(prog_frame, bg="#1a1a1a", height=10)
        bar_bg.pack(fill="x", pady=(6, 0))
        pct = min(100, total_units)
        bar_fg = tk.Frame(bar_bg, bg=LOTTO_GREEN, height=10, width=int(8.6 * pct))
        bar_fg.place(x=0, y=0, relheight=1)

        note = ("🔒 Target locked. Cannot change until a new transaction record is needed."
                if locked else
                "🟡 Partial match — not bought yet. Each draw may offer a new candidate "
                "until you save toward it to lock it in.")
        tk.Label(card, text=note, font=("Courier New", 8), fg=LOTTO_GREY, bg=LOTTO_PANEL,
                 wraplength=820, justify="left", anchor="w").pack(fill="x", pady=(10, 12))

        btn_label = "💰 Buy 1.5 Units" if (self.road_price and self.road_price > 10) else "💰 Save RM10"
        if not locked:
            btn_label += " & Lock Target"

        if self._road_buy_armed:
            # ── Inline confirmation step — replaces the old askyesno popup ──
            price = self.road_price
            if price and price > 10:
                units, amount = 1.5, round(1.5 * price, 2)
                confirm_msg = f"Price RM{price:.3f} exceeds RM10 — contribution capped at 1.5 units (≈RM{amount:.2f})"
            elif price:
                units, amount = round(10 / price, 4), 10.0
                confirm_msg = f"Confirm RM10 contribution (≈+{units:.4f} units @ RM{price:.3f})"
            else:
                confirm_msg = "Price still loading…"

            confirm_box = tk.Frame(card, bg="#0f1f18", highlightbackground=LOTTO_TEAL,
                                   highlightthickness=1, padx=12, pady=10)
            confirm_box.pack(fill="x")
            tk.Label(confirm_box, text=confirm_msg, font=("Courier New", 9),
                     fg="#ccc", bg="#0f1f18", wraplength=780, justify="left").pack(fill="x", pady=(0, 8))
            cbtn_row = tk.Frame(confirm_box, bg="#0f1f18")
            cbtn_row.pack(fill="x")
            tk.Button(cbtn_row, text="✅ Confirm Contribution", font=("Courier New", 9, "bold"),
                      bg=LOTTO_GREEN, fg="#000", relief="flat", padx=10, pady=6, cursor="hand2",
                      command=self.open_road_confirm).pack(side="left", expand=True, fill="x", padx=(0, 4))
            tk.Button(cbtn_row, text="✖ Cancel", font=("Courier New", 9), bg="#1a1a1a",
                      fg=LOTTO_GREY, relief="flat", padx=10, pady=6, cursor="hand2",
                      command=self._cancel_road_buy).pack(side="left", expand=True, fill="x", padx=(4, 0))
        else:
            tk.Button(card, text=btn_label, font=("Courier New", 11, "bold"), bg=LOTTO_GREEN, fg="#000",
                      relief="flat", padx=16, pady=8, cursor="hand2",
                      command=self._arm_road_buy).pack(fill="x")

        # Road log
        if self.road_log:
            log_frame = tk.Frame(self.road_section, bg=LOTTO_BG)
            log_frame.pack(fill="x", pady=(8, 0))
            for entry in self.road_log[:6]:
                row = tk.Frame(log_frame, bg="#1a1422", highlightbackground=LOTTO_PURPLE,
                                highlightthickness=1, padx=10, pady=4)
                row.pack(fill="x", pady=2)
                tag = " (capped)" if entry.get("capped") else ""
                tk.Label(row, text=f"RM{entry['amountMYR']:.2f}", font=("Courier New", 9, "bold"),
                         fg=LOTTO_GOLD, bg="#1a1422").pack(side="left")
                tk.Label(row, text=f"+{entry['units']:.4f} units @ RM{entry['priceAtBuy']:.3f}{tag}",
                         font=("Courier New", 9), fg="#ccc", bg="#1a1422").pack(side="left", padx=12)
                tk.Label(row, text=entry["date"], font=("Courier New", 8), fg=LOTTO_DGREY,
                         bg="#1a1422").pack(side="right")

    def refresh_road_price(self):
        if not self.road_target:
            return
        self.road_price_loading = True
        self._build_road_section()

        def worker():
            price = lotto_fetch_live_price(self.road_target["code"])
            self.road_price = price
            self.road_price_loading = False
            self.after(0, self._build_road_section)

        threading.Thread(target=worker, daemon=True).start()

    def set_candidate_target(self, code, name):
        self.road_target = {"code": code, "name": name}
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

        if price > 10:
            units = 1.5
            amount = round(1.5 * price, 2)
        else:
            units = round(10 / price, 4)
            amount = 10.0

        date_str = datetime.now().strftime("%m/%d/%Y")
        self.road_log.insert(0, {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "amountMYR": amount,
            "units": units,
            "priceAtBuy": price,
            "capped": price > 10,
        })
        # Feed back into MYFOLIO (in-memory ledger) — matched on both sides
        lotto_record_buy_to_myfolio(
            self.myfolio_portfolios, self.road_target["code"], units, price, date_str
        )
        self._road_buy_armed = False
        self._build_road_section()

    # ──────────────────────────────────────────────────────────
    # LOTTERY LOGIC
    # ──────────────────────────────────────────────────────────

    def _reset_pool(self):
        self.stocks = [
            {"code": c, "name": n, "pickmax": lotto_rand_pickmax_for(c, self.owned, self.starting_units),
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
        ticks = 18
        for i in range(ticks):
            for s in self.stocks:
                s["pickmax"] = lotto_rand_pickmax_for(s["code"], self.owned, self.starting_units)
                s["score"] = round(random.random() * s["pickmax"], 2)
            self.after(0, self._refresh_table)
            time_module.sleep(0.07)

        final = []
        for code, name in LOTTO_STOCK_LIST:
            pm = lotto_rand_pickmax_for(code, self.owned, self.starting_units)
            base = round(random.random() * pm, 4)
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
        self.owned.add(code)
        self.tx_history.insert(0, {
            "code": code, "name": stock["name"],
            "time": datetime.now().strftime("%H:%M:%S"), "bonus": 0.01,
        })

        # Feed back into MYFOLIO — match on both sides. Use live price if we
        # can get one quickly; if not, fall back to a nominal RM1.00/unit,1 unit
        # placeholder buy so the transaction still records (price can be
        # corrected later directly in the ledger).
        price = lotto_fetch_live_price(code)
        if price is None:
            price = 1.00
        units = 1.0
        date_str = datetime.now().strftime("%m/%d/%Y")
        lotto_record_buy_to_myfolio(self.myfolio_portfolios, code, units, price, date_str)
        # Keep local units_map in sync immediately for next-draw PickMax tuning
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
        for s in self.stocks:
            total, breakdown = lotto_get_extra_buy(s["code"], self.owned)
            extra_txt = "+{:.2f} ({})".format(
                total, ", ".join(f"+{amt:.2f}{l[0]}" for l, amt in [(b[0], b[1]) for b in breakdown])
            ) if total > 0 else "—"
            score_txt = f"{s['score']:.4f}" if s["score"] is not None else "—"
            rank_txt = f"#{s['rank']}" if s["rank"] else "—"
            tags = ("owned",) if s["code"] in self.owned else ()
            self.tree.insert("", "end", values=(rank_txt, s["code"], s["name"],
                                                  s["pickmax"], extra_txt, score_txt), tags=tags)

# =============================================================================
class SplashScreen(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('MYIPO Index — Loading')
        self.configure(bg='#0d1117')
        self.resizable(False, False)
        self.overrideredirect(True)

        W, H = 500, 400
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f'{W}x{H}+{(sw-W)//2}+{(sh-H)//2}')

        self._result   = None
        self._csv_path = tk.StringVar(value=DEFAULT_INPUT_FILE)
        self._build_ui()

    def _build_ui(self):
        tk.Label(self, text='MYIPO+', bg='#0d1117', fg='#00C9FF',
                 font=('Segoe UI', 32, 'bold')).pack(pady=(36, 0))
        tk.Label(self, text='Index Dashboard', bg='#0d1117', fg='#cdd9e5',
                 font=('Segoe UI', 13)).pack()
        tk.Label(self, text=f'by MYIPO Research  ·  {TODAY.strftime("%d %b %Y")}',
                 bg='#0d1117', fg='#444', font=('Segoe UI', 8)).pack(pady=(2, 18))

        file_row = tk.Frame(self, bg='#0d1117')
        file_row.pack(fill='x', padx=30)

        tk.Entry(file_row, textvariable=self._csv_path,
                 bg='#161b22', fg='#cdd9e5', insertbackground='white',
                 font=('Segoe UI', 8), relief='flat', bd=4).pack(side='left', fill='x', expand=True, ipady=4)

        tk.Button(file_row, text='📂', command=self._pick_file,
                  bg='#21262d', fg='white', relief='flat',
                  font=('Segoe UI', 9), cursor='hand2', padx=6).pack(side='left', padx=(4, 0))

        self._stage_var  = tk.StringVar(value='Select a CSV and click Load.')
        self._detail_var = tk.StringVar(value='')

        tk.Label(self, textvariable=self._stage_var,
                 bg='#0d1117', fg='#cdd9e5', font=('Segoe UI', 9, 'bold')).pack(pady=(14, 0))
        tk.Label(self, textvariable=self._detail_var,
                 bg='#0d1117', fg='#555', font=('Segoe UI', 8)).pack()

        self._pbar = ttk.Progressbar(self, mode='indeterminate', length=440)
        self._pbar.pack(pady=(10, 0), padx=30)

        online_row = tk.Frame(self, bg='#0d1117')
        online_row.pack(pady=(10, 0))
        self._prefer_online = tk.BooleanVar(value=True)
        tk.Checkbutton(
            online_row, text='☁️  Use Online Database (Google Sheets)',
            variable=self._prefer_online,
            bg='#0d1117', fg='#cdd9e5', selectcolor='#0d1117',
            activebackground='#0d1117', activeforeground='#00C9FF',
            font=('Segoe UI', 9), relief='flat', cursor='hand2'
        ).pack()
        tk.Label(online_row, text='(falls back to local CSV if offline)',
                 bg='#0d1117', fg='#444', font=('Segoe UI', 7)).pack()

        self._load_btn = tk.Button(
            self, text='⟳  Load Data', command=self._start_load,
            bg='#00C9FF', fg='#0d1117', font=('Segoe UI', 11, 'bold'),
            relief='flat', padx=20, pady=6, cursor='hand2'
        )
        self._load_btn.pack(pady=(10, 0))

        self._spinner_chars = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏']
        self._spinner_idx   = 0
        self._spinner_var   = tk.StringVar(value='')
        tk.Label(self, textvariable=self._spinner_var,
                 bg='#0d1117', fg='#00C9FF', font=('Segoe UI', 14)).pack(pady=(6, 0))

    def _pick_file(self):
        path = filedialog.askopenfilename(
            title='Select portfolio CSV',
            filetypes=[('CSV files', '*.csv'), ('All files', '*.*')]
        )
        if path:
            self._csv_path.set(path)

    def _log(self, msg):
        msg_l = msg.strip()
        if msg_l.startswith('📊  Source: ONLINE'):
            self._detected_source = 'online'
        elif msg_l.startswith('📊  Source: LOCAL'):
            self._detected_source = 'local'
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
        path = self._csv_path.get()
        if not os.path.exists(path):
            messagebox.showerror('File Not Found', f'Cannot find:\n{path}')
            return
        self._load_btn.config(state='disabled', text='Loading…', bg='#333')
        self._pbar.start(12)
        self._animate_spinner()
        self._stage_var.set('Starting…')
        threading.Thread(target=self._load_thread, args=(path, self._prefer_online.get()), daemon=True).start()

    def _load_thread(self, path, prefer_online=True):
        try:
            result = build_indices(
                path,
                log_cb=lambda m: self.after(0, lambda msg=m: self._log(msg)),
                prefer_online=prefer_online,
            )
            self._result = result
            # Detect source from first log line stored in result or fallback
            self._last_source = getattr(self, '_detected_source', 'local')

            # ── Preload NEWPOR (MYFOLIO / LottoStock) data on the same
            #    background thread, same pattern as the MYIPO index data
            #    itself — fetch + cache once here, so MYFOLIO and LottoStock
            #    open instantly later instead of each doing their own fetch.
            self.after(0, lambda: self._stage_var.set('⬇  Preloading NEWPOR (MYFOLIO) data…'))
            try:
                newpor_rows = _mf_load_csv(
                    log_cb=lambda m: self.after(0, lambda msg=m: self._detail_var.set(msg)),
                    prefer_online=prefer_online,
                )
                newpor_validation   = _mf_validate_ledger(newpor_rows)
                newpor_portfolios   = _mf_build_portfolios(newpor_rows) if newpor_validation["ok"] else {}
                self._newpor_rows       = newpor_rows
                self._newpor_validation = newpor_validation
                self._newpor_portfolios = newpor_portfolios
            except Exception as newpor_exc:
                # NEWPOR preload failing should NOT block the MYIPO dashboard
                # from opening — MYFOLIO/LottoStock just fall back to their
                # own live fetch when opened individually.
                import traceback as _tb
                self._newpor_rows       = None
                self._newpor_validation = {"ok": False, "errors": [f"{newpor_exc}\n{_tb.format_exc()}"],
                                           "warnings": [], "row_count": 0, "checked_at": None}
                self._newpor_portfolios = None

            self.after(0, self._launch_dashboard)
        except Exception as exc:
            # ── FIX 4: capture exception correctly for Python 3.14 ──
            err_msg = str(exc)
            self.after(0, lambda msg=err_msg: self._on_error(msg))

    def _on_error(self, msg):
        self._pbar.stop()
        self._spinner_var.set('')
        self._load_btn.config(state='normal', text='⟳  Load Data', bg='#00C9FF')
        self._stage_var.set('❌  Error — see details below.')
        self._detail_var.set(msg[:80])
        messagebox.showerror('Load Error', msg)

    def _launch_dashboard(self):
        self._pbar.stop()
        self._stage_var.set('✅  Done! Opening dashboard…')
        self.after(400, self._open_dashboard)

    def _open_dashboard(self):
        frames, df, wsnap, bmap, snames, future_ipos, stock_price_df, delist_candidates = self._result
        self.destroy()
        app = MYIPOApp(
            preloaded=(frames, df, wsnap, bmap, snames, future_ipos, stock_price_df, delist_candidates),
            csv_path=self._csv_path.get(),
        )
        app._data_source = getattr(self, '_last_source', 'local')
        if hasattr(app, '_source_label_var'):
            src = app._data_source
            app._source_label_var.set(f'source: {"☁️" if src == "online" else "📂"} {src}')
        # Hand off preloaded NEWPOR data so MYFOLIO/LottoStock open instantly
        app.newpor_rows       = getattr(self, '_newpor_rows', None)
        app.newpor_portfolios = getattr(self, '_newpor_portfolios', None)
        app.newpor_validation = getattr(self, '_newpor_validation', None)
        app.mainloop()


# =============================================================================
# APPLOCK — placeholder PIN gate (full multi-user login is a future update)
# Simple fixed-PIN screen that gates the whole app before SplashScreen loads.
# Not real authentication yet — just the UI/flow scaffold.
# =============================================================================
APPLOCK_PIN = "1234"   # placeholder — replace with real auth in a future update

class AppLockScreen(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PAA — Locked")
        self.configure(bg='#0d1117')
        self.geometry("420x340")
        self.resizable(False, False)
        self._pin_var = tk.StringVar(value="")
        self._build_ui()

    def _build_ui(self):
        center = tk.Frame(self, bg='#0d1117')
        center.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(center, text="🔒", font=("Segoe UI", 40), bg='#0d1117', fg='#58A6FF').pack()
        tk.Label(center, text="PAA — APPLOCK", font=("Segoe UI", 16, "bold"),
                 fg='#58A6FF', bg='#0d1117').pack(pady=(6, 2))
        tk.Label(center, text="Enter PIN to continue", font=("Segoe UI", 9),
                 fg='#888', bg='#0d1117').pack(pady=(0, 16))

        entry = tk.Entry(center, textvariable=self._pin_var, show="•", justify="center",
                          font=("Segoe UI", 18), bg='#161b22', fg='#cdd9e5',
                          insertbackground='#cdd9e5', relief="flat", width=10)
        entry.pack(ipady=8)
        entry.bind("<Return>", lambda e: self._try_unlock())
        entry.focus_set()

        self._error_var = tk.StringVar(value="")
        tk.Label(center, textvariable=self._error_var, font=("Segoe UI", 9),
                 fg='#F85149', bg='#0d1117').pack(pady=(8, 0))

        tk.Button(center, text="Unlock", font=("Segoe UI", 11, "bold"),
                  bg='#58A6FF', fg='#0d1117', relief="flat", padx=20, pady=8,
                  cursor="hand2", command=self._try_unlock).pack(pady=(14, 0))

        tk.Label(center, text="(placeholder lock — default PIN: 1234)",
                 font=("Segoe UI", 7), fg='#444', bg='#0d1117').pack(pady=(12, 0))

    def _try_unlock(self):
        if self._pin_var.get() == APPLOCK_PIN:
            self.destroy()
            splash = SplashScreen()
            splash.mainloop()
        else:
            self._error_var.set("Incorrect PIN — try again.")
            self._pin_var.set("")


# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == '__main__':
    lock = AppLockScreen()
    lock.mainloop()
