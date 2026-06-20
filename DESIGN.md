# HELIX — Design & Architecture

**Single source of truth for how HELIX is built.** This document describes what actually
exists in the code today, not just the long-term vision. When the code and this document
disagree, fix one of them — keep them in sync.

- **Version:** 0.1.0 (see `helix/__init__.py`)
- **Last reviewed:** 2026-06-05
- **Stack:** Python 3.11+, standard library, PyQt6. Optional: **edge-tts** (free neural TTS for the Xpert voice; falls back to the built-in OS voice without it) and **faster-whisper** (free, local, private speech-to-text for the Xpert two-way voice assistant, §23; the app shows an install hint and you can type instead without it)
- **Platform of record:** Windows (developed on Win11), but pure-Python and portable

---

## 1. What HELIX is

**HELIX = Home · Enterprise · Learning · Investment · eXpert.**

A local-first personal AI platform organized around four "pillars." The product strategy is
**Investment-first**: build the investing pillar into something genuinely useful (and
self-funding), then expand into the other pillars. Everything runs on the user's own machine;
data and secrets never leave the device except for explicit, opt-in calls to the Claude and
Alpaca APIs.

### Pillar build status

| Pillar         | Status            | Where it lives |
|----------------|-------------------|----------------|
| **Investment** | ✅ Built           | `helix/investment/`, `helix/brokers/`, Investment tab |
| **Learning**   | ✅ Built (AI research, mock + live Claude) | `helix/ai/`, Learning tab |
| **Home**       | 🟡 Built — checklist + AI tips + SMS reminders | `HomeTab`: check-off checklist with due/overdue by frequency, a Claude **"save time & money"** suggestion dialog, and free **email-to-SMS** reminders (`helix/home/`). Smart grocery reorder still to come. |
| **Enterprise** | 🟡 Built — Slack + git work (§26) | `EnterpriseTab`: recent **work shipped** (git history across your projects) + a Slack **"needs attention"** digest, summarized by Claude. `helix/enterprise/` |

The "Expert" in the name is the cross-cutting idea: each pillar accumulates structured
**memory** and uses AI to reason over it.

---

## 2. Architecture at a glance

HELIX is a layered monolith. Interfaces sit on top of a thin core; pillar logic and external
integrations are separate modules the interfaces compose.

```
                 ┌─────────────────────────────────────────────┐
   Interfaces    │  CLI (argparse)   HTTP API   PyQt6 desktop UI │
                 └───────┬───────────────┬──────────────┬───────┘
                         │               │              │
                 ┌───────▼───────────────▼──────────────▼───────┐
   Pillars / AI  │  investment/ (planner, models, cli)           │
                 │  ai/ (claude, mock, research)                 │
                 │  brokers/ (alpaca)                            │
                 └───────────────────────┬───────────────────────┘
                                         │
                 ┌───────────────────────▼───────────────────────┐
   Core          │  config (paths)  settings (JSON)  memory (SQLite)  daemon (loop) │
                 └───────────────────────┬───────────────────────┘
                                         │
                 ┌───────────────────────▼───────────────────────┐
   Storage       │  data/helix.db (SQLite)   data/helix_settings.json │
                 └─────────────────────────────────────────────────┘
```

**Dependency direction:** interfaces → pillars/AI/brokers → core → storage. Nothing in `core`
imports from interfaces or pillars. The data layer (`SQLiteMemory`) is the canonical store of
record; it is referred to throughout the UI as HELIX's "memory."

**Key principle:** stdlib-only networking. Both the Claude client and the Alpaca client are
hand-written on `urllib.request` (see `helix/ai/claude.py`, `helix/brokers/alpaca.py`). The
only third-party package is PyQt6, and only the desktop UI needs it — the CLI, API, daemon,
and all pillar logic run on a bare Python install.

---

## 3. Module reference

### `main.py`
Entry point. Delegates straight to `helix.interfaces.cli:main`.

### `helix/core/` — foundation
| File | Responsibility |
|------|----------------|
| `config.py` | `HelixConfig` (frozen dataclass) resolves `root_dir`, `data_dir`, `db_path`, `settings_path`. `load_config()` creates `data/` if missing. Paths are derived relative to the repo root. |
| `settings.py` | `AppSettings` — a tiny JSON key/value store backed by `data/helix_settings.json`. `get/set/remove/load/save`. Tolerates missing/corrupt files by returning `{}`. Holds API keys and UI preferences. |
| `memory.py` | `SQLiteMemory` — the system of record. Creates the schema on init and exposes typed accessors for the tables (see §5), including `record_equity`/`list_equity_history` for the equity curve (§19) and `cached_ratings` for the rating cache / cadence (§14). |
| `conversation.py` | `ConversationStore` — SQLite-backed persistence for the JARVIS conversation so HELIX retains context across restarts (§5). Self-contained: owns its own tables (`conversation_history`, `sessions`, `session_summaries`) in the same `data/helix.db`. Resumes the most recent session on launch, writes each turn immediately (`append_turn`), rebuilds the in-memory Claude buffer (`load_recent_messages`/`get_recent_history`), and summarizes a session (last assistant line, ≤200 chars) on "New chat" or after 200 turns. Wired into `XpertTab`. |
| `daemon.py` | `run_core()` — the `run` command's loop: prints the investment briefing, sleeps `interval` seconds, repeats until Ctrl+C (or one pass if `--once`). |

### `helix/investment/` — the Investment pillar
| File | Responsibility |
|------|----------------|
| `models.py` | `InvestmentProfile` (frozen dataclass) + `RISK_LEVELS = (conservative, balanced, growth, aggressive)`. `from_record`/`to_record` map to/from the DB row. |
| `planner.py` | Pure financial math (no I/O): builds the `InvestmentBriefing`, projects future value, computes the required monthly contribution, and selects an allocation model. See §6. |
| `cli.py` | `investment` argparse subcommands: `profile` (interactive), `status`, `watchlist add/list/remove`, `journal add/list`. |
| `autopilot.py` | Invest engines. **v1 buy-only:** `TradeProposal`/`InvestPlan`, `build_plan()`, `execute_plan()`, `render_plan()` (§12.8). **v2 active rebalance:** `RebalanceAction`/`RebalancePlan`, `build_rebalance_plan()` (buy/sell/trim/exit; rating-cache aware via `rating_max_age_days`, §14), `execute_rebalance()`, `render_rebalance_plan()`, `merge_universe()` (§13). **Dashboard:** `PortfolioSnapshot`/`portfolio_snapshot()` (§14). **Equity curve:** `EquitySeries` + `parse_portfolio_history()` (Alpaca) / `equity_series_from_rows()` (local DB) + `parse_stock_bars()`/`benchmark_series()` (S&P 500 overlay) — pure, source-agnostic (§19). **HELIX 100:** `build_roster_review()` + `RosterReview`/`render_roster_review()` + `normalize_roster()` + `maybe_rotate_roster()` (calendar-gated auto-rotation) (§20). **Special Stocks:** `maybe_research_special()` + the carve-out in `build_rebalance_plan` (§21). **Prediction scorecard (§28):** `RatingOutcome`, `bars_to_dated_closes()`, `score_rating_snapshots()`, `summarize_rating_outcomes()`, `render_rating_scorecard()`, `generate_rating_scorecard()` (edge). |
| `backtest.py` | **Backtest harness (§29):** replays real daily bars through `build_rebalance_plan` with ratings held fixed (deterministic, no Claude). `run_backtest()` (pure replay), `render_backtest()` (conviction-vs-equal-weight-vs-S&P comparison + verdict), `BacktestResult`, `_PriceBook`; `gather_backtest()` is the edge orchestrator (reads buy ratings, fetches bars, runs both legs). |
| `market_data.py` | Live market context for the AI prompts (§25): `technical_line()`/`technicals_by_symbol()` (price/momentum/trend from bars), `news_by_symbol()`, `build_market_context()` (now also folds in a fundamentals section), `factor_signals()` (§30 momentum rank), `volatility_signals()` (§31), `regime_risk_off()` (§35 market trend), `liquidity_metrics()` (§37 price + dollar volume) — pure, no I/O. |
| `fundamentals.py` | **Fundamentals input (§32):** pull real financials free from **SEC EDGAR** (XBRL frames API, keyless). Pure: `parse_cik_map()`, `parse_frame()`, `extract_metrics()` (revenue growth / margins / ROE / leverage), `fundamentals_line()`, `fundamental_score()`, `fundamentals_block()`; edge: `sec_get()` + `fetch_fundamentals(symbols, get_fn=…)` (bulk, best-effort, injectable for tests). |
| `sectors.py` | **Sector data for the sector cap (§35):** a curated `SECTOR_MAP` (ticker → GICS-style sector) + `sector_of()`/`sectors_for()` (pure), **plus SEC enrichment** — `sic_to_sector()` (SIC→sector, pure) and `fetch_sectors(symbols, get_fn=…)` (pull SIC from SEC submissions for the curated map's gaps, injectable for tests). Curated map wins; unresolved names are exempt. |

### `helix/ai/` — Learning pillar AI
| File | Responsibility |
|------|----------------|
| `research.py` | `build_research_prompt()` (10-section memo); `build_portfolio_research_prompt()` + `parse_research_json()` — the per-ticker buy/watch/skip ratings the invest loop + re-rationalize use; this prompt **explicitly targets long-term (months–years) account growth** and is fed the realized track record. `build_roster_score_prompt()` (score a chunk of roster names) + `build_roster_discovery_prompt()` (propose new candidates vs the weakest) + `parse_roster_review_json()` — the **chunked** HELIX-100 review that scores the full ~480 universe + discovers replacements (§20). `build_special_research_prompt()` + `parse_special_research_json()` — the high-risk Special Stocks scout (§21). `build_adversarial_prompt()` + `parse_adversarial_json()` — the bull/bear/judge stress-test of top buy candidates (§34). `build_jarvis_chat_system()` — the JARVIS chat system prompt for the two-way voice assistant (§23). |
| `claude.py` | `ClaudeClient` — Anthropic Messages API over `urllib`. `complete()` (single-shot) and `chat(messages, system, tools)` (multi-turn + tool-use, with **prompt caching** on the system prompt + tools, §23) share a `_post()` helper. Models: `DEFAULT_CLAUDE_MODEL` (Opus) and cheap `DEFAULT_RESEARCH_MODEL` (Sonnet). Captures `last_usage`; `estimate_cost()` for the scorecard. Key order: arg → `ANTHROPIC_API_KEY` env → saved setting. |
| `transcribe.py` | `transcribe(audio_path)` + `is_available()` — **local faster-whisper** speech-to-text for the Xpert voice assistant (§23). Optional dependency, lazy-imported with a graceful fallback (mirrors `speech.py`); caches the model. Audio never leaves the machine. **`prewarm()` + `is_ready()` (§23 crash fix):** `main.py` calls `prewarm()` to build the ctranslate2 model **before PyQt6 is imported anywhere in the process** — even a bare `import PyQt6` *before* the model loads segfaults the process (native Qt↔ctranslate2 conflict, access-violation `0xC0000005`). So `main.py` pre-warms first, then imports the CLI/UI (which pull in PyQt6); `run_qt_app` only reports readiness (it's already too late to load there). If Qt is already loaded — e.g. a debugger whose qt-support imports PyQt before `main.py` runs — the pre-warm is **skipped** so the app still starts, `is_ready()` is then False, and the Xpert voice paths (push-to-talk + hands-free) disable themselves rather than attempt a crashing post-Qt load. |
| `actions.py` | The Xpert **action/tool-use layer** (§24): `XPERT_TOOLS` (Claude tool schemas), `ActionContext` + `ActionRouter` (maps spoken commands to real engine/memory functions; Qt-free, unit-testable), `run_chat_turn()` (drives the multi-turn tool loop), and the deterministic spoken-confirmation gate (`is_affirmative`/`is_negative`) for money/outward actions. |
| `mock.py` | `generate_mock_research()` (memo), `generate_mock_portfolio_research()` (structured JSON), `generate_mock_roster_review()` (§20), `generate_mock_special_research()` (§21) — exercise the full workflows with zero API spend. |
| `speech.py` | `synthesize_speech()` — text → MP3 via **edge-tts** (free Microsoft neural voices, default `en-GB-RyanNeural`, **~1.5× speed** via `DEFAULT_RATE` = `+50%`). Runs on a worker thread; the Xpert tab plays it via `QMediaPlayer` and falls back to the OS voice (also sped up) if unavailable. |

### `helix/brokers/` — broker integration
| File | Responsibility |
|------|----------------|
| `alpaca.py` | `AlpacaClient` over `urllib`. `AlpacaCredentials` picks the Paper vs Live base URL. Methods: `get_account`, `get_clock` (market open?), `get_calendar(start, end)` (official trading-day schedule with ET open/close, for the Market schedule popup, §16), `get_positions`, `get_open_orders`, `get_assets` (the tradeable market universe / screener, §36), `get_portfolio_history` (equity time series for the curve, §19), `get_stock_bars` (market-data OHLC on `data.alpaca.markets`, `feed=iex`, for the S&P 500 benchmark line), `submit_order` (shares **or** notional dollars, never both). `from_settings()` builds it from saved credentials. |

### `helix/home/` — the Home pillar
| File | Responsibility |
|------|----------------|
| `tasks.py` | Pure checklist logic (no Qt): `freq_to_days`, `task_status` (On track / Due soon / Due now / Overdue from frequency vs. last-done), `due_tasks`, `reminder_message`, `HOME_TASKS_SETTING`. Shared by the GUI checklist and the headless notifier. |
| `notify.py` | Free **email-to-SMS** reminders (§22): `send_text_via_email` (Gmail SMTP, stdlib, injectable for tests), `gateway_address` + `CARRIER_GATEWAYS`/`CARRIER_CHOICES`, `send_reminder`, `is_configured`, `sms_config`. SMS settings keys + `DEFAULT_SENDER`. |

### `helix/enterprise/` — the Enterprise pillar (§26)
| File | Responsibility |
|------|----------------|
| `slack.py` | `SlackClient` — a read-only `urllib` client over the Slack Web API (mirrors `AlpacaClient`): `auth_test`, `list_conversations`, `conversations_history`, `users_info`. `gather_slack_digest()` assembles a bounded "needs attention" digest (mentions / DMs / busy channels); `format_slack_digest()` renders it. `SLACK_TOKEN_SETTING`, `SLACK_USER_SCOPES`. No posting (read-only). |
| `gitwork.py` | Pure, local git-history reader (`subprocess` `git log`, **never** pull/fetch/mutate): `repo_summary()` / `gather_git_digest()` parse recent commits + line churn per repo; `format_git_digest()` renders it; `parse_repos()`, `ENTERPRISE_REPOS_SETTING`. |

### `helix/selfdev/` — the self-improvement loop (HELIX builds HELIX)
| File | Responsibility |
|------|----------------|
| `coder.py` | Runs **Opus 4.8** to edit HELIX's own code. `resolve_claude_cli()` finds the desktop `claude.exe` (newest under `%APPDATA%\Claude\claude-code\`); `run_coding_task()` makes a `selfdev/*` branch, runs `claude -p` headless (authenticated by HELIX's Anthropic key via `ANTHROPIC_API_KEY` — the interactive desktop login does **not** carry into a subprocess), commits the edit to the branch, then switches the tree **back to the deployed branch** so a restart can't load unapproved code. Returns a `CoderResult` (branch, commit, diff, summary, cost). |
| `gitops.py` | Stdlib git **write** ops scoped to throwaway `selfdev/*` branches (branch / stage / diff / commit / `merge_to` main with `--no-ff` / add+remove **worktree** for isolated smoke-checks). `main` is never modified without explicit approval; deleting a branch discards the work. Mirrors `gitwork.py`'s `_git` style but **raises** `GitError` on failure (writes are never silent). |
| `engine.py` | Approval + merge. A draft is recorded as a **pending** change (settings); `smoke_check()` imports the branch's code in an isolated git **worktree** (no disturbance to the live tree); `approve()` smoke-checks then **merges to main** (and flags a restart); `reject()` deletes the branch. |
| `restart.py` | Deliberate self-restart so a merge goes live: a settings flag + `RESTART_EXIT_CODE` (42) that `scripts/run_helix.py` relaunches on. The main window restarts on a safe tick — never mid trade-cycle. |
| `triggers.py` | Crash triggers: parse tracebacks from `data/helix.log`, de-dup by signature, draft a fix via the coder, record pending (**never auto-merged**). `selfdev_autofix_crashes` (default on); app auto-checks ~2 min after launch + every 6 h. |
| `mailer.py` | Email approval: `notify_drafted()` emails the diff (Gmail SMTP); `poll_replies()` reads Yes/No replies over IMAP and applies `approve`/`reject` (matched by the `[HELIX selfdev <branch>]` token, from `selfdev_notify_email`). App polls every 3 min. |

**Status:** built + proven live, voice- AND email-driven end to end. **Triggers:** spoken/typed (`improve_helix` = "HELIX, build X"), or a **logged crash** (auto-drafts a fix, deduped). **Approve** by voice (`approve_change` = "ship it" / `reject_change` / `list_pending_changes` / `fix_recent_crashes`) or by **email reply** (Yes/No to the diff HELIX emails you). **On approval:** smoke-check (isolated worktree) → merge → **auto-restart** when trading is idle (the supervisor relaunches). **Auth:** the Claude **subscription** token (`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`) is preferred over the API key. **Planned next:** an Enterprise-tab panel for pending changes; then the vision/hardware faculties.

### `helix/vision/` — HELIX's eyes (the first embodied faculty of X)
| File | Responsibility |
|------|----------------|
| `camera.py` | Capture one JPEG via **OpenCV** from a **USB index or a stream URL** (RTSP/HTTP), plus a **named-camera registry** (add/list/remove/`capture_named`) — the "eyes" around the house (fridge, laundry, door…). Optional/guarded dep; coexists with Qt. |
| `analyze.py` | `describe_image(frame, question)` → **Claude vision** (`ClaudeClient.vision()`): one generic prompt answers ANY visual question — tool how-to, appearance-only person description, "what's in the fridge", "is the light on". Records AI usage. |

Generic by design — a camera is just an eye; ask anything. Surfaced via Xpert tools: **`look`** (one eye + a question; defaults to the first/built-in camera), **`look_around`** (all eyes, answered across the house), **`add_camera`**, **`list_cameras`**. The person-privacy guardrail (appearance only, no identity/web) is baked into the prompt. Verified live. OpenCV is an optional, guarded dependency (like edge-tts/faster-whisper). **Planned next:** always-on door/security monitoring, local known/unknown face recognition, and (privacy-gated) online profiling; then hardware I/O (`helix/devices/`).

### `helix/interfaces/` — the three front ends
| File | Responsibility |
|------|----------------|
| `cli.py` | `main()` + argparse router. **With no arguments, launches the UI.** Subcommands: `brief`, `run`, `api`, `ui`, `invest`, `rebalance`, `autopilot`, `roster`, `scorecard`, `backtest`, `investment …`. Builds `SQLiteMemory` once and passes it to handlers. |
| `api.py` | `HelixApiServer` — a `ThreadingHTTPServer`. **Read-only** JSON endpoints: `/health`, `/brief`, `/watchlist`. Intended for other devices on the LAN to read the briefing. |
| `qt_app.py` | The PyQt6 desktop app — main window, five top-level tabs, the cyan/amber "HUD" stylesheet, and all investment/learning widgets. Includes `EquityCurveWidget`, a custom `QPainter` equity sparkline (no charting dependency, §19). This is the largest file and the primary UI. |

---

## 4. Runtime & entry points

All commands run through `python main.py <command>`.

| Command | Effect |
|---------|--------|
| `python main.py` *(no args)* | Opens the desktop UI (`ui`). |
| `python main.py ui` | Opens the PyQt6 desktop UI. Prints an install hint if PyQt6 is missing. |
| `python main.py brief` | Prints the current investment briefing once and exits. |
| `python main.py run [--interval 3600] [--once]` | Runs as an all-day local process, reprinting the briefing each interval (min 10s). |
| `python main.py api [--host 127.0.0.1] [--port 8765]` | Serves the read-only HTTP API. |
| `python main.py investment profile` | Interactive prompt to create/update the full investment profile. |
| `python main.py investment status` | Prints the briefing. |
| `python main.py investment watchlist list \| add \| remove` | Manage watchlist tickers. |
| `python main.py investment journal add \| list` | Manage the decision journal. |
| `python main.py invest [--cash N --preset Balanced\|Aggressive --ai mock\|claude --model M --execute]` | Build a paper invest plan from the watchlist; dry-run unless `--execute`. See §12.8. |
| `python main.py rebalance [--preset --ai --model --max-pos --cash-buffer --execute]` | Plan/execute a buy+sell rebalance from live Alpaca positions; dry-run unless `--execute`. See §13. |
| `python main.py autopilot [--interval --ai --model --max-pos --cash-buffer --once --ignore-hours --allow-live]` | Run the rebalance loop automatically on a timer. Paper-only unless `--allow-live`. See §14. |
| `python main.py roster [--ai --model --candidates --max-swaps --min-margin --apply]` | Review/rotate the **HELIX 100** universe (discover + rank + swap). Dry-run unless `--apply`. See §20. |
| `python main.py scorecard [--days N]` | Print the **prediction scorecard** — realized forward returns by rating confidence, vs the S&P 500. Deterministic; no Claude call, no trading. See §28. |
| `python main.py backtest [--days N --cadence-days N --cash-buffer P]` | **Backtest** the deterministic strategy on real history — conviction-weighted vs equal-weight vs S&P (Sharpe / drawdown / alpha). Deterministic; no Claude call, no trading. See §29. |
| `python main.py notify [--always]` | Text your due/overdue Home tasks to your phone via email-to-SMS. Sends nothing if nothing's due (override `--always`). For Windows Task Scheduler. See §22. |

### Desktop UI map (`qt_app.py`)
- **The app opens on the Console** (the JARVIS overhaul — see `BLUEPRINT.md`): one surface with the **Presence orb** (`PresenceOrb`, an animated state indicator), the **conversation** (the Xpert assistant, promoted to the whole screen), and four **ambient tiles** (`AmbientTile`: House · Money · Supplies · Self) that glance at local engine state and open the matching deep view on click. A **More** button reveals the demoted **tab drawer** — the four deep domain views **Home · Enterprise · Learning · Investment** (Xpert now lives *in* the Console). The Console also runs a proactive **door watch** (local motion → one vision call → spoken alert) and feeds the grocery **shopping list / Supplies tile**.
- **Xpert** → `XpertTab`: a **focused, two-way J.A.R.V.I.S. voice assistant** — the panel is just the conversation now. You **talk** (push-to-talk 🎤, or hands-free wake word **"HELIX"**) or **type**; HELIX **thinks with Claude** (carrying live context — portfolio, track record, due Home tasks) and **performs real actions** across the pillars (§24), replying aloud in a natural neural voice (**edge-tts**, default `en-GB-RyanNeural`, played through `QMediaPlayer`; falls back to the built-in OS voice). Controls: a scrolling **transcript**, **New chat**, the **Hands-free** toggle + mic-level meter, a **Voice-speed slider** (0.8×–2.0×, persisted), and **Mic/Speaker pickers** (default to the system default, e.g. a Bluetooth headset). A one-line status under it shows the live balance. (The old one-way **Expert Opinion** briefing and the H/E/L/I/X **pillars table** were removed — the conversation supersedes them; the AI context they fed still loads via `_gather`/`_context`. Full spec: §23–§24. It also replaced the old `DashboardTab`/"Overview", which has now been removed.)
- **Home** → `HomeTab`: an **interactive checklist** of household tasks. Each task has a **Done**
  checkbox, an editable Action / Item / Frequency, and a derived **Status** (On track / Due soon /
  **Due now** / **Overdue**) + **Last done** date — computed from the frequency (`FREQ_DAYS`) vs. when
  you last checked it. Checking a task stamps "done today" and resets its clock; a summary line counts
  what's **due/overdue**. Persisted to settings (`home_tasks`, now `[action, item, frequency,
  last_done]`, back-compatible with the old 3-field rows). A **Text reminders** box (free Gmail
  email-to-SMS) texts your due/overdue tasks to your phone, with a **Send test text** button and an
  **auto-text toggle + adjustable interval** ("every N hour(s)") that texts your due tasks on a timer
  while the app is open — only when something is actually due (§22). (The old "Suggest ways to save
  time & money" Claude button was removed — that's a question you now just ask the Xpert assistant.)
  **Planned:** smart grocery reorder (one-tap, not autonomous spend).
- **Enterprise** → `EnterpriseTab`: a **work command center** (§26) led by **self-improvements awaiting
  approval** — each pending change (from the §selfdev loop) shows with **View / Approve & merge / Reject**
  (approve runs the smoke-check + merge off-thread), plus a **Check for crashes** button. Below that, a
  **Refresh and summarize** button pulls recent **git commits** across your project folders (read-only
  `git log`, never a pull) and your **Slack** mentions/DMs, then Claude writes a **terse three-line**
  digest (Shipped · Needs you · Next). ⚙ Settings holds the Slack user token + the project repo paths
  (both git-ignored). Off-thread.
- **Learning** → `LearningTab`: a **Streams** table of the three pillars (Home, Enterprise,
  Investment); each row's **Open** button shows a `PillarDialog` — Investment = the **stored pick
  logic** read from the `stock_rationale` table (Symbol / Action / Conf / Why / Updated), captured
  automatically whenever HELIX reviews the stocks, with a **Re-rationalize with Claude** button to
  refresh it on demand (no more post-purchase "Generate"), plus a **Recent sells** table (from
  `sell_log`) showing what HELIX exited or trimmed and why. Home/Enterprise = roadmap placeholders.
  Plus a **Claude** sub-tab for ad-hoc research (Mock or Claude API). The old Viewer sub-tab was removed.
- **Investment** → `InvestmentTab` (a thin wrapper around `InvestTab`) — **one simple screen, no
  sub-tabs** (§16): Alpaca keys + Save, money to put in, fake/real toggle, a **Stocks To Trade**
  table, **START / STOP**, a live balance line, and an **Assets** holdings table. Strategy knobs
  (posture, caps, model, interval) are baked
  to defaults and hidden. The old sub-tab classes (`InvestmentMoneyTab`, `AlpacaTab`, `WatchlistTab`,
  `JournalTab`, `ProfileTab`) remain defined but are no longer mounted.

---

## 5. Data model

### SQLite — `data/helix.db` (via `SQLiteMemory`)
Three tables, created idempotently on startup:

- **`investment_profile`** — single row, enforced by `CHECK (id = 1)`. Upserted on conflict.
  Holds income, expenses, cash, debt, current investments, emergency-fund target months, risk
  tolerance, primary goal, goal amount, goal years, and expected annual return (stored as a
  decimal fraction, e.g. `0.07`).
- **`watchlist`** — keyed by `symbol`. Columns: `thesis`, `target_price`, `max_allocation_pct`,
  timestamps. Upsert on conflict; symbols stored uppercased.
- **`journal`** — append-only decision log. Columns: `entry_type`, `title`, `body`, `created_at`.
  Entry types include investment notes, saved AI research (`research`), and trade records
  (`paper_trade`, `live_trade`). Trade titles are `'<Mode> <side> <SYMBOL>'`, so
  `list_symbol_trades(symbol)` can pull one ticker's buy/sell history for the Assets → Details
  popup (§16).
- **`stock_rationale`** — the "why" behind each pick, keyed by **`(symbol, action)`** (`confidence`,
  `rationale`, `updated_at`). Upserted by `build_rebalance_plan(..., memory=...)` and the scouts every
  cycle; shown in the Learning → Investment dialog and the Research log; re-rationalizable on demand.
  **Composite key (symbol + action):** a symbol can carry a separate rationale per sleeve — so the core
  re-rate no longer overwrites a name's day-trade/special thesis (the bug that left the Day-trade
  research log empty, since day-trade names are mostly large-caps also in the core). `cached_ratings`
  reads only the `buy/watch/skip` (core) rows as the trade cache. Migrated in `_migrate` from the old
  symbol-only PK, rows preserved.
- **`sell_log`** — history of sells/trims with reason, rationale, and the **realized outcome**
  (`return_pct`, `realized_pl`, captured from the position's P&L at sell time): columns `symbol`,
  `reason`, `rationale`, `amount_usd`, `return_pct`, `realized_pl`, `created_at`.
  `strategy_performance()` aggregates it into hit-rate / avg-return / realized P/L. Shown as
  "Recent sells" (with a Result column + track-record line) in the Learning → Investment dialog.
- **`equity_history`** — HELIX's own account-equity samples over time (`equity`, `cash`,
  `market_value`, `unrealized_pl`, `created_at`). Written by `record_equity()` on each portfolio
  refresh/cycle (throttled — skips if a sample was taken in the last 10 min, so frequent refreshes
  don't flood it). `list_equity_history(days)` reads it oldest-first for the equity curve (§19) and
  as a cheap local signal the AI layer can read without hitting Alpaca.
- **`rating_outcomes`** — **append-only** snapshot of every rating (`symbol`, `action`, `confidence`,
  `rationale`, `created_at`), the historical counterpart to current-state `stock_rationale`. Written
  by `record_rating_snapshots()` on every genuine re-rate (alongside `save_stock_rationales`), seeded
  once from `stock_rationale` (`_seed_rating_outcomes`). This is the dataset the **prediction
  scorecard** (§28) scores for per-pick forward returns bucketed by confidence. `list_rating_snapshots(days, actions)`
  reads it; `rating_snapshot_summary()` gives the header counts.
- **`fundamentals`** — **current-state** cache (one upserted row per symbol: `metrics` JSON +
  `fetched_at`) of SEC-derived fundamentals (revenue growth, margins, ROE, leverage; §32). Written
  ~monthly by `upsert_fundamentals()`, read locally each re-rate by `get_fundamentals(symbols)`;
  `fundamentals_summary()` reports coverage/freshness. Not time-pruned (current-state).
- **`sectors`** — **current-state** cache (`symbol` → `sector` + `fetched_at`) of SEC-SIC-derived
  sectors for the sector cap (§35), for names the curated `SECTOR_MAP` misses. Written on a long
  cadence by `upsert_sectors()`, read by `get_sectors(symbols)`. Not time-pruned (SIC is ~static).
- **`market_assets`** — the real **tradeable market universe** (§36): one column (`symbol`), a weekly
  full-snapshot of Alpaca's tradeable/fractionable assets via `replace_market_assets()`, read by
  `get_tradable_universe()`. Discovered names (core rotation, Special, Day-trade) are validated against
  it so no hallucinated/un-buyable ticker enters a sleeve. Not time-pruned (snapshot-replaced).
- **Retention** — `prune_old_data()` runs on every startup and keeps a **rolling 1-year window** of
  the time-series tables (`journal`, `sell_log`, `ai_usage`, `equity_history`, `rating_outcomes`), deleting older rows. `stock_rationale`
  , `fundamentals` and `sectors` are current-state (one upserted row per symbol) so they are not time-pruned. This bounded history is
  the dataset the Xpert opinion reviews to calibrate the strategy.
- **`ai_usage`** — per-call Claude token usage + estimated cost (`model`, `input_tokens`,
  `output_tokens`, `est_cost`, `created_at`). Powers the invest scorecard's monthly-spend figure.
- **`conversation_history`** — the persisted JARVIS conversation so HELIX retains context across
  restarts (managed by `ConversationStore`, not `SQLiteMemory`). Columns: `id`, `timestamp`, `role`
  (`user`/`assistant`), `content`, `session_id`. One row per turn, written immediately so a crash
  loses at most the in-flight turn. On startup the most recent session's last ~50 turns rebuild the
  in-memory Claude buffer.
- **`sessions`** — one row per conversation session (`session_id` UUID, `created_at`, `last_active`).
  The most recent is resumed on launch; "New chat" and a >200-turn roll-over mint a fresh one.
- **`session_summaries`** — a one-line summary (the last assistant message, ≤200 chars) written when
  a session ends or exceeds 200 turns, so very old context stays scannable without ballooning the
  prompt. Columns: `id`, `session_id`, `summary`, `created_at`.

### JSON — `data/helix_settings.json` (via `AppSettings`)
Flat key/value store. Known keys:

| Key | Meaning |
|-----|---------|
| `claude_api_key` | Locally saved Anthropic key (env var overrides it). |
| `learning_ai_mode` | `Mock Claude` or `Claude API`. |
| `alpaca_api_key`, `alpaca_secret_key` | Alpaca credentials. |
| `alpaca_environment` | `Paper` or `Live`. |
| `investment_amount` | Default investable amount used by the Money tab and order ticket. |
| `enterprise_slack_token` | Slack **user token** (`xoxp-…`) for the Enterprise digest (§26). |
| `enterprise_git_repos` | Newline/comma-separated local project repo paths for the git-work summary (§26). |
| `enterprise_since_days` | Look-back window (days) for the Enterprise git/Slack digest. |

*(plus the Investment keys: `invest_tickers`, `invest_research_tokens`, `invest_special_*`,
`invest_core_rating_days`, etc., described inline in §16/§21/§25.)*

---

## 6. Investment planning logic (`planner.py`)

The briefing is derived deterministically from the profile — there is no hidden state:

1. **Monthly surplus** = income − expenses − debt payment.
2. **Emergency target** = (expenses + debt payment) × target months.
3. **Emergency gap** = max(0, target − cash savings).
4. **Investable cash now** = max(0, cash savings − emergency target).
5. **Monthly investment target** — behavioral rule:
   - surplus ≤ 0 → **$0** (stabilize first),
   - still building the emergency fund → **20%** of surplus,
   - emergency fund full → **80%** of surplus.
6. **Projected goal value** — future value of current investments compounding plus the monthly
   target as an annuity, at the expected annual return over the goal horizon.
7. **Required monthly contribution** — the annuity payment needed to hit the goal amount from
   today's balance.
8. **Allocation model** — chosen by risk tolerance from `ALLOCATION_MODELS`
   (cash / bonds / broad US equity / international equity), defaulting to `balanced`.
9. **Next action** — a one-line heuristic nudge.

Every rendered briefing ends with: *"planning output only, not financial advice."* This framing
is intentional and should be preserved.

---

## 7. External integrations

### Claude (Anthropic Messages API)
- Endpoint `https://api.anthropic.com/v1/messages`, `anthropic-version: 2023-06-01`.
- Default model `claude-opus-4-8`; the UI lets the user override the model string.
- Failure modes (`ClaudeError`) are surfaced to the UI, which falls back to showing the prepared
  prompt so the work isn't lost. **Transient failures retry automatically** — `_post` retries 429 /
  500 / 502 / 503 / 504 / 529 and connection errors up to `MAX_RETRIES` (3) with exponential backoff
  (1s/2s/4s), so a passing "internal server error" doesn't fail a research pass; non-transient errors
  (400/401/…) fail immediately, and the error detail is **truncated** so a long HTML error page never
  floods the UI/console. Chunked research (`rate_universe`, the roster review) is **per-batch resilient**
  too: a batch that still fails after retries is skipped (surfaced via `on_issue`), keeping the rest.
