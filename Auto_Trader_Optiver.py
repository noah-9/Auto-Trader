"""
autotrader_final.py - Optibook FutureFocus 2026 (c10 + trade recorder)
Records every fill to exports/trades.csv with fair-value context.
"""
import time
import math
import logging
import collections
import datetime as dt
from typing import Dict, Optional, Tuple

from optibook.synchronous_client import Exchange
from optibook.exporter import Exporter

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("autotrader")

# ==============================================================================
# PARAMETERS
# ==============================================================================
ENABLED = {
    'S_DUAL'     : True,
    'S_FUT_MM'   : True,
    'S_ETF_MM'   : True,
    'S_STOCK_MM' : True,
    'S_DELTA'    : False,
}

MAX_POS    = 100
RATE_LIMIT = 20
TICK       = 0.1
LOG_EVERY  = 20

DUAL_EDGE = 0.05
DUAL_VOL  = 100

FUT_MM_SPREAD   = 0.15
FUT_MM_VOL      = 50
FUT_MM_MAX_POS  = 90
FUT_MM_INV_SKEW = 0.001
FUT_REPRICE_THR = 0.08

ETF_MM_SPREAD   = 0.12
ETF_MM_VOL      = 100
ETF_MM_MAX_POS  = 100
ETF_MM_INV_SKEW = 0.002
ETF_REPRICE_THR = 0.05

STOCK_MM_VOL      = 100
STOCK_MM_MAX_POS  = 100
STOCK_REPRICE_THR = 0.10

DELTA_MIN_TRIGGER = 5
DELTA_MIN_TRADE   = 3
DELTA_INTERVAL    = 10

WEIGHTS = {
    'AMZN': 953.21, 
    'JPM': 129.25, 
    'NVDA': 908.06, 
    'XOM': 2245.39, 
    'NVO': 124.78
}

ETF_M, ETF_C, R = 0.25, 2.50, 0.03
DUAL_PAIRS  = [('NVO', 'NVO_DUAL'), ('NVDA', 'NVDA_DUAL')]
ETF_ID      = 'OB5X_ETF'
FUTURE_IDS  = ['OB5X_202609_F', 'OB5X_202612_F', 'OB5X_202703_F']

# ==============================================================================
# TRADE RECORDER
# ==============================================================================
exporter = Exporter(debugging=False)

# Per-instrument running cash (to compute realized PnL)
_realized_cash: Dict[str, float] = collections.defaultdict(float)
_trade_count:   Dict[str, int]   = collections.defaultdict(int)

# Stock mid cache (updated each sweep, used for adverse-selection detection)
_stock_mid_cache: Dict[str, float] = {}

def record_trades(ex, iids, books, instruments):
    """
    Poll new private trades, log to CSV, track realized PnL per instrument.
    Columns: timestamp, iid, side, price, volume, fair_value, gap, running_cash
    """
    rows = []
    for iid in iids:
        for t in ex.poll_new_trades(iid):
            # Compute fair value at time of trade
            fair = _fair_value(iid, books, instruments)
            gap  = round(t.price - fair, 4) if fair else None
            
            # side='bid' means we BOUGHT; 'ask' means we SOLD
            sign = +1 if t.side == 'bid' else -1
            cash_delta = -sign * t.price * t.volume   # buying costs cash
            
            _realized_cash[iid] += cash_delta
            _trade_count[iid]   += 1
            
            rows.append([
                t.timestamp.strftime('%H:%M:%S.%f'),
                iid,
                'BUY' if t.side == 'bid' else 'SELL',
                f"{t.price:.2f}",
                str(t.volume),
                f"{fair:.3f}" if fair else "N/A",
                f"{gap:+.3f}" if gap is not None else "N/A",
                f"{_realized_cash[iid]:.2f}",
                str(_trade_count[iid]),
            ])
            
            # Log any trade where gap shows we're on the WRONG side
            # (buying above fair or selling below fair by > 0.1)
            if gap is not None:
                if t.side == 'bid' and gap > 0.15:   # buying too high
                    logger.warning(f"[TRADE] BAD BUY  {iid} {t.volume}@{t.price:.2f}  "
                                   f"fair={fair:.3f}  gap={gap:+.3f}")
                elif t.side == 'ask' and gap < -0.15: # selling too low
                    logger.warning(f"[TRADE] BAD SELL {iid} {t.volume}@{t.price:.2f}  "
                                   f"fair={fair:.3f}  gap={gap:+.3f}")
            else:
                # For stocks (no fair value): flag if we buy and then the mid
                # is already below our buy price (can detect adverse selection)
                m = _stock_mid_cache.get(iid)
                if m and t.side == 'bid' and t.price > m + 0.05:
                    logger.warning(f"[TRADE] PRICEY BUY  {iid} {t.volume}@{t.price:.2f}  "
                                   f"mid_now={m:.2f}  overpaid={t.price-m:+.3f}")
    if rows:
        exporter.export({'trades.csv': [
            ['time','instrument','side','price','volume',
             'fair_value','gap_from_fair','running_cash','trade_n']
        ] + rows})

