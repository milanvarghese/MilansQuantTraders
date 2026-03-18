# AI Trading on Prediction Markets & Crypto — Research Report
**Date:** 2026-03-18

## Executive Summary

92.4% of Polymarket traders lose money. Only 0.51% have profits exceeding $1,000. The viral "$14K in 48 hours" Claude bot claim is unverified. But real edges exist — they're just smaller, harder, and more technical than the hype suggests.

---

## 1. Verified Profitable Strategies

### Strategy A: Latency Arbitrage on Crypto Contracts (DEAD as of 2026)
- Exploit delay between Binance/Coinbase confirmed prices and Polymarket's 15-min crypto markets
- One wallet (0x8dxd) turned $313 into $414K in 1 month with 98% WR
- **Status: DEAD** — Polymarket introduced dynamic taker fees (Jan 2026, ~3.15% at 50% prob) and removed 500ms taker delay (Feb 2026)
- Arbitrage window collapsed from 12.3 seconds (2024) to 2.7 seconds (late 2025)
- 73% of remaining profits captured by sub-100ms colocated bots

### Strategy B: Dutch-Book / Combinatorial Arbitrage
- Buy complete set of mutually exclusive outcomes when total price < $1.00
- Guaranteed profit regardless of outcome
- Academic study (arXiv:2508.03474): ~$40M extracted Apr 2024 - Apr 2025 (86M trades analyzed)
- Needs spread > 2.5-3% to overcome Polymarket's 2% winner fee
- Average opportunity window: 2.7 seconds, requires sub-100ms execution

### Strategy C: LLM Ensemble Probability Estimation
- Use multiple AI models to estimate true probability, bet when edge > 4-5%
- Best documented: Foresight-32B (fine-tuned Qwen3-32B) beats all frontier LLMs on Brier score
- Realistic returns: ~$52 per 251 questions (academic, reproducible)
- The dylanpersonguy bot uses 3-model ensemble: GPT-4o (40%) + Claude 3.5 (35%) + Gemini (25%)

### Strategy D: Near-Resolution Directional Betting
- Focus on markets resolving within 48 hours where info asymmetry is highest
- Post maker orders at 90-95 cents on likely winner at T-10 seconds before resolution
- Pocket $0.05-0.10/share plus maker rebates
- This is where the edge shifted after the fee changes

### Strategy E: Cross-Platform Arbitrage (Polymarket vs Kalshi)
- Academic research shows significant price disparities
- Polymarket leads price discovery, Kalshi lags by minutes
- Different resolution semantics create persistent windows
- Repo: github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot

### Strategy F: Market Making
- One documented case: $700-800/day with $10K capital
- Place orders on both sides of low-volatility markets
- Maker fees are zero + USDC rebates
- Win rate 78-85%, returns 1-3% monthly
- Requires <100ms cancel/replace or adverse selection kills you
- Book depth: $5K-15K per side on 5-min BTC contracts

---

## 2. Open-Source Repos

### Tier 1: Most Complete

