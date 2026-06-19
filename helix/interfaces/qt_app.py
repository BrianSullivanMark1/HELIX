from __future__ import annotations

import array
import logging
import math
import os
import re
import sys
import tempfile
import wave
from datetime import date, datetime, timedelta, timezone

from PyQt6.QtCore import Qt, QPointF, QRectF, QTimer, QObject, QRunnable, QThreadPool, QUrl, pyqtSignal
from PyQt6.QtTextToSpeech import QTextToSpeech, QVoice
from PyQt6.QtMultimedia import QAudioFormat, QAudioOutput, QAudioSource, QMediaDevices, QMediaPlayer
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from helix.ai.claude import (
    CLAUDE_API_KEY_SETTING,
    ClaudeClient,
    ClaudeConfig,
    ClaudeError,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_RESEARCH_MODEL,
    estimate_cost,
)
from helix.ai.mock import (
    generate_mock_portfolio_research,
    generate_mock_research,
)
from helix.ai.speech import synthesize_speech
from helix.ai.transcribe import is_available as stt_available, is_ready as stt_ready, transcribe
from helix.ai.actions import (
    ActionContext,
    ActionRouter,
    is_affirmative,
    is_negative,
    run_chat_turn,
)
from helix.ai.research import (
    build_enterprise_summary_prompt,
    build_jarvis_chat_system,
    build_portfolio_research_prompt,
    build_research_prompt,
    parse_research_json,
)
from helix.enterprise.slack import (
    SLACK_TOKEN_SETTING,
    SLACK_USER_SCOPES,
    SlackClient,
    SlackError,
    format_slack_digest,
    gather_slack_digest,
)
from helix.enterprise.gitwork import (
    ENTERPRISE_REPOS_SETTING,
    format_git_digest,
    gather_git_digest,
    parse_repos,
)
from helix.core.config import load_config
from helix.investment.autopilot import (
    DAYTRADE_SETTING,
    DEFAULT_DAYTRADE_ALLOCATION_PCT,
    DEFAULT_INDEX_ALLOCATION_PCT,
    DEFAULT_INDEX_SYMBOL,
    DEFAULT_DAYTRADE_RESEARCH_DAYS,
    DEFAULT_CORE_STOP_LOSS_PCT,
    DEFAULT_DEFENSIVE_CASH_BUFFER_PCT,
    DEFAULT_DRAWDOWN_BRAKE_PCT,
    DEFAULT_MIN_POSITIONS,
    DEFAULT_MAX_POSITIONS,
    DEFAULT_MIN_POSITION_USD,
    DEFAULT_TRIM_BAND_PCT,
    DEFAULT_RATING_MAX_AGE_DAYS,
    DEFAULT_ROSTER_REVIEW_DAYS,
    DEFAULT_SECTOR_CAP_PCT,
    DEFAULT_SPECIAL_ALLOCATION_PCT,
    DEFAULT_SPECIAL_RESEARCH_DAYS,
    INVEST_RESEARCH_TOKENS_SETTING,
    RiskControls,
    LAST_DAYTRADE_RESEARCH_SETTING,
    LAST_SPECIAL_RESEARCH_SETTING,
    RESEARCH_EFFORT_LEVELS,
    RESEARCH_TIMEOUT_SECONDS,
    SPECIAL_SETTING,
    research_max_tokens,
    EquitySeries,
    benchmark_series,
    build_rebalance_plan,
    composite_factor_scores,
    discover_market_candidates,
    equity_series_from_rows,
    execute_rebalance,
    generate_rating_scorecard,
    maybe_refresh_core_ratings,
    maybe_research_daytrade,
    maybe_research_special,
    maybe_rotate_roster,
    performance_digest,
    normalize_roster,
    parse_portfolio_history,
    parse_stock_bars,
    portfolio_snapshot,
    refresh_scorecard_feedback,
    screen_candidates,
    SCREEN_PROFILES,
    tradable_assets,
    tradable_symbols,
)
from helix.investment.market_data import build_market_context, factor_signals, regime_risk_off, volatility_signals
from helix.investment.fundamentals import fetch_fundamentals, fundamental_score, fundamentals_block
from helix.investment.sectors import fetch_sectors, sector_of, sectors_for
from helix.brokers.alpaca import (
    ALPACA_API_KEY_SETTING,
    ALPACA_ENV_LIVE,
    ALPACA_ENV_PAPER,
    ALPACA_ENVIRONMENT_SETTING,
    ALPACA_SECRET_KEY_SETTING,
    AlpacaClient,
    AlpacaError,
)
from helix.core.memory import SQLiteMemory
from helix.core.settings import AppSettings
from helix.core.reliability import LOGGER_NAME, install_crash_guard, setup_logging
from helix.selfdev import engine as selfdev_engine, mailer as selfdev_mailer, restart as selfdev_restart, triggers as selfdev_triggers
from helix.vision import analyze as vision_analyze, camera as vision_camera, watch as vision_watch
from helix.investment.models import InvestmentProfile, RISK_LEVELS
from helix.investment.planner import build_briefing, render_briefing
from helix.home.tasks import HOME_TASKS_SETTING, due_tasks, task_status
from helix.home.notify import (
    CARRIER_CHOICES,
    DEFAULT_SENDER,
    SMS_APP_PASSWORD_SETTING,
    SMS_CARRIER_SETTING,
    SMS_PHONE_SETTING,
    SMS_SENDER_SETTING,
    is_configured,
    send_reminder,
)


_LOG = logging.getLogger(LOGGER_NAME)

DEFAULT_EMERGENCY_MONTHS = 6
DEFAULT_PRIMARY_GOAL = "Build long-term wealth"
RISK_RETURN_ASSUMPTIONS = {
    "conservative": 0.04,
    "balanced": 0.06,
    "growth": 0.075,
    "aggressive": 0.09,
}
AI_MODE_SETTING = "learning_ai_mode"
AI_MODE_MOCK = "Mock Claude"
AI_MODE_CLAUDE = "Claude API"
INVESTMENT_AMOUNT_SETTING = "investment_amount"
TRADE_AMOUNT_DOLLARS = "Dollars"
TRADE_AMOUNT_SHARES = "Shares"
INVEST_PRESET_SETTING = "invest_preset"
INVEST_AI_MODE_SETTING = "invest_ai_mode"
INVEST_MODEL_SETTING = "invest_model"
INVEST_MODE_PRACTICE = "Practice (paper money)"
INVEST_MODE_REAL = "Real (live money)"
INVEST_SPECIAL_ALLOCATION_SETTING = "invest_special_allocation_pct"
INVEST_DAYTRADE_ALLOCATION_SETTING = "invest_daytrade_allocation_pct"  # day-trade sleeve % (§27)
INVEST_INDEX_ALLOCATION_SETTING = "invest_index_allocation_pct"  # index-core sleeve % (§42)
INVEST_INDEX_SYMBOL_SETTING = "invest_index_symbol"             # the index ETF to hold (default VOO)
INVEST_CORE_SATELLITE_APPLIED_SETTING = "invest_core_satellite_applied"  # one-time core-satellite mix migration
INVEST_SPECIAL_FUNDING_SETTING = "invest_special_funding"  # "house" (profits only) | "always" (deploy the % from day one)
INVEST_AI_RESEARCH_SETTING = "invest_ai_research"          # on = refresh research on cadence (Claude $); off = trade off cached only
INVEST_CORE_RATING_DAYS_SETTING = "invest_core_rating_days"  # re-rate the HELIX 500 every N days (default 7)
INVEST_SPECIAL_DAYS_SETTING = "invest_special_days"          # scout special stocks every N days (default 1)
INVEST_DAYTRADE_DAYS_SETTING = "invest_daytrade_days"        # scout day-trade momentum every N days (default 1, §27)
INVEST_MAX_POSITIONS_SETTING = "invest_max_positions"        # cap the core at top-N buy names (0 = uncapped, §30)
INVEST_VOL_ADJUST_SETTING = "invest_vol_adjust"              # volatility-adjusted (inverse-vol) sizing tilt (§31)
INVEST_FACTOR_OVERLAY_SETTING = "invest_factor_overlay"      # blend a composite factor over the LLM rating (§33)
INVEST_ADVERSARIAL_SETTING = "invest_adversarial"            # bull/bear/judge stress-test of top buys (§34)
# Risk controls (§35) — protective; default ON. Each a simple toggle; thresholds are the DEFAULT_* constants.
INVEST_SECTOR_CAP_SETTING = "invest_sector_cap"              # cap any one sector's share of the book
INVEST_DRAWDOWN_BRAKE_SETTING = "invest_drawdown_brake"      # raise cash when the account is in drawdown
INVEST_REGIME_SETTING = "invest_regime_filter"              # raise cash when the market is risk-off
INVEST_STOP_LOSS_SETTING = "invest_stop_loss"               # exit a core holding that blows up
INVEST_DIVERSIFY_SETTING = "invest_diversify_floor"         # never concentrate below the floor of names
LAST_SECTORS_FETCH_SETTING = "invest_last_sectors"          # timestamp of the last SEC sector enrichment
DEFAULT_SECTORS_DAYS = 180                                  # sectors are ~static; refresh rarely
SECTORS_FETCH_LIMIT = 150                                   # SEC sector lookups per run (bounded; fills over a few cycles)
LAST_ASSETS_FETCH_SETTING = "invest_last_assets"            # timestamp of the last real-market-list refresh (§36)
DEFAULT_ASSETS_DAYS = 7                                     # refresh the tradeable-asset universe weekly
INVEST_FUNDAMENTALS_SETTING = "invest_use_fundamentals"      # feed SEC fundamentals into the rating prompt (§32)
INVEST_FUNDAMENTALS_DAYS_SETTING = "invest_fundamentals_days"  # how often to refresh fundamentals from SEC (days)
LAST_FUNDAMENTALS_FETCH_SETTING = "invest_last_fundamentals"  # timestamp of the last SEC fundamentals refresh
DEFAULT_FUNDAMENTALS_DAYS = 30                               # fundamentals change quarterly; refresh monthly
INVEST_ROSTER_DAYS_SETTING = "invest_roster_days"            # review/rotate the roster every N days (default 90)
INVEST_LAST_RESEARCH_ISSUE_SETTING = "invest_last_research_issue"  # last "research parsed to nothing" diagnostic (§10)
SMS_AUTO_ENABLED_SETTING = "sms_auto_enabled"              # auto-text due tasks on a timer while the app is open
SMS_AUTO_HOURS_SETTING = "sms_auto_hours"                  # how often (hours)
INVEST_PRINCIPAL_SETTING = "invest_principal"  # protected base for house-money special funding (§21)
INVEST_CASH_BUFFER_SETTING = "invest_cash_buffer_pct"
INVEST_AUTO_INTERVAL_SETTING = "invest_auto_interval"
INVEST_LAST_CYCLE_OK_SETTING = "invest_last_cycle_ok"  # heartbeat: timestamp of the last successful auto cycle (§39)
INVEST_AUTO_RUNNING_SETTING = "invest_auto_running"  # persisted RUNNING state so a relaunch can resume paper trading (§39)
INVEST_DISCOVERY_OFFSET_SETTING = "invest_discovery_offset"  # rotating scan position over the tradeable market (§40)
DISCOVERY_SCAN_LIMIT = 400   # tradeable names scanned per roster review (bounded; rotates over cycles)
DISCOVERY_TOP_N = 25         # data-ranked candidates handed to the model to judge (§40)
AUTO_INTERVALS = {
    "15 minutes": 900000,
    "1 hour": 3600000,
    "4 hours": 14400000,
    "1 day": 86400000,
}
# When the market is closed, re-check at most this often (a free clock call, no Claude spend) so the
# loop wakes up near the next open instead of sleeping a full interval.
MARKET_CLOSED_RETRY_MS = 900000  # 15 min
INVEST_CHART_RANGE_SETTING = "invest_chart_range"
# Label -> (Alpaca period, timeframe, days-of-local-history). >30d periods must use 1D.
CHART_RANGES = {
    "1D": ("1D", "5Min", 1),   # today's intraday equity (5-minute points); no daily S&P overlay (§19)
    "1W": ("1W", "1H", 7),
    "1M": ("1M", "1D", 31),
    "3M": ("3M", "1D", 93),
    "1Y": ("1A", "1D", 366),
}
DEFAULT_CHART_RANGE = "1M"
INVEST_TICKERS_SETTING = "invest_tickers"
# The "HELIX 500" — a broad S&P-500-style universe of large/mid-cap US equities. Rated in batches
# (chunked, §16) so the size never truncates a research call. The loader de-dupes/uppercases, and
# unknown/stale tickers degrade gracefully (Alpaca rejects unknown orders; the rating skips them).
DEFAULT_TICKERS = (
    "AAPL, MSFT, NVDA, GOOGL, GOOG, AMZN, META, AVGO, TSLA, ORCL, CRM, ADBE, AMD, ACN, CSCO, IBM, "
    "INTC, QCOM, TXN, INTU, NOW, AMAT, MU, LRCX, KLAC, SNPS, CDNS, ADI, MCHP, NXPI, ON, MPWR, FTNT, "
    "PANW, CRWD, ANET, DELL, HPQ, HPE, NTAP, WDC, STX, GLW, KEYS, TEL, APH, CDW, ZBRA, TDY, JNPR, "
    "FFIV, AKAM, SWKS, QRVO, TER, ENPH, FSLR, PLTR, SNOW, DDOG, NET, MDB, TEAM, WDAY, ADSK, ANSS, "
    "PAYC, FICO, IT, ROP, TYL, PTC, EPAM, GEN, SMCI, ARM, "
    "NFLX, CMCSA, DIS, T, VZ, TMUS, CHTR, WBD, FOXA, PARA, OMC, IPG, LYV, EA, TTWO, MTCH, PINS, "
    "SNAP, RBLX, ABNB, UBER, LYFT, DASH, BKNG, EXPE, EBAY, ETSY, "
    "HD, LOW, MCD, SBUX, NKE, TJX, ROST, LULU, ORLY, AZO, YUM, CMG, DRI, MAR, HLT, RCL, CCL, NCLH, "
    "GM, F, APTV, BWA, LEN, DHI, PHM, NVR, GRMN, POOL, WHR, NWL, TPR, RL, GPC, BBY, DG, DLTR, ULTA, "
    "KMX, LKQ, W, DPZ, "
    "WMT, COST, PG, KO, PEP, PM, MO, MDLZ, CL, KMB, GIS, K, HSY, STZ, KHC, KDP, MNST, KR, SYY, ADM, "
    "HRL, SJM, CAG, CPB, MKC, CLX, CHD, TSN, TAP, EL, KVUE, "
    "JPM, BAC, WFC, C, GS, MS, BLK, SCHW, AXP, SPGI, MCO, CME, ICE, COF, USB, PNC, TFC, BK, STT, "
    "NTRS, FITB, HBAN, RF, CFG, KEY, MTB, ALL, TRV, PGR, CB, AIG, MET, PRU, AFL, HIG, GL, CINF, WRB, "
    "L, AJG, MMC, AON, BRO, ACGL, FDS, MSCI, MKTX, NDAQ, CBOE, V, MA, PYPL, FI, GPN, COIN, HOOD, "
    "SOFI, ALLY, DFS, SYF, "
    "UNH, JNJ, LLY, ABBV, MRK, PFE, TMO, ABT, DHR, BMY, AMGN, GILD, CVS, CI, ELV, HUM, CNC, MOH, "
    "ISRG, MDT, SYK, BSX, BDX, EW, ZBH, BAX, RMD, STE, HOLX, IDXX, IQV, A, MTD, WAT, DGX, LH, RVTY, "
    "COO, ALGN, DXCM, PODD, VRTX, REGN, MRNA, BIIB, ILMN, INCY, TECH, BMRN, NBIX, EXAS, HSIC, CAH, "
    "MCK, COR, ZTS, DVA, UHS, HCA, CRL, RGEN, VTRS, GEHC, "
    "GE, CAT, HON, UNP, BA, RTX, LMT, GD, NOC, DE, MMM, EMR, ETN, ITW, PH, ROK, AME, DOV, IR, CMI, "
    "PCAR, FAST, GWW, URI, PAYX, ADP, CTAS, RSG, WM, CSX, NSC, FDX, UPS, ODFL, JBHT, CHRW, EXPD, "
    "LUV, DAL, UAL, AAL, ALK, GEV, TT, CARR, OTIS, JCI, LII, AOS, PNR, XYL, IEX, FTV, HUBB, NDSN, "
    "GGG, WAB, TXT, LDOS, LHX, HII, AXON, TDG, HWM, HEI, CSL, MAS, ALLE, "
    "XOM, CVX, COP, EOG, SLB, MPC, PSX, VLO, OXY, WMB, KMI, OKE, HES, DVN, FANG, HAL, BKR, APA, "
    "CTRA, EQT, TRGP, LNG, OVV, "
    "LIN, APD, SHW, ECL, FCX, NEM, NUE, DOW, DD, PPG, CTVA, VMC, MLM, ALB, IFF, LYB, CE, EMN, CF, "
    "MOS, FMC, PKG, IP, AMCR, BALL, AVY, SEE, NTR, STLD, RS, "
    "NEE, DUK, SO, D, AEP, SRE, EXC, XEL, PEG, ED, WEC, ES, EIX, DTE, PCG, AEE, CMS, CNP, ATO, NI, "
    "LNT, EVRG, FE, AES, PPL, NRG, PNW, CEG, VST, "
    "PLD, AMT, EQIX, CCI, PSA, O, WELL, DLR, SPG, SBAC, VICI, AVB, EQR, EXR, MAA, INVH, ARE, VTR, "
    "ESS, UDR, CPT, HST, KIM, REG, BXP, FRT, DOC, IRM, CBRE, WY"
)
DEFAULT_PRESET = "Aggressive"
DEFAULT_CASH_BUFFER = 10.0
AUTO_INTERVAL_MS = 900000


def _eastern_offset(d: date) -> timezone:
    """US/Eastern UTC offset for a date, by US DST rules (stdlib-only, no tz database needed):
    EDT (UTC-4) from the 2nd Sunday of March to the 1st Sunday of November, else EST (UTC-5).
    Lets us convert the market's ET hours to the user's local time via `.astimezone()`."""
    def nth_sunday(year: int, month: int, n: int) -> date:
        first = date(year, month, 1)
        first_sunday = 1 + (6 - first.weekday()) % 7  # weekday(): Mon=0 .. Sun=6
        return date(year, month, first_sunday + 7 * (n - 1))

    dst_start = nth_sunday(d.year, 3, 2)
    dst_end = nth_sunday(d.year, 11, 1)
    return timezone(timedelta(hours=-4 if dst_start <= d < dst_end else -5))


def _fmt_ampm(hour: int, minute: int) -> str:
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def _hhmm_to_et(hhmm) -> str:
    """'09:30' -> '9:30 AM' (already Eastern; just reformats)."""
    try:
        hh, mm = (int(part) for part in str(hhmm).split(":")[:2])
    except (ValueError, TypeError):
        return str(hhmm or "")
    return _fmt_ampm(hh, mm)


def _hhmm_to_local(d: date, hhmm) -> str:
    """'09:30' ET on date `d` -> the user's local clock time, e.g. '6:30 AM'."""
    try:
        hh, mm = (int(part) for part in str(hhmm).split(":")[:2])
    except (ValueError, TypeError):
        return ""
    eastern = datetime(d.year, d.month, d.day, hh, mm, tzinfo=_eastern_offset(d))
    local = eastern.astimezone()  # .astimezone() with no arg uses the OS local timezone
    return _fmt_ampm(local.hour, local.minute)


class NoScrollComboBox(QComboBox):
    """A combo box that ignores the scroll wheel, so scrolling the page never changes its value.

    Critical for the fake/real money toggle: an accidental wheel scroll must never flip Practice ->
    Real. Ignoring the event lets it bubble up to the scroll area, which scrolls the page instead.
    """

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt override)
        event.ignore()


class NoScrollDoubleSpinBox(QDoubleSpinBox):
    """A spin box that ignores the scroll wheel (same rationale as NoScrollComboBox)."""

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt override)
        event.ignore()


class WorkerSignals(QObject):
    finished = pyqtSignal(object)
    error = pyqtSignal(object)


class Worker(QRunnable):
    """Runs a no-arg callable on a background thread and reports back via signals."""

    def __init__(self, fn) -> None:
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # surfaced to the UI through the error signal
            self.signals.error.emit(exc)
        else:
            self.signals.finished.emit(result)


def spawn_worker(registry: set, work, done) -> None:
    """Run `work` off-thread; call `done(ok, payload)` on the UI thread when it finishes.

    `registry` keeps the worker referenced until completion so its signals are delivered.
    """
    worker = Worker(work)
    worker.setAutoDelete(False)
    registry.add(worker)
    worker.signals.finished.connect(lambda result: (registry.discard(worker), done(True, result)))
    worker.signals.error.connect(lambda exc: (registry.discard(worker), done(False, exc)))
    QThreadPool.globalInstance().start(worker)


def run_qt_app(memory: SQLiteMemory) -> int:
    log = setup_logging()
    install_crash_guard(log)  # an unhandled slot error logs + keeps the app alive, not abort (§39)
    log.info("HELIX desktop starting")
    # Speech-to-text is pre-warmed in main.py BEFORE PyQt6 is imported — building the ctranslate2 model
    # after Qt's native libs are loaded segfaults the process, and a bare `import PyQt6` is enough to
    # trip it (§23). By the time we get here PyQt6 is already imported, so we must NOT load the model
    # now — we only report readiness. If the pre-warm was skipped (e.g. under a debugger that imports Qt
    # before main.py), is_ready() is False and the voice paths (push-to-talk + hands-free) disable
    # themselves rather than attempt a crashing post-Qt load.
    log.info("speech-to-text %s", "ready" if stt_ready() else "unavailable (voice disabled this run)")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("HELIX")
    apply_hud_style(app)

    window = HelixMainWindow(memory)
    window.resize(980, 560)
    window.setMinimumSize(720, 480)
    window.show()
    exit_code = app.exec()
    QThreadPool.globalInstance().waitForDone(3000)
    log.info("HELIX desktop exited (code %s)", exit_code)
    # Qt + native multimedia/camera objects can fault during interpreter teardown on Windows, which
    # would make the process exit with a CRASH code (0xC0000409) even on a clean close — and the §39
    # supervisor would then relaunch on every normal exit. Flush, then exit HARD with the intended code,
    # skipping the crashy finalization: 0 stops the supervisor, RESTART_EXIT_CODE (42) relaunches it.
    import logging
    logging.shutdown()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(exit_code)
    return exit_code  # unreachable; kept for type sanity


_PRESENCE_TEXT = {
    "idle": "Standing by, sir.",
    "listening": "Listening…",
    "transcribing": "Catching that…",
    "thinking": "Thinking…",
    "acting": "On it…",
    "speaking": "Speaking.",
}


class PresenceOrb(QWidget):
    """HELIX's living presence — an animated orb that breathes when idle and reacts to its state
    (listening / thinking / acting / speaking). The signature JARVIS element of the Console."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(64, 64)
        self._state = "idle"
        self._level = 0.0
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)  # ~30 fps

    def set_state(self, state: str) -> None:
        self._state = state or "idle"

    def set_level(self, level: float) -> None:
        try:
            self._level = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            self._level = 0.0

    def _tick(self) -> None:
        self._phase += 0.09
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        base = min(self.width(), self.height()) * 0.26
        state = self._state
        color = QColor(255, 200, 87) if state == "speaking" else QColor(29, 216, 255)
        wobble = math.sin(self._phase)
        if state == "listening":
            amp = 0.12 + 0.55 * self._level
        elif state in ("thinking", "acting", "transcribing"):
            amp = 0.16
        elif state == "speaking":
            amp = 0.18
        else:
            amp = 0.06
        radius = base * (1.0 + amp * (0.5 + 0.5 * wobble))

        glow = QRadialGradient(QPointF(cx, cy), radius * 2.5)
        inner = QColor(color); inner.setAlpha(70); glow.setColorAt(0.0, inner)
        outer = QColor(color); outer.setAlpha(0); glow.setColorAt(1.0, outer)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QPointF(cx, cy), radius * 2.5, radius * 2.5)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        for index, alpha in enumerate((130, 70)):
            ring = QColor(color); ring.setAlpha(alpha)
            pen = QPen(ring); pen.setWidthF(2.0)
            painter.setPen(pen)
            rr = radius * (1.0 + 0.30 * index)
            painter.drawEllipse(QPointF(cx, cy), rr, rr)

        if state in ("thinking", "acting", "transcribing"):
            pen = QPen(QColor(color)); pen.setWidthF(3.0); pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            span = QRectF(cx - radius * 1.55, cy - radius * 1.55, radius * 3.1, radius * 3.1)
            painter.drawArc(span, int((self._phase * 55) % 360) * 16, 90 * 16)

        core = QRadialGradient(QPointF(cx, cy), radius)
        c0 = QColor(color); c0.setAlpha(235); core.setColorAt(0.0, c0)
        c1 = QColor(color); c1.setAlpha(110); core.setColorAt(1.0, c1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(core)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
        painter.end()


class AmbientTile(QFrame):
    """A small glanceable card on the Console — a label, a value, and a hint. Click to open the deep
    view. Awareness, not a menu."""

    def __init__(self, title: str, on_click=None, parent=None) -> None:
        super().__init__(parent)
        self._on_click = on_click
        self.setObjectName("ambientTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QFrame#ambientTile{border:1px solid #16323b;border-radius:10px;background:rgba(18,38,46,0.35);}"
            "QFrame#ambientTile:hover{border-color:#1dd8ff;}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(1)
        cap = QLabel(title)
        cap.setStyleSheet("color:#6fb3c0;border:none;")
        self._value = QLabel("…")
        self._value.setStyleSheet("font-weight:700;font-size:15px;border:none;")
        self._hint = QLabel("")
        self._hint.setStyleSheet("color:#7faebb;border:none;")
        lay.addWidget(cap)
        lay.addWidget(self._value)
        lay.addWidget(self._hint)

    def set_value(self, value: str, hint: str = "") -> None:
        self._value.setText(value)
        self._hint.setText(hint)

    def mousePressEvent(self, _event) -> None:
        if self._on_click:
            try:
                self._on_click()
            except Exception:
                pass


class ConsoleView(QWidget):
    """The single-screen HELIX Console: Presence orb + the conversation (the heart) + four ambient
    tiles (House · Money · Supplies · Self). Deep views are one tap or one sentence away."""

    def __init__(self, xpert: "XpertTab", memory: SQLiteMemory, open_view, parent=None) -> None:
        super().__init__(parent)
        self._xpert = xpert
        self.memory = memory
        self.settings = AppSettings()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.setSpacing(12)

        top = QHBoxLayout()
        self.orb = PresenceOrb()
        self.orb.setFixedSize(76, 76)
        title = QVBoxLayout()
        title.setSpacing(0)
        name = QLabel("HELIX")
        name.setObjectName("sectionHeader")
        self.presence = QLabel(_PRESENCE_TEXT["idle"])
        self.presence.setStyleSheet("color:#6fb3c0;")
        title.addWidget(name)
        title.addWidget(self.presence)
        more = QPushButton("More")
        more.setToolTip("Open the deep views — Home, Enterprise, Learning, Investment")
        more.clicked.connect(lambda: open_view(None))
        top.addWidget(self.orb)
        top.addSpacing(8)
        top.addLayout(title)
        top.addStretch(1)
        top.addWidget(more)
        layout.addLayout(top)

        layout.addWidget(xpert, 1)  # the conversation — the heart

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.tile_house = AmbientTile("House", lambda: open_view("home"))
        self.tile_money = AmbientTile("Money", lambda: open_view("investment"))
        self.tile_supplies = AmbientTile("Supplies", lambda: open_view("home"))
        self.tile_self = AmbientTile("Self", lambda: open_view("enterprise"))
        for tile in (self.tile_house, self.tile_money, self.tile_supplies, self.tile_self):
            tiles.addWidget(tile)
        layout.addLayout(tiles)

        self._orb_timer = QTimer(self)
        self._orb_timer.timeout.connect(self._sync_presence)
        self._orb_timer.start(70)
        self._tile_timer = QTimer(self)
        self._tile_timer.timeout.connect(self.refresh_tiles)
        self._tile_timer.start(30000)
        self.refresh_tiles()

        # Proactive door/area watch (§vision): cheap local motion detection on a 'door' camera; on
        # movement, one vision call describes who's there and HELIX announces it — only when idle.
        self._watch_det = vision_watch.MotionDetector()
        self._watch_workers: set = set()
        self._watch_busy = False
        self._watch_cooldown = 0
        self._watch_timer = QTimer(self)
        self._watch_timer.timeout.connect(self._watch_tick)
        self._watch_timer.start(8000)

    def _sync_presence(self) -> None:
        state = getattr(self._xpert, "_convo_state", "idle")
        self.orb.set_state(state)
        try:
            self.orb.set_level(self._xpert.level_bar.value() / 100.0)
        except Exception:
            pass
        self.presence.setText(_PRESENCE_TEXT.get(state, _PRESENCE_TEXT["idle"]))

    def refresh_tiles(self) -> None:
        """Update the glance tiles from local engine state (no network, so it never janks)."""
        try:
            cams = len(vision_camera.list_cameras(self.settings))
            due = len(due_tasks(self.settings.get(HOME_TASKS_SETTING) or []))
            self.tile_house.set_value(
                f"{cams} camera(s)" if cams else "No eyes yet",
                f"{due} chore(s) due" if due else "Chores clear",
            )
        except Exception:
            self.tile_house.set_value("—", "")
        try:
            rows = self.memory.list_equity_history(30)
            if rows:
                last = rows[-1]
                eq = float(last.get("equity") or 0.0)
                pl = float(last.get("unrealized_pl") or 0.0)
                self.tile_money.set_value(f"${eq:,.0f}", f"open P/L ${pl:+,.0f}")
            else:
                self.tile_money.set_value("—", "connect Alpaca")
        except Exception:
            self.tile_money.set_value("—", "")
        try:
            shopping = self.settings.get("shopping_list") or []
            self.tile_supplies.set_value(
                f"{len(shopping)} on the list" if shopping else "List empty",
                "tap to order" if shopping else "—",
            )
        except Exception:
            self.tile_supplies.set_value("—", "")
        try:
            pending = len(selfdev_engine.list_pending(self.settings))
            self.tile_self.set_value(
                f"{pending} change(s) ready" if pending else "All caught up",
                "tap to approve" if pending else "self-improving",
            )
        except Exception:
            self.tile_self.set_value("—", "")

    # ---- proactive door/area watch ---------------------------------------- #

    def _watch_tick(self) -> None:
        if self._watch_busy or not vision_camera.is_available():
            return
        if self._watch_cooldown > 0:
            self._watch_cooldown -= 1
            return
        cam = vision_camera.get_camera(self.settings, "door")
        if not cam:
            return  # opt-in: only watches if a camera named "door" is registered
        self._watch_busy = True
        source = cam.get("source", vision_camera.DEFAULT_CAMERA_INDEX)
        spawn_worker(self._watch_workers, lambda: self._watch_capture(source), self._watch_captured)

    def _watch_capture(self, source):
        frame = vision_camera.capture_jpeg(source)
        return frame if self._watch_det.check(frame) else None  # only a frame back if something moved

    def _watch_captured(self, ok: bool, payload) -> None:
        if not ok or payload is None:
            self._watch_busy = False
            return
        frame = payload
        spawn_worker(
            self._watch_workers,
            lambda: vision_analyze.describe_image(
                frame,
                question="Is a person at the door? If so describe them briefly (sex, rough age, what "
                "they're doing); if not, say what moved.",
                memory=self.memory,
            ),
            self._watch_described,
        )

    def _watch_described(self, ok: bool, payload) -> None:
        self._watch_busy = False
        self._watch_cooldown = 8  # ~1 minute of quiet before the next alert
        if ok and payload:
            self._xpert.announce(f"Someone's at the door, sir. {payload}")


class HelixMainWindow(QMainWindow):
    def __init__(self, memory: SQLiteMemory) -> None:
        super().__init__()
        self.memory = memory
        self.setWindowTitle("HELIX")
        self.setStatusBar(QStatusBar())

        # Deep domain views live behind 'More' or a sentence; Xpert is promoted to the Console itself.
        self.xpert_tab = XpertTab(memory)
        self.home_tab = HomeTab(memory)
        self.enterprise_tab = EnterpriseTab(memory)
        self.learning_tab = LearningTab(memory)
        self.investment_tab = InvestmentTab(memory, on_saved=self.refresh_all)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.home_tab, "Home")
        self.tabs.addTab(self.enterprise_tab, "Enterprise")
        self.tabs.addTab(self.learning_tab, "Learning")
        self.tabs.addTab(self.investment_tab, "Investment")

        more_page = QWidget()
        more_layout = QVBoxLayout(more_page)
        more_layout.setContentsMargins(10, 8, 10, 10)
        back_bar = QHBoxLayout()
        back_button = QPushButton("‹  Console")
        back_button.clicked.connect(self._show_console)
        back_bar.addWidget(back_button)
        back_bar.addStretch(1)
        more_layout.addLayout(back_bar)
        more_layout.addWidget(self.tabs, 1)

        self.console = ConsoleView(self.xpert_tab, memory, self.open_view)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.console)  # 0 — the JARVIS console (default)
        self.stack.addWidget(more_page)     # 1 — the deep domain tabs
        self.setCentralWidget(self.stack)

        # The Xpert assistant acts on the other pillars (start/stop investing, home tasks).
        self.xpert_tab.bind_investment(self.investment_tab.invest_tab)
        self.xpert_tab.bind_home(self.home_tab)

        self.refresh_all()

        # Self-improvement background beat (§selfdev): apply a pending restart on a safe tick.
        self.settings = AppSettings()
        selfdev_restart.clear_restart(self.settings)  # consume any flag from the session that just restarted
        self._selfdev_timer = QTimer(self)
        self._selfdev_timer.timeout.connect(self._selfdev_tick)
        self._selfdev_timer.start(60000)  # every 60s
        # Auto crash-fix (§selfdev): draft fixes for new logged crashes, off-thread, ~2 min after
        # launch and every 6 hours. Drafts only — never auto-merged (approval still required).
        self._sd_workers = set()
        self._crash_busy = False
        QTimer.singleShot(120000, self._check_crashes)
        self._crash_timer = QTimer(self)
        self._crash_timer.timeout.connect(self._check_crashes)
        self._crash_timer.start(6 * 3600 * 1000)
        # Email approval (§selfdev): poll for Brian's Yes/No replies, off-thread, every 3 min.
        self._email_busy = False
        self._email_timer = QTimer(self)
        self._email_timer.timeout.connect(self._poll_email)
        self._email_timer.start(180000)

    def refresh_all(self) -> None:
        self.xpert_tab.refresh()
        self.learning_tab.refresh()
        self.investment_tab.refresh()
        self.enterprise_tab.refresh()
        self.statusBar().showMessage("HELIX memory synced", 3000)

    def open_view(self, name: str | None = None) -> None:
        """Switch to a deep domain view (or just the More page if name is None)."""
        self.stack.setCurrentIndex(1)
        mapping = {
            "home": self.home_tab,
            "enterprise": self.enterprise_tab,
            "learning": self.learning_tab,
            "investment": self.investment_tab,
        }
        widget = mapping.get(name)
        if widget is not None:
            self.tabs.setCurrentWidget(widget)

    def _show_console(self) -> None:
        self.stack.setCurrentIndex(0)

    def _auto_trading(self) -> bool:
        """True while the Investment auto-loop is running — used to defer a restart so we never
        interrupt a live trade cycle."""
        try:
            return bool(getattr(self.investment_tab.invest_tab, "_running", False))
        except Exception:
            return False

    def _selfdev_tick(self) -> None:
        """Background self-improvement beat. Applies a pending restart when it's safe (not mid-trade)."""
        try:
            if selfdev_restart.restart_pending(self.settings) and not self._auto_trading():
                self.statusBar().showMessage("Restarting to apply a self-improvement…", 5000)
                QApplication.exit(selfdev_restart.RESTART_EXIT_CODE)
        except Exception:
            pass

    def _check_crashes(self) -> None:
        """Off-thread: draft fixes for any new logged crash (recorded pending; never auto-merged)."""
        if self._crash_busy:
            return
        self._crash_busy = True
        spawn_worker(
            self._sd_workers,
            lambda: selfdev_triggers.maybe_fix_crashes(self.settings),
            self._crashes_done,
        )

    def _crashes_done(self, ok: bool, payload) -> None:
        self._crash_busy = False
        if ok and payload:
            self.statusBar().showMessage(
                f"Drafted {len(payload)} crash fix(es) — ask Xpert to review and approve", 10000
            )

    def _poll_email(self) -> None:
        """Off-thread: apply any Yes/No email replies to pending self-improvements."""
        if self._email_busy or not selfdev_mailer.is_configured(self.settings):
            return
        self._email_busy = True
        spawn_worker(self._sd_workers, lambda: selfdev_mailer.poll_replies(self.settings), self._email_done)

    def _email_done(self, ok: bool, payload) -> None:
        self._email_busy = False
        if ok and payload:
            applied = ", ".join(f"{a['action']} {a['branch']}" for a in payload)
            self.statusBar().showMessage(f"Email approval applied: {applied}", 10000)