def _fair_value(iid, books, instruments):
    X = index_val(books)
    if iid == ETF_ID and X:
        return ETF_C + ETF_M * X
    if iid in FUTURE_IDS and X:
        instr = instruments.get(iid)
        if instr and instr.expiry:
            return X * math.exp(R * tau_years(instr.expiry))
    if iid.endswith('_DUAL'):
        return mid(books.get(iid.replace('_DUAL', '')))
    return None   # stocks: no absolute fair value

def print_trade_summary(positions):
    """Print realized cash per instrument + current position."""
    lines = []
    for iid in sorted(_realized_cash.keys()):
        pos  = positions.get(iid, 0)
        cash = _realized_cash[iid]
        n    = _trade_count[iid]
        lines.append(f"  {iid:<22} pos={pos:+4d}  realized_cash={cash:>10.2f}  trades={n}")
    if lines:
        logger.info("[TRADE SUMMARY]")
        for l in lines: 
            logger.info(l)

# ==============================================================================
# RATE LIMITER
# ==============================================================================
_upd: collections.deque = collections.deque()

def _send(fn, *a, **kw):
    now = time.time()
    while _upd and now - _upd[0] > 1.0: 
        _upd.popleft()
    if len(_upd) >= RATE_LIMIT:
        wait = 1.0 - (now - _upd[0]) + 0.01
        if wait > 0: 
            time.sleep(wait)
        now = time.time()
        while _upd and now - _upd[0] > 1.0: 
            _upd.popleft()
    r = fn(*a, **kw)
    _upd.append(time.time())
    return r

def ioc_buy(ex, i, p, v):  return _send(ex.insert_order, i, price=p, volume=v, side="bid", order_type="ioc")
def ioc_sell(ex, i, p, v): return _send(ex.insert_order, i, price=p, volume=v, side="ask", order_type="ioc")
def lim_buy(ex, i, p, v):  return _send(ex.insert_order, i, price=p, volume=v, side="bid", order_type="limit")
def lim_sell(ex, i, p, v): return _send(ex.insert_order, i, price=p, volume=v, side="ask", order_type="limit")
def del_orders(ex, i):     return _send(ex.delete_orders, i)

def best_bid(bk): return bk.bids[0].price if (bk and bk.bids) else None
def best_ask(bk): return bk.asks[0].price if (bk and bk.asks) else None
def bid_vol(bk):  return bk.bids[0].volume if (bk and bk.bids) else 0
def ask_vol(bk):  return bk.asks[0].volume if (bk and bk.asks) else 0

def mid(bk):
    b, a = best_bid(bk), best_ask(bk)
    return (b + a) / 2 if b and a else None

