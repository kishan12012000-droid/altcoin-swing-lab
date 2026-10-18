
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify

app = Flask(__name__)

DELTA_API = "https://api.india.delta.exchange"
TIMEOUT = 10

MIN_SPOT_TURNOVER_USD = 1000
MIN_FUTURES_TURNOVER_USD = 50000
MAX_RESULTS = 30

EXCLUDED_ASSETS = {
    "BTC", "ETH", "XAUT", "USDT", "USDC", "DAI"
}


def to_float(value):
    try:
        number = float(value) if value is not None else None
        return number if number is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def get_asset(item, market):
    asset = item.get("underlying_asset_symbol")

    if asset:
        return str(asset).upper()

    symbol = str(item.get("symbol") or "").upper()

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

    if not isinstance(payload, dict):
        raise ValueError("Unexpected ticker API response")

    if not payload.get("success"):
        raise ValueError(
            f"Delta ticker API failed: {contract_type}"
        )

    result = payload.get("result", [])

    if not isinstance(result, list):
        raise ValueError(
            f"Invalid ticker data: {contract_type}"
        )

    return result


def screen_market(items, market, min_turnover):
    screened = []

    for item in items:
        if not isinstance(item, dict):
            continue

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

        if not symbol or not asset:
            continue

        if asset in EXCLUDED_ASSETS:
            continue

        if turnover is None or turnover < min_turnover:
            continue

        if price is None or price <= 0:
            continue

        if change is None:
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
            "trading_status": status
        })

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
        # Fetch Spot and Futures data concurrently.
        with ThreadPoolExecutor(max_workers=2) as executor:
            spot_future = executor.submit(
                fetch_tickers, "spot"
            )
            futures_future = executor.submit(
                fetch_tickers, "perpetual_futures"
            )

            spot_raw = spot_future.result()
            futures_raw = futures_future.result()

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

    except (requests.RequestException, ValueError) as error:
        app.logger.exception("Scanner market-data request failed")

        return jsonify({
            "status": "error",
            "message": "Market data request failed",
            "detail": str(error)
        }), 502

    except Exception:
        app.logger.exception("Unexpected scanner error")

        return jsonify({
            "status": "error",
            "message": "Unexpected internal scanner error"
        }), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
