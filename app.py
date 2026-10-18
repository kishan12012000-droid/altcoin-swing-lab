
import os
import time
import math
import logging
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# =========================================================
# CONFIGURATION
# =========================================================

DELTA_API = "https://api.india.delta.exchange"

REQUEST_TIMEOUT = (2, 4)
CACHE_TTL = 10
CANDLE_CACHE_TTL = 300
MAX_RESULTS = 30
MAX_ANALYSED_PER_MARKET = 5
MAX_CANDLE_WORKERS = 10

MIN_SPOT_TURNOVER_USD = 1000
MIN_FUTURES_TURNOVER_USD = 50000

EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
BREAKOUT_LOOKBACK = 20
MIN_CANDLES = 60
ATR_STOP_MULTIPLIER = 1.5
TARGET_R_MULTIPLIERS = (1.5, 2.0)

EXCLUDED_ASSETS = {
    "BTC", "ETH", "XAUT", "USDT", "USDC", "DAI",
    "USD", "INR", "BUSD", "FDUSD", "TUSD"
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("altcoin_swing_lab")

executor = ThreadPoolExecutor(max_workers=MAX_CANDLE_WORKERS)
cache_lock = threading.Lock()

ticker_cache = {
    "spot": {"data": None, "time": 0},
    "perpetual_futures": {"data": None, "time": 0}
}
candle_cache = {}

# =========================================================
# HELPERS
# =========================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def to_float(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def first_number(item, keys):
    for key in keys:
        number = to_float(item.get(key))
        if number is not None and number > 0:
            return number
    return None


def get_asset(item, market):
    asset = item.get("underlying_asset_symbol")

    if isinstance(asset, str) and asset.strip():
        return asset.strip().upper()

    symbol = item.get("symbol", "")
    if not isinstance(symbol, str):
        return ""

    symbol = symbol.strip().upper()

    if market == "spot":
        for separator in ("_", "-", "/"):
            if separator in symbol:
                return symbol.split(separator)[0].strip()

    if market == "perpetual_futures":
        for suffix in ("USDT", "USD"):
            if symbol.endswith(suffix):
                return symbol[:-len(suffix)]

    return ""


def api_get(path, params):
    response = requests.get(
        f"{DELTA_API}{path}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError(f"Delta API failure: {path}")

    return payload


# =========================================================
# TICKER DATA
# =========================================================

def fetch_tickers(market):
    payload = api_get(
        "/v2/tickers",
        {"contract_types": market}
    )

    result = payload.get("result")
    if not isinstance(result, list):
        raise ValueError(f"Invalid ticker data: {market}")

    return result


def get_tickers_cached(market):
    now = time.time()

    with cache_lock:
        cached = ticker_cache[market]
        if cached["data"] is not None and (
            now - cached["time"] < CACHE_TTL
        ):
            return cached["data"], True

    data = fetch_tickers(market)

    with cache_lock:
        ticker_cache[market] = {
            "data": data,
            "time": time.time()
        }

    return data, False


def screen_market(items, market, min_turnover):
    screened = []

    for item in items:
        if not isinstance(item, dict):
            continue

        contract_type = item.get("contract_type")
        if contract_type and contract_type != market:
            continue

        status = (
            item.get("product_trading_status")
            or item.get("trading_status")
        )

        if status:
            status = str(status).lower()
            if status not in {
                "operational", "live", "active", "trading"
            }:
                continue

        symbol = item.get("symbol")
        asset = get_asset(item, market)

        if not symbol or not asset or asset in EXCLUDED_ASSETS:
            continue

        turnover = first_number(
            item, ["turnover_usd", "turnover_24h_usd", "turnover"]
        )
        price = first_number(
            item, ["spot_price", "mark_price", "close", "last_price"]
        )
        change = to_float(
            item.get("ltp_change_24h", item.get("change_24h"))
        )
        volume = to_float(item.get("volume"))

        if turnover is None or turnover < min_turnover:
            continue
        if price is None or change is None:
            continue

        screened.append({
            "market": market,
            "symbol": symbol,
            "asset": asset,
            "price": price,
            "change_24h_pct": change,
            "direction_24h": (
                "BULLISH" if change > 0 else
                "BEARISH" if change < 0 else "NEUTRAL"
            ),
            "trend_1h": "NOT_CHECKED",
            "trend_4h": "NOT_CHECKED",
            "volume": volume,
            "turnover_usd": turnover,
            "open_interest": item.get("oi"),
            "funding_rate": (
                item.get("funding_rate")
                if market == "perpetual_futures" else None
            ),
            "trading_status": status or "UNKNOWN"
        })

    screened.sort(
        key=lambda row: row["turnover_usd"], reverse=True
    )

    results = screened[:MAX_RESULTS]
    for rank, row in enumerate(results, 1):
        row["liquidity_rank"] = rank

    return results


# =========================================================
# CANDLE DATA
# =========================================================

def fetch_candles(symbol, resolution):
    cache_key = (symbol, resolution)
    now = time.time()

    with cache_lock:
        cached = candle_cache.get(cache_key)
        if cached and now - cached["time"] < CANDLE_CACHE_TTL:
            return cached["data"], True

    duration = 3600 if resolution == "1h" else 14400
    end = int(time.time())
    start = end - duration * 100

    payload = api_get(
        "/v2/history/candles",
        {
            "resolution": resolution,
            "symbol": symbol,
            "start": start,
            "end": end
        }
    )

    result = payload.get("result")
    if not isinstance(result, list):
        raise ValueError(f"Invalid candle data: {symbol} {resolution}")

    candles = []
    for candle in result:
        if not isinstance(candle, dict):
            continue

        values = {
            "time": to_float(candle.get("time")),
            "open": to_float(candle.get("open")),
            "high": to_float(candle.get("high")),
            "low": to_float(candle.get("low")),
            "close": to_float(candle.get("close")),
            "volume": to_float(candle.get("volume"))
        }

        if all(values[key] is not None for key in (
            "time", "open", "high", "low", "close"
        )):
            if (
                values["close"] > 0
                and values["high"] >= values["low"]
                and values["high"] >= values["close"]
                and values["low"] <= values["close"]
            ):
                candles.append(values)

    candles.sort(key=lambda candle: candle["time"])

    # Exclude the current, potentially incomplete candle.
    if candles:
        current_bucket = int(time.time() // duration)
        candles = [
            candle for candle in candles
            if int(candle["time"] // duration) < current_bucket
        ]

    if len(candles) < MIN_CANDLES:
        raise ValueError(
            f"Insufficient completed candles for {symbol} {resolution}"
        )

    with cache_lock:
        candle_cache[cache_key] = {
            "data": candles,
            "time": time.time()
        }

    return candles, False


# =========================================================
# INDICATORS
# =========================================================

def ema(values, period):
    if len(values) < period:
        return None

    # Seed with the first-period simple moving average.
    result = sum(values[:period]) / period
    multiplier = 2.0 / (period + 1)

    for value in values[period:]:
        result = value * multiplier + result * (1 - multiplier)

    return result


def calculate_atr(candles, period=ATR_PERIOD):
    if len(candles) < period + 1:
        return None

    true_ranges = []

    for index in range(1, len(candles)):
        current = candles[index]
        previous_close = candles[index - 1]["close"]

        true_range = max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close)
        )
        true_ranges.append(true_range)

    if len(true_ranges) < period:
        return None

    atr = sum(true_ranges[:period]) / period

    for value in true_ranges[period:]:
        atr = ((atr * (period - 1)) + value) / period

    return atr


def calculate_trend(candles):
    closes = [candle["close"] for candle in candles]

    if len(closes) < MIN_CANDLES:
        return {
            "trend": "INSUFFICIENT_DATA",
            "ema_20": None,
            "ema_50": None
        }

    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW)

    if fast is None or slow is None:
        trend = "INSUFFICIENT_DATA"
    elif fast > slow and closes[-1] > fast:
        trend = "BULLISH"
    elif fast < slow and closes[-1] < fast:
        trend = "BEARISH"
    else:
        trend = "SIDEWAYS"

    return {
        "trend": trend,
        "ema_20": round(fast, 10),
        "ema_50": round(slow, 10)
    }


# =========================================================
# SWING SETUP ANALYSIS
# =========================================================

def analyse_market_item(item):
    symbol = item["symbol"]
    market = item["market"]

    output = dict(item)
    output.update({
        "trend_1h": "NOT_CHECKED",
        "trend_4h": "NOT_CHECKED",
        "setup": "NONE",
        "signal": "WAIT",
        "entry": None,
        "stop_loss": None,
        "target_1": None,
        "target_2": None,
        "risk_reward_target_1": None,
        "risk_reward_target_2": None,
        "atr_1h": None,
        "analysis_status": "PENDING",
        "analysis_errors": []
    })

    try:
        candles_1h, _ = fetch_candles(symbol, "1h")
        candles_4h, _ = fetch_candles(symbol, "4h")

        trend_1h = calculate_trend(candles_1h)
        trend_4h = calculate_trend(candles_4h)

        output["trend_1h"] = trend_1h["trend"]
        output["trend_4h"] = trend_4h["trend"]
        output["ema_20_1h"] = trend_1h["ema_20"]
        output["ema_50_1h"] = trend_1h["ema_50"]
        output["ema_20_4h"] = trend_4h["ema_20"]
        output["ema_50_4h"] = trend_4h["ema_50"]

        atr = calculate_atr(candles_1h)
        if atr is None or atr <= 0:
            raise ValueError("ATR unavailable")

        output["atr_1h"] = round(atr, 10)

        # Use the latest completed candle for all setup decisions.
        last = candles_1h[-1]
        previous = candles_1h[-BREAKOUT_LOOKBACK - 1:-1]

        if len(previous) < BREAKOUT_LOOKBACK:
            raise ValueError("Insufficient candles for breakout check")

        prior_high = max(c["high"] for c in previous)
        prior_low = min(c["low"] for c in previous)

        close = last["close"]
        bullish_candle = last["close"] > last["open"]
        bearish_candle = last["close"] < last["open"]

        pullback_long = (
            trend_1h["trend"] == "BULLISH"
            and close >= trend_1h["ema_20"]
            and bullish_candle
        )
        pullback_short = (
            trend_1h["trend"] == "BEARISH"
            and close <= trend_1h["ema_20"]
            and bearish_candle
        )

        breakout_long = close > prior_high and bullish_candle
        breakout_short = close < prior_low and bearish_candle

        direction = None
        setup = "NONE"

        if (
            trend_4h["trend"] == "BULLISH"
            and trend_1h["trend"] == "BULLISH"
        ):
            if breakout_long:
                direction, setup = "LONG", "BREAKOUT"
            elif pullback_long:
                direction, setup = "LONG", "PULLBACK"

        # Spot markets do not support short selling in this scanner.
        if market == "perpetual_futures" and (
            trend_4h["trend"] == "BEARISH"
            and trend_1h["trend"] == "BEARISH"
        ):
            if breakout_short:
                direction, setup = "SHORT", "BREAKDOWN"
            elif pullback_short:
                direction, setup = "SHORT", "PULLBACK"

        output["setup"] = setup
        output["analysis_status"] = "ANALYSED"

        if direction is None:
            return output

        entry = close
        risk_distance = atr * ATR_STOP_MULTIPLIER

        if direction == "LONG":
            stop_loss = entry - risk_distance
            target_1 = entry + risk_distance * TARGET_R_MULTIPLIERS[0]
            target_2 = entry + risk_distance * TARGET_R_MULTIPLIERS[1]
        else:
            stop_loss = entry + risk_distance
            target_1 = entry - risk_distance * TARGET_R_MULTIPLIERS[0]
            target_2 = entry - risk_distance * TARGET_R_MULTIPLIERS[1]

        if stop_loss <= 0 or target_1 <= 0 or target_2 <= 0:
            output["analysis_errors"].append("Invalid price levels")
            return output

        output.update({
            "signal": direction,
            "entry": round(entry, 10),
            "stop_loss": round(stop_loss, 10),
            "target_1": round(target_1, 10),
            "target_2": round(target_2, 10),
            "risk_reward_target_1": TARGET_R_MULTIPLIERS[0],
            "risk_reward_target_2": TARGET_R_MULTIPLIERS[1]
        })

        return output

    except (requests.RequestException, ValueError) as error:
        output["analysis_status"] = "DATA_ERROR"
        output["analysis_errors"] = [str(error)]
        return output
    except Exception:
        logger.exception("Analysis failed for %s", symbol)
        output["analysis_status"] = "ANALYSIS_ERROR"
        output["analysis_errors"] = ["Unexpected analysis error"]
        return output


def analyse_candidates(candidates):
    if not candidates:
        return []

    future_map = {
        executor.submit(analyse_market_item, item): item
        for item in candidates
    }

    results = []
    deadline = time.monotonic() + 12

    try:
        for future in as_completed(
            future_map,
            timeout=max(0.1, deadline - time.monotonic())
        ):
            try:
                results.append(future.result())
            except Exception:
                item = future_map[future]
                fallback = dict(item)
                fallback.update({
                    "signal": "WAIT",
                    "setup": "NONE",
                    "analysis_status": "ANALYSIS_ERROR"
                })
                results.append(fallback)
    except TimeoutError:
        logger.warning("Candle analysis reached its time limit")

    completed_symbols = {item["symbol"] for item in results}

    for future, item in future_map.items():
        if item["symbol"] not in completed_symbols:
            future.cancel()
            fallback = dict(item)
            fallback.update({
                "signal": "WAIT",
                "setup": "NONE",
                "analysis_status": "PENDING",
                "analysis_errors": ["Analysis deadline reached"]
            })
            results.append(fallback)

    results.sort(
        key=lambda row: (
            row.get("signal") in ("LONG", "SHORT"),
            row.get("turnover_usd", 0)
        ),
        reverse=True
    )
    return results


# =========================================================
# ROUTES
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "project": "Altcoin Swing Lab",
        "status": "running",
        "message": "Altcoin Swing Scanner API",
        "endpoints": ["/", "/health", "/api/scanner"],
        "timestamp": utc_now()
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "service": "Altcoin Swing Lab",
        "timestamp": utc_now()
    }), 200


@app.route("/api/scanner", methods=["GET"])
def scanner():
    # Optional query parameter: ?analyse=true
    # Analysis is enabled by default.
    analyse = request.args.get("analyse", "true").lower() not in {
        "false", "0", "no"
    }

    markets = {
        "spot": {"count": 0, "data": [], "data_source": "Delta Exchange India"},
        "perpetual_futures": {
            "count": 0, "data": [], "data_source": "Delta Exchange India"
        }
    }

    errors = {}
    cache_used = {}

    jobs = {
        market: executor.submit(get_tickers_cached, market)
        for market in ("spot", "perpetual_futures")
    }

    for market, future in jobs.items():
        try:
            raw_data, was_cached = future.result(timeout=7)

            min_turnover = (
                MIN_SPOT_TURNOVER_USD if market == "spot"
                else MIN_FUTURES_TURNOVER_USD
            )

            data = screen_market(raw_data, market, min_turnover)
            markets[market] = {
                "count": len(data),
                "data": data,
                "data_source": "Delta Exchange India"
            }
            cache_used[market] = was_cached

        except Exception as error:
            errors[market] = str(error)
            logger.warning("Ticker fetch failed for %s: %s", market, error)

            with cache_lock:
                cached = ticker_cache[market]["data"]
                cached_time = ticker_cache[market]["time"]

            if cached is not None:
                data = screen_market(
                    cached,
                    market,
                    MIN_SPOT_TURNOVER_USD if market == "spot"
                    else MIN_FUTURES_TURNOVER_USD
                )
                markets[market] = {
                    "count": len(data),
                    "data": data,
                    "data_source": "Delta Exchange India",
                    "stale_cache": True,
                    "cache_age_seconds": round(
                        max(0, time.time() - cached_time), 2
                    )
                }

    if analyse:
        for market in ("spot", "perpetual_futures"):
            original = markets[market]["data"]
            candidates = original[:MAX_ANALYSED_PER_MARKET]
            analysed = analyse_candidates(candidates)

            analysed_by_symbol = {
                item["symbol"]: item for item in analysed
            }

            merged = [
                analysed_by_symbol.get(item["symbol"], item)
                for item in original
            ]

            markets[market]["data"] = merged
            markets[market]["analysed_count"] = len(
                analysed_by_symbol
            )
            markets[market]["analysis_limit"] = MAX_ANALYSED_PER_MARKET

    status = "connected" if not errors else "degraded"

    return jsonify({
        "project": "Altcoin Swing Lab",
        "status": status,
        "source": "Delta Exchange India",
        "timestamp": utc_now(),
        "analysis_enabled": analyse,
        "strategy": {
            "trend_method": "EMA20/EMA50 on completed candles",
            "trend_timeframes": ["1h", "4h"],
            "setups": ["BREAKOUT", "BREAKDOWN", "PULLBACK"],
            "stop_method": "1.5 x 1H ATR",
            "target_r_multipliers": list(TARGET_R_MULTIPLIERS),
            "short_signals": "Perpetual futures only",
            "note": "Signals are candidates, not guaranteed outcomes"
        },
        "screening": {
            "excluded_assets": sorted(EXCLUDED_ASSETS),
            "minimum_spot_turnover_usd": MIN_SPOT_TURNOVER_USD,
            "minimum_futures_turnover_usd": MIN_FUTURES_TURNOVER_USD,
            "ranking": "turnover_usd_descending",
            "max_results_per_market": MAX_RESULTS
        },
        "cache_used": cache_used,
        "errors": errors,
        "markets": markets
    }), 200


# =========================================================
# LOCAL START
# =========================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
