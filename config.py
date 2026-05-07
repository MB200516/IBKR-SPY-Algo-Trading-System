"""
config.py — Single source of truth for all system parameters.
Never hardcode values in other modules. Import from here.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class IBKRConfig:
    host: str = "127.0.0.1"
    port: int = 7497          # 7497 = paper, 7496 = live
    client_id: int = 1
    timeout: float = 30.0     # seconds to wait for connection
    readonly: bool = False
    use_delayed_data: bool = True   # Set True if you have no live market data subscription.
                                    # Uses IBKR market data type 3 (delayed, free).
                                    # Set False for live/frozen data (requires subscription).
                                    # WARNING: Delayed data is for TESTING ONLY — do not use
                                    # for live trading decisions.


@dataclass
class InstrumentConfig:
    symbol: str = "SPY"
    sec_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    timeframe: str = "1 min"   # IBKR bar size string
    rth_only: bool = True


@dataclass
class TradingHoursConfig:
    market_open_hour: int = 9
    market_open_minute: int = 30
    market_close_hour: int = 16
    market_close_minute: int = 0
    last_entry_hour: int = 15
    last_entry_minute: int = 50   # No new entries after 3:50 PM
    timezone: str = "US/Eastern"


@dataclass
class RiskConfig:
    account_size: float = 10_000.0
    risk_per_trade_pct: float = 0.002       # 0.2% = $20 max loss per trade
    daily_loss_limit_pct: float = 0.006     # 0.6% = $60 daily loss limit
    max_notional_per_trade: float = 5_000.0
    max_trades_per_day: int = 20
    cooldown_bars: int = 2
    max_consecutive_losses: int = 2

    @property
    def risk_per_trade_dollars(self) -> float:
        return self.account_size * self.risk_per_trade_pct

    @property
    def daily_loss_limit_dollars(self) -> float:
        return self.account_size * self.daily_loss_limit_pct


@dataclass
class StrategyConfig:
    # Indicators
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 7
    vwap_zone_pct: float = 0.0015      # wider than original 0.001, not tighter


    # Long entry RSI bounds
    long_rsi_low: float = 45.0      # was 35.0 — don't enter deeply oversold
    long_rsi_high: float = 65.0     # was 75.0 — don't enter already overbought

    # Short entry RSI bounds
    short_rsi_low: float = 35.0     # was 25.0
    short_rsi_high: float = 55.0    # was 65.0

    # EMA slope threshold — trend must be actively moving
    ema_slope_threshold: float = 0.01   # halved


    # Volume filter — bar volume must be this % of 20-bar average
    min_volume_ratio: float = 0.8       # skip bars below 80% of avg volume


@dataclass
class ExitConfig:
    stop_loss_pct: float = 0.002      # 0.20%
    take_profit_pct: float = 0.002  # 0.35%
    time_stop_bars: int = 10           # was 15 — cut losers faster


@dataclass
class SystemConfig:
    # Operational mode
    # "readonly"  → signals printed only, no orders
    # "paper"     → paper trading, real IBKR orders
    # "live"      → live trading (use with extreme caution)
    mode: str = "readonly"

    # Paths
    log_dir: str = "logs"
    data_dir: str = "data"
    trade_log_file: str = "logs/trades.csv"
    daily_summary_file: str = "logs/daily_summary.csv"
    error_log_file: str = "logs/errors.log"
    hist_data_file: str = "data/spy_1min_hist.csv"

    # Historical warm-up
    warmup_days: int = 5        # Days of history to pull on startup

    # Reconnect
    max_reconnect_attempts: int = 10
    reconnect_delay_seconds: float = 5.0

    # Logging level: DEBUG, INFO, WARNING, ERROR
    log_level: str = "INFO"


@dataclass
class AppConfig:
    ibkr: IBKRConfig = field(default_factory=IBKRConfig)
    instrument: InstrumentConfig = field(default_factory=InstrumentConfig)
    hours: TradingHoursConfig = field(default_factory=TradingHoursConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    system: SystemConfig = field(default_factory=SystemConfig)


# ── Default singleton config ────────────────────────────────────────────────
# Import this in all other modules:
#   from config import config
config = AppConfig()