class DashboardTab(QWidget):
    def __init__(self, memory: SQLiteMemory) -> None:
        super().__init__()
        self.memory = memory

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)
        header = QLabel("Investment Briefing")
        header.setObjectName("sectionHeader")

        self.briefing = QTextEdit()
        self.briefing.setObjectName("briefingPanel")
        self.briefing.setReadOnly(True)
        self.briefing.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)

        layout.addWidget(header)
        layout.addWidget(self.briefing, 1)
        layout.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignRight)

    def refresh(self) -> None:
        briefing = build_briefing(self.memory.get_investment_profile())
        self.briefing.setPlainText(render_briefing(briefing))


# --- Audio devices + hands-free wake-word ("HELIX") voice detection (§23) --------------------- #

XPERT_INPUT_DEVICE_SETTING = "xpert_input_device"    # preferred mic, by description
XPERT_OUTPUT_DEVICE_SETTING = "xpert_output_device"  # preferred speaker, by description
XPERT_VOICE_SPEED_SETTING = "xpert_voice_speed"      # HELIX's talking rate (×), default 1.5

# Energy-based voice-activity detection (VAD). The speech threshold is ADAPTIVE — it tracks the
# ambient noise floor, so it works across mics (a quiet close-talk headset vs. a noisier array mic)
# instead of a single fixed level that mis-fires on one and goes deaf on the other.
WAKE_RMS_FLOOR = 260.0       # absolute minimum speech threshold (int16 RMS)
WAKE_SPEECH_FACTOR = 3.2     # speech must be this many× the running ambient noise floor
WAKE_NOISE_INIT = 200.0      # starting noise-floor estimate
WAKE_END_SILENCE_S = 0.7     # this much trailing quiet ends an utterance
WAKE_MIN_SPEECH_S = 0.3      # ignore shorter blips (clicks, coughs)
WAKE_MAX_UTTER_S = 12.0      # hard cap per utterance
WAKE_PREROLL_S = 0.25        # keep this much pre-speech audio so the wake word isn't clipped
WAKE_FOLLOWUP_MS = 9000      # after a reply, accept a follow-up with NO wake word for this long
# Accept the obvious mis-hearings of "HELIX" so a clear command still lands.
_WAKE_RE = re.compile(r"\b(?:hey\s+|ok\s+|okay\s+)?(?:he+lix|helics|healix|helex|heelux)\b[\s,.:;!?-]*", re.IGNORECASE)


def _pcm_rms(pcm: bytes) -> float:
    """RMS level of 16-bit little-endian mono PCM (stdlib only — no numpy, no deprecated audioop)."""
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def _split_wake(text: str) -> tuple:
    """(matched, command): if 'HELIX' is in `text`, return True + the words after it, else (False, '')."""
    match = _WAKE_RE.search(text or "")
    if not match:
        return False, ""
    return True, (text[match.end():] or "").strip()


def _write_wav16(data: bytes, path: str) -> None:
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)  # Int16
        handle.setframerate(16000)
        handle.writeframes(data)


def _find_audio_device(devices: list, description: str):
    """Return the QAudioDevice whose description matches `description`, else None."""
    for device in devices or []:
        try:
            if device.description() == description:
                return device
        except Exception:
            continue
    return None


def _mono16k_format() -> QAudioFormat:
    fmt = QAudioFormat()
    fmt.setSampleRate(16000)
    fmt.setChannelCount(1)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return fmt


class VadSegmenter:
    """Turns a stream of PCM chunks into complete spoken utterances using energy + trailing silence.
    Pure (no Qt) so the segmentation is unit-testable; WakeWordListener feeds it live mic chunks."""

    def __init__(self, sample_rate: int = 16000) -> None:
        bytes_per_s = sample_rate * 2  # 16-bit mono
        self._end_silence = int(WAKE_END_SILENCE_S * bytes_per_s)
        self._min_speech = int(WAKE_MIN_SPEECH_S * bytes_per_s)
        self._max_bytes = int(WAKE_MAX_UTTER_S * bytes_per_s)
        self._preroll_cap = int(WAKE_PREROLL_S * bytes_per_s)
        self._noise = WAKE_NOISE_INIT  # adapts to ambient; persists across utterances
        self.reset()

    def reset(self) -> None:
        self._in_speech = False
        self._utter = bytearray()
        self._silence = 0
        self._preroll = bytearray()

    @property
    def threshold(self) -> float:
        return max(WAKE_RMS_FLOOR, self._noise * WAKE_SPEECH_FACTOR)

    def push(self, chunk: bytes):
        """Feed a chunk; return a completed utterance (bytes) when one ends, else None."""
        if not chunk:
            return None
        rms = _pcm_rms(chunk)
        loud = rms >= self.threshold
        if loud:
            if not self._in_speech:
                self._in_speech = True
                self._utter = bytearray(self._preroll)  # seed with pre-roll so the wake word survives
                self._preroll = bytearray()
            self._utter += chunk
            self._silence = 0
        elif self._in_speech:
            self._utter += chunk
            self._silence += len(chunk)
            if self._silence >= self._end_silence:
                return self._finish()
        else:
            self._noise = 0.95 * self._noise + 0.05 * rms  # track the ambient noise floor
            self._preroll += chunk
            if len(self._preroll) > self._preroll_cap:
                del self._preroll[: len(self._preroll) - self._preroll_cap]
        if self._in_speech and len(self._utter) >= self._max_bytes:
            return self._finish()
        return None

    def _finish(self):
        utter = bytes(self._utter)
        spoken = len(utter) - self._silence  # rough speech length, minus trailing quiet
        self.reset()
        return utter if spoken >= self._min_speech else None


class MicRecorder(QObject):
    """Push-to-talk mic capture via QtMultimedia's QAudioSource, written out as a 16 kHz mono WAV
    that faster-whisper transcribes. Optional/guarded: if the multimedia backend or an input device
    is missing, is_available() is False and the Xpert tab disables the Talk button gracefully
    (mirroring how edge-tts / faster-whisper degrade)."""

    def __init__(self, device=None, parent=None) -> None:
        super().__init__(parent)
        self._source = None
        self._io = None
        self._buffer = bytearray()
        self._device = None
        self._format = None
        self._available = False
        try:
            if device is None or device.isNull():
                device = QMediaDevices.defaultAudioInput()
            if device is None or device.isNull():
                return
            self._device = device
            self._format = _mono16k_format()
            self._available = True
        except Exception:
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def start(self) -> bool:
        if not self._available:
            return False
        try:
            self._buffer = bytearray()
            self._source = QAudioSource(self._device, self._format, self)
            self._io = self._source.start()
            if self._io is None:
                self._source = None
                return False
            self._io.readyRead.connect(self._on_ready)
            return True
        except Exception:
            self._source = None
            self._io = None
            return False

    def _on_ready(self) -> None:
        if self._io is not None:
            self._buffer += bytes(self._io.readAll())

    def stop(self) -> bytes:
        if self._io is not None:
            try:
                self._buffer += bytes(self._io.readAll())
            except Exception:
                pass
        if self._source is not None:
            try:
                self._source.stop()
            except Exception:
                pass
        data = bytes(self._buffer)
        self._buffer = bytearray()
        self._source = None
        self._io = None
        return data

    def save_wav(self, data: bytes, path: str) -> None:
        _write_wav16(data, path)


class WakeWordListener(QObject):
    """Always-on, hands-free mic capture for the 'HELIX' wake word. Continuously reads the mic,
    segments speech with VadSegmenter, and emits each finished utterance for transcription.
    Processing is gated by set_active() so it goes quiet while HELIX is transcribing / thinking /
    speaking (it never transcribes its own reply). Optional/guarded like MicRecorder."""

    utterance = pyqtSignal(bytes)
    level = pyqtSignal(float)  # 0..1 mic level, for a live meter

    def __init__(self, device=None, parent=None) -> None:
        super().__init__(parent)
        self._source = None
        self._io = None
        self._device = None
        self._format = None
        self._available = False
        self._active = False
        self._seg = VadSegmenter()
        try:
            if device is None or device.isNull():
                device = QMediaDevices.defaultAudioInput()
            if device is None or device.isNull():
                return
            self._device = device
            self._format = _mono16k_format()
            self._available = True
        except Exception:
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def start(self) -> bool:
        if not self._available:
            return False
        try:
            self._seg.reset()
            self._source = QAudioSource(self._device, self._format, self)
            self._io = self._source.start()
            if self._io is None:
                self._source = None
                return False
            self._io.readyRead.connect(self._on_ready)
            self._active = True
            return True
        except Exception:
            self._source = None
            self._io = None
            return False

    def set_active(self, on: bool) -> None:
        """Gate processing without tearing down the stream: while inactive, mic chunks are drained
        and discarded (VAD reset), so HELIX never hears / transcribes its own spoken replies."""
        if on and not self._active:
            self._seg.reset()
        self._active = bool(on)

    def stop(self) -> None:
        if self._source is not None:
            try:
                self._source.stop()
            except Exception:
                pass
        self._source = None
        self._io = None
        self._active = False
        self._seg.reset()

    def _on_ready(self) -> None:
        if self._io is None:
            return
        chunk = bytes(self._io.readAll())  # always drain so the device buffer can't back up
        if not chunk or not self._active:
            return
        self.level.emit(min(1.0, _pcm_rms(chunk) / 8000.0))
        utter = self._seg.push(chunk)
        if utter:
            self.utterance.emit(utter)


