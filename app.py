import os
import time
import math
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import Lock

import requests
from flask import Flask, jsonify

app = Flask(__name__)

# ---------------- CONFIGURATION ----------------

DELTA_API = "https://api.india.delta.exchange"

REQUEST_TIMEOUT = (3, 5)
SCANNER_WAIT_SECONDS = 8
CACHE_TTL_SECONDS = 10

MIN_SPOT_TURNOVER_USD = 1000
MIN_FUTURES_TURNOVER_USD = 50000
MAX_RESULTS = 30

EXCLUDED_ASSETS = {
    "BTC", "ETH", "XAUT", "USDT", "USDC", "DAI",
    "USD", "INR", "BUSD", "FDUSD", "TUSD"
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("altcoin_swing_lab")

session = requests.Session()
executor = ThreadPoolExecutor(max_workers=2)
cache_lock = Lock()

ticker_cache = {
    "spot": {"data": None, "time": 0},
    "perpetual_futures": {"data": None, "time": 0}
}


# ---------------- HELPERS ----------------

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def to_float(value):
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError, OverflowError):
        pass
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
                parts = symbol.split(separator)
                if len(parts) >= 2:
                    return parts[0].strip()

    if market == "perpetual_futures":
        for suffix in ("USDT", "USD"):
            if symbol.endswith(suffix):
                return symbol[:-len(suffix)]

    return ""


# ---------------- DELTA API ----------------

def fetch_tickers(market):
    """Fetch tickers with a short network timeout and basic validation."""

    response = session.get(
        f"{DELTA_API}/v2/tickers",
        params={"contract_types": market},
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as error:
        raise ValueError("Delta API returned invalid JSON") from error

    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError(f"Delta API reported failure for {market}")

    result = payload.get("result")

    if not isinstance(result, list):
        raise ValueError(f"Unexpected ticker format for {market}")

    return result


def get_tickers_cached(market):
    """Return recent cached data, otherwise fetch and cache fresh data."""

    now = time.time()

    with cache_lock:
        cached = ticker_cache[market]
        if (
            cached["data"] is not None
            and now - cached["time"] < CACHE_TTL_SECONDS
        ):
            return cached["data"], True

    data = fetch_tickers(market)

    with cache_lock:
        ticker_cache[market] = {
            "data": data,
            "time": time.time()
        }

    return data, False


# ---------------- SCANNER LOGIC ----------------

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
            if status not in {"operational", "live", "active", "trading"}:
                continue

        symbol = item.get("symbol")
        asset = get_asset(item, market)

        if not symbol or not asset:
            continue

        if asset in EXCLUDED_ASSETS:
            continue

        turnover = first_number(
            item,
            ["turnover_usd", "turnover_24h_usd", "turnover"]
        )

        price = first_number(
            item,
            ["spot_price", "mark_price", "close", "last_price"]
        )

        change = to_float(
            item.get("ltp_change_24h", item.get("change_24h"))
        )

        volume = to_float(item.get("volume"))

        if turnover is None or turnover < min_turnover:
            continue

        if price is None or change is None:
            continue

        if change > 0:
            direction = "BULLISH"
        elif change < 0:
            direction = "BEARISH"
        else:
            direction = "NEUTRAL"

        screened.append({
            "market": market,
            "symbol": symbol,
            "asset": asset,
            "price": price,
            "change_24h_pct": change,
            "direction_24h": direction,
            "trend_1h": "NOT_CHECKED",
            "trend_4h": "NOT_CHECKED",
            "volume": volume,
            "turnover_usd": turnover,
            "open_interest": item.get("oi"),
            "funding_rate": (
                item.get("funding_rate")
                if market == "perpetual_futures"
                else None
            ),
            "trading_status": status or "UNKNOWN"
        })

    screened.sort(
        key=lambda row: row["turnover_usd"],
        reverse=True
    )

    results = screened[:MAX_RESULTS]

    for rank, row in enumerate(results, start=1):
        row["liquidity_rank"] = rank

    return results


# ---------------- ROUTES ----------------

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
    markets = {
        "spot": {
            "count": 0,
            "data": [],
            "data_source": "Delta Exchange India"
        },
        "perpetual_futures": {
            "count": 0,
            "data": [],
            "data_source": "Delta Exchange India"
        }
    }

    errors = {}
    cache_used = {}

    jobs = {
        "spot": executor.submit(get_tickers_cached, "spot"),
        "perpetual_futures": executor.submit(
            get_tickers_cached, "perpetual_futures"
        )
    }

    for market, future in jobs.items():
        try:
            raw_data, was_cached = future.result(
                timeout=SCANNER_WAIT_SECONDS
            )

            min_turnover = (
                MIN_SPOT_TURNOVER_USD
                if market == "spot"
                else MIN_FUTURES_TURNOVER_USD
            )

            data = screen_market(raw_data, market, min_turnover)

            markets[market] = {
                "count": len(data),
                "data": data,
                "data_source": "Delta Exchange India"
            }

            cache_used[market] = was_cached

        except FutureTimeout:
            errors[market] = "Market data request timed out"
            logger.warning("Scanner timeout: %s", market)

        except (requests.RequestException, ValueError) as error:
            errors[market] = str(error)
            logger.warning("Market data error for %s: %s", market, error)

        except Exception:
            errors[market] = "Unexpected market data processing error"
            logger.exception("Unexpected scanner error: %s", market)

    # If a fetch failed, try returning the last successful cached snapshot.
    for market in list(errors):
        with cache_lock:
            cached = ticker_cache[market]["data"]
            cached_time = ticker_cache[market]["time"]

        if cached is not None:
            min_turnover = (
                MIN_SPOT_TURNOVER_USD
                if market == "spot"
                else MIN_FUTURES_TURNOVER_USD
            )

            data = screen_market(cached, market, min_turnover)
            markets[market] = {
                "count": len(data),
                "data": data,
                "data_source": "Delta Exchange India",
                "stale_cache": True,
                "cache_age_seconds": round(
                    max(0, time.time() - cached_time), 2
                )
            }

    status = "connected" if not errors else "degraded"

    return jsonify({
        "project": "Altcoin Swing Lab",
        "status": status,
        "source": "Delta Exchange India",
        "timestamp": utc_now(),
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


# ---------------- LOCAL START ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)