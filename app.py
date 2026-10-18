
import requests
from flask import Flask, jsonify

app = Flask(__name__)

DELTA_API = "https://api.india.delta.exchange"
TIMEOUT = 15


def fetch_tickers(contract_type):
    response = requests.get(
        f"{DELTA_API}/v2/tickers",
        params={"contract_types": contract_type},
        timeout=TIMEOUT
    )
    response.raise_for_status()
    data = response.json()

    if not data.get("success"):
        raise ValueError(f"Delta API failed for {contract_type}")

    return data.get("result", [])


def normalize_ticker(item, market):
    return {
        "market": market,
        "symbol": item.get("symbol"),
        "underlying": item.get("underlying_asset_symbol"),
        "price": item.get("spot_price") or item.get("close"),
        "change_24h_pct": item.get("ltp_change_24h"),
        "volume": item.get("volume"),
        "turnover_usd": item.get("turnover_usd"),
        "open_interest": item.get("oi"),
        "funding_rate": item.get("funding_rate"),
        "trading_status": item.get("product_trading_status")
            or item.get("trading_status")
    }


@app.route("/")
def home():
    return jsonify({
        "project": "Altcoin Swing Lab",
        "status": "running"
    })


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


@app.route("/api/scanner")
def scanner():
    try:
        spot = fetch_tickers("spot")
        futures = fetch_tickers("perpetual_futures")

        spot_rows = [
            normalize_ticker(item, "spot")
            for item in spot
            if item.get("contract_type") == "spot"
            and item.get("product_trading_status", item.get("trading_status"))
                == "operational"
            and item.get("symbol")
        ]

        futures_rows = [
            normalize_ticker(item, "perpetual_futures")
            for item in futures
            if item.get("contract_type") == "perpetual_futures"
            and item.get("product_trading_status", item.get("trading_status"))
                == "operational"
            and item.get("symbol")
        ]

        return jsonify({
            "status": "connected",
            "source": "Delta Exchange India",
            "markets": {
                "spot": {
                    "count": len(spot_rows),
                    "data": spot_rows
                },
                "perpetual_futures": {
                    "count": len(futures_rows),
                    "data": futures_rows
                }
            }
        })

    except (requests.RequestException, ValueError) as error:
        return jsonify({
            "status": "error",
            "message": "Could not fetch market data from Delta Exchange",
            "detail": str(error)
        }), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