class XpertTab(QWidget):
    """The HELIX 'brain' - a two-way J.A.R.V.I.S.-style voice assistant that can act on every
    pillar, plus the one-way expert overview across all five pillars (H E L I X)."""

    # Worker -> main-thread signals (Qt queues these across threads, so tool side effects that
    # touch widgets are marshalled safely back to the UI thread).
    request_invest = pyqtSignal(str)        # "start" / "stop" auto-investing on the Investment tab
    request_home_refresh = pyqtSignal()     # reload the Home checklist after a task change
    convo_step = pyqtSignal(str)            # live "what HELIX is doing now" status during a turn

    def __init__(self, memory: SQLiteMemory) -> None:
        super().__init__()
        self.memory = memory
        self.settings = AppSettings()
        self._workers = set()
        self._state = None
        # Conversation state for the voice assistant (§23).
        self._history = []            # full Messages-API history; persists across turns
        self._pending_action = None   # a money/outward action awaiting an explicit spoken "yes"
        self._convo_state = "idle"
        self._convo_context = ""      # HELIX live context, snapshotted at the start of each turn
        self._speak_done_cb = None    # called when the current spoken reply finishes
        self._pending_speech = ""
        self._invest_tab = None       # bound by HelixMainWindow (start/stop auto-investing)
        self._home_tab = None
        self._inv_live = False
        self._inv_running = False
        self._inv_keys = False
        # Hands-free wake-word ("HELIX") state. Off at launch — an always-on mic is opt-in each session.
        self._handsfree = False
        self._wake_listener = None
        self._followup = False        # within a window, the next utterance needs no wake word
        self._loading_devices = False
        self._followup_timer = QTimer(self)
        self._followup_timer.setSingleShot(True)
        self._followup_timer.timeout.connect(lambda: setattr(self, "_followup", False))
        self._setup_tts()
        self._setup_mic()
        self._router = self._build_router()
        self.request_invest.connect(self._do_invest_action)
        self.request_home_refresh.connect(self._do_home_refresh)
        self.convo_step.connect(self._on_convo_step)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer_layout.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        header = QLabel("Xpert — Talk to HELIX")
        header.setObjectName("sectionHeader")
        subtitle = QLabel(
            "Speak or type to HELIX — it answers and gets things done. Hold the button to talk, or "
            "turn on Hands-free and just say HELIX."
        )
        subtitle.setWordWrap(True)

        # --- Two-way voice conversation: the J.A.R.V.I.S. assistant (§23) ---
        self.convo_box = QGroupBox("Conversation")
        convo_layout = QVBoxLayout(self.convo_box)
        convo_layout.setSpacing(10)
        self.transcript = QTextEdit()
        self.transcript.setObjectName("briefingPanel")
        self.transcript.setReadOnly(True)
        self.transcript.setMinimumHeight(320)
        self.convo_progress = QProgressBar()
        self.convo_progress.setRange(0, 0)
        self.convo_progress.setTextVisible(False)
        self.convo_progress.setMaximumHeight(6)
        self.convo_progress.setVisible(False)
        self.convo_status = QLabel("")
        self.convo_status.setWordWrap(True)

        self.talk_button = QPushButton("\U0001f3a4  Hold to Talk")
        self.talk_button.setMinimumHeight(48)
        self.talk_button.pressed.connect(self._on_talk_pressed)
        self.talk_button.released.connect(self._on_talk_released)
        new_chat_button = QPushButton("New chat")
        new_chat_button.clicked.connect(self._new_chat)
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("...or type to HELIX and press Enter")
        self.text_input.returnPressed.connect(self._on_send)
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._on_send)

        talk_row = QHBoxLayout()
        talk_row.addWidget(self.talk_button, 1)
        talk_row.addWidget(new_chat_button)

        # Hands-free wake word ("HELIX") + a live mic-level meter.
        self.handsfree_check = QCheckBox("Hands-free — just say “HELIX”")
        self.handsfree_check.setObjectName("handsfreeToggle")
        self.handsfree_check.setToolTip(
            "Always listen and act when you say HELIX — no button. Pauses itself while HELIX talks."
        )
        self.handsfree_check.toggled.connect(self._toggle_handsfree)
        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(False)
        self.level_bar.setMaximumHeight(8)
        self.level_bar.setVisible(False)
        hf_row = QHBoxLayout()
        hf_row.addWidget(self.handsfree_check)
        hf_row.addWidget(self.level_bar, 1)

        # Voice output speed — how fast HELIX talks (0.8×–2.0×).
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setMinimum(80)
        self.speed_slider.setMaximum(200)
        self.speed_slider.setSingleStep(5)
        self.speed_slider.setPageStep(10)
        try:
            saved_speed = float(self.settings.get(XPERT_VOICE_SPEED_SETTING, 1.5))
        except (TypeError, ValueError):
            saved_speed = 1.5
        self.speed_slider.setValue(int(max(0.8, min(2.0, saved_speed)) * 100))
        self.speed_value = QLabel()
        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Voice speed"))
        speed_row.addWidget(self.speed_slider, 1)
        speed_row.addWidget(self.speed_value)

        # Mic / speaker pickers (default to the system default, e.g. your Bluetooth headset).
        self.mic_picker = NoScrollComboBox()
        self.speaker_picker = NoScrollComboBox()
        self._load_device_pickers()
        self.mic_picker.currentIndexChanged.connect(self._on_input_device_changed)
        self.speaker_picker.currentIndexChanged.connect(self._on_output_device_changed)
        dev_row = QHBoxLayout()
        dev_row.addWidget(QLabel("Mic"))
        dev_row.addWidget(self.mic_picker, 1)
        dev_row.addWidget(QLabel("Speaker"))
        dev_row.addWidget(self.speaker_picker, 1)

        type_row = QHBoxLayout()
        type_row.addWidget(self.text_input, 1)
        type_row.addWidget(self.send_button)
        convo_layout.addWidget(self.transcript, 1)
        convo_layout.addWidget(self.convo_progress)
        convo_layout.addWidget(self.convo_status)
        convo_layout.addLayout(talk_row)
        convo_layout.addLayout(hf_row)
        convo_layout.addLayout(speed_row)
        convo_layout.addLayout(dev_row)
        convo_layout.addLayout(type_row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#6fb3c0;")

        layout.addWidget(header)
        layout.addWidget(subtitle)
        layout.addWidget(self.convo_box, 1)
        layout.addWidget(self.status)

        self._update_speed_label()
        self.refresh()
        self._new_chat()

    def refresh(self) -> None:
        spawn_worker(self._workers, self._gather, self._gather_done)

    def _gather(self) -> dict:
        usage = self.memory.ai_usage_summary()
        rationale = self.memory.list_stock_rationale()
        recent_sells = self.memory.list_sells(limit=8)
        digest = self.memory.investment_digest()
        performance = self.memory.strategy_performance()
        action_counts = {"buy": 0, "watch": 0, "skip": 0}
        for record in rationale:
            action = record.get("action", "")
            if action in action_counts:
                action_counts[action] += 1
        data = {
            "calls": usage["calls"],
            "month_cost": usage["month_cost"],
            "rated": len(rationale),
            "action_counts": action_counts,
            "recent_sells": [(s["symbol"], s["reason"]) for s in recent_sells],
            "trades": digest["trades"],
            "sells_total": digest["sells"],
            "since": digest["since"],
            "performance": performance,
            "alpaca_ok": False,
        }
        try:
            client = AlpacaClient.from_settings(self.settings)
            snapshot = portfolio_snapshot(client.get_account(), client.get_positions())
            data.update(
                alpaca_ok=True,
                equity=snapshot.equity,
                cash=snapshot.cash,
                gains=snapshot.unrealized_pl,
                n_positions=len(snapshot.positions),
                positions=[(p.symbol, p.market_value) for p in snapshot.positions[:8]],
            )
        except AlpacaError:
            pass
        return data

    def _gather_done(self, ok: bool, payload) -> None:
        if not ok:
            self.status.setText("Could not load system state — you can still talk to HELIX.")
            return
        self._state = payload  # feeds the conversation's live context (see _context)
        if payload.get("alpaca_ok"):
            sign = "+" if payload["gains"] >= 0 else ""
            self.status.setText(
                f"Balance ${payload['equity']:,.2f}  ·  {payload['n_positions']} positions  ·  "
                f"Gains {sign}${payload['gains']:,.2f}   —   ask me anything."
            )
        else:
            self.status.setText("Save your Alpaca keys in the Investment tab to load your balance.")

    def _context(self) -> str:
        state = self._state or {}
        try:
            due = due_tasks(self.settings.get(HOME_TASKS_SETTING) or [])
        except Exception:
            due = []
        if due:
            due_txt = ", ".join(
                f"{entry['action']} {entry['item']}".strip()
                + ("/overdue" if entry["status"] == "Overdue" else "")
                for entry in due
            )
            home_line = f"Home: {len(due)} task(s) due now ({due_txt})."
        else:
            home_line = "Home: household tasks all caught up."
        lines = [
            home_line,
            "Enterprise: planned (later).",
            f"Learning: {state.get('calls', 0)} Claude calls, ~${state.get('month_cost', 0):.4f} this month.",
        ]
        if state.get("alpaca_ok"):
            sign = "+" if state.get("gains", 0) >= 0 else ""
            lines.append(
                f"Investment: balance ${state.get('equity', 0):,.2f}, cash ${state.get('cash', 0):,.2f}, "
                f"{state.get('n_positions', 0)} positions, open gains {sign}${state.get('gains', 0):,.2f}."
            )
            if state.get("positions"):
                holdings = ", ".join(f"{sym} ${val:,.0f}" for sym, val in state["positions"])
                lines.append(f"Holdings: {holdings}.")
        else:
            lines.append("Investment: Alpaca not connected / no balance loaded.")
        counts = state.get("action_counts", {})
        lines.append(
            f"Stored pick logic: {counts.get('buy', 0)} buy-rated, {counts.get('watch', 0)} watch, "
            f"{counts.get('skip', 0)} skip across {state.get('rated', 0)} stocks."
        )
        if state.get("recent_sells"):
            sells_txt = "; ".join(f"{sym} ({reason})" for sym, reason in state["recent_sells"])
            lines.append(f"Recent sells: {sells_txt}.")
        if state.get("since"):
            lines.append(
                f"Track record on file: {state.get('trades', 0)} trades and "
                f"{state.get('sells_total', 0)} sells since {state['since']} (rolling 1-year window)."
            )
        perf = state.get("performance") or {}
        if perf.get("closed", 0) > 0:
            lines.append(
                f"Realized results: hit rate {perf['hit_rate']}% over {perf['closed']} closed positions, "
                f"avg return {perf['avg_return_pct']:+.1f}%, realized P/L ${perf['realized_pl']:+,.2f}."
            )
        return "\n".join(lines)

    @staticmethod
    def _plain(text: str) -> str:
        for ch in ("#", "*", "`"):
            text = text.replace(ch, "")
        return text.strip()

    def _setup_tts(self) -> None:
        self._speaking = False
        # Preferred: a natural neural voice (edge-tts) played through a Qt media player.
        try:
            self.player = QMediaPlayer(self)
            self.audio_out = QAudioOutput(self)
            self.player.setAudioOutput(self.audio_out)
            self.audio_out.setVolume(1.0)
            out_device = self._saved_output_device()
            if out_device is not None:
                self.audio_out.setDevice(out_device)  # route HELIX's voice to the chosen speaker/headset
            self.player.mediaStatusChanged.connect(self._on_media_status)
        except Exception:
            self.player = None
        # Fallback: the built-in OS voice (robotic, but offline).
        try:
            self.tts = QTextToSpeech(self)
            for locale in self.tts.availableLocales():
                if locale.name() == "en_GB":
                    self.tts.setLocale(locale)
                    break
            for voice in self.tts.availableVoices():
                try:
                    if voice.gender() == QVoice.Gender.Male:
                        self.tts.setVoice(voice)
                        break
                except Exception:
                    break
            self.tts.setRate(self._tts_rate())  # match the chosen voice speed
            self.tts.setPitch(-0.1)
            self.tts.setVolume(1.0)
            self.tts.stateChanged.connect(self._on_tts_state)
        except Exception:
            self.tts = None

    # ---- voice output speed ----------------------------------------------- #

    def _voice_speed(self) -> float:
        """Current voice speed multiplier from the slider (falls back to the saved setting)."""
        if getattr(self, "speed_slider", None) is not None:
            return self.speed_slider.value() / 100.0
        try:
            return float(self.settings.get(XPERT_VOICE_SPEED_SETTING, 1.5))
        except (TypeError, ValueError):
            return 1.5

    def _voice_rate_str(self) -> str:
        """edge-tts rate string from the speed, e.g. 1.5× -> '+50%', 0.8× -> '-20%'."""
        pct = round((self._voice_speed() - 1.0) * 100)
        return f"+{pct}%" if pct >= 0 else f"{pct}%"

    def _tts_rate(self) -> float:
        """QTextToSpeech rate (-1..1) for the offline fallback voice, from the speed multiplier."""
        return max(-1.0, min(1.0, self._voice_speed() - 1.0))

    def _update_speed_label(self) -> None:
        if getattr(self, "speed_value", None) is not None:
            self.speed_value.setText(f"{self._voice_speed():.2f}×")

    def _on_speed_changed(self, _value: int) -> None:
        self._update_speed_label()
        self.settings.set(XPERT_VOICE_SPEED_SETTING, self._voice_speed())
        if getattr(self, "tts", None) is not None:
            try:
                self.tts.setRate(self._tts_rate())
            except Exception:
                pass

    def _setup_mic(self) -> None:
        self.mic = MicRecorder(self._saved_input_device(), self)

    def _saved_input_device(self):
        desc = self.settings.get(XPERT_INPUT_DEVICE_SETTING, "")
        return _find_audio_device(QMediaDevices.audioInputs(), desc) if desc else None

    def _saved_output_device(self):
        desc = self.settings.get(XPERT_OUTPUT_DEVICE_SETTING, "")
        return _find_audio_device(QMediaDevices.audioOutputs(), desc) if desc else None

    # ---- action wiring (the "Act" layer) ---------------------------------- #

    def _build_router(self) -> ActionRouter:
        ctx = ActionContext(
            memory=self.memory,
            settings=self.settings,
            research_fn=self._research_fn,
            is_live=lambda: self._inv_live,
            auto_running=lambda: self._inv_running,
            keys_ready=lambda: self._inv_keys,
            start_auto=lambda: self.request_invest.emit("start"),
            stop_auto=lambda: self.request_invest.emit("stop"),
            refresh_home=lambda: self.request_home_refresh.emit(),
        )
        return ActionRouter(ctx)

    def bind_investment(self, invest_tab) -> None:
        self._invest_tab = invest_tab

    def bind_home(self, home_tab) -> None:
        self._home_tab = home_tab

    def _do_invest_action(self, which: str) -> None:
        """Main-thread handler for start/stop auto-investing (emitted from a worker, queued here)."""
        if self._invest_tab is None:
            return
        if which == "start":
            self._invest_tab.voice_start()
            self._inv_running = bool(getattr(self._invest_tab, "_running", False))
        elif which == "stop":
            self._invest_tab.voice_stop()
            self._inv_running = False

    def _do_home_refresh(self) -> None:
        if self._home_tab is not None:
            self._home_tab.refresh()

    def _on_convo_step(self, tool_name: str) -> None:
        friendly = {
            "get_portfolio": "Checking your portfolio...",
            "get_recent_sells": "Looking up recent sells...",
            "get_track_record": "Reviewing the track record...",
            "set_auto_investing": "Adjusting auto-investing...",
            "get_home_tasks": "Checking your home tasks...",
            "complete_home_task": "Updating your task list...",
            "add_home_task": "Adding the task...",
            "text_my_tasks": "Preparing a text...",
            "review_helix_100": "Reviewing the HELIX 500...",
            "scout_special_stocks": "Scouting moonshot stocks...",
        }.get(tool_name, "Working on it...")
        self._set_convo_state("acting", friendly)

    def _research_fn(self, prompt: str) -> str:
        """A Claude call for the roster/special tools, recording usage (mirrors InvestTab)."""
        model = DEFAULT_RESEARCH_MODEL
        client = ClaudeClient(ClaudeConfig(model=model, timeout_seconds=RESEARCH_TIMEOUT_SECONDS))
        text = client.complete(prompt, max_tokens=research_max_tokens(self.settings))  # Settings -> Research effort
        usage = client.last_usage or {}
        in_tok = int(usage.get("input_tokens", 0) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)
        self.memory.record_ai_usage(model, in_tok, out_tok, estimate_cost(model, in_tok, out_tok))
        return text

    # ---- hands-free wake word ("HELIX") + audio devices ------------------- #

    def _load_device_pickers(self) -> None:
        self._loading_devices = True
        for picker, devices, key in (
            (self.mic_picker, QMediaDevices.audioInputs(), XPERT_INPUT_DEVICE_SETTING),
            (self.speaker_picker, QMediaDevices.audioOutputs(), XPERT_OUTPUT_DEVICE_SETTING),
        ):
            picker.clear()
            picker.addItem("System default", "")
            for device in devices:
                try:
                    picker.addItem(device.description(), device.description())
                except Exception:
                    continue
            index = picker.findData(self.settings.get(key, ""))
            picker.setCurrentIndex(index if index >= 0 else 0)
        self._loading_devices = False

    def _on_input_device_changed(self, _index: int) -> None:
        if self._loading_devices:
            return
        self.settings.set(XPERT_INPUT_DEVICE_SETTING, self.mic_picker.currentData() or "")
        self._setup_mic()  # rebuild the push-to-talk recorder on the new mic
        if self._handsfree:  # restart the wake listener on the new mic
            self._start_wake()
            if self._wake_listener is not None:
                self._wake_listener.set_active(self._convo_state == "idle")

    def _on_output_device_changed(self, _index: int) -> None:
        if self._loading_devices:
            return
        self.settings.set(XPERT_OUTPUT_DEVICE_SETTING, self.speaker_picker.currentData() or "")
        device = self._saved_output_device() or QMediaDevices.defaultAudioOutput()
        if getattr(self, "audio_out", None) is not None:
            try:
                self.audio_out.setDevice(device)
            except Exception:
                pass

    def _toggle_handsfree(self, on: bool) -> None:
        if on:
            if not self.mic.is_available():
                self._append_transcript("HELIX", "No microphone is available, sir.")
                self.handsfree_check.setChecked(False)
                return
            if not stt_available():
                self._append_transcript(
                    "HELIX", "Hands-free needs faster-whisper, sir: pip install faster-whisper."
                )
                self.handsfree_check.setChecked(False)
                return
            if not stt_ready():  # model didn't pre-load before Qt — loading it now would crash (§23)
                self._append_transcript(
                    "HELIX", "The voice model didn't load at startup, sir — restart HELIX to enable hands-free."
                )
                self.handsfree_check.setChecked(False)
                return
            if not self._claude_ready():
                self._append_transcript("HELIX", "Save a Claude key first, sir - Learning, Claude.")
                self.handsfree_check.setChecked(False)
                return
            if not self._start_wake():
                self._append_transcript("HELIX", "I couldn't open the microphone for hands-free, sir.")
                self.handsfree_check.setChecked(False)
                return
            self._handsfree = True
            self._followup = False
            self.level_bar.setVisible(True)
            self._append_transcript("HELIX", "Hands-free on, sir. Just say HELIX, then your request.")
        else:
            self._handsfree = False
            self._stop_wake()
            self.level_bar.setVisible(False)
        self._set_convo_state("idle")

    def _start_wake(self) -> bool:
        self._stop_wake()
        self._wake_listener = WakeWordListener(self._saved_input_device(), self)
        if not self._wake_listener.is_available():
            self._wake_listener = None
            return False
        self._wake_listener.utterance.connect(self._on_wake_utterance)
        self._wake_listener.level.connect(self._on_wake_level)
        return self._wake_listener.start()

    def _stop_wake(self) -> None:
        if self._wake_listener is not None:
            try:
                self._wake_listener.stop()
            except Exception:
                pass
            self._wake_listener = None
        if hasattr(self, "level_bar"):
            self.level_bar.setValue(0)

    def _arm_followup(self) -> None:
        self._followup = True
        self._followup_timer.start(WAKE_FOLLOWUP_MS)

    def _on_wake_level(self, level: float) -> None:
        self.level_bar.setValue(int(level * 100))

    def _on_wake_utterance(self, pcm: bytes) -> None:
        if self._convo_state != "idle":  # already mid-turn; ignore
            return
        if self._wake_listener is not None:
            self._wake_listener.set_active(False)  # quiet while we transcribe / think / speak
        handle, path = tempfile.mkstemp(suffix=".wav", prefix="helix_wake_")
        os.close(handle)
        try:
            _write_wav16(pcm, path)
        except Exception:
            self._set_convo_state("idle")  # re-arms the listener via the gating in _set_convo_state
            return
        self._set_convo_state("transcribing")
        spawn_worker(
            self._workers, lambda: transcribe(path), lambda ok, p: self._wake_transcribed(ok, p, path)
        )

    def _wake_transcribed(self, ok: bool, payload, path: str = "") -> None:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        text = str(payload or "").strip() if ok else ""
        matched, after = _split_wake(text)
        in_followup = self._followup
        self._followup = False
        if matched:
            command = after.strip()
        elif in_followup and text:
            command = text  # within the follow-up window, the wake word isn't required
        else:
            self._set_convo_state("idle")  # not addressed to HELIX - keep listening
            return
        if not command:
            # bare "HELIX" - acknowledge, then take the next utterance as the command
            self._append_transcript("You", "HELIX")
            self._speak_reply("Yes, sir?")
            return
        self._append_transcript("You", command)
        self._handle_user_text(command)

    # ---- conversation UI helpers ------------------------------------------ #

    def _new_chat(self) -> None:
        self._history = []
        self._pending_action = None
        self.transcript.clear()
        self._append_transcript(
            "HELIX", "Standing by, sir. Hold the Talk button and speak, or type below."
        )
        self._set_convo_state("idle")

    def _append_transcript(self, who: str, text: str) -> None:
        color = "#ffc857" if who == "HELIX" else "#1dd8ff"
        safe = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.transcript.append(f'<span style="color:{color};font-weight:700;">{who}:</span> {safe}')
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def announce(self, text: str) -> None:
        """Proactively say something (a door alert, a low-stock nudge) — but ONLY when idle, so HELIX
        never talks over a turn. Used by the Console's awareness loop."""
        if not text or self._convo_state != "idle":
            return
        self._append_transcript("HELIX", text)
        self._speak_reply(text)

    def _set_convo_state(self, state: str, detail: str = "") -> None:
        self._convo_state = state
        self.convo_progress.setVisible(state in ("transcribing", "thinking", "acting", "speaking"))
        labels = {
            "idle": "Ready.",
            "listening": "● Listening... release to send.",
            "transcribing": "Transcribing...",
            "thinking": "Thinking...",
            "acting": detail or "Working on it...",
            "speaking": "Speaking, sir.",
        }
        if state == "listening":
            base = "● Listening... release to send."
        elif self._handsfree:
            base = "Hands-free on — say “HELIX”." if state == "idle" else labels.get(state, "")
        else:
            base = labels.get(state, "")
        self.convo_status.setText(detail if (detail and state == "acting") else base)
        mic_ok = self.mic.is_available()
        if self._handsfree:
            self.talk_button.setText("\U0001f3a4  Hands-free on — say “HELIX”")
        elif state == "listening":
            self.talk_button.setText("● Listening... (release)")
        elif not mic_ok:
            self.talk_button.setText("\U0001f3a4  Mic unavailable - type below")
            self.talk_button.setToolTip("No microphone detected. Type to HELIX instead.")
        else:
            self.talk_button.setText("\U0001f3a4  Hold to Talk")
        # While hands-free is on, the listener owns the mic, so the manual press-to-talk is disabled.
        self.talk_button.setEnabled(mic_ok and not self._handsfree and state in ("idle", "listening"))
        self.text_input.setEnabled(state == "idle")
        self.send_button.setEnabled(state == "idle")
        # Gate the wake listener: it only processes audio while idle (silent during a turn / reply).
        if self._wake_listener is not None:
            self._wake_listener.set_active(self._handsfree and state == "idle")

    @staticmethod
    def _claude_ready() -> bool:
        return ClaudeClient().is_configured()

    def _begin_turn(self) -> None:
        """Snapshot live investment state on the main thread before the worker runs (the worker
        must not touch widgets), and capture the HELIX context for the system prompt."""
        if self._invest_tab is not None:
            try:
                self._inv_live = self._invest_tab.is_real()
                self._inv_running = bool(getattr(self._invest_tab, "_running", False))
            except Exception:
                pass
        self._inv_keys = bool(
            self.settings.get(ALPACA_API_KEY_SETTING) and self.settings.get(ALPACA_SECRET_KEY_SETTING)
        )
        self._convo_context = self._context()

    # ---- push-to-talk ----------------------------------------------------- #

    def _on_talk_pressed(self) -> None:
        if self._convo_state != "idle":
            return
        if not self.mic.is_available():
            self._append_transcript("HELIX", "No microphone is available, sir - type to me instead.")
            return
        if not stt_available():
            self._append_transcript(
                "HELIX",
                "Voice needs faster-whisper, sir: pip install faster-whisper. You can type meanwhile.",
            )
            return
        if not stt_ready():  # model didn't pre-load before Qt — loading it now would crash (§23)
            self._append_transcript(
                "HELIX", "The voice model didn't load at startup, sir — restart HELIX, then talk. Type meanwhile."
            )
            return
        if not self.mic.start():
            self._append_transcript("HELIX", "I couldn't open the microphone, sir.")
            return
        self._set_convo_state("listening")

    def _on_talk_released(self) -> None:
        if self._convo_state != "listening":
            return
        data = self.mic.stop()
        if len(data) < 9600:  # < ~0.3s of 16 kHz 16-bit mono = a slip, not speech
            self._set_convo_state("idle", "Didn't catch that - hold a touch longer.")
            return
        handle, path = tempfile.mkstemp(suffix=".wav", prefix="helix_stt_")
        os.close(handle)
        try:
            self.mic.save_wav(data, path)
        except Exception:
            self._set_convo_state("idle", "Couldn't save the audio.")
            return
        self._set_convo_state("transcribing")
        spawn_worker(
            self._workers,
            lambda: transcribe(path),
            lambda ok, payload: self._transcribed(ok, payload, path),
        )

    def _transcribed(self, ok: bool, payload, path: str = "") -> None:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        if not ok:
            self._append_transcript("HELIX", f"Speech-to-text didn't work: {payload}")
            self._set_convo_state("idle")
            return
        text = str(payload or "").strip()
        if not text:
            self._set_convo_state("idle", "Didn't catch that, sir.")
            return
        self._append_transcript("You", text)
        self._handle_user_text(text)

    def _on_send(self) -> None:
        if self._convo_state != "idle":
            return
        text = self.text_input.text().strip()
        if not text:
            return
        self.text_input.clear()
        self._append_transcript("You", text)
        self._handle_user_text(text)

    # ---- the turn (Think + Act + Speak) ----------------------------------- #

    def _handle_user_text(self, text: str) -> None:
        # Deterministic spoken-confirmation gate for any pending money/outward action: it fires
        # ONLY on the user's own affirmative words, never on the model's say-so.
        if self._pending_action is not None:
            pending = self._pending_action
            if is_affirmative(text):
                self._pending_action = None
                self._history.append({"role": "user", "content": text})
                self._set_convo_state("acting", "Confirming...")
                spawn_worker(
                    self._workers,
                    lambda: self._router.execute_confirmed(*pending),
                    self._confirmed_done,
                )
                return
            if is_negative(text):
                self._pending_action = None
                reply = "Cancelled, sir."
                self._history.append({"role": "user", "content": text})
                self._history.append({"role": "assistant", "content": reply})
                self._append_transcript("HELIX", reply)
                self._speak_reply(reply)
                return
            # Ambiguous: abandon the gated action (no implicit yes) and treat as a fresh request.
            self._pending_action = None

        if not self._claude_ready():
            self._append_transcript(
                "HELIX", "I need a Claude API key to talk, sir - save one in Learning, Claude."
            )
            self._set_convo_state("idle")
            return
        self._begin_turn()
        self._history.append({"role": "user", "content": text})
        self._history = self._trim_history(self._history)
        snapshot = list(self._history)
        self._set_convo_state("thinking")
        spawn_worker(self._workers, lambda: self._think(snapshot), self._thought)

    @staticmethod
    def _trim_history(messages: list, limit: int = 24) -> list:
        """Keep the conversation tail bounded for cost/latency, but never start the window on a
        dangling tool_result / assistant turn (the Messages API would reject that)."""
        msgs = messages[-limit:]
        while msgs and not (msgs[0].get("role") == "user" and isinstance(msgs[0].get("content"), str)):
            msgs.pop(0)
        return msgs

    def _think(self, messages: list):
        model = DEFAULT_RESEARCH_MODEL
        client = ClaudeClient(ClaudeConfig(model=model))
        system = build_jarvis_chat_system(self._convo_context)
        result = run_chat_turn(
            client, model, system, messages, self._router,
            on_step=lambda name: self.convo_step.emit(name),
        )
        for usage in result.usages:
            in_tok = int(usage.get("input_tokens", 0) or 0)
            out_tok = int(usage.get("output_tokens", 0) or 0)
            if in_tok or out_tok:
                self.memory.record_ai_usage(model, in_tok, out_tok, estimate_cost(model, in_tok, out_tok))
        return result

    def _thought(self, ok: bool, payload) -> None:
        if not ok:
            reply = "I couldn't reach Claude just now, sir."
            self._append_transcript("HELIX", f"{reply} ({payload})")
            self._speak_reply(reply)
            return
        result = payload
        self._history = result.messages
        self._pending_action = result.pending
        self._append_transcript("HELIX", result.reply)
        self._speak_reply(result.reply)

    def _confirmed_done(self, ok: bool, payload) -> None:
        reply = str(payload) if ok else f"That didn't go through, sir: {payload}"
        self._history.append({"role": "assistant", "content": reply})
        self._append_transcript("HELIX", reply)
        self._speak_reply(reply)

    # ---- speaking --------------------------------------------------------- #

    def _speak_reply(self, text: str) -> None:
        self._set_convo_state("speaking")

        def done() -> None:
            self._set_convo_state("idle")
            if self._handsfree:
                self._arm_followup()  # accept a follow-up with no wake word for a beat
                if self._wake_listener is not None:
                    # Brief guard so HELIX's own voice tail / room echo can't re-trigger the wake word.
                    self._wake_listener.set_active(False)
                    QTimer.singleShot(450, self._resume_wake)

        self._speak_text(self._plain(text), on_done=done)

    def _resume_wake(self) -> None:
        if self._handsfree and self._wake_listener is not None and self._convo_state == "idle":
            self._wake_listener.set_active(True)

    def _speak_text(self, text: str, on_done=None) -> None:
        text = (text or "").strip()
        self.stop_speaking()
        self._speak_done_cb = on_done
        if not text:
            self._finish_speaking()
            return
        self._pending_speech = text
        if self.player is not None and not self._speaking:
            self._speaking = True
            rate = self._voice_rate_str()  # honor the voice-speed slider
            spawn_worker(self._workers, lambda: synthesize_speech(text, rate=rate), self._speak_ready)
        else:
            self._fallback_say(text)
            self._guard_speaking(text)

    def _speak_ready(self, ok: bool, payload) -> None:
        self._speaking = False
        text = getattr(self, "_pending_speech", "")
        if ok and payload and self.player is not None:
            self.player.setSource(QUrl.fromLocalFile(payload))
            self.player.play()
            self._guard_speaking(text)
        else:
            self._fallback_say(text)
            self._guard_speaking(text)

    def _guard_speaking(self, text: str) -> None:
        """Safety net: re-enable the UI even if the end-of-speech signal never arrives."""
        ms = max(2500, min(60000, len(text or "") * 70))
        QTimer.singleShot(ms, self._finish_speaking)

    def _on_media_status(self, status) -> None:
        try:
            ended = status == QMediaPlayer.MediaStatus.EndOfMedia
        except Exception:
            ended = False
        if ended:
            self._finish_speaking()

    def _on_tts_state(self, state) -> None:
        try:
            ready = state == QTextToSpeech.State.Ready
        except Exception:
            ready = False
        if ready and self._speak_done_cb is not None:
            self._finish_speaking()

    def _finish_speaking(self) -> None:
        cb = self._speak_done_cb
        self._speak_done_cb = None
        if cb is not None:
            cb()

    def _fallback_say(self, text: str) -> None:
        if getattr(self, "tts", None) is not None and text and not text.startswith("Click 'Generate"):
            self.tts.stop()
            self.tts.say(text)

    def stop_speaking(self) -> None:
        if getattr(self, "player", None) is not None:
            self.player.stop()
        if getattr(self, "tts", None) is not None:
            self.tts.stop()


ENTERPRISE_SINCE_DAYS_SETTING = "enterprise_since_days"


class EnterpriseTab(QWidget):
    """Your work command center: recent **work shipped** (git history across your projects) and what
    **needs your attention** (Slack mentions/DMs), summarized by Claude into a glance-able briefing.
    Read-only; the Slack token + repo list live in the git-ignored settings. Heavy work runs off-thread."""

    status_signal = pyqtSignal(str)

    def __init__(self, memory: SQLiteMemory) -> None:
        super().__init__()
        self.memory = memory
        self.settings = AppSettings()
        self._workers: set = set()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        header = QLabel("Enterprise — your work command center")
        header.setObjectName("sectionHeader")
        self.since_box = QSpinBox()
        self.since_box.setRange(1, 90)
        self.since_box.setSuffix(" days")
        self.since_box.setValue(int(self.settings.get(ENTERPRISE_SINCE_DAYS_SETTING, 7) or 7))
        self.since_box.valueChanged.connect(lambda v: self.settings.set(ENTERPRISE_SINCE_DAYS_SETTING, v))
        self.settings_button = QPushButton("⚙ Settings")
        self.settings_button.clicked.connect(self.show_settings)
        self.refresh_button = QPushButton("Refresh and summarize")
        self.refresh_button.clicked.connect(self.refresh_summary)
        top = QHBoxLayout()
        top.addWidget(header)
        top.addStretch(1)
        top.addWidget(QLabel("Look back"))
        top.addWidget(self.since_box)
        top.addWidget(self.settings_button)
        top.addWidget(self.refresh_button)
        layout.addLayout(top)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status_signal.connect(self.status.setText)
        layout.addWidget(self.status)

        # Self-improvements awaiting approval — the headline of this tab.
        pending_box = QGroupBox("Self-improvements awaiting approval")
        pending_outer = QVBoxLayout(pending_box)
        pend_top = QHBoxLayout()
        self.pending_hint = QLabel("")
        self.pending_hint.setStyleSheet("color:#6fb3c0;")
        self.crash_button = QPushButton("Check for crashes")
        self.crash_button.clicked.connect(self._check_crashes_clicked)
        pend_top.addWidget(self.pending_hint)
        pend_top.addStretch(1)
        pend_top.addWidget(self.crash_button)
        pending_outer.addLayout(pend_top)
        self.pending_container = QVBoxLayout()
        pending_outer.addLayout(self.pending_container)
        layout.addWidget(pending_box)

        brief_box = QGroupBox("At a glance")
        brief_layout = QVBoxLayout(brief_box)
        self.briefing = QTextEdit()
        self.briefing.setReadOnly(True)
        self.briefing.setMinimumHeight(96)
        self.briefing.setMaximumHeight(120)
        brief_layout.addWidget(self.briefing)
        layout.addWidget(brief_box)

        panels = QHBoxLayout()
        git_box = QGroupBox("Work shipped (git)")
        git_layout = QVBoxLayout(git_box)
        self.git_view = QTextEdit()
        self.git_view.setReadOnly(True)
        self.git_view.setMinimumHeight(220)
        git_layout.addWidget(self.git_view)
        slack_box = QGroupBox("Slack — needs your attention")
        slack_layout = QVBoxLayout(slack_box)
        self.slack_view = QTextEdit()
        self.slack_view.setReadOnly(True)
        self.slack_view.setMinimumHeight(220)
        slack_layout.addWidget(self.slack_view)
        panels.addWidget(git_box)
        panels.addWidget(slack_box)
        layout.addLayout(panels)

        self.cost_label = QLabel("")
        self.cost_label.setStyleSheet("color:#6fb3c0;")
        layout.addWidget(self.cost_label)

        self._build_settings_dialog()
        self._show_config_hint()

    def _default_repos(self) -> str:
        try:
            return str(load_config().root_dir)
        except Exception:
            return ""

    def _repos(self) -> list:
        raw = self.settings.get(ENTERPRISE_REPOS_SETTING, "") or self._default_repos()
        return parse_repos(raw)

    def _build_settings_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("HELIX — Enterprise Settings")
        outer = QVBoxLayout(dialog)
        form = QGridLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.slack_token = QLineEdit()
        self.slack_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.slack_token.setText(self.settings.get(SLACK_TOKEN_SETTING, ""))
        self.slack_token.setPlaceholderText("xoxp-… (Slack user token)")
        save_slack = QPushButton("Save")
        save_slack.clicked.connect(self._save_slack)
        test_slack = QPushButton("Test")
        test_slack.clicked.connect(self._test_slack)
        form.addWidget(QLabel("Slack user token"), 0, 0)
        form.addWidget(self.slack_token, 0, 1)
        form.addWidget(save_slack, 0, 2)
        form.addWidget(test_slack, 0, 3)
        self.repos_edit = QTextEdit()
        self.repos_edit.setMinimumHeight(120)
        self.repos_edit.setPlainText(self.settings.get(ENTERPRISE_REPOS_SETTING, "") or self._default_repos())
        save_repos = QPushButton("Save")
        save_repos.clicked.connect(self._save_repos)
        form.addWidget(QLabel("Project repo paths\n(one per line)"), 1, 0)
        form.addWidget(self.repos_edit, 1, 1, 1, 2)
        form.addWidget(save_repos, 1, 3)
        outer.addLayout(form)
        note = QLabel(
            "Git: list local folders of your projects (one path per line) — HELIX reads recent commits "
            "(read-only, it never pulls or changes anything). Slack: create an app at api.slack.com/apps, "
            f"add these User Token Scopes [{SLACK_USER_SCOPES}], install it to your workspace, and paste "
            "the 'User OAuth Token'. Secrets stay on this machine (git-ignored)."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#6fb3c0;")
        outer.addWidget(note)
        self._slack_test_label = QLabel("")
        self._slack_test_label.setWordWrap(True)
        outer.addWidget(self._slack_test_label)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        outer.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.resize(640, 380)
        self._settings_dialog = dialog

    def show_settings(self) -> None:
        self._settings_dialog.exec()

    def _save_slack(self) -> None:
        self.settings.set(SLACK_TOKEN_SETTING, self.slack_token.text().strip())
        self._slack_test_label.setText("Saved.")
        self._show_config_hint()

    def _save_repos(self) -> None:
        self.settings.set(ENTERPRISE_REPOS_SETTING, self.repos_edit.toPlainText().strip())
        self._slack_test_label.setText(f"Saved {len(self._repos())} repo path(s).")
        self._show_config_hint()

    def _test_slack(self) -> None:
        token = self.slack_token.text().strip()
        if not token:
            self._slack_test_label.setText("Enter a token first.")
            return
        self._slack_test_label.setText("Testing…")
        spawn_worker(self._workers, lambda: SlackClient(token).auth_test(), self._slack_tested)

    def _slack_tested(self, ok: bool, payload) -> None:
        if ok and isinstance(payload, dict):
            self._slack_test_label.setText(
                f"Connected as {payload.get('user', 'you')} on {payload.get('team', 'your team')}."
            )
        else:
            self._slack_test_label.setText(f"Could not connect. {payload}")

    def _show_config_hint(self) -> None:
        repos = self._repos()
        has_slack = bool(self.settings.get(SLACK_TOKEN_SETTING, ""))
        bits = [f"{len(repos)} project repos", "Slack connected" if has_slack else "Slack not set up yet"]
        self.status.setText(". ".join(bits) + ". Click Refresh and summarize.")

    def refresh_summary(self) -> None:
        self.progress.setVisible(True)
        self.refresh_button.setEnabled(False)
        since = self.since_box.value()
        repos = self._repos()
        token = self.settings.get(SLACK_TOKEN_SETTING, "")
        spawn_worker(self._workers, lambda: self._gather(repos, token, since), self._gathered)

    def _gather(self, repos: list, token: str, since: int) -> dict:
        self.status_signal.emit("Reading git history across your projects")
        git_summaries = gather_git_digest(repos, since_days=since)
        git_text = format_git_digest(git_summaries, since_days=since)
        slack_text = ""
        if token:
            self.status_signal.emit("Pulling your Slack activity")
            try:
                slack_text = format_slack_digest(
                    gather_slack_digest(SlackClient(token), lookback_hours=max(1, since) * 24)
                )
            except SlackError as exc:
                slack_text = f"Slack could not be reached. {exc}"
        else:
            slack_text = "Slack is not connected yet. Add a Slack token in Settings to see your messages here."
        summary = ""
        cost = 0.0
        if git_summaries or token:
            self.status_signal.emit("Summarizing with Claude")
            try:
                model = DEFAULT_RESEARCH_MODEL
                client = ClaudeClient(ClaudeConfig(model=model, timeout_seconds=RESEARCH_TIMEOUT_SECONDS))
                summary = client.complete(build_enterprise_summary_prompt(git_text, slack_text), max_tokens=2048)
                usage = client.last_usage or {}
                in_tok = int(usage.get("input_tokens", 0) or 0)
                out_tok = int(usage.get("output_tokens", 0) or 0)
                cost = estimate_cost(model, in_tok, out_tok)
                self.memory.record_ai_usage(model, in_tok, out_tok, cost)
            except ClaudeError as exc:
                summary = f"(AI summary unavailable: {exc})\nThe raw git/Slack digests are shown below."
        return {"summary": summary, "git_text": git_text, "slack_text": slack_text, "cost": cost}

    def _gathered(self, ok: bool, payload) -> None:
        self.progress.setVisible(False)
        self.refresh_button.setEnabled(True)
        if not ok:
            self.status.setText(f"Error: {payload}")
            return
        self.briefing.setPlainText(
            payload["summary"]
            or "Nothing to show yet. Add your project folders and your Slack token in Settings, then refresh."
        )
        self.git_view.setPlainText(payload["git_text"])
        self.slack_view.setPlainText(payload["slack_text"])
        self.status.setText("Updated.")
        if payload.get("cost"):
            month = self.memory.ai_usage_summary().get("month_cost", 0.0)
            self.cost_label.setText(
                f"Claude cost estimate: this summary ${payload['cost']:.4f}, this month ${month:.4f}."
            )

    def refresh(self) -> None:
        self._show_config_hint()
        self._render_pending()

    # --- self-improvements awaiting approval --------------------------------- #

    def _clear_layout(self, lay) -> None:
        while lay.count():
            item = lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def _render_pending(self) -> None:
        self._clear_layout(self.pending_container)
        try:
            items = selfdev_engine.list_pending(self.settings)
        except Exception:
            items = []
        self.pending_hint.setText(
            f"{len(items)} waiting for your OK" if items else "Nothing waiting — all caught up."
        )
        for rec in items:
            frame = QFrame()
            frame.setStyleSheet(
                "QFrame{border:1px solid #16323b;border-radius:8px;} QPushButton{padding:4px 12px;}"
            )
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(12, 10, 12, 10)
            task = QLabel((rec.get("task") or "(change)").strip())
            task.setWordWrap(True)
            task.setStyleSheet("font-weight:700;border:none;")
            files = rec.get("files") or []
            sub = (rec.get("summary") or "").strip()
            sub = sub[:157] + "…" if len(sub) > 160 else sub
            meta = QLabel(sub + (f"   ·   {len(files)} file(s)" if files else ""))
            meta.setWordWrap(True)
            meta.setStyleSheet("color:#6fb3c0;border:none;")
            btns = QHBoxLayout()
            view = QPushButton("View")
            view.clicked.connect(lambda _=False, r=rec: self._view_pending(r))
            reject = QPushButton("✗ Reject")
            reject.clicked.connect(lambda _=False, b=rec.get("branch"): self._reject(b))
            approve = QPushButton("✓ Approve & merge")
            approve.clicked.connect(lambda _=False, b=rec.get("branch"): self._approve(b))
            btns.addWidget(view)
            btns.addStretch(1)
            btns.addWidget(reject)
            btns.addWidget(approve)
            fl.addWidget(task)
            fl.addWidget(meta)
            fl.addLayout(btns)
            self.pending_container.addWidget(frame)

    def _view_pending(self, rec: dict) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Proposed change")
        v = QVBoxLayout(dialog)
        body = QTextEdit()
        body.setReadOnly(True)
        body.setPlainText(
            f"Task:\n{rec.get('task', '')}\n\nWhat changed:\n{rec.get('summary', '')}\n\n"
            f"Files / diffstat:\n{rec.get('diffstat') or chr(10).join(rec.get('files', []))}\n\n"
            f"Branch: {rec.get('branch', '')}"
        )
        v.addWidget(body)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        v.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.resize(680, 460)
        dialog.exec()

    def _pending_busy(self, busy: bool, message: str = "") -> None:
        self.progress.setVisible(busy)
        self.crash_button.setEnabled(not busy)
        if message:
            self.status.setText(message)

    def _approve(self, branch: str) -> None:
        if not branch:
            return
        self._pending_busy(True, f"Smoke-checking and merging {branch}…")
        spawn_worker(self._workers, lambda: selfdev_engine.approve(self.settings, pending_id=branch), self._pending_done)

    def _reject(self, branch: str) -> None:
        if not branch:
            return
        self._pending_busy(True, f"Rejecting {branch}…")
        spawn_worker(self._workers, lambda: selfdev_engine.reject(self.settings, pending_id=branch), self._pending_done)

    def _check_crashes_clicked(self) -> None:
        self._pending_busy(True, "Checking the log for crashes and drafting fixes…")
        spawn_worker(self._workers, lambda: selfdev_triggers.maybe_fix_crashes(self.settings), self._crashes_drafted)

    def _crashes_drafted(self, ok: bool, payload) -> None:
        self._pending_busy(False)
        if ok and payload:
            self.status.setText(f"Drafted {len(payload)} crash fix(es) — review below.")
        elif ok:
            self.status.setText("No new crashes to fix.")
        else:
            self.status.setText(f"Crash check failed: {payload}")
        self._render_pending()

    def _pending_done(self, ok: bool, payload) -> None:
        self._pending_busy(False)
        if ok and hasattr(payload, "message"):
            self.status.setText(payload.message)
        elif not ok:
            self.status.setText(f"Error: {payload}")
        self._render_pending()


class PlaceholderTab(QWidget):
    def __init__(self, title: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)

        header = QLabel(title)
        header.setObjectName("sectionHeader")
        panel = QGroupBox("Reserved")

        panel_layout = QVBoxLayout(panel)
        panel_layout.addStretch(1)

        layout.addWidget(header)
        layout.addWidget(panel, 1)


class HomeTab(QWidget):
    """Interactive household checklist: check tasks off, see what's due by frequency, and get AI
    suggestions for saving time & money. Persisted to settings (action/item/frequency + last-done)."""

    HOME_TASKS_SETTING = "home_tasks"
    DEFAULT_TASKS = [
        ["Collect", "Mail", "Daily"],
        ["Wash", "Bedding", "Weekly"],
        ["Water", "Plants", "Twice a week"],
        ["Clean", "Kato's lizard tank", "Weekly"],
        ["Do", "Laundry", "Weekly"],
        ["Wash", "Dishes", "Daily"],
        ["Clean", "Bathrooms", "Weekly"],
        ["Vacuum & shampoo", "Carpets", "Monthly"],
        ["Replace", "Air filter", "Monthly"],
        ["Order", "Groceries", "Weekly"],
        ["Pay", "Home bills", "Monthly"],
        ["Pay", "Business bills", "Monthly"],
        ["Restock", "Baby supplies", "Weekly"],
        ["Order", "Butcher / meat", "Weekly"],
        ["Take out", "Trash", "Weekly"],
        ["Prep", "Meals for the week", "Weekly"],
        ["Inventory", "Non-food household items", "Monthly"],
        ["Review", "Appointments", "Weekly"],
    ]
    _STATUS_COLORS = {
        "Overdue": "#ff6b6b", "Due now": "#ffc857", "Due soon": "#1dd8ff", "On track": "#33d17a",
    }

    def __init__(self, memory: SQLiteMemory) -> None:
        super().__init__()
        self.memory = memory
        self.settings = AppSettings()
        self._loading = False
        self._workers = set()
        self._suggest_busy = False
        self._sms_busy = False

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer_layout.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        header = QLabel("Home")
        header.setObjectName("sectionHeader")
        subtitle = QLabel(
            "Check tasks off as you do them - HELIX flags what's due by its frequency. "
            "Double-click Action / Item / Frequency to edit."
        )
        subtitle.setWordWrap(True)
        self.summary = QLabel()
        self.summary.setWordWrap(True)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Done", "Action", "Item", "Frequency", "Status", "Last done"])
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.SelectedClicked
        )
        self.table.itemChanged.connect(self._on_item_changed)

        actions = QHBoxLayout()
        actions.setSpacing(12)
        add_button = QPushButton("Add Task")
        add_button.clicked.connect(self.add_row)
        remove_button = QPushButton("Remove Selected")
        remove_button.clicked.connect(self.remove_row)
        reset_button = QPushButton("Reset to Defaults")
        reset_button.clicked.connect(self.reset_defaults)
        actions.addStretch(1)
        actions.addWidget(reset_button)
        actions.addWidget(remove_button)
        actions.addWidget(add_button)

        sms_box = QGroupBox("Text reminders to my phone (free, via Gmail email-to-SMS)")
        sms_form = QGridLayout(sms_box)
        sms_form.setHorizontalSpacing(12)
        sms_form.setVerticalSpacing(8)
        self.sms_sender = QLineEdit(self.settings.get(SMS_SENDER_SETTING, DEFAULT_SENDER) or DEFAULT_SENDER)
        self.sms_password = QLineEdit()
        self.sms_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.sms_password.setPlaceholderText(
            "Saved" if self.settings.get(SMS_APP_PASSWORD_SETTING) else "Gmail App Password (needs 2FA)"
        )
        self.sms_phone = QLineEdit(self.settings.get(SMS_PHONE_SETTING, "") or "")
        self.sms_phone.setPlaceholderText("Your phone number")
        self.sms_carrier = NoScrollComboBox()
        for label, key in CARRIER_CHOICES:
            self.sms_carrier.addItem(label, key)
        saved_carrier = self.settings.get(SMS_CARRIER_SETTING, "")
        for index in range(self.sms_carrier.count()):
            if self.sms_carrier.itemData(index) == saved_carrier:
                self.sms_carrier.setCurrentIndex(index)
                break
        save_sms = QPushButton("Save")
        save_sms.clicked.connect(self._save_sms)
        test_sms = QPushButton("Send test text")
        test_sms.clicked.connect(self.send_test_text)
        self.sms_status = QLabel()
        self.sms_status.setWordWrap(True)
        # Auto-text reminders on a timer while the app is open (§22).
        self.auto_check = QCheckBox("Auto-text my due tasks every")
        self.auto_check.setObjectName("autoTextToggle")
        self.auto_check.setChecked(bool(self.settings.get(SMS_AUTO_ENABLED_SETTING, False)))
        self.auto_hours = QSpinBox()
        self.auto_hours.setMinimum(1)
        self.auto_hours.setMaximum(24)
        self.auto_hours.setSuffix(" hour(s)")
        self.auto_hours.setValue(int(self.settings.get(SMS_AUTO_HOURS_SETTING, 1) or 1))
        self.auto_check.toggled.connect(self._on_auto_toggled)
        self.auto_hours.valueChanged.connect(self._on_auto_hours_changed)
        auto_row = QHBoxLayout()
        auto_row.addWidget(self.auto_check)
        auto_row.addWidget(self.auto_hours)
        auto_row.addStretch(1)
        sms_note = QLabel(
            "Uses a Gmail App Password (turn on 2-Step Verification, then create an App Password). "
            "Auto-text only fires while HELIX is open; for texts when it's closed, also schedule  "
            "python main.py notify  in Windows Task Scheduler."
        )
        sms_note.setWordWrap(True)
        sms_note.setStyleSheet("color: #6fb3c0;")
        sms_form.addWidget(QLabel("Sender Gmail"), 0, 0)
        sms_form.addWidget(self.sms_sender, 0, 1)
        sms_form.addWidget(QLabel("App password"), 1, 0)
        sms_form.addWidget(self.sms_password, 1, 1)
        sms_form.addWidget(QLabel("Your phone"), 2, 0)
        sms_form.addWidget(self.sms_phone, 2, 1)
        sms_form.addWidget(QLabel("Carrier"), 3, 0)
        sms_form.addWidget(self.sms_carrier, 3, 1)
        sms_form.addLayout(auto_row, 4, 0, 1, 2)
        sms_buttons = QHBoxLayout()
        sms_buttons.addStretch(1)
        sms_buttons.addWidget(test_sms)
        sms_buttons.addWidget(save_sms)
        sms_form.addLayout(sms_buttons, 5, 0, 1, 2)
        sms_form.addWidget(self.sms_status, 6, 0, 1, 2)
        sms_form.addWidget(sms_note, 7, 0, 1, 2)

        layout.addWidget(header)
        layout.addWidget(subtitle)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)
        layout.addWidget(sms_box)

        # Auto-text reminder timer (fires while the app is open; §22).
        self.sms_timer = QTimer(self)
        self.sms_timer.timeout.connect(self._auto_text_tick)
        self._apply_auto_timer()

        self.load_tasks()

    def _on_auto_toggled(self, checked: bool) -> None:
        self.settings.set(SMS_AUTO_ENABLED_SETTING, checked)
        self.settings.set(SMS_AUTO_HOURS_SETTING, self.auto_hours.value())
        self._apply_auto_timer()
        if checked:
            self._auto_text_tick()  # immediate first check on enable (sends only if something's due)

    def _on_auto_hours_changed(self, _value: int) -> None:
        self.settings.set(SMS_AUTO_HOURS_SETTING, self.auto_hours.value())
        self._apply_auto_timer()  # restart with the new interval; no immediate re-send

    def _apply_auto_timer(self) -> None:
        if bool(self.settings.get(SMS_AUTO_ENABLED_SETTING, False)):
            hours = max(1, int(self.settings.get(SMS_AUTO_HOURS_SETTING, 1) or 1))
            self.sms_timer.start(hours * 3600 * 1000)
            self.sms_status.setText(
                f"Auto-reminders on — I'll text your due tasks every {hours} hour(s) while HELIX is open."
            )
        else:
            self.sms_timer.stop()

    def _auto_text_tick(self) -> None:
        if self._sms_busy:
            return
        tasks = self.settings.get(self.HOME_TASKS_SETTING) or []
        if not due_tasks(tasks):  # only text when there's actually something due/overdue
            return
        if not is_configured(self.settings):
            self.sms_status.setText("Auto-reminder skipped — set your phone, carrier, and app password.")
            return
        self._sms_busy = True
        spawn_worker(self._workers, lambda: send_reminder(tasks, self.settings), self._auto_text_done)

    def _auto_text_done(self, ok: bool, payload) -> None:
        self._sms_busy = False
        self.sms_status.setText(f"Auto-reminder sent: {payload}" if ok else f"Auto-reminder failed: {payload}")

    def _status_for(self, freq: str, last_done: str) -> tuple:
        label = task_status(freq, last_done)  # canonical logic in helix.home.tasks (also used headless)
        return label, self._STATUS_COLORS.get(label, "#6fb3c0")

    def _fmt_last(self, last_done: str) -> str:
        if not last_done:
            return "never"
        try:
            return datetime.strptime(str(last_done)[:10], "%Y-%m-%d").strftime("%b %d")
        except ValueError:
            return "never"

    def _set_row(self, row: int, action: str, item: str, freq: str, last_done: str) -> None:
        status, color = self._status_for(freq, last_done)
        satisfied = status in ("On track", "Due soon")
        done = QTableWidgetItem()
        done.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        done.setCheckState(Qt.CheckState.Checked if (last_done and satisfied) else Qt.CheckState.Unchecked)
        done.setData(Qt.ItemDataRole.UserRole, last_done or "")
        done.setToolTip("Check when you've done it - HELIX resets this task's clock to today.")
        self.table.setItem(row, 0, done)
        self.table.setItem(row, 1, QTableWidgetItem(action))
        self.table.setItem(row, 2, QTableWidgetItem(item))
        self.table.setItem(row, 3, QTableWidgetItem(freq))
        status_item = QTableWidgetItem(status)
        status_item.setForeground(QColor(color))
        status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, 4, status_item)
        last_item = QTableWidgetItem(self._fmt_last(last_done))
        last_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, 5, last_item)

    def load_tasks(self) -> None:
        raw = self.settings.get(self.HOME_TASKS_SETTING)
        tasks = raw if isinstance(raw, list) and raw else [list(t) for t in self.DEFAULT_TASKS]
        self._loading = True
        self.table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            cells = (list(task) + ["", "", "", ""])[:4]
            self._set_row(row, cells[0], cells[1], cells[2], cells[3])
        self._loading = False
        self._update_summary()

    def _refresh_status(self, row: int) -> None:
        done = self.table.item(row, 0)
        freq = self.table.item(row, 3).text() if self.table.item(row, 3) else ""
        last_done = done.data(Qt.ItemDataRole.UserRole) if done else ""
        status, color = self._status_for(freq, last_done)
        if self.table.item(row, 4):
            self.table.item(row, 4).setText(status)
            self.table.item(row, 4).setForeground(QColor(color))
        if self.table.item(row, 5):
            self.table.item(row, 5).setText(self._fmt_last(last_done))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        row, col = item.row(), item.column()
        self._loading = True
        if col == 0:  # Done checkbox toggled -> stamp/clear last-done
            checked = item.checkState() == Qt.CheckState.Checked
            item.setData(Qt.ItemDataRole.UserRole, datetime.now().strftime("%Y-%m-%d") if checked else "")
        self._refresh_status(row)
        self._loading = False
        self.save_tasks()
        self._update_summary()

    def save_tasks(self) -> None:
        tasks = []
        for row in range(self.table.rowCount()):
            def cell(col: int) -> str:
                return self.table.item(row, col).text().strip() if self.table.item(row, col) else ""

            action, item_, freq = cell(1), cell(2), cell(3)
            done = self.table.item(row, 0)
            last_done = done.data(Qt.ItemDataRole.UserRole) if done else ""
            if any([action, item_, freq]):
                tasks.append([action, item_, freq, last_done or ""])
        self.settings.set(self.HOME_TASKS_SETTING, tasks)

    def _update_summary(self) -> None:
        due = overdue = 0
        for row in range(self.table.rowCount()):
            status = self.table.item(row, 4).text() if self.table.item(row, 4) else ""
            if status == "Overdue":
                overdue += 1
            elif status == "Due now":
                due += 1
        bits = []
        if overdue:
            bits.append(f"{overdue} overdue")
        if due:
            bits.append(f"{due} due now")
        headline = "   ·   ".join(bits) if bits else "All caught up"
        self.summary.setText(f"{headline}      ({self.table.rowCount()} tasks)")

    def add_row(self) -> None:
        row = self.table.rowCount()
        self._loading = True
        self.table.insertRow(row)
        self._set_row(row, "", "", "Weekly", "")
        self._loading = False
        self.table.editItem(self.table.item(row, 1))

    def remove_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self.save_tasks()
            self._update_summary()

    def reset_defaults(self) -> None:
        self.settings.set(self.HOME_TASKS_SETTING, [list(t) for t in self.DEFAULT_TASKS])
        self.load_tasks()

    def refresh(self) -> None:
        self.load_tasks()

    def _current_tasks(self) -> list:
        out = []
        for row in range(self.table.rowCount()):
            def cell(col: int) -> str:
                return self.table.item(row, col).text().strip() if self.table.item(row, col) else ""

            action, item_, freq = cell(1), cell(2), cell(3)
            if action or item_:
                out.append(f"{action} {item_} ({freq})".strip())
        return out

    def _save_sms(self) -> None:
        self.settings.set(SMS_SENDER_SETTING, self.sms_sender.text().strip() or DEFAULT_SENDER)
        password = self.sms_password.text().strip()
        if password:
            self.settings.set(SMS_APP_PASSWORD_SETTING, password)
        self.settings.set(SMS_PHONE_SETTING, self.sms_phone.text().strip())
        self.settings.set(SMS_CARRIER_SETTING, self.sms_carrier.currentData())
        self.sms_password.clear()
        self.sms_password.setPlaceholderText(
            "Saved" if self.settings.get(SMS_APP_PASSWORD_SETTING) else "Gmail App Password (needs 2FA)"
        )
        self.sms_status.setText("Saved.")

    def send_test_text(self) -> None:
        if self._sms_busy:
            return
        self._save_sms()
        if not is_configured(self.settings):
            self.sms_status.setText("Set your phone, carrier, and Gmail app password first.")
            return
        tasks = self.settings.get(self.HOME_TASKS_SETTING) or []
        self._sms_busy = True
        self.sms_status.setText("Sending a test text...")
        spawn_worker(self._workers, lambda: send_reminder(tasks, self.settings), self._test_text_done)

    def _test_text_done(self, ok: bool, payload) -> None:
        self._sms_busy = False
        self.sms_status.setText(str(payload) if ok else f"Failed: {payload}")