def spread_ticks(bk):
    b, a = best_bid(bk), best_ask(bk)
    return round((a - b) / TICK) if b and a else None

def round_tick(p): 
    return round(round(p / TICK) * TICK, 10)

def tau_years(e):  
    return max(0.0, (e - dt.datetime.now()).total_seconds() / (365.25*86400))

def index_val(books):
    t = 0.0
    for s, w in WEIGHTS.items():
        m = mid(books.get(s))
        if m is None: 
            return None
        t += w * m
    return t / 1000.0

def etf_nav(books):
    X = index_val(books)
    return (ETF_C + ETF_M * X) if X else None

def fut_fair(books, expiry):
    X = index_val(books)
    return X * math.exp(R * tau_years(expiry)) if X else None

# ==============================================================================
# STRATEGIES 
# ==============================================================================
def s_dual(ex, books, positions):
    """
    Execute dual-listing arbitrage when price discrepancies
    exceed the configured edge threshold.
    """
    if not ENABLED['S_DUAL']: 
        return
    for base_id, dual_id in DUAL_PAIRS:
        bkb = books.get(base_id)
        bkd = books.get(dual_id)
        
        bb = best_bid(bkb)
        ba = best_ask(bkb)
        db = best_bid(bkd)
        da = best_ask(bkd)
        
        if None in (bb, ba, db, da): 
            continue
            
        bp = positions.get(base_id, 0)
        dp = positions.get(dual_id, 0)
        
        edge = bb - da
        if edge >= DUAL_EDGE:
            vol = min(DUAL_VOL, MAX_POS - dp, MAX_POS + bp, ask_vol(bkd), bid_vol(bkb))
            if vol > 0:
                ioc_buy(ex, dual_id, da, vol)
                ioc_sell(ex, base_id, bb, vol)
                
        edge = db - ba
        if edge >= DUAL_EDGE:
            vol = min(DUAL_VOL, MAX_POS + dp, MAX_POS - bp, bid_vol(bkd), ask_vol(bkb))
            if vol > 0:
                ioc_sell(ex, dual_id, db, vol)
                ioc_buy(ex, base_id, ba, vol)

_fut_last_fair = {}
_fut_last_quote = {}

def s_fut_mm(ex, books, positions, instruments):
    """
    Market making strategy for futures contracts.
    Calculates fair value based on the underlying index and time to expiry,
    applies inventory skewing to manage risk, and provides liquidity via limit orders.
    """
    if not ENABLED['S_FUT_MM']: 
        return
    for fid in FUTURE_IDS:
        instr = instruments.get(fid)
        if not instr or not instr.expiry: 
            continue
            
        fair = fut_fair(books, instr.expiry)
        if fair is None: 
            continue
            
        pos = positions.get(fid, 0)
        last = _fut_last_fair.get(fid)
        if last and abs(fair - last) < FUT_REPRICE_THR: 
            continue
            
        inv_shift = round_tick(-FUT_MM_INV_SKEW * pos * TICK)
        bid_price = round_tick(fair - FUT_MM_SPREAD + inv_shift)
        ask_price = round_tick(fair + FUT_MM_SPREAD + inv_shift)
        
        if bid_price >= ask_price:
            bid_price = round_tick(fair - TICK)
            ask_price = round_tick(fair + TICK)
            
        bid_v = min(FUT_MM_VOL, max(0, FUT_MM_MAX_POS - pos))
        ask_v = min(FUT_MM_VOL, max(0, FUT_MM_MAX_POS + pos))
        desired = (bid_price, ask_price, bid_v, ask_v)
        
        if _fut_last_quote.get(fid) == desired: 
            continue
            
        del_orders(ex, fid)
        if bid_v > 0: 
            lim_buy(ex, fid, bid_price, bid_v)
        if ask_v > 0: 
            lim_sell(ex, fid, ask_price, ask_v)
            
        _fut_last_fair[fid] = fair
        _fut_last_quote[fid] = desired

