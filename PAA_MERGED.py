import pandas as pd
import yfinance as yf
import numpy as np
import os
import threading
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
    - If prefer_online=True  → try online first, fall back to local.
    - If prefer_online=False → local only.
    Saves a local cache of the online data when cache_online=True.
    """
    def log(m):
        if log_cb: log_cb(m)

    if prefer_online:
        df_online, src = fetch_online_db(log_cb=log_cb)
        if df_online is not None:
            if cache_online:
                try:
                    cache_path = os.path.join(os.path.dirname(local_path), LOCAL_CACHE_NAME)
                    df_online.to_csv(cache_path, index=False)
                    log(f'💾  Online data cached to {cache_path}')
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
TR_SERIES_CONFIG = [(f'{c[0]} TR', c[1], c[2], c[3]) for c in INDEX_CONFIG]

# =============================================================================
# TOP-10 SERIES DEFINITION
# =============================================================================
TOP10_LABEL      = 'MYIPO+ Top 10'

ALL_INDEX_LABELS = (
    [c[0] for c in INDEX_CONFIG] +
    [c[0] for c in YSERIES_CONFIG] +
    [c[0] for c in TR_SERIES_CONFIG] +
    [TOP10_LABEL]
)

COLORS = [
    '#00C9FF','#FF6B6B','#FFD93D','#6BCB77','#845EC2',
    '#F9A825','#00B4D8','#E76F51','#A8DADC','#457B9D',
    '#2EC4B6','#FF9F1C',
]
TR_COLORS = [
    '#7FECFF','#FFADAD','#FFEEAA','#B5F5C3','#C4A4E8',
]
COLOR_MAP = {label: COLORS[i % len(COLORS)] for i, label in enumerate(ALL_INDEX_LABELS)}
for i, cfg in enumerate(TR_SERIES_CONFIG):
    COLOR_MAP[cfg[0]] = TR_COLORS[i % len(TR_COLORS)]
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
    for label, qty_col, board_filter, use_d365 in INDEX_CONFIG:
        log(f"   Building {label}...")
        subset = df_listed.copy()
        if board_filter is not None:
            subset = subset[subset['Board'] == board_filter]
        subset = subset[subset[qty_col] > 0].sort_values('Trade Date')
        if subset.empty: continue

        holdings_val, exit_mask   = _build_holdings(subset, qty_col, use_d365)
        index_final, weights      = _calc_index(holdings_val, exit_mask if use_d365 else None)

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

        btn_row = tk.Frame(top_block, bg='#161b22')
        btn_row.pack(fill='x', padx=8, pady=6)
        for _txt, _cmd, _fg in [
            ('All',      self._select_all,     '#cdd9e5'),
            ('None',     self._deselect_all,    '#cdd9e5'),
            ('Core',     self._select_core,     '#cdd9e5'),
            ('Y-Series', self._select_yseries,  '#cdd9e5'),
            ('TR',       self._select_trseries, '#7FECFF'),
            ('Top10',    self._select_top10,    '#FFD700'),
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
            bottom_block, columns=('Index', 'Last', 'Chg%', 'Ret%'),
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

        for col, w, anchor in [('Index', 112, 'w'), ('Last', 58, 'e'), ('Chg%', 52, 'e'), ('Ret%', 56, 'e')]:
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
        MYFOLIOWindow(self)

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

        cols = ['Date'] + ALL_INDEX_LABELS
        self.data_tree = ttk.Treeview(frame, columns=cols, show='headings')
        for col in cols:
            self.data_tree.heading(col, text=col)
            self.data_tree.column(col, width=90, anchor='e', stretch=False)
        self.data_tree.column('Date', width=90, anchor='w')

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
        elif tab == 2: pass
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

        selected = self._selected_labels()
        period   = self.period_var.get()

        if not selected:
            ax.set_title('No indices selected', color='#888')
            self.canvas_line.draw()
            return

        for lbl in selected:
            series = self.all_frames[lbl][lbl].dropna()
            if series.empty: continue
            mask = self._get_period_mask(series.index)
            s    = series[mask]
            if s.empty: continue
            ax.plot(s.index, s.values, label=lbl,
                    color=COLOR_MAP[lbl], linewidth=1.5)

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

        ax.axhline(100, color='#333', linewidth=0.8, linestyle='--')
        ax.set_title(f'MYIPO Index — Cumulative (Base 100)  [{period}]',
                     color='#cdd9e5', fontsize=11, pad=10)
        ax.set_ylabel('Index Value', color='#888')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %y'))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        self.fig_line.autofmt_xdate(rotation=30, ha='right')
        ax.legend(fontsize=7, loc='upper left',
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

        df       = self.output_df.copy()
        val_cols = [lbl for lbl in ALL_INDEX_LABELS if lbl in df.columns]
        for dt, row in df[val_cols].tail(500).iloc[::-1].iterrows():
            date_str = dt.strftime('%d/%m/%Y') if hasattr(dt, 'strftime') else str(dt)
            vals     = [f'{v:.2f}' if pd.notna(v) else '' for v in row]
            self.data_tree.insert('', 'end', values=[date_str] + vals)

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
            self.stats_tree.insert('', 'end',
                values=(lbl.replace('MYIPO+ ', ''), f'{last:.2f}',
                        f'{chg1:+.2f}', f'{period_ret:+.1f}%'),
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
    "/pub?output=csv"
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

# Portfolio code + platform per currency bucket
_NEWPOR_CCY_MAP = {
    "USD": ("SWAY1", "SWAY"),
    "MYR": ("SWAY1-MYR", "SWAY"),
}

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

def _mf_load_csv(url: str = MYFOLIO_CSV_URL) -> list:
    """Download NEWPOR transaction ledger CSV and return list of normalised dicts."""
    with _mf_urlopen(url, timeout=15) as resp:
        raw = resp.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
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


# ── MYFOLIO Main Window (Toplevel, launched from MYIPOApp) ────────────────────
class MYFOLIOWindow(tk.Toplevel):
    """
    Portfolio Command Center embedded as a Toplevel inside the merged app.
    USD portfolios (SWAY, THEO, RAKU-US) and MYR portfolios (UPXX, RAKU-KL)
    are shown in separate notebook tabs.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.title("MYFOLIO — Portfolio Command Center")
        self.geometry("1240x760"); self.minsize(1020, 640)
        self.configure(bg=_MF_BG)

        self.portfolios: dict = {}
        self.active_code   = tk.StringVar(value="")
        self._loading      = False
        self._status_var   = tk.StringVar(value="")
        self._cur_tab      = "USD"   # track which currency tab is active

        self._build_ui()
        self._load_async()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Top bar
        top = tk.Frame(self, bg=_MF_PANEL, height=52)
        top.pack(fill="x"); top.pack_propagate(False)

        tk.Label(top, text="⬡ MYFOLIO", font=("Consolas", 15, "bold"),
                 bg=_MF_PANEL, fg=_MF_ACCENT).pack(side="left", padx=18)
        tk.Label(top, text="PORTFOLIO COMMAND CENTER", font=("Consolas", 9),
                 bg=_MF_PANEL, fg=_MF_SUBTEXT).pack(side="left")
        tk.Label(top, text="│ SCA-NEWPOR", font=("Consolas", 8),
                 bg=_MF_PANEL, fg=_MF_YELLOW).pack(side="left", padx=6)

        self.status_lbl = tk.Label(top, textvariable=self._status_var,
                                   font=_MF_FONT_SM, bg=_MF_PANEL, fg=_MF_YELLOW)
        self.status_lbl.pack(side="left", padx=20)
        self.clock_lbl = tk.Label(top, font=_MF_FONT_SM, bg=_MF_PANEL, fg=_MF_SUBTEXT)
        self.clock_lbl.pack(side="right", padx=18)
        tk.Button(top, text="⟳ Refresh", font=_MF_FONT_SM,
                  bg=_MF_CARD, fg=_MF_ACCENT, relief="flat", bd=0, padx=10, pady=4,
                  command=self._load_async).pack(side="right", padx=6)
        self._tick_clock()

        # Body
        body = tk.Frame(self, bg=_MF_BG); body.pack(fill="both", expand=True)

        # Sidebar
        left = tk.Frame(body, bg=_MF_PANEL, width=240)
        left.pack(side="left", fill="y"); left.pack_propagate(False)

        tk.Label(left, text="PORTFOLIOS", font=("Consolas", 8, "bold"),
                 bg=_MF_PANEL, fg=_MF_SUBTEXT).pack(anchor="w", padx=16, pady=(18, 6))

        # Currency tab buttons in sidebar
        ctab_row = tk.Frame(left, bg=_MF_PANEL)
        ctab_row.pack(fill="x", padx=12, pady=(0, 4))
        self._usd_btn = tk.Button(ctab_row, text="USD", font=("Consolas", 8, "bold"),
                                  bg=_MF_ACCENT, fg=_MF_BG, relief="flat", bd=0, padx=8, pady=3,
                                  command=lambda: self._switch_currency("USD"))
        self._usd_btn.pack(side="left", expand=True, fill="x", padx=(0, 2))
        self._myr_btn = tk.Button(ctab_row, text="MYR", font=("Consolas", 8, "bold"),
                                  bg=_MF_CARD, fg=_MF_TEXT, relief="flat", bd=0, padx=8, pady=3,
                                  command=lambda: self._switch_currency("MYR"))
        self._myr_btn.pack(side="left", expand=True, fill="x")

        self.portfolio_list = tk.Listbox(left, bg=_MF_CARD, fg=_MF_TEXT,
                                         selectbackground=_MF_ACCENT, selectforeground=_MF_BG,
                                         font=_MF_FONT_SM, relief="flat", bd=0, activestyle="none")
        self.portfolio_list.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.portfolio_list.bind("<<ListboxSelect>>", self._on_select)

        tk.Label(left, text="DATA SOURCE", font=("Consolas", 7, "bold"),
                 bg=_MF_PANEL, fg=_MF_SUBTEXT).pack(anchor="w", padx=16, pady=(4, 2))
        tk.Label(left, text="Google Sheets  (PtW CSV)\nSCA-NEWPOR Database",
                 font=("Consolas", 8), bg=_MF_PANEL, fg=_MF_GREEN, wraplength=200).pack(anchor="w", padx=16)
        self.last_sync_lbl = tk.Label(left, text="Last sync: —",
                                      font=("Consolas", 7), bg=_MF_PANEL, fg=_MF_SUBTEXT, wraplength=200)
        self.last_sync_lbl.pack(anchor="w", padx=16, pady=(0, 12))

        # Main content
        self.main = tk.Frame(body, bg=_MF_BG); self.main.pack(side="left", fill="both", expand=True)

        header = tk.Frame(self.main, bg=_MF_BG); header.pack(fill="x", padx=22, pady=(20, 8))
        self.title_lbl = tk.Label(header, font=_MF_FONT_H1, bg=_MF_BG, fg=_MF_TEXT, text="—")
        self.title_lbl.pack(side="left")
        tk.Button(header, text="📊 Charts", font=_MF_FONT_SM,
                  bg=_MF_CARD, fg=_MF_PURPLE, relief="flat", bd=0, padx=10, pady=5,
                  command=self._open_charts).pack(side="right", padx=4)

        # Currency badge
        self.cur_badge = tk.Label(header, text="● USD", font=("Consolas", 9, "bold"),
                                  bg=_MF_BG, fg=_MF_ACCENT)
        self.cur_badge.pack(side="right", padx=8)

        # KPI strip
        self.kpi_frame = tk.Frame(self.main, bg=_MF_BG)
        self.kpi_frame.pack(fill="x", padx=16, pady=4)

        # Detail area
        detail = tk.Frame(self.main, bg=_MF_BG); detail.pack(fill="both", expand=True, padx=22, pady=8)

        self.meta_frame = tk.Frame(detail, bg=_MF_PANEL, width=220)
        self.meta_frame.pack(side="left", fill="y", padx=(0, 8)); self.meta_frame.pack_propagate(False)

        self.hold_frame = tk.Frame(detail, bg=_MF_PANEL)
        self.hold_frame.pack(side="left", fill="both", expand=True)

        hold_cols = ("Ticker","Market","Currency","Units","Avg Cost","Cost Value",
                     "Current Price","Market Value","P/L","P/L %","Date")
        self.hold_tree = ttk.Treeview(self.hold_frame, columns=hold_cols, show="headings")
        col_widths = {"Ticker":90,"Market":60,"Currency":70,"Units":80,"Avg Cost":85,
                      "Cost Value":95,"Current Price":95,"Market Value":95,
                      "P/L":90,"P/L %":75,"Date":95}
        for col in hold_cols:
            self.hold_tree.heading(col, text=col)
            self.hold_tree.column(col, width=col_widths.get(col, 85),
                                  anchor="w" if col in ("Ticker","Date") else "e")

        scroll_y = ttk.Scrollbar(self.hold_frame, orient="vertical", command=self.hold_tree.yview)
        self.hold_tree.configure(yscrollcommand=scroll_y.set)
        self.hold_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll_y.pack(side="left", fill="y", pady=8)

        style = ttk.Style()
        style.configure("MF.Treeview", background=_MF_CARD, foreground=_MF_TEXT,
                        fieldbackground=_MF_CARD, font=_MF_FONT_SM, rowheight=26, borderwidth=0)
        style.configure("MF.Treeview.Heading", background=_MF_BORDER, foreground=_MF_SUBTEXT,
                        font=("Consolas", 8, "bold"))
        style.map("MF.Treeview", background=[("selected", _MF_ACCENT)],
                  foreground=[("selected", _MF_BG)])
        self.hold_tree.configure(style="MF.Treeview")

    # ── Currency tab switch ───────────────────────────────────────────────────
    def _switch_currency(self, currency: str):
        self._cur_tab = currency
        if currency == "USD":
            self._usd_btn.configure(bg=_MF_ACCENT, fg=_MF_BG)
            self._myr_btn.configure(bg=_MF_CARD,   fg=_MF_TEXT)
            badge_color = _MF_ACCENT
        else:
            self._myr_btn.configure(bg=_MF_GREEN, fg=_MF_BG)
            self._usd_btn.configure(bg=_MF_CARD,  fg=_MF_TEXT)
            badge_color = _MF_GREEN
        self.cur_badge.configure(text=f"● {currency}", fg=badge_color)
        self._rebuild_list()
        self._refresh_detail()

    def _portfolios_for_currency(self, currency: str) -> dict:
        """Return only portfolios whose base/primary currency matches."""
        result = {}
        for code, port in self.portfolios.items():
            # A portfolio "belongs" to a currency if ALL its holdings are in that currency,
            # or if its base_currency matches. For mixed RAKU portfolios, it goes to
            # whichever currency has more cost value.
            holdings = port["holdings"]
            if not holdings:
                continue
            cur_costv: dict = {}
            for item in holdings.values():
                cur_costv[item["currency"]] = cur_costv.get(item["currency"], 0.0) + item["cost_value"]
            dominant = max(cur_costv, key=cur_costv.get)
            if dominant == currency:
                result[code] = port
        return result

    # ── Clock ─────────────────────────────────────────────────────────────────
    def _tick_clock(self):
        import datetime as _dt
        self.clock_lbl.configure(text=_dt.datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        self.after(1000, self._tick_clock)

    # ── Data loading ──────────────────────────────────────────────────────────
    def _load_async(self):
        if self._loading: return
        self._loading = True
        _MF_PRICE_CACHE.clear()
        self._status_var.set("⟳ Syncing NEWPOR database…")
        import threading as _thr
        _thr.Thread(target=self._fetch_thread, daemon=True).start()

    def _fetch_thread(self):
        try:
            rows = _mf_load_csv()
            ports = _mf_build_portfolios(rows)
            self.after(0, lambda: self._on_loaded(ports))
        except _mf_URLError as exc:
            msg = str(exc.reason)
            self.after(0, lambda: self._on_error(f"Network error: {msg}"))
        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda: self._on_error(msg))

    def _on_loaded(self, portfolios: dict):
        import datetime as _dt
        self._loading = False
        self.portfolios = portfolios
        now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._status_var.set(f"✓ Synced  ({len(portfolios)} portfolios)")
        self.last_sync_lbl.configure(text=f"Last sync:\n{now}")
        self._rebuild_list()
        self._refresh_detail()

    def _on_error(self, msg: str):
        self._loading = False
        self._status_var.set("✗ Load failed")
        from tkinter import messagebox as _mb
        _mb.showerror("NEWPOR Load Error",
                      f"Could not load portfolio data:\n\n{msg}\n\n"
                      "Check that the MYFOLIO sheet is published:\n"
                      "File → Share → Publish to web → CSV")

    def _rebuild_list(self):
        self.portfolio_list.delete(0, "end")
        visible = self._portfolios_for_currency(self._cur_tab)
        codes = sorted(visible.keys())
        for code in codes:
            plat = visible[code]["platform"]
            self.portfolio_list.insert("end", f"{code}  [{plat}]")
        # Auto-select first if current active not visible
        active = self.active_code.get()
        if active not in visible and codes:
            active = codes[0]
            self.active_code.set(active)
        for idx, code in enumerate(codes):
            if code == active:
                self.portfolio_list.selection_set(idx)
                self.portfolio_list.activate(idx)
                break

    # ── Sidebar selection ─────────────────────────────────────────────────────
    def _on_select(self, _event=None):
        sel = self.portfolio_list.curselection()
        if not sel: return
        code = self.portfolio_list.get(sel[0]).split(" ", 1)[0]
        self.active_code.set(code)
        self._refresh_detail()

    # ── Detail refresh ────────────────────────────────────────────────────────
    def _refresh_detail(self):
        code = self.active_code.get()
        if not code or code not in self.portfolios: return
        port = self.portfolios[code]

        self.title_lbl.configure(text=f"PORTFOLIO: {port['code']}  [{port['platform']}]")
        for w in self.kpi_frame.winfo_children():  w.destroy()
        for w in self.meta_frame.winfo_children(): w.destroy()
        for row in self.hold_tree.get_children():  self.hold_tree.delete(row)

        total_cost:   dict = {}
        total_mktval: dict = {}

        for ticker, item in sorted(port["holdings"].items()):
            is_cash  = item.get("_is_cash", False)
            units    = item["shares"]
            avg_cost = item["avg_cost"]
            costv    = item["cost_value"]
            market   = item["market"]
            currency = item["currency"]

            # Cash holdings: price is always 1.0, no P/L
            if is_cash:
                price  = 1.0
                mktval = units           # cash value = units
                pl     = 0.0
                pl_pct = 0.0
                tag    = "mf_cash"
            else:
                price  = _mf_get_price(ticker, market)
                mktval = units * price if price is not None else None
                pl     = mktval - costv if mktval is not None else None
                pl_pct = (pl / costv * 100) if pl is not None and costv else None
                tag    = "mf_gain" if pl is not None and pl >= 0 else "mf_loss"

            total_cost[currency]   = total_cost.get(currency, 0.0) + costv
            if mktval is not None:
                total_mktval[currency] = total_mktval.get(currency, 0.0) + mktval

            self.hold_tree.insert("", "end", tags=(tag,), values=(
                ticker, market, currency,
                f"{units:,.4f}", f"{avg_cost:,.4f}", _mf_fmt_money(costv),
                _mf_fmt_price(price) if not is_cash else "1.0000",
                _mf_fmt_money(mktval),
                _mf_fmt_signed(pl) if pl is not None else ("cash" if is_cash else "N/A"),
                _mf_fmt_pct(pl_pct) if pl_pct is not None else ("—" if is_cash else "N/A"),
                item.get("date", ""),
            ))

        self.hold_tree.tag_configure("mf_gain", foreground=_MF_GREEN)
        self.hold_tree.tag_configure("mf_loss", foreground=_MF_RED)
        self.hold_tree.tag_configure("mf_cash", foreground=_MF_YELLOW)

        # ── KPI strip ─────────────────────────────────────────────────────────
        n_holdings = sum(1 for t, h in port["holdings"].items() if not h.get("_is_cash"))
        self._kpi("Platform",  port["platform"], _MF_ACCENT)
        self._kpi("Holdings",  str(n_holdings), _MF_PURPLE)

        cost_txt = " | ".join(f"{cur} {v:,.2f}" for cur, v in total_cost.items()) or "—"
        mv_txt   = " | ".join(f"{cur} {v:,.2f}" for cur, v in total_mktval.items()) or "—"
        self._kpi("Invested", cost_txt, _MF_SUBTEXT)
        self._kpi("Market Value", mv_txt, _MF_ACCENT)

        # Unrealised P/L (exclude cash from both sides)
        upl_parts = []; all_pos = True
        invest_cost:  dict = {}
        invest_mktval: dict = {}
        for ticker, item in port["holdings"].items():
            if item.get("_is_cash"): continue
            cur = item["currency"]
            invest_cost[cur]  = invest_cost.get(cur, 0.0)  + item["cost_value"]
            price = _mf_get_price(ticker, item["market"])
            if price is not None:
                invest_mktval[cur] = invest_mktval.get(cur, 0.0) + item["shares"] * price
        for cur, mv in invest_mktval.items():
            tc  = invest_cost.get(cur, 0.0)
            upl = mv - tc; pct = (upl / tc * 100) if tc else 0.0
            if upl < 0: all_pos = False
            upl_parts.append(f"{cur} {upl:+,.2f} ({pct:+.2f}%)")
        self._kpi("Unrealised P/L", " | ".join(upl_parts) or "—", _MF_GREEN if all_pos else _MF_RED)

        # Realised P/L from transaction ledger
        rpl   = port.get("_realised_pl", 0.0)
        fees  = port.get("_total_fees", 0.0)
        dep   = port.get("_total_deposited", 0.0)
        wdraw = port.get("_total_withdrawn", 0.0)
        ccy0  = port["base_currency"]
        self._kpi("Realised P/L", f"{ccy0} {rpl:+,.2f}", _MF_GREEN if rpl >= 0 else _MF_RED)

        # ── Meta panel ────────────────────────────────────────────────────────
        tk.Label(self.meta_frame, text=" DETAILS", font=_MF_FONT_H3,
                 bg=_MF_PANEL, fg=_MF_ACCENT).pack(anchor="w", padx=10, pady=(10, 4))

        cash_bal = port["holdings"].get(f"{ccy0}-CASH", {}).get("shares", 0.0)
        for label, value in [
            ("Code",          port["code"]),
            ("Platform",      port["platform"]),
            ("Market",        ", ".join(port["markets"])),
            ("Currency",      ", ".join(port["currencies"])),
            ("Holdings",      str(n_holdings)),
            ("Invested Cost", cost_txt),
            ("Market Value",  mv_txt),
            ("Cash Balance",  f"{ccy0} {cash_bal:,.4f}"),
            ("Deposited",     f"{ccy0} {dep:,.2f}"),
            ("Withdrawn",     f"{ccy0} {wdraw:,.2f}"),
            ("Fees Paid",     f"{ccy0} {fees:,.4f}"),
            ("Realised P/L",  f"{ccy0} {rpl:+,.2f}"),
        ]:
            tk.Label(self.meta_frame, text=label, font=("Consolas", 8, "bold"),
                     bg=_MF_PANEL, fg=_MF_SUBTEXT).pack(anchor="w", padx=12, pady=(6, 0))
            color = _MF_TEXT
            if label == "Realised P/L":
                color = _MF_GREEN if rpl >= 0 else _MF_RED
            elif label == "Cash Balance":
                color = _MF_YELLOW
            tk.Label(self.meta_frame, text=value, font=_MF_FONT_SM,
                     bg=_MF_PANEL, fg=color, wraplength=200).pack(anchor="w", padx=12)

    def _kpi(self, label: str, value: str, color: str):
        frame = tk.Frame(self.kpi_frame, bg=_MF_CARD, padx=14, pady=10)
        frame.pack(side="left", expand=True, fill="both", padx=6)
        tk.Label(frame, text=label, font=_MF_FONT_SM, bg=_MF_CARD, fg=_MF_SUBTEXT).pack(anchor="w")
        tk.Label(frame, text=value, font=_MF_FONT_LG, bg=_MF_CARD, fg=color, wraplength=230).pack(anchor="w")

    def _open_charts(self):
        code = self.active_code.get()
        if not code or code not in self.portfolios:
            from tkinter import messagebox as _mb; _mb.showinfo("Charts", "No portfolio selected."); return
        MFChartWindow(self, self.portfolios[code], self.portfolios)


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
        app.mainloop()


# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == '__main__':
    splash = SplashScreen()
    splash.mainloop()