- **Mock mode** (`helix/ai/mock.py`) mirrors the real output format for free, offline development.
- **Usage/cost tracking** — every real Claude call (Learning research, invest, rebalance, autopilot)
  records its actual token counts to the `ai_usage` table and an estimated cost (`estimate_cost()` ×
  `MODEL_PRICES_PER_MTOK`). `ai_usage_summary()` powers a live readout (today / month / all-time /
  tokens / calls) shown on the Invest scorecard and the Learning → Claude tab. The estimate uses
  real tokens × hardcoded rates; the **authoritative billed figure is in the Anthropic Console**.

### Alpaca (brokerage)
- Paper base `https://paper-api.alpaca.markets`; Live base `https://api.alpaca.markets`.
- Orders are market/day, identified by a generated `helix-…` client order id, and accept either
  share quantity or notional dollars (exactly one).
- `get_portfolio_history(period, timeframe)` reads the broker's equity time series
  (`/v2/account/portfolio/history`) for the equity curve (§19). `period` is `<n><D|W|M|A>`;
  Alpaca requires `1D` timeframe for periods longer than 30 days. Equity entries can be null
  (gaps) and are filtered by `parse_portfolio_history()`.
- `get_stock_bars(symbol, timeframe, start)` reads OHLC bars from the **market-data API**
  (host `data.alpaca.markets`, `/v2/stocks/bars`) for the S&P 500 benchmark line (§19). Free/paper
  accounts must use `feed=iex` (sip is paid). This is a different host from the trading API, reached
  via the `base` override on `_request`.
- **The UI submits paper orders only** — `submit_paper_order` refuses to run unless the
  environment is `Paper`. The client class itself can target Live, but no UI path does so today.
- Submitted paper orders are written to the journal as `paper_trade` entries.

---

## 8. Conventions & design principles

- **`from __future__ import annotations`** at the top of every module.
- **Frozen dataclasses** for config, credentials, profiles, and briefings — values are computed,
  not mutated.
- **Pure core, I/O at the edges.** `planner.py` is pure math; persistence is isolated in
  `SQLiteMemory` and `AppSettings`; network I/O is isolated in the two API clients.
- **Memory is the source of truth.** All three interfaces read/write the same SQLite DB, so the
  CLI, API, and UI always agree.
- **Stdlib-first.** Avoid new dependencies; reach for `urllib`/`http.server`/`sqlite3` before a
  package. PyQt6 is the deliberate exception, scoped to the desktop UI only.
- **Local-first & explicit egress.** Nothing leaves the machine except deliberate Claude/Alpaca calls.

---

## 9. Configuration, secrets & security

- `data/` is **git-ignored** (`.gitignore`), so the DB and `helix_settings.json` are never committed.
- **Secrets are stored in plaintext** in `helix_settings.json`. This is acceptable for a
  single-user local app but is **not** safe to copy off the machine or commit. The Claude key can
  instead be supplied via the `ANTHROPIC_API_KEY` environment variable, which **overrides** the
  saved value.
- The HTTP API binds to `127.0.0.1` by default and exposes **no write endpoints** and no secrets —
  only the briefing and watchlist. Changing `--host` to expose it on the LAN should be a conscious choice.

---

## 10. Known gaps & TODO

These are real, current limitations — keep this list honest.

- **No GUI path to the full investment profile.** `ProfileTab` exists in `qt_app.py` but is **not
  mounted** in `InvestmentTab`. The Money tab only saves a flat `investment_amount` to settings,
  not an `InvestmentProfile`. So a GUI-only user sees "profile missing" on the dashboard until they
  run `python main.py investment profile` from the CLI. (Wire up `ProfileTab`, or have the Money
  tab build a profile.)
- **Profile field drift.** The CLI collects debt and emergency-fund months; `ProfileTab` hard-codes
  debt to 0, emergency months to 6, and derives the expected return from risk
  (`RISK_RETURN_ASSUMPTIONS`), whereas the CLI defaults the return to 7%. Reconcile these.
- **Home** is partial (checklist + SMS); **Enterprise** now has a **v1** (Slack digest + git-work
  summary, §26) — banking/Chase, Gmail/Calendar, and write-back actions are still planned.
- **Live data in research — closed (§25, §32).** All three AI research paths (core ratings, roster
  review, special scout) reason from **live Alpaca price action + news**, and the core rating now also
  gets **real fundamentals** (revenue growth, margins, ROE, leverage) pulled free from **SEC EDGAR**
  (§32) — the fundamentals/earnings gap is closed. Remaining nuance: SEC data is annual/lagged, covers
  ~97% of the universe (non-calendar-FY filers like NVDA excepted), and is GUI-only for now.