_etf_last_nav = None
_etf_last_quote = None

def s_etf_mm(ex, books, positions):
    """
    Market making strategy for the ETF.
    Prices the ETF around its theoretical Net Asset Value (NAV) derived from 
    index constituents, adjusts for current inventory, and maintains bid/ask quotes.
    """
    if not ENABLED['S_ETF_MM']: 
        return
    global _etf_last_nav, _etf_last_quote
    
    nav = etf_nav(books)
    pos = positions.get(ETF_ID, 0)
    
    if nav is None: 
        return
    if _etf_last_nav and abs(nav - _etf_last_nav) < ETF_REPRICE_THR: 
        return
        
    inv_shift = round_tick(-ETF_MM_INV_SKEW * pos * TICK)
    bid_price = round_tick(nav - ETF_MM_SPREAD + inv_shift)
    ask_price = round_tick(nav + ETF_MM_SPREAD + inv_shift)
    
    if bid_price >= ask_price:
        bid_price = round_tick(nav - TICK)
        ask_price = round_tick(nav + TICK)
        
    bid_v = min(ETF_MM_VOL, max(0, ETF_MM_MAX_POS - pos))
    ask_v = min(ETF_MM_VOL, max(0, ETF_MM_MAX_POS + pos))
    desired = (bid_price, ask_price, bid_v, ask_v)
    
    if _etf_last_quote == desired: 
        return
        
    del_orders(ex, ETF_ID)
    if bid_v > 0: 
        lim_buy(ex, ETF_ID, bid_price, bid_v)
    if ask_v > 0: 
        lim_sell(ex, ETF_ID, ask_price, ask_v)
        
    _etf_last_nav = nav
    _etf_last_quote = desired

_stock_last_mid = {}
_stock_last_quote = {}

def s_stock_mm(ex, books, positions):
    """
    Market making strategy for underlying index stocks.
    Captures the bid-ask spread by placing limit orders around the mid-price,
    re-pricing dynamically while respecting maximum position limits.
    """
    if not ENABLED['S_STOCK_MM']: 
        return
    for stk in WEIGHTS.keys():
        bk = books.get(stk)
        bb = best_bid(bk)
        ba = best_ask(bk)
        
        if bb is None or ba is None: 
            continue
            
        sp = spread_ticks(bk)
        pos = positions.get(stk, 0)
        m  = (bb + ba) / 2
        
        _stock_mid_cache[stk] = m   # update cache for adverse-selection detection
        
        last = _stock_last_mid.get(stk)
        if last and abs(m - last) < STOCK_REPRICE_THR: 
            continue
            
        if sp and sp >= 2:
            bid_price = round_tick(bb + TICK)
            ask_price = round_tick(ba - TICK)
        else:
            bid_price = bb
            ask_price = ba
            
        if bid_price >= ask_price: 
            continue
            
        bid_v = min(STOCK_MM_VOL, max(0, STOCK_MM_MAX_POS - pos))
        ask_v = min(STOCK_MM_VOL, max(0, STOCK_MM_MAX_POS + pos))
        desired = (bid_price, ask_price, bid_v, ask_v)
        
        if _stock_last_quote.get(stk) == desired: 
            continue
            
        del_orders(ex, stk)
        if bid_v > 0: 
            lim_buy(ex, stk, bid_price, bid_v)
        if ask_v > 0: 
            lim_sell(ex, stk, ask_price, ask_v)
            
        _stock_last_mid[stk] = m
        _stock_last_quote[stk] = desired

