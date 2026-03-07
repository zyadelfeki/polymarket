#!/usr/bin/env python3

import asyncio
import csv
import logging
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import structlog

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
	sys.path.insert(0, ROOT_DIR)

from database.ledger_async import AsyncLedger
from main import TradingSystem, _resolve_runtime_controls


class ProofCharlieGate:
	def __init__(self, result):
		self._result = result

	async def evaluate_market(self, **_kwargs):
		return self._result


class ProofRecommendation:
	def __init__(self, side: str = "YES"):
		self.side = side
		self.size = Decimal("10")
		self.kelly_fraction = Decimal("0.1")
		self.p_win = 0.62
		self.p_win_raw = 0.64
		self.p_win_calibrated = 0.62
		self.implied_prob = 0.5
		self.edge = 0.08
		self.confidence = 0.8
		self.regime = "BULLISH"
		self.technical_regime = "TRENDING"
		self.reason = "charlie_signal side=YES p_win=0.620 implied=0.500 edge=0.080 fee=0.010 conf=0.800"
		self.model_votes = None
		self.ofi_conflict = False


class StaticApiClient:
	def __init__(self, market_response):
		self.market_response = market_response

	async def get_market(self, _market_id):
		return self.market_response


def configure_logging() -> None:
	logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
	structlog.configure(
		processors=[
			structlog.stdlib.add_log_level,
			structlog.processors.TimeStamper(fmt="iso"),
			structlog.processors.JSONRenderer(),
		],
		wrapper_class=structlog.stdlib.BoundLogger,
		context_class=dict,
		logger_factory=structlog.stdlib.LoggerFactory(),
		cache_logger_on_first_use=True,
	)


async def build_system(root: Path) -> tuple[TradingSystem, AsyncLedger, Path, Path]:
	config = _resolve_runtime_controls(
		{
			"environment": "proof",
			"version": "proof-harness",
			"trading": {
				"paper_trading": True,
				"min_price": 0.01,
				"max_price": 0.99,
				"max_position_size_pct": 10.0,
				"min_position_size": 1.0,
				"max_order_size": 1000.0,
				"max_entry_price_abs": "0.65",
			},
			"strategies": {
				"latency_arb": {
					"max_position_size_pct": 5.0,
				}
			},
			"runtime_controls": {
				"blocked_markets": ["quarantine-proof-market"],
				"lifecycle_guard": {
					"enabled": True,
					"max_active_entries_per_market": 1,
					"allow_add_on": False,
					"min_price_improvement_abs": "0.05",
				},
				"calibration": {
					"fail_closed": True,
					"min_positive_coef": 0.0,
					"require_monotonic_smoke_test": True,
					"smoke_test_points": [0.30, 0.50, 0.79],
					"observe_only_on_invalid": True,
					"dataset_export_path": str(root / "calibration_dataset_v2.csv"),
					"observation_export_path": str(root / "calibration_observations.csv"),
				},
				"quarantine": {
					"enabled": True,
					"seed_static_blocklist": True,
					"auto_review_after_days": 7,
				},
			},
		}
	)

	system = TradingSystem(config)
	ledger = AsyncLedger(db_path=str(root / "proof.db"))
	await ledger.initialize()
	ledger.get_equity = AsyncMock(return_value=Decimal("100"))

	system.ledger = ledger
	system.execution = AsyncMock()
	system.execution.place_order_with_risk_check = AsyncMock()
	system.charlie_gate = ProofCharlieGate(ProofRecommendation())

	await system._initialize_quarantine_store()
	return (
		system,
		ledger,
		Path(config["runtime_controls"]["calibration"]["observation_export_path"]),
		Path(config["runtime_controls"]["calibration"]["dataset_export_path"]),
	)


async def main() -> None:
	configure_logging()
	root = Path(tempfile.mkdtemp(prefix="polymarket-proof-"))
	print(f"proof_root={root}")

	system, ledger, observation_path, dataset_path = await build_system(root)

	try:
		system._calibration_guard_status = {
			"blocked": True,
			"reason": "non_positive_coef=-0.1",
			"coef": -0.1,
			"monotonic": False,
		}
		await system._execute_opportunity(
			{
				"market_id": "market-proof-observe",
				"token_id": "token-yes",
				"side": "YES",
				"edge": Decimal("0.05"),
				"market_price": Decimal("0.41"),
				"confidence": "HIGH",
				"question": "Will BTC settle up?",
			},
			trigger="proof_bad_calibration",
		)

		system._calibration_guard_status = {
			"blocked": False,
			"reason": None,
			"coef": 0.2,
			"monotonic": True,
		}
		await system._execute_opportunity(
			{
				"market_id": "quarantine-proof-market",
				"token_id": "token-yes",
				"side": "YES",
				"edge": Decimal("0.05"),
				"market_price": Decimal("0.40"),
				"confidence": "HIGH",
				"question": "Will BTC settle up?",
			},
			trigger="proof_quarantine",
		)

		system.api_client = StaticApiClient(
			{
				"winning_side": "YES",
				"outcomePrices": ["1", "0"],
				"resolutionTime": "2026-03-06T19:15:00Z",
			}
		)
		await system._resolve_pending_calibration_observations()
		await system.calibration_observation_service.export_csv_artifacts()

		print("=== OBSERVATION ROWS ===")
		with open(observation_path, newline="", encoding="utf-8") as observation_file:
			for row in csv.DictReader(observation_file):
				print(row)

		print("=== DATASET ROWS ===")
		with open(dataset_path, newline="", encoding="utf-8") as dataset_file:
			for row in csv.DictReader(dataset_file):
				print(row)
	finally:
		await ledger.close()


if __name__ == "__main__":
	asyncio.run(main())
