[...]
_CACHE_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'paa_cache')
_CACHE_PRICES_DIR = os.path.join(_CACHE_DIR, 'prices')
_CACHE_DIVS_DIR   = os.path.join(_CACHE_DIR, 'divs')
_CACHE_LIVE_DIR   = os.path.join(_CACHE_DIR, 'live')

_TTL_PRICE  = 86_400        # 1 day
_TTL_DIV    = 7 * 86_400    # 7 days
_TTL_LIVE   = 300           # 5 min
_TTL_EPS    = 86_400        # 24 hr
[...]
def _cache_fresh(path: str, ttl: int) -> bool:
    [...]
def _cache_rparquet(path: str):
    [...]
def _cache_wparquet(path: str, df):
    [...]
def _cache_rjson(path: str) -> dict:
    [...]
def _cache_wjson(path: str, data: dict):
    [...]
def _pticker(ticker: str) -> str:
    [...]
def _dticker(ticker: str) -> str:
    [...]
def _ljson(key: str) -> str:
    [...]
def cache_get_prices(ticker: str, start_str: str = None) -> 'pd.Series | None':
    [...]
def cache_set_prices(ticker: str, series: 'pd.Series'):
    [...]
def cache_get_divs(ticker: str) -> 'pd.Series | None':
    [...]
def cache_set_divs(ticker: str, series: 'pd.Series'):
    [...]
def cache_get_live(key: str, ttl: int = _TTL_LIVE):
    [...]
def cache_set_live(key: str, value):
    _cache_wjson(_ljson(key), {'v': value, 't': time_module.time()})

def cache_get_live_dict(key: str, ttl: int = _TTL_LIVE) -> dict:
    [...]
def cache_set_live_dict(key: str, data: dict):
[...]
ONLINE_DB_URL = (
    [...]
)
ONLINE_TIMEOUT   = 10          # seconds before giving up
LOCAL_CACHE_NAME = 'teet_cache.csv'   # saved alongside the local CSV (was TSV, now CSV)

def fetch_online_db(log_cb=None):
    [...]
    def log(m):
    [...]
def resolve_input(local_path, prefer_online=True, log_cb=None, cache_online=True):
    [...]
    def log(m):
[...]
DEFAULT_FOLDER       = r'C:\Users\User\Documents'
DEFAULT_INPUT_FILE   = os.path.join(DEFAULT_FOLDER, 'teet.csv')
DEFAULT_OUTPUT_CSV   = os.path.join(DEFAULT_FOLDER, 'MYIPO_Index_History.csv')
# (per-K-ID ledger cache path is derived at runtime — see _mf_load_csv)
START_DATE_STR     = (pd.Timestamp.now() - pd.DateOffset(years=7)).strftime('%Y-%m-%d')
MALAYSIA_TZ        = ZoneInfo('Asia/Kuala_Lumpur') if ZoneInfo else None
[...]
INDEX_CONFIG = [
    [...]
]
[...]
YSERIES_CONFIG = [
    [...]
]
[...]
TR_SERIES_CONFIG = (
    [(f'{c[0]} TR', c[1], c[2], c[3]) for c in INDEX_CONFIG] +
    [(f'{c[0]} TR', c[1], None, False) for c in YSERIES_CONFIG]
)
[...]
FUND_PAR_NAV      = 0.2500     # RM per unit at inception (same for all funds)
FUND_UNITS_ISSUED = 1000       # fixed units in issue (same for all funds)
FUND_DIST_MONTHS_DAYS = [(3, 31), (6, 30), (9, 30), (12, 31)]  # (month, day)
FUND_DIST_CASH_FLOOR_PER_UNIT = 0.0100  # RM/unit — skip payout if cash/unit below this
[...]
FUND_SELL_FEE_PER_TXN = 2.01    # RM, flat, deducted from every sell/exit proceeds
[...]
FUND_CONFIG = [
[...]
]
[...]
FUND_LABEL      = 'MYIPO+ Fund'
FUND_BASE_INDEX = 'MYIPO+'
FUND_LABELS     = [c[0] for c in FUND_CONFIG]   # all fund labels, in order
[...]
VFUND_INCEPTION = '2026-01-01'   # snapped forward to the first trading day

VFUND_CONFIG = [
    [...]
]
VFUND_LABELS = [c['label'] for c in VFUND_CONFIG]
[...]
ALL_FUND_LABELS = FUND_LABELS + VFUND_LABELS   # both classes, ledger-tracked

FUND_META = {}
[...]
TOP10_LABEL      = 'MYIPO+ Top 10'
_TOP10_HISTORY   = []   # legacy alias — Top 10 in/out records
_TOP_SERIES_HISTORY = {}  # {N: [history contract records]} for Top 10/20/50/100
TOP_SERIES_LABELS = ['MYIPO+ Top 10', 'MYIPO+ Top 20', 'MYIPO+ Top 50', 'MYIPO+ Top 100']
# label -> N, derived rather than hand-written so the two can't drift apart
_TOP_LABEL_N = {lbl: int(lbl.rsplit(' ', 1)[1]) for lbl in TOP_SERIES_LABELS}

ALL_INDEX_LABELS = (
    [...]
)
[...]
LINE_STYLE_MAP = {}
[...]
COLORS = [
    [...]
]
TR_COLORS = [
    '#7FECFF','#FFADAD','#FFEEAA','#B5F5C3','#C4A4E8',
]
FUND_COLORS = [
    '#00E5A0','#5EEAD4','#34D399','#A7F3D0','#6EE7B7',
    '#10B981','#2DD4BF','#99F6E4',
]
COLOR_MAP = {label: COLORS[i % len(COLORS)] for i, label in enumerate(ALL_INDEX_LABELS)}
[...]
WORLD_COLORS = {
    [...]
}
[...]
def build_indices(input_file, log_cb=None, prefer_online=True, apply_sell_fee=True, drip_distributions=False):
    def log(msg):
    [...]
    def _parse_my_dates(series):
    [...]
        def _dl_chunk(idx_chunk):
    [...]
        def _fetch_divs(sym):
    [...]
    def _snap_date(dt):
        [...]
    def _build_holdings(subset, qty_col, use_d365=False, price_data=None):
        [...]
    def _calc_index(holdings_val, exit_mask=None):
        [...]
    def _build_top_series(top_n: int):
        [...]
        def _next_td(target_dt):
[...]
# ==========================================================
# Save For Rewards (SoR) Engine
# ==========================================================

class SaveForRewards:
    """
    SoR (Save for Rewards) Engine for SCA and StockLotto.
    Calculates Cr (Credits) based on regional stock/ETF positions and 'Road To' progress.

    Current Supported Markets
    -------------------------
    ✓ US Stocks / ETFs
    ✓ Malaysia StockLotto33

    Future Expansion Pack (FEP)
    ---------------------------
    • Hong Kong Market
    • Singapore Market
    """

    @staticmethod
    def calculate_position_cr(
        region,
        units,
        is_custom=False,
        custom_threshold=0.01,
        is_lotto_33=False
    ):
        """
        Calculate Credits (Cr) earned from holdings.
        
        Args:
            region (str): Market region ('US', 'MY', 'HK', 'SG')
            units (float): Number of units held
            is_custom (bool): Whether to use custom threshold
            custom_threshold (float): Custom Cr conversion ratio
            is_lotto_33 (bool): Whether position is Malaysia StockLotto33
            
        Returns:
            int: Credits earned from position
        """

        # Custom Rule
        if is_custom and custom_threshold > 0:
            return int(units // custom_threshold)

        # US: Every 0.01 share = 1 Cr
        if region == "US":
            return int(units // 0.01)

        # Malaysia: StockLotto33 -> 1 unit = 1 Cr
        if region == "MY":
            if is_lotto_33:
                return int(units)

        # HK & SG moved into Future Expansion Pack
        return 0


    @staticmethod
    def calculate_road_to_cr(target_percentage):
        """
        Calculate Credits (Cr) earned from 'Road To' target milestones.
        Includes base Cr + Bonus Cr* logic.
        
        Args:
            target_percentage (float): Achievement percentage toward road target (0-100)
            
        Returns:
            int: Total credits earned (base + bonus)
            
        Road To Reward Structure:
        - 100% = 100Cr + 100Cr* = 200Cr total
        - 90% = 90Cr + 50Cr* = 140Cr total
        - 75% = 80Cr + 40Cr* = 120Cr total
        - 67% = 70Cr + 30Cr* = 100Cr total
        - 50% = 50Cr + 25Cr* = 75Cr total
        - 34% = 35Cr + 15Cr* = 50Cr total
        - 25% = 25Cr + 10Cr* = 35Cr total
        - 20% = 20Cr total
        - 10% = 10Cr total
        """

        if target_percentage >= 100:
            return 200
        elif target_percentage >= 90:
            return 140
        elif target_percentage >= 75:
            return 120
        elif target_percentage >= 67:
            return 100
        elif target_percentage >= 50:
            return 75
        elif target_percentage >= 34:
            return 50
        elif target_percentage >= 25:
            return 35
        elif target_percentage >= 20:
            return 20
        elif target_percentage >= 10:
            return 10

        return 0

[...]
class MYIPOApp(tk.Tk):
    def __init__(self, preloaded=None, csv_path=None):
        [...]
    def _build_ui(self):
        [...]
        def _make_section(parent, title, labels, accent='#555'):
            [...]
            def _toggle(event=None, av=arrow_var, t=title, b=body, p=parent):
    [...]
    def _open_myfolio(self):
        [...]
    def _open_lottostock(self):
        [...]
    def _open_sca(self):
        [...]
    def _open_settings(self):
        [...]
        def _sign_out():
            [...]
        def _change_pin():
        [...]
        def _clear_cache():
        [...]
    def _open_change_pin_dialog(self, parent=None):
        [...]
        def _apply():
        [...]
    def _make_checkbox(self, parent, lbl):
    [...]
    BURSA_INDICES = [
        [...]
    ]

    def _build_market_ticker(self, parent):
    [...]
    def _toggle_marquee_dir(self):
        [...]
    def _cycle_marquee_speed(self):
        [...]
    def _toggle_marquee_run(self):
        [...]
    def _pause_marquee(self, hovering: bool):
        [...]
    def _animate_marquee(self):
        [...]
    def _position_marquee(self):
        [...]
    def _draw_marquee(self):
        [...]
        def _fmt(sym, last):
            [...]
        def _draw_copy(x0, tag):
        [...]
    def _marquee_content_x(self, event_x):
        [...]
    def _on_marquee_click(self, event):
        [...]
    def _on_marquee_motion(self, event):
        [...]
    def _ticker_sections(self):
        [...]
    def _refresh_market_ticker(self):
        [...]
        def _worker():
            [...]
            def _fetch(sym):
        [...]
    def _apply_market_data(self, results):
        [...]
    def _open_top10_board(self, top_n: int = 10):
        [...]
        def _rebuild():
    [...]
    CHART_EXTRA = [('^KLSE', 'FBM KLCI', 'MYR')]

    def _chart_world_set(self):
        return list(self.CHART_EXTRA) + list(self.WORLD_INDICES)

    def _load_world_history(self, symbols=None, log_cb=None):
        [...]
        def log(m):
        [...]
    def _open_world_chart(self):
        [...]
        def _worker():
            [...]
        def _done():
        [...]
    def _rebuild_world_section(self, labels):
        [...]
    def _build_line_tab(self):
        [...]
    def _on_line_tk_click(self, tk_event):
        [...]
    def _clear_line_hover(self):
        [...]
    def _on_line_click(self, event):
        [...]
    def _show_line_inspector(self, xdata):
        [...]
    def _build_bar_tab(self):
        [...]
    def _build_table_tab(self):
        [...]
    def _build_stock_tab(self):
    [...]
    def _style_ax(ax):
        [...]
    def _pick_file(self):
        [...]
    def _show_loader(self):
        [...]
    def _animate_spinner(self):
        [...]
    def _loader_update(self, msg):
        [...]
    def _hide_loader(self):
        [...]
    def _run_refresh(self):
        [...]
    def _load_thread(self):
        [...]
    def _on_load_done(self):
        [...]
    def _export_csv(self):
        [...]
    def _now_myt(self):
        return datetime.now(MALAYSIA_TZ) if MALAYSIA_TZ else datetime.now()

    def _ipo_event_dt(self, date_value, hour, minute=0):
    [...]
    def _fmt_countdown(delta):
        [...]
    def _tick_future_header(self):
        [...]
    def _update_future_header(self):
        [...]
    def _get_period_mask(self, index):
        [...]
    def _selected_labels(self):
        return [lbl for lbl in ALL_INDEX_LABELS if self.check_vars[lbl].get() and lbl in self.all_frames]

    def _redraw_chart(self):
        [...]
    def _refresh_stock_list(self):
    [...]
    def _pick_annotation_indices(n_points, max_labels=12):
        [...]
    def _draw_stock_performance(self):
        [...]
    def _draw_line(self):
        [...]
        def _transform(s):
        [...]
    def _draw_bar(self):
        [...]
    def _build_comp_tab(self):
        [...]
    def _open_wc_inspector(self, event=None):
        [...]
    def _draw_composition(self):
    [...]
    def _build_fund_events_tab(self):
        [...]
    def _refresh_fund_events_dropdown(self):
        [...]
    def _draw_fund_events(self):
    [...]
    def _build_fund_ledger_tab(self):
        [...]
        def _sofp_card(parent, header, header_col, fields, val_fg_rule):
        [...]
    def _draw_fund_ledger(self):
    [...]
    PTS_RANKS = [
        [...]
    ]
    PTS_STARTING_CREDITS = 10_000.0
    PTS_MIN_STAKE        = 100.0      # minimum credits per play (all users)
    [...]
    PAA_TIERS = {
        [...]
    }
    LOGIN_REWARDS  = [100, 200, 300, 400, 500, 600, 700]   # day 1..7 cycle
    STREAK_BONUS   = {2: 100, 4: 150, 7: 200}               # consecutive-day bonus
    AD_REWARD_MIN, AD_REWARD_MAX, AD_DAILY_LIMIT = 100, 999, 3
    RAFFLE_MIN_WEEKS = 2   # subscription weeks needed for raffle eligibility

    def _pts_tier(self, pts: dict) -> dict:
        return self.PAA_TIERS.get(pts.get('tier', 'Free'), self.PAA_TIERS['Free'])

    def _pts_max_stake(self, pts: dict) -> float:
        return float(self._pts_tier(pts)['max_stake'])

    def _pts_check_stake(self, pts: dict, stake: float):
        [...]
    def _rewards_daily_engine(self, pts: dict) -> list:
        [...]
    def _rewards_watch_ad(self, pts: dict):
    [...]
    AD_VIEW_SECONDS = 15   # minimum view time before the claim unlocks

    def _rewards_ad_page(self) -> str:
[...]
    def _rewards_open_ad_popup(self):
        [...]
            def _pw():
        [...]
        def _claim():
        [...]
        def _tick(remaining):
        [...]
    def _rewards_tier_weeks(self, pts: dict) -> float:
        [...]
    def _rewards_raffle_status(self, pts: dict) -> str:
        [...]
    def _rewards_set_tier(self, tier_name: str):
    [...]
    def _build_fep_section(self, parent):
        """
        Build Future Expansion Pack (Beta Supporter) UI section.
        Includes information about HK & SG market expansion and subscription benefits.
        """
        support_frame = tk.LabelFrame(
            parent,
            text="🚀 Future Expansion Pack (Beta Supporter)",
            bg="#161b22",
            fg="#FFD700",
            font=("Segoe UI", 11, "bold"),
            padx=10,
            pady=10
        )
        support_frame.pack(fill="x", padx=10, pady=(20, 10))

        description = (
            "Support the future development of PredictTheShares.\n\n"

            "RM2.99 / Week\n\n"

            "Benefits\n"
            "• 5,000 Credits every day (35,000 Credits every week)\n"
            "• Priority Beta Supporter Badge\n"
            "• Help fund upcoming market expansion\n"
            "• Future Expansion Pack (HK & SG) development\n"
            "• 50% Advertisement Experience\n"
            "• All remaining features stay identical to the Free Tier\n\n"

            "Thank you for supporting the continued development ❤️"
        )

        tk.Label(
            support_frame,
            text=description,
            justify="left",
            bg="#161b22",
            fg="#cdd9e5",
            font=("Segoe UI", 10)
        ).pack(anchor="w")

        def open_fep_beta():
            import webbrowser
            webbrowser.open(
                "https://buy.stripe.com/aFa9ATcgU5Rf96G7RU1B604"
            )

        tk.Button(
            support_frame,
            text="Become a Beta Supporter (RM2.99/week)",
            command=open_fep_beta,
            bg="#238636",
            fg="white",
            activebackground="#2ea043",
            activeforeground="white",
            cursor="hand2",
            font=("Segoe UI", 10, "bold")
        ).pack(anchor="w", pady=12)

    RAFFLES = {
        'ZusCoffer':   {'cadence_days': 7,  'prize': 'RM10 Zus Gift Card'},
        'StarBockers': {'cadence_days': 30, 'prize': 'RM30 Starbucks Gift Card'},
    }

    def _raffle_entrants(self, tier_name: str) -> list:
        [...]
    def _raffle_run_draw(self, tier_name: str):
    [...]
    def _stripe_api_get(self, path: str, params: str = ''):
        [...]
    def _stripe_amount_tier(self, amount_sen: int):
        [...]
    def _stripe_sync_subscriptions(self):
        [...]
    STRIPE_PAYLINK_DEFAULTS = {
        [...]
    }

    # Benefit copy per tier, mirroring the Stripe product descriptions.
    TIER_BENEFITS = {
        [...]
    }

    def _stripe_paylink(self, tier_name: str) -> str:
        [...]
    def _open_credit_buy_popup(self):
        [...]
    def _tier_days_active(self, w: dict) -> int:
        [...]
    def _rewards_open_subscribe(self, tier_name: str):
        [...]
    def _admin_emergency_lock(self, parent):
        [...]
    def _open_admin_dashboard(self):
        [...]
        def _status(kid):
        [...]
        def _save_key():
        [...]
        def _save_links():
        [...]
        def _sync():
            sync_var.set('Syncing…')
            def _worker():
                ok2, msg2 = self._stripe_sync_subscriptions()
                def _done():
        [...]
    def _build_rewards_tab(self):
        [...]
    PTS_TRADE_LOT        = 10_000.0   # lot size reference from proposal

    # ── Category definitions ──────────────────────────────────────────────────
    PTS_CATEGORIES = [
        [...]
    ]

    def _pts_load(self) -> dict:
        [...]
    def _pts_save(self, pts: dict):
        [...]
    def _pts_rank(self, pts: dict) -> tuple:
    [...]
    WORLD_INDICES = [
        [...]
    ]
    SP500_CONFIG = [
        [...]
    ]

    # ── SPDR Select Sector ETFs — the 11 S&P 500 sector SPDRs, all USD ──────
    SPDR_SELECT_ETFS = [
        [...]
    ]
    [...]
    MSCI_ETF_CONFIG = [
        [...]
    ]
    [...]
    CRYPTO_ETF_CONFIG = [
        ('IBIT', 'iShares Bitcoin Trust'),
        ('ETHA', 'iShares Ethereum Trust'),
    ]
    [...]
    CRYPTO_PAIRS = [
        ('BTC-USD', 'Bitcoin'),
        ('ETH-USD', 'Ethereum'),
    ]
    [...]
    FX_PAIRS = [
        [...]
    ]
    [...]
    FX_QUOTE_LOT = {'JPY': 100, 'KRW': 1_000, 'IDR': 10_000}

    def _zdws_fx_pair_level(self, code: str):
        [...]
    def _zdws_level_decimals(self, underlying: str) -> int:
        [...]
    def _zdws_fmt_level(self, underlying: str, lvl) -> str:
        [...]
    def _zdws_fx_to_myr(self, ccy: str):
        [...]
    def _zdws_world_level(self, symbol: str):
    [...]
    _TTL_TICK_DISPLAY = _TTL_LIVE * 12   # 1 hour

    def _zdws_cached_level_only(self, symbol: str, ttl: int = None):
    [...]
    def _extract_batch_closes(data, sym, n_symbols):
        [...]
    def _zdws_warm_batch_cache(self, symbols: list) -> int:
        [...]
    def _sp500_config(self) -> list:
        [...]
    US_PRICE_SNAPSHOT = 'us_prices.csv'   # last good batch, persisted beside the script

    def _us_snapshot_path(self) -> str:
        [...]
    def _us_snapshot_save(self):
        [...]
    def _us_snapshot_load(self) -> int:
        [...]
    def _ensure_us_universe_warm(self):
        [...]
        def _worker():
        [...]
    def _zdws_current_level(self, underlying: str):
        [...]
    def _zdws_move_pct(self, w: dict, cur_level: float):
        [...]
    def _zdws_auto_settle(self, pts: dict) -> list:
        [...]
    def _zdws_open_position(self, underlying: str, side: str, stake: float,
        [...]
    def _zdws_market_close(self, warrant_id: int):
    [...]
    ZDOS_TIERS = [
        [...]
    ]
    ZDOS_POSTYPES = ['LONG_CALL', 'SHORT_CALL', 'LONG_PUT', 'SHORT_PUT']
    [...]
    ZDOS_STRATEGIES = {
        [...]
    }

    def _zdos_strategy_legs(self, strategy: str, base_tier: int) -> list:
        [...]
    def _zdos_tier(self, tier_num: int) -> dict:
        return next((t for t in self.ZDOS_TIERS if t['tier'] == tier_num), self.ZDOS_TIERS[0])

    def _zdos_strike_level(self, entry: float, tier_num: int, postype: str) -> float:
        [...]
    def _zdos_is_winning(self, pos: dict, move_pct) -> bool:
        [...]
    def _zdos_multiplier(self, pos: dict) -> float:
        [...]
    def _zdos_auto_settle(self, pts: dict) -> list:
        [...]
    def _zdos_open_position(self, underlying: str, postype: str, tier_num: int,
        [...]
    def _zdos_open_strategy(self, underlying: str, strategy: str, base_tier: int,
        [...]
    def _zdos_market_close(self, position_id: int, silent: bool = False):
        [...]
    def _zdos_close_strategy(self, position_ids: list):
        [...]
    def _pts_check_expired(self, pts: dict) -> list:
        [...]
    def _build_pts_tab(self):
        [...]
        def _labels_for_category(cat):
        [...]
        def _on_cat_filter(*_):
        [...]
        def _render_question(m, level_opt=None):
            [...]
        def _update_market_ui(*_):
            [...]
        def _update_q_text(*_):
            [...]
        def _preview_odds(*_):
            [...]
        def _update_settle_info(*_):
            [...]
        def _submit_prediction():
            [...]
        def _resolve_selected():
            [...]
        def _daily_topup():
            [...]
    def _pts_grant_daily_login(self):
        [...]
    def _pts_settlement_dates(self, include_daily: bool = False):
        [...]
    def _infer_ccy_from_symbol(self, sym: str) -> str:
        [...]
    _VFUND_CCY = {c['label']: c['ccy'] for c in VFUND_CONFIG}   # ISO codes: MYR/USD

    def _price_ccy_prefix(self, sym: str) -> str:
        [...]
    def _zdws_ccy(self, underlying: str) -> str:
        [...]
    def _zdws_underlyings(self) -> list:
        [...]
    def _build_zdws_tab(self):
        [...]
        def _left_wheel(e):
        [...]
        def _apply_group_filter(*_):
        [...]
        def _refresh_ctx(*_):
        [...]
        def _mint():
        [...]
    def _build_zdos_tab(self):
        [...]
        def _left_wheel(e):
        [...]
        def _apply_group_filter(*_):
        [...]
        def _toggle_strategy_mode(*_):
            [...]
        def _refresh_ctx(*_):
        [...]
        def _write():
        [...]
            def _render_leg_row(parent, o_pos, indent=0):
        [...]
    def _pts_generate_markets(self) -> list:
        [...]
    def _populate_table(self):
        [...]
    def _set_table_columns(self, val_cols):
        [...]
    def _update_stats(self):
        [...]
    def _select_all(self):
        [...]
    def _deselect_all(self):
        [...]
    def _select_core(self):
        [...]
    def _select_yseries(self):
        [...]
    def _select_trseries(self):
        [...]
    def _select_top10(self):
        [...]
    def _select_fund(self):
        [...]
    def _select_vfund(self):
[...]
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
[...]
_NEWPOR_CCY_MAP = {
    [...]
}
[...]
MARKET_SPEC = {
    [...]
}
# market code -> ccy, for reverse lookups
_MARKET_TO_CCY = {v["market"]: k for k, v in MARKET_SPEC.items()}


def ccy_symbol(ccy: str) -> str:
[...]
_MF_PRICE_CACHE: dict = {}

def _mf_ticker_for_market(ticker: str, market: str) -> str:
    [...]
def _mf_as_price(value):
    [...]
def _mf_get_price(ticker: str, market: str, fallback=None):
[...]
_MF_HIST_PRICE_CACHE: dict = {}
_MF_DIV_CACHE: dict = {}   # ticker (with .KL) -> pd.Series of dividends (full history)
_MF_EPS_CACHE: dict = {}   # ticker -> float EPS

def _preload_div_cache_from_disk():
[...]
def _mf_get_price_history(ticker: str, market: str, start_date=None):
    [...]
def _mf_fmt_price(v)  -> str: return f"{v:,.4f}"  if v is not None else "N/A"
def _mf_fmt_money(v)  -> str: return f"{v:,.2f}"  if v is not None else "N/A"
def _mf_fmt_signed(v) -> str: return f"{v:+,.2f}" if v is not None else "N/A"
def _mf_fmt_pct(v)    -> str: return f"{v:+.2f}%" if v is not None else "N/A"

def _fmt_units(v) -> str:
    [...]
def _port_display_name(port_key: str) -> str:
    [...]
def _port_ccy(port_key: str) -> str:
[...]
def _mf_norm(k: str) -> str:
[...]
_NC_DATE   = "date"
_NC_TYPE   = "transactiontype"   # Deposit / Buy / Sell / Fees / Withdraw / Dividend
_NC_ASSET  = "assetcode"
_NC_CCY    = "currency"
_NC_UNIT   = "unit"
_NC_VALUE  = "value"             # price per unit (may have $, RM prefix)
_NC_AMOUNT = "amount"            # total amount    (may have $, RM prefix / negative)

_CASH_ASSETS = {"USD-CASH", "MYR-CASH", "SGD-CASH"}  # cash instrument codes

def _mf_strip_currency(s: str) -> float:
    [...]
_NEWPOR_CACHE_PREFIX = 'ledger'   # per-K-ID cache: ledger_<kid>.csv next to teet.csv
[...]
_LEDGER_CACHE_EPOCH = '2026-07-v2-fullhash'


def _purge_stale_ledger_caches():
[...]
def _mf_fetch_online_chunked(url: str, log_cb=None, chunk_size: int = 200):
    [...]
    def log(m):
    [...]
def _mf_delta_sync(online_text: str, cache_path: str, log_cb=None) -> tuple[str, bool]:
    [...]
    def log(m):
    [...]
    def _digest(body):
    [...]
def _prefetch_sca_dividends(portfolios: dict, log_cb=None, chunk_size: int = 5):
    [...]
    def log(m):
    [...]
    def _fetch_one_div(ticker_key, lookup):
    [...]
def _mf_parse_rows(raw_text: str, log_cb=None) -> list:
    """Parse ledger CSV text into normalised row dicts.
    Shared by the online-sheet path and the imported-CSV path."""
    def log(m):
    [...]
def _mf_load_csv(url: str = None, log_cb=None,
    [...]
    def log(m):
    [...]
def _mf_build_portfolios(rows: list) -> dict:
    [...]
def _mf_validate_ledger(rows: list) -> dict:
[...]
class MFChartWindow(tk.Toplevel):
    _PAL = [
        [...]
    ]

    def __init__(self, parent, portfolio: dict, all_portfolios: dict):
        [...]
    def _switch(self, label, draw_fn):
        [...]
    def _new_fig(self, w=9, h=5.4):
        return self._Fig(figsize=(w, h), facecolor=_MF_BG)

    def _embed(self, fig):
        [...]
    def _ax_dark(self, ax):
        [...]
    def _no_data(self, ax, msg="No priced holdings to display"):
        [...]
    def _holdings_rows(self, port=None):
        [...]
    def _draw_tp_ta(self):
        [...]
    def _draw_tp_pie(self):
        [...]
    def _draw_sp_pie(self):
        [...]
    def _draw_pl_bar(self):
        [...]
    def _draw_pl_pct(self):
[...]
class MYFOLIOWindow(tk.Toplevel):
    [...]
    PERIODS = ['1D', '5D', '1M', '3M', '6M', 'YTD', '1Y', 'All']

    def __init__(self, parent, preloaded_rows=None, preloaded_portfolios=None):
    [...]
    def _build_ui(self):
    [...]
    def _make_fig(self):
        [...]
    def _style_ax(self, ax):
        [...]
    def _embed_fig(self, fig, parent):
        [...]
    def _build_line_tab(self):
        [...]
    def _build_bar_tab(self):
        [...]
    def _build_table_tab(self):
        [...]
    def _build_total_tab(self):
        [...]
    def _build_comp_tab(self):
    [...]
    def _tick_clock(self):
    [...]
    def _switch_currency(self, ccy: str):
        [...]
    def _portfolios_for_currency(self, ccy: str) -> dict:
    [...]
    def _load_async(self):
        [...]
    def _fetch_thread(self):
        def _log(msg):
        [...]
    def _on_loaded(self, portfolios: dict, row_count: int = None):
        [...]
    def _on_error(self, msg: str):
        [...]
    def _rebuild_list(self):
        [...]
    def _on_select(self, _e=None):
        [...]
    def _on_period_change(self):
        [...]
    def _refresh_detail(self):
        [...]
    def _redraw_active_tab(self):
        [...]
    def _refresh_summary(self, port: dict):
        [...]
    def _draw_line_chart(self, port: dict):
        [...]
    def _draw_bar_chart(self, port: dict):
        [...]
    def _draw_table(self, port: dict):
        [...]
        def _parse_date(s):
    [...]
    def _set_total_ccy(self, opt: str):
        [...]
    def _draw_total(self):
        [...]
    def _draw_comp(self, port: dict):
[...]
LOTTO_STOCK_LIST = [
    [...]
]

LOTTO_RETAIL     = {"7084", "5326", "5296"}
LOTTO_HEALTHCARE = {"5225"}
[...]
LOTTO_ETF_LIST = [
    [...]
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
[...]
def lotto_derive_owned_from_portfolios(ports: dict):
    [...]
def lotto_pull_sca_state(log_cb=None):
    [...]
def lotto_record_etf_buy_to_portfolios(portfolios: dict, code: str,
    [...]
def lotto_record_buy_to_myfolio(portfolios: dict, code: str, units: float,
[...]
def lotto_rand_pickmax_for(code: str, owned: set, units_map: dict) -> int:
    [...]
def lotto_get_extra_buy(code: str, owned: set) -> tuple:
    [...]
def lotto_fetch_live_price(code: str, is_etf: bool = False):
[...]
class LottoStockWindow(tk.Toplevel):
    def __init__(self, parent, preloaded_rows=None, preloaded_portfolios=None,
    [...]
    def _build_lock_screen(self):
        [...]
    def _show_lock_screen(self):
        [...]
    def _hide_lock_screen(self):
        [...]
    def _update_lock_text(self, validation: dict):
        [...]
    def _build_ui(self):
    [...]
    def _refresh_sca_link(self, initial=False):
        [...]
        def _log(msg):
            [...]
        def worker():
        [...]
    def _on_sca_synced(self, bursa_owned, bursa_units, etf_owned, etf_units,
    [...]
    def _build_road_section(self):
        [...]
    def refresh_road_price(self):
        [...]
        def worker():
        [...]
    def set_candidate_target(self, code, name):
        [...]
    def _arm_road_buy(self):
        [...]
    def _cancel_road_buy(self):
        [...]
    def open_road_confirm(self):
    [...]
    def _on_mode_change(self):
        [...]
    def _on_road_target_change(self):
        [...]
    def _current_road_goal(self) -> int:
        return self._road_target_var.get()

    def _reset_pool(self):
        [...]
    def _selected_top_n(self):
        [...]
    def handle_draw(self):
        [...]
    def _spin_animation(self):
        [...]
    def _on_draw_complete(self):
        [...]
    def _reset_top_display(self):
    [...]
    def _confirm_transaction(self, stock):
        [...]
    def _skip_transaction(self, stock):
        [...]
    def mark_buy(self, stock):
        [...]
    def _accept_road_switch(self):
        [...]
    def _decline_road_switch(self):
    [...]
    def _refresh_bonus_banner(self):
        [...]
    def _refresh_top_section(self):
        [...]
    def _refresh_tx_section(self):
        [...]
    def _refresh_table(self):
[...]
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
        [...]
    def _force_source_setup(self):
        [...]
    def _load_from_cache_then_refresh(self):
        """Load this K-ID's cached ledger for instant display, then refresh."""
        def _worker():
        [...]
    def _on_cache_loaded(self, ports):
    [...]
    def _build_ui(self):
        [...]
    def _scroll_frame(self):
        [...]
    def _build_portfolio_chart(self):
        [...]
    def _style_pchart_ax(self):
        [...]
    def _redraw_portfolio_chart(self):
        [...]
    def _clear_pchart_hover(self):
        [...]
    def _on_pchart_hover(self, event):
        [...]
    def _view_options(self):
        [...]
    def _rebuild_view_buttons(self):
        [...]
    def _build_header(self):
        [...]
    def _refresh_source_label(self):
        [...]
    def _open_source_dialog(self, first_run: bool = False) -> bool:
        [...]
        def _browse():
        [...]
        def _save_and_load():
        [...]
    def _build_error_banner(self):
        [...]
    CCY_ACCENT = {'MYR': '#00b4d8', 'USD': '#00e676',
                  'HKD': '#ff6b6b', 'SGD': '#ffd166'}

    def _build_summary_cards(self):
        [...]
    def _rebuild_summary_cards(self):
        [...]
    def _make_summary_card(self, parent, title, accent, ccy):
        [...]
    def _build_tabs(self):
        [...]
    def _toggle_pie(self):
        [...]
    def _draw_pie(self):
        [...]
    _SORT_ARROWS = {None: '', False: '  ▲', True: '  ▼'}

    @staticmethod
    def _sort_key(val: str):
        [...]
    def _sort_holdings(self, col: str):
        [...]
    def _reapply_sort(self):
        [...]
    def _build_holdings_table(self):
        [...]
    def _refresh_today_activity(self):
        [...]
    def _build_weight_section(self):
        [...]
    def _build_footer(self):
        [...]
        def _open_sheet():
            [...]
        def _copy_url():
        [...]
    def _an_refresh_portfolio_dropdown(self):
        [...]
    def _an_redraw(self):
        [...]
    def _refresh(self, force_online: bool = True):
        [...]
    def _load_worker(self, force_online: bool = True):
        [...]
    def _on_sheet_loaded(self, ports, valid, n_before=0, n_after=0):
        [...]
    def _flash_status(self, msg: str, secs: int = 6):
        [...]
    def _on_sheet_error(self, msg):
        [...]
    def _apply_portfolios(self, ports):
        [...]
    def _render_from_cache(self):
        [...]
    def _fetch_live_prices(self):
        threading.Thread(target=self._price_worker, daemon=True).start()

    def _price_worker(self):
        [...]
        def _fx_one(ccy):
        [...]
        def _fetch_one(ticker, mkt):
        [...]
    def _on_prices_loaded(self, price_data, history_data, fx_ok):
    [...]
    def _render_all(self, price_data, history_data, placeholder=False):
        [...]
    def _update_card(self, card, port, ccy, price_data, usdmyr,
        [...]
    def _render_weight_bars(self, ports_to_show, price_data):
        [...]
    def _spark_text(self, hist_series):
    [...]
    def _on_view_change(self):
        [...]
    def _on_tab_change(self):
        [...]
    def _on_row_click(self, event):
        [...]
    def _open_stock_chart(self, ticker):
        [...]
        def _on_body_configure(e):
        [...]
        def _on_mousewheel(e):
        [...]
        def _parse(s):
        [...]
        def _draw_chart(hist):
            [...]
        def _fetch_and_draw():
        [...]
        def _calc_dividend(ttm_div, pat, policy_rate_pct):
            [...]
        def _update_result(*_):
        [...]
        def _fetch_div_data():
        [...]
            def _get_dividends():
                [...]
            def _get_eps():
            [...]
        def _apply_div_data(ttm_div, pat, div_history):
            [...]
        def _apply_div_error(msg):
        [...]
        def _build_actual_table(div_history):
        [...]
    def _open_add_dialog(self):
    [...]
    def _set_indicator(self, sheet=None, fx=None):
        [...]
    def _show_banner(self, msg, sub=''):
        [...]
    def _hide_banner(self):
        self._banner_frame.pack_forget()

    def _update_time(self):
        [...]
    def _schedule_auto(self):
        [...]
    def _auto_tick(self):
[...]
class SplashScreen(tk.Tk):
    def __init__(self):
        [...]
    def _build_ui(self):
        [...]
    def _refresh_file_sizes(self):
        [...]
    def _log(self, msg):
        [...]
    def _animate_spinner(self):
        [...]
    def _start_load(self):
        [...]
    def _load_thread(self, path, prefer_online=True, apply_sell_fee=True):
        [...]
        def _fetch_teet():
            [...]
        def _fetch_mybook():
        [...]
    def _on_error(self, msg):
        [...]
    def _launch_dashboard(self):
        [...]
    def _open_dashboard(self):
[...]
APPLOCK_PIN = "1234"   # retired — kept only so old imports don't break;
                       # K-ID sign-in replaced PIN-only access entirely.
_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'paa_settings.json')
_DEFAULT_SETTINGS = {
    [...]
}
[...]
def kid_hash_pin(username: str, pin: str) -> str:
    [...]
def kid_find_account(settings: dict, username: str):
    [...]
ADMIN_KID = 'admin1'   # reserved administrator K-ID
# Default admin PIN is 0000 — stored as its salted hash (sha256('admin1::0000')),
# overridable by settings['admin_pw_hash'] if the admin ever changes it.
ADMIN_DEFAULT_PIN_HASH = '4cc5c192af04c46d777fc80c005df5f8baac04a4db8b9e3350f64a465376cd76'

def kid_verify(settings: dict, username: str, pin: str) -> bool:
    [...]
def kid_create(settings: dict, username: str, pin: str) -> tuple:
[...]
CURRENT_KID = ''   # set at login; '' = no account (legacy PIN mode)

# ── Presence / online-status heartbeat ───────────────────────────────────────
PRESENCE_ONLINE_SECS = 300   # 'online' = seen within the last 5 minutes
_presence_last_write = 0.0   # module-level throttle so we don't hammer disk

def presence_touch(login: bool = False):
    [...]
def sca_normalise_sheet_url(url: str) -> str:
    [...]
def sca_has_source(settings: dict, username: str = None) -> bool:
[...]
def kid_recovery_questions(rows: list, portfolios: dict, n: int = 3) -> list:
    [...]
    def _decoys(correct, pool, k=3):
    [...]
def kid_load_recovery_data(settings: dict, username: str):
    [...]
def kid_reset_pin(settings: dict, username: str, new_pin: str) -> tuple:
    [...]
def sca_get_source(settings: dict, username: str = None) -> dict:
    [...]
def sca_save_source(settings: dict, username: str, mode: str,
[...]
def _pts_fresh_wallet() -> dict:
    [...]
def load_app_settings() -> dict:
    [...]
def save_app_settings(settings: dict):
    [...]
class AppLockScreen(tk.Tk):
    [...]
    def __init__(self):
    [...]
    def _build_ui(self):
        [...]
    def _open_recovery(self):
        [...]
        def _build_quiz():
            [...]
        def _verify():
        [...]
        def _bg():
        [...]
    def _open_reset_pin(self, user: str):
        [...]
        def _apply():
        [...]
    def _toggle_mode(self):
    [...]
    def _create(self):
        [...]
    def _sign_in(self):
    [...]
    ADMIN_RCODE_SHA256 = ('7e23f29f13be8c3431f50d6c92fe6b4eedf651cfa0cc16aece474e67bdd5128f')

    def _admin_rcode_hash(self) -> str:
        [...]
    def _admin_rcode_check(self) -> bool:
        [...]
        def _submit(_=None):
        [...]
    def _admin_recovery_flow(self) -> bool:
        [...]
        def _submit():
        [...]
    def _launch(self, username: str):
[...]