def s_delta(ex, books, positions):
    """
    Delta hedging strategy.
    Monitors net directional exposure (delta) accumulated from futures positions 
    and executes IOC orders on underlying stocks to neutralize the risk.
    """
    if not ENABLED['S_DELTA']: 
        return
        
    net_delta = sum(positions.get(fid, 0) for fid in FUTURE_IDS)
    if abs(net_delta) < DELTA_MIN_TRIGGER: 
        return
        
    for stk, w in WEIGHTS.items():
        target  = max(-MAX_POS, min(MAX_POS, int(round(-net_delta * w / 1000))))
        current = positions.get(stk, 0)
        diff = target - current
        
        if abs(diff) < DELTA_MIN_TRADE: 
            continue
            
        bk = books.get(stk)
        if diff > 0:
            vol = min(diff, MAX_POS - current, ask_vol(bk))
            pa = best_ask(bk)
            if vol > 0 and pa: 
                ioc_buy(ex, stk, pa, vol)
        else:
            vol = min(-diff, MAX_POS + current, bid_vol(bk))
            pb = best_bid(bk)
            if vol > 0 and pb: 
                ioc_sell(ex, stk, pb, vol)

def log_snapshot(exchange, books, positions, sweep):
    try: 
        pnl = exchange.get_pnl()
    except: 
        pnl = float('nan')
        
    net_delta = sum(positions.get(fid, 0) for fid in FUTURE_IDS)
    pos_str   = "  ".join(f"{k}:{v:+d}" for k, v in positions.items() if v != 0) or "FLAT"
    X = index_val(books)
    
    logger.info(f"SWEEP {sweep:>4}  PnL={pnl:>9.2f}  Δ={net_delta:+d}  [{pos_str}]"
                + (f"  OB5X={X:.3f}" if X else ""))

# ==============================================================================
# MAIN
# ==============================================================================
def new_exchange():
    ex = Exchange()
    ex.connect()
    return ex

def main():
    exchange    = new_exchange()
    logger.info("✓ Connected  |  trades → exports/trades.csv")
    instruments = exchange.get_instruments()
    iids        = list(instruments.keys())
    
    # Drain trade history so we only see new trades going forward
    for iid in iids:
        exchange.poll_new_trades(iid)
        
    exposed = {k: v for k, v in exchange.get_positions().items() if v != 0}
    if exposed: 
        logger.warning(f"Non-zero start: {exposed}")
        
    sweep = 0
    while True:
        try:
            positions = exchange.get_positions()
            books     = {iid: exchange.get_last_price_book(iid) for iid in iids}
            
            # Record fills FIRST (before placing new orders)
            record_trades(exchange, iids, books, instruments)
            
            s_dual(exchange, books, positions)
            s_fut_mm(exchange, books, positions, instruments)
            s_etf_mm(exchange, books, positions)
            s_stock_mm(exchange, books, positions)
            
            if sweep % DELTA_INTERVAL == 0:
                s_delta(exchange, books, positions)
                
            sweep += 1
            if sweep % LOG_EVERY == 0:
                log_snapshot(exchange, books, positions, sweep)
            if sweep % 100 == 0:
                print_trade_summary(positions)
                
        except KeyboardInterrupt:
            logger.info("Cancelling all orders...")
            for iid in iids:
                try: 
                    exchange.delete_orders(iid)
                except: 
                    pass
            print_trade_summary(exchange.get_positions())
            logger.info("Stopped. Check exports/trades.csv")
            break
            
        except Exception as exc:
            logger.warning(f"Error: {exc}")
            _fut_last_fair.clear()
            _fut_last_quote.clear()
            _stock_last_mid.clear()
            _stock_last_quote.clear()
            
            global _etf_last_nav, _etf_last_quote
            _etf_last_nav = None
            _etf_last_quote = None
            
            try:
                exchange = new_exchange()
                instruments = exchange.get_instruments()
                iids = list(instruments.keys())
                # Re-drain trade history after reconnect
                for iid in iids: 
                    exchange.poll_new_trades(iid)
                logger.info("✓ Reconnected.")
            except Exception as ce:
                logger.error(f"Reconnect failed: {ce}")
                time.sleep(2.0)

if __name__ == "__main__":
    main()