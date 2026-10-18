
import requests
import time
from flask import Flask, jsonify

app = Flask(__name__)

DELTA_API = "https://api.india.delta.exchange"
TIMEOUT = 15

# Initial screening thresholds
MIN_SPOT_TURNOVER_USD = 1000
MIN_FUTURES_TURNOVER_USD = 50000
MAX_RESULTS = 30

# BTC, ETH aur stablecoins ko scanner se exclude karein
EXCLUDED_ASSETS = {
    "BTC", "ETH", "XAUT", "USDT", "USDC", "DAI"
}


def to_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def get_asset(item, market):
    asset = item.get("underlying_asset_symbol")

    if asset:
        return asset.upper()

    symbol = item.get("symbol", "").upper()

    if market == "spot" and "_INR" in symbol:
        return symbol.split("_INR")[0]

    if market == "perpetual_futures" and symbol.endswith("USD"):
        return symbol[:-3]

    return ""


def fetch_tickers(contract_type):
    response = requests.get(
        f"{DELTA_API}/v2/tickers",
        params={"contract_types": contract_type},
        timeout=TIMEOUT
    )

    response.raise_for_status()
    payload = response.json()

    if not payload.get("success"):
        raise ValueError(
            f"Ticker request failed: {contract_type}"
        )

    return payload.get("result", [])


def fetch_candles(symbol, resolution):
    end = int(time.time())
    start = end - (7 * 24 * 60 * 60)

    response = requests.get(
        f"{DELTA_API}/v2/history/candles",
        params={
            "resolution": resolution,
            "symbol": symbol,
            "start": start,
            "end": end
        },
        timeout=TIMEOUT
    )

    response.raise_for_status()
    payload = response.json()

    if not payload.get("success"):
        raise ValueError(
            f"Candle request failed: {symbol}, {resolution}"
        )

    return payload.get("result", [])

def calculate_trend(candles):
    if not candles or len(candles) < 20:
        return "INSUFFICIENT_DATA"

    try:
        candles = sorted(
            candles,
            key=lambda candle: candle["time"]
        )

        closes = [
            float(candle["close"])
            for candle in candles
        ]

        recent_closes = closes[-20:]
        sma20 = sum(recent_closes) / len(recent_closes)
        latest_close = closes[-1]

        if latest_close > sma20:
            return "BULLISH"
        elif latest_close < sma20:
            return "BEARISH"
        else:
            return "NEUTRAL"

    except (KeyError, TypeError, ValueError):
        return "INVALID_DATA"




def screen_market(items, market, min_turnover):
    screened = []

    for item in items:
        if item.get("contract_type") != market:
            continue

        status = (
            item.get("product_trading_status")
            or item.get("trading_status")
        )

        if status != "operational":
            continue

        symbol = item.get("symbol")
        asset = get_asset(item, market)

        turnover = to_float(item.get("turnover_usd"))
        price = to_float(item.get("spot_price"))

        if price is None:
            price = to_float(item.get("mark_price"))

        if price is None:
            price = to_float(item.get("close"))

        change = to_float(item.get("ltp_change_24h"))
        volume = to_float(item.get("volume"))

        # Required data validation
        if not symbol or not asset:
            continue

        if asset in EXCLUDED_ASSETS:
            continue

        if turnover is None or turnover < min_turnover:
            continue

        if price is None or price <= 0:
            continue

        # Skip markets without 24-hour change data
        if change is None:
            continue

        # 24-hour direction label
        if change > 0:
            direction = "BULLISH"
        elif change < 0:
            direction = "BEARISH"
        else:
            direction = "NEUTRAL"

        trend_1h = "NOT_CHECKED"
        trend_4h = "NOT_CHECKED"

        if market == "perpetual_futures":
            try:
                candles_1h = fetch_candles(symbol, "1h")
                candles_4h = fetch_candles(symbol, "4h")

                trend_1h = calculate_trend(candles_1h)
                trend_4h = calculate_trend(candles_4h)

            except (requests.RequestException, ValueError):
                trend_1h = "DATA_ERROR"
                trend_4h = "DATA_ERROR"


        screened.append({
            "market": market,
            "symbol": symbol,
            "asset": asset,
            "price": price,
            "change_24h_pct": change,
            "direction_24h": direction,
            "trend_1h": trend_1h,
            "trend_4h": trend_4h,
            "volume": volume,
            "turnover_usd": turnover,
            "open_interest": item.get("oi"),
            "funding_rate": (
                item.get("funding_rate")
                if market == "perpetual_futures"
                else None
            ),
            "trading_status": status
        })

    # Highest turnover first
    screened.sort(
        key=lambda row: row["turnover_usd"],
        reverse=True
    )

    results = screened[:MAX_RESULTS]

    for rank, row in enumerate(results, start=1):
        row["liquidity_rank"] = rank

    return results


@app.route("/")
def home():
    return jsonify({
        "project": "Altcoin Swing Lab",
        "status": "running",
        "message": "Altcoin Swing Scanner API"
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "healthy"
    })


@app.route("/api/scanner")
def scanner():
    try:
        spot_raw = fetch_tickers("spot")

        futures_raw = fetch_tickers(
            "perpetual_futures"
        )

        spot = screen_market(
            spot_raw,
            "spot",
            MIN_SPOT_TURNOVER_USD
        )

        futures = screen_market(
            futures_raw,
            "perpetual_futures",
            MIN_FUTURES_TURNOVER_USD
        )

        return jsonify({
            "status": "connected",
            "source": "Delta Exchange India",

            "screening": {
                "excluded_assets": sorted(EXCLUDED_ASSETS),
                "minimum_spot_turnover_usd":
                    MIN_SPOT_TURNOVER_USD,
                "minimum_futures_turnover_usd":
                    MIN_FUTURES_TURNOVER_USD,
                "ranking": "turnover_usd_descending",
                "max_results_per_market": MAX_RESULTS
            },

            "markets": {
                "spot": {
                    "count": len(spot),
                    "data": spot
                },

                "perpetual_futures": {
                    "count": len(futures),
                    "data": futures
                }
            }
        })

    except (
        requests.RequestException,
        ValueError
    ) as error:
        return jsonify({
            "status": "error",
            "message": "Market data request failed",
            "detail": str(error)
        }), 502


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
