
import requests
from flask import Flask, jsonify

app = Flask(__name__)

DELTA_API = "https://api.india.delta.exchange"


@app.route("/")
def home():
    return jsonify({
        "project": "Altcoin Swing Lab",
        "status": "running",
        "message": "Altcoin Swing Scanner API"
    })


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


@app.route("/api/scanner")
def scanner():
    try:
        response = requests.get(
            f"{DELTA_API}/v2/tickers",
            timeout=15
        )
        response.raise_for_status()
        payload = response.json()

        if not payload.get("success"):
            return jsonify({
                "status": "error",
                "message": "Delta API did not return success"
            }), 502

        tickers = payload.get("result", [])
        signals = []

        for item in tickers:
            symbol = item.get("symbol", "")
            price = item.get("close") or item.get("mark_price")
            volume = item.get("volume")

            if not symbol or price is None:
                continue

            signals.append({
                "symbol": symbol,
                "price": price,
                "volume": volume
            })

        return jsonify({
            "status": "connected",
            "source": "Delta Exchange India",
            "count": len(signals),
            "signals": signals
        })

    except requests.RequestException as error:
        return jsonify({
            "status": "error",
            "message": "Could not fetch Delta Exchange market data",
            "detail": str(error)
        }), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