| Repo | Stack | Features |
|------|-------|----------|
| [Polymarket/agents](https://github.com/Polymarket/agents) | Python, OpenAI, Langchain, ChromaDB | Official framework, RAG for news |
| [dylanpersonguy/Fully-Autonomous-Polymarket-AI-Trading-Bot](https://github.com/dylanpersonguy/Fully-Autonomous-Polymarket-AI-Trading-Bot) | GPT-4o + Claude + Gemini | 3-model ensemble, 15 risk checks, whale tracking, 4 execution strategies, Flask dashboard |
| [alsk1992/CloddsBot](https://github.com/alsk1992/CloddsBot) | TypeScript, Claude primary | 118+ strategies, multi-platform (Polymarket+Kalshi+Binance+Hyperliquid), VaR/CVaR risk |
| [artvandelay/polymarket-agents](https://github.com/artvandelay/polymarket-agents) | Claude 3.5 Sonnet via OpenRouter | 10 MCP tools, ~$0.002 per analysis, pluggable strategies |

### Tier 2: MCP Servers (Claude-native)

| Repo | Tools |
|------|-------|
| [caiovicentino/polymarket-mcp-server](https://github.com/caiovicentino/polymarket-mcp-server) | 45 tools across 4 categories |
| [berlinbra/polymarket-mcp](https://github.com/berlinbra/polymarket-mcp) | Market data + trading |
| [JamesANZ/prediction-market-mcp](https://github.com/JamesANZ/prediction-market-mcp) | Polymarket + PredictIt + Kalshi |

### Tier 3: Specialized

| Repo | Purpose |
|------|---------|
| [CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot](https://github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot) | Cross-platform BTC arbitrage |
| [warproxxx/poly-maker](https://github.com/warproxxx/poly-maker) | Market making (author says "not profitable in today's market") |
| [elizaos-plugins/plugin-polymarket](https://github.com/elizaos-plugins/plugin-polymarket) | ElizaOS framework integration |

---

## 3. Polymarket CLOB API Technical Details

### Authentication
- L1: EIP-712 private key signature creates L2 credentials (one-time)
- L2: HMAC-SHA256 using derived API key, secret, passphrase (ongoing)

### Rate Limits (per 10-second window)
| Endpoint | Limit |
|----------|-------|
| POST /order | 3,500 burst / 36,000 per 10 min |
| DELETE /order | 3,000 burst / 30,000 per 10 min |
| POST /orders (batch) | 1,000 burst / 15,000 per 10 min |
| /book | 1,500 |
| Gamma /markets | 300 |

### Order Types
- GTC (Good Till Cancelled), GTD (Good Till Date), FOK (Fill or Kill), FAK (Fill and Kill)
- Batch: up to 15 orders per request
- `feeRateBps` field **required** on fee-enabled markets (must query dynamically)

### SDKs
- Python: `py-clob-client` (pip install py-clob-client)
- TypeScript: `@polymarket/clob-client`
- Rust: `polymarket-client-sdk`

### Critical 2026 Changes
- 500ms taker delay removed (Feb 2026) — orders execute instantly
- Dynamic taker fees on 15-min crypto markets: fee = C * 0.25 * (p * (1-p))^2
- Max ~3.15% at 50% probability

---

## 4. Verified Returns Data

| Source | Claim | Verification |
|--------|-------|----|
| Academic (arXiv:2508.03474) | ~$40M total arb profits, Apr 2024 - Apr 2025 | HIGH (86M trades) |
| Wallet 0x8dxd | $313 -> $414K, 1 month, 98% WR (latency arb) | MEDIUM (on-chain) |
| Market maker case study | $700-800/day with $10K capital | MEDIUM (article) |
| Foresight-32B simulated | $52 profit per 251 questions | HIGH (academic) |
| Open-source bot real test | $23 day 1, $9 day 2, $3 day 3, then zero | HIGH (first-person) |
| 340-star GitHub bot | $31.20 over 10 days, then flatlined | MEDIUM (article) |
| Polystrat agents (Olas) | 37% show positive P&L | MEDIUM (marketing) |
| Claude $14K viral claim | $1K -> $14.2K in 48 hours | VERY LOW (unverified X post) |
| **Platform-wide stats** | **92% lose money; 0.51% earned >$1K** | **HIGH (on-chain)** |

---

## 5. Risk and Failure Cases

### Platform Risk
- Polymarket changes rules without notice (dynamic fees, delay removal)
- Changes can render entire bot classes obsolete overnight
- Not regulated — no recourse if funds are lost

### Technical Risk
- Residential internet (150ms+) is non-competitive vs colocated (<5ms)
- LLM hallucination risk — confident but wrong forecasts
- Thin liquidity ($5K-15K per side) means slippage eats edge
- Copy-trading bots show net losses live due to slippage

### Strategy Decay
- What worked in February 2026 fails by March as others replicate
- Public bot repos are explicitly flagged as loss-making reference implementations
- Top traders run ghost accounts because mains get front-run

### Documented Failures
- AI bot: $23 -> $1.50 (unsellable tokens on certain market types)
- Claude Code-built Bybit bot: 60% WR but -$39.20 net (bad risk/reward)
- Open-source bots on home internet: by 2026, sub-100ms bots capture everything first

---

## 6. Best Architecture for an AI Trading Bot (2026)

```
Market Discovery (Gamma API, filter by volume/liquidity/time-to-resolution)
  -> Data Collection (CLOB orderbook, price history, news, external data)
  -> Probability Estimation (LLM ensemble: 3+ models, independent forecasts)
  -> Calibration (Platt scaling, historical regression, evidence penalties)
  -> Edge Calculation (model_prob - market_prob - fees > threshold)
  -> Risk Check (15+ independent checks, any failure blocks trade)
  -> Position Sizing (fractional Kelly with quality multipliers)
  -> Execution (py-clob-client, limit orders, TWAP/Iceberg for size)
  -> Monitoring (WebSocket feeds, settlement tracking, drawdown heat)
```

### Key Design Principles
1. **Multi-model ensemble** — no single LLM, use 3+ with weighted averaging
2. **Independent forecasting** — models must NOT see market price (prevents anchoring)
3. **Calibration is everything** — raw LLM probabilities are poorly calibrated
4. **Triple safety gates** — per-order, config-level, and environment-level kill switches
5. **Maker orders preferred** — zero fees + rebates vs taker fees
6. **Near-resolution focus** — highest info asymmetry in last 48 hours

---

## 7. Implications for Our Bot

### What we should build next (priority order):
1. **Dutch-book scanner** — detect mutually exclusive outcome sets summing to < $0.97
2. **Multi-model probability estimation** — add Claude + Gemini to our probability engine
3. **Near-resolution focus** — prioritize markets closing within 48 hours
4. **Maker order execution** — switch from taker to maker for zero fees + rebates
5. **Cross-platform arbitrage** — Polymarket vs Kalshi price differences

### What we should NOT pursue:
- Latency arbitrage (dead, needs sub-100ms infra we don't have)
- Copy-trading (documented net losses due to slippage)
- High-frequency market making (needs colocated servers)

### Realistic expectations:
- $50-200/month at our scale ($50 bankroll) with disciplined execution
- Edge is small (2-5%) and fleeting — speed and volume matter
- 37% of well-built bots are profitable vs 7.6% of all wallets — good bot = 5x better odds

---

## Sources

### Academic / High-Verification
- [arXiv:2508.03474 - Arbitrage in Prediction Markets ($40M study)](https://arxiv.org/abs/2508.03474)
- [Foresight-32B - Lightning Rod Labs](https://blog.lightningrod.ai/p/foresight-32b-beats-frontier-llms-on-live-polymarket-predictions)
- [QuantPedia: Systematic Edges in Prediction Markets](https://quantpedia.com/systematic-edges-in-prediction-markets/)

### Official Documentation
- [Polymarket CLOB API Docs](https://docs.polymarket.com/developers/CLOB/introduction)
- [Polymarket Rate Limits](https://docs.polymarket.com/quickstart/introduction/rate-limits)
- [py-clob-client](https://github.com/Polymarket/py-clob-client)

### News / Analysis
- [CoinDesk: AI agents rewriting prediction market trading](https://www.coindesk.com/tech/2026/03/15/ai-agents-are-quietly-rewriting-prediction-market-trading/)
- [Finance Magnates: Polymarket dynamic fees](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)
- [Yahoo Finance: Arbitrage bots dominate Polymarket](https://finance.yahoo.com/news/arbitrage-bots-dominate-polymarket-millions-100000888.html)
- [Cryptopolitan: Claude bots on Polymarket](https://www.cryptopolitan.com/polymarket-bots-claude-democratizing-betting/)

### Open-Source Repos
- [Polymarket/agents (Official)](https://github.com/Polymarket/agents)
- [Fully-Autonomous-Polymarket-AI-Trading-Bot](https://github.com/dylanpersonguy/Fully-Autonomous-Polymarket-AI-Trading-Bot)
- [CloddsBot](https://github.com/alsk1992/CloddsBot)
- [polymarket-mcp-server](https://github.com/caiovicentino/polymarket-mcp-server)
- [polymarket-kalshi-btc-arbitrage-bot](https://github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot)
- [poly-maker](https://github.com/warproxxx/poly-maker)
