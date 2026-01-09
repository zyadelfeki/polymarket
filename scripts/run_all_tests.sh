#!/bin/bash

set -e

echo "========================================"
echo "POLYMARKET BOT - FULL TEST SUITE"
echo "========================================"
echo ""

echo "Step 1: Validating setup..."
python scripts/validate_setup.py
echo ""

echo "Step 2: Testing Kelly sizer..."
python scripts/test_kelly.py
echo ""

echo "Step 3: Testing Binance feed (30 seconds)..."
timeout 30 python scripts/test_binance.py || true
echo ""

echo "Step 4: Testing Polymarket client..."
python scripts/test_polymarket.py
echo ""

echo "========================================"
echo "✅ ALL TESTS COMPLETE"
echo "========================================"
echo ""
echo "Ready to run: python main.py"
echo ""