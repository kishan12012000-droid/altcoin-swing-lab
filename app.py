
from flask import Flask, jsonify

app = Flask(__name__)

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
    return jsonify({
        "status": "not_configured",
        "message": "Market data integration is the next step.",
        "signals": []
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
