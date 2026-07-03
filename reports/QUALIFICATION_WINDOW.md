# Qualification Window — Pre-Registration

**Window start:** 2026-07-03 ~23:45 UTC (all three bots archive-reset and relaunched fresh)
**Frozen at commit:** `9bda6c1` (engine v2 + Phase A fixes)
**Prior history:** archived at `archive_prefreeze_2026-07-03/` (server + local copy). Nothing before this point counts toward qualification.

## Why this exists

The 2026-07-03 audit found every prior config decision was validated by a backtester with lookahead bias (v1 said PF 1.77; the honest v2 engine says PF 0.98 on identical config+data — matching live). This window is the first clean, pre-registered forward test. **Post-hoc subset selection is not allowed** — the hypotheses below were declared before the window started, not discovered afterward.

## Frozen configurations (pre-registered hypotheses)

### Crypto Scalper — $50 fresh
- Pairs: **AVAX, ADA** only (whitelist)
- **Shorts only** (`allow_buy_side: false`) — hypothesis from two independent windows: live sell PF 1.19 (Mar–Jun) + honest backtest PF 1.24 (Dec–Mar)
- Confluence gating via regime profiles (recorded per-trade as `effective_min_confluence`); tuned base 7
- Exits: TP 4.0x / SL 3.5x 1H ATR, progressive stop, 36h max hold — all snapshotted at entry
- Auto-tuner: **log-only** (`auto_tune_apply: false`)
- Fees modeled: 0.1%/side taker (Binance tier)

### Stock Trader — $1,000 fresh
- Universe: **growth_tech + semiconductors** (23 symbols) — hypothesis: growth_tech was the only sector clearing significance live (PF 2.66, t=2.99, n=58)
- Exits: TP 1.5x / SL 2.0x daily ATR, intrabar via 5-min bar extremes (SL-first), trailing after 1.0x ATR, **max hold 66 wall-clock hours**
- Per-symbol: 7-day cooldown on losing stop_loss/time_exit/drawdown_kill; removal after 2 losses in 30d
- Breakers: equity-based -15% pause / -25% kill; dynamic screener OFF; bandit log-only

### Polymarket Paper — $50 fresh
- Categories as configured; 30-day max time-to-resolution for crypto/event; dutch_book excluded from exposure count
- Kelly calibration now starts from clean data (prior history was corrupted by the pnl=$0 close bug)

## Pass/fail gates (from the audit; measured at window end)

| Gate | Crypto | Stock |
|---|---|---|
| Minimum sample | ≥200 closed trades over ≥90 days | ≥100 closed round trips |
| Profit factor | ≥ 1.3 net of 0.1%/side fees | ≥ 1.3 at realistic costs |
| Expectancy t-stat | ≥ 2 | ≥ 2 |
| Extra | Gross (0-fee) PF ≥ 1.4 | No symbol >25% of gross profit; ≥8 distinct winning symbols |

Cross-cutting: equity max drawdown ≤ 15% during the window; forward PF must be ≥ 70% of the honest-backtest OOS PF for the same config; 100% of trades must carry side/regime/raw-score records.

## Rules

1. **Any config change restarts the clock.** If a change ships mid-window, the window is void and restarts at the new freeze.
2. The auto-tuners stay log-only for the entire window.
3. Universes/sides may not be re-widened by hand — only via v2-engine backtest evidence, and that still restarts the clock.
4. If the gates fail at window end, the verdict is accepted: that strategy does not go live; next step is redesign (stock backtester → score redesign), not another parameter tweak.
5. First real capital (only if gates pass): ≤ $200, hard kill at -20% equity, halt if realized costs exceed modeled by >0.1%/side round trip.
