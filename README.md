# Optibook FutureFocus 2026: Automated Trading Bot

## Introduction

What is Optibook?
Optibook is a simulated electronic exchange built by Optiver for educational trading competitions. Participants write automated trading algorithms in Python that connect to the exchange via an API and trade in real time against other teams.

The exchange hosts a set of financial instruments — in our case, 5 underlying equities (AMZN, JPM, NVDA, XOM, NVO) and 3 quarterly index futures (OB5X_202609_F, OB5X_202612_F, OB5X_202703_F). The futures track a weighted basket of these equities, and the challenge is to figure out how to price them correctly and profit from mispricings — all while managing risk, respecting position limits (±100 lots per instrument), and staying within an API rate limit of ~25 calls/second.

Teams are ranked by total Profit and Loss (PnL) at the end of each trading session. There's no manual trading — your code is your entire strategy.

---

## 🚀 Features

This repository contains `Auto_Trader_Optiver.py`, an autonomous trading client equipped with a robust rate limiter, automated risk management, and a comprehensive trade recording system. 

### Core Trading Strategies
The bot utilizes a multi-strategy approach, easily toggled via the `ENABLED` configuration dictionary:

* **Dual-Listing Arbitrage (`S_DUAL`):** Scans for price discrepancies between base stocks and their dual-listed counterparts (e.g., `NVO` vs `NVO_DUAL`). Executes Immediate-Or-Cancel (IOC) orders to lock in risk-free profit when the spread exceeds the defined edge threshold.
* **Futures Market Making (`S_FUT_MM`):** Calculates the theoretical fair value of futures based on the underlying index basket and time to expiry. Provides liquidity by placing limit orders around this fair value, adjusting quotes dynamically based on inventory skew to manage exposure.
* **ETF Market Making (`S_ETF_MM`):** Prices the ETF against its theoretical Net Asset Value (NAV) using the underlying index components. Maintains bid/ask quotes and adjusts for accumulated inventory risk.
* **Stock Market Making (`S_STOCK_MM`):** Captures the bid-ask spread on underlying index components. Updates quotes dynamically while avoiding adverse selection.
* **Delta Hedging (`S_DELTA`):** Monitors net directional exposure (delta) accumulated from the futures market making strategy. Neutralizes risk by executing offsetting IOC orders in the underlying equity markets when delta limits are breached.

### Engine Components
* **Trade Recorder:** Logs every executed fill to `exports/trades.csv`. Captures timestamp, instrument, side, price, volume, fair value at the time of trade, gap from fair value, running cash, and trade counts.
* **Adverse Selection Detection:** Automatically flags and logs warnings if the bot buys/sells at prices significantly worse than the true fair value or current mid-price.
* **Rate Limiter:** Ensures the bot strictly adheres to the exchange's API limits (20 calls per second) using a rolling deque to prevent disconnects or penalties.

---

## 🛠️ Configuration

Key parameters can be tuned directly at the top of `Auto_Trader_Optiver.py`:

```python
# Strategy Toggles
ENABLED = {
    'S_DUAL'     : True,
    'S_FUT_MM'   : True,
    'S_ETF_MM'   : True,
    'S_STOCK_MM' : True,
    'S_DELTA'    : False,
}

# General Settings
MAX_POS    = 100  # Exchange position limit
RATE_LIMIT = 20   # Maximum API calls per second
