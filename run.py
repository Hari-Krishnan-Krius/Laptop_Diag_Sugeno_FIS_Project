#!/usr/bin/env python3
"""
Laptop Diagnostics System — Startup Script
Run this file to start the server: python run.py
"""

import os
from dotenv import load_dotenv

# Load .env if it exists
load_dotenv()

from app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"

    print("=" * 60)
    print("  💻 Laptop Motherboard Diagnostics System")
    print("     Sugeno Fuzzy Inference System")
    print("     Laptop-Motherboard-Diagnostics · 2026")
    print("=" * 60)
    print(f"  ➡  Open in browser:  http://localhost:{port}")
    print(f"  ➡  MongoDB URI:       {os.environ.get('MONGO_URI', 'mongodb://localhost:27017/laptop_diagnostics')}")
    print("=" * 60)

    app.run(debug=debug, host="0.0.0.0", port=port, use_reloader=False)