class LearningTab(QWidget):
    PILLARS = [
        {
            "name": "Home",
            "status": "Next project",
            "summary": "Smart-home assistant: routines, reminders, household automation. The next pillar once the investing tool is solid.",
        },
        {
            "name": "Enterprise",
            "status": "Planned",
            "summary": "Business tooling: documents, workflows, analytics. On the roadmap after Home.",
        },
        {
            "name": "Investment",
            "status": "Active",
            "summary": "Live today: auto-invests via Alpaca using Claude research. Open it for market analysis and briefs.",
        },
    ]

    def __init__(self, memory: SQLiteMemory) -> None:
        super().__init__()
        self.memory = memory
        self.settings = AppSettings()

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer_layout.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        self._workers = set()

        header = QLabel("Learning")
        header.setObjectName("sectionHeader")

        self.subtabs = QTabWidget()

        self.pillars_table = QTableWidget(0, 3)
        self.pillars_table.setHorizontalHeaderLabels(["Pillar", "Status", "Open"])
        self.pillars_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.pillars_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.pillars_table.verticalHeader().setVisible(False)
        self.pillars_table.verticalHeader().setDefaultSectionSize(54)
        self.pillars_table.setAlternatingRowColors(True)
        self.pillars_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.pillars_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        streams_panel = QWidget()
        streams_layout = QVBoxLayout(streams_panel)
        streams_layout.setContentsMargins(18, 18, 18, 18)
        intro = QLabel("Open a pillar to view its briefing. Investment is live; Home and Enterprise are upcoming.")
        intro.setWordWrap(True)
        streams_layout.addWidget(intro)
        streams_layout.addWidget(self.pillars_table, 1)

        ai_panel = QWidget()
        ai_layout = QVBoxLayout(ai_panel)
        ai_layout.setContentsMargins(18, 18, 18, 18)
        ai_form = QGridLayout()
        ai_form.setHorizontalSpacing(16)
        ai_form.setVerticalSpacing(14)

        self.ai_mode = QComboBox()
        self.ai_mode.addItems((AI_MODE_MOCK, AI_MODE_CLAUDE))
        self.ai_mode.setCurrentText(self.settings.get(AI_MODE_SETTING, AI_MODE_MOCK))
        self.ai_mode.currentTextChanged.connect(self.save_ai_mode)
        self.ai_model = QLineEdit(DEFAULT_RESEARCH_MODEL)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText(self.api_key_placeholder())
        self.ai_focus = QLineEdit()
        self.ai_focus.setPlaceholderText("Ticker or question, e.g. VOO, AAPL, rates, AI chips")
        self.ai_status = QLabel(self.claude_status_text())
        self.ai_status.setWordWrap(True)

        save_key = QPushButton("Save Key")
        save_key.clicked.connect(self.save_claude_key)
        clear_key = QPushButton("Clear Key")
        clear_key.clicked.connect(self.clear_claude_key)
        ask_ai = QPushButton("Run Research")
        ask_ai.clicked.connect(self.run_ai_research)
        save_research = QPushButton("Save To Journal")
        save_research.clicked.connect(self.save_ai_output)

        ai_form.addWidget(QLabel("Mode"), 0, 0)
        ai_form.addWidget(self.ai_mode, 0, 1)
        ai_form.addWidget(QLabel("Model"), 1, 0)
        ai_form.addWidget(self.ai_model, 1, 1)
        ai_form.addWidget(QLabel("Claude API key"), 2, 0)
        ai_form.addWidget(self.api_key, 2, 1)
        ai_form.addWidget(QLabel("Focus"), 3, 0)
        ai_form.addWidget(self.ai_focus, 3, 1)
        ai_form.addWidget(save_key, 0, 2)
        ai_form.addWidget(clear_key, 1, 2)
        ai_form.addWidget(ask_ai, 2, 2)
        ai_form.addWidget(save_research, 3, 2)

        self.ai_progress = QProgressBar()
        self.ai_progress.setRange(0, 0)
        self.ai_progress.setTextVisible(False)
        self.ai_progress.setMaximumHeight(6)
        self.ai_progress.setVisible(False)

        self.ai_output = QTextEdit()
        self.ai_output.setObjectName("briefingPanel")
        self.ai_output.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.ai_output.setMinimumHeight(300)

        self.ai_cost = QLabel()
        self.ai_cost.setWordWrap(True)

        ai_layout.addLayout(ai_form)
        ai_layout.addWidget(self.ai_status)
        ai_layout.addWidget(self.ai_progress)
        ai_layout.addWidget(self.ai_cost)
        ai_layout.addWidget(self.ai_output, 1)

        self.subtabs.addTab(streams_panel, "Streams")
        self.subtabs.addTab(ai_panel, "Claude")

        layout.addWidget(header)
        layout.addWidget(self.subtabs, 1)

        self.refresh()

    def refresh(self) -> None:
        self.pillars_table.setRowCount(len(self.PILLARS))
        for row, pillar in enumerate(self.PILLARS):
            self.pillars_table.setItem(row, 0, QTableWidgetItem(pillar["name"]))
            self.pillars_table.setItem(row, 1, QTableWidgetItem(pillar["status"]))
            button = QPushButton("Open")
            button.clicked.connect(lambda checked=False, p=pillar: self.open_pillar(p))
            self.pillars_table.setCellWidget(row, 2, button)
        self.update_ai_cost()

    def open_pillar(self, pillar: dict) -> None:
        PillarDialog(self, self.memory, self.settings, pillar).exec()

    def update_ai_cost(self) -> None:
        summary = self.memory.ai_usage_summary()
        self.ai_cost.setText(
            f"Claude usage (estimated): today ${summary['today_cost']:.4f}  |  this month ${summary['month_cost']:.4f}  "
            f"|  all-time ${summary['total_cost']:.4f} over {summary['calls']} call(s), "
            f"{summary['input_tokens'] + summary['output_tokens']:,} tokens. Exact billing: Anthropic Console."
        )

    def api_key_placeholder(self) -> str:
        return "Saved locally" if self.settings.get(CLAUDE_API_KEY_SETTING) else "Paste key once, then Save Key"

    def claude_status_text(self) -> str:
        if self.settings.get(AI_MODE_SETTING, AI_MODE_MOCK) == AI_MODE_MOCK:
            return "Mock Claude mode is active. No credits or API call required."
        if self.settings.get(CLAUDE_API_KEY_SETTING):
            return "Claude key saved locally. Environment variable still overrides it."
        return "Claude is not connected yet. Paste an API key and click Save Key."

    def save_ai_mode(self, mode: str) -> None:
        self.settings.set(AI_MODE_SETTING, mode)
        self.ai_status.setText(self.claude_status_text())

    def save_claude_key(self) -> None:
        api_key = self.api_key.text().strip()
        if not api_key:
            QMessageBox.warning(self, "HELIX", "Paste a Claude API key first.")
            return

        self.settings.set(CLAUDE_API_KEY_SETTING, api_key)
        self.api_key.clear()
        self.api_key.setPlaceholderText(self.api_key_placeholder())
        self.ai_status.setText(self.claude_status_text())
        QMessageBox.information(self, "HELIX", "Claude API key saved locally.")

    def clear_claude_key(self) -> None:
        self.settings.remove(CLAUDE_API_KEY_SETTING)
        self.api_key.clear()
        self.api_key.setPlaceholderText(self.api_key_placeholder())
        self.ai_status.setText(self.claude_status_text())
        QMessageBox.information(self, "HELIX", "Saved Claude API key cleared.")

    def run_ai_research(self) -> None:
        mode = self.ai_mode.currentText()
        model = self.ai_model.text().strip() or DEFAULT_RESEARCH_MODEL
        focus = self.ai_focus.text().strip()
        watchlist = self.memory.list_watchlist()

        if mode == AI_MODE_MOCK:
            response = generate_mock_research(stream_name="Investment", focus=focus, watchlist=watchlist)
            self.ai_status.setText("Mock research complete.")
            self.ai_output.setPlainText(response)
            return

        prompt = build_research_prompt(stream_name="Investment", focus=focus, watchlist=watchlist)
        self.ai_status.setText(f"Asking Claude: {model}")
        self.ai_progress.setVisible(True)
        spawn_worker(self._workers, lambda: self._claude_call(model, prompt), self._research_done)

    def _claude_call(self, model: str, prompt: str) -> str:
        client = ClaudeClient(ClaudeConfig(model=model))
        text = client.complete(prompt)
        usage = client.last_usage or {}
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        self.memory.record_ai_usage(model, input_tokens, output_tokens, estimate_cost(model, input_tokens, output_tokens))
        return text

    def _research_done(self, ok: bool, payload) -> None:
        self.ai_progress.setVisible(False)
        if not ok:
            self.ai_status.setText("Claude request not completed.")
            self.ai_output.setPlainText(str(payload))
            return
        self.update_ai_cost()
        self.ai_status.setText("Claude research complete.")
        self.ai_output.setPlainText(payload)

    def save_ai_output(self) -> None:
        body = self.ai_output.toPlainText().strip()
        if not body:
            QMessageBox.warning(self, "HELIX", "There is no AI research to save.")
            return

        focus = self.ai_focus.text().strip() or "Investment"
        self.memory.add_journal_entry(entry_type="research", title=f"Research: {focus}", body=body)
        QMessageBox.information(self, "HELIX", "Research saved to journal.")


class PillarDialog(QDialog):
    def __init__(self, parent, memory: SQLiteMemory, settings: AppSettings, pillar: dict) -> None:
        super().__init__(parent)
        self.memory = memory
        self.settings = settings
        self.pillar = pillar
        self._workers = set()
        self.setWindowTitle(f"HELIX - {pillar['name']}")
        self.setMinimumSize(760, 700)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(12)

        title = QLabel(pillar["name"])
        title.setObjectName("sectionHeader")
        status = QLabel(f"Status: {pillar['status']}")
        summary = QLabel(pillar["summary"])
        summary.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(status)
        layout.addWidget(summary)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(6)
        self.progress.setVisible(False)

        buttons = QHBoxLayout()
        if pillar["name"] == "Investment":
            note = QLabel(
                "The logic behind each pick, captured when HELIX last reviewed the stocks. "
                "'Re-rationalize' asks Claude to refresh it now."
            )
            note.setWordWrap(True)
            layout.addWidget(note)

            self.rationale_table = QTableWidget(0, 5)
            self.rationale_table.setHorizontalHeaderLabels(["Symbol", "Action", "Conf", "Why", "Updated"])
            self.rationale_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            self.rationale_table.horizontalHeader().setStretchLastSection(True)
            self.rationale_table.verticalHeader().setVisible(False)
            self.rationale_table.verticalHeader().setDefaultSectionSize(34)
            self.rationale_table.setAlternatingRowColors(True)
            self.rationale_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            layout.addWidget(self.rationale_table, 1)

            sells_label = QLabel("Recent sells - what HELIX exited or trimmed, and why")
            sells_label.setWordWrap(True)
            layout.addWidget(sells_label)
            self.sells_table = QTableWidget(0, 6)
            self.sells_table.setHorizontalHeaderLabels(["Symbol", "Reason", "Why", "Amount", "Result", "When"])
            self.sells_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self.sells_table.horizontalHeader().setStretchLastSection(True)
            self.sells_table.verticalHeader().setVisible(False)
            self.sells_table.verticalHeader().setDefaultSectionSize(34)
            self.sells_table.setAlternatingRowColors(True)
            self.sells_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.sells_table.setMaximumHeight(200)
            layout.addWidget(self.sells_table)

            self.perf_label = QLabel("")
            self.perf_label.setWordWrap(True)
            layout.addWidget(self.perf_label)

            layout.addWidget(self.progress)

            rerationalize = QPushButton("Re-rationalize with Claude")
            rerationalize.clicked.connect(self.rerationalize)
            buttons.addWidget(rerationalize)
            self.load_rationale()
            self.load_sells()
        else:
            body = QTextEdit()
            body.setObjectName("briefingPanel")
            body.setReadOnly(True)
            body.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
            body.setPlainText(self._coming_soon_text())
            layout.addWidget(body, 1)
            layout.addWidget(self.progress)

        buttons.addStretch(1)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def _stocks(self) -> list:
        raw = self.settings.get(INVEST_TICKERS_SETTING, DEFAULT_TICKERS)
        return [s.strip().upper() for s in str(raw).replace(";", ",").split(",") if s.strip()]

    def _coming_soon_text(self) -> str:
        return (
            f"{self.pillar['name']} is on the HELIX roadmap.\n\n"
            f"{self.pillar['summary']}\n\n"
            "Investment is the pillar with working code today. Home is the next project, then "
            "Enterprise. This screen will fill in when that pillar is built."
        )

    def load_rationale(self) -> None:
        rows = self.memory.list_stock_rationale()
        if not rows:
            self.rationale_table.setRowCount(1)
            self.rationale_table.setItem(0, 0, QTableWidgetItem("-"))
            self.rationale_table.setItem(
                0, 3, QTableWidgetItem("No logic stored yet. Run the Investment loop, or click Re-rationalize.")
            )
            return
        self.rationale_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            cells = [
                row["symbol"],
                row["action"],
                row["confidence"],
                row["rationale"],
                str(row.get("updated_at", ""))[:10],
            ]
            for col, value in enumerate(cells):
                self.rationale_table.setItem(row_index, col, QTableWidgetItem(str(value)))

    def load_sells(self) -> None:
        self._load_performance()
        rows = self.memory.list_sells(limit=50)
        if not rows:
            self.sells_table.setRowCount(1)
            self.sells_table.setItem(0, 0, QTableWidgetItem("-"))
            self.sells_table.setItem(0, 2, QTableWidgetItem("No sells yet."))
            return
        self.sells_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            return_pct = row.get("return_pct")
            result = f"{return_pct:+.1f}%" if return_pct is not None else "-"
            cells = [
                row["symbol"],
                row["reason"],
                row["rationale"],
                f"${float(row['amount_usd']):,.2f}",
                result,
                str(row.get("created_at", ""))[:16],
            ]
            for col, value in enumerate(cells):
                self.sells_table.setItem(row_index, col, QTableWidgetItem(str(value)))

    def _load_performance(self) -> None:
        perf = self.memory.strategy_performance()
        if perf["closed"] > 0:
            self.perf_label.setText(
                f"Track record: hit rate {perf['hit_rate']}% over {perf['closed']} closed positions   |   "
                f"avg return {perf['avg_return_pct']:+.1f}%   |   realized P/L ${perf['realized_pl']:+,.2f}. "
                "HELIX feeds this back into its next stock ratings."
            )
        else:
            self.perf_label.setText(
                "Track record: no closed positions yet - once HELIX sells, realized results appear here "
                "and begin calibrating future picks."
            )

    def rerationalize(self) -> None:
        stocks = self._stocks()
        if not stocks:
            return
        self.progress.setVisible(True)
        spawn_worker(self._workers, lambda: self._rationale_call(stocks), self._rationale_done)

    def _rationale_call(self, stocks: list) -> int:
        model = DEFAULT_RESEARCH_MODEL
        watchlist = [{"symbol": s, "thesis": "user pick", "max_allocation_pct": None} for s in stocks]
        client = ClaudeClient(ClaudeConfig(model=model, timeout_seconds=RESEARCH_TIMEOUT_SECONDS))
        text = client.complete(
            build_portfolio_research_prompt(100000.0, watchlist, "Aggressive"),
            max_tokens=research_max_tokens(self.settings),  # Settings -> Research effort
        )
        usage = client.last_usage or {}
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        self.memory.record_ai_usage(model, input_tokens, output_tokens, estimate_cost(model, input_tokens, output_tokens))
        ratings = {record["symbol"]: record for record in parse_research_json(text)}
        self.memory.save_stock_rationales(ratings)
        return len(ratings)

    def _rationale_done(self, ok: bool, payload) -> None:
        self.progress.setVisible(False)
        self.load_rationale()


class EquityCurveWidget(QWidget):
    """A compact, HUD-styled equity sparkline painted with QPainter (no charting dependency).

    Consumes an `EquitySeries` (from Alpaca history or HELIX's own equity_history) and draws the
    line, a translucent area fill, a dashed baseline at the period's starting equity, an end-point
    marker, the period change, and start/end date labels. Up = green, down = red, flat = cyan.
    """

    _BG = QColor("#071417")
    _BORDER = QColor("#286979")
    _MUTED = QColor("#6fb3c0")
    _BASELINE = QColor("#ffc857")
    _UP = QColor("#33d17a")
    _DOWN = QColor("#ff6b6b")
    _FLAT = QColor("#1dd8ff")
    _BENCH = QColor("#9fb1bd")  # S&P 500 overlay (dim, dashed)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._series = EquitySeries([])
        self._benchmark = EquitySeries([])
        self._range_label = ""
        self.setMinimumHeight(180)

    def set_series(self, series, range_label: str = "", benchmark=None) -> None:
        # Coalesce on None only — a short (<2 pt) series is still a valid series to hold;
        # paintEvent decides whether it has enough points to draw a line.
        self._series = series if series is not None else EquitySeries([])
        self._benchmark = benchmark if benchmark is not None else EquitySeries([])
        self._range_label = range_label
        self.update()

    def _line_color(self) -> QColor:
        change = self._series.change_usd
        if change > 0:
            return self._UP
        if change < 0:
            return self._DOWN
        return self._FLAT

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setPen(QPen(self._BORDER, 1))
        painter.setBrush(QBrush(self._BG))
        painter.drawRoundedRect(card, 8, 8)

        series = self._series
        benchmark = self._benchmark if self._benchmark else None
        small = QFont(self.font())
        small.setPointSize(max(7, self.font().pointSize() - 1))
        bold = QFont(small)
        bold.setBold(True)

        if not series:
            painter.setFont(small)
            painter.setPen(self._MUTED)
            painter.drawText(card, Qt.AlignmentFlag.AlignCenter, "Balance history fills in as HELIX runs.")
            painter.end()
            return

        align_l = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        align_r = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Header row 1: "EQUITY · <range>" left, account period change right.
        painter.setFont(small)
        row1 = QRectF(14, 7, self.width() - 28, 17)
        painter.setPen(self._MUTED)
        painter.drawText(row1, align_l, "BALANCE" + (f"   ·   {self._range_label}" if self._range_label else ""))
        sign = "+" if series.change_usd >= 0 else "-"
        painter.setFont(bold)
        painter.setPen(self._line_color())
        painter.drawText(row1, align_r, f"{sign}${abs(series.change_usd):,.2f}    {sign}{abs(series.change_pct):.2f}%")

        # Header row 2: S&P 500 legend left, HELIX-minus-index gap right (the honest scoreboard).
        row2 = QRectF(14, 26, self.width() - 28, 16)
        if benchmark:
            painter.setPen(QPen(self._BENCH, 1.5, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(16, 34), QPointF(34, 34))
            painter.setFont(small)
            painter.setPen(self._BENCH)
            painter.drawText(QRectF(40, 26, 160, 16), align_l, "S&P 500")
            relative = series.change_pct - benchmark.change_pct
            rsign = "+" if relative >= 0 else "-"
            painter.setFont(bold)
            painter.setPen(self._UP if relative >= 0 else self._DOWN)
            painter.drawText(row2, align_r, f"vs S&P 500   {rsign}{abs(relative):.2f}%")
        else:
            painter.setFont(small)
            painter.setPen(self._MUTED)
            painter.drawText(row2, align_r, "S&P 500: n/a")

        # Plot geometry (top leaves room for the two header rows).
        left, right, top, bottom = 16.0, 16.0, 50.0, 24.0
        plot_w = self.width() - left - right
        plot_h = self.height() - top - bottom
        if plot_w <= 4 or plot_h <= 4:
            painter.end()
            return

        # Shared y-scale across both lines so the comparison is honest.
        scale_values = list(series.points) + (list(benchmark.points) if benchmark else [])
        low, high = min(scale_values), max(scale_values)
        span = high - low
        if span <= 0:  # flat — pad so the line sits mid-card
            pad = max(abs(high) * 0.01, 1.0)
            low, high, span = low - pad, high + pad, 2 * pad

        def y_of(value: float) -> float:
            return top + plot_h * (1.0 - (value - low) / span)

        def points_for(values: list) -> list:
            n = len(values)
            if n < 2:
                return []
            return [QPointF(left + plot_w * (i / (n - 1)), y_of(v)) for i, v in enumerate(values)]

        # Dashed baseline at the account's starting equity.
        base_color = QColor(self._BASELINE)
        base_color.setAlpha(120)
        painter.setPen(QPen(base_color, 1, Qt.PenStyle.DashLine))
        base_y = y_of(series.start)
        painter.drawLine(QPointF(left, base_y), QPointF(left + plot_w, base_y))

        # Benchmark line behind the account line (dim, dashed).
        if benchmark:
            bench_points = points_for(benchmark.points)
            if bench_points:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(self._BENCH, 1.5, Qt.PenStyle.DashLine))
                painter.drawPolyline(QPolygonF(bench_points))

        # Account line: area fill + solid line + end marker.
        points = points_for(series.points)
        line_color = self._line_color()
        area = QPainterPath()
        area.moveTo(points[0].x(), top + plot_h)
        for point in points:
            area.lineTo(point)
        area.lineTo(points[-1].x(), top + plot_h)
        area.closeSubpath()
        gradient = QLinearGradient(0.0, top, 0.0, top + plot_h)
        fill_top = QColor(line_color)
        fill_top.setAlpha(90)
        fill_bottom = QColor(line_color)
        fill_bottom.setAlpha(10)
        gradient.setColorAt(0.0, fill_top)
        gradient.setColorAt(1.0, fill_bottom)
        painter.fillPath(area, QBrush(gradient))

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(line_color, 2))
        painter.drawPolyline(QPolygonF(points))

        end = points[-1]
        glow = QColor(line_color)
        glow.setAlpha(70)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(end, 6.0, 6.0)
        painter.setBrush(line_color)
        painter.drawEllipse(end, 3.0, 3.0)

        # Start/end date labels.
        painter.setFont(small)
        painter.setPen(self._MUTED)
        half = plot_w / 2
        labels_y = self.height() - 20
        if series.start_label:
            painter.drawText(QRectF(left, labels_y, half, 16), align_l, series.start_label)
        if series.end_label:
            painter.drawText(QRectF(left + half, labels_y, half, 16), align_r, series.end_label)
        painter.end()


class InvestmentTab(QWidget):
    """One simple screen — no sub-tabs. Delegates to InvestTab."""

    def __init__(self, memory: SQLiteMemory, on_saved=None) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.invest_tab = InvestTab(memory)
        layout.addWidget(self.invest_tab)

    def refresh(self) -> None:
        self.invest_tab.refresh()


class InvestTab(QWidget):
    """One screen: Alpaca keys, money to put in, fake/real toggle, START, balance, holdings."""

    research_step = pyqtSignal(str)  # worker -> UI: live "what HELIX is investigating now" messages
    research_issue = pyqtSignal(str)  # worker -> UI: a research call parsed to nothing (§10), don't fail silently

    def __init__(self, memory: SQLiteMemory) -> None:
        super().__init__()
        self.memory = memory
        self.settings = AppSettings()

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer_layout.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        self._workers = set()
        self._portfolio_busy = False
        self._portfolio_quiet = False  # auto-refresh updates silently (no busy-bar flash)
        self._cycle_busy = False
        self._busy_count = 0
        self._raw_positions = {}  # symbol -> raw Alpaca position dict, for the per-row details popup

        self.balance_label = QLabel("Balance: -")
        self.balance_label.setStyleSheet("font-size: 28pt; font-weight: 800; color: #ffc857;")
        self.balance_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.balance_label.setWordWrap(True)
        self.balance_sub = QLabel("")
        self.balance_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.balance_sub.setWordWrap(True)

        self.market_label = QLabel()
        self.market_label.setTextFormat(Qt.TextFormat.RichText)

        self.equity_chart = EquityCurveWidget()
        self.chart_range = NoScrollComboBox()
        self.chart_range.addItems(tuple(CHART_RANGES.keys()))
        self.chart_range.setCurrentText(self.settings.get(INVEST_CHART_RANGE_SETTING, DEFAULT_CHART_RANGE))
        self.chart_range.currentTextChanged.connect(self.on_chart_range_changed)

        # Settings widgets — created here but shown in a dialog (the "Settings" button), not inline.
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText(self._key_placeholder(ALPACA_API_KEY_SETTING))
        self.secret_key = QLineEdit()
        self.secret_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_key.setPlaceholderText(self._key_placeholder(ALPACA_SECRET_KEY_SETTING))
        self.save_keys_button = QPushButton("Save Keys")
        self.save_keys_button.clicked.connect(self.save_keys)

        self.mode = NoScrollComboBox()  # wheel must never flip Practice <-> Real
        self.mode.addItems((INVEST_MODE_PRACTICE, INVEST_MODE_REAL))
        self.mode.setCurrentText(self._mode_from_settings())
        self.mode.currentTextChanged.connect(self.on_mode_changed)

        self.interval = NoScrollComboBox()
        self.interval.addItems(tuple(AUTO_INTERVALS.keys()))
        self.interval.setCurrentText(self.settings.get(INVEST_AUTO_INTERVAL_SETTING, "1 day"))
        self.interval.currentTextChanged.connect(self.on_interval_changed)

        # Adopt the core-satellite mix once (§42) BEFORE the sleeve sliders read their values below.
        self._apply_core_satellite_default()
        self.special_position = percent_box(
            float(self.settings.get(INVEST_SPECIAL_ALLOCATION_SETTING, DEFAULT_SPECIAL_ALLOCATION_PCT * 100))
        )
        self.special_position.editingFinished.connect(
            lambda: self.settings.set(INVEST_SPECIAL_ALLOCATION_SETTING, self.special_position.value())
        )

        # Day-trade sleeve % (§27): a third sleeve for short-term momentum trades, carved off the top
        # like Special. Core gets whatever's left after Special + Day-trade + the cash buffer.
        self.daytrade_position = percent_box(
            float(self.settings.get(INVEST_DAYTRADE_ALLOCATION_SETTING, DEFAULT_DAYTRADE_ALLOCATION_PCT * 100))
        )
        self.daytrade_position.editingFinished.connect(
            lambda: self.settings.set(INVEST_DAYTRADE_ALLOCATION_SETTING, self.daytrade_position.value())
        )

        # Index core % (§42, core-satellite): hold a broad index ETF (VOO) at this %, carved off the top
        # like the speculative sleeves. The book tracks the market by default; the AI sleeves are satellites.
        self.index_position = percent_box(
            float(self.settings.get(INVEST_INDEX_ALLOCATION_SETTING, DEFAULT_INDEX_ALLOCATION_PCT * 100))
        )
        self.index_position.editingFinished.connect(
            lambda: self.settings.set(INVEST_INDEX_ALLOCATION_SETTING, self.index_position.value())
        )

        # How the Special Stocks sleeve is funded (§21): house-money (only profits above your starting
        # balance — safer, may stay empty until you're in the green) vs. always deploy the full %.
        self.special_funding = NoScrollComboBox()
        self.special_funding.addItem("House money — invest only profits (safer)", "house")
        self.special_funding.addItem("Always invest the full % (riskier)", "always")
        _fund_idx = self.special_funding.findData(self.settings.get(INVEST_SPECIAL_FUNDING_SETTING, "house"))
        self.special_funding.setCurrentIndex(max(0, _fund_idx))
        self.special_funding.currentIndexChanged.connect(
            lambda: self.settings.set(INVEST_SPECIAL_FUNDING_SETTING, self.special_funding.currentData())
        )

        # Cost control: when ON (default), HELIX refreshes its AI research on its normal cadence
        # (core ~weekly, special stocks ~nightly — a small Claude cost). When OFF, it trades off the
        # last research it ran and spends nothing more on Claude (picks/ratings go stale).
        self.ai_research_check = QCheckBox("Refresh AI research (uses Claude)")
        self.ai_research_check.setObjectName("aiResearchToggle")
        self.ai_research_check.setChecked(bool(self.settings.get(INVEST_AI_RESEARCH_SETTING, True)))
        self.ai_research_check.setToolTip(
            "On: HELIX keeps its stock research fresh on its normal cadence (core weekly, specials "
            "nightly) — a small Claude cost.  Off: it trades off the last research it ran and makes "
            "no new Claude calls (so it costs ~nothing, but the picks stop updating)."
        )
        self.ai_research_check.toggled.connect(self._on_ai_research_toggled)

        # How often each Claude research pass may run (cadence, in days). HELIX re-runs no sooner
        # than this; the loop still re-checks often but only spends when the gate is open.
        self.core_days = self._day_spin(
            INVEST_CORE_RATING_DAYS_SETTING, int(DEFAULT_RATING_MAX_AGE_DAYS), 1, 90
        )
        self.special_days = self._day_spin(
            INVEST_SPECIAL_DAYS_SETTING, DEFAULT_SPECIAL_RESEARCH_DAYS, 1, 30
        )
        self.daytrade_days = self._day_spin(
            INVEST_DAYTRADE_DAYS_SETTING, DEFAULT_DAYTRADE_RESEARCH_DAYS, 1, 30
        )
        self.roster_days = self._day_spin(
            INVEST_ROSTER_DAYS_SETTING, DEFAULT_ROSTER_REVIEW_DAYS, 7, 365
        )

        # Concentration (§30): cap the core at the top-N highest-conviction buy names so capital
        # concentrates in the best ideas instead of a closet index. 0 = uncapped (default — no change).
        # Backtest first (Prediction scorecard / `python main.py backtest`) to pick N on the evidence:
        # tighter N raises return AND volatility, so it is a risk choice, not a free lunch.
        self.max_positions = QSpinBox()
        self.max_positions.setMinimum(0)
        self.max_positions.setMaximum(500)
        self.max_positions.setSpecialValueText(f"Default (top {DEFAULT_MAX_POSITIONS})")  # shown when value == 0
        try:
            self.max_positions.setValue(int(self.settings.get(INVEST_MAX_POSITIONS_SETTING, 0) or 0))
        except (TypeError, ValueError):
            self.max_positions.setValue(0)
        self.max_positions.setToolTip(
            "Hold at most this many core names (the top-N by conviction, then momentum). "
            f"0 = the baked default (top {DEFAULT_MAX_POSITIONS}), which concentrates rather than spreading across "
            "the whole universe. Set your own N to override - backtest first (tighter = higher return but "
            "higher volatility)."
        )
        self.max_positions.valueChanged.connect(
            lambda value: self.settings.set(INVEST_MAX_POSITIONS_SETTING, value)
        )

        # Volatility-adjusted sizing (§31): tilt capital toward steadier names (bounded inverse-vol)
        # on top of conviction, targeting more equal risk per position. Off by default; backtest the
        # "conviction + vol-adj" leg first to see whether it lifts Sharpe on your basket.
        self.vol_adjust_check = QCheckBox("Volatility-adjusted sizing (steadier names get more)")
        self.vol_adjust_check.setObjectName("aiResearchToggle")  # reuse the HUD toggle styling
        self.vol_adjust_check.setChecked(bool(self.settings.get(INVEST_VOL_ADJUST_SETTING, False)))
        self.vol_adjust_check.setToolTip(
            "Tilts position sizes by a bounded inverse-volatility factor on top of conviction, so "
            "steadier names get a bit more and jumpy names less - aiming for more equal risk per "
            "position (often higher Sharpe / lower drawdown). Conviction stays the main driver. "
            "Backtest the 'conviction + vol-adj' leg first."
        )
        self.vol_adjust_check.toggled.connect(
            lambda checked: self.settings.set(INVEST_VOL_ADJUST_SETTING, bool(checked))
        )

        # Factor overlay (§33): blend a deterministic composite factor (momentum + quality + low-vol)
        # over the LLM's call — a 'buy' the numbers strongly contradict is tempered to a watch, a
        # strong-factor buy is confidence-bumped. The LLM proposes, the numbers check. Off by default.
        self.factor_overlay_check = QCheckBox("Factor overlay (let the numbers check the LLM's buys)")
        self.factor_overlay_check.setObjectName("aiResearchToggle")
        self.factor_overlay_check.setChecked(bool(self.settings.get(INVEST_FACTOR_OVERLAY_SETTING, False)))
        self.factor_overlay_check.setToolTip(
            "Blends a deterministic factor score - momentum, SEC quality, and low volatility - with "
            "Claude's rating. A buy the numbers strongly contradict is downgraded to a watch; a "
            "strong-factor buy gets a confidence bump. Makes the decision quant + LLM, not LLM-alone. "
            "Off by default; A/B the 'conviction + factor-overlay' leg in the backtest first."
        )
        self.factor_overlay_check.toggled.connect(
            lambda checked: self.settings.set(INVEST_FACTOR_OVERLAY_SETTING, bool(checked))
        )

        # Adversarial pick-checking (§34): stress-test the top buy candidates with a bull case + a
        # forced bear case + an impartial judge; a buy that doesn't survive is downgraded. One extra
        # Claude call per checked name (bounded), so it's opt-in (off by default).
        self.adversarial_check = QCheckBox("Bull-vs-bear check on top buys (extra Claude cost)")
        self.adversarial_check.setObjectName("aiResearchToggle")
        self.adversarial_check.setChecked(bool(self.settings.get(INVEST_ADVERSARIAL_SETTING, False)))
        self.adversarial_check.setToolTip(
            "Before committing, HELIX re-examines its highest-conviction buys by arguing the bull case, "
            "then a forced bear case to refute the buy, then judging impartially - downgrading any buy "
            "that doesn't survive. Catches fragile picks. Costs one extra Claude call per checked name "
            "(top ~12), so it's off by default; its payoff shows in the scorecard over time."
        )
        self.adversarial_check.toggled.connect(
            lambda checked: self.settings.set(INVEST_ADVERSARIAL_SETTING, bool(checked))
        )

        # Fundamentals input (§32): feed real SEC financials (growth, margins, ROE, leverage) into the
        # rating prompt so picks weigh the numbers, not just price/news. Free, keyless (SEC EDGAR),
        # refreshed ~monthly. On by default — it's better input, not a risk knob.
        self.fundamentals_check = QCheckBox("Use SEC fundamentals in research (free, no key)")
        self.fundamentals_check.setObjectName("aiResearchToggle")
        self.fundamentals_check.setChecked(bool(self.settings.get(INVEST_FUNDAMENTALS_SETTING, True)))
        self.fundamentals_check.setToolTip(
            "Pulls real fundamentals - revenue growth, profit margins, ROE, leverage - free from SEC "
            "EDGAR filings into the stock-rating prompt, so HELIX weighs the numbers, not just price "
            "and news. Refreshed about monthly. On by default; its payoff shows in the scorecard over time."
        )
        self.fundamentals_check.toggled.connect(
            lambda checked: self.settings.set(INVEST_FUNDAMENTALS_SETTING, bool(checked))
        )
        self.fundamentals_days = self._day_spin(
            INVEST_FUNDAMENTALS_DAYS_SETTING, DEFAULT_FUNDAMENTALS_DAYS, 7, 365
        )

        # Risk controls (§35) — protective, default ON. They don't chase return; they stop one sector,
        # one crash, or one blown-up stock from sinking the account.
        self.sector_cap_check = self._risk_toggle(
            "Sector cap (max ~25% in any one sector)", INVEST_SECTOR_CAP_SETTING,
            "Trims the core so no single sector (e.g. tech) quietly becomes a giant bet. ~25% cap; "
            "well-known large-caps are mapped to sectors, others are left uncapped.",
        )
        self.drawdown_brake_check = self._risk_toggle(
            "Drawdown brake (raise cash if down 15% from peak)", INVEST_DRAWDOWN_BRAKE_SETTING,
            "If the account falls 15% below its high-water mark, automatically hold ~40% cash and "
            "deploy less - to stop the bleeding in a bad stretch. May lag a sharp recovery.",
        )
        self.regime_check = self._risk_toggle(
            "Regime filter (defensive when the market is falling)", INVEST_REGIME_SETTING,
            "When the S&P 500 is below its long-term trend (risk-off), HELIX holds more cash and buys "
            "less. Bull-market tactics fail in a downtrend. May lag the first leg of a rebound.",
        )
        self.stop_loss_check = self._risk_toggle(
            "Per-stock stop-loss (exit a core name down ~25%)", INVEST_STOP_LOSS_SETTING,
            "A deep catastrophe stop: if a core holding falls ~25%, sell it rather than keep averaging "
            "down. Caps single-name blow-ups. (Special moonshots and day-trades have their own rules.)",
        )
        self.diversify_check = self._risk_toggle(
            "Diversification floor (hold at least ~20 names)", INVEST_DIVERSIFY_SETTING,
            "Never let concentration shrink the core below ~20 names, so a few bad picks can't sink "
            "you. Only bites when the Max-core-positions concentration cap is on.",
        )

        # How many tokens each Claude research pass may generate (Settings -> "Research effort").
        # A ceiling, not a target: higher just gives room for longer rationales / a bigger universe
        # without the JSON getting cut off (the §10 silent-truncation bug) — it doesn't force spend.
        self.research_tokens = NoScrollComboBox()  # wheel must never bump the token budget
        for _label, _tokens in RESEARCH_EFFORT_LEVELS:
            self.research_tokens.addItem(_label, _tokens)
        _eff_idx = self.research_tokens.findData(research_max_tokens(self.settings))
        self.research_tokens.setCurrentIndex(_eff_idx if _eff_idx >= 0 else 0)
        self.research_tokens.setToolTip(
            "How many tokens each Claude research pass may generate. It's a ceiling, not a target: "
            "raising it gives room for longer rationales and a bigger stock universe without the "
            "response being cut off — it doesn't by itself force more spend. Standard (8K) already "
            "fits the ~100-name core; raise it if you expand the universe or want fuller write-ups."
        )
        self.research_tokens.currentIndexChanged.connect(
            lambda: self.settings.set(INVEST_RESEARCH_TOKENS_SETTING, self.research_tokens.currentData())
        )

        # The trade universe is auto-managed (HELIX 500): seeded to the default basket and
        # self-curated by maybe_rotate_roster — no manual add/remove. Just shown as a count.
        self.stock_symbols = self._load_symbols()
        self.universe_label = QLabel()
        self.universe_label.setStyleSheet("color: #6fb3c0;")

        top_bar = QHBoxLayout()
        top_bar.setSpacing(12)
        self.start_button = QPushButton("START")
        self.start_button.setMinimumHeight(44)
        self.start_button.setMinimumWidth(120)
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("STOP")
        self.stop_button.setMinimumHeight(44)
        self.stop_button.setMinimumWidth(120)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        self.schedule_button = QPushButton("Market schedule")
        self.schedule_button.setToolTip("When the market is open — in ET and your local time, plus upcoming trading days.")
        self.schedule_button.clicked.connect(self.show_market_schedule)
        self.settings_button = QPushButton("⚙ Settings")
        self.settings_button.setToolTip("Alpaca keys, fake/real money, review interval, caps, and the Special Stocks sleeve.")
        self.settings_button.clicked.connect(self.show_settings)
        top_bar.addWidget(self.market_label)
        top_bar.addWidget(self.schedule_button)
        top_bar.addWidget(self.settings_button)
        top_bar.addStretch(1)
        top_bar.addWidget(self.start_button)
        top_bar.addWidget(self.stop_button)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setMinimumHeight(16)
        self.progress.setMaximumHeight(16)
        self.progress.setVisible(False)

        self.status = QLabel()
        self.status.setWordWrap(True)
        # Live research-activity messages from the worker thread land on the status line.
        self.research_step.connect(self.status.setText)
        # A research call that parsed to nothing (§10) is no longer silent — surface + persist it.
        self.research_issue.connect(self._on_research_issue)

        owned_box = QGroupBox("Assets")
        owned_layout = QVBoxLayout(owned_box)
        owned_layout.setSpacing(8)
        self.core_label = QLabel()
        self.core_label.setStyleSheet("color:#1dd8ff; font-weight:700;")
        self.special_heading = QLabel()
        self.special_heading.setStyleSheet("color:#ffc857; font-weight:700;")
        self.daytrade_heading = QLabel()
        self.daytrade_heading.setStyleSheet("color:#ff8c42; font-weight:700;")
        # Keep the core / special / day-trade split labels in sync with the % controls, live.
        self.special_position.valueChanged.connect(lambda _v: self._update_sleeve_labels())
        self.daytrade_position.valueChanged.connect(lambda _v: self._update_sleeve_labels())
        self.index_position.valueChanged.connect(lambda _v: self._update_sleeve_labels())
        self._update_sleeve_labels()
        self.positions_table = self._make_positions_table()
        self.positions_table.setMinimumHeight(240)
        self.special_table = self._make_positions_table()
        self.special_table.setMinimumHeight(130)
        self.daytrade_table = self._make_positions_table()
        self.daytrade_table.setMinimumHeight(110)
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(lambda: self.refresh_portfolio())
        research_button = QPushButton("Research log")
        research_button.clicked.connect(self.show_research_log)
        # Prediction scorecard (§28): the honest "are the picks any good?" report — forward returns
        # by rating confidence, vs the S&P. Sits beside the Research log.
        scorecard_button = QPushButton("Prediction scorecard")
        scorecard_button.clicked.connect(self.show_scorecard)
        # Refresh research now: force a full AI research pass on demand (re-rate the core, scout
        # Special + Day-trade, review the roster), regardless of cadence — without placing any trades.
        self.refresh_research_button = QPushButton("Refresh research now")
        self.refresh_research_button.setToolTip(
            "Run HELIX's AI research immediately - re-rate the core stocks, scout new Special and "
            "Day-trade picks, and review the universe for swaps - using Claude, without trading. "
            "Updates the Research log and the picks. Costs a few Claude calls."
        )
        self.refresh_research_button.clicked.connect(self.refresh_research_now)
        # Research log sits at the TOP of the Assets section for quick access to what HELIX researched.
        research_row = QHBoxLayout()
        research_row.addStretch(1)
        research_row.addWidget(self.refresh_research_button)
        research_row.addWidget(scorecard_button)
        research_row.addWidget(research_button)
        owned_layout.addLayout(research_row)
        owned_layout.addWidget(self.core_label)
        owned_layout.addWidget(self.positions_table, 2)
        owned_layout.addWidget(self.special_heading)
        owned_layout.addWidget(self.special_table, 1)
        owned_layout.addWidget(self.daytrade_heading)
        owned_layout.addWidget(self.daytrade_table, 1)
        buttons_row = QHBoxLayout()
        buttons_row.addStretch(1)
        buttons_row.addWidget(refresh_button)
        owned_layout.addLayout(buttons_row)

        self.cost_label = QLabel()
        self.cost_label.setWordWrap(True)

        self.research_eta_label = QLabel()  # countdown to the next AI research check (when on)
        self.research_eta_label.setWordWrap(True)
        self.research_eta_label.setStyleSheet("color:#6fb3c0;")

        # Surfaces a research parse-failure (§10) so "researched but nothing saved" is never silent.
        self.research_issue_label = QLabel()
        self.research_issue_label.setWordWrap(True)
        self.research_issue_label.setObjectName("researchIssue")
        self.research_issue_label.setStyleSheet("color:#ff6b6b; font-weight:600;")
        self.research_issue_label.setVisible(bool(self.settings.get(INVEST_LAST_RESEARCH_ISSUE_SETTING)))
        if self.research_issue_label.isVisible():
            self.research_issue_label.setText(f"⚠ {self.settings.get(INVEST_LAST_RESEARCH_ISSUE_SETTING)}")

        self.auto_timer = QTimer(self)
        self.auto_timer.setSingleShot(True)  # each cycle reschedules the next (market-aware)
        self.auto_timer.timeout.connect(self._auto_tick)
        self._running = False

        self._market_busy = False
        self.market_timer = QTimer(self)
        self.market_timer.timeout.connect(self.refresh_market_status)
        self.market_timer.start(60000)  # keep the open/closed light live (free clock call)

        # Keep the balance + chart live while the app is open: a quiet portfolio refresh every 60s.
        self.portfolio_timer = QTimer(self)
        self.portfolio_timer.timeout.connect(self._auto_refresh_portfolio)
        self.portfolio_timer.start(60000)

        # Always-on (§39): if we were auto-investing before a restart/relaunch, resume it (paper only).
        QTimer.singleShot(1200, self._maybe_resume_auto)

        chart_header = QHBoxLayout()
        chart_header.addStretch(1)
        chart_header.addWidget(QLabel("Range"))
        chart_header.addWidget(self.chart_range)

        self._build_settings_dialog()

        layout.addLayout(top_bar)
        layout.addWidget(self.balance_label)
        layout.addWidget(self.balance_sub)
        layout.addLayout(chart_header)
        layout.addWidget(self.equity_chart)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addWidget(owned_box, 1)
        layout.addWidget(self.research_eta_label)
        layout.addWidget(self.research_issue_label)
        layout.addWidget(self.cost_label)

        self.refresh()

    def _on_ai_research_toggled(self, checked: bool) -> None:
        self.settings.set(INVEST_AI_RESEARCH_SETTING, checked)
        self._update_research_eta()

    def _on_research_issue(self, message: str) -> None:
        """Surface a research call that parsed to nothing (§10) — runs on the main thread (queued via
        the research_issue signal). Persists the full raw diagnostic (journal + setting) and shows a
        short red warning so 'researched but nothing saved' can never be silent again."""
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        short = message.split("Raw response:")[0].strip()  # drop the raw dump from the on-screen line
        self.settings.set(INVEST_LAST_RESEARCH_ISSUE_SETTING, f"{stamp} - {short}")
        try:
            self.memory.add_journal_entry("research_error", "Research parse failure", f"{stamp}\n{message}")
        except Exception:
            pass
        self.research_issue_label.setText(f"⚠ {stamp} - {short}")
        self.research_issue_label.setVisible(True)

    def _update_research_eta(self) -> None:
        """Show the countdown to the next AI research check (special scout + core ratings)."""
        if not hasattr(self, "research_eta_label"):
            return
        if not bool(self.settings.get(INVEST_AI_RESEARCH_SETTING, True)):
            self.research_eta_label.setText(
                "AI research is paused — trading off saved research, no new Claude cost."
            )
            return
        special_days = int(self.settings.get(INVEST_SPECIAL_DAYS_SETTING, DEFAULT_SPECIAL_RESEARCH_DAYS) or DEFAULT_SPECIAL_RESEARCH_DAYS)
        special_next = self._next_due_from_date(
            self.settings.get(LAST_SPECIAL_RESEARCH_SETTING, ""), special_days
        )
        bits = [f"special scout {self._fmt_until(special_next)}"]
        core_next = self._core_next_due()
        if core_next is not None:
            bits.append(f"core ratings {self._fmt_until(core_next)}")
        tail = "" if getattr(self, "_running", False) else "  (begins when you press START)"
        self.research_eta_label.setText("Next AI research — " + "  ·  ".join(bits) + f".{tail}")

    @staticmethod
    def _fmt_until(target) -> str:
        secs = (target - datetime.now()).total_seconds()
        if secs <= 60:
            return "due now"
        days, rem = divmod(int(secs), 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        if days >= 1:
            return f"in {days}d {hours}h"
        if hours >= 1:
            return f"in {hours}h {mins}m"
        return f"in {mins}m"

    @staticmethod
    def _next_due_from_date(last_date, days) -> datetime:
        stamp = str(last_date or "")[:10]
        if not stamp:
            return datetime.now()  # never run -> due now
        try:
            return datetime.strptime(stamp, "%Y-%m-%d") + timedelta(days=int(days))
        except (ValueError, TypeError):
            return datetime.now()

    def _core_next_due(self):
        """The core ratings cache goes stale when its OLDEST rating passes the max age (~weekly)."""
        try:
            rows = self.memory.list_stock_rationale()
        except Exception:
            return None
        stamps = []
        for row in rows:
            if row.get("action") in ("buy", "watch", "skip"):
                try:
                    stamps.append(datetime.fromisoformat(str(row.get("updated_at", ""))[:19]))
                except (ValueError, TypeError):
                    pass
        if not stamps:
            return datetime.now()  # never rated -> due now
        core_days = int(self.settings.get(INVEST_CORE_RATING_DAYS_SETTING, DEFAULT_RATING_MAX_AGE_DAYS) or DEFAULT_RATING_MAX_AGE_DAYS)
        return min(stamps) + timedelta(days=core_days)

    def _apply_core_satellite_default(self) -> None:
        """One-time (§42): move an existing account to the core-satellite mix — an index core plus
        LIGHTER speculative sleeves — instead of the old all-equity speculative weights. Sets the mix
        once (flag-guarded) so the account de-risks on upgrade; fully adjustable in Settings afterward."""
        if self.settings.get(INVEST_CORE_SATELLITE_APPLIED_SETTING):
            return
        self.settings.set(INVEST_INDEX_ALLOCATION_SETTING, DEFAULT_INDEX_ALLOCATION_PCT * 100)
        self.settings.set(INVEST_SPECIAL_ALLOCATION_SETTING, DEFAULT_SPECIAL_ALLOCATION_PCT * 100)
        self.settings.set(INVEST_DAYTRADE_ALLOCATION_SETTING, DEFAULT_DAYTRADE_ALLOCATION_PCT * 100)
        self.settings.set(INVEST_INDEX_SYMBOL_SETTING, DEFAULT_INDEX_SYMBOL)
        self.settings.set(INVEST_CORE_SATELLITE_APPLIED_SETTING, "1")

    def _update_sleeve_labels(self) -> None:
        """Keep the Assets headings (Index / Core / Special / Day-trade split) in sync with the % controls."""
        special = self.special_position.value()
        daytrade = self.daytrade_position.value()
        index = self.index_position.value()
        core = max(0.0, 100.0 - special - daytrade - index)
        self.core_label.setText(f"Core — HELIX 500 (~{core:.0f}%, + index {index:.0f}%)")
        self.special_heading.setText(f"Special Stocks — higher-risk sleeve (the ~{special:.0f}%)")
        self.daytrade_heading.setText(f"Day-trade — short-term momentum (the ~{daytrade:.0f}%)")

    def _risk_toggle(self, text: str, setting_key: str, tip: str) -> QCheckBox:
        """A protective risk-control checkbox bound to a setting (§35), default ON."""
        box = QCheckBox(text)
        box.setObjectName("aiResearchToggle")
        box.setChecked(bool(self.settings.get(setting_key, True)))
        box.setToolTip(tip)
        box.toggled.connect(lambda checked, key=setting_key: self.settings.set(key, bool(checked)))
        return box

    def _day_spin(self, setting_key: str, default, low: int, high: int) -> QSpinBox:
        """A 'N days' spin box bound to a setting — used for the Claude research cadences."""
        box = QSpinBox()
        box.setMinimum(low)
        box.setMaximum(high)
        box.setSuffix(" days")
        try:
            box.setValue(int(self.settings.get(setting_key, default) or default))
        except (TypeError, ValueError):
            box.setValue(int(default))
        box.valueChanged.connect(
            lambda value: (self.settings.set(setting_key, value), self._update_research_eta())
        )
        return box

    def _make_positions_table(self) -> QTableWidget:
        """A holdings table: Symbol/Qty/Value/Gains/Gains%/Details — shared by the Core + Special tables."""
        table = QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(["Symbol", "Qty", "Value", "Gains", "Gains %", ""])
        head = table.horizontalHeader()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4, 5):
            head.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(40)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return table

    def _build_settings_dialog(self) -> None:
        """House the (formerly inline) Setup form in a dialog opened by the Settings button. The form is
        in a scroll area so the growing settings list always fits any window, with Close pinned below."""
        dialog = QDialog(self)
        dialog.setWindowTitle("HELIX — Investment Settings")
        outer = QVBoxLayout(dialog)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        body = QVBoxLayout(content)
        intro = QLabel(
            "You really only need your Alpaca keys, Fake vs Real money, and START. Everything else is "
            "advanced - tap \"Show advanced settings\" if you want it (sensible defaults; hover any item "
            "for what it does). Run the AI research anytime with \"Refresh research now\" on the main screen."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#6fb3c0; padding-bottom:6px;")
        body.addWidget(intro)
        def _header(text: str) -> QLabel:
            label = QLabel(text)
            label.setStyleSheet("color:#1dd8ff; font-weight:700; padding-top:6px;")
            return label

        # --- Essentials: the only things most users ever touch. ---
        essentials = QGridLayout()
        essentials.setHorizontalSpacing(16)
        essentials.setVerticalSpacing(12)
        essentials.addWidget(QLabel("Alpaca API key"), 0, 0)
        essentials.addWidget(self.api_key, 0, 1)
        essentials.addWidget(QLabel("Alpaca secret"), 1, 0)
        essentials.addWidget(self.secret_key, 1, 1)
        essentials.addWidget(self.save_keys_button, 1, 2)
        essentials.addWidget(QLabel("Fake or real money"), 2, 0)
        essentials.addWidget(self.mode, 2, 1)
        essentials.addWidget(QLabel("Review every"), 3, 0)
        essentials.addWidget(self.interval, 3, 1)
        essentials.addWidget(QLabel("Universe"), 4, 0)
        essentials.addWidget(self.universe_label, 4, 1)
        body.addLayout(essentials)

        # --- Advanced: hidden by default; HELIX runs these on sensible defaults under the hood. ---
        self.advanced_toggle = QCheckBox("Show advanced settings")
        self.advanced_toggle.setToolTip(
            "Sleeve splits, AI-research cadence/cost, concentration, fundamentals and risk controls. "
            "All have sensible defaults - you can safely ignore them."
        )
        body.addWidget(self.advanced_toggle)
        self.advanced_box = QWidget()
        adv = QGridLayout(self.advanced_box)
        adv.setHorizontalSpacing(16)
        adv.setVerticalSpacing(12)
        adv.setContentsMargins(0, 0, 0, 0)
        adv.addWidget(_header("Sleeves — how the money is split:"), 0, 0, 1, 3)
        adv.addWidget(QLabel("Index core % (VOO)"), 1, 0); adv.addWidget(self.index_position, 1, 1)
        adv.addWidget(QLabel("Special stocks %"), 2, 0); adv.addWidget(self.special_position, 2, 1)
        adv.addWidget(QLabel("Day-trade %"), 3, 0); adv.addWidget(self.daytrade_position, 3, 1)
        adv.addWidget(QLabel("Special funding"), 4, 0); adv.addWidget(self.special_funding, 4, 1)
        adv.addWidget(_header("AI research — cost & depth:"), 5, 0, 1, 3)
        adv.addWidget(self.ai_research_check, 6, 0, 1, 3)
        adv.addWidget(QLabel("Research effort"), 7, 0); adv.addWidget(self.research_tokens, 7, 1, 1, 2)
        adv.addWidget(_header("Research cadence — how often Claude re-runs (uses tokens):"), 8, 0, 1, 3)
        adv.addWidget(QLabel("Re-rate core stocks"), 9, 0); adv.addWidget(self.core_days, 9, 1)
        adv.addWidget(QLabel("Scout special stocks"), 10, 0); adv.addWidget(self.special_days, 10, 1)
        adv.addWidget(QLabel("Scout day-trade stocks"), 11, 0); adv.addWidget(self.daytrade_days, 11, 1)
        adv.addWidget(QLabel("Review the roster"), 12, 0); adv.addWidget(self.roster_days, 12, 1)
        adv.addWidget(_header("Concentration — how many ideas to hold (§30):"), 13, 0, 1, 3)
        adv.addWidget(QLabel("Max core positions"), 14, 0); adv.addWidget(self.max_positions, 14, 1)
        adv.addWidget(self.vol_adjust_check, 15, 0, 1, 3)
        adv.addWidget(self.factor_overlay_check, 16, 0, 1, 3)
        adv.addWidget(self.adversarial_check, 17, 0, 1, 3)
        adv.addWidget(_header("Fundamentals — weigh the numbers, not just price/news (§32):"), 18, 0, 1, 3)
        adv.addWidget(self.fundamentals_check, 19, 0, 1, 3)
        adv.addWidget(QLabel("Refresh fundamentals"), 20, 0); adv.addWidget(self.fundamentals_days, 20, 1)
        adv.addWidget(_header("Risk controls — keep one bet/crash/blow-up from sinking you (§35):"), 21, 0, 1, 3)
        adv.addWidget(self.sector_cap_check, 22, 0, 1, 3)
        adv.addWidget(self.drawdown_brake_check, 23, 0, 1, 3)
        adv.addWidget(self.regime_check, 24, 0, 1, 3)
        adv.addWidget(self.stop_loss_check, 25, 0, 1, 3)
        adv.addWidget(self.diversify_check, 26, 0, 1, 3)
        note = QLabel(
            "Settings save as you change them. Index core — a VOO position so the book tracks the market; "
            "the AI sleeves are satellites trying to beat it. Special funding — House money only buys "
            "speculative names from profit above your starting balance; Always invests the full % (riskier)."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#6fb3c0;")
        adv.addWidget(note, 27, 0, 1, 3)
        self.advanced_box.setVisible(False)
        self.advanced_toggle.toggled.connect(self.advanced_box.setVisible)
        body.addWidget(self.advanced_box)
        body.addStretch(1)
        outer.addWidget(scroll, 1)  # the settings list scrolls...
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        outer.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)  # ...Close stays pinned at the bottom
        dialog.resize(560, 400)  # compact when collapsed; the scroll area scrolls when advanced is shown
        dialog.setMinimumSize(420, 300)
        self._settings_dialog = dialog

    def show_settings(self) -> None:
        self._update_universe_label()
        self.api_key.setPlaceholderText(self._key_placeholder(ALPACA_API_KEY_SETTING))
        self.secret_key.setPlaceholderText(self._key_placeholder(ALPACA_SECRET_KEY_SETTING))
        self._settings_dialog.exec()

    def refresh(self) -> None:
        self.mode.setCurrentText(self._mode_from_settings())
        self.api_key.setPlaceholderText(self._key_placeholder(ALPACA_API_KEY_SETTING))
        self.secret_key.setPlaceholderText(self._key_placeholder(ALPACA_SECRET_KEY_SETTING))
        self._update_universe_label()
        self.update_status()
        self.refresh_market_status()
        self.refresh_portfolio()

    def _key_placeholder(self, setting_key: str) -> str:
        return "Saved" if self.settings.get(setting_key) else "Paste once, then Save Keys"

    def _mode_from_settings(self) -> str:
        environment = self.settings.get(ALPACA_ENVIRONMENT_SETTING, ALPACA_ENV_PAPER)
        return INVEST_MODE_REAL if environment == ALPACA_ENV_LIVE else INVEST_MODE_PRACTICE

    def is_real(self) -> bool:
        return self.mode.currentText() == INVEST_MODE_REAL

    def on_mode_changed(self, text: str) -> None:
        if text == INVEST_MODE_REAL:
            confirm = QMessageBox.warning(
                self,
                "HELIX - Real Money",
                "Real mode places LIVE orders with REAL money and can lose money.\n\nSwitch to real money?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                self.mode.setCurrentText(INVEST_MODE_PRACTICE)
                return
            self.settings.set(ALPACA_ENVIRONMENT_SETTING, ALPACA_ENV_LIVE)
        else:
            self.settings.set(ALPACA_ENVIRONMENT_SETTING, ALPACA_ENV_PAPER)
        self.update_status()

    def update_status(self) -> None:
        running = getattr(self, "_running", False)
        state = f"RUNNING - reviewing every {self.interval.currentText()}" if running else "Stopped"
        self.status.setText(f"{state}   |   {self.mode.currentText()}")

    def _busy(self, on: bool) -> None:
        self._busy_count = max(0, self._busy_count + (1 if on else -1))
        self.progress.setVisible(self._busy_count > 0)

    def save_keys(self) -> None:
        api = self.api_key.text().strip()
        secret = self.secret_key.text().strip()
        if api:
            self.settings.set(ALPACA_API_KEY_SETTING, api)
        if secret:
            self.settings.set(ALPACA_SECRET_KEY_SETTING, secret)
        self.api_key.clear()
        self.secret_key.clear()
        self.api_key.setPlaceholderText(self._key_placeholder(ALPACA_API_KEY_SETTING))
        self.secret_key.setPlaceholderText(self._key_placeholder(ALPACA_SECRET_KEY_SETTING))
        QMessageBox.information(self, "HELIX", "Alpaca keys saved.")
        self.refresh_portfolio()

    def _watchlist_from_tickers(self) -> list:
        return [
            {"symbol": symbol, "thesis": "user pick", "max_allocation_pct": None}
            for symbol in self.stock_symbols
        ]

    def _load_symbols(self) -> list:
        raw = self.settings.get(INVEST_TICKERS_SETTING, DEFAULT_TICKERS)
        out = []
        for token in str(raw).replace(";", ",").split(","):
            symbol = token.strip().upper()
            if symbol and symbol not in out:
                out.append(symbol)
        return out

    def _save_symbols(self) -> None:
        self.settings.set(INVEST_TICKERS_SETTING, ", ".join(self.stock_symbols))

    def _update_universe_label(self) -> None:
        self.universe_label.setText(f"{len(self.stock_symbols)} stocks · auto-managed (HELIX 500)")

    def build_research_fn(self, universe: list):
        model = DEFAULT_RESEARCH_MODEL
        client = ClaudeClient(ClaudeConfig(model=model, timeout_seconds=RESEARCH_TIMEOUT_SECONDS))

        def research_fn(prompt: str) -> str:
            text = client.complete(prompt, max_tokens=research_max_tokens(self.settings))  # Settings -> Research effort
            usage = client.last_usage or {}
            input_tokens = int(usage.get("input_tokens", 0) or 0)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            self.memory.record_ai_usage(
                model, input_tokens, output_tokens, estimate_cost(model, input_tokens, output_tokens)
            )
            return text

        return research_fn

    def _fetch_market_context(self, client, symbols: list) -> str:
        """Live price action (weekly bars, ~1y) + recent news for the universe, digested for the AI
        rating prompt (§25). Best-effort — returns '' if market data is unavailable. Only called when
        a re-rate actually fires (lazy), so most cycles make no extra data calls."""
        try:
            start = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
            bars = client.get_bars_multi(symbols, timeframe="1Week", start=start)
        except Exception:
            bars = {}
        try:
            news = client.get_news(symbols, limit=50)
        except Exception:
            news = []
        fundamentals_text = ""  # §32: real SEC fundamentals for this chunk, read from the local cache
        if self.settings.get(INVEST_FUNDAMENTALS_SETTING, True):
            try:
                fundamentals_text = fundamentals_block(self.memory.get_fundamentals(symbols), symbols)
            except Exception:
                fundamentals_text = ""
        try:
            return build_market_context(bars, news, symbols, fundamentals_text=fundamentals_text)
        except Exception:
            return ""

    def _maybe_refresh_fundamentals(self, symbols: list) -> None:
        """Refresh the SEC fundamentals cache (§32) on a monthly cadence (settings-gated timestamp),
        off-thread and best-effort. One bulk frames pull covers the whole universe; the weekly re-rate
        then reads it locally. No-op when disabled or still fresh; a network failure is surfaced (not
        fatal) and leaves the timestamp unset so it retries next cycle."""
        if not self.settings.get(INVEST_FUNDAMENTALS_SETTING, True):
            return
        days = int(self.settings.get(INVEST_FUNDAMENTALS_DAYS_SETTING, DEFAULT_FUNDAMENTALS_DAYS) or DEFAULT_FUNDAMENTALS_DAYS)
        last = self.settings.get(LAST_FUNDAMENTALS_FETCH_SETTING, "")
        if last:
            try:
                if (datetime.now() - datetime.strptime(str(last)[:10], "%Y-%m-%d")).days < days:
                    return
            except ValueError:
                pass
        try:
            self.research_step.emit("Pulling fundamentals from SEC filings...")
            # Fetch for the whole tradeable universe when known (§36/§37) — same bulk frames call, just
            # extract more — so quality screening + the factor overlay cover discovered names too, not
            # only the roster. Falls back to the roster before the market list is first cached.
            universe = self.memory.get_tradable_universe()
            target = sorted(universe) if universe else list(symbols)
            metrics = fetch_fundamentals(target)
            if metrics:
                self.memory.upsert_fundamentals(metrics)
                self.settings.set(LAST_FUNDAMENTALS_FETCH_SETTING, datetime.now().strftime("%Y-%m-%d"))
        except Exception as exc:
            self.research_issue.emit(f"Fundamentals fetch failed (continuing without): {exc}")

    def _maybe_refresh_sectors(self, symbols: list) -> None:
        """SEC sector enrichment (§35): for names the curated map misses, look up their SIC code from
        SEC EDGAR and cache the derived sector, so the sector cap covers ~the whole universe instead of
        only the hand-mapped names. Sectors are ~static, so this runs on a long cadence, bounded per run
        (fills over a few cycles), off-thread and best-effort. No-op when the sector cap is off, or once
        every name is resolved and the cache is fresh."""
        if not self.settings.get(INVEST_SECTOR_CAP_SETTING, True):
            return
        last = self.settings.get(LAST_SECTORS_FETCH_SETTING, "")
        if last:
            try:
                if (datetime.now() - datetime.strptime(str(last)[:10], "%Y-%m-%d")).days < DEFAULT_SECTORS_DAYS:
                    return
            except ValueError:
                pass
        cached = self.memory.get_sectors(symbols)
        need = [s for s in symbols if sector_of(s) is None and str(s).strip().upper() not in cached]
        if not need:  # everything is curated-mapped or already cached -> mark fresh, done
            self.settings.set(LAST_SECTORS_FETCH_SETTING, datetime.now().strftime("%Y-%m-%d"))
            return
        try:
            self.research_step.emit("Looking up sectors from SEC filings...")
            found = fetch_sectors(need[:SECTORS_FETCH_LIMIT])
            if found:
                self.memory.upsert_sectors(found)
            # Stamp only once the whole universe is resolved, so a bounded partial run keeps filling next cycle.
            done = self.memory.get_sectors(symbols)
            if not any(sector_of(s) is None and str(s).strip().upper() not in done for s in symbols):
                self.settings.set(LAST_SECTORS_FETCH_SETTING, datetime.now().strftime("%Y-%m-%d"))
        except Exception as exc:
            self.research_issue.emit(f"Sector lookup failed (continuing): {exc}")

    def _maybe_refresh_market_assets(self, client) -> None:
        """Refresh the real-market universe (§36): cache Alpaca's tradeable asset list weekly, so every
        discovered name (core rotation, Special, Day-trade) can be validated against actual, buyable
        market tickers. One free Alpaca call; off-thread, best-effort. No-op when the cache is fresh."""
        last = self.settings.get(LAST_ASSETS_FETCH_SETTING, "")
        try:
            have = bool(self.memory.get_tradable_universe())
        except Exception:
            have = False
        if last and have:
            try:
                if (datetime.now() - datetime.strptime(str(last)[:10], "%Y-%m-%d")).days < DEFAULT_ASSETS_DAYS:
                    return
            except ValueError:
                pass
        try:
            self.research_step.emit("Refreshing the tradeable market list...")
            universe = tradable_assets(client.get_assets(), require_fractionable=False)  # §42: all tradable names
            if universe:
                self.memory.replace_market_assets(universe)
                self.settings.set(LAST_ASSETS_FETCH_SETTING, datetime.now().strftime("%Y-%m-%d"))
        except Exception as exc:
            self.research_issue.emit(f"Market-list refresh failed (continuing): {exc}")

    def _fetch_news_context(self, client) -> str:
        """Broad recent market news (not filtered to the universe) — grounds the moonshot scout in
        what's inflecting NOW (§25). Best-effort; news-only (no per-name technicals)."""
        try:
            news = client.get_news(None, limit=40)
        except Exception:
            news = []
        try:
            return build_market_context({}, news, [])
        except Exception:
            return ""

    def _fetch_price_signals(self, client, symbols: list) -> tuple[dict, dict]:
        """Momentum/trend factor scores (§30) + volatilities (§31) for the universe, from ONE weekly-bar
        fetch. Scores rank the top-N concentration cut; volatilities drive the inverse-vol sizing tilt.
        Best-effort; returns ({}, {}) on failure (the engine falls back to no ranking / no tilt). Only
        called when a concentration cap or vol-adjust is on, so plain cycles pay for no extra fetch."""
        try:
            start = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
            bars = client.get_bars_multi(symbols, timeframe="1Week", start=start)
            return factor_signals(bars), volatility_signals(bars)
        except Exception:
            return {}, {}

    def _discover_seed_candidates(self, client) -> list:
        """Data-breadth discovery (§40): surface candidate tickers from the BROAD tradeable market by
        market data (momentum + low-vol + liquidity), beyond what the model would name from memory.
        Scans a bounded, rotating slice each review (fills over cycles → respects rate limits + the free
        feed) and hands the top names to the roster review for the model to judge. Best-effort: returns
        [] on any failure so discovery never blocks a rotation."""
        try:
            pool = sorted(self.memory.get_tradable_universe())
            if not pool:
                return []
            exclude = {str(s).strip().upper() for s in self.stock_symbols}
            offset = int(self.settings.get(INVEST_DISCOVERY_OFFSET_SETTING, 0) or 0) % len(pool)
            window = pool[offset:offset + DISCOVERY_SCAN_LIMIT]
            if len(window) < DISCOVERY_SCAN_LIMIT and len(pool) > DISCOVERY_SCAN_LIMIT:
                window += pool[: DISCOVERY_SCAN_LIMIT - len(window)]  # wrap around the market
            self.research_step.emit(f"Screening the market for new candidates ({len(window)} names)...")
            seeds = discover_market_candidates(client, window, exclude=exclude, top_n=DISCOVERY_TOP_N)
            self.settings.set(INVEST_DISCOVERY_OFFSET_SETTING, str((offset + DISCOVERY_SCAN_LIMIT) % len(pool)))
            return seeds
        except Exception as exc:  # noqa: BLE001 — never block a rotation on the screener
            self.research_issue.emit(f"Market screener failed (using model picks): {exc}")
            return []

    def _make_screen_fn(self, client, profile: str):
        """Build a §37 quality/liquidity screen for a sleeve ('core'/'special'/'daytrade'): given
        candidate symbols, fetch their recent daily bars (liquidity) + cached SEC fundamentals
        (quality, core only) and return the subset that clears the sleeve's thresholds. Best-effort —
        a fetch failure screens on whatever data is available."""
        cfg = SCREEN_PROFILES[profile]

        def screen(symbols):
            names = [str(s).strip().upper() for s in symbols if str(s).strip()]
            if not names:
                return set()
            try:
                start = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
                bars = client.get_bars_multi(names, timeframe="1Day", start=start)
            except Exception:
                bars = {}
            funds = self.memory.get_fundamentals(names)
            return screen_candidates(
                names, bars, funds,
                min_price=cfg["min_price"], min_dollar_volume=cfg["min_dollar_volume"],
                max_debt_equity=cfg["max_debt_equity"], min_net_margin=cfg["min_net_margin"],
                check_quality=bool(cfg["check_quality"]),
            )

        return screen

    def _compute_risk_controls(self, client, symbols: list, total_equity: float) -> RiskControls:
        """Assemble the §35 risk controls from the Settings toggles + live data (high-water equity for
        the drawdown brake, SPY trend for the regime filter, the sector map). Each control is opt-out;
        a failed fetch just disables that one trigger (best-effort), never aborts the cycle."""
        s = self.settings
        sector_on = bool(s.get(INVEST_SECTOR_CAP_SETTING, True))
        sectors = None
        if sector_on:  # SEC-enriched cache (§35) for the tail + curated map (wins where both exist)
            sectors = dict(self.memory.get_sectors(symbols))
            sectors.update(sectors_for(symbols))
        equity_peak = 0.0
        if s.get(INVEST_DRAWDOWN_BRAKE_SETTING, True):
            try:
                history = [_to_float(r.get("equity")) for r in self.memory.list_equity_history(400)]
                equity_peak = max(history + [_to_float(total_equity)])
            except Exception:
                equity_peak = _to_float(total_equity)
        risk_off = False
        if s.get(INVEST_REGIME_SETTING, True):
            try:
                start = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
                spy = client.get_bars_multi(["SPY"], timeframe="1Week", start=start).get("SPY", [])
                risk_off = regime_risk_off(spy)
            except Exception:
                risk_off = False
        return RiskControls(
            sectors=sectors,
            sector_cap_pct=DEFAULT_SECTOR_CAP_PCT if sector_on else 0.0,
            equity_peak=equity_peak,
            drawdown_brake_pct=DEFAULT_DRAWDOWN_BRAKE_PCT if s.get(INVEST_DRAWDOWN_BRAKE_SETTING, True) else 0.0,
            defensive_cash_buffer_pct=DEFAULT_DEFENSIVE_CASH_BUFFER_PCT,
            risk_off=risk_off,
            core_stop_loss_pct=DEFAULT_CORE_STOP_LOSS_PCT if s.get(INVEST_STOP_LOSS_SETTING, True) else 0.0,
            min_positions=DEFAULT_MIN_POSITIONS if s.get(INVEST_DIVERSIFY_SETTING, True) else 0,
        )

    def _performance_review(self, client, holdings_pl: dict) -> str:
        """The feedback-loop digest fed to every research prompt (§17): a §38 scorecard calibration
        line (the model's own forward returns by confidence vs the S&P, daily-cached) + HELIX's
        realized record + how its current picks are doing (forward returns from live P&L) + the
        account vs the S&P 500 over ~30 days. The vs-S&P part is two free Alpaca reads; everything is
        best-effort."""
        equity_review = ""
        try:
            history = parse_portfolio_history(client.get_portfolio_history("1M", "1D"))
            points = getattr(history, "points", [])
            if len(points) >= 2 and points[0] > 0:
                account_ret = 100.0 * (points[-1] - points[0]) / points[0]
                start = (datetime.now() - timedelta(days=35)).strftime("%Y-%m-%d")
                spy = parse_stock_bars(client.get_stock_bars("SPY", "1Day", start), "SPY")
                if len(spy) >= 2 and spy[0] > 0:
                    spy_ret = 100.0 * (spy[-1] - spy[0]) / spy[0]
                    verb = "beating" if account_ret >= spy_ret else "trailing"
                    equity_review = (
                        f"Over the last ~30 days your account is {account_ret:+.1f}% vs the S&P 500 "
                        f"{spy_ret:+.1f}% - {verb} the market by {abs(account_ret - spy_ret):.1f} points."
                    )
        except Exception:
            equity_review = ""
        scorecard = ""
        try:
            scorecard = refresh_scorecard_feedback(self.memory, client, self.settings)
        except Exception:
            scorecard = ""
        return performance_digest(
            self.memory, holdings_pl=holdings_pl, equity_review=equity_review, scorecard=scorecard
        )

    def start(self) -> None:
        if not (self.settings.get(ALPACA_API_KEY_SETTING) and self.settings.get(ALPACA_SECRET_KEY_SETTING)):
            QMessageBox.warning(self, "HELIX", "Save your Alpaca API key and secret first.")
            return
        if not self._watchlist_from_tickers():
            QMessageBox.warning(self, "HELIX", "Enter at least one stock ticker.")
            return
        self._save_symbols()
        self.settings.set(INVEST_AUTO_INTERVAL_SETTING, self.interval.currentText())

        every = self.interval.currentText()
        if self.is_real():
            warning = (
                f"REAL-MONEY auto-investing will place LIVE orders automatically every {every} "
                "and can lose real money. Start?"
            )
        else:
            warning = f"HELIX will auto-invest with PAPER (fake) money every {every}. Start?"
        confirm = QMessageBox.warning(
            self, "HELIX - Start", warning,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.settings.remove(LAST_SPECIAL_RESEARCH_SETTING)  # force a fresh moonshot scout on every START
        self._set_running(True)
        self.update_status()
        self._auto_tick()  # immediate first cycle; _cycle_done schedules the next (market-aware)

    def stop(self) -> None:
        self.auto_timer.stop()
        self._set_running(False)
        self.update_status()

    def _maybe_resume_auto(self) -> None:
        """Always-on (§39): if auto-investing was ON when HELIX last went down, resume it on launch —
        PAPER only, no confirmation dialog (mirrors voice_start). LIVE never auto-resumes; the
        real-money gate stays manual. This is what makes the crash supervisor actually resume trading
        (the soft crash guard already preserves state by keeping the process alive)."""
        if self._running or not self.settings.get(INVEST_AUTO_RUNNING_SETTING):
            return
        if self.is_real():
            self.status.setText("Auto-investing was ON (LIVE) before restart — press START to resume.")
            _LOG.info("not auto-resuming LIVE trading on launch; manual START required")
            return
        if not (self.settings.get(ALPACA_API_KEY_SETTING) and self.settings.get(ALPACA_SECRET_KEY_SETTING)):
            return
        if not self.stock_symbols:
            return
        _LOG.info("resuming paper auto-investing after restart")
        self._set_running(True)
        self.update_status()
        self._auto_tick()

    def voice_start(self) -> bool:
        """Start auto-investing WITHOUT the GUI confirmation dialog. The Xpert voice layer applies
        its own spoken confirmation gate (live real-money needs an explicit 'yes'), so a second
        modal here would just block the conversation. Returns False if Alpaca keys aren't saved."""
        if not (self.settings.get(ALPACA_API_KEY_SETTING) and self.settings.get(ALPACA_SECRET_KEY_SETTING)):
            return False
        if self._running:
            return True
        self._save_symbols()
        self.settings.set(INVEST_AUTO_INTERVAL_SETTING, self.interval.currentText())
        self.settings.remove(LAST_SPECIAL_RESEARCH_SETTING)  # fresh moonshot scout on start (mirrors START)
        self._set_running(True)
        self.update_status()
        self._auto_tick()  # immediate first cycle; _cycle_done schedules the next (market-aware)
        return True

    def voice_stop(self) -> None:
        self.stop()

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        # Persist so the always-on supervisor can resume paper trading after a hard-crash relaunch (§39).
        self.settings.set(INVEST_AUTO_RUNNING_SETTING, "1" if running else "")

    def _interval_ms(self) -> int:
        return AUTO_INTERVALS.get(self.interval.currentText(), 900000)

    def _schedule_next(self, ms: int) -> None:
        """Arm the single-shot timer for the next cycle, but only while RUNNING."""
        if self._running:
            self.auto_timer.start(int(max(1000, ms)))

    def _next_open_delay_ms(self, clock: dict) -> int:
        """ms until the next market open (Alpaca clock), capped to MARKET_CLOSED_RETRY_MS so we
        re-check at least that often (free) and land right on the open for the final hop."""
        raw = (clock or {}).get("next_open")
        if not raw:
            return MARKET_CLOSED_RETRY_MS
        try:
            open_dt = datetime.fromisoformat(str(raw))
            now = datetime.now(open_dt.tzinfo) if open_dt.tzinfo else datetime.now()
            seconds = (open_dt - now).total_seconds()
        except (ValueError, TypeError):
            return MARKET_CLOSED_RETRY_MS
        if seconds <= 0:
            return 60000
        return int(min(seconds * 1000 + 5000, MARKET_CLOSED_RETRY_MS))

    def on_interval_changed(self, text: str) -> None:
        self.settings.set(INVEST_AUTO_INTERVAL_SETTING, text)
        self.update_status()  # the new interval applies on the next scheduled cycle

    def _chart_selection(self) -> tuple:
        period, timeframe, _days = CHART_RANGES.get(
            self.chart_range.currentText(), CHART_RANGES[DEFAULT_CHART_RANGE]
        )
        return period, timeframe

    def _chart_days(self) -> int:
        return CHART_RANGES.get(self.chart_range.currentText(), CHART_RANGES[DEFAULT_CHART_RANGE])[2]

    def on_chart_range_changed(self, text: str) -> None:
        self.settings.set(INVEST_CHART_RANGE_SETTING, text)
        self.refresh_portfolio()

    def _auto_tick(self) -> None:
        if self._cycle_busy:
            return
        try:
            self._cycle_busy = True
            self.research_issue_label.setVisible(False)  # fresh per cycle; re-shown if research fails again
            symbols = list(self.stock_symbols)
            is_real = self.is_real()
            special_pct = self.special_position.value()
            daytrade_pct = self.daytrade_position.value()
            self.status.setText("RUNNING: researching and trading...")
            self._busy(True)
            _LOG.info("auto cycle starting (%d symbols, %s)", len(symbols), "real" if is_real else "paper")
            spawn_worker(
                self._workers, lambda: self._run_cycle(symbols, is_real, special_pct, daytrade_pct), self._cycle_done
            )
        except Exception:  # never let a launch error wedge the loop (busy stuck True / no reschedule)
            _LOG.exception("auto cycle failed to launch; rescheduling")
            self._cycle_busy = False
            self._busy(False)
            self._schedule_next(self._interval_ms())

    def _run_cycle(self, symbols: list, is_real: bool, special_pct: float = 0.0, daytrade_pct: float = 0.0) -> dict:
        client = AlpacaClient.from_settings(self.settings)
        clock = client.get_clock()
        # Configurable Claude-research cadences (Settings), in days.
        core_days = float(self.settings.get(INVEST_CORE_RATING_DAYS_SETTING, DEFAULT_RATING_MAX_AGE_DAYS) or DEFAULT_RATING_MAX_AGE_DAYS)
        special_days = int(self.settings.get(INVEST_SPECIAL_DAYS_SETTING, DEFAULT_SPECIAL_RESEARCH_DAYS) or DEFAULT_SPECIAL_RESEARCH_DAYS)
        daytrade_days = int(self.settings.get(INVEST_DAYTRADE_DAYS_SETTING, DEFAULT_DAYTRADE_RESEARCH_DAYS) or DEFAULT_DAYTRADE_RESEARCH_DAYS)
        roster_days = int(self.settings.get(INVEST_ROSTER_DAYS_SETTING, DEFAULT_ROSTER_REVIEW_DAYS) or DEFAULT_ROSTER_REVIEW_DAYS)
        if not clock.get("is_open", False):
            # Off-hours research, prepared for the open: refresh core (80%) ratings AND scout Special
            # (20%) picks. Both cadence-gated (~weekly/~nightly) and persisted to SQLite for the
            # Research log. Skipped entirely when "Refresh AI research" is off (cost control).
            if self.settings.get(INVEST_AI_RESEARCH_SETTING, True):
                try:
                    research_fn = self.build_research_fn([])
                    account = client.get_account()
                    equity = _to_float(account.get("equity") or account.get("portfolio_value"))
                    holdings_pl = {
                        p.get("symbol", ""): {"unrealized_pl": _to_float(p.get("unrealized_pl"))}
                        for p in client.get_positions()
                        if p.get("symbol")
                    }
                    watchlist = [{"symbol": s, "thesis": "user pick", "max_allocation_pct": None} for s in symbols]
                    track = self._performance_review(client, holdings_pl)  # feedback loop (§17)
                    self._maybe_refresh_market_assets(client)  # §36: real tradeable market list (first)
                    tradable = self.memory.get_tradable_universe() or None  # §36: validate discoveries
                    self._maybe_refresh_fundamentals(symbols)  # §32: SEC fundamentals (whole tradeable universe)
                    self.research_step.emit("Researching the core stocks for the open...")
                    maybe_refresh_core_ratings(
                        self.memory, watchlist, {}, research_fn, total_equity=equity, preset=DEFAULT_PRESET,
                        rating_max_age_days=core_days,
                        market_context_fn=lambda syms: self._fetch_market_context(client, syms),
                        on_issue=self.research_issue.emit,
                        progress_fn=self.research_step.emit,
                        performance_override=track,
                        adversarial=bool(self.settings.get(INVEST_ADVERSARIAL_SETTING, False)),  # §34
                    )
                    self.research_step.emit("Scouting new moonshot stocks...")
                    maybe_research_special(
                        self.settings, self.memory, research_fn, holdings_pl=holdings_pl,
                        research_days=special_days,
                        market_context_fn=lambda: self._fetch_news_context(client),
                        on_issue=self.research_issue.emit,
                        performance=track, tradable=tradable,
                        screen_fn=self._make_screen_fn(client, "special"),  # §37
                    )
                    self.research_step.emit("Scouting short-term momentum stocks...")
                    maybe_research_daytrade(
                        self.settings, self.memory, research_fn,
                        research_days=daytrade_days,
                        market_context_fn=lambda: self._fetch_news_context(client),
                        on_issue=self.research_issue.emit,
                        performance=track, tradable=tradable,
                        screen_fn=self._make_screen_fn(client, "daytrade"),  # §37
                    )
                except Exception as exc:
                    self.research_issue.emit(f"Off-hours research failed before the open: {exc}")
            return {"status": "market closed.", "market_closed": True, "retry_ms": self._next_open_delay_ms(clock)}
        account = client.get_account()
        positions = client.get_positions()
        holdings = {p.get("symbol", ""): _to_float(p.get("market_value")) for p in positions if p.get("symbol")}
        holdings_pl = {
            p.get("symbol", ""): {
                "market_value": _to_float(p.get("market_value")),
                "unrealized_pl": _to_float(p.get("unrealized_pl")),
                "unrealized_plpc": _to_float(p.get("unrealized_plpc")),
            }
            for p in positions
            if p.get("symbol")
        }
        if not symbols and not holdings:
            return {"status": "no stocks to trade."}
        total_equity = _to_float(account.get("equity") or account.get("portfolio_value"))
        cash = _to_float(account.get("cash"))
        research_fn = self.build_research_fn([])
        do_research = bool(self.settings.get(INVEST_AI_RESEARCH_SETTING, True))
        rotated, swaps = False, 0
        track = ""
        if do_research:
            track = self._performance_review(client, holdings_pl)  # feedback loop (§17): results -> research
            self._maybe_refresh_market_assets(client)  # §36: real tradeable market list (first, weekly)
            tradable = self.memory.get_tradable_universe() or None  # §36: validate every discovered name
            self._maybe_refresh_fundamentals(symbols)  # §32: SEC fundamentals (whole tradeable universe, monthly)
            self._maybe_refresh_sectors(symbols)  # §35: SEC sector enrichment for the cap (long cadence)
            # Self-curating universe (HELIX 500): rotate the roster if a review is due (quarterly).
            self.research_step.emit("Reviewing the stock universe...")
            symbols, rotated, swaps = maybe_rotate_roster(
                self.settings, self.memory, symbols, holdings, research_fn,
                review_days=roster_days,
                market_context_fn=lambda syms: self._fetch_market_context(client, syms),  # per-chunk (§20)
                on_issue=self.research_issue.emit,
                progress_fn=self.research_step.emit,
                tradable=tradable,
                screen_fn=self._make_screen_fn(client, "core"),  # §37: S&P-caliber liquidity + quality
                seed_fn=lambda: self._discover_seed_candidates(client),  # §40: data-breadth discovery
            )
            self.research_step.emit("Scouting new moonshot stocks...")
            try:  # scout Special Stocks (holdings_pl lets rotation protect winners), even on the open path
                maybe_research_special(
                    self.settings, self.memory, research_fn, holdings_pl=holdings_pl,
                    research_days=special_days,
                    market_context_fn=lambda: self._fetch_news_context(client),
                    on_issue=self.research_issue.emit,
                    performance=track, tradable=tradable,
                    screen_fn=self._make_screen_fn(client, "special"),  # §37
                )
            except Exception as exc:  # secondary to core trading — surface it, but don't abort the cycle
                self.research_issue.emit(f"Special scout failed: {exc}")
            self.research_step.emit("Scouting short-term momentum stocks...")
            try:  # scout the day-trade momentum sleeve (§27)
                maybe_research_daytrade(
                    self.settings, self.memory, research_fn,
                    research_days=daytrade_days,
                    market_context_fn=lambda: self._fetch_news_context(client),
                    on_issue=self.research_issue.emit,
                    performance=track, tradable=tradable,
                    screen_fn=self._make_screen_fn(client, "daytrade"),  # §37
                )
            except Exception as exc:
                self.research_issue.emit(f"Day-trade scout failed: {exc}")
        special_symbols = normalize_roster(self.settings.get(SPECIAL_SETTING, ""))
        daytrade_symbols = normalize_roster(self.settings.get(DAYTRADE_SETTING, ""))
        # Special funding (§21): "house" protects your starting balance (buys specials only from
        # profit above it — may stay empty until you're up); "always" deploys the full % from day one.
        if self.settings.get(INVEST_SPECIAL_FUNDING_SETTING, "house") == "always":
            special_principal = 0.0
        else:
            special_principal = _to_float(self.settings.get(INVEST_PRINCIPAL_SETTING, 0) or 0)
            if special_principal <= 0 and total_equity > 0:
                special_principal = total_equity
                self.settings.set(INVEST_PRINCIPAL_SETTING, special_principal)
        watchlist = [{"symbol": s, "thesis": "user pick", "max_allocation_pct": None} for s in symbols]
        self.research_step.emit("Rating stocks and planning trades..." if do_research else "Planning trades from saved research...")
        # Cost control: with research off, reuse cached ratings of ANY age so no new Claude call is made.
        rating_age = core_days if do_research else 100000.0
        # Concentration (§30) + vol-adjusted sizing (§31) + factor overlay (§33): all opt-in (default
        # off). When any is on, one weekly-bar fetch yields momentum + volatilities; the overlay also
        # blends in cached SEC quality (§32) into a composite. Otherwise no extra fetch, no change.
        max_positions = int(self.settings.get(INVEST_MAX_POSITIONS_SETTING, 0) or 0)
        vol_adjust = bool(self.settings.get(INVEST_VOL_ADJUST_SETTING, False))
        factor_overlay = bool(self.settings.get(INVEST_FACTOR_OVERLAY_SETTING, False))
        adversarial = bool(self.settings.get(INVEST_ADVERSARIAL_SETTING, False))  # §34 bull/bear/judge
        factor_scores = volatilities = None
        if max_positions > 0 or vol_adjust or factor_overlay:
            self.research_step.emit(
                "Scoring factors (momentum, quality, volatility)..." if factor_overlay
                else ("Ranking the universe to concentrate..." if max_positions > 0
                      else "Measuring volatility for sizing...")
            )
            momentum, volatilities = self._fetch_price_signals(client, symbols)
            if factor_overlay:  # composite = momentum (§30) + SEC quality (§32) + low-vol (§31)
                quality: dict = {}
                try:
                    for symbol, metrics in self.memory.get_fundamentals(symbols).items():
                        score = fundamental_score(metrics)
                        if score is not None:
                            quality[symbol] = score
                except Exception:
                    quality = {}
                factor_scores = composite_factor_scores(momentum, quality, volatilities)
            elif max_positions > 0:
                factor_scores = momentum  # concentration ranks on momentum when the overlay is off
        # Reduce over-diversification (baked smart default): if the user hasn't set an explicit cap,
        # still concentrate into the top ~30 buys and skip dust-sized new positions, rather than
        # spreading capital across the whole 350+ universe. An explicit cap is always honored.
        effective_max_positions = max_positions if max_positions > 0 else DEFAULT_MAX_POSITIONS
        plan = build_rebalance_plan(
            total_equity, cash, holdings, watchlist, research_fn,
            max_position_pct=1.0,  # no per-stock cap (removed at user direction) — sized by conviction + the cash buffer
            max_positions=effective_max_positions,
            min_position_usd=DEFAULT_MIN_POSITION_USD,
            factor_scores=factor_scores,
            factor_overlay=factor_overlay,
            volatilities=volatilities,
            vol_adjust=vol_adjust,
            adversarial=adversarial,
            risk=self._compute_risk_controls(client, symbols, total_equity),  # §35 risk controls
            cash_buffer_pct=DEFAULT_CASH_BUFFER / 100.0,
            preset=DEFAULT_PRESET,
            trim_band_pct=DEFAULT_TRIM_BAND_PCT,  # let winners run before trimming overweight names
            memory=self.memory,
            rating_max_age_days=rating_age,
            special_symbols=special_symbols,
            special_allocation_pct=max(0.0, special_pct) / 100.0,
            special_principal=special_principal,
            daytrade_symbols=daytrade_symbols,
            daytrade_allocation_pct=max(0.0, daytrade_pct) / 100.0,
            index_symbol=self.settings.get(INVEST_INDEX_SYMBOL_SETTING, DEFAULT_INDEX_SYMBOL),
            index_allocation_pct=max(0.0, float(self.settings.get(INVEST_INDEX_ALLOCATION_SETTING, DEFAULT_INDEX_ALLOCATION_PCT * 100) or 0.0)) / 100.0,
            holdings_pl=holdings_pl,
            market_context_fn=lambda syms: self._fetch_market_context(client, syms),
            on_issue=self.research_issue.emit,
            progress_fn=self.research_step.emit,
            performance_override=track,
        )
        mode_label = "live" if is_real else "paper"
        self.research_step.emit("Placing orders...")
        results = execute_rebalance(plan.actions, client, self.memory, mode_label=mode_label, holdings_pl=holdings_pl,
                                    nonfractionable=self.memory.get_nonfractionable_symbols())
        placed = sum(1 for _action, outcome in results if not outcome.startswith("FAILED"))
        status = f"placed {placed}/{len(results)} order(s)."
        if rotated:
            status = f"rotated {swaps} name(s); " + status
        return {"status": status, "new_roster": symbols if rotated else None}

    def refresh_research_now(self) -> None:
        """Manual 'Refresh research now' button: force a full AI research pass (re-rate core, scout
        Special + Day-trade, review the roster) regardless of cadence, off-thread, WITHOUT trading."""
        if not (self.settings.get(ALPACA_API_KEY_SETTING) and self.settings.get(ALPACA_SECRET_KEY_SETTING)):
            QMessageBox.warning(self, "HELIX", "Save your Alpaca API key and secret first.")
            return
        if self._cycle_busy:
            QMessageBox.information(self, "HELIX", "HELIX is already working - give it a moment, then try again.")
            return
        if not self.stock_symbols:
            QMessageBox.warning(self, "HELIX", "No stocks in the universe to research yet.")
            return
        confirm = QMessageBox.question(
            self, "HELIX - Refresh research",
            "Run HELIX's AI research now? It will re-rate the core stocks, scout new Special and "
            "Day-trade picks, and review the universe for swaps - using Claude (a few calls). It "
            "updates the picks and the Research log but places NO trades.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._cycle_busy = True
        self.research_issue_label.setVisible(False)
        symbols = list(self.stock_symbols)
        self.status.setText("Refreshing AI research (no trading)...")
        self._busy(True)
        spawn_worker(self._workers, lambda: self._do_research_refresh(symbols), self._research_refresh_done)

    def _do_research_refresh(self, symbols: list) -> dict:
        """Force every research pass once (no trading). Mirrors the research half of `_run_cycle` with
        force=True so cadence gates are bypassed; safe in any market hours."""
        client = AlpacaClient.from_settings(self.settings)
        research_fn = self.build_research_fn([])
        core_days = float(self.settings.get(INVEST_CORE_RATING_DAYS_SETTING, DEFAULT_RATING_MAX_AGE_DAYS) or DEFAULT_RATING_MAX_AGE_DAYS)
        roster_days = int(self.settings.get(INVEST_ROSTER_DAYS_SETTING, DEFAULT_ROSTER_REVIEW_DAYS) or DEFAULT_ROSTER_REVIEW_DAYS)
        special_days = int(self.settings.get(INVEST_SPECIAL_DAYS_SETTING, DEFAULT_SPECIAL_RESEARCH_DAYS) or DEFAULT_SPECIAL_RESEARCH_DAYS)
        daytrade_days = int(self.settings.get(INVEST_DAYTRADE_DAYS_SETTING, DEFAULT_DAYTRADE_RESEARCH_DAYS) or DEFAULT_DAYTRADE_RESEARCH_DAYS)
        account = client.get_account()
        positions = client.get_positions()
        equity = _to_float(account.get("equity") or account.get("portfolio_value"))
        holdings = {p.get("symbol", ""): _to_float(p.get("market_value")) for p in positions if p.get("symbol")}
        holdings_pl = {
            p.get("symbol", ""): {
                "market_value": _to_float(p.get("market_value")),
                "unrealized_pl": _to_float(p.get("unrealized_pl")),
                "unrealized_plpc": _to_float(p.get("unrealized_plpc")),
            }
            for p in positions if p.get("symbol")
        }
        track = self._performance_review(client, holdings_pl)
        self._maybe_refresh_market_assets(client)
        tradable = self.memory.get_tradable_universe() or None
        self._maybe_refresh_fundamentals(symbols)
        self._maybe_refresh_sectors(symbols)
        watchlist = [{"symbol": s, "thesis": "user pick", "max_allocation_pct": None} for s in symbols]
        rotated, swaps, new_syms = False, 0, symbols
        # Each pass is wrapped so a failure in one (e.g. a persistent API error after retries) surfaces
        # to the research-issue line but doesn't sink the whole refresh — the rest still run.
        self.research_step.emit("Re-rating the core stocks...")
        try:
            maybe_refresh_core_ratings(
                self.memory, watchlist, holdings, research_fn, total_equity=equity, preset=DEFAULT_PRESET,
                rating_max_age_days=core_days,
                market_context_fn=lambda syms: self._fetch_market_context(client, syms),
                on_issue=self.research_issue.emit, progress_fn=self.research_step.emit,
                performance_override=track,
                adversarial=bool(self.settings.get(INVEST_ADVERSARIAL_SETTING, False)),
                force=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.research_issue.emit(f"Core re-rate failed: {exc}")
        self.research_step.emit("Reviewing the universe for swaps...")
        try:
            new_syms, rotated, swaps = maybe_rotate_roster(
                self.settings, self.memory, symbols, holdings, research_fn, review_days=roster_days,
                market_context_fn=lambda syms: self._fetch_market_context(client, syms),
                on_issue=self.research_issue.emit, progress_fn=self.research_step.emit,
                tradable=tradable, screen_fn=self._make_screen_fn(client, "core"),
                seed_fn=lambda: self._discover_seed_candidates(client), force=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.research_issue.emit(f"Roster review failed: {exc}")
        self.research_step.emit("Scouting new moonshot stocks...")
        try:
            maybe_research_special(
                self.settings, self.memory, research_fn, holdings_pl=holdings_pl, research_days=special_days,
                market_context_fn=lambda: self._fetch_news_context(client), on_issue=self.research_issue.emit,
                performance=track, tradable=tradable, screen_fn=self._make_screen_fn(client, "special"), force=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.research_issue.emit(f"Special scout failed: {exc}")
        self.research_step.emit("Scouting short-term momentum stocks...")
        try:
            maybe_research_daytrade(
                self.settings, self.memory, research_fn, research_days=daytrade_days,
                market_context_fn=lambda: self._fetch_news_context(client), on_issue=self.research_issue.emit,
                performance=track, tradable=tradable, screen_fn=self._make_screen_fn(client, "daytrade"), force=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.research_issue.emit(f"Day-trade scout failed: {exc}")
        return {"rotated": rotated, "swaps": swaps, "roster": new_syms if rotated else None}

    def _research_refresh_done(self, ok: bool, payload) -> None:
        self._cycle_busy = False
        self._busy(False)
        self._update_research_eta()
        if not ok:
            self.status.setText(f"Research refresh failed: {payload}")
            return
        roster = payload.get("roster")
        if roster:
            self.stock_symbols = roster
            self._update_universe_label()
        status = "AI research refreshed - picks and Research log are up to date."
        if payload.get("rotated"):
            status = f"AI research refreshed; rotated {payload['swaps']} name(s) into the universe."
        self.status.setText(status)
        self.refresh_portfolio(quiet=True)

    def _cycle_done(self, ok: bool, payload) -> None:
        # Self-healing (§39): whatever happens below, the busy flag is cleared and the next cycle is
        # re-armed in `finally` — a permanently-running trader must never be left wedged by a display
        # error. (A failed _run_cycle is already caught by spawn_worker and arrives here as ok=False.)
        self._cycle_busy = False
        self._busy(False)
        next_ms = self._interval_ms()
        try:
            self._update_research_eta()  # a cycle may have refreshed the research timestamps
            if not ok:
                label = "Cycle error" if isinstance(payload, AlpacaError) else "Research failed"
                self.status.setText(f"{label}: {payload}")
                _LOG.warning("auto cycle failed: %s", payload)
            elif payload.get("market_closed"):
                next_ms = int(payload.get("retry_ms") or MARKET_CLOSED_RETRY_MS)
                self.status.setText("RUNNING: market closed — waiting for the next open (no trades, no AI cost).")
                self.refresh_portfolio()
            else:
                new_roster = payload.get("new_roster")
                if new_roster:
                    self.stock_symbols = new_roster
                    self._update_universe_label()
                self.status.setText(f"RUNNING: {payload['status']} Next review in {self.interval.currentText()}.")
                self.settings.set(INVEST_LAST_CYCLE_OK_SETTING, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                _LOG.info("auto cycle ok: %s", payload.get("status", ""))
                self.refresh_portfolio()
        except Exception:  # a UI/display error must never stop the trading loop
            _LOG.exception("cycle_done handler error (loop continues)")
            try:
                self.status.setText("RUNNING: recovered from a display error; continuing.")
            except Exception:
                pass
        finally:
            self._schedule_next(next_ms)

    def update_cost_label(self) -> None:
        summary = self.memory.ai_usage_summary()
        self.cost_label.setText(
            f"Claude cost (est.): today ${summary['today_cost']:.4f}   |   month ${summary['month_cost']:.4f}   |   "
            f"all-time ${summary['total_cost']:.4f} ({summary['calls']} calls). Exact billing: Anthropic Console."
        )

    def show_research_log(self) -> None:
        """View what HELIX researched (Core ratings + Special + Day-trade scouts), from SQLite."""
        rows = self.memory.list_stock_rationale()
        # Special/Day-trade rows now persist per-sleeve (composite key); limit the view to the CURRENT
        # pick lists so names rotated off a sleeve don't linger in the log.
        special_set = set(normalize_roster(self.settings.get(SPECIAL_SETTING, "")))
        daytrade_set = set(normalize_roster(self.settings.get(DAYTRADE_SETTING, "")))
        core = [r for r in rows if r.get("action") in ("buy", "watch", "skip")]
        special = [r for r in rows if r.get("action") == "special" and (not special_set or r["symbol"] in special_set)]
        daytrade = [r for r in rows if r.get("action") == "daytrade" and (not daytrade_set or r["symbol"] in daytrade_set)]

        dialog = QDialog(self)
        dialog.setWindowTitle("HELIX Research - prepared for the open")
        layout = QVBoxLayout(dialog)
        intro = QLabel(
            "What HELIX researched and stored in its memory. Core = the long-term HELIX 500 ratings; "
            "Special = the high-risk moonshot scout; Day-trade = the short-term momentum picks. "
            "Double-click any row for the full detail."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(QLabel(f"Core - ratings  ·  {len(core)} names"))
        layout.addWidget(self._rationale_table(core, ["Symbol", "Action", "Conf", "Why", "Updated"], special=False))
        layout.addWidget(QLabel(f"Special - moonshot scout  ·  {len(special)} names"))
        layout.addWidget(self._rationale_table(special, ["Symbol", "Conviction", "Thesis", "Updated"], special=True))
        layout.addWidget(QLabel(f"Day-trade - momentum picks  ·  {len(daytrade)} names"))
        layout.addWidget(self._rationale_table(daytrade, ["Symbol", "Conviction", "Thesis", "Updated"], special=True))
        if not rows:
            layout.addWidget(QLabel("No research yet - it fills in after the first cycle / market-closed scout."))

        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.resize(740, 540)
        dialog.exec()

    def show_scorecard(self) -> None:
        """Open the prediction scorecard (§28): realized forward returns by rating confidence, vs the
        S&P 500 — the honest 'are the picks actually any good?' report. Fetching daily bars for the
        whole rated universe is slow, so it runs off-thread (only price reads, no Claude, no trading)."""
        if not (self.settings.get(ALPACA_API_KEY_SETTING) and self.settings.get(ALPACA_SECRET_KEY_SETTING)):
            QMessageBox.warning(
                self, "HELIX",
                "Save your Alpaca API key and secret first - the scorecard needs price history to score outcomes.",
            )
            return
        self.status.setText("Scoring HELIX's past ratings against the market...")
        self._busy(True)
        spawn_worker(self._workers, self._compute_scorecard, self._scorecard_done)

    def _compute_scorecard(self) -> str:
        client = AlpacaClient.from_settings(self.settings)
        report, _summary = generate_rating_scorecard(self.memory, client)
        return report

    def _scorecard_done(self, ok: bool, payload) -> None:
        self._busy(False)
        if not ok:
            self.status.setText(f"Scorecard error: {payload}")
            QMessageBox.warning(self, "HELIX", f"Could not build the scorecard: {payload}")
            return
        self.status.setText("Prediction scorecard ready.")
        dialog = QDialog(self)
        dialog.setWindowTitle("HELIX Prediction Scorecard")
        layout = QVBoxLayout(dialog)
        intro = QLabel(
            "Do HELIX's high-conviction buys actually beat its low-conviction picks - and the S&P 500? "
            "This scores every past rating by its realized forward return at one week / one month / "
            "three months, bucketed by confidence. Buckets fill in as ratings age past each horizon. "
            "Paper, simulated; gross of costs. Not financial advice."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setFont(QFont("Consolas", 11))
        text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)  # keep the fixed-width columns aligned
        text.setPlainText(payload)
        layout.addWidget(text)
        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.resize(660, 580)
        dialog.exec()

    def _rationale_table(self, rows: list, headers: list, special: bool) -> QTableWidget:
        table = QTableWidget(len(rows), len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setMinimumHeight(180)
        long_col = 2 if special else 3  # Thesis / Why — give it the stretch so more fits inline
        head = table.horizontalHeader()
        for col in range(len(headers)):  # size each column to its content so 'Updated' isn't cut off
            head.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(long_col, QHeaderView.ResizeMode.Stretch)
        for row_index, row in enumerate(rows):
            updated = str(row.get("updated_at", ""))[:16]
            if special:
                cells = [row.get("symbol", ""), row.get("confidence", ""), row.get("rationale", ""), updated]
            else:
                cells = [row.get("symbol", ""), row.get("action", ""), row.get("confidence", ""), row.get("rationale", ""), updated]
            for col, value in enumerate(cells):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))  # hover shows the full text
                table.setItem(row_index, col, item)
        table._rows = rows  # stash for the detail view
        table._special = special
        table.cellDoubleClicked.connect(lambda r, _c, t=table: self._show_rationale_detail(t, r))
        return table

    @staticmethod
    def _rationale_detail_text(row: dict, special: bool) -> str:
        symbol = str(row.get("symbol", ""))
        updated = str(row.get("updated_at", ""))[:16]
        if special:
            return (
                f"{symbol}    ({row.get('confidence', '')} conviction)\n\n"
                f"{row.get('rationale', '') or 'No thesis recorded.'}\n\nResearched: {updated}"
            )
        return (
            f"{symbol}    ({row.get('action', '')} · {row.get('confidence', '')} confidence)\n\n"
            f"{row.get('rationale', '') or 'No rationale recorded.'}\n\nUpdated: {updated}"
        )

    def _show_rationale_detail(self, table: QTableWidget, row_index: int) -> None:
        rows = getattr(table, "_rows", [])
        if not (0 <= row_index < len(rows)):
            return
        row = rows[row_index]
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{row.get('symbol', '')} - research detail")
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(self._rationale_detail_text(row, getattr(table, "_special", False)))
        layout.addWidget(text)
        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.resize(460, 280)
        dialog.exec()

    def _auto_refresh_portfolio(self) -> None:
        """Live balance + chart while the app is open. Quiet (no busy bar); the _portfolio_busy guard
        skips it if a manual refresh or trading cycle is mid-flight; skipped until Alpaca keys exist."""
        if not (self.settings.get(ALPACA_API_KEY_SETTING) and self.settings.get(ALPACA_SECRET_KEY_SETTING)):
            return
        self.refresh_portfolio(quiet=True)

    def refresh_portfolio(self, quiet: bool = False) -> None:
        self.update_cost_label()
        if self._portfolio_busy:
            return
        self._portfolio_busy = True
        self._portfolio_quiet = quiet
        if not quiet:
            self._busy(True)
        period, timeframe = self._chart_selection()
        days = self._chart_days()
        spawn_worker(
            self._workers, lambda: self._fetch_portfolio(period, timeframe, days), self._portfolio_done
        )

    def _fetch_portfolio(self, period: str = "1M", timeframe: str = "1D", days: int = 31):
        client = AlpacaClient.from_settings(self.settings)
        raw_positions = client.get_positions()
        snapshot = portfolio_snapshot(client.get_account(), raw_positions)
        try:
            series = parse_portfolio_history(client.get_portfolio_history(period, timeframe))
        except AlpacaError:
            series = EquitySeries([])
        return snapshot, series, self._fetch_benchmark(client, series, days), raw_positions

    def _fetch_benchmark(self, client, series, days: int):
        """S&P 500 (SPY) over the same window, normalized to the account's start. None if unavailable.
        Skipped for the 1-day intraday view (days <= 1) — daily SPY bars can't match an intraday line,
        so the 1D chart shows the account's own line only."""
        if not series or int(days) <= 1:
            return None
        try:
            start = (datetime.now() - timedelta(days=int(days) + 5)).strftime("%Y-%m-%d")
            closes = parse_stock_bars(client.get_stock_bars("SPY", "1Day", start), "SPY")
        except AlpacaError:
            return None
        bench = benchmark_series(series.start, closes, series.start_label, series.end_label)
        return bench if bench else None

    def _portfolio_done(self, ok: bool, payload) -> None:
        self._portfolio_busy = False
        if not self._portfolio_quiet:
            self._busy(False)
        if not ok:
            self.balance_label.setText("Balance: -")
            self.balance_sub.setText("Save your Alpaca keys to load your balance.")
            self.positions_table.setRowCount(0)
            self.special_table.setRowCount(0)
            self.daytrade_table.setRowCount(0)
            self._raw_positions = {}
            self._update_chart(EquitySeries([]))
            return
        snapshot, series, benchmark, raw_positions = payload
        self._raw_positions = {str(p.get("symbol", "")).upper(): p for p in (raw_positions or [])}
        sign = "+" if snapshot.unrealized_pl >= 0 else ""
        self.balance_label.setText(f"${snapshot.equity:,.2f}")
        self.balance_sub.setText(
            f"Cash ${snapshot.cash:,.2f}      Gains {sign}${snapshot.unrealized_pl:,.2f}"
        )
        self.populate_positions(snapshot.positions)
        # Record HELIX's own equity sample (durable curve for the AI layer), then draw.
        self.memory.record_equity(
            snapshot.equity, snapshot.cash, snapshot.market_value, snapshot.unrealized_pl
        )
        self._update_chart(series, benchmark)

    def _update_chart(self, series, benchmark=None) -> None:
        """Draw the Alpaca curve + S&P 500 overlay; fall back to HELIX's own recorded equity if empty."""
        if not series:
            series = equity_series_from_rows(self.memory.list_equity_history(self._chart_days()))
            benchmark = None  # the local fallback has no index overlay
        self.equity_chart.set_series(series, self.chart_range.currentText(), benchmark)

    def refresh_market_status(self) -> None:
        """Update the green/red market light. A free Alpaca clock call, off-thread; polled every 60s."""
        self._update_research_eta()  # cheap local countdown refresh (rides the 60s market poll)
        if self._market_busy:
            return
        if not (self.settings.get(ALPACA_API_KEY_SETTING) and self.settings.get(ALPACA_SECRET_KEY_SETTING)):
            self.market_label.setText('<span style="color:#6fb3c0">○ Market status — save Alpaca keys</span>')
            return
        self._market_busy = True
        spawn_worker(self._workers, self._fetch_clock, self._market_status_done)

    def _fetch_clock(self) -> dict:
        return AlpacaClient.from_settings(self.settings).get_clock()

    def _market_status_done(self, ok: bool, payload) -> None:
        self._market_busy = False
        if not ok or not isinstance(payload, dict):
            self.market_label.setText('<span style="color:#6fb3c0">○ Market status unavailable</span>')
            return
        self.market_label.setText(self._format_market(payload))

    def _format_market(self, clock: dict) -> str:
        if bool(clock.get("is_open")):
            color = "#33d17a"
            when = self._fmt_market_time(clock.get("next_close"))
            text = f"Market open · closes {when}" if when else "Market open"
        else:
            color = "#ff6b6b"
            when = self._fmt_market_time(clock.get("next_open"))
            text = f"Market closed · opens {when}" if when else "Market closed"
        return f'<span style="color:{color}; font-size:13pt">●</span> {text}'

    @staticmethod
    def _fmt_market_time(raw) -> str:
        """RFC3339 timestamp -> 'Wed 4:00 PM ET' (Alpaca clock times are US/Eastern)."""
        if not raw:
            return ""
        try:
            moment = datetime.fromisoformat(str(raw))
        except (ValueError, TypeError):
            return ""
        hour = moment.hour % 12 or 12
        meridiem = "AM" if moment.hour < 12 else "PM"
        return f"{moment.strftime('%a')} {hour}:{moment.minute:02d} {meridiem} ET"

    @staticmethod
    def _fmt_local_dt(raw) -> str:
        """Alpaca offset-aware RFC3339 -> the user's local time, e.g. 'Fri 6:30 AM'."""
        if not raw:
            return ""
        try:
            moment = datetime.fromisoformat(str(raw)).astimezone()
        except (ValueError, TypeError):
            return ""
        return f"{moment.strftime('%a')} {_fmt_ampm(moment.hour, moment.minute)}"

    def show_market_schedule(self) -> None:
        """Open the Market Schedule popup. Loads the live clock + calendar off-thread first, then
        shows the dialog fully populated (so its widgets never outlive a pending worker)."""
        self.schedule_button.setEnabled(False)
        self.schedule_button.setText("Loading...")
        spawn_worker(self._workers, self._fetch_schedule, self._show_schedule_dialog)

    def _fetch_schedule(self) -> dict:
        out = {"clock": None, "calendar": [], "error": ""}
        try:
            client = AlpacaClient.from_settings(self.settings)
            out["clock"] = client.get_clock()
            today = date.today()
            out["calendar"] = client.get_calendar(today.isoformat(), (today + timedelta(days=18)).isoformat())
        except AlpacaError as error:
            out["error"] = str(error)
        except Exception as error:  # never let the popup fail to open
            out["error"] = str(error)
        return out

    def _show_schedule_dialog(self, ok: bool, payload) -> None:
        self.schedule_button.setEnabled(True)
        self.schedule_button.setText("Market schedule")
        data = payload if (ok and isinstance(payload, dict)) else {"error": str(payload)}
        self._build_schedule_dialog(data).exec()

    def _build_schedule_dialog(self, data: dict) -> QDialog:
        clock = data.get("clock")
        calendar = data.get("calendar") or []
        error = data.get("error", "")
        tzname = datetime.now().astimezone().tzname() or "your time"

        dialog = QDialog(self)
        dialog.setWindowTitle("HELIX - Market Schedule")
        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)

        intro = QLabel(
            "The US stock market trades <b>9:30 AM - 4:00 PM Eastern</b>, Monday through Friday - "
            "closed on weekends and holidays, with occasional 1:00 PM ET early closes. HELIX only "
            "auto-trades during this regular session, so nothing is bought while it's closed."
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        today = date.today()
        local_line = QLabel(
            f"In your time ({tzname}): <b>{_hhmm_to_local(today, '09:30')} - "
            f"{_hhmm_to_local(today, '16:00')}</b>."
        )
        local_line.setTextFormat(Qt.TextFormat.RichText)
        local_line.setStyleSheet("color:#ffc857; font-size:13pt;")
        layout.addWidget(local_line)

        if clock:
            light = self._format_market(clock)
            if clock.get("is_open"):
                local_next = self._fmt_local_dt(clock.get("next_close"))
                tail = f" - closes {local_next} {tzname}" if local_next else ""
            else:
                local_next = self._fmt_local_dt(clock.get("next_open"))
                tail = f" - opens {local_next} {tzname}" if local_next else ""
            status = QLabel(light + tail)
        elif error:
            status = QLabel("Live status &amp; the calendar need your Alpaca keys (save them above).")
            status.setStyleSheet("color:#6fb3c0;")
        else:
            status = QLabel("")
        status.setTextFormat(Qt.TextFormat.RichText)
        status.setWordWrap(True)
        layout.addWidget(status)

        if calendar:
            layout.addWidget(QLabel("Upcoming trading days (holidays are skipped):"))
            table = QTableWidget(len(calendar), 3)
            table.setHorizontalHeaderLabels(["Day", "Hours (ET)", f"Hours ({tzname})"])
            head = table.horizontalHeader()
            head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            head.setStretchLastSection(True)
            table.verticalHeader().setVisible(False)
            table.setAlternatingRowColors(True)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setMinimumHeight(260)
            for row, day in enumerate(calendar):
                raw_date = str(day.get("date", ""))
                open_t, close_t = day.get("open", "09:30"), day.get("close", "16:00")
                try:
                    d = date.fromisoformat(raw_date[:10])
                    label = d.strftime("%a %b %d")
                    local_hours = f"{_hhmm_to_local(d, open_t)} - {_hhmm_to_local(d, close_t)}"
                except ValueError:
                    label, local_hours = raw_date, ""
                early = "  (early close)" if str(close_t) < "16:00" else ""
                table.setItem(row, 0, QTableWidgetItem(label))
                table.setItem(row, 1, QTableWidgetItem(f"{_hhmm_to_et(open_t)} - {_hhmm_to_et(close_t)}{early}"))
                table.setItem(row, 2, QTableWidgetItem(local_hours))
            layout.addWidget(table)
        elif not error:
            layout.addWidget(QLabel("No upcoming trading days were returned."))

        note = QLabel("Times come from Alpaca's official market calendar (handles holidays & early closes).")
        note.setStyleSheet("color:#6fb3c0;")
        note.setWordWrap(True)
        layout.addWidget(note)

        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.resize(580, 560)
        return dialog

    def populate_positions(self, rows: list) -> None:
        """Split holdings into the Core / Special / Day-trade tables (§21/§27)."""
        special_set = set(normalize_roster(self.settings.get(SPECIAL_SETTING, "")))
        daytrade_set = set(normalize_roster(self.settings.get(DAYTRADE_SETTING, ""))) - special_set
        special = [p for p in rows if str(p.symbol).upper() in special_set]
        daytrade = [p for p in rows if str(p.symbol).upper() in daytrade_set]
        core = [
            p for p in rows
            if str(p.symbol).upper() not in special_set and str(p.symbol).upper() not in daytrade_set
        ]
        self._fill_positions_table(self.positions_table, core)
        self._fill_positions_table(self.special_table, special)
        self._fill_positions_table(self.daytrade_table, daytrade)

    def _fill_positions_table(self, table: QTableWidget, rows: list) -> None:
        table.setRowCount(len(rows))
        for row, position in enumerate(rows):
            cells = [
                position.symbol,
                f"{position.qty:g}",
                f"${position.market_value:,.2f}",
                f"${position.unrealized_pl:,.2f}",
                f"{position.unrealized_plpc:.2f}%",
            ]
            for column, value in enumerate(cells):
                table.setItem(row, column, QTableWidgetItem(value))
            # Compact Details button, wrapped with margins so it sits cleanly inside the row.
            details = QPushButton("Details")
            details.setObjectName("rowButton")
            details.setFixedHeight(26)
            details.setToolTip("When HELIX bought it, the thesis, and the numbers")
            details.clicked.connect(lambda _checked=False, p=position: self.show_position_details(p))
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(6, 3, 6, 3)
            holder_layout.addWidget(details)
            table.setCellWidget(row, 5, holder)

    def show_position_details(self, position) -> None:
        """Fast popup for one holding: live numbers, HELIX's thesis, and its buy/sell history."""
        self._build_position_dialog(position).exec()

    @staticmethod
    def _parse_trade(entry: dict) -> tuple:
        """Pull (side, notional, status) out of a journal trade body."""
        side = notional = status = ""
        for line in str(entry.get("body", "") or "").splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "side":
                side = value
            elif key == "notional":
                notional = value
            elif key == "status":
                status = value
        return side, notional, status

    @staticmethod
    def _days_since(raw) -> int | None:
        try:
            moment = datetime.fromisoformat(str(raw)[:19])
        except (ValueError, TypeError):
            return None
        return max(0, (datetime.now() - moment).days)

    def _build_position_dialog(self, position) -> QDialog:
        symbol = str(position.symbol).upper()
        raw = (getattr(self, "_raw_positions", {}) or {}).get(symbol, {})
        try:
            trades = self.memory.list_symbol_trades(symbol, limit=30)
        except Exception:
            trades = []
        try:
            rationale = next(
                (r for r in self.memory.list_stock_rationale() if str(r.get("symbol", "")).upper() == symbol),
                None,
            )
        except Exception:
            rationale = None

        # Prefer the richer raw Alpaca fields; fall back to the parsed PositionRow.
        qty, avg_cost = position.qty, position.avg_entry
        market_value, pl, plpc = position.market_value, position.unrealized_pl, position.unrealized_plpc
        current_price = _to_float(raw.get("current_price")) or (market_value / qty if qty else 0.0)
        cost_basis = _to_float(raw.get("cost_basis")) or (avg_cost * qty)
        change_today = _to_float(raw.get("change_today")) * 100.0 if raw.get("change_today") is not None else None

        dialog = QDialog(self)
        dialog.setWindowTitle(f"HELIX - {symbol}")
        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)

        head = QLabel(symbol)
        head.setStyleSheet("font-size: 18pt; font-weight: 800; color: #ffc857;")
        layout.addWidget(head)

        pl_color = "#33d17a" if pl >= 0 else "#ff6b6b"
        sign = "+" if pl >= 0 else ""
        facts = [
            ("Quantity", f"{qty:g} shares"),
            ("Avg cost", f"${avg_cost:,.2f}"),
            ("Current price", f"${current_price:,.2f}"),
            ("Market value", f"${market_value:,.2f}"),
            ("Cost basis", f"${cost_basis:,.2f}"),
            ("Open P/L", f"<span style='color:{pl_color}'>{sign}${pl:,.2f}  ({plpc:+.2f}%)</span>"),
        ]
        if change_today is not None:
            td_color = "#33d17a" if change_today >= 0 else "#ff6b6b"
            facts.append(("Today", f"<span style='color:{td_color}'>{change_today:+.2f}%</span>"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        for index, (label, value) in enumerate(facts):
            cell = QLabel(f"<span style='color:#6fb3c0'>{label}</span>  {value}")
            cell.setTextFormat(Qt.TextFormat.RichText)
            grid.addWidget(cell, *divmod(index, 2))
        layout.addLayout(grid)

        if rationale and rationale.get("rationale"):
            action, conf = rationale.get("action", ""), rationale.get("confidence", "")
            updated = str(rationale.get("updated_at", ""))[:10]
            meta = ", ".join(part for part in (action, conf) if part)
            tail = f" · {updated}" if updated else ""
            thesis = QLabel(
                f"<b>HELIX's take:</b> {rationale.get('rationale', '')}  "
                f"<span style='color:#6fb3c0'>({meta}{tail})</span>"
            )
            thesis.setTextFormat(Qt.TextFormat.RichText)
            thesis.setWordWrap(True)
            layout.addWidget(thesis)

        layout.addWidget(QLabel("<b>HELIX trade history</b> (its own order log):"))
        if trades:
            first_buy = next((t for t in reversed(trades) if "buy" in str(t.get("title", "")).lower()), None)
            if first_buy:
                when = str(first_buy.get("created_at", ""))[:10]
                held = self._days_since(first_buy.get("created_at"))
                ago = f" ({held} day{'' if held == 1 else 's'} ago)" if held is not None else ""
                layout.addWidget(QLabel(f"First bought {when}{ago}."))
            table = QTableWidget(len(trades), 4)
            table.setHorizontalHeaderLabels(["When", "Side", "Amount", "Status"])
            t_head = table.horizontalHeader()
            t_head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setVisible(False)
            table.setAlternatingRowColors(True)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setMinimumHeight(180)
            for row, entry in enumerate(trades):
                side, notional, status = self._parse_trade(entry)
                when = str(entry.get("created_at", ""))[:16].replace("T", " ")
                amount = f"${_to_float(notional):,.2f}" if notional else ""
                values = [when, side.upper(), amount, status]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if col == 1 and side.lower() == "sell":
                        item.setForeground(QColor("#ff6b6b"))
                    elif col == 1 and side.lower() == "buy":
                        item.setForeground(QColor("#33d17a"))
                    table.setItem(row, col, item)
            layout.addWidget(table)
        else:
            empty = QLabel("No HELIX trade records for this symbol yet (it logs each order it places).")
            empty.setStyleSheet("color:#6fb3c0;")
            empty.setWordWrap(True)
            layout.addWidget(empty)

        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.resize(520, 540)
        return dialog


class InvestmentMoneyTab(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.settings = AppSettings()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)

        money_box_group = QGroupBox("Investment Cash")
        form = QFormLayout(money_box_group)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(14)

        self.amount = money_box(float(self.settings.get(INVESTMENT_AMOUNT_SETTING, 100.0)))
        self.status = QLabel()

        form.addRow("Amount", self.amount)
        form.addRow("Status", self.status)

        actions = QHBoxLayout()
        actions.setSpacing(12)
        save = QPushButton("Save Amount")
        save.clicked.connect(self.save_amount)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear_amount)
        actions.addStretch(1)
        actions.addWidget(clear)
        actions.addWidget(save)

        layout.addWidget(money_box_group)
        layout.addLayout(actions)
        layout.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        amount = float(self.settings.get(INVESTMENT_AMOUNT_SETTING, self.amount.value()))
        self.amount.setValue(amount)
        self.status.setText(f"Investable amount saved: {_money_or_blank(amount)}")

    def save_amount(self) -> None:
        self.settings.set(INVESTMENT_AMOUNT_SETTING, self.amount.value())
        self.refresh()
        QMessageBox.information(self, "HELIX", "Investment amount saved.")

    def clear_amount(self) -> None:
        self.settings.remove(INVESTMENT_AMOUNT_SETTING)
        self.amount.setValue(0.0)
        self.status.setText("No investment amount saved.")


class AlpacaTab(QWidget):
    def __init__(self, memory: SQLiteMemory) -> None:
        super().__init__()
        self.memory = memory
        self.settings = AppSettings()

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer_layout.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)

        credentials_box = QGroupBox("Alpaca API")
        form = QFormLayout(credentials_box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(14)

        self.environment = QComboBox()
        self.environment.addItems((ALPACA_ENV_PAPER, ALPACA_ENV_LIVE))
        self.environment.setCurrentText(
            self.settings.get(ALPACA_ENVIRONMENT_SETTING, ALPACA_ENV_PAPER)
        )

        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText(self.key_placeholder(ALPACA_API_KEY_SETTING))

        self.secret_key = QLineEdit()
        self.secret_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_key.setPlaceholderText(self.key_placeholder(ALPACA_SECRET_KEY_SETTING))

        self.status = QLabel()

        form.addRow("Environment", self.environment)
        form.addRow("API key", self.api_key)
        form.addRow("Secret key", self.secret_key)
        form.addRow("Status", self.status)

        actions = QHBoxLayout()
        actions.setSpacing(12)
        refresh_account = QPushButton("Refresh Account")
        refresh_account.clicked.connect(self.refresh_account)
        save = QPushButton("Save Alpaca")
        save.clicked.connect(self.save_alpaca)
        clear = QPushButton("Clear Alpaca")
        clear.clicked.connect(self.clear_alpaca)
        actions.addStretch(1)
        actions.addWidget(refresh_account)
        actions.addWidget(clear)
        actions.addWidget(save)

        ticket_box = QGroupBox("Paper Order Ticket")
        ticket_form = QGridLayout(ticket_box)
        ticket_form.setHorizontalSpacing(16)
        ticket_form.setVerticalSpacing(14)

        self.trade_symbol = QLineEdit()
        self.trade_symbol.setPlaceholderText("VOO")
        self.trade_side = QComboBox()
        self.trade_side.addItems(("Buy", "Sell"))
        self.trade_amount_type = QComboBox()
        self.trade_amount_type.addItems((TRADE_AMOUNT_DOLLARS, TRADE_AMOUNT_SHARES))
        self.trade_amount_type.currentTextChanged.connect(self.update_trade_amount_mode)
        self.trade_amount = QDoubleSpinBox()
        self.trade_amount.setRange(0.0, 1_000_000.0)
        self.trade_amount.setDecimals(4)
        self.trade_amount.setSingleStep(1.0)
        self.trade_amount.setMinimumHeight(42)
        self.trade_amount.setValue(float(self.settings.get(INVESTMENT_AMOUNT_SETTING, 100.0)))
        self.trade_status = QLabel("Paper trading only. Market/day orders.")

        submit_order = QPushButton("Submit Paper Order")
        submit_order.clicked.connect(self.submit_paper_order)

        ticket_form.addWidget(QLabel("Symbol"), 0, 0)
        ticket_form.addWidget(self.trade_symbol, 0, 1)
        ticket_form.addWidget(QLabel("Side"), 0, 2)
        ticket_form.addWidget(self.trade_side, 0, 3)
        ticket_form.addWidget(QLabel("Amount type"), 1, 0)
        ticket_form.addWidget(self.trade_amount_type, 1, 1)
        ticket_form.addWidget(QLabel("Amount"), 1, 2)
        ticket_form.addWidget(self.trade_amount, 1, 3)
        ticket_form.addWidget(self.trade_status, 2, 0, 1, 3)
        ticket_form.addWidget(submit_order, 2, 3)
        self.update_trade_amount_mode()

        self.account_tabs = QTabWidget()

        self.account_table = QTableWidget(0, 2)
        self.account_table.setHorizontalHeaderLabels(["Metric", "Value"])
        self.account_table.horizontalHeader().setStretchLastSection(True)
        self.account_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.account_table.verticalHeader().setVisible(False)
        self.account_table.verticalHeader().setDefaultSectionSize(42)
        self.account_table.setAlternatingRowColors(True)
        self.account_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self.positions_table = QTableWidget(0, 6)
        self.positions_table.setHorizontalHeaderLabels(
            ["Symbol", "Qty", "Market Value", "Cost Basis", "Unrealized P/L", "Side"]
        )
        self.positions_table.horizontalHeader().setStretchLastSection(True)
        self.positions_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.positions_table.verticalHeader().setVisible(False)
        self.positions_table.verticalHeader().setDefaultSectionSize(42)
        self.positions_table.setAlternatingRowColors(True)
        self.positions_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self.orders_table = QTableWidget(0, 6)
        self.orders_table.setHorizontalHeaderLabels(
            ["Symbol", "Side", "Qty", "Type", "Status", "Submitted"]
        )
        self.orders_table.horizontalHeader().setStretchLastSection(True)
        self.orders_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.orders_table.verticalHeader().setVisible(False)
        self.orders_table.verticalHeader().setDefaultSectionSize(42)
        self.orders_table.setAlternatingRowColors(True)
        self.orders_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self.account_tabs.addTab(self.account_table, "Account")
        self.account_tabs.addTab(self.positions_table, "Positions")
        self.account_tabs.addTab(self.orders_table, "Open Orders")

        layout.addWidget(credentials_box)
        layout.addLayout(actions)
        layout.addWidget(ticket_box)
        layout.addWidget(self.account_tabs, 1)
        self.refresh()

    def refresh(self) -> None:
        self.environment.setCurrentText(
            self.settings.get(ALPACA_ENVIRONMENT_SETTING, ALPACA_ENV_PAPER)
        )
        self.api_key.setPlaceholderText(self.key_placeholder(ALPACA_API_KEY_SETTING))
        self.secret_key.setPlaceholderText(self.key_placeholder(ALPACA_SECRET_KEY_SETTING))
        self.status.setText(self.status_text())
        self.trade_amount.setValue(float(self.settings.get(INVESTMENT_AMOUNT_SETTING, self.trade_amount.value())))

    def key_placeholder(self, setting_key: str) -> str:
        return "Saved locally" if self.settings.get(setting_key) else "Paste once, then Save Alpaca"

    def status_text(self) -> str:
        api_saved = bool(self.settings.get(ALPACA_API_KEY_SETTING))
        secret_saved = bool(self.settings.get(ALPACA_SECRET_KEY_SETTING))
        environment = self.settings.get(ALPACA_ENVIRONMENT_SETTING, ALPACA_ENV_PAPER)
        if api_saved and secret_saved:
            return f"Configured for {environment} trading."
        return "Not connected."

    def save_alpaca(self) -> None:
        api_key = self.api_key.text().strip()
        secret_key = self.secret_key.text().strip()

        if api_key:
            self.settings.set(ALPACA_API_KEY_SETTING, api_key)
        if secret_key:
            self.settings.set(ALPACA_SECRET_KEY_SETTING, secret_key)
        self.settings.set(ALPACA_ENVIRONMENT_SETTING, self.environment.currentText())

        self.api_key.clear()
        self.secret_key.clear()
        self.refresh()
        QMessageBox.information(self, "HELIX", "Alpaca settings saved.")

    def clear_alpaca(self) -> None:
        self.settings.remove(ALPACA_API_KEY_SETTING)
        self.settings.remove(ALPACA_SECRET_KEY_SETTING)
        self.settings.set(ALPACA_ENVIRONMENT_SETTING, ALPACA_ENV_PAPER)
        self.api_key.clear()
        self.secret_key.clear()
        self.refresh()
        self.account_table.setRowCount(0)
        self.positions_table.setRowCount(0)
        self.orders_table.setRowCount(0)
        QMessageBox.information(self, "HELIX", "Alpaca settings cleared.")

    def update_trade_amount_mode(self) -> None:
        if self.trade_amount_type.currentText() == TRADE_AMOUNT_DOLLARS:
            self.trade_amount.setPrefix("$")
            self.trade_amount.setSuffix("")
            self.trade_amount.setDecimals(2)
            self.trade_amount.setSingleStep(1.0)
            return

        self.trade_amount.setPrefix("")
        self.trade_amount.setSuffix(" shares")
        self.trade_amount.setDecimals(4)
        self.trade_amount.setSingleStep(0.1)

    def submit_paper_order(self) -> None:
        self.save_environment_only()
        environment = self.settings.get(ALPACA_ENVIRONMENT_SETTING, ALPACA_ENV_PAPER)
        if environment != ALPACA_ENV_PAPER:
            QMessageBox.warning(
                self,
                "HELIX",
                "Paper workflow only. Switch Alpaca environment to Paper before submitting orders.",
            )
            return

        symbol = self.trade_symbol.text().strip().upper()
        amount = self.trade_amount.value()
        if not symbol:
            QMessageBox.warning(self, "HELIX", "Ticker symbol is required.")
            return
        if amount <= 0:
            QMessageBox.warning(self, "HELIX", "Order amount must be greater than zero.")
            return

        side = self.trade_side.currentText().lower()
        amount_type = self.trade_amount_type.currentText()
        qty = amount if amount_type == TRADE_AMOUNT_SHARES else None
        notional = amount if amount_type == TRADE_AMOUNT_DOLLARS else None

        self.trade_status.setText(f"Submitting paper {side} order for {symbol}...")
        QApplication.processEvents()

        try:
            client = AlpacaClient.from_settings(self.settings)
            order = client.submit_order(
                symbol=symbol,
                side=side,
                qty=qty,
                notional=notional,
            )
        except AlpacaError as error:
            self.trade_status.setText("Paper order failed.")
            QMessageBox.warning(self, "HELIX", str(error))
            return

        order_id = order.get("id", "unknown")
        status = order.get("status", "submitted")
        submitted_at = order.get("submitted_at", "")
        self.trade_status.setText(f"Paper order {status}: {symbol}")
        self.memory.add_journal_entry(
            entry_type="paper_trade",
            title=f"Paper {side} {symbol}",
            body="\n".join(
                [
                    f"Order ID: {order_id}",
                    f"Status: {status}",
                    f"Submitted: {submitted_at}",
                    f"Symbol: {symbol}",
                    f"Side: {side}",
                    f"Amount type: {amount_type}",
                    f"Amount: {amount}",
                    "Order type: market",
                    "Time in force: day",
                ]
            ),
        )
        self.refresh_account()
        QMessageBox.information(self, "HELIX", f"Paper order submitted for {symbol}.")

    def refresh_account(self) -> None:
        self.save_environment_only()
        self.status.setText("Refreshing Alpaca account...")
        QApplication.processEvents()

        try:
            client = AlpacaClient.from_settings(self.settings)
            account = client.get_account()
            positions = client.get_positions()
            orders = client.get_open_orders()
        except AlpacaError as error:
            self.status.setText("Alpaca refresh failed.")
            QMessageBox.warning(self, "HELIX", str(error))
            return

        self.populate_account_table(account)
        self.populate_positions_table(positions)
        self.populate_orders_table(orders)
        environment = self.settings.get(ALPACA_ENVIRONMENT_SETTING, ALPACA_ENV_PAPER)
        self.status.setText(f"{environment} account refreshed.")

    def save_environment_only(self) -> None:
        self.settings.set(ALPACA_ENVIRONMENT_SETTING, self.environment.currentText())

    def populate_account_table(self, account: dict) -> None:
        metrics = [
            ("Status", account.get("status", "")),
            ("Currency", account.get("currency", "")),
            ("Buying Power", _money_or_blank(account.get("buying_power"))),
            ("Cash", _money_or_blank(account.get("cash"))),
            ("Portfolio Value", _money_or_blank(account.get("portfolio_value"))),
            ("Equity", _money_or_blank(account.get("equity"))),
            ("Long Market Value", _money_or_blank(account.get("long_market_value"))),
            ("Day Trading Buying Power", _money_or_blank(account.get("daytrading_buying_power"))),
            ("Trading Blocked", str(account.get("trading_blocked", ""))),
            ("Account Blocked", str(account.get("account_blocked", ""))),
        ]
        self.account_table.setRowCount(len(metrics))
        for row, (metric, value) in enumerate(metrics):
            self.account_table.setItem(row, 0, QTableWidgetItem(metric))
            self.account_table.setItem(row, 1, QTableWidgetItem(str(value)))

    def populate_positions_table(self, positions: list[dict]) -> None:
        self.positions_table.setRowCount(len(positions))
        for row, position in enumerate(positions):
            values = [
                position.get("symbol", ""),
                position.get("qty", ""),
                _money_or_blank(position.get("market_value")),
                _money_or_blank(position.get("cost_basis")),
                _money_or_blank(position.get("unrealized_pl")),
                position.get("side", ""),
            ]
            for column, value in enumerate(values):
                self.positions_table.setItem(row, column, QTableWidgetItem(str(value)))

    def populate_orders_table(self, orders: list[dict]) -> None:
        self.orders_table.setRowCount(len(orders))
        for row, order in enumerate(orders):
            values = [
                order.get("symbol", ""),
                order.get("side", ""),
                order.get("qty", ""),
                order.get("type", ""),
                order.get("status", ""),
                order.get("submitted_at", ""),
            ]
            for column, value in enumerate(values):
                self.orders_table.setItem(row, column, QTableWidgetItem(str(value)))


class ProfileTab(QWidget):
    def __init__(self, memory: SQLiteMemory, on_saved) -> None:
        super().__init__()
        self.memory = memory
        self.on_saved = on_saved
        self.loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)
        form_box = QGroupBox("Simple Capital Profile")
        form = QFormLayout(form_box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(14)

        self.monthly_income = money_box()
        self.monthly_expenses = money_box()
        self.cash_savings = money_box()
        self.current_investments = money_box()
        self.risk_tolerance = QComboBox()
        self.risk_tolerance.addItems(RISK_LEVELS)
        self.goal_amount = money_box(100000.0)
        self.goal_years = integer_box(1, 60, 10)

        form.addRow("Monthly income", self.monthly_income)
        form.addRow("Monthly required spending", self.monthly_expenses)
        form.addRow("Cash on hand", self.cash_savings)
        form.addRow("Money already invested", self.current_investments)
        form.addRow("Risk tolerance", self.risk_tolerance)
        form.addRow("Target portfolio value", self.goal_amount)
        form.addRow("Years to target", self.goal_years)

        actions = QHBoxLayout()
        actions.setSpacing(12)
        save = QPushButton("Save Profile")
        save.clicked.connect(self.save_profile)
        reload_profile = QPushButton("Reload")
        reload_profile.clicked.connect(self.load_profile)
        actions.addStretch(1)
        actions.addWidget(reload_profile)
        actions.addWidget(save)

        layout.addWidget(form_box)
        layout.addLayout(actions)
        layout.addStretch(1)

    def load_profile(self) -> None:
        if self.loading:
            return

        record = self.memory.get_investment_profile()
        if not record:
            return

        self.loading = True
        try:
            profile = InvestmentProfile.from_record(record)
            self.monthly_income.setValue(profile.monthly_income)
            self.monthly_expenses.setValue(profile.monthly_expenses + profile.monthly_debt_payment)
            self.cash_savings.setValue(profile.cash_savings)
            self.current_investments.setValue(profile.current_investments)
            self.risk_tolerance.setCurrentText(profile.risk_tolerance)
            self.goal_amount.setValue(profile.goal_amount)
            self.goal_years.setValue(profile.goal_years)
        finally:
            self.loading = False

    def save_profile(self) -> None:
        risk_tolerance = self.risk_tolerance.currentText()
        profile = InvestmentProfile(
            monthly_income=self.monthly_income.value(),
            monthly_expenses=self.monthly_expenses.value(),
            cash_savings=self.cash_savings.value(),
            debt_total=0.0,
            monthly_debt_payment=0.0,
            current_investments=self.current_investments.value(),
            target_emergency_months=DEFAULT_EMERGENCY_MONTHS,
            risk_tolerance=risk_tolerance,
            primary_goal=DEFAULT_PRIMARY_GOAL,
            goal_amount=self.goal_amount.value(),
            goal_years=self.goal_years.value(),
            expected_annual_return=RISK_RETURN_ASSUMPTIONS.get(risk_tolerance, 0.06),
        )
        self.memory.save_investment_profile(profile.to_record())
        QMessageBox.information(self, "HELIX", "Investment profile saved.")
        self.on_saved()


class WatchlistTab(QWidget):
    def __init__(self, memory: SQLiteMemory) -> None:
        super().__init__()
        self.memory = memory

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Symbol", "Thesis", "Target", "Max Allocation"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        editor = QGroupBox("Watchlist Item")
        form = QGridLayout(editor)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(14)
        self.symbol = QLineEdit()
        self.symbol.setPlaceholderText("VOO")
        self.thesis = QLineEdit()
        self.thesis.setPlaceholderText("Why this belongs on the list")
        self.target_price = money_box()
        self.max_allocation_pct = percent_box()

        form.addWidget(QLabel("Symbol"), 0, 0)
        form.addWidget(self.symbol, 0, 1)
        form.addWidget(QLabel("Thesis"), 0, 2)
        form.addWidget(self.thesis, 0, 3)
        form.addWidget(QLabel("Target price"), 1, 0)
        form.addWidget(self.target_price, 1, 1)
        form.addWidget(QLabel("Max allocation %"), 1, 2)
        form.addWidget(self.max_allocation_pct, 1, 3)

        actions = QHBoxLayout()
        actions.setSpacing(12)
        add = QPushButton("Add / Update")
        add.clicked.connect(self.save_item)
        remove = QPushButton("Remove Selected")
        remove.clicked.connect(self.remove_selected)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        actions.addStretch(1)
        actions.addWidget(refresh)
        actions.addWidget(remove)
        actions.addWidget(add)

        layout.addWidget(self.table, 1)
        layout.addWidget(editor)
        layout.addLayout(actions)

    def refresh(self) -> None:
        items = self.memory.list_watchlist()
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            self.table.setItem(row, 0, QTableWidgetItem(item["symbol"]))
            self.table.setItem(row, 1, QTableWidgetItem(item["thesis"]))
            self.table.setItem(row, 2, QTableWidgetItem(_money_or_blank(item["target_price"])))
            self.table.setItem(row, 3, QTableWidgetItem(_percent_or_blank(item["max_allocation_pct"])))

    def save_item(self) -> None:
        symbol = self.symbol.text().strip().upper()
        thesis = self.thesis.text().strip()
        if not symbol or not thesis:
            QMessageBox.warning(self, "HELIX", "Symbol and thesis are required.")
            return

        self.memory.upsert_watchlist_item(
            symbol=symbol,
            thesis=thesis,
            target_price=self.target_price.value() or None,
            max_allocation_pct=self.max_allocation_pct.value() or None,
        )
        self.symbol.clear()
        self.thesis.clear()
        self.target_price.setValue(0.0)
        self.max_allocation_pct.setValue(0.0)
        self.refresh()

    def remove_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        symbol_item = self.table.item(row, 0)
        if symbol_item is None:
            return
        self.memory.remove_watchlist_item(symbol_item.text())
        self.refresh()


class JournalTab(QWidget):
    def __init__(self, memory: SQLiteMemory) -> None:
        super().__init__()
        self.memory = memory

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["ID", "Type", "Title", "Created"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        editor = QGroupBox("Decision Entry")
        form = QFormLayout(editor)
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(14)
        self.entry_type = QLineEdit("investment")
        self.title = QLineEdit()
        self.body = QTextEdit()
        self.body.setMinimumHeight(120)
        form.addRow("Type", self.entry_type)
        form.addRow("Title", self.title)
        form.addRow("Body", self.body)

        actions = QHBoxLayout()
        actions.setSpacing(12)
        add = QPushButton("Add Entry")
        add.clicked.connect(self.add_entry)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        actions.addStretch(1)
        actions.addWidget(refresh)
        actions.addWidget(add)

        layout.addWidget(self.table, 1)
        layout.addWidget(editor)
        layout.addLayout(actions)

    def refresh(self) -> None:
        entries = self.memory.list_journal_entries(limit=25)
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self.table.setItem(row, 0, QTableWidgetItem(str(entry["id"])))
            self.table.setItem(row, 1, QTableWidgetItem(entry["entry_type"]))
            self.table.setItem(row, 2, QTableWidgetItem(entry["title"]))
            self.table.setItem(row, 3, QTableWidgetItem(entry["created_at"]))

    def add_entry(self) -> None:
        title = self.title.text().strip()
        body = self.body.toPlainText().strip()
        entry_type = self.entry_type.text().strip() or "investment"
        if not title or not body:
            QMessageBox.warning(self, "HELIX", "Title and body are required.")
            return
        self.memory.add_journal_entry(entry_type, title, body)
        self.title.clear()
        self.body.clear()
        self.refresh()


def money_box(default: float = 0.0) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(0.0, 1_000_000_000.0)
    box.setDecimals(2)
    box.setSingleStep(100.0)
    box.setPrefix("$")
    box.setValue(default)
    box.setMinimumHeight(42)
    return box


def percent_box(default: float = 0.0) -> QDoubleSpinBox:
    box = NoScrollDoubleSpinBox()
    box.setRange(0.0, 100.0)
    box.setDecimals(2)
    box.setSingleStep(1.0)
    box.setSuffix("%")
    box.setValue(default)
    box.setMinimumHeight(42)
    return box


def integer_box(minimum: int, maximum: int, default: int) -> QSpinBox:
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setValue(default)
    box.setMinimumHeight(42)
    return box


def apply_hud_style(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 12))
    app.setStyleSheet(
        """
        QMainWindow,
        QWidget {
            background-color: #061013;
            color: #e7fbff;
            font-size: 13pt;
        }

        QTabWidget::pane {
            background-color: #071417;
            border: 1px solid #1bbfe8;
            border-radius: 8px;
            top: -1px;
        }

        QTabBar::tab {
            background-color: #0b2026;
            color: #8eeaff;
            border: 1px solid #255b68;
            border-bottom-color: #1bbfe8;
            border-top-left-radius: 7px;
            border-top-right-radius: 7px;
            padding: 10px 18px;
            margin-right: 6px;
            font-weight: 700;
            min-width: 96px;
        }

        QTabBar::tab:selected {
            background-color: #102f38;
            color: #fff2c2;
            border-color: #ffbd3e;
        }

        QTabBar::tab:hover {
            background-color: #123a45;
            color: #ffffff;
        }

        QLabel {
            color: #dff9ff;
            font-size: 13pt;
        }

        QCheckBox {
            color: #eaffff;
            spacing: 8px;
            background: transparent;
        }

        /* Prominent, high-contrast toggles — the Hands-free wake word (Xpert), the Home auto-text
           reminder, and the Investment AI-research/cost toggle. Scoped by objectName so table
           task-checkboxes stay default. */
        QCheckBox#handsfreeToggle,
        QCheckBox#autoTextToggle,
        QCheckBox#aiResearchToggle {
            color: #ffc857;
            font-size: 14pt;
            font-weight: 800;
            padding: 4px 0;
        }

        QCheckBox#handsfreeToggle::indicator,
        QCheckBox#autoTextToggle::indicator,
        QCheckBox#aiResearchToggle::indicator {
            width: 22px;
            height: 22px;
            border: 2px solid #1dd8ff;
            border-radius: 5px;
            background-color: #081316;
        }

        QCheckBox#handsfreeToggle::indicator:hover,
        QCheckBox#autoTextToggle::indicator:hover,
        QCheckBox#aiResearchToggle::indicator:hover {
            border-color: #ffbd3e;
            background-color: #0b1d22;
        }

        QCheckBox#handsfreeToggle::indicator:checked,
        QCheckBox#autoTextToggle::indicator:checked,
        QCheckBox#aiResearchToggle::indicator:checked {
            background-color: #ffbd3e;
            border-color: #ffbd3e;
        }

        QCheckBox#handsfreeToggle::indicator:checked:hover,
        QCheckBox#autoTextToggle::indicator:checked:hover,
        QCheckBox#aiResearchToggle::indicator:checked:hover {
            background-color: #ffd06a;
        }

        QLabel#sectionHeader {
            color: #ffc857;
            font-size: 24pt;
            font-weight: 800;
            padding: 2px 0 10px 0;
            border-bottom: 2px solid #1dd8ff;
        }

        QGroupBox {
            background-color: #09181c;
            border: 1px solid #286979;
            border-radius: 8px;
            color: #ffc857;
            font-size: 14pt;
            font-weight: 800;
            margin-top: 14px;
            padding: 14px;
        }

        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 14px;
            padding: 0 8px;
            background-color: #061013;
        }

        QLineEdit,
        QTextEdit,
        QDoubleSpinBox,
        QSpinBox,
        QComboBox {
            background-color: #081316;
            color: #f3fdff;
            border: 1px solid #2c6574;
            border-radius: 6px;
            padding: 8px 10px;
            selection-background-color: #ffbd3e;
            selection-color: #081316;
            min-height: 34px;
        }

        QTextEdit {
            font-family: Consolas, "Segoe UI";
            font-size: 14pt;
        }

        QTextEdit#briefingPanel {
            background-color: #061013;
            border: 2px solid #1dd8ff;
            color: #eaffff;
            font-size: 15pt;
            padding: 12px;
        }

        QLineEdit:focus,
        QTextEdit:focus,
        QDoubleSpinBox:focus,
        QSpinBox:focus,
        QComboBox:focus {
            border: 2px solid #ffbd3e;
            background-color: #0b1d22;
        }

        QComboBox::drop-down {
            border-left: 1px solid #2c6574;
            width: 30px;
        }

        QPushButton {
            background-color: #11333c;
            color: #f1fcff;
            border: 1px solid #1dd8ff;
            border-radius: 6px;
            padding: 10px 20px;
            font-size: 13pt;
            font-weight: 800;
            min-height: 38px;
        }

        QPushButton:hover {
            background-color: #175161;
            border-color: #ffbd3e;
            color: #fff6d6;
        }

        QPushButton:pressed {
            background-color: #ffbd3e;
            color: #061013;
        }

        /* Compact buttons embedded in table rows (e.g. Assets → Details) — no tall min-height. */
        QPushButton#rowButton {
            min-height: 0;
            padding: 3px 14px;
            font-size: 11pt;
            font-weight: 700;
        }

        QTableWidget {
            background-color: #071417;
            alternate-background-color: #0c2026;
            border: 1px solid #286979;
            border-radius: 6px;
            color: #e7fbff;
            gridline-color: #214d58;
            font-size: 13pt;
            selection-background-color: #164653;
            selection-color: #ffffff;
        }

        QHeaderView::section {
            background-color: #102f38;
            color: #ffc857;
            border: 0;
            border-right: 1px solid #286979;
            border-bottom: 1px solid #286979;
            padding: 10px;
            font-size: 13pt;
            font-weight: 800;
        }

        QTableCornerButton::section {
            background-color: #102f38;
            border: 0;
        }

        QStatusBar {
            background-color: #061013;
            color: #8eeaff;
            border-top: 1px solid #286979;
            font-size: 12pt;
        }

        QScrollBar:vertical {
            background-color: #071417;
            width: 16px;
            margin: 0;
        }

        QScrollBar::handle:vertical {
            background-color: #1e7f94;
            border-radius: 6px;
            min-height: 36px;
        }

        QScrollBar::handle:vertical:hover {
            background-color: #1dd8ff;
        }

        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {
            height: 0;
        }

        QMessageBox {
            background-color: #061013;
        }
        """
    )


def _to_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _money_or_blank(value: object) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _percent_or_blank(value: object) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return str(value)