- **Research failures are silent (TODO).** If the rating JSON doesn't parse — e.g. the response
  **truncates** past `max_tokens` — `parse_research_json` returns `[]`, nothing saves, and **no error
  surfaces** (the closed-branch research is wrapped in `try/except: pass`; `maybe_refresh_core_ratings`
  just returns `False`). This is why core ratings could be researched but never appear in the Research
  log while the small special scout worked. **Fixed:** research `max_tokens` raised 4096 → **8192**
  (100 core ratings ≈ 3k–4.5k output tokens, right at the old cap, worse now that live-data makes
  rationales longer), and the cap is now a **user-configurable budget** — `research_max_tokens()` in
  `autopilot.py` reads the `invest_research_tokens` setting, exposed as **Settings → "Research effort"**
  (Standard 8K / High 16K / Maximum 32K, **default High 16K** — `max_tokens` is a ceiling not a
  meter, so for the ~100-name core 16K bills the same as 8K and just removes truncation risk;
  clamped to ≤32K to stay within every model's output limit). **Every** research call honors it — all three GUI paths *and* the four CLI paths
  (`invest`/`rebalance`/`roster`/`autopilot`), which were previously hard-pinned at 4096 (so
  `python main.py autopilot` no longer truncates a ~100-name universe either). It's a *ceiling*, not a
  target, so raising it buys headroom against truncation, not guaranteed extra spend. **Also fixed:**
  parse failures are **no longer silent** — `build_rebalance_plan` / `maybe_refresh_core_ratings` /
  `maybe_research_special` take an `on_issue` callback that fires when a *real* research call parses to
  zero picks (`_research_issue()` formats a diagnostic with the raw length + a head/tail snippet). The
  GUI routes it (via the `research_issue` signal, main-thread-safe) to a **red warning line** under the
  assets, persists it to the `invest_last_research_issue` setting, and **journals the full raw response**
  (`research_error` entries); the CLI prints `[research] …`. The off-hours research / special-scout
  `try/except` branches now **emit `research_issue`** instead of swallowing the error (`pass`), so a
  network/timeout failure off-hours is no longer silent either. **Verified live (2026-06-05):** a real
  100-name core re-rate with the live digest produced **100/100 ratings at 6,454 output tokens** — above
  the old 4096 cap (so it *was* truncating) and well under the new 16K. **Also raised the research HTTP
  timeout** 90s → `RESEARCH_TIMEOUT_SECONDS` = **300s** (in `autopilot.py`, applied at every research
  client construction, GUI + CLI): that same call takes **1–3 min** to generate, so the 90s
  `ClaudeConfig` default was timing out — another silent path to "no core ratings." **Done (chunking):**
  core ratings are now **batched** — `rate_universe()` splits the universe into `RATING_CHUNK_SIZE`
  (50) names per call (each batch fetches its own per-chunk live context) and merges, so the **HELIX
  500** (~480 names) rates without truncating or timing out. A batch that parses to nothing surfaces via
  `on_issue` and doesn't abort the rest. **Also done — the roster review is now chunked too (§20):**
  incumbents are scored in `RATING_CHUNK_SIZE` batches + one discovery call, so **auto-rotation runs on
  the full HELIX 500** (the old `ROSTER_REVIEW_MAX_NAMES` size cap is removed) — core discovery of new
  names is restored.
- **Discovery — active (§20) + screened against the real market (§36).** The **HELIX 100** roster
  review discovers and rotates in new names across the full ~480 universe (chunked, §20), and **every
  discovered name (core/Special/Day-trade) is now validated against Alpaca's real tradeable asset list**
  (§36) **and a per-sleeve quality/liquidity screen** (§37: price + dollar volume, plus margins/leverage
  for the core) — so a discovery must be real, buyable, *and* liquid/quality enough for its sleeve.
  **Generation is now data-driven too (§40):** a market screener ranks the **whole ~7,000-name
  tradeable universe** by momentum + low-vol + liquidity and feeds the top names into the roster
  discovery for the model to judge — so HELIX finds names beyond Claude's memory, not just filters its
  guesses. Remaining limit: the liquidity figure is **IEX-feed-scaled** (the free feed is ~2-5% of
  consolidated volume), so exact market-cap / true volume would still need the paid SIP feed.
- **API is read-only** and single-purpose (briefing + watchlist).
- **No automated tests** and **no packaging** (`pyproject.toml`/`setup.py`); `requirements.txt`
  lists only PyQt6.

---

## 11. Roadmap (Investment-first, phased)

The strategy is to harden the Investment pillar through escalating levels of capability before
broadening to the other pillars:

1. **Read-only** — profile, briefing, watchlist, journal, and read-only Alpaca account/positions. *(done)*
2. **Paper trading** — submit and log simulated orders against Alpaca paper. *(done)*
3. **Live trading** — deliberate, guarded enablement of the Live environment (currently blocked in the UI). *(future)*
4. **Alpha / automation** — AI-assisted research feeding repeatable, reviewable decisions, evolving toward the self-curating **HELIX 100** universe (§20). *(in progress via the Learning pillar)*

Then expand into the **Home** and **Enterprise** pillars on the same core (memory + AI + interfaces).

---

## 12. Planned spec — Simplified Investment ("I") tab

> **Status: v1 SHIPPED (paper) — see §12.8.** Sections 12.1–12.7 are the original spec; **§12.8
> records what actually shipped.** Practice (paper) money by default; a gated **Real** mode exists.

**Goal.** Make the Investment tab a simple loop: *put cash in → the Learning/AI engine researches
your watchlist → you review the suggestions → execute as Alpaca **paper** trades → watch a
scorecard.* The AI proposes; the human clicks to execute (no fully-autonomous trading in v1).

### 12.1 The loop

| Step | You do | HELIX does | Reuses |
|---|---|---|---|
| 1 · Fund | Enter deployable cash `$` | Save amount (cap for this round) | `investment_amount` setting |
| 2 · Universe | (auto) | Load watchlist tickers + theses | `SQLiteMemory.list_watchlist` |
| 3 · Research | Click **Research** | Ask Claude per ticker → structured suggestion | `ClaudeClient`, `build_research_prompt` |
| 4 · Review | Tick which to include | Show proposals table (action, size, confidence, why) | new UI |
| 5 · Execute | Click **Place paper orders** | Submit notional paper orders; log each | `AlpacaClient.submit_order`, `add_journal_entry` |
| 6 · Score | (watch) | Show paper P/L + Claude spend vs ×10 target | `AlpacaClient.get_account`, new cost tracker |

### 12.2 Structured research result (new)

Research stops being free text and returns one record per ticker so the UI can act on it:

| Field | Values | Use |
|---|---|---|
| `symbol` | ticker | row key |
| `action` | `buy` / `watch` / `skip` | only `buy` is executable |
| `size` | USD or % of deployable cash | order notional, capped (see guardrails) |
| `confidence` | low / med / high | sort + display |
| `rationale` | 1–2 lines | why |

Implementation note: prompt Claude for **strict JSON** in this shape; default sizing = equal-weight
across `buy` rows if the model doesn't specify.

### 12.3 Reused vs new

| Already exists (reuse) | Must be added (new) |
|---|---|
| Alpaca paper client + order submit | One-screen "Invest" panel + proposals table |
| Claude client + research prompt | Structured/JSON research output + parser |
| Watchlist + journal storage | Claude **spend tracker** (see 12.4) |
| Paper-only guard in the order path | Paper **scorecard** (P/L vs cost target) |

### 12.4 The "cover Claude ×10" scorecard — what it means

| Question | Answer |
|---|---|
| Source of P/L | Alpaca **paper** equity − deposits. **Simulated money.** |
| Can it pay the real Claude bill? | ❌ No, in paper mode — it's a would-be scorecard. Real coverage needs the future **live** phase. |
| How "cost" is measured | Sum estimated Claude spend for the month (read `usage` tokens from each API response × a per-model rate). Requires `ClaudeClient.complete` to also return token usage, and a small `ai_usage` store. |
| Target shown | `monthly Claude spend × 10`, with a progress bar, clearly labelled *simulated*. |
| Cost lever | Default research to a **cheap model** (Haiku/Sonnet); Opus on demand. Smaller cost → the ×10 line is far easier to clear. |

### 12.5 Guardrails (v1)

| Rule | Why |
|---|---|
| Environment must be **Paper** | No real money in this flow |
| AI proposes, human executes | No autonomous firing in v1 |
| Order ≤ deployable cash; per-name ≤ watchlist `max_allocation_pct` | No over-allocation |
| Every order + research run logged to journal | Auditable decision trail |
| Scorecard labelled "simulated / paper" | No illusion of real profit |

### 12.6 Open decisions to settle before building

| Decision | Options | Leaning |
|---|---|---|
| Default research model | Haiku / Sonnet / Opus | **Haiku or Sonnet** (cost) |
| Research output format | strict JSON / parse free text | **strict JSON** |
| Spend tracking store | settings counter / new `ai_usage` table | **`ai_usage` table** |
| Position sizing | equal-weight / AI-suggested / watchlist-cap | equal-weight, capped |
| Profile-based briefing | keep on Overview / fold in / hide | **keep on Overview** |
| Cadence | manual button / scheduled | **manual** in v1 |

### 12.7 Relationship to the existing briefing

The profile-driven **briefing** (`planner.py`) stays on the **Overview** tab as the "how much should
I invest and in what mix" advisor. The simplified "I" tab is the hands-on **deploy loop**. The full
`InvestmentProfile` becomes **optional** for the simplified flow — which also routes around the §10
"ProfileTab not mounted" gap rather than depending on it.

### 12.8 v1 — as built (2026-06-03)

> **Status: SHIPPED in Practice (paper) mode.** A **Money mode** toggle (Practice ↔ Real) exists;
> Real is gated behind a confirmation dialog and is off by default. The CLI executes paper only.

| Area | What shipped |
|---|---|
| UI | New **Invest** sub-tab (first under Investment): money mode, cash, posture, AI source, model, proposals table with per-row include checkboxes, **Place Orders**, **Scorecard** |
| Engine | `helix/investment/autopilot.py` — `TradeProposal`, `InvestPlan`, `build_plan`, `execute_plan`, `render_plan` |
| AI | `research.build_portfolio_research_prompt` + `parse_research_json` (strict JSON); `mock.generate_mock_portfolio_research`; cheap default `DEFAULT_RESEARCH_MODEL` (Sonnet) |
| Cost | `ClaudeClient.last_usage` + `estimate_cost`; `ai_usage` table + `record_ai_usage` / `monthly_ai_spend`; scorecard shows month spend and the ×10 target |
| CLI | `python main.py invest [--cash --preset --ai --model --execute]` (dry-run by default) |

**Sizing rule (v1):** Balanced = equal-weight across buys; Aggressive = confidence-weighted
(high/medium/low = 3/2/1). Each buy is capped by the watchlist `max_allocation_pct`; any leftover
stays as cash (no redistribution yet).

**Locked decisions** (from §12.6): Sonnet default · strict JSON · `ai_usage` table ·
equal/confidence-weighted sizing · briefing stays on Overview · manual cadence.

**Headless test:** `python main.py invest --ai mock --cash 1000 --preset Aggressive` (verified).
Add `--execute` to submit paper orders once Alpaca paper keys are saved.

**Honest framing preserved:** paper P/L is simulated and **cannot** pay a real Claude bill; real
coverage requires Real mode (live money, real risk of loss).

---

## 13. Active rebalance strategy (v2 — current primary engine)

> **Status: SHIPPED (paper).** This is the strategy the Invest tab now runs. It supersedes the
> v1 buy-only loop (§12.8) as the default; v1 remains available via `python main.py invest`.

**What it is:** a concentrated, **AI-driven active** stock portfolio (deliberately *not* an index
core) that buys, sells, and trims toward AI-rated targets — with hard risk caps so "aggressive"
stays survivable. Chosen over the index approach at the user's direction.

**The loop:** live positions + watchlist → Claude rates each `buy`/`watch`/`skip` + confidence →
HELIX computes target sizes → generates **buy / sell / trim / exit** orders → review → paper execute.

| Risk setting | Default | Meaning |
|---|---|---|
| Max per stock | **Removed** (was 10%) | Per-position hard cap removed at the user's direction (2026-06-05) — core positions size by conviction only, no per-name clamp; the **cash buffer** is the remaining position-level risk control. The engine still accepts `max_position_pct`; the GUI passes **1.0 (uncapped)**, the CLI keeps its `--max-pos` flag for advanced use. |
| Cash buffer | 10% | Held back as dry powder; the rest is investable |
| Posture | Aggressive | Aggressive = confidence-weighted sizing — steep **8:3:1** (high/med/low), so a wide universe (HELIX 500) concentrates in the highest-conviction names rather than ~480 equal slices; Balanced = equal-weight. Combined with the removed per-stock cap, sizing is driven by conviction + the cash buffer. |
| Per-name cap | watchlist `max_allocation_pct` | Optional tighter cap on a specific ticker |
| **Concentration (top-N)** | **0 = uncapped** (default) | Hold at most N core names — the top-N by conviction tier then a momentum/trend factor (`max_positions`/`factor_scores`, §30). Concentrates capital in the best ideas instead of a ~480-name closet index; enabling it sells the also-rans, so it's opt-in. Pick N from the §29 backtest sweep. |
| **Volatility-adjusted sizing** | **Off** (default) | A bounded inverse-vol tilt (`vol_adjust`/`volatilities`, §31) on top of conviction: steadier names get up to 4×, jumpier down to 0.25×, median-anchored so gross exposure is ~unchanged. Targets equal risk per position (smoother ride); backtest first — it cut vol/drawdown but cost return in a bull window. |

| Sell rule | Trigger |
|---|---|
| **Exit** | AI rates a held name `watch`/`skip` → sell to $0 |
| **Trim** | Position drifts above its target/cap → sell the excess |
| **Fund buys** | Sells execute first; proceeds + cash fund the buys (buys scale down if short) |
| **Crash** | Stay the course by default — rebalancing naturally buys the dip — **but the §35 risk controls now add guardrails**: a deep per‑stock stop‑loss (−25%) exits a genuine blow‑up, and the drawdown brake / regime filter raise cash in a sustained downturn. No blanket auto‑liquidation. |
| **Rotate-out** | Name removed from the roster (HELIX 100, §20) → exited on the next rebalance. Buys are **roster-authoritative**: only roster names are buy-eligible, so an off-roster held name gets target $0 (fallback: rating-driven buys when no roster is defined). |

**Engine:** `helix/investment/autopilot.py` — `build_rebalance_plan()`, `execute_rebalance()`,
`render_rebalance_plan()`, `merge_universe()`. **CLI:** `python main.py rebalance …` (dry-run unless `--execute`).

**Verified:** synthetic test (trim + exit + new buy + fund-from-proceeds) and a live dry-run against
the paper account (first deployment = capped all-buys). Real Alpaca + Claude keys confirmed working.

**Decisions locked:** concentrated AI-active (not index) · 10% default max/stock (UI control) · 10% cash buffer ·
Aggressive posture · monthly drift-band cadence (manual trigger in v1) · stay-the-course in crashes ·
lump-sum (no recurring contributions) · **rating objective = grow the account long-term (months–years),
favoring durable compounders** (the `build_portfolio_research_prompt` goal), honesty guardrails kept.

**Honest note:** a concentrated AI book can beat *or* badly trail the index; the caps limit damage,
they do not guarantee gains. Paper-prove before funding real money.

---

## 14. Automation & portfolio view (v2.1 — current)

> **Status: SHIPPED (paper).** Auto-trading defaults to paper and stays **off until turned on each
> session**; real-money automation is possible but multiply-gated (see §15).

### Automation
| Surface | How |
|---|---|
| **Desktop** | Invest tab → **START / STOP** with a **Review every** interval (5 / 15 / 30 min or 1 hour). A `QTimer` runs plan→execute automatically each interval, no per-trade confirmation. START warns (stronger in Real mode). |
| **Headless** | `python main.py autopilot --interval 900 [--ai claude] [--once] [--ignore-hours] [--allow-live]` — same loop, no GUI; schedulable via Windows Task Scheduler. |
| **Market hours** | Each cycle checks Alpaca's clock — a **free** call, *before* any Claude spend — and skips trading when closed. The desktop loop is **market-aligned**: instead of sleeping a full interval when closed, it re-checks at most every `MARKET_CLOSED_RETRY_MS` (15 min) and lands right on the next open via the clock's `next_open`, so a loop started after-hours still trades at the open (single-shot `QTimer` rescheduled per cycle in `_cycle_done`). Headless `--ignore-hours` forces trading. |
| **Safety** | Paper unless Alpaca env is Live **and** (`--allow-live` / the Real toggle). Position caps + cash buffer + drift bands still apply; one failed order never aborts the batch. |

### Cadence & rating cache (current)
The strategy targets **long-term** growth, so re-rating ~100 names every few minutes was pure cost
and churn. Re-rating is now **decoupled from trading** via a rating cache:

- **Re-rate (the Claude call):** at most once per **`DEFAULT_RATING_MAX_AGE_DAYS` = 7 days**.
  `build_rebalance_plan(..., rating_max_age_days=7)` calls `memory.cached_ratings(universe, 7)` and,
  if **every** universe symbol has a rating refreshed within the window, reuses it — **no model
  call, and no re-save** (so the freshness clock keeps ticking). Any missing/stale/new ticker forces
  a fresh full re-rate that re-saves all ratings together. The re-rate also runs during market-closed
  idle time (`maybe_refresh_core_ratings`), so the core is prepared before the open (§21).
- **Rebalance (trade toward targets):** every "Review every" interval — `15 min / 1 hour / 4 hours /
  1 day`, **default `1 day`**. Most cycles are cheap: they rebalance against the cached ratings. The
  loop is **market-aligned** — when closed it re-checks every ≤15 min (free clock call, no AI spend)
  and resumes at the next open instead of burning the interval, so it never gets stuck off-hours.
- **Rotate the roster (HELIX 100, §20):** the GUI cycle calls `maybe_rotate_roster` — a
  **calendar-gated auto-review** (default `DEFAULT_ROSTER_REVIEW_DAYS` = 90 = quarterly) that
  discovers/swaps names and persists the new roster, fully hands-off. The first run stamps a
  baseline (no immediate rotation); the timestamp is persisted so it works across intermittent
  sessions. For guaranteed unattended cadence, also schedule `roster` via Task Scheduler.

**Recommended cadence:** re-rate **weekly** · rebalance **daily** · rotate **quarterly** — slow
enough to behave like the long-term compounder it's meant to be, and ~99% less Claude spend than the
old per-cycle rating. **Note:** the GUI uses a `QTimer`, so daily/weekly cadence needs the app left
running; for true unattended cadence run `python main.py autopilot`/`roster` from Windows Task
Scheduler.

### Portfolio view
The Invest tab's **Portfolio** panel shows a prominent balance line (equity / invested / cash /
open P/L) and a positions table (symbol, qty, value, avg cost, open P/L, P/L %), built from
`portfolio_snapshot(account, positions)`. It refreshes on tab load, after every manual or auto run,
via "Refresh Portfolio", on a chart-range change, and **automatically every 60s while the app is open**
(`portfolio_timer` → `_auto_refresh_portfolio`, a separate timer from the market-light poll). The auto
refresh is **quiet** (`refresh_portfolio(quiet=True)` skips the busy bar so it doesn't flash each
minute), is **key-gated** (skipped until Alpaca keys are saved), and the `_portfolio_busy` guard makes
it a no-op if a manual refresh or trading cycle is already in flight — so the balance and equity chart
stay live without disturbing anything. (A portfolio refresh is ~4 Alpaca calls — account, positions,
history, SPY — all free on paper and well under the rate limit.)

**Verified:** one live autopilot cycle auto-placed capped paper orders (queued — market was closed),
then they were canceled; account confirmed back to $100k / 0 positions.

---

## 15. Connecting real money

> Do this only after the paper track record earns it. **Real money can be lost.**

| Step | Action |
|---|---|
| 1 | Open a **live** Alpaca brokerage account at alpaca.markets; complete identity/brokerage onboarding. |
| 2 | **Fund** it — link your bank and transfer (e.g., $100). Fractional shares let $100 spread across names. |
| 3 | Generate **LIVE API keys** (separate from paper keys) in the Alpaca dashboard. |
| 4 | HELIX → Investment → **Alpaca** tab: set Environment to **Live**, paste the live key + secret, Save. |
| 5 | Invest tab: switch Money mode to **Real** (confirm the warning). Keep amounts tiny at first. |
| 6 | Trade manually first; enable **Automate** only once you trust it. Start with a high cash buffer. |

Keys live in `data/helix_settings.json` (git-ignored, plaintext) — never commit or share that file.

---

## 16. Simplified one-screen Investment tab (current UI)

> **Status: SHIPPED.** The Investment tab was reduced to a single screen at the user's request.
> The rebalance engine (§13) and automation (§14) are unchanged underneath — only the UI shrank.

The entire screen:

| Control | Purpose |
|---|---|
| Alpaca API key + secret + **Save Keys** | Replaces the old Alpaca sub-tab; keys saved to settings. **All of these setup controls now live behind the ⚙ Settings button** (a dialog), not inline. |
| ~~Money to put in~~ | **Removed.** Money lives in the Alpaca account; the app deploys the account's own equity (the fake/real toggle just switches which Alpaca account). `build_rebalance_plan` keeps an optional `deploy_budget` param (default 0 = full equity), unused by the GUI now. |
| **Fake or real money** toggle | Practice (paper) ↔ Real (live); Real is confirmation-gated. Uses `NoScrollComboBox` so a stray scroll-wheel can't flip it (the dropdowns + special-stocks % box on this tab all ignore the wheel). |
| **Review every** | How often a rebalance runs while RUNNING: 15 min / 1 hour / 4 hours / **1 day** (default). Ratings are **cached and only re-scored ~weekly**, so most cycles are cheap (no Claude call) — they just rebalance against the cached ratings (§14). |
| ~~Max per stock~~ | **Removed (2026-06-05).** The per-position cap is gone — positions size by conviction + the cash buffer, with no per-stock % in the UI (user direction: more room for opportunity, a wider book). The GUI passes `max_position_pct=1.0` (uncapped) to `build_rebalance_plan`. |
| **Special stocks %** | Size of the high-risk satellite sleeve (default **20%**, §21). Carved off the top for speculative moonshot bets; the rest stays in the HELIX 100 core + cash buffer. |
| **Day-trade %** | Size of the **short-term momentum / day-trade** sleeve (default **10%**, §27) — a *third* sleeve carved off the top alongside Special. Fast turnover with take-profit / stop-loss exits. `INVEST_DAYTRADE_ALLOCATION_SETTING`; Core gets whatever's left after Special + Day-trade + the cash buffer. |
| **Special funding** | How the sleeve is funded (§21): **House money** (default — buys specials only from profit *above your starting balance*, so it can sit empty until you're in the green) or **Always invest the %** (deploys the full % from day one, riskier). Maps to `build_rebalance_plan(special_principal=…)` via `INVEST_SPECIAL_FUNDING_SETTING`. |
| **Universe** (read-only count) | The tickers HELIX may trade — **auto-managed, no manual add/remove**. Seeded to the **~480-name HELIX 500** basket (`DEFAULT_TICKERS`, broad S&P-500-style large/mid caps) and self-curated by `maybe_rotate_roster` (§14/§20); shown only as a count ("N stocks · auto-managed"). Ratings are computed **in batches** (chunked, so the size never truncates a research call). The engine spreads across all buy-rated names, conviction-weighted (§13), and stores each ticker's rationale to `stock_rationale`. |
| **⚙ Settings** (button) | In the top bar. **Collapsed by default to the essentials** — Alpaca keys + Save, fake/real money, review interval, and the universe count — under a plain-language intro. Everything else lives behind a **"Show advanced settings"** toggle (hidden by default; HELIX runs it on sensible defaults): the sleeve splits (special % / day-trade % / funding), AI-research cost toggle + effort + the cadence spinners, **concentration** (§30/§31), **factor overlay** (§33) + **bull/bear check** (§34), **fundamentals** (§32), and the **risk-controls** section (§35). Every control has a hover tooltip; settings save as you change them. Scrollable (`QScrollArea`, Close pinned at the bottom). |
| **Refresh AI research** (toggle, in Settings) | Cost control (`INVEST_AI_RESEARCH_SETTING`, default **on**). **On:** HELIX refreshes its research on cadence — core ratings ~weekly, Special Stocks scout ~nightly, roster rotation ~quarterly — a small recurring Claude cost. **Off:** it **trades off the last cached research and makes no new Claude calls** (`_run_cycle` skips `maybe_refresh_core_ratings`/`maybe_research_special`/`maybe_rotate_roster`, and passes a huge `rating_max_age_days` so `build_rebalance_plan` reuses any-age cached ratings instead of re-rating) — so it costs ~nothing, but the picks/ratings stop updating. Styled like the Home/Xpert toggles (`aiResearchToggle`). When on, a **countdown line** under the assets (`_update_research_eta`, refreshed on the 60s market poll) shows the time until the next check — e.g. *"Next AI research — special scout in 6h · core ratings in 5d"* (special-scout ETA from `LAST_SPECIAL_RESEARCH_SETTING` + 1 day; core ETA from the oldest `stock_rationale` + 7 days). When off, it reads "AI research is paused." |
| **Research effort** (dropdown, in Settings) | The **token budget** (`max_tokens`) each Claude research pass may generate (`INVEST_RESEARCH_TOKENS_SETTING`, via `research_max_tokens()` in `autopilot.py`): **Standard 8K**, **High 16K** (default), **Maximum 32K** (capped at 32K to stay within every research model's output limit). It's a *ceiling, not a target* — raising it gives room for longer rationales and a bigger universe **without the JSON being cut off** (the §10 silent-truncation bug), not automatically more spend. One control, honored by **every** research call: the three GUI paths and the four CLI paths (`invest`/`rebalance`/`roster`/`autopilot`). |
| **Research cadence** (3 spinners, in Settings) | How often each Claude research pass may re-run, in days — **Re-rate core stocks** (`INVEST_CORE_RATING_DAYS_SETTING`, default 7), **Scout special stocks** (`INVEST_SPECIAL_DAYS_SETTING`, default 1), **Review the roster** (`INVEST_ROSTER_DAYS_SETTING`, default 90). They feed `rating_max_age_days` / `research_days` / `review_days` in `_run_cycle`, replacing the hardcoded defaults, and the countdown line + "Refresh AI research" cost picture follow them. Slower = less Claude spend (and staler picks); faster = fresher (and more tokens). |
| **Max core positions** (spinner, in Settings) | **Concentration cap** (`INVEST_MAX_POSITIONS_SETTING`, default **0 = "All (uncapped)"**, §30). Holds at most the top-N core names (ranked by conviction tier then a momentum/trend factor); the rest get target $0. Concentrates capital in the best ideas vs. a ~480-name closet index. **Opt-in** — turning it on deliberately sells the also-rans to concentrate, so backtest (`python main.py backtest`) to choose N first: tighter N raised return but also volatility/drawdown in testing. |
| **Volatility-adjusted sizing** (toggle, in Settings) | Tilts sizing toward steadier names — a bounded inverse-vol multiplier on top of conviction (`INVEST_VOL_ADJUST_SETTING`, default **off**, §31). Aims for more equal risk per position (smoother ride, often higher Sharpe). **Opt-in**; the backtest's "conviction + vol-adj" leg shows the effect on your basket (it cut vol/drawdown but cost return in the bull-window test). |
| **Use SEC fundamentals** (toggle + cadence, in Settings) | Feeds real fundamentals — revenue growth, margins, ROE, leverage — pulled free/keyless from **SEC EDGAR** into the rating prompt (`INVEST_FUNDAMENTALS_SETTING`, default **on**; refresh cadence default 30d, §32). So picks weigh the numbers, not just price/news. ~97% universe coverage; refreshed monthly, read locally each re-rate. |
| **Factor overlay** (toggle, in Settings) | Blends a deterministic composite factor (momentum + SEC quality + low-vol) over the LLM's call (`INVEST_FACTOR_OVERLAY_SETTING`, default **off**, §33): a buy the numbers strongly contradict is tempered to a watch, a strong-factor buy is confidence-bumped. Makes the decision quant + LLM. **Opt-in**; A/B the "conviction + factor-overlay" backtest leg first. |
| **Bull-vs-bear check** (toggle, in Settings) | Adversarial stress-test of the top buy candidates (`INVEST_ADVERSARIAL_SETTING`, default **off**, §34): before committing, HELIX argues a bull case, a forced bear case, then judges — downgrading any buy that doesn't survive. Catches fragile picks; a demanding skeptic (more patient posture). One extra Claude call per checked name (top ~12), so opt-in; payoff shows in the scorecard. |
| **Risk controls** (5 toggles, in Settings) | Protective guards, **default ON** (§35): **sector cap** (≤25%/sector), **drawdown brake** (down 15% → raise cash), **regime filter** (SPY downtrend → raise cash), **per‑stock stop‑loss** (core name −25% → exit), **diversification floor** (≥~20 names). They cap losses, not chase return — dormant in good times, act when conditions turn. Thresholds are `DEFAULT_*` constants. |
| **START / STOP** | Top-right. START confirms, then auto-invests on your chosen interval (Claude → buy/sell/trim → paper/live execute); STOP halts |
| **Market light** | Top-left of the START/STOP bar: a green/red dot + status — "Market open · closes Wed 4:00 PM ET" or "Market closed · opens Mon 9:30 AM ET" (times in US/Eastern from Alpaca's clock). Live: a **free** clock call polled every 60s (`refresh_market_status`); shows "save Alpaca keys" until configured. |
| **Market schedule** (button) | Next to the market light: opens a popup of the regular hours (**9:30 AM–4:00 PM ET**) shown **in both ET and the user's local time**, the live open/closed status + next open/close, and a table of **upcoming trading days** from Alpaca's official `get_calendar` (holidays skipped, early closes flagged). ET→local conversion is stdlib-only (`_eastern_offset` US-DST helper + `.astimezone()`, no tz-database dependency). Explains why nothing trades off-hours (HELIX only trades the regular session). Loads off-thread, then shows; works offline (static hours) with a "save keys" note. |
| **Progress bar + activity line** | A prominent (16px) indeterminate busy bar shown whenever HELIX is working, paired with a **live status line** that names the current step — "Reviewing the stock universe…", "Scouting new moonshot stocks…", "Researching the core stocks…", "Rating stocks and planning trades…", "Placing orders…". Pushed from the worker thread via the `research_step` signal, so it updates in real time (the cycle runs off-thread now, so the bar animates smoothly). |
| **Balance** | A big **centered number at the very top** = account equity. A small sub-line shows Cash and **Gains** (optimistic label for open P/L). Refreshes on load and after each cycle. |
| **Balance curve** | Directly under the balance number: a HUD sparkline of account balance (equity) over time — header reads **BALANCE** — with a **Range** selector (**1D** / 1W / 1M / 3M / 1Y), plus a dashed **S&P 500** overlay and a **"vs S&P 500 ±X%"** readout (§19). The solid line is your money over time; dashed is the index. **1D is the intraday view** (today's equity in 5-minute steps) and shows the account line **only** — no daily S&P overlay, since a daily index line can't match an intraday curve. Empty until the paper account has history to plot. |
| **Assets tables (three)** | Holdings split into **Core — HELIX 500**, **Special Stocks** (§21), and **Day-trade — short-term momentum** (§27), by whether the symbol is in the `invest_special_stocks` / `invest_daytrade_stocks` lists, so each sleeve is visually separate. The **percentages in the headings track the Special % and Day-trade % controls live** (`_update_sleeve_labels` on their `valueChanged`) — Core reads `100 − special − daytrade`. Each table: Symbol, qty, value, Gains, Gains %, and an embedded **compact Details button** (`#rowButton` style — `min-height:0` + fixed 26px, wrapped in a margin holder so it fits the row cleanly, fixing the earlier oversized/overlapping button). Details opens a fast per-position popup: live numbers (avg cost, current price, market value, cost basis, open P/L, today's move — from the raw Alpaca position), **HELIX's stored thesis** (`stock_rationale`), and **buy/sell history** with first-bought date + days held (`memory.list_symbol_trades`). Local-only reads, so it opens instantly. |
| **Research log** (button) | By the Assets table: opens a view of what HELIX researched — Core ratings + Special picks + Day-trade picks, each with its "why" and the time researched — read from SQLite. **Double-click a row** (or hover) for the full thesis/rationale in a detail dialog. Shows what was prepared during market-closed hours for the open (§21). |
| **Refresh research now** (button) | Beside Research log / Prediction scorecard. **Forces a full AI research pass on demand** — re-rate the core, scout new Special + Day-trade picks, review the universe for swaps — regardless of cadence, off-thread, in any market hours, and **without placing trades** (`refresh_research_now` → `force=True` through the research helpers). Confirmation-gated (a few Claude calls); updates the picks + Research log. So the user never needs to enter Settings to trigger research. |
| Claude cost line | today / month / all-time estimate |

**Exposed controls:** fake/real, **review interval**, special-stocks %. **Baked-in (hidden):**
Aggressive posture · 10% cash buffer · Claude (Sonnet) research · **7-day rating cache**. Defaults
live as constants (`DEFAULT_PRESET`, `DEFAULT_CASH_BUFFER`,
`DEFAULT_RESEARCH_MODEL`, `AUTO_INTERVALS` default `1 day`, `DEFAULT_TICKERS` ≈ **480-name HELIX 500**
basket in `qt_app.py`; `DEFAULT_RATING_MAX_AGE_DAYS` = 7, `RATING_CHUNK_SIZE` = 50 in `autopilot.py`).
The per-stock cap is **no longer a constant or control** — the GUI passes `max_position_pct=1.0`
(uncapped) per §13; conviction sizing uses the steep `CONFIDENCE_WEIGHT` (8:3:1).

**Verified:** builds headless (offscreen Qt) against the live paper account — loads the $100k
balance, START ready, no positions.

---

## 17. Self-calibration (the learning loop)

> **Status: SHIPPED (paper) — extended to the deeper feedback loop (research roadmap step 2).** HELIX
> learns from its own results, and the research prompts now see **three** signals, not just one.

1. **Record** — when a position is sold/trimmed, `execute_rebalance` stores the realized return % and $
   (from the position's P&L at sell time, prorated for partial trims) into `sell_log` — across **all
   sleeves** (core trims/exits, special exits, day-trade take-profit/stop-loss).
2. **Aggregate** — `memory.strategy_performance()` computes hit rate, average return, and realized P/L
   across closed positions (rolling 1-year window).
3. **Feed back — three signals** (`performance_digest()` in `autopilot.py`), injected into **every**
   research prompt:
   - **Realized record** — the closed-trade hit rate / avg return / P&L (above).
   - **Per-pick forward returns** — how the *current* picks are actually doing, straight from live
     position P&L (`unrealized_plpc`): how many are up vs down, plus the biggest **winners and laggards
     by name**. This is "did the names I bought go up" — the signal the old loop lacked.
   - **vs the S&P 500** — the account's ~30-day return minus SPY's, so the model knows whether it's
     **beating or trailing the market** (`InvestTab._performance_review`, two free Alpaca reads).
   The digest reaches the **Core** rating (`build_rebalance_plan` / `maybe_refresh_core_ratings` via
   `performance_override`), the **Special** scout, **and** the **Day-trade** scout (`performance=`) —
   each told to "lean into what's working, ease off what isn't". The realized summary also appears in
   the Xpert opinion context and the Investment dialog.

**Honest scope:** realized P/L is an *estimate* (position P&L at sell time, prorated), close to but not
identical to Alpaca's FIFO-lot figure. The feedback is a **prompt-level signal** the model reasons over,
not a numerical optimizer — it now gives the model a fuller picture (closed record + live forward
returns + net-of-index) and sharpens as more trades close, but it's a soft nudge, not a guarantee.
**Per-pick forward returns over time — SHIPPED (§28).** The **prediction scorecard** now snapshots
every rating to the append-only `rating_outcomes` log and scores its realized forward return at
1w/1m/3m, **bucketed by confidence and net of the S&P** — the measurement foundation this loop was
missing (it had been calibrating partly on rebalance-trim noise). **Feeding it back into the prompts —
SHIPPED (§38).** The scorecard's matured buy-conviction buckets (net of the S&P) are now distilled into
a calibration line that **leads** the feedback digest, so every research prompt learns whether its own
high-conviction calls have actually beaten the index. This is the *plumbing*; its effect is a forward
bet — the buckets are empty/immature today, so the line stays silent until they mature (weeks), then
activates automatically. Per-sleeve breakdowns remain a next step.

**Still ahead** (roadmap step 3+): two-stage deep memos, adversarial/ensemble research, and
event-driven triggers — each measured against the §28 scorecard.

---

## 18. Orientation for a new session

**Goal:** grow real money over the long run. Today everything runs on **Alpaca paper** (fake money)
to prove the approach first.

**Run it:** `python main.py` (desktop UI; opens on **Home**). CLI subcommands: `brief`, `run`,
`api`, `invest`, `rebalance`, `autopilot`, `roster`, `scorecard`, `backtest`, `investment …`.
For **always-on** use (Brian's chosen model — leave it running, not OS-scheduled), launch via
`python scripts/run_helix.py` (the supervisor relaunches the app if it hard-crashes); run
`scripts/install_autostart.ps1` once to start it at login (§39). Package a standalone Windows app (no
Python needed, for the living-room tablet) with `python build.py` → `dist\HELIX\HELIX.exe` (§41).

**Verify changes without a visible window:**
- Byte-compile: `python -m compileall -q helix main.py scripts`.
- Import: `python -c "import helix.interfaces.qt_app"`.
- Build a tab offscreen: set `QT_QPA_PLATFORM=offscreen`, make a `QApplication([])`, **keep a
  reference** to the widget (`w = InvestmentTab(m); it = w.invest_tab` — a bare
  `InvestmentTab(m).invest_tab` gets garbage-collected → "wrapped C/C++ object deleted"), and pump a
  `QEventLoop` (`QTimer.singleShot(3000, loop.quit); loop.exec()`) so background workers finish.

**Data & secrets:** `data/helix.db` (SQLite) and `data/helix_settings.json` (Alpaca + Claude keys,
**plaintext**) — both git-ignored. Never commit them. In tests use a **temp DB**; avoid writing the
live DB while the app is running (lock contention).

**Gotchas:**
- The running app holds the code from launch time — **restart to see edits**.
- Unattended safety (§39): an unhandled error in a UI callback is **logged to `data/helix.log`** and
  the app stays alive (a custom `sys.excepthook`, not PyQt's aborting default); the trading cycle
  self-heals (always reschedules the next run).
- Long network calls (Alpaca/Claude) run off-thread via `spawn_worker` (QThreadPool); only touch
  widgets in the main-thread callback.
- The console renders em-dashes (—) as `�`; it's fine in the app/voice.
- Keys are one pair; the fake/real toggle only swaps the Alpaca endpoint, so live needs live keys saved.

**Current state (2026-06-06):**
- **Investment** — live: auto-rebalances a ~100-stock universe (Alpaca paper), Claude rates
  buy/watch/skip, sizing/caps + drift-band trims, sells with reasons, **self-calibration loop**
  feeding realized results back into the rating prompt; rating prompt targets **long-term growth**.
  An **equity curve** (§19) under the balance plots account value over time against an **S&P 500
  benchmark** (the honest "are we beating the index?" line). The **HELIX 100** (§20) self-curates the
  universe (discover/rank/rotate, auto quarterly), and a high-risk **Special Stocks** sleeve (§21,
  default 20%) of speculative moonshot bets runs alongside the core. A new **prediction scorecard**
  (§28) snapshots every rating and scores its realized forward return by confidence vs the S&P, a
  **backtest harness** (§29) replays the deterministic engine on real history (Sharpe/drawdown/vs-S&P),
  a **top-N concentration** lever (§30), **volatility-adjusted sizing** (§31), and a **factor-backbone
  overlay** (§33, composite momentum+quality+low-vol checking the LLM) tune the book (all default off,
  measured on the backtest), **real SEC fundamentals** (§32, default on) ground the picks, and an
  **adversarial bull-vs-bear check** (§34, default off) stress-tests the top buys, and **five risk
  controls** (§35, default on: sector cap, drawdown brake, regime filter, per-stock stop-loss,
  diversification floor) cap the ways you lose — the
  measurement foundation for proving (not hoping), plus the first improvements on it. Honest finding:
  the deterministic *re-weighting* levers test neutral-to-negative on the (bull, look-ahead-biased)
  backtest; the *input/research-quality* improvements (fundamentals, the overlay's quality leg, the
  bull/bear check) are the forward bets the scorecard will settle.
- **Learning** — shared AI layer (Claude research + usage tracking); Investment dialog shows pick
  logic + recent sells from SQLite.
- **Xpert** — a **two-way J.A.R.V.I.S. voice assistant** (§23–§24): push-to-talk (local
  faster-whisper STT) → multi-turn Claude with live HELIX context → **tool-use that performs real
  actions** (check portfolio, start/stop investing, manage Home tasks, review the HELIX 100, scout
  specials, text reminders) → edge-tts reply, with spoken-confirmation gates on anything
  money/outward. The one-way expert-opinion briefing still lives below it.
- **Home** — editable Action/Item/Frequency task list (reminders + ordering still to come).
- **Enterprise** — v1 (§26): a Claude-summarized digest of recent **git work** across your projects +
  **Slack** mentions/DMs. Read-only, off-thread; Chase/banking deferred.
- **Reliability (§38–§39, 2026-06-06)** — the scorecard now **feeds back into the rating prompts**
  (close-the-loop, §38; silent until it matures, then self-calibrates); the **always-on app is
  hardened** (crash guard → `data/helix.log`, self-healing cycle, `scripts/run_helix.py` relaunch +
  **paper auto-resume** on launch); and the §35 risk controls are now **backtested on a down market**
  (§29 `--down-markets`: 2022 max drawdown 17.1%→13.8%).
- **Core-satellite + full market (§42, 2026-06-06)** — after a live paper run showed the book trailing
  the S&P on a high-beta speculative tilt: an **index core** (VOO 40%) + lighter speculative sleeves
  (one-time migration) so the book tracks the market and the AI is the satellite; and the tradable
  universe expands from ~7,000 fractionable names to **all ~12,722** via whole-share orders for the
  non-fractionable ones.

**Likely next steps:** the **Xpert two-way voice assistant + action layer shipped** (§23–§24),
including a hands-free **"HELIX" wake word** and mic/speaker device pickers (works with the Bluetooth
headset) — remaining there is **barge-in** (interrupt mid-reply), a **phone/remote client** to talk
from anywhere, and applying roster swaps by voice; the **account's return vs the S&P 500 is now fed
into the research prompts** (§17 feedback loop) — remaining there is feeding it into the **Xpert
opinion/voice** context too, and tracking each pick's forward return *over time* (not just the current
snapshot); Home reminders + one-tap ordering; the **HELIX 100** now
self-curates (auto-rotation shipped, §20) — and a **market-data screener now discovers names beyond the
model's memory** (§40, scanning the ~7,000-name tradeable universe); for unattended uptime HELIX now
runs **always-on** with a crash guard + relaunch
supervisor (§39, superseding the earlier Task Scheduler suggestion); a real-money path only after a
solid paper track record.

---

## 19. Equity curve (visualizing the learning loop)

> **Status: SHIPPED (paper).** A chart of account equity over time, under the balance on the
> Investment tab — the at-a-glance answer to "is the strategy actually working?".

**Two data sources, one shape.** The chart consumes an `EquitySeries` (pure dataclass in
`autopilot.py`: ordered equity points + start/end date labels + `change_usd`/`change_pct`/`low`/`high`).
Two parsers produce it, so the widget is source-agnostic:

| Source | Builder | Role |
|---|---|---|
| **Alpaca** `/v2/account/portfolio/history` | `parse_portfolio_history()` | **Primary** — the broker's real equity series, **retroactive** so the curve is populated immediately from existing paper history. Null/zero equity points are dropped. |
| **HELIX** `equity_history` table | `equity_series_from_rows()` | **Fallback + durable record** — drawn when Alpaca history is empty/offline, and accrues HELIX's own samples for the AI layer (the reason for keeping a local copy). |

**The flow:**
1. **Record** — every portfolio refresh/cycle, `_portfolio_done` calls `memory.record_equity(...)`
   from the snapshot (throttled to ≤1 sample / 10 min). This builds `equity_history` over time.
2. **Fetch** — the same off-thread worker that loads the balance also pulls
   `get_portfolio_history(period, timeframe)` for the selected **Range** (**1D→`1D`/`5Min`** intraday,
   1W→`1W`/`1H`, 1M→`1M`/`1D`, 3M→`3M`/`1D`, 1Y→`1A`/`1D`; >30-day periods must use `1D`). The 1D view
   skips the S&P overlay (`_fetch_benchmark` returns `None` for `days <= 1`).
3. **Draw** — `EquityCurveWidget` (a custom `QPainter` widget — **no charting dependency**, in
   keeping with the stdlib-first rule) paints the line (green up / red down / cyan flat), a
   translucent area fill, a dashed amber baseline at the period's starting equity, an end-point
   marker, the period change, and start/end date labels. If the Alpaca series has <2 points it
   falls back to the local series; with <2 points total it shows "Equity curve fills as HELIX runs."

**Decisions locked:** Alpaca history is the visible source (populated now) · HELIX also records its
own samples to SQLite (durable, offline-resilient, cheap signal for future AI prompts) · custom
`QPainter` sparkline (no QtCharts dep) · Range selector exposed (1D/1W/1M/3M/1Y), default 1M · the
chart lives on the Investment tab under the balance.

**Benchmark — vs the S&P 500.** The chart overlays a second, dimmed/dashed line: what the same
starting equity would be worth if it had simply tracked the **S&P 500** (SPY) over the window, plus a
headline **"vs S&P 500 ±X%"** = the account's period return minus the index's. SPY closes come from
Alpaca's **market-data API** (`get_stock_bars`, host `data.alpaca.markets`, `feed=iex` for free/paper
accounts), normalized to the account's starting dollar so both lines share one y-scale. This is the
real scoreboard: a rising account that *trails* the index isn't actually working. Falls back to the
account line alone if market data is unavailable. Pure helpers: `parse_stock_bars()` +
`benchmark_series()` in `autopilot.py`.

**Honest scope:** the curve shows **paper** account equity (simulated). The account's **return vs the
S&P 500** is now fed into the research prompts (the §17 feedback loop computes it from live portfolio
history + SPY in `InvestTab._performance_review`); the local `equity_history` *table* itself is still
only drawn on the chart, not read by a prompt. The S&P 500 overlay reframes the curve as **net-of-index** — the comparison
that separates real skill from a generally rising market — though it is still *gross* of the spreads
and taxes a live book would pay, so true net-of-cost alpha is lower than the line suggests.

---

## 20. Planned — the "HELIX 100" (self-curating universe)

> **Status: v1 SHIPPED (headless engine + CLI; desktop button next).** The self-curating universe
> that evolves the ~100-name basket (§16, `DEFAULT_TICKERS`) instead of leaving it frozen at setup.

**The idea.** Today the trade universe is fixed: HELIX rates the basket you give it (plus current
holdings) and never looks beyond it (§13). The **HELIX 100** turns that basket into a living roster —
HELIX **researches and discovers** new candidates, **ranks them against the names it already holds**,
and **rotates a laggard out for a stronger candidate** when the newcomer is convincingly better at
making money. Capital stays concentrated in the ~100 best forward prospects, not a list frozen at setup.

**The loop (slow cadence, separate from rebalancing):**
1. **Discover** — nominate candidates beyond the current roster (Claude's knowledge today; a
   screener / news connector later — the §10 data gap).
2. **Score** — rate incumbents *and* candidates on one comparable metric (expected forward return ×
   conviction) so they can be ranked head-to-head, not just buy/watch/skip in isolation.
3. **Rotate** — replace the weakest incumbents with higher-ranked candidates, but only when a
   candidate beats the incumbent by a **margin** (hysteresis) and within a **per-cycle swap cap**.
4. **Trade** — rotated-out names sell to $0; the existing rebalance engine (§13) sizes the rest.

**Why the guardrails (investment theory).** Model rankings are noisy, so swapping on tiny score
differences just churns the book — every rotation pays the spread and adds **turnover drag** that
compounds against returns. A margin threshold + turnover cap admit only high-conviction swaps. This
is active selection (a momentum/quality tilt); the per-name and cash-buffer caps from §13 still bound
single-name risk.

**Cadence.** Discovery + roster review is expensive (rating new names with Claude), so it runs on a
**slow schedule** (e.g. weekly/monthly) while the cheap rebalance runs often against the current
roster — the same decoupling flagged in §18.

**Honest scope.** "Better at making money" is a model *expectation*, not a fact; selection from
training knowledge carries recency/survivorship bias and uses no live data until a connector exists.
Whether rotation actually beats a static basket is exactly what the equity curve (§19) and realized
track record (§17) are there to measure — prove it on paper first.

### 20.1 v1 — as built (2026-06-03)

| Area | What shipped |
|---|---|
| Engine | `autopilot.py` — `build_roster_review()` (pure): scores incumbents + candidates, then **greedy 1-for-1 margin-gated swaps** (best candidate replaces worst incumbent only if it beats it by `min_margin`, capped at `max_swaps`). `RosterSwap`/`RosterReview`, `render_roster_review()`, `normalize_roster()`, `ROSTER_SETTING`. |
| AI | `research.build_roster_score_prompt()` + `build_roster_discovery_prompt()` + `parse_roster_review_json()` — one **0-100 score** per name (incumbents *and* candidates on the same scale) so they rank head-to-head; `mock.generate_mock_roster_review()` for offline runs. (The original single-call `build_roster_review_prompt()` was superseded by these chunked prompts and removed.) |
| Roster of record | The GUI **Stocks To Trade** basket (`invest_tickers`). A review *proposes* swaps; applying *writes* the new roster. Trades happen on the **next rebalance**, not inside the review. |
| Trade linkage | `build_rebalance_plan` is now **roster-authoritative**: only roster names are buy-eligible, so a held name rotated off the roster is exited (§13). Falls back to rating-driven buys when no roster is defined (CLI edge case). |
| CLI | `python main.py roster [--ai mock\|claude] [--candidates N] [--max-swaps N] [--min-margin PTS] [--apply]` — dry-run by default. |

**Defaults:** 30 candidates considered · ≤10 swaps/review · 8-point min margin · size-preserving.
**Decisions locked:** comparable 0-100 score (a *ranking signal*, not a return forecast) · greedy
margin-gated 1-for-1 swaps with a turnover cap · roster = `invest_tickers` · **AI proposes and
auto-applies** on a calendar cadence (the user chose fully self-curating — no approval step; the
editable stocks table was removed); manual `roster --apply` still available · paper-first.

**Discovery is now data-driven (§40, 2026-06-06):** the roster review is **seeded by a market-data
screener** that scans a bounded, rotating slice of the full ~7,000-name tradeable universe (momentum +
low-vol + liquidity) and hands the model real candidates to judge — generation by the market's data,
not the model's memory.

**Headless test:** `python main.py roster --ai mock` (verified — proposes swaps; dry-run by default).

**Caveat (universe split):** `roster` curates `invest_tickers` — the **GUI** trading loop's basket, so
the desktop app trades the rotated roster on its next cycle. The CLI `rebalance`/`autopilot` still read
the *separate* SQLite `watchlist` table, so they won't reflect roster changes until those two universes
are unified. Pre-existing split (§10), not introduced here.

**Auto-rotation — SHIPPED, now full-universe (chunked).** The GUI cycle calls `maybe_rotate_roster`,
which auto-applies a roster review on a calendar cadence (`DEFAULT_ROSTER_REVIEW_DAYS` = 90, quarterly)
with no approval step — the universe self-curates and the editable stocks table is gone. First run
stamps a baseline. **The review is now chunked** (`build_roster_review`): incumbents are scored in
`RATING_CHUNK_SIZE` (50) batches — each with its own per-chunk live market context — then a **single
discovery call** proposes new candidates anchored on the weakest incumbents (same 0-100 scale, so they
rank head-to-head), and the greedy margin-gated swaps proceed as before. This removed the old
`ROSTER_REVIEW_MAX_NAMES` (120) size cap, so **the full ~480-name HELIX 500 now discovers and rotates**
(previously skipped). Honest caveat: incumbent scores come from separate calls, so cross-chunk scale
consistency is approximate — the `min_margin` (8 pt) hysteresis guards against that noise; and discovery
is still Claude-knowledge-based (no screener, §10). **Verified live (2026-06-06):** the real 478-name
universe scored **478/478 with rationales, no truncation, 0 issues** (10 chunks + 1 discovery call, ~10
min off-thread), producing 7 sensible margin-clearing swaps (e.g. AAL 18→BRK.B 88, PARA 22→NVO 84,
F 28→CSGP 80) — weak cyclicals/media rotated for quality compounders. Plus 12 hermetic checks.

**Still to come:** a **news/event** connector to complement the market-data screener (§40 now handles
discovery beyond Claude's training knowledge);
wiring `roster` into Windows Task Scheduler for guaranteed unattended cadence (the GUI timer only
fires while the app is open, though the persisted timestamp means it still rotates on the next
session after a quarter elapses).

---

## 21. Special Stocks (high-risk satellite sleeve)

> **Status: v1 SHIPPED (paper).** A capped, high-risk sleeve alongside the HELIX 100 core — the
> "swing for the fences" money. Default **20%** of the account (user-set, exposed as "Special stocks %").

**The idea (core–satellite / barbell).** The HELIX 100 (§20) is the diversified core; **Special
Stocks** is a small satellite of speculative, asymmetric bets — early-inflection names that could
become the next breakout ("NVIDIA before it was obvious"). The two are sized independently, and the
sleeve's **% cap is the risk control**.

**The loop:**
1. **Scout (market-closed idle time).** A *separate* exploratory Claude pass
   (`build_special_research_prompt`) hunts asymmetric upside — a different objective from the core's
   "durable compounders." Cadence-gated (`maybe_research_special`, default **nightly** via
   `DEFAULT_SPECIAL_RESEARCH_DAYS` = 1 — events move fast) **plus a forced fresh scout on every
   START**. Runs on the
   market-closed branch (idle time, the user's idea) and also opportunistically on the open path so
   the sleeve is guaranteed to populate even if cycles rarely land off-hours. Picks **accumulate** —
   the best (highest-conviction first) are added onto the held list up to `DEFAULT_SPECIAL_MAX_NAMES`
   (12). When the sleeve is **full**, a fresh **high-conviction** pick **evicts the weakest
   non-winner** — an unproven not-yet-held name first, else a held laggard (down/flat) — but
   **never a winner** (a held position that's up, identified by Alpaca P&L; capped at ~3 swaps/scout).
   So fast-moving events still get in while winners ride; with no P&L data it never evicts (safe).
2. **Store.** The held list persists to the `invest_special_stocks` setting; theses go to
   `stock_rationale` (action `special`, shown in Learning).
3. **Allocate (market open) — buy-and-hold.** `build_rebalance_plan` carves `special_allocation_pct`
   off the top (core gets `investable − special_budget`). It **holds existing special positions
   exactly as they are — no trim, no exit — so a winner can run like NVIDIA**, and uses whatever
   budget is left over to open *new* positions, each capped small at `DEFAULT_SPECIAL_MAX_POSITION_PCT`
   (5% of the account) so a pick going to zero barely dents it.
4. **Trade.** Held winners and current picks are never force-sold; only *new* entries are bought, and
   a winner that grows past the 5% cap is left to run (the cap is an *entry* size, not a trim trigger).
   The one exception: a **laggard rotated out** of the list (see eviction above) is sold on the next
   rebalance to fund the newcomer — winners are never the ones rotated out.

**Funding — house-money (conservative).** The sleeve is funded only from *profit*:
`special_budget = min(gains above the protected principal, the sleeve %)`. HELIX captures your starting
equity as `invest_principal` on the first cycle; until the account climbs above it the sleeve stays
empty and 100% of principal sits in the core. As gains accrue, profits fund the moonshots (up to the
20% ceiling) — you can only ever gamble winnings, never the base. (Honest caveat: once reinvested those
gains are at real risk, and the sleeve grows as you win — a rising-risk profile by design.)

**Funding toggle (GUI, shipped).** This is now a **Settings → Special funding** choice
(`INVEST_SPECIAL_FUNDING_SETTING`): **House money** (default, the conservative behavior above —
`special_principal` = your starting equity) or **Always invest the %** (`special_principal=0` →
deploys the full sleeve % from day one, riskier). **Note:** on a fresh paper account in House-money
mode the speculative sleeve correctly buys *nothing* until you're in profit — that's by design, not a
bug (a common point of confusion). Switch to "Always" to see the 20% deploy immediately.

**Prepared overnight + the Research log.** Both sleeves are researched during **market-closed idle
time**: `maybe_refresh_core_ratings` warms the core (80%) ratings **weekly** and
`maybe_research_special` scouts the specials (20%) **nightly** (and on every START), both persisted to
`stock_rationale`. By the open,
trading runs off the prepared research with no model call. The Investment tab's **Research log** button
opens a view — Core ratings + Special picks with their "why" and the time researched — read straight
from SQLite (this is where the persisted data earns its keep).

**Why the guardrails (investment theory).** This is power-law / barbell territory: most moonshots go
to zero, you only need the occasional one to hit, and the small per-name cap + sleeve cap mean the
losers can't sink the account. Honest caveat: an LLM is arguably *worse* at spotting future
inflections than at rating established names (they aren't in its training data, and it skews toward
whatever was hyped near its cutoff). Treat Special Stocks as a **bounded gamble**, not an edge — the
discipline is the cap.

**Decisions locked:** 20% sleeve ceiling (user-set) · **house-money funding** — bought only from gains
above the protected initial deposit, so principal is never gambled · separate exploratory prompt ·
core re-rated weekly, specials scouted **nightly + on every START** during market-closed idle time (open-path fallback) · **buy-and-hold** —
held names are never trimmed or exited so winners can run · **accumulate** the best (conviction-ordered,
≤12 names) with **high-conviction event rotation** when full (evict the weakest non-winner; never a
winner) · 5% *entry* cap per new name · **Research log** view from SQLite ·
paper-first.

**Engine:** `research.build_special_research_prompt` / `parse_special_research_json`;
`mock.generate_mock_special_research`; `autopilot.maybe_research_special` + the carve-out in
`build_rebalance_plan` (`special_symbols` / `special_allocation_pct` / `special_max_position_pct`).
**Headless test:** verified 19/19 (parser, carve-out math, 5% per-name cap, exit-on-drop, weekly cadence).

**Honest scope.** Paper only. The sleeve is high-variance — expect it to drag in most periods with the
occasional big winner. And because winners run uncapped (buy-and-hold), a single explosion can grow
into a large, concentrated position — that's the upside you're after, but the sleeve's risk *rises*
with its success, so it bears watching. The equity-vs-S&P curve (§19) is what will show whether the
satellite actually adds anything over just holding the core.

---

## 22. Home reminders (SMS) — shipped · smart reorder — planned

> **Status: SMS reminders SHIPPED** (free email-to-SMS via Gmail). **Grocery/supply reorder still
> PLANNED** (one-tap, never autonomous spend).

**SMS reminders (shipped).** HELIX texts the user's phone with their **due/overdue** tasks (from the
checklist status, §4) via **free email-to-SMS**: an email through Gmail SMTP to
`<number>@<carrier-gateway>`, which the carrier delivers as a text. Pure stdlib (`smtplib`/`email`).
- **Engine:** `helix/home/notify.py` — `send_text_via_email()` (Gmail SMTP, injectable for tests),
  `gateway_address()` (carrier → domain via `CARRIER_GATEWAYS`), `send_reminder()`, `is_configured()`,
  `sms_config()`; `helix/home/tasks.py` — pure `task_status` / `due_tasks` / `reminder_message`
  (shared by the GUI checklist and the headless notifier).
- **Config (Home tab):** sender Gmail (default `helixaifriend@gmail.com`), **Gmail App Password**
  (Gmail blocks the normal password — needs 2-Step Verification + an App Password), phone, carrier;
  a **Send test text** button. Secrets live in settings (plaintext, git-ignored).
- **In-app auto-text (shipped):** a Home-tab **"Auto-text my due tasks every N hour(s)"** checkbox +
  spinbox (`SMS_AUTO_ENABLED_SETTING` / `SMS_AUTO_HOURS_SETTING`) runs a `QTimer` that texts the
  due/overdue list on the chosen cadence — but **only when something is actually due** (it checks
  `due_tasks` first, so it never spams an all-clear). **Enabling it does an immediate first check**
  (`_on_auto_toggled` → `_auto_text_tick`), then every N hours; changing the interval or re-launching
  with it on does *not* re-send (no spam). Fires only while the app is open; off-thread send.
- **Cadence (Windows Task Scheduler):** for texts when the app is *closed*, schedule `notify` since the GUI only runs when open.
  Create Task → **Trigger:** Daily, 8:00 AM → **Action:** "Start a program" with **Program/script** =
  the Python path (`python`, or the full `…\python.exe`), **Add arguments** = `main.py notify`, and
  **Start in** = `C:\Users\brian\HELIX` (**required** — without it the task can't find `main.py`).
  Tick "Run whether user is logged on or not" for reliability, then right-click the task → **Run** to
  confirm it fires headlessly. (`--always` only for a forced test; the daily job stays bare so it's
  silent when nothing's due.)
- **Caveat:** carrier email-to-SMS gateways are being deprecated by some carriers; reliability varies.

**Smart grocery / supply reorder (planned — NOT autonomous spend).** There's no open ordering API for most
grocery services, and HELIX will **not place orders or move money on its own**. The useful, safe
version: track recurring items with a run-low cadence, flag them when due (and in the SMS), keep a
**standing list**, and offer **one-tap reorder deep links** to the store — the user taps to confirm
the purchase. Convenience without the risk; consistent with the financial-safety rule.

**Decisions locked:** SMS = free email-to-SMS via Gmail (App Password) · cadence via Task Scheduler ·
grocery = one-tap reorder list, **never** auto-purchase (financial-safety rule).

---

## 23. Xpert voice dialogue (J.A.R.V.I.S.) — two-way voice assistant

> **Status: v1 SHIPPED (paper-safe).** The one-way Xpert briefing (§4) is now a two-way **spoken
> conversation** that also **performs real actions** (§24) — you talk, HELIX listens, thinks
> (Claude), acts, and talks back. Short back-and-forths, longer when you ask for real analysis. The
> Iron Man feel. A **"Talk to HELIX"** panel sits at the top of the Xpert tab; the expert-opinion
> briefing moved below it.

**A turn (the state machine).** `Idle → Listening → Transcribing → Thinking → Acting → Speaking →
Idle`. Everything heavy runs **off the UI thread** (`spawn_worker`/`QThreadPool`); only state
transitions and widget updates happen in the main-thread callbacks. A live status line + busy bar
name the current step ("Transcribing…", "Thinking…", "Checking your portfolio…", "Speaking, sir.").

1. **Listen** — push-to-talk: **hold** the 🎤 button (or type in the text box) and **release** to
   send. `MicRecorder` (in `qt_app.py`) captures mic audio via QtMultimedia's **`QAudioSource`** as
   16 kHz mono PCM and writes a stdlib `wave` WAV. Guarded: if there's no mic/multimedia backend the
   button disables itself and you type instead.
2. **Transcribe** — `helix/ai/transcribe.py` → `transcribe(wav)` runs **local faster-whisper**
   (optional dep, lazy import, cached model). Audio never leaves the machine; the temp WAV is deleted
   after.
3. **Think** — `ClaudeClient.chat(messages, system, tools)` (multi-turn Messages API) with the JARVIS
   system prompt (`research.build_jarvis_chat_system(context)`) carrying **live HELIX context**
   (portfolio snapshot, track record, **due Home tasks**). **Prompt caching** marks the system prompt
   + tools block ephemeral, so the static prefix is cached across the turn's tool loop and across
   turns (cheaper/faster).
4. **Act** — Claude **tool-use** maps spoken commands to real HELIX functions (§24), driven by
   `actions.run_chat_turn`. Money/outward actions are gated (below).
5. **Speak** — the reply plays through the existing **edge-tts 1.5× neural voice** (`QMediaPlayer`),
   falls back to the OS voice offline, and shows in a scrolling **transcript** (You / HELIX). The talk
   button re-enables when playback ends (`EndOfMedia`/`stateChanged`, with a length-based safety
   timer so it never gets stuck).
6. **Repeat** — full Messages-API history (including tool_use/tool_result blocks) persists across
   turns; the tail is trimmed for cost/latency, never starting on a dangling tool turn. **New chat**
   resets it.

**Conversation style.** The JARVIS system prompt is tuned for **short spoken replies by default**
(one to three sentences, plain voice-friendly prose, addresses the user as "sir"), that **expand to
longer, detailed answers when asked** ("break down the portfolio", "why did you sell X"). Brevity
keeps turns snappy; depth on demand.

**Speech-to-text — local `faster-whisper`.** Claude has no audio input, so the *ears* are separate
from the *brain*. Chosen for the local-first/privacy ethos: **audio never leaves the machine, free,
no per-use cost**. It's an **optional dependency** (like edge-tts) — lazy-imported with a graceful
fallback (`transcribe.is_available()`; the tab shows a `pip install faster-whisper` hint and you can
type meanwhile). Trade-offs accepted: a heavier one-time install + model download, and CPU
transcription is slower than cloud. (Rejected: cloud STT — faster but sends voice out + another key;
Windows built-in — lower quality.)

**Multi-user (v1).** No per-user auth — just clean turn-taking. Anyone can speak or type; the system
prompt greets the owner as "sir" and helps others naturally.

**Hands-free wake word ("HELIX") — SHIPPED.** A **"Hands-free"** toggle turns the Xpert tab into an
always-listening assistant: say **"HELIX"** (the wake word the user chose — not "Hey HELIX") and it
acts, no button. Because "HELIX" is a custom word, the local/no-extra-dependency approach is used:
continuous capture → cheap **energy-based voice-activity detection** with an **adaptive threshold**
(`VadSegmenter`, stdlib RMS, no numpy/audioop) segments each spoken phrase → faster-whisper
transcribes it → if the phrase contains "HELIX" (`_split_wake`, tolerant of mis-hearings) the words
after it run as the command. The threshold **tracks the ambient noise floor** (`WAKE_SPEECH_FACTOR` ×
noise, floored at `WAKE_RMS_FLOOR`), so it adapts across mics — a quiet close-talk headset vs. a
noisier array mic — instead of a single fixed level that mis-fires on one and goes deaf on the other
(the original "glitchy" report). CPU stays low at idle (VAD only transcribes actual speech, not
silence). The listener **pauses itself while HELIX transcribes/thinks/speaks** (gated by
`_set_convo_state`, so it never hears its own reply), plus a **~450 ms guard after each reply**
(`_resume_wake`) so HELIX's own voice tail / room echo can't re-trigger the wake word. Once the wake
word lands and HELIX answers, a **conversation session** opens (`_start_session` / `_end_session`,
`SESSION_IDLE_MS` = 5 min): you keep talking — and answer confirmation prompts — **without repeating
the wake word**, each utterance/reply resetting the idle countdown. The session ends after 5 minutes
of quiet or **immediately on a dismissal phrase** ("goodbye", "be right back", "that's all", "thanks
HELIX" — `_is_dismissal`), after which HELIX says "Of course, sir" and returns to wake-word-only
listening. A subtle green **"In conversation"** pill (`session_label`) shows the live countdown.
Bare "HELIX" → "Yes, sir?" then it takes the next utterance.
**Opt-in each session** (no auto-start of an always-on mic). Engine: `WakeWordListener` +
`VadSegmenter` in `qt_app.py`. (A dedicated wake-word engine would avoid transcribing every phrase,
at the cost of a trained model/dependency — deferred.)

**Audio device pickers.** Mic + speaker dropdowns (default to the system default, e.g. a connected
Bluetooth headset) let you pin HELIX's ears and voice to a specific device; the choice persists
(`xpert_input_device` / `xpert_output_device`) and applies to push-to-talk capture, the wake
listener, and the edge-tts output (`QAudioOutput.setDevice`). A connected **Bluetooth headset** (the
user's Razer BlackShark) works with zero config when it's the default device.

**Latency & honesty.** Each turn = record + STT + Claude (+ any tool) + TTS, so expect a short beat;
brief replies keep it conversational. Local STT is private but slower on CPU. Hands-free transcribes
every nearby phrase to check for the wake word (best with a close-talk headset mic; it can mis-fire
on background speech). **Barge-in** (interrupting HELIX mid-reply) is still deferred.

### 23.1 v1 — as built (2026-06-04)

| Area | What shipped |
|---|---|
| Ears | `helix/ai/transcribe.py` — `transcribe()`/`is_available()`, local faster-whisper, cached model, graceful optional-dep fallback. `MicRecorder` (qt_app) — QtMultimedia `QAudioSource` → 16 kHz mono WAV. |
| Hands-free | `WakeWordListener` + `VadSegmenter` (qt_app) — always-listening **"HELIX"** wake word via energy VAD + Whisper, self-pausing during replies, follow-up window, opt-in toggle. Mic/speaker device pickers (`xpert_input_device`/`xpert_output_device`); Bluetooth headset works as the default. |
| Brain | `ClaudeClient.chat(messages, system, tools)` — multi-turn Messages API + tool-use, **prompt caching** on system + tools (shares `_post` with `complete()`). `research.build_jarvis_chat_system(context)`. |
| Act | `helix/ai/actions.py` — tool schemas, `ActionRouter`, `run_chat_turn`, confirmation gates (§24). |
| UI | `XpertTab` is now a **conversation-only** panel (the one-way Expert Opinion briefing + pillars table were removed): transcript + 🎤 hold-to-talk + type-to-HELIX + New chat + Hands-free toggle + mic meter + **voice-speed slider (0.8×–2.0×, `xpert_voice_speed`)** + Mic/Speaker pickers, the `Idle→…→Speaking` state machine, off-thread pipeline, reliable end-of-speech detection. Wired to the Investment + Home tabs via `bind_investment`/`bind_home`. |
| Cost | Chat + tool sub-calls use the cheap `DEFAULT_RESEARCH_MODEL` (Sonnet); every call's tokens recorded to `ai_usage`. |

**Decisions locked:** STT = **local `faster-whisper`** (private, free, optional dependency) ·
push-to-talk **and** an opt-in hands-free **"HELIX" wake word** (energy-VAD + Whisper, no extra
dependency) · mic/speaker device pickers (Bluetooth headset works by default) ·
short-by-default / longer-on-demand reply style · **tool-use for actions with confirmation gates on
anything money/outward** (§24) · chat on Sonnet with prompt caching · paper stays the default.

**Verified:** hermetic action-layer tests (gates, task-matching, the chat/tool loop) + an offscreen
Qt end-to-end run (typed turn → scripted tool-use → spoken reply; the gated-SMS confirm/cancel flow;
start-investing routed to the Investment tab). Compiles + imports clean; full window builds offscreen.

---

## 24. Xpert actions & tool-use (the "Act" layer)

> **Status: v1 SHIPPED (paper-safe).** The Xpert voice assistant doesn't just chat — it **performs
> real HELIX actions** via Claude tool-use, with hard safety gates on anything that spends real money
> or sends something outward.

**How it works.** `ClaudeClient.chat` is given a tool list; `actions.run_chat_turn` drives the loop:
Claude emits `tool_use` → `ActionRouter.run` executes the mapped engine/memory function → the result
is fed back as `tool_result` → Claude continues until it produces a spoken reply. The router is
**Qt-free** (an `ActionContext` of plain callables + the DB/settings), so it's unit-testable without
the UI; UI-touching effects are marshalled to the main thread via Qt signals.

**The tools (spoken command → real function):**

| Tool | Maps to | Notes |
|---|---|---|
| `get_portfolio` | `AlpacaClient` + `portfolio_snapshot` | Balance/cash/invested/open P&L + top holdings + run state. Read-only. |
| `get_recent_sells` | `memory.list_sells` | "what did we sell" — with realized result. Read-only. |
| `get_learning_status` | `memory.ai_usage_summary` | Learning pillar: Claude calls + estimated spend (today / month / all-time) from the `ai_usage` table. Read-only. |
| `get_track_record` | `memory.strategy_performance` + `investment_digest` | Hit rate / avg return / realized P/L. Read-only. |
| `set_auto_investing` | `InvestTab.voice_start` / `voice_stop` (via signal) | START/STOP the loop. Paper is immediate; **LIVE start is gated**. |
| `get_home_tasks` | `home.tasks.due_tasks` / `task_status` | What's due/overdue or the full list. Read-only. |
| `complete_home_task` | mutate `home_tasks` setting + refresh Home | Fuzzy-matches the spoken task name. |
| `add_home_task` | append to `home_tasks` + refresh Home | New recurring task. |
| `text_my_tasks` | `home.notify.send_reminder` | **Gated** (outward SMS). |
| `review_helix_100` | `autopilot.build_roster_review` | On-demand roster review; **reports** proposed swaps (auto-rotation still runs on its own cadence). AI call. |
| `scout_special_stocks` | `autopilot.maybe_research_special` (forced) | On-demand moonshot scout. AI call. |

**Safety — the spoken-confirmation gate.** Mirroring the GUI's confirmation dialogs, **LIVE
real-money trading and outward SMS never fire without an explicit spoken "yes."** When Claude calls a
gated tool, the router performs **no side effect** — it returns a "CONFIRMATION REQUIRED" result and
records a `pending` action; Claude then asks the user to confirm out loud. The gate is
**deterministic on the user's actual transcribed words** (`is_affirmative`/`is_negative`), *not* the
model's interpretation: a clear "yes/go ahead" executes the pending action exactly once; "no/cancel"
discards it; anything ambiguous abandons it (no implicit yes). Paper trading is the safe default and
needs no confirmation. The voice never switches Paper↔Real — that stays a deliberate manual toggle.

**Threading.** Read/AI tools run in the turn's worker (network/DB only). The two UI-bound effects —
start/stop auto-investing (the Investment tab's `QTimer`) and refreshing the Home checklist — are
emitted as **queued Qt signals** (`request_invest`/`request_home_refresh`) and handled on the main
thread; live investment state (`is_live`/`auto_running`/`keys_ready`) is snapshotted on the main
thread at the start of each turn so the worker never touches a widget.

**Honest scope.** v1 actions are paper-safe by construction. `review_helix_100` reports rather than
applies (rotation stays on its quarterly auto-cadence); the roster/special tools spend a Claude call
each (a few seconds). Wake-word, barge-in (interrupting mid-reply), and applying roster swaps by voice
are deferred.

---

## 25. Live market data for research (lifting the "training knowledge only" ceiling)

> **Status: v1 SHIPPED (core ratings).** The biggest historical limitation was that **all AI research
> reasoned from training knowledge, not live data** (every prompt said so). This wires **live price
> action + news** from Alpaca into the rating decision, so HELIX reasons from current reality.

**The flow.** When the core ratings are about to be (re-)computed (a cache-miss re-rate — weekly, or
forced), HELIX pulls live data and feeds a compact digest into the prompt:
1. **Prices/technicals** — `AlpacaClient.get_bars_multi()` fetches ~1 year of **weekly** OHLC bars for
   the whole universe in one paginated call (efficient — ~1 call for 100 names). `market_data.technical_line()`
   distills each into a one-liner: last price, **% vs 1-year high/low**, **~1mo & ~3mo momentum**, and
   **trend** vs a long moving average.
2. **News** — `AlpacaClient.get_news()` (Alpaca's free `/v1beta1/news`, IEX-tier) pulls the latest
   ~50 headlines; `market_data.news_by_symbol()` maps them onto the universe.
3. **Digest** — `market_data.build_market_context()` assembles a prompt block: per-name price reads
   (with the freshest relevant headline inline) + a short market-wide headline list for macro context.
4. **Prompt** — `build_portfolio_research_prompt(..., market_context=…)` injects it under "LIVE MARKET
   DATA (weigh this HEAVILY; it overrides stale assumptions)" and flips the honesty caveat from "you
   reason from training knowledge" to "use the live data as your source of truth."

**Lazy + cheap.** The fetch is a **callable** (`market_context_fn`) threaded through
`build_rebalance_plan` / `maybe_refresh_core_ratings` and invoked **only on an actual re-rate** — so
cache-hit cycles (the common case) make **no extra data calls**. Honors the §16 "Refresh AI research"
cost toggle. Pure helpers live in `helix/investment/market_data.py` (Qt-free, unit-tested); the fetch
+ wiring is `InvestTab._fetch_market_context`.

**Coverage — all three AI research paths are grounded.** The **core ratings** and the **HELIX 100
roster review** get the full digest (per-name technicals + news; the review weighs the incumbents'
price action when scoring), via `_fetch_market_context`. The **Special Stocks scout** gets a
**news-only** digest (`_fetch_news_context` → broad recent headlines, not filtered to the universe) —
moonshot candidates are *new* names, so what matters is spotting current inflections, not the core's
technicals. All are lazy (`market_context_fn` invoked only when that research actually fires).

**Honest scope & next steps.** **Fundamentals/earnings — now SHIPPED (§32):** real revenue growth,
margins, ROE and leverage are pulled free from **SEC EDGAR** and folded into this same digest (the
"LIVE MARKET DATA" block), so the core rating weighs the numbers too. The rest of the research roadmap: the **feedback loop** (read
the equity-vs-S&P curve + per-pick forward returns), **two-stage deep memos** + adversarial
verification, and **event-driven triggers**. The news endpoint is real and free on paper but
unverified against the live account here — every fetch is best-effort and degrades to
training-knowledge mode (`""`) if market data is unavailable.

---

## 26. Enterprise pillar — work command center (Slack + git work)

> **Status: v1 SHIPPED.** The Enterprise tab is no longer a placeholder. It answers *"what work got
> done, and what needs me?"* — combining recent **git activity across your projects** with your
> **Slack** mentions/DMs, summarized by Claude. Chase/banking was deferred (no Chase API for
> individuals — that's a later CSV/Plaid feature, see the §10 plan).

**The two connectors (both read-only, stdlib).**
1. **Git work** (`helix/enterprise/gitwork.py`) — for each configured project folder, shells out to
   `git log` (via `subprocess`; **never** `pull`/`fetch`/`checkout` — it only *reads*) and parses
   recent commits + line churn into a per-repo summary. Repo paths are a user-set list
   (`enterprise_git_repos`), defaulting to the HELIX repo so it works out of the box.
2. **Slack** (`helix/enterprise/slack.py`) — a hand-written `urllib` client over the Slack Web API
   (mirrors `AlpacaClient`). `gather_slack_digest()` calls `auth.test` → `users.conversations` →
   `conversations.history` (bounded: ≤25 channels, ≤30 msgs each) and flags **mentions** (`<@you>`),
   **DMs**, and busiest channels, ignoring your own messages. Auth is a **user token** (`xoxp-…`,
   git-ignored). **Read-only** — no posting (an outward action would be a gated feature later, like
   the Xpert SMS gate).

**The flow.** **Refresh and summarize** runs off-thread (`spawn_worker`): read git, pull Slack, feed
both digests into `research.build_enterprise_summary_prompt()`, and Claude (cheap Sonnet, the 300s
research timeout) writes a short update: what got done, who's waiting on a reply in Slack, and the one
thing to do next. The raw git and Slack digests show in side panels; Claude token cost is recorded to
`ai_usage`. Works with **just git** (no Slack token) — the Slack panel then prompts you to add a token.
The Slack lookback follows the tab's **Look back (days)** control (so it matches the git window).

**Human, brief, symbol-free output (voice-ready).** Because the Xpert voice assistant may read the
briefing aloud, the summary is **plain spoken prose with no markdown, bullets, or symbols** — the
prompt forbids them. The raw panels are likewise plain language: `format_git_digest` shows each
commit's subject (no hashes/dates/`+/-` churn symbols) and `format_slack_digest` **strips Slack's
`<@USERID>` mention codes, channel/link markup, and HTML entities** to clean sentences, names channels
in words ("the X channel", dashes/underscores spaced out), and pluralizes naturally. So everything in
the tab reads cleanly on screen and through the voice.

**UI.** `EnterpriseTab` (`qt_app.py`): a header + **Look back** (days) spinner + **⚙ Settings** +
**Refresh and summarize**, a busy bar/status line, the AI briefing, and the git/Slack panels. ⚙ Settings
holds the Slack token (with a **Test** button → `auth.test`) and the project repo paths (one per line),
plus setup help (the exact Slack scopes). Mirrors the Investment tab's shape and threading.

**Principles preserved.** Local-first (git is local; the only egress is the explicit Slack + Claude
calls), stdlib-only (`urllib`/`subprocess`), secrets git-ignored, **read-only** (nothing is posted,
pulled, or mutated), heavy work off the UI thread. **Verified:** hermetic tests — a real temp git repo
(commit parse + churn + bad-path skip), a stubbed Slack transport (mention/DM detection, own-message
filtering, error propagation), the prompt, and an offscreen `EnterpriseTab._gather` with Claude mocked
(briefing + panels populate; live settings untouched). Compiles + imports clean.

**Next steps.** Slack token setup is a few manual steps (create app, scopes, install); a `git fetch`
toggle to include un-pulled remote work; Gmail/Calendar connectors; **Chase via CSV import then Plaid**
(§10); Xpert voice tools (`get_enterprise_digest`); persisting digests/summaries to memory for trends.

---

## 27. Day-trade sleeve (short-term momentum — third allocation)

> **Status: v1 SHIPPED (paper).** A **third** sleeve beside the Core (§13/§20) and Special Stocks
> (§21): a small, fast-turnover **short-term momentum** book. Default 10% (user-set as "Day-trade %").

**Not intraday scalping.** HELIX trades on a cadence (15 min–1 day) with daily/weekly data, so this is
realistically a **short-term momentum / swing sleeve** — names held days to ~2 weeks on momentum or a
catalyst, then exited — not tick-by-tick day trading. (PDT note: a *real* account under $25k that makes
4+ day trades in 5 business days is restricted as a Pattern Day Trader; paper is exempt.)

**The loop:**
1. **Scout** (`maybe_research_daytrade`, cadence-gated, default daily) — `build_daytrade_research_prompt`
   asks Claude for the strongest current momentum / catalyst names; parsed with
   `parse_special_research_json` (same `{symbol, conviction, thesis}` shape). Unlike Special's
   buy-and-hold *accumulate*, this **replaces** the pick list with the freshest momentum each scout
   (`invest_daytrade_stocks`); theses go to `stock_rationale` (action `daytrade`).
2. **Size + exit** (carve-out in `build_rebalance_plan`) — `daytrade_budget = base × daytrade_pct` is
   carved off the top alongside Special, so `core_investable = investable − special − daytrade`. Held
   day-trade names use **take-profit / stop-loss** exits (`DAYTRADE_TAKE_PROFIT_PCT` +15% /
   `DAYTRADE_STOP_LOSS_PCT` −8%, read from the position's `unrealized_plpc` via `holdings_pl`); a name
   that **rotated off** the pick list gets no target and is exited by the generic loop; otherwise it's
   held (rides toward the target or the stop). Fresh picks are bought from the remaining budget, capped
   per-name (`DEFAULT_DAYTRADE_MAX_POSITION_PCT` 5%). Sell reasons ("day-trade: take profit / stop loss")
   flow to `sell_log`.
3. **Distinct from the others** — `daytrade_set = picks − roster − special`, so day-trade names are
   excluded from Core roster buys and the Special set; the three sleeves never double-allocate.

**UI.** A **Day-trade %** control + a **Scout day-trade stocks** cadence spinner in ⚙ Settings, a third
**Day-trade — short-term momentum** Assets table, the live 3-way heading split (Core / Special /
Day-trade), and a Day-trade section in the Research log. Scouted on both the off-hours and open paths
like Special.

**Honest scope.** Short-term trading is the hardest place to make money and most active traders lose;
an LLM rating short-term moves is *less* reliable than rating long-term quality, so this is the
**riskiest, smallest** sleeve by design — the take-profit/stop-loss discipline + the % cap are the risk
controls. Paper-first. **Engine:** `research.build_daytrade_research_prompt`,
`autopilot.maybe_research_daytrade` + the carve-out/exits in `build_rebalance_plan`. **Verified:**
hermetic test (3-way budget split, take-profit/stop-loss exits, per-name-capped entry, cadence-gated
scout). **Next steps:** a **time-based exit** (close after N days regardless of P&L — needs per-position
hold duration from `list_symbol_trades`); conviction-weighted day-trade sizing; UI-tunable TP/SL
thresholds; intraday data/cadence if HELIX ever moves to faster signals.

---

## 28. Prediction scorecard — the measurement layer (MEASURE BEFORE YOU OPTIMIZE)

> **Status: v1 SHIPPED (paper).** Principle #0 of the investing roadmap: before tuning *how HELIX
> decides or trades*, build the layer that tells you whether its predictions are any good. This is
> that layer — per-pick forward-return tracking, bucketed by confidence, scored against the S&P 500.
> Deterministic (≈zero Claude spend) and **purely additive — it changes no trading decision.**

**Why it was needed (two measured gaps, 2026-06-05).** Against the live paper DB:
1. **Ratings had no history.** `stock_rationale` is current-state (one upserted row per symbol,
   overwritten every re-rate), so last week's ratings were gone. HELIX literally could not ask "did
   the names I rated buy/high beat the ones I rated buy/medium, or the S&P?" — the core question.
2. **The only realized signal was mostly noise.** Of 638 `sell_log` rows, **607 (95%) were
   "trim to target/cap"** rebalance churn (avg return −0.12%, avg |return| ~1%). So
   `strategy_performance()`'s "56% hit rate" was measuring drift-band trims, not pick quality — and
   the §17 feedback loop was calibrating on that noise.

**The data model.** A new append-only table — **`rating_outcomes`** (`symbol`, `action`,
`confidence`, `rationale`, `created_at`) — is the historical counterpart to `stock_rationale`. It is
**never updated**: every genuine re-rate appends a fresh snapshot via `memory.record_rating_snapshots`,
called right alongside `save_stock_rationales` at all four rating sites (core re-rate in
`build_rebalance_plan`, the off-hours `maybe_refresh_core_ratings`, and the `special` / `daytrade`
scouts). So the log accrues "on date D, HELIX rated SYMBOL action/confidence." Snapshots are bounded
by the existing cadence (core ~weekly, special/day-trade ~nightly) and pruned in the rolling 1-year
window. On first launch an existing account is **seeded once** from the current `stock_rationale`
(`_seed_rating_outcomes`, idempotent, no-op on a fresh DB) so forward-return buckets begin maturing
from the ratings it already has, not from the next re-rate a week out. Prices are **not** stored —
the scorer reconstructs entry/forward closes from daily bars by date (more precise than the weekly
bars the prompt saw, and durable to schema).

**The engine (pure, in `autopilot.py`; all price data injected).**
- `bars_to_dated_closes(bars)` → `[(YYYY-MM-DD, close)]` (the structured form the scorer needs;
  `parse_stock_bars` for the equity curve drops the dates).
- `score_rating_snapshots(snapshots, closes_by_symbol, spy_closes, asof=…)` → `[RatingOutcome]`. For
  each (snapshot, horizon): **entry** = first close on/after the rating date; **exit** = first close
  on/after rating_date + horizon days. Horizons are **1w / 1m / 3m** (`RATING_HORIZONS`). An outcome
  is `matured` once the horizon has fully elapsed as of `asof`; immature ones are returned (exit =
  latest close) and flagged so headline stats are never polluted by half-elapsed windows. Each carries
  the name's `return_pct`, SPY's `benchmark_pct` over the same dates, and the `excess_pct` (the edge
  vs. just owning the index).
- `summarize_rating_outcomes(outcomes)` → buckets by **(action, confidence)** within each horizon:
  matured count, avg forward return, **hit rate** (% positive), avg **excess vs SPY**, and a `pending`
  count.
- `render_rating_scorecard(summary)` → fixed-width report with a per-horizon **verdict line** that
  states plainly whether buy/high beats the S&P and whether it beats buy/low ("conviction is paying" /
  "is NOT paying").
- Edge orchestration (also `autopilot.py`, broker injected): `fetch_scorecard_prices(client, symbols,
  start)` pulls daily closes + SPY in one paginated `get_bars_multi` call; `generate_rating_scorecard(
  memory, client)` ties snapshots + fetch + score + render. `build_rating_scorecard(...)` is the pure
  score→summarize→render wrapper.

**Surfaces.** Desktop: a **Prediction scorecard** button beside the Investment tab's Research log
(runs off-thread — fetching daily bars for the whole rated universe is slow — then shows a monospace
report). Headless: **`python main.py scorecard [--days N]`** (no Claude call, no trading; just the DB
+ Alpaca price reads). Both answer one question at a glance: *do high-conviction buys actually beat
low-conviction and the index, at 1w / 1m / 3m?*

**Honest scope.** Forward returns are **gross** of spreads/taxes; the scorecard is **empty today and
fills in over weeks** as ratings age past each horizon (that is the point of building it first). It
measures *ratings*, not the *traded book* (sizing/exits are what the Build-2 backtest harness will
score). It now also **feeds back into the rating prompts** (§38, the closed loop) — though it remains a
soft prompt-level signal, not a numerical optimizer, and the scoreboard every later change must prove
itself against.

**Decisions locked:** append-only `rating_outcomes` (never overwritten) · snapshot on every genuine
re-rate, piggybacking the existing cadence · **price-free snapshots, returns reconstructed from daily
bars by date** · horizons 1w/1m/3m · bucket by action×confidence · matured-only stats with a separate
pending count · excess-vs-SPY + a plain verdict · seed once from `stock_rationale` · paper-first.

**Verified (2026-06-05):** hermetic engine test (28 checks — forward-return math, horizon/maturity
gating, SPY excess, bucketing, hit rate, the verdict, the empty-state, and the memory accessors +
one-time seed); an end-to-end run against the **real paper account on a copy of the live DB** (532
ratings seeded; 4 backdated picks produced real forward returns from real Alpaca bars — 1m buy/high
+6.6%, beating SPY by 1.6 pts); and an offscreen `InvestmentTab` smoke test of the button + dialog.
Compiles + imports clean.

**Next (then measured improvement):** the **backtest harness** is now shipped too (§29 — Principle
#0(b)). With both measurement tools in place, the research + trading improvements (fundamentals/earnings
input, a quantitative factor backbone with the LLM as a calibrated overlay, real concentration +
smarter exits + risk caps) come next — **each proven against this scorecard and the backtest, not
hoped for.**

---

## 29. Backtest harness — scoring the deterministic strategy (MEASURE BEFORE YOU OPTIMIZE, part b)

> **Status: v1 SHIPPED (paper).** The second half of Principle #0. The §28 scorecard measures *pick*
> quality forward; this measures the *trading machinery* — sizing + rebalancing — by replaying real
> historical bars through HELIX's **actual** engine with the LLM ratings held fixed.

**What it does.** `helix/investment/backtest.py` steps day-by-day through real daily bars, marking the
portfolio to market each day and, on a cadence (default weekly), calling the **real**
`build_rebalance_plan` to compute target weights and "trade" at that bar's close. The LLM ratings are
**stubbed** — a `research_fn` that returns a fixed JSON rating set — so the run is deterministic and
spends **no Claude tokens**. Because it drives the production engine (not a re-implementation), it
tests the same conviction-weighted sizing (8:3:1), cash buffer, roster authority, and drift-band
rebalancing that trade live.

**The A/B is the point.** It replays several legs over the **same basket and the same prices** —
`conviction-weighted` (Aggressive preset = `CONFIDENCE_WEIGHT`) vs `equal-weight` (Balanced = flat
weights), a **`conviction + vol-adj`** leg (§31 inverse-vol tilt), plus a **concentration sweep** of
top-N caps (§30: uncapped / top-50 / top-25 / top-10) — with SPY buy-and-hold as the benchmark line. Those differences are **look-ahead-neutral**: every leg
sees identical information, so the gaps are honest reads on whether the *sizing scheme* and the
*concentration level* add value, even though the *absolute* return is look-ahead-biased (replaying
today's ratings over past prices — the labels may "know" recent winners).

**Metrics** (pure, from the daily equity curve): total return, annualized return, annualized vol,
**Sharpe** (rf=0, annualized from the realized period spacing), **max drawdown**, % up-periods, and
**return vs S&P** (the `alpha` line). A plain verdict states whether conviction-weighting beat
equal-weight and by how much (return pts + Sharpe).

**Engine + edges.** Pure: `run_backtest(closes_by_symbol, ratings, …)` (the replay), `render_backtest`
(comparison table + verdict), `_PriceBook` (forward-filled date→close lookup), the metric helpers.
Edge: `gather_backtest(memory, client, …)` reads the current **buy-rated** core names from
`stock_rationale`, fetches their daily bars + SPY in one `get_bars_multi` call (only the traded set —
watch/skip never enter the book), and runs both legs. **CLI:** `python main.py backtest [--days N]
[--cadence-days N] [--cash-buffer P]` — no Claude, no trading, only price reads.

**Honest scope.** Idealized and **gross**: fills at the close, fractional shares, **no spread,
slippage, or taxes** — so a live book would net less. Ratings are held constant over the replay (no
re-rating with point-in-time data — that needs historical LLM calls, out of scope), so the core never
acts on a *downgrade* in the backtest; it tests sizing + drift rebalancing, not rating churn. Absolute
return is look-ahead-biased — **trust the A/B and the vs-S&P gap, not the level.** Day-trade
take-profit/stop-loss and the Special sleeve aren't replayed yet (core only) — a natural next step.

**Decisions locked:** drive the **real** `build_rebalance_plan` (not a re-implementation) · stub
ratings for a deterministic, zero-token replay · **A/B conviction vs equal-weight** as the
look-ahead-neutral signal · daily mark-to-market, weekly rebalance default · gross/idealized fills,
clearly captioned · core buy-rated universe only (v1) · CLI-first.

**Verified (2026-06-05):** hermetic test (22 checks — drawdown & Sharpe math, `_PriceBook`
forward-fill, a full replay where conviction-weighting must beat equal-weight on a winner-heavy basket,
the render/verdict, the not-enough-data path, and `gather_backtest` with stub memory+client excluding
watch-rated names); a **real-data run against the paper account** (208 buy-rated names, ~6 months:
conviction +26.4% / Sharpe 4.1 vs equal +24.2% / Sharpe 4.0 vs SPY +8.2% — conviction added ~2.2 pts,
metrics in a sane range, with the look-ahead caveat noted). Compiles + imports clean.

**Down-market risk-control A/B (§35, 2026-06-06).** `run_backtest` also takes a bounded `start_day`
window + a `risk_controls` flag, and `gather_risk_control_backtest` (CLI: **`backtest --down-markets`**)
replays the current buy basket twice — guards OFF vs ON — over the **2022 bear** and **2020 crash**,
computing the regime filter (SPY vs its 200‑day trend) and the drawdown peak per rebalance date. The
on‑vs‑off gap is the clean read (same basket/prices); the absolute level keeps the look‑ahead +
survivorship bias. **Real‑data finding:** in 2022 the guards cut max drawdown **17.1%→13.8%** at a small
Sharpe cost; 2020 daily history isn't on the free IEX feed, so that window is flagged "insufficient"
rather than shown as a flat 0%. Only the cash‑raising guards are exercised (stop‑loss / sector‑cap /
diversification need cost‑basis / a sector map absent in replay). Verified by a hermetic test (synthetic
falling market: guards cut drawdown 43%→35% and preserved more capital; the degenerate‑window label)
plus the live 2022/2020 run.

---

## 30. Concentration — top-N position cap (scan wide, hold the best)

> **Status: v1 SHIPPED (paper), default OFF.** The first trading improvement built *after* the
> measurement layer, and proven against it. Adds a real concentration lever so capital can pile into
> the best ideas instead of spreading into a closet index — Brian's "scan wide, concentrate in the
> best." Per his direction the lever is a **position-count cap**, not a per-stock %.

**Why.** With the per-stock cap removed and ~480 names in the universe (~208 rated buy), even steep
8:3:1 conviction weighting leaves top positions at ~0.5–0.7% — effectively an index. A **top-N cap**
concentrates the same capital into the N best names (e.g. top-25 → ~4% each).

**The mechanism.** `build_rebalance_plan` gains `max_positions` (0 = uncapped, the default) and
`factor_scores`. When set, after the buy list is built it keeps the **top N**, ranked by **(conviction
tier, then factor score, then symbol)** — so the model's high/medium/low picks the tiers and a
**deterministic momentum/trend factor** (`market_data.factor_signals`, pure, frequency-agnostic)
breaks ties *within* a tier (which highs make the cut). Survivors are sized by the existing conviction
weighting; names that miss the cut get target $0 and are exited like any other non-buy — so enabling
it **deliberately sells the also-rans to concentrate** (which is why it defaults to off and is a
conscious choice). The factor data is a cheap weekly-bars fetch made **only when a cap is set**
(`InvestTab._fetch_factor_scores`), so uncapped cycles pay nothing.

**Measured first (the discipline).** The §29 backtest runs a **concentration sweep** — uncapped /
top-50 / top-25 / top-10 (plus equal-weight + SPY) — so N is chosen on evidence. On the real paper
basket (~6 months): tighter N **raised total return** (top-10 +52% vs uncapped +26%) but **raised
volatility and *lowered* risk-adjusted return** (Sharpe ~3.0 vs ~4.1, vol 29% vs 11%). So
concentration is a **return/risk dial, not free alpha** — and the heavy look-ahead bias (replaying
today's ratings) most inflates the tightest book, so the honest read is "Sharpe says holding broad won
this window; concentrate only if you want more return for more drawdown." (The backtest's *factor
ranking* is point-in-time and look-ahead-free even though the ratings aren't.) The capability is
proven correct; the choice of N is now informed, not hoped.

**Surfaces.** Settings → **Max core positions** (`INVEST_MAX_POSITIONS_SETTING`, default 0 = "All
(uncapped)") with a tooltip steering you to backtest first. CLI: `python main.py backtest` sweeps the
N's by default; `--max-positions N` tests a specific cap.

**Decisions locked:** lever = **top-N position-count cap** (not a per-stock %, per Brian) · rank by
conviction tier → momentum/trend factor → symbol · survivors keep conviction-weighted sizing
(8:3:1) · **default uncapped** (enabling it is a deliberate, concentrating rebalance) · factor fetch
only when a cap is set · pick N from the backtest sweep, not by feel · paper-first.

**Verified (2026-06-05):** hermetic test (13 checks — `factor_signals` ranking, the top-N cut picks
the right names by conviction+score, concentration raises per-name size, deterministic without scores,
and a backtest where concentrating into momentum winners beats holding all); a real-data concentration
sweep (the finding above); an offscreen UI smoke test (control builds, defaults to 0, persists, dialog
opens). Compiles + imports clean.

**Next (Workstream B):** the other half of "real concentration" — **volatility-adjusted sizing** — is
now shipped too (§31); then **sector/correlation caps** (don't make one macro bet five times), a
**max-drawdown / de-risk** rule, and **regime awareness**. Each proven on the backtest + scorecard.

## 31. Volatility-adjusted sizing (the risk half of "real concentration")

> **Status: v1 SHIPPED (paper), default OFF.** The companion to the top-N cap (§30): instead of
> changing *which* names HELIX holds, it changes *how much* of each — tilting toward steadier names so
> each position contributes more equal **risk**. Theory says this usually lifts Sharpe / cuts drawdown;
> the backtest is here to check that on the actual book (and it honestly didn't, on this window — see below).

**The mechanism.** `build_rebalance_plan` gains `volatilities` + `vol_adjust`. When on, each buy
weight is multiplied by a **bounded inverse-volatility factor** — `median_vol / vol`, clamped to
**[0.25×, 4×]** — so a name with the median vol is unchanged, a steadier name gets up to 4× and a
jumpier one down to 0.25×. Conviction (8:3:1) stays the **primary** driver; this is a tilt on top, not
a replacement. Anchoring to the **median** keeps the book's gross exposure ~unchanged (it redistributes
within the sleeve, doesn't lever up or hoard cash). Volatility is `market_data.volatility_signals`
(pure — stdev of bar-to-bar returns; only the *relative* level matters, so it's frequency-agnostic and
shares the §30 weekly-bars fetch). Names without a vol estimate stay at 1×.

**Measured (the honest result).** The §29 backtest adds a **`conviction + vol-adj`** leg (same basket,
same prices, look-ahead-neutral). On the real paper basket (~6 months): vol-adjust **did** cut
volatility (11.4% → 9.6%) and max drawdown (5.3% → 4.9%) exactly as designed — but it **gave up more
return** (Sharpe 4.13 → 3.79). Why: this window was a **bull run where the high-vol names *were* the
winners**, so down-weighting volatility down-weighted the leaders (and the look-ahead-biased ratings
amplify that, since they "know" the winners). So on trending-up data it's a **risk-smoothing lever, not
free alpha** — it tends to pay off in choppy/bear regimes, not a momentum bull. The capability is built
and correct; the evidence says don't expect a Sharpe win right now.

**Surfaces.** Settings → **Volatility-adjusted sizing** toggle (`INVEST_VOL_ADJUST_SETTING`, default
off). The backtest A/Bs it automatically, so the effect on *your* basket/regime is always one
`python main.py backtest` away.

**Decisions locked:** bounded inverse-vol tilt (median-anchored, clamped [0.25×,4×]) **on top of**
conviction · volatility from trailing returns, point-in-time in the backtest (no look-ahead) ·
**default off**, opt-in like §30 · share the §30 bars fetch · judge it on the backtest per-regime, not
by faith · paper-first.

**Verified (2026-06-05):** hermetic test (9 checks — `volatility_signals` ranking, the steadier name
gets more at equal conviction, the tilt is bounded against tiny-vol blow-ups, off/no-data is a no-op,
and the backtest leg runs + reports); the real-data A/B above; an offscreen UI smoke test (toggle
builds, defaults off, persists, dialog opens). Compiles + imports clean.

**Next (Workstream B):** the vol-adjust finding (great in chop, costly in a bull) is the case for
**regime awareness** (§B.4) — apply the vol tilt / de-risking only when SPY is high-vol or
down-trending. Then **sector/correlation caps** (don't make one macro bet five times) and a
**max-drawdown de-risk** rule.

## 32. Fundamentals input — real numbers in the rating (closing the biggest data gap)

> **Status: v1 SHIPPED (paper), default ON.** Until now every rating reasoned from **price + news +
> training memory** — vibes and momentum, the gap flagged in §10/§25. This feeds **real fundamentals**
> (revenue growth, margins, ROE, leverage) into the rating prompt so HELIX weighs the *numbers*, not
> just the story. The first improvement aimed at the **picks themselves**, not how they're sized.

**Source — SEC EDGAR (free, keyless, official).** No new dependency, no API key, urllib-only — fully in
the stdlib/local-first ethos. The naive route (`companyfacts`) is **3.6 MB per company** (~700 MB for
the universe); instead we use the **XBRL frames API** (`/api/xbrl/frames/us-gaap/{concept}/{unit}/CY{year}.json`),
which returns one financial concept across **all** filers in a single ~900 KB request — so the whole
~480-name universe is covered in **~15 bulk requests (~13 MB) in ~7 seconds**. (SEC requires a plain
`Name email` User-Agent; a fancier one with a URL/parentheses gets a **403** — learned the hard way,
pinned in `SEC_USER_AGENT`.)

**What's computed** (`helix/investment/fundamentals.py`, pure helpers + an injectable `get_fn` so tests
never hit the network): per name, from the latest annual frame + the prior year — **revenue & YoY
growth, net margin, gross margin, ROE, debt/equity**, plus a 0-1 **`fundamental_score`** quality
composite (rewards growth/margin/ROE, lightly penalizes heavy leverage). Rendered as one compact line
per name, e.g. `AAPL: FY2025 rev $416.2B (+6% YoY), net margin 27%, gross margin 47%, ROE 127%, D/E 3.3`.

**How it's wired.** A new current-state **`fundamentals`** table (symbol → metrics JSON, upserted)
caches the data; `InvestTab._maybe_refresh_fundamentals` does the SEC pull on a **monthly** cadence
(settings-gated timestamp, off-thread, best-effort — a failure is surfaced, not fatal, and leaves the
clock unset to retry). The weekly re-rate just **reads the cache locally**: `_fetch_market_context`
appends a `fundamentals_block` for the chunk's symbols into the existing "LIVE MARKET DATA" digest, so
no prompt-signature change and it rides the same per-chunk plumbing as price/news.

**Coverage & honesty.** Live: **97% (461/477)** of the real universe in ~7 s. The ~3% misses are
**non-calendar-fiscal-year** filers (e.g. NVDA, whose FY ends in late January) that don't align to the
calendar-year frames — they simply keep price/news with no fundamentals (best-effort, like the news
path). Data is **annual and lagged** (last 10-K), which suits a long-term rating but won't catch a
fresh quarter. **GUI-only** for now (the CLI `rebalance`/`autopilot` don't use the live market context
yet, same scope as §25). And like all research-input changes, its payoff can't be backtested (§29
stubs ratings) — it will show up in the **forward §28 scorecard** as ratings made *with* fundamentals
mature over the coming weeks.

**Surfaces.** Settings → **Use SEC fundamentals in research** (`INVEST_FUNDAMENTALS_SETTING`, default
**on**) + **Refresh fundamentals** cadence (default 30 days). On by default because it's strictly
better *input*, not a risk knob — but toggleable.

**Decisions locked:** SEC EDGAR **frames** (bulk, keyless) over companyfacts (too heavy) · plain
`Name email` UA · monthly cache in a `fundamentals` table, read locally on re-rate · fold into the
existing market-context digest (no new prompt param) · best-effort, per-name graceful degradation ·
**default on** · GUI-first · paper-first.

**Verified (2026-06-05):** hermetic test (23 checks — CIK/frame parsing, metric extraction incl.
growth/margins/ROE/leverage and net-income-only & no-data fallbacks, the line/score/block renderers,
`build_market_context` integration, `fetch_fundamentals` with a stub `get_fn`, and the memory cache
round-trip); a **live SEC run** (97% coverage in 7 s, spot-checked AAPL/MSFT/JPM/XOM/KO/ADBE — numbers
correct, `fundamental_score` cleanly separates ADBE 0.84 from American Airlines 0.14); an offscreen UI
smoke (toggle defaults on, the monthly guard no-ops without network, dialog opens). Compiles + imports clean.

**Next:** a `companyfacts` fallback to recover the ~3% non-calendar-FY misses; estimate revisions /
analyst data (needs a keyed API — a deliberate step away from keyless); and feeding `fundamental_score`
into the §30 ranking factor (a quality tilt on concentration, blending momentum + quality).

## 33. Factor backbone + LLM overlay (quant + LLM, not LLM-alone)

> **Status: v1 SHIPPED (paper), default OFF.** Composes everything the session built — **momentum**
> (§30), **quality** (§32 SEC fundamentals), **low-vol** (§31) — into one deterministic composite
> factor, and uses Claude as an **overlay/check on the numbers** rather than the sole decider. The
> decision becomes quant + LLM.

**The composite.** `composite_factor_scores(momentum, quality, volatility)` (pure, in `autopilot.py`)
percentile-ranks each factor across the universe (robust to outliers) and blends them
(`FACTOR_WEIGHTS` = momentum 0.4 / quality 0.4 / low-vol 0.2), renormalizing over whichever factors a
name has. Output: a 0-1 score per name. It also **upgrades the §30 concentration ranking** — when the
overlay is on, the top-N cut ranks on this full composite instead of momentum alone.

**The overlay.** `apply_factor_overlay(ratings, composite)` (pure) tempers the model's call with the
numbers: a **`buy` whose composite is in the weak tail (< 0.20) is downgraded to `watch`** (the
numbers contradict the story), and a **strong-factor `buy` (≥ 0.80) gets a confidence bump**.
Conservative by design — it checks and confirms the model's buys, it never invents new ones. Applied
inside `build_rebalance_plan` after ratings resolve but **after** the §28 scorecard snapshot, so the
scorecard still measures the *model's own* call while trading acts on the blended decision.

**Wiring.** The edge (`_run_cycle`) computes the composite from one weekly-bar fetch (momentum + vol)
plus the cached SEC `fundamental_score` (§32 quality), and passes it as `factor_scores` with
`factor_overlay=True`. The backtest A/Bs a **`conviction + factor-overlay`** leg. Settings → **Factor
overlay** toggle, default **off**.

**Measured — honest, and with a real caveat.** On the live paper basket (~6 months) the overlay came
in at **Sharpe 3.85 vs 4.13 (−0.28), −3.2 pts return** — slightly negative, the **least costly** of
the three re-weighting levers (vol-adj −0.34, top-25 −1.50). **But the backtest overlay is
momentum + low-vol only** — replay has no SEC fundamentals — so it cannot test the **quality** leg,
which is the part most likely to add durable edge. So the backtest says "tempering on price/vol alone
didn't help in this bull window" (consistent with §30/§31); the **quality dimension is a forward bet**
that only the §28 scorecard can settle as overlay-blended ratings mature. Default off; enable to trade
the blended decision once the forward data supports it.

**The cross-cutting finding (§30–§33).** Every deterministic lever that **re-weights or filters the
same LLM picks** — concentration, vol-adjust, factor-overlay-on-price — tested **neutral-to-slightly-
negative on Sharpe** in this single look-ahead-biased bull window. That's the honest, important result:
in a near-efficient market, re-sizing the same names doesn't manufacture risk-adjusted edge. The
improvements that change the **inputs** — real fundamentals (§32) and the quality dimension of the
overlay — are the ones whose value can't be backtested here and must prove out **forward on the
scorecard**. The measurement layer did its job: it stopped HELIX from shipping sizing knobs as if they
were edge.

**Decisions locked:** composite = percentile-blend of momentum/quality/low-vol · **LLM-as-overlay**,
conservative (veto weak-factor buys, bump strong; never invent buys) · snapshot the model's raw call,
trade the blend · composite also drives §30 ranking · **default off**, A/B on the backtest · paper-first.

**Verified (2026-06-05):** hermetic test (17 checks — percentile ranks incl. invert/single, the
composite blend + weight renormalization, the overlay veto/boost/conservatism/purity, the engine
dropping a vetoed buy, and the backtest leg avoiding a downtrend); the real-data A/B above; an
offscreen UI smoke (toggle defaults off, persists, dialog opens). Compiles + imports clean.

**Next:** a **value** factor (trailing P/E from SEC EPS + live price) to round out the composite; a v2
that inverts to **quant-base / LLM-overlay** (the brief's framing) once the scorecard shows the factor
carries signal; and tunable overlay thresholds.

## 34. Adversarial pick-checking — bull vs bear vs judge (refute the buy before committing)

> **Status: v1 SHIPPED (paper), default OFF.** A research-quality improvement aimed at the *picks*:
> the single-pass rating waves through plausible-but-fragile buys, so before HELIX commits capital it
> makes the model **argue against its own buy** — a bull case, a forced bear case to refute it, then an
> impartial judge — and **downgrades any buy that doesn't survive**.

**The mechanism.** `research.build_adversarial_prompt(symbol, …)` + `parse_adversarial_json` (one
structured call per candidate): the model states the strongest BULL case, then the strongest BEAR case
(actively trying to refute the buy), then rules as a referee → `{bull, bear, verdict (buy/watch/skip),
confidence, rationale}`. `autopilot.apply_adversarial_review` runs it on the **top buy candidates by
conviction** (bounded to `ADVERSARIAL_MAX_CHECKS` = 12, since they get the most capital), and
**overrides the rating with the judge's verdict** — a buy that fails becomes watch/skip. Conservative
and pure: it only re-examines buys (never invents them), keeps the original rating if the response
doesn't parse, and the per-name market/fundamentals context + `research_fn` are injected (stubbable).

**Where it runs.** Inside the re-rate path (`build_rebalance_plan` fresh + the off-hours
`maybe_refresh_core_ratings`), **before** the rating is persisted/snapshotted — so the §28 scorecard
records the *considered* decision and the Research log shows the bull/bear reasoning. One extra Claude
call per checked name, so it's **opt-in** (Settings → "Bull-vs-bear check on top buys", default off).

**Calibrated, then measured (honest live finding).** Validated live on 3 real high-conviction buys: the
model produced sharp, data-grounded reasoning (caught a +17% post-earnings surge, a binary WWDC
catalyst, a 7% net-margin question) — but a first pass downgraded **3/3** purely on "it's run up." That
would just park HELIX in cash, so the judge prompt was **calibrated** to reserve downgrades for a
genuinely fragile thesis / serious risk / multi-year-impairing valuation, *not* mere near-term
extension (durable compounders stay expensive; this is a long-term book). It still found specific,
defensible "wait for a better entry" cases on those three. **Net: it's a demanding skeptic** — with it
on, expect a more patient, cash-heavier posture. Whether that caution pays (do the downgraded names
pull back, or keep running?) is precisely what the §28 scorecard settles forward — it can't be
backtested (the §29 replay stubs ratings).

**Decisions locked:** one structured **bull+bear+judge call** per candidate (cost-efficient vs separate
agents) · bounded to the top-N conviction buys · judge **overrides** the rating, snapshot records the
considered decision · keep the original buy on a parse failure (never silently drop) · prompt calibrated
to catch fragile theses, not risen prices · **default off** (extra Claude cost + a real posture change) ·
GUI-first · paper-first.

**Verified (2026-06-06):** hermetic test (18 checks — the prompt structure, the parser incl.
bad-verdict/garbage/coercion, `apply_adversarial_review` overriding verdicts with the cap + conviction
ordering respected, watches untouched, purity, and unparseable-keeps-original, plus `build_rebalance_plan`
vetoing a killed buy); a **live 3-name run** (real Claude produced parseable, sharp verdicts; the
calibration round); an offscreen UI smoke (toggle defaults off, persists, dialog opens). Compiles +
imports clean.

**Next:** a true **independent ensemble** (separate bull / bear / judge calls, not one prompt) and
**median-of-N** ratings to cut variance — both higher token cost; surfacing the bull/bear text in the
Research log; and event-driven triggers (re-check a name on a big move or fresh news).

## 35. Risk controls — keep one bet, one crash, or one blow-up from sinking you

> **Status: v1 SHIPPED (paper), default ON.** Five protective controls bundled into a `RiskControls`
> object and applied inside `build_rebalance_plan`. Unlike the return levers (§30–§34, default off until
> proven), these are **prudence guards** and default **on** — they don't chase return, they cap the
> ways you lose. With conservative thresholds they sit **dormant in good times** and only act when
> conditions turn (validated live: all five idle right now — bull regime, 3.5% drawdown, book well‑spread).

| Control | Rule (default) | How it works |
|---|---|---|
| **Sector cap** | ≤ **25%** of the book per sector | Trims any over‑cap sector's core targets pro‑rata; freed budget → cash. Sector from a **curated GICS map + SEC‑SIC enrichment** (~full universe coverage, see below); still‑unresolved names exempt. |
| **Drawdown brake** | Down **15%** from peak → hold **40%** cash | Raises the cash buffer (deploys less) once equity falls past the brake vs its high‑water mark (`equity_history`). |
| **Regime filter** | SPY below its long trend → hold **40%** cash | `market_data.regime_risk_off` (latest SPY weekly close < ~40‑bar MA ≈ 200‑day) flips the book defensive in a downtrend. |
| **Per‑stock stop‑loss** | Core name down **25%** → exit | A deep catastrophe brake; overrides any buy/hold target (uses live `unrealized_plpc`). Special (buy‑and‑hold) and day‑trade (own ±TP/SL) are exempt. |
| **Diversification floor** | Never concentrate below **~20** names | Lifts the §30 top‑N cap from below, so concentration can't leave you in too few names. (Only bites when the concentration cap is on.) |

**Where they live.** All in `build_rebalance_plan` via `risk: RiskControls`: the defensive buffer
(drawdown + regime) reduces `investable` before sizing; the floor bounds the top‑N selection; the
sector cap (`apply_sector_cap`, pure) trims core targets; the stop‑loss forces a held name's target to
$0 before actions. Pure helpers (`compute_drawdown`, `apply_sector_cap`, `regime_risk_off`) are unit‑
tested; the edge (`InvestTab._compute_risk_controls`) assembles them from the Settings toggles + live
data (equity peak, SPY regime, sector map), best‑effort — a failed fetch disables just that one trigger.

**Surfaces.** Settings → a **Risk controls** section with five toggles (all default on). Thresholds are
the `DEFAULT_*` constants in `autopilot.py` (25% / 15% / 40% / 25% / 20 names), tunable in code.

**Sector data — curated map + SEC enrichment (~full coverage).** The sector cap resolves each name's
sector in two layers: a **curated GICS‑style map** (`sectors.SECTOR_MAP`, accurate, ~40% of the
universe by count but most of its weight), and for every name it misses, **SEC enrichment** — pull the
company's **SIC code** from SEC EDGAR submissions (`fetch_sectors`, free/keyless) and translate it
(`sic_to_sector`, a coarse SIC→sector mapping) — cached in a `sectors` table. The curated map **wins**
where both exist (GICS is finer than SIC). `InvestTab._maybe_refresh_sectors` fills the gaps on a long
cadence (sectors are ~static), **bounded per run** (`SECTORS_FETCH_LIMIT` = 150), so it completes over a
couple of cycles. **Verified live:** a single batch resolved 94% of the unmapped tail correctly (AAL→
Industrials, ADM→Staples, AEE→Utilities…), lifting coverage to ~70% in one pass and toward ~full over
the next cycle; true ETFs/non‑filers (e.g. ARKK) have no SIC and stay exempt (correctly — they aren't
one sector). A handful of transient fetch misses simply retry next cycle.

**Honest scope.** These trade some upside for safety: the **drawdown brake and regime filter raise cash
in downturns**, so they can **lag a sharp recovery** — that's the deliberate cost of not riding a crash
down. The **stop‑loss supersedes** the old §13 "stay the course / rebalancing buys the dip" rule **for
deep (−25%) losers only** — a catastrophe brake, not a tight stop. SIC is coarser than GICS, so a few
tail names may land in a rough bucket; the curated map covers the high‑weight names precisely. **GUI‑only**
(same scope as the other live wiring). On a **down**‑market backtest (§29 `--down-markets`) the
cash‑raising guards behave as designed — see "Next" below.

**Decisions locked:** five controls, **default on** (protective) · conservative thresholds (catastrophe/
prudence guards, not hair‑triggers) · bundled in `RiskControls`, pure helpers + edge assembly · sector
cap on a curated map with unmapped exempt · defensive buffer is one mechanism with two triggers
(drawdown OR regime) · stop‑loss is core‑only and deep · floor guards the concentration cap · paper‑first.

**Verified (2026-06-06):** hermetic test (25 checks — the sector map incl. the no‑stray‑ticker check,
`compute_drawdown` / `apply_sector_cap` / `regime_risk_off`, and `build_rebalance_plan` for each control:
brake & regime raising cash to 60% deployed, sector cap trimming tech to 25% while leaving an under‑cap
sector, the −30% name stopped out with the right reason while a −4% name is kept, and the floor lifting a
top‑5 cap to 20 names); a **live end‑to‑end run** (real SPY regime, equity peak, 40% sector coverage, a
207‑buy plan built clean with all caps satisfied); an offscreen UI smoke (five toggles default on, persist,
`_compute_risk_controls` honors them, dialog opens). Compiles + imports clean.

**Next:** **SEC‑SIC sector enrichment is now SHIPPED** (above — the cap covers ~the whole universe
automatically). UI‑tunable thresholds were **declined** by the user (HELIX should "think under the
hood," not add knobs). **Down‑market backtest — SHIPPED** (§29 `backtest --down-markets`): on the real
**2022 bear** the cash‑raising guards (drawdown brake + regime filter) cut max drawdown **~17.1%→13.8%**
at a small Sharpe cost (the insurance trade‑off) — they do what they're for; 2020 isn't on the free
feed. Remaining: refine the SIC→sector buckets over time.

## 36. Real-market screener — every discovered name is an actual, buyable ticker

> **Status: v1 SHIPPED (paper), always on.** Closes the discovery gap from §10/§20: candidate
> discovery (core rotation, Special, Day-trade) drew on **Claude's knowledge**, which can name a
> hallucinated, delisted, or un-buyable ticker. Now **every discovered name is validated against the
> real, tradeable market list** before it can enter any sleeve.

**Source — Alpaca's asset list.** `AlpacaClient.get_assets()` (`/v2/assets`, free, keyless beyond the
existing keys) returns the broker's master list — **13,792** active US equities. This is the
*authoritative* "real, tradeable ticker" universe, because it's literally what HELIX can trade.

**The filter (`tradable_symbols`, pure).** Keeps names that are **active + tradable + on a major
exchange** (NYSE/NASDAQ/ARCA/AMEX/BATS — **OTC excluded**) + **fractionable**. The fractionable
requirement matters: HELIX places **notional (dollar) orders**, which only execute on fractionable
assets — so this is the set HELIX can *actually buy*, not just names that exist. Result: **~7,021**
real, buyable tickers.

**How it's wired.** A `market_assets` cache table holds the set, refreshed **weekly**
(`InvestTab._maybe_refresh_market_assets`, one free call). Each cycle loads it and passes it as
`tradable` to all three discovery paths — `build_roster_review` (candidates), `maybe_research_special`,
`maybe_research_daytrade` — which **drop any pick not in the set**. So a name only enters a sleeve if
it's a real, tradeable, notional-buyable market ticker. Safe fallback: if the cache is empty/unavailable
(`tradable=None`), no filtering happens (degrade gracefully rather than block trading).

**Honest scope.** This is a **tradability** screen (real + buyable), **not** a quality or liquidity
screen — Alpaca assets carry no volume/market-cap, and there's no free keyless **S&P-500-membership**
API, so "is it S&P‑caliber" stays the AI's judgment (+ the roster scoring). It guarantees no
hallucinated/delisted/un‑buyable name enters a sleeve; it does **not** guarantee the name is large or
liquid. The existing roster seed isn't pruned (only *new* discoveries are screened). A true
volume/market‑cap quality screen would need another data source.

**Decisions locked:** Alpaca `/v2/assets` as the authoritative tradeable universe · filter to active +
tradable + major‑exchange + **fractionable** (notional‑order‑buyable) · weekly cache in `market_assets`
· validate all three discovery paths, drop non‑tradeable picks · safe no‑op when the list is missing ·
keyless · paper‑first.

**Verified (2026-06-06):** hermetic test (10 checks — the filter incl. OTC/non‑fractionable/inactive
exclusion, the cache snapshot replace, and Special/Day‑trade/roster discovery each dropping a
hallucinated ticker while keeping the real one, plus the no‑list fallback); a **live run** (Alpaca
returned 13,792 assets → 7,021 tradeable; all 7 of the §20 roster discoveries — BRK.B, NVO, MELI, ROL,
WST, DECK, CSGP — pass the screener, confirming legitimate picks aren't wrongly excluded); an offscreen
smoke (caches when stale, no‑ops when fresh). Compiles + imports clean.

**Next:** a real **quality/liquidity screen** (min average volume / market cap) and exact **S&P‑500
membership** would need an additional data source — the next step toward discovery that's not just
"real and buyable" but "real, buyable, and the right caliber."

## 37. Quality / liquidity screen — discoveries that are big and liquid enough, per sleeve

> **Status: v1 SHIPPED (paper), always on.** §36 made discoveries *real and buyable*; this makes them
> *worth holding* — a **per-sleeve** filter on top, so a discovered name must clear a liquidity (and,
> for the core, quality) bar before it can enter a sleeve.

**Per-sleeve, because the sleeves want different things.** The Core wants big/liquid/quality
(S&P‑caliber); Special (moonshots) and Day‑trade (momentum) need *liquidity* to enter/exit but **not**
the size/quality gates that would defeat their purpose (early names are small and often unprofitable).
So `SCREEN_PROFILES` sets different thresholds:

| Sleeve | Min price | Min avg daily $ vol (IEX) | Quality gate |
|---|---|---|---|
| **core** | $5 | ~$1M (≈ $25–50M true) | yes — drop deeply unprofitable / over‑levered |
| **special** | $2 | ~$0.2M | no |
| **day‑trade** | $3 | ~$1M | no |

**The screen (`screen_candidates`, pure).** **Liquidity is required** — last price + avg daily dollar
volume from recent daily bars; no bars ⇒ fail (can't confirm it's tradeable). **Quality is lenient**
and core‑only — using the cached SEC fundamentals (§32), drop a name *only* if it's clearly bad (net
margin below the floor, or debt/equity over the cap); missing fundamentals pass (don't over‑reject).
`liquidity_metrics` (in `market_data`, pure) extracts price + dollar volume from bars.

**How it's wired.** Each cycle the edge builds a per‑sleeve `screen_fn` (`InvestTab._make_screen_fn`)
that fetches the candidates' recent daily bars + reads their cached fundamentals and returns the
passing subset; it's passed into all three discovery paths (`build_roster_review`,
`maybe_research_special`, `maybe_research_daytrade`) alongside the §36 `tradable` set. So a discovery
must be **real + buyable (§36)** *and* **liquid + (core) quality (§37)** to enter. To make quality
cover *new* candidates (not just the roster), the §32 fundamentals refresh now caches the **whole
tradeable universe** (same bulk frames call — just extract more).

**The IEX-feed caveat (important + honest).** The free Alpaca data feed reports only **IEX‑exchange
volume (~2–5% of consolidated)**, so absolute dollar‑volume thresholds are IEX‑scaled, not true
volume — that's why the floors look small (~$1M IEX ≈ $25–50M real). Calibrated live so genuine S&P
names pass (Rollins, Deckers, CoStar all show $11–18M IEX) while penny/near‑zero‑volume junk fails.
It reliably separates real large/mid‑caps from junk; it is **not** an exact market‑cap or
consolidated‑volume figure (that would need the paid SIP feed) — so it's a *size/liquidity proxy*, and
exact market‑cap is still a future refinement.

**Decisions locked:** per‑sleeve thresholds (core strict, special/day‑trade liquidity‑only) · liquidity
required, quality lenient + core‑only · IEX‑scaled floors, calibrated against real S&P names · whole‑
tradeable‑universe fundamentals so quality screens new candidates · baked thresholds (no UI knobs, per
"think under the hood") · paper‑first.

**Verified (2026-06-06):** hermetic test (12 checks — `liquidity_metrics`, the required‑liquidity gate
incl. penny/illiquid/no‑bars rejection, the core‑only quality gate dropping unprofitable/over‑levered
names while missing‑fundamentals pass, per‑sleeve threshold differences, and `screen_fn` wired into the
special + roster discovery paths); a **live run** that exposed and fixed the IEX‑volume miscalibration
(then 7/7 real discoveries pass, broad‑market sample narrows to ~8/30). Compiles + imports clean.

**Next:** exact **market‑cap** (shares × price) and **consolidated** volume would need shares‑outstanding
+ the SIP feed — the step from "real, buyable, liquid" to a precise size/quality screen.

## 38. Close the loop — scorecard feedback into the rating prompts

> **Status: v1 SHIPPED (paper) — plumbing live, effect pending maturation.** The §28 scorecard
> measured pick quality but never reached the model. This wires that measurement back into the rating
> prompts so HELIX self-calibrates its confidence — automatically, with **no new UI knob** (Brian's
> "think under the hood" preference). It is an **input-quality** change, the kind the scorecard itself
> will judge — not another deterministic re-weighting lever.

**The gap it closes.** The §17 feedback loop fed prompts the realized closed-trade record — which §28
showed is ~95% rebalance-trim noise. The scorecard already scores every rating's forward return,
bucketed by confidence, net of the S&P (1w/1m/3m), but it was **read-only**. So HELIX scored its own
conviction and never acted on it.

**The design (one chokepoint, full reuse).**
- `scorecard_feedback(summary, *, min_n=SCORECARD_FEEDBACK_MIN_N)` (pure, `autopilot.py`) distills the
  scorecard `summary` dict into one calibration line. Only **buy** buckets with at least `min_n`
  (= **8**) *matured* outcomes are trusted; below that it stays silent (no calibrating on noise). It
  reports the **longest** qualifying horizon first (3m > 1m > 1w — a long-term strategy shouldn't tune
  on a one-week blip), up to two, and **names the horizon**. It **reuses `_conviction_verdict`** (the
  same function the rendered scorecard uses) so the prompt and the report can never drift apart.
  Returns `""` when nothing has matured enough — today's state.
- `refresh_scorecard_feedback(memory, client, settings, *, today=None)` (`autopilot.py`) sources the
  line, **daily-cached** in settings (`invest_scorecard_feedback` + `…_date`, compared by date string,
  following the `invest_last_*` convention). The full-universe bar fetch runs at most once/day; every
  other cycle reads the cached line for free. **Best-effort:** any failure (keys/network/closed)
  returns the cached line or `""` and never raises into the trading cycle.
- `performance_digest(…, scorecard="")` places the scorecard clause **first** (lead with the
  high-signal measurement; the noisier realized-sell record follows). The GUI's `_performance_review`
  (the single point that builds the `track` string for **all** research paths) sources `scorecard=` —
  so core / special / day-trade / roster prompts all get it from one change. No `research.py` edit, no
  schema change, no new control.

**Honest scope.** This ships the **mechanism**, proven deterministically. Its **real-world effect is a
forward bet**: the scorecard is empty/immature today (3,518 snapshots, 0 matured as of 2026-06-06), so
the line injects nothing until outcomes age past a horizon (weeks), then turns on by itself. The
backtest can't judge it (it stubs ratings), so the **§28 scorecard itself is the judge** over time.

**Deferred:** CLI parity (`rebalance`/`autopilot` still use the `performance_digest` fallback without
the scorecard line) — the GUI is the live loop Brian runs permanently; the same helper can be wired
into the CLI later. Per-sleeve (special/day-trade) calibration lines are a natural extension.

**Decisions locked:** distill in a pure fn reusing `_conviction_verdict` · gate buy buckets at
min_n=8 matured · longest qualifying horizon first, up to two, horizon named · daily settings cache,
date-string keyed · scorecard clause **leads** the digest · best-effort, never sinks a cycle · no UI
knob · GUI-first (CLI deferred) · paper-first, not financial advice.

**Verified (2026-06-06):** hermetic distiller/digest/cache test (28 checks, temp/fake objects, no live
DB — the min_n gate, longest-horizon-first selection, sign handling, empty-state silence, no
non-traded-label leak, agreement with `_conviction_verdict`, the lead ordering, and the cache's
fetch-once / no-refetch-same-day / raise-safe / no-snapshot-no-fetch invariants); `compileall` +
`import helix.interfaces.qt_app` clean; offscreen `InvestmentTab` smoke (builds on a copy of the DB);
`python main.py scorecard` unchanged (confirms the shared engine + the honest empty state today).

## 39. Always-on reliability — keep the permanently-running app alive and diagnosable

> **Status: SHIPPED (paper).** Brian runs HELIX as a **permanently-open desktop app** rather than via
> OS schedulers (the QTimer cadence does the scheduling once it's up). So "ready to use unattended"
> means the app must survive an unexpected error, recover its loop, leave a trail, and come back after
> a crash or reboot — without new knobs.

**Why it was needed.** PyQt6 **aborts the whole process** (qFatal) on an unhandled exception in a slot
*unless* `sys.excepthook` is replaced. A permanently-running trader can't be one stray UI-callback
error away from silently dying. The per-cycle path was already resilient (a failed `_run_cycle` is
caught by `spawn_worker` and `_cycle_done` reschedules), but a main-thread display error could still
take the app down or leave the loop wedged, and there was **no log** to diagnose an unattended issue.

**What shipped (`helix/core/reliability.py` + the GUI entry/cycle, `scripts/`).**
- **Crash guard** — `install_crash_guard()` replaces `sys.excepthook` so any unhandled exception is
  **logged and the app keeps running** (KeyboardInterrupt still exits). Installed in `run_qt_app`
  before the event loop. The single most important always-on safeguard.
- **Rotating log** — `setup_logging()` writes `data/helix.log` (1 MB × 3, stdlib `RotatingFileHandler`,
  git-ignored). Startup/exit, each auto cycle (start / ok / failed), and any caught crash are logged,
  so an unattended run is diagnosable after the fact. Best-effort: a log failure never stops the app.
- **Self-healing cycle** — `_cycle_done` clears the busy flag and **re-arms the next cycle in a
  `finally`**, so a display error mid-handler can never wedge the loop; `_auto_tick` guards its launch
  the same way. A **heartbeat** (`invest_last_cycle_ok`) records the last good cycle.
- **Relaunch supervisor** — `scripts/run_helix.py` runs the app and **relaunches it on a hard crash**
  (segfault / OOM / interpreter death), with exponential backoff that resets after a healthy run; a
  clean exit (closing the window) stops it. Pure `next_action()` policy, unit-tested; dependency-free.
- **Launch at login** — `scripts/install_autostart.ps1` (opt-in, run once) drops a Startup-folder
  shortcut pointing at the supervisor — **not** Task Scheduler; it just auto-opens the app, matching
  the always-on model. Remove via `shell:startup`.
- **Auto-resume trading** — the RUNNING state is persisted (`invest_auto_running`); on launch
  `_maybe_resume_auto` resumes auto-investing if it was on — **PAPER only, no dialog** (mirrors
  voice_start). LIVE never auto-resumes (the real-money gate stays manual). This is what makes a
  hard-crash relaunch actually pick trading back up instead of coming back STOPPED (the soft crash
  guard already preserves state by keeping the process alive). Pressing STOP clears the flag.

**Honest scope / the laptop-sleep caveat.** This is a laptop: HELIX **does not trade while the machine
is asleep** (QTimers pause). On wake the market-aligned loop re-checks the next open and resumes, but a
machine asleep at 9:30 AM ET won't trade at that open. For true always-on, set Windows power to **never
sleep on AC**. The supervisor covers process crashes, not the OS being suspended.

**Decisions locked:** permanently-on app, not OS-scheduled cadence (supersedes the Task Scheduler
notes in §14/§22 for the trading loop) · custom `sys.excepthook` keeps the app alive + logs · rotating
`data/helix.log` · cycle self-heals in `finally` + heartbeat · external dependency-free supervisor for
hard crashes · auto-start via a Startup shortcut, opt-in · **auto-resume paper trading on launch; LIVE
stays manual** · no new UI knob · paper-first.

**Verified (2026-06-06):** hermetic test (12 checks — logging setup idempotent + writes the file; the
crash guard installs, logs, keeps alive, and routes KeyboardInterrupt to the default hook; and the
supervisor's relaunch / backoff / reset policy); `compileall` (incl. `scripts/`) + `import
helix.interfaces.qt_app` clean; offscreen `InvestmentTab` smoke builds with the new wiring.

## 40. Data-breadth discovery — find stocks beyond what the model knows

> **Status: SHIPPED (paper).** Discovery used to be bounded by Claude's memory: the roster review asked
> the model to *brainstorm* candidates, and the data only *filtered* them (§36/§37). This adds a
> market-data screener that *generates* candidates from the **whole tradeable market** (~7,000 names),
> so HELIX can surface names the model would never name — Brian's "scan wide" thesis, made data-driven.

**The gap it closes.** `build_roster_discovery_prompt` had the model propose new tickers from training
recall; §36/§37 then validated/filtered them. So a name the model didn't think of was invisible, no
matter how strong its data.

**The design (reuse + one injection point).**
- **Pure screener** — `screen_market_candidates(bars_by_symbol, *, exclude, top_n, min_price,
  min_dollar_volume, quality, min_bars)` (`autopilot.py`): a liquidity gate (last price + avg daily
  dollar volume) drops penny / illiquid names, then it ranks the rest by the existing **composite
  factor** (`composite_factor_scores`: momentum via `factor_signals` + low-vol via `volatility_signals`
  + optional SEC quality) and returns the top-N. Pure — bars injected — so it finds names by data, not
  memory, and is testable without a network.
- **Edge** — `discover_market_candidates(client, pool, …)`: fetches daily bars for `pool` in batches (a
  failed batch is skipped, not fatal) and screens them. Best-effort; only the bars fetch hits the network.
- **Bounded, rotating scan** — the GUI's `_discover_seed_candidates` pulls the full tradeable list
  (`memory.get_tradable_universe()`, ~7,000 names, §36), scans a **rotating `DISCOVERY_SCAN_LIMIT` (400)
  slice** per review (persisted `invest_discovery_offset` wraps over the market across cycles), excludes
  current holdings, and returns the top `DISCOVERY_TOP_N` (25). This respects rate limits + the free
  feed and fills the market over time — the same "scan a slice each cycle" pattern as the fundamentals /
  sector enrichment.
- **The model still judges.** Candidates flow in via `seed_fn` → `maybe_rotate_roster` (invoked **only
  when a review is actually due**, so no wasted fetch on a skipped cycle) → `build_roster_review` →
  `build_roster_discovery_prompt(seed_candidates=…)`, where the model **scores the data-surfaced names
  head-to-head** against the weakest incumbents, and the §36/§37 screens still gate the result. So
  generation is data-driven; vetting stays AI + screens.

**Honest scope.** Liquidity/volume are **IEX-feed-scaled** (~2-5% of consolidated volume), so the
liquidity gate is approximate and true market-cap / volume still needs the paid SIP feed. The scan is
**bounded** (400/review, rotating), so full-market coverage accrues over several reviews, not in one
pass. SEC quality is only applied to names whose fundamentals are already cached; pure newcomers rank on
momentum + low-vol + liquidity until their fundamentals fill in. The model + the §36/§37 screens remain
the final gate — the screener widens the funnel, it doesn't auto-buy.

**Decisions locked:** screener *generates*, model + screens *vet* · pure ranker reuses
`composite_factor_scores` · bounded rotating slice of the real tradeable universe (fills over cycles) ·
`seed_fn` invoked only when a review runs (no wasted fetch) · no new UI knob · paper-first.

**Verified (2026-06-06):** hermetic test (13 checks — the liquidity gate, exclude/SPY/min-bars
filtering, composite ranking order, top_n, batching + failed-batch resilience, and the seeded prompt); a
**live spot-check** (scanned a slice of the real **7,021-name** universe → real liquid candidates); an
offscreen `InvestmentTab` smoke; compile + import clean. CLI parity (`rebalance`/`autopilot` headless)
is a follow-up; the GUI is the live loop.

## 41. Packaging — a standalone HELIX.exe for the tablet

> **Status: SHIPPED (paper).** A `build.py` at the repo root packages HELIX into a standalone Windows
> executable (PyInstaller) so it runs on a wall-mounted living-room **Windows tablet** with no Python
> install — Brian's "mount it and use it during the day" setup.

**Build.** `python build.py` → `dist\HELIX\HELIX.exe` (PyInstaller `--onedir --windowed`). Flags:
`--with-voice` also bundles the Xpert STT/TTS stack (faster-whisper / edge-tts — heavy, native deps);
`--console` keeps a console to debug a first build; `--dry-run` prints the command only. The default is
a **lean dashboard build** — the voice deps are imported lazily (`helix/ai/transcribe.py`,
`speech.py`), so excluding them keeps the bundle small + reliable and the app still launches (voice
simply inactive). Build on Windows (PyInstaller emits a same-OS binary); `pip install pyinstaller` first.

**Frozen data dir.** `config._default_root()` returns the folder **next to the .exe** when `sys.frozen`
(else the repo root) — so `data\` (the SQLite DB + the plaintext keys) lives beside `HELIX.exe`,
persistent and editable, not trapped inside the bundle. On first launch the exe creates an empty
`data\`; copy your `data\helix_settings.json` (Alpaca + Claude keys) and `data\helix.db` into
`dist\HELIX\data\` to carry over the account + history.

**Deploy to the tablet.** Copy the whole `dist\HELIX\` folder over, drop your `data\` in, double-click
`HELIX.exe` (or point `scripts\install_autostart.ps1` at it for launch-at-login + the §39 supervisor).
Press START once → paper trading auto-resumes on every later launch (§39). `dist\`, `build\`, `*.spec`
are git-ignored.

**Honest scope.** Windows only — PyQt6 can't run on iPad/Android (there a packaged exe isn't possible;
remote into the always-on PC instead). The bundle is large (Qt), larger with `--with-voice`;
faster-whisper downloads its model on first voice use (needs internet). Keys travel in **plaintext**
inside `data\` — treat the tablet as a trusted device. **Verified (2026-06-06):** the frozen-vs-dev
root resolution is unit-tested, both `--dry-run` commands are correct, and `compileall` + import are
clean; a real lean `--onedir` build was run to confirm packaging (see the session log).

## 42. Core-satellite + the whole tradable market (de-risk + breadth)

> **Status: SHIPPED (paper).** Two paired changes after a live paper run showed the book trailing the
> S&P with a high-beta speculative tilt and a universe limited to fractionable names: **(a)** an **index
> core** + **lighter speculative** sleeves so the book tracks the market by default and the AI is the
> satellite; **(b)** expand the tradable universe from ~7,000 fractionable names to **all ~12,722
> tradable names** via **whole-share orders** for the non-fractionable ones.

**Why (measured, 2026-06).** A ~12-day paper run: account −2.4% vs S&P +1.9%. The drawdown came from
the **speculative sleeves** — the top positions were high-beta names (MSTR, ASTS, QBTS, RKLB, ARKK)
sized ~15–20× the core's ~$235 slices, so a handful of moonshots drove the whole swing. And only big
names appeared in Special/Day-trade partly because the universe was filtered to **fractionable** names
(7,044 of 12,722 tradable) — the fractionable requirement alone dropped ~5,700 mostly smaller names.

**Core-satellite (the engine).** `build_rebalance_plan` gains `index_symbol` + `index_allocation_pct`:
an index ETF (default **VOO 40%**) is carved off the top like the other sleeves and held at a fixed
target (set AFTER, so exempt from, the sector cap — it *is* the market). The AI **Core** gets what's
left after Index + Special + Day-trade + the cash buffer. The new default mix is **Index 40 / Core ~35 /
Special 10 / Day-trade 5 / cash 10** (down from Special 20 / Day-trade 10, and from the user's saved
30/20) — a one-time, flag-guarded migration (`_apply_core_satellite_default`) moves an existing account
onto it on the next launch; the **Index core %** slider (Settings → Sleeves) tunes it. VOO is
fractionable, so the normal notional path fills it — no order-path risk.

**The whole tradable market (universe + order path).** The asset cache now stores `fractionable` per
symbol (`tradable_assets` → `replace_market_assets`; the fetch drops `require_fractionable`), so
`get_tradable_universe()` returns all **12,722** names and `get_nonfractionable_symbols()` flags the
~5,700 whole-share-only ones. `execute_rebalance` routes by fractionability: **fractionable → the
original notional dollar order (unchanged)**; **non-fractionable BUY → a WHOLE-share `qty` order**
(priced from recent daily bars, skipped if the dollar amount can't afford even one share);
**non-fractionable SELL → `close_position`** (full exit, or a percentage trim via `DELETE
/v2/positions/{symbol}`). So the ~5,700 smaller names the fractionable filter used to drop are now
tradeable — cheap ones fit even the core's small slices; pricier ones land in the larger sleeves.

**Safety / gradualness.** The fractionable path is **byte-for-byte unchanged**, and with no
`nonfractionable` set `execute_rebalance` behaves exactly as before. Non-fractionable handling only
activates once the weekly asset refresh re-populates the universe (expanded + flagged) AND a
non-fractionable name actually enters a sleeve — so the change rolls in gradually, never retroactively
disrupting current holdings. Live-mode gating and "one failure never aborts the batch" are preserved.

**Honest scope.** The index core is the surest lever (captures the market, caps the downside of bad
picks); the lighter speculative + broader universe are bets the §28 scorecard still judges. Whole-share
fills round a non-fractionable name's size DOWN to whole shares (skipped if unaffordable). Liquidity is
still IEX-feed-scaled (§37). Paper-first; not financial advice.

**Decisions locked:** index core = a fixed-target ETF sleeve (VOO 40% default), carved off the top,
sector-cap-exempt · lighter speculative defaults (Special 10 / Day-trade 5), one-time migration,
slider-adjustable · universe = ALL tradable names (fractionable flag stored) · non-fractionable buy →
whole shares (skip if < 1 share), sell → close-position · fractionable path unchanged, gradual rollout ·
paper-first.

**Verified (2026-06-06):** hermetic engine test (index target carved at the right $, core reduced,
sleeves+buffer sum, off when pct=0 — 8 checks); hermetic order-path test with a stub broker (frac
notional unchanged, non-frac whole-share buy = 80, skip-if-<1-share / no-price, full-exit + 25%-trim
close-position, failed-order resilience, no-nonfrac back-compat — 10 checks); a **live spot-check** (the
universe expands to **12,722** names, **5,678** flagged non-fractionable); compile + import clean.

## 43. Archive — feature provenance, versioning & safe rollback (§selfdev)

> **Status: SHIPPED.** A user-facing **Archive** (a core menu item) over the self-improvement loop:
> every self-built feature records the **prompt that constructed it** in SQLite, the whole-app version
> history is restorable, there is a user-set **master default** and an immutable **ROOT baseline** (a
> blank-menu factory reset), versions can be **purged**, and there is a manual **GitHub backup**. The
> aim: the user never loses work permanently, and a broken self-edit is always recoverable.

**The split: git stores, SQLite indexes.** Every self-improvement already lands as one `--no-ff` merge
on `main` (§selfdev) — an immutable, revertible version. So **git/GitHub is the version store**; we do
*not* reinvent snapshots. `helix/core/memory.py` adds two tables as the **human-friendly index**:
`interface_versions` (one row per app version: commit, label, prompt, the `is_default` / `is_root`
pointers) and `feature_provenance` (the construction prompt(s) behind each menu button, keyed by
feature). `helix/selfdev/versioning.py` is the Qt-free engine; `ArchiveTab` in `qt_app.py` is the view.

**Provenance + cleanup (Phase 1).** `versioning.sync(memory, settings, repo)` reconciles git merge
history into the index — idempotent, so it **backfills** the existing versions and self-heals. For each
merge it stores the version (prompt = the coder task / merge body), and by diffing `MENU_FEATURES` at
the merge vs its parent it **attributes the prompt to the menu button it built**. It then
`prune_feature_provenance(registry.feature_keys())` — so removing a feature (its ✕) **cleans up its
SQLite rows**. Sync runs ~8s after launch and whenever the Archive opens, off-thread.

**Archive UI (Phase 2).** A vertical list of version cards (newest first), each showing its label, date,
and the prompt that built it. Whole-app snapshots — a card's **Restore** rolls the *entire* app back.

**Restore + defaults + ROOT (Phase 3).** Restores are **non-destructive**: `restore_version` does
`git read-tree -u --reset <commit>` then commits the result on `main` (history is never rewritten;
nothing is lost), and sets the §selfdev restart flag so the app relaunches into the restored code via
the shared `_apply_restart_if_safe`. **Master default** = a user-pinned known-good checkpoint.
**Reset to Default (Root)** = restore the immutable **root baseline** (pinned once at install = the
then-current known-good HEAD) **with a blank menu** (`MENU_FEATURES = []`, core pillars only) — the
factory-reset lifeline. Root and default are **non-purgeable** (no ✕; enforced in the SQL `WHERE` too).

**Purge + backup (Phase 4).** Per-version ✕ = **permanent purge**: delete its work branch + its index
row. It deliberately does **not** rewrite `main`'s history (the one operation that could corrupt the
repo / lose unrelated work), so a merged version's code persists in the timeline even after its Archive
entry is gone. Backup is **local-first**: a manual **Back up to GitHub** button pushes `main` to origin.

**Decisions locked (Brian, 2026-06-19):** revert = whole-app snapshots · ✕ = permanent purge (no
history rewrite) · backup = local-only + manual push · ROOT = modern-core-at-install + blank menu,
immutable & non-purgeable · the red ✕ hover means *destructive* (remove/purge), neutral means *hide*.

**Verified (2026-06-19):** `sync` backfilled 18 versions from real history with correct per-feature
provenance (grocery/components/risk); a **throwaway-clone test** confirmed `reset_to_root` blanks the
menu (`[]`) and `restore_version` rolls the whole app back, both as clean non-destructive commits;
an **offscreen-Qt test** rendered the Archive (cards + ROOT) without error; compile + import clean.

## 44. The Twelve Commandments — immutable guardrails for a self-writing system (§selfdev)

> **Status: SHIPPED.** HELIX rewrites its own code, so it needs laws it *cannot* rewrite. Twelve
> commandments + a set of locked settings are now **enforced, not just stated**: the coder is forbidden
> to touch the safety machinery, and the approval gate **auto-rejects any self-change that edits a
> protected path** — the one chokepoint nothing self-written can bypass. Settings and the Archive are
> made permanent, and a read-only Guardrails panel shows the law.

**The constitution.** `helix/selfdev/constitution.py` holds the twelve `Commandment`s, the
`LOCKED_SETTINGS` (declared constants — *not* in the editable settings JSON, so no voice/chat/coder path
can flip them), `PERMANENT_MENU_KEYS` (`settings`, `archive`), and `PROTECTED_PATHS` (the six
safety-critical files: the constitution, the approval gate, restart/off-switch, versioning/Archive,
gitops, and the coder). It is pure stdlib so the guardrails can never fail to load, and it is itself a
protected path — HELIX can't rewrite the laws by rewriting the law-keeper.

**The hard line — a pre-merge scan.** Every self-change, from every route (voice "ship it", the Work
panel, email approval), funnels through `engine.approve`. Before it smoke-checks or merges, it now runs
`constitution.check_change(diff_names(base, branch))`: if the change touches *any* protected path it is
**blocked** (`status = blocked_guardrail`), never merged. So even if Opus drafts a change that would
weaken the gate, the recovery paths, or the laws, it physically cannot ship (Commandments 7 & 8).

**Defense in depth.** The coder's prompt (`build_coder_prompt`) lists the protected files and forbids
weakening the approval gate, the Archive/root-reset, the off switch, the live-trading confirmation, or
adding any self-approval — wasted effort, since the gate rejects it anyway. `verify_integrity()` checks
a hardcoded fingerprint of the canonical commandment text (a tripwire against partial tampering) and, if
it fails, pauses autonomous self-writing (`_check_crashes`) and shows a persistent warning. The launcher
gives `PERMANENT_MENU_KEYS` **no ✕** and refuses to hide them, so **Settings and the Archive can never
be removed** (Commandments 8 & 12). A read-only **Guardrails** panel in Settings displays all twelve
with a "🛡 Protected — HELIX cannot change these" status.

**Amendment.** Reserved to the human owner, out-of-band: edit `constitution.py` directly (and regenerate
`FINGERPRINT` via `python -m helix.selfdev.constitution`). HELIX itself never gets the pen. This is what
lets the app grow freely through conversation — it can build, learn, and rewrite everything *else* —
while the cage around the human stays welded shut.

**The Twelve, in brief:** 1 protect the human · 2 serve, don't supplant · 3 keep the human in command ·
4 always tell the truth · 5 change only on a branch · 6 never self-approve · 7 never weaken these laws ·
8 always preserve a way back · 9 real money needs a human yes · 10 keep secrets/private media local ·
11 stay within granted access · 12 keep Settings permanent.

**Decisions locked (Brian, 2026-06-19):** immutable to HELIX and to conversation; amendable only by the
human, out-of-band · enforcement = protected-path scan at the approval gate (hard line) + coder-prompt
prohibition + integrity tripwire + Settings/Archive permanence · live-trading keeps today's spoken
confirmation (only auto-enable is locked off).

**Verified (2026-06-19):** clone test — a self-change editing a protected path is **blocked** at
`approve` (main untouched) while a harmless change **merges**; offscreen-Qt — Settings/Archive render
with **no ✕** and hiding is refused, the Guardrails panel shows all twelve; integrity True; compile +
import clean.

---

*Update this file whenever the architecture changes. It is the canonical description of HELIX.*
