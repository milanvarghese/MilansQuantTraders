# Bot Optimization Research Report
## Compiled: 2026-03-17
## Goal: Maximum ROI on minimum ($50) investment capital

---

## PART 1: GITHUB REPO COMPARISON (What competitors do better)

### Top Repos Analyzed

| Repo | Stars | Strategy | Key Advantage Over Our Bot |
|------|-------|----------|---------------------------|
| [Polymarket/agents](https://github.com/Polymarket/agents) | ~2,500 | LLM-powered framework (LangChain + RAG) | RAG for news context ingestion |
| [dylanpersonguy/Fully-Autonomous-Polymarket-AI-Trading-Bot](https://github.com/dylanpersonguy/Fully-Autonomous-Polymarket-AI-Trading-Bot) | 16 | 9-stage pipeline, 3-LLM ensemble | **6 exit strategies, graduated drawdown, Platt scaling** |
| [ent0n29/polybot](https://github.com/ent0n29/polybot) | 203 | Microservices + event streaming | Real-time price reactions (vs our 5-min polling) |
| [warproxxx/poly-maker](https://github.com/warproxxx/poly-maker) | 941 | Market making | **Confirms market-making is dead in 2026** |
| [OctoBot-Prediction-Market](https://github.com/Drakkar-Software/OctoBot-Prediction-Market) | 48 | Copy trading + arbitrage | YES+NO < $1.00 arbitrage detection |
| [suislanchez/polymarket-kalshi-weather-bot](https://github.com/suislanchez/polymarket-kalshi-weather-bot) | 45 | GFS ensemble + BTC microstructure | Dual-strategy diversification, Brier score tracking |
| [solship/Polymarket-Weather-Trading-Bot](https://github.com/solship/Polymarket-Weather-Trading-Bot) | 68 | NWS forecast comparison | Simple but functional exit threshold (45%) |
| [OctagonAI/kalshi-deep-trading-bot](https://github.com/OctagonAI/kalshi-deep-trading-bot) | 126 | Deep research -> probability -> trade | Anti-anchoring (research before seeing prices), hedging |
| [Metaculus/forecasting-tools](https://github.com/Metaculus/forecasting-tools) | 56 | Key Factor Analysis + Base Rates | Structured forecasting methodology |

### What We Do BETTER Than All Competitors
1. **KDE probability estimation** (vs raw counting 28/31 = 90.3%)
2. **Multi-model 120+ member ensemble** (ECMWF 51 + GFS 31 + ICON 40)
3. **12 risk checks** (most competitors have 2-3)
4. **Research-backed underdispersion correction** (1.2x spread inflation from Gneiting et al.)
5. **11 cities including international** (competitors do 5 US only)

### Critical Gaps We Must Fix (from competitor analysis)

| Gap | Best Reference Repo | Impact |
|-----|-------------------|--------|
| No exit logic (hold to resolution) | dylanpersonguy (6 strategies) | **HIGH** |
| No calibration tracking (Brier score) | suislanchez, dylanpersonguy | **HIGH** |
| Binary pause vs graduated drawdown | dylanpersonguy heat system | **MEDIUM** |
| No Platt scaling on KDE output | Navnoor Bawa quant system | **MEDIUM** |
| No intra-market arbitrage (YES+NO<$1) | OctoBot | **MEDIUM** |
| No analog day comparison | wethr.net (16+ models) | **MEDIUM** |

### Key Algorithms to Steal

**From dylanpersonguy bot - 6 Exit Strategies:**
1. Dynamic stop-loss (tightens as PnL improves)
2. Trailing stop
3. Hold-to-resolution (current default)
4. Time-based exit (48h before resolution)
5. Edge reversal detection (exit when current_price > estimated_prob)
6. Kill switch (emergency circuit breaker)

**From dylanpersonguy bot - Graduated Drawdown Heat System:**
- Normal: full position sizing
- Warning: reduce to 75% sizing
- Critical: reduce to 50% sizing
- Max: pause all trading

**From Navnoor Bawa - Platt Scaling:**
- Calibrate raw KDE probabilities using `CalibratedClassifierCV`
- Achieved Brier score 0.022
- Quarter-Kelly (25%) recommended: "Full Kelly: 33% chance of halving. Quarter Kelly: 4% chance."

**From suislanchez - Brier Score Tracking:**
- Track predicted probability vs actual outcome
- Brier = mean((predicted - actual)^2)
- Lower is better. Perfect = 0.0, random = 0.25

---

## PART 2: ACADEMIC PAPER SUMMARIES

### Paper 1: Le (2026) - "Decomposing Crowd Wisdom"
- **Source:** 292M trades across 327K contracts on Kalshi/Polymarket
- **Finding:** Structured calibration failures with domain-specific biases
- **Key insight:** Markets become better calibrated as resolution approaches (horizon effect)
- **Action:** Trade EARLY when markets are least efficient, right after forecast updates

### Paper 2: Whelan (UCD, 2025) - "Economics of the Kalshi Prediction Market"
- **Source:** 300K+ Kalshi contracts
- **Finding:** Average return is **-20% pre-fees, -22% post-fees**
- **Key insight:** Only selective traders with genuine informational edge profit
- **Action:** Don't spray trades. Only trade where we have REAL edge (weather forecasts)

### Paper 3: IMDEA Networks (2025) - "Unravelling the Probabilistic Forest"
- **Source:** 86M bets on Polymarket, April 2024 - April 2025
- **Finding:** $40M in arbitrage profits extracted. High APYs from fast capital turnover.
- **Key insight:** Near-expiry harvesting works because of fast turnover, not large mispricings
- **Action:** Optimize for capital velocity - fast resolution markets over locked-up capital

### Paper 4: Buterin (2021) - "Prediction Markets: Tales from the Election"
- **Finding:** Capital lockup is the #1 barrier to market efficiency
- **Key insight:** Even visible 15%+ mispricings go unexploited because capital is locked
- **Action:** Small, low-liquidity weather markets are likely LESS efficient (fewer sophisticated players)

### Paper 5: Tsang & Yang (2026) - "The Anatomy of Polymarket"
- **Source:** On-chain Polygon data analysis
- **Finding:** Newer markets are less efficient than mature ones (Kyle's lambda declines over time)
- **Action:** Target NEW weather markets, not established ones

### Paper 6: Hubacek & Sir (2020) - "Beating the Market with a Bad Predictive Model"
- **Finding:** You don't need superior accuracy to profit
- **Key insight:** Decorrelated models that capture info the market misses can profit
- **Action:** Our bot doesn't need to beat NOAA. It needs to find where Polymarket MISPRICES NOAA-level information

### Paper 7: Arcos, Renner & Oppenheim (2025) - "A Resource Theory of Gambling"
- **Finding:** Classic Kelly assumes infinite bets - doesn't apply to small bankrolls
- **Key insight:** With few bets, optimal strategy differs from Kelly
- **Action:** Start with flat $2 bets until 20+ resolved trades, then switch to adaptive Kelly

### Paper 8: Proskurnikov & Barmish (2023) - "Nonlinear Control for Robust Growth"
- **Finding:** When you don't know your true edge, nonlinear adaptive strategies beat fixed Kelly
- **Key insight:** Bet LESS than fractional Kelly when edge is uncertain
- **Action:** Use conservative end of edge estimate range for sizing

### Paper 9: Snowberg & Wolfers - "Favorite-Longshot Bias"
- **Finding:** Longshots (<15c) systematically overpriced. Favorites (>85c) underpriced.
- **Key insight:** Buy favorites, avoid longshots
- **Action:** Never buy YES < $0.15. Prefer YES > $0.85 where our model agrees

### Paper 10: Navajas et al. (2017) - "Aggregated Knowledge"
- **Finding:** 4 good sources combined > thousands of mediocre sources
- **Key insight:** Quality of component models > quantity
- **Action:** Our 3-model ensemble (ECMWF + GFS + ICON) is the right approach. Don't add noise.

### Paper 11: Management Science (2023) - "Longshot Bias Is Context-Dependent"
- **Finding:** Bias varies by market type and participant demographics
- **Key insight:** Weather markets may show LESS longshot bias than political markets
- **Action:** Don't blindly apply longshot filters to weather. Measure empirically.

---

## PART 3: SMALL-CAPITAL OPTIMIZATION

### Fee Structure (2026)
- **Most markets: ZERO fees** (no trading, deposit, or withdrawal fees)
- **Crypto markets (15-min):** Taker fees apply
- **Some sports markets:** Fees introduced Feb 2026
- **2% winner fee** on net winnings only (critical for near-expiry math)
- **Gas on Polygon:** <$0.01 per trade (negligible)

### Near-Expiry Harvesting: Fee-Adjusted Math
```
Buying at $0.95: gross 5.2%, net after 2% fee = 3.05%
Buying at $0.93: gross 7.5%, net after 2% fee = 5.35%  <-- SWEET SPOT
Buying at $0.90: gross 11.1%, net after 2% fee = 8.89%
Buying at $0.88: gross 13.6%, net after 2% fee = 11.35%
Buying at $0.97: gross 3.1%, net after 2% fee = 1.04%  <-- BARELY VIABLE
```
**Optimal entry range: $0.88-$0.93**

### Fee-Aware Kelly Formula
```python
def fee_adjusted_kelly(win_prob, market_price, fee_rate=0.02):
    net_win = (1 - market_price) * (1 - fee_rate)
    loss = market_price
    edge = win_prob * net_win - (1 - win_prob) * loss
    if edge <= 0:
        return 0
    return edge / net_win
```

### Position Sizing Rules for $50
- **Per trade:** $1-$2.50 (2-5% of bankroll)
- **Max single position:** $5 (10%)
- **Max total exposure:** $20 (40%)
- **Active positions:** 5-10 uncorrelated
- **Kelly fraction:** Start 0.10, increase to 0.25 only after 50+ trades with CLV > 5%

### Dynamic Kelly Based on Proven Edge
```python
if rolling_clv_50_trades > 0.10:   kelly_fraction = 0.25  # Strong edge proven
elif rolling_clv_50_trades > 0.05: kelly_fraction = 0.20  # Moderate edge
elif rolling_clv_50_trades > 0.02: kelly_fraction = 0.15  # Marginal edge
else:                               kelly_fraction = 0.10  # Gathering data
```

### Risk of Ruin Table
| Kelly Fraction | Chance Halving Before Doubling | Growth vs Full Kelly |
|---------------|-------------------------------|---------------------|
| Full (1.0x) | 33% | 100% |
| Half (0.5x) | 11% | ~75% |
| Quarter (0.25x) | ~3% | ~50% |
| Tenth (0.1x) | <1% | ~19% |

### Compounding Projections
| Timeframe | 3% Weekly | 5% Weekly |
|-----------|-----------|-----------|
| 1 month | $56 | $61 |
| 3 months | $71 | $90 |
| 6 months | $107 | $179 |
| 1 year | $230 | $643 |

### Spread Rules
- **Only trade markets with bid-ask spread <= 3 cents**
- Liquid markets (elections, major sports): 1-2c spread, manageable
- Illiquid markets: 10-34c+ spread, catastrophic for small trades
- **Always use limit orders** (save spread, act as maker)

### Market Selection for Small Capital
1. **Weather markets** - best edge/liquidity for bot, documented profits
2. **Sports markets** - clear outcomes, good liquidity on major events
3. **Political markets** - deepest liquidity but capital locked longer
4. **Niche/new markets** - less competition, small capital is advantage

---

## PART 4: DOCUMENTED PROFITABLE WEATHER BOTS

| Bot | Start | Profit | Markets | Strategy |
|-----|-------|--------|---------|----------|
| Anonymous | $1,000 | $24,000 | London weather | Ensemble forecasts since Apr 2025 |
| Anonymous | Unknown | $65,000 | NYC, London, Seoul | Multi-city |
| suislanchez | ~$1,000 | $1,800 | Kalshi + Polymarket | GFS 31-member ensemble |
| Top wallets | Various | Millions | Various cities | Advanced forecast models |

### Why Weather Markets Have Edge
1. GFS ensemble runs 4x/day; 2-6 hour info gap between runs
2. Humans check 1-2x/day; bots scan every 5 minutes
3. 2-degree-wide middle buckets create sharp probability cliffs
4. Fewer sophisticated participants than political markets
5. Daily resolution = rapid compounding

### Useful Tools
- [Degen Doppler](https://degendoppler.com/) - Polymarket weather edge finder
- [Polyforecast](https://polyforecast.io) - Free daily weather signals
- [PolyWhaler](https://www.polywhaler.com/) - Whale tracker
- [PolyTrack](https://www.polytrackhq.app) - Wallet tracker
- [EventArb](https://www.eventarb.com/) - Arbitrage calculator
- [Wethr.net](https://wethr.net) - 16+ weather model aggregator

---

## PART 5: PRIORITY OPTIMIZATION STACK

### Tier 1: Critical Fixes (Do First)
1. **Fix Gamma API price update bug** - prices never update, exits never trigger
2. **Fix outcome ordering assumption** - could trade wrong direction
3. **Fix naive datetime** - UTC everywhere
4. **Implement fee-aware Kelly** - current Kelly ignores 2% winner fee
5. **Add edge reversal exit** - exit when current_price > estimated_prob

### Tier 2: High-Impact Improvements
6. **Add CLV tracking** - zero cost, essential edge diagnostic
7. **Implement graduated drawdown** (Normal/Warning/Critical/Max) vs binary pause
8. **Consolidate weather_scanner + paper_trader state** into single JSON
9. **Add Brier score tracking** for model calibration
10. **Paginate market fetching** - scan ALL markets, not just first 1000

### Tier 3: Edge Enhancement
11. **Add near-expiry harvesting** at $0.88-$0.93 entry range
12. **Implement dynamic Kelly** based on rolling CLV
13. **Add spread check** - only trade when spread <= 3c
14. **Add intra-market arbitrage scanner** (YES+NO < $0.97)
15. **Add sports market analysis** using The Odds API

### Tier 4: Advanced
16. **Platt scaling** on KDE output for calibration
17. **Analog day comparison** for unusual weather patterns
18. **Parallel ensemble fetching** (currently sequential, 10s total)
19. **Adaptive Kelly fraction** based on recent performance
20. **Volatility term structure** (time, season -> sigma)

---

## FULL SOURCE LIST

### Academic Papers
- Le (2026) - Crowd Wisdom Decomposition, 292M trades
- Whelan (2025) - Kalshi Economics, -20% avg return
- IMDEA (2025) - $40M arbitrage profits, 86M bets
- Tsang & Yang (2026) - Polymarket Anatomy, on-chain
- Buterin (2021) - Prediction Market Trading, efficiency barriers
- Hubacek & Sir (2020) - Beating Markets with Bad Models
- Snowberg & Wolfers - Favorite-Longshot Bias (NBER)
- Arcos et al. (2025) - Kelly in Finite Regimes
- Proskurnikov & Barmish (2023) - Kelly Under Uncertainty
- Uhrin et al. (2021) - Adaptive Fractional Kelly
- Navajas et al. (2017) - Small Expert Groups > Large Crowds
- Management Science (2023) - Context-Dependent Longshot Bias
- Tallarico & Olivares (2024) - Neural Weather Derivatives
- Cont & Kukanov (2012) - Optimal Order Placement

### GitHub Repos
- https://github.com/Polymarket/agents
- https://github.com/dylanpersonguy/Fully-Autonomous-Polymarket-AI-Trading-Bot
- https://github.com/ent0n29/polybot
- https://github.com/warproxxx/poly-maker
- https://github.com/Drakkar-Software/OctoBot-Prediction-Market
- https://github.com/suislanchez/polymarket-kalshi-weather-bot
- https://github.com/solship/Polymarket-Weather-Trading-Bot
- https://github.com/OctagonAI/kalshi-deep-trading-bot
- https://github.com/Metaculus/forecasting-tools
- https://github.com/speedyhughes/kalshi-poly-arb

### Practical Guides
- https://cryptonews.com/cryptocurrency/polymarket-strategies/
- https://navnoorbawa.substack.com/p/building-a-quantitative-prediction
- https://blog.devgenius.io/found-the-weather-trading-bots-quietly-making-24-000-on-polymarket-and-built-one-myself-for-free-120bd34d6f09
- https://docs.polymarket.com/polymarket-learn/trading/fees
