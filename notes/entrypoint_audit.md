# Entrypoint And Hygiene Audit

Scope: Issue #71 only, sliced from Issue #1. No trading logic or runtime behavior changed.

## Canonical Runtime Entrypoint

Use:

```bash
python main.py --config config/production.yaml --mode paper
python main.py --config config/production.yaml --mode live
```

Evidence:

- `run.bat` launches `main.py --config config/production.yaml --mode paper`.
- `run.ps1` launches `main.py --config config/production.yaml --mode $Mode`.
- `scripts/run_paper.ps1` launches `main.py --mode paper` after loading `.env`.
- `main.py` owns the CLI parser and the `if __name__ == '__main__': asyncio.run(main())` block.
- `tests/test_launcher_reconcile_scripts.py` asserts that the Windows launchers target `main.py` and do not point at `main_v2.py`.

## Alternate Entrypoints Kept

- `main_production.py`: compatibility alias only. Integration tests still import `main_production.ProductionTradingBot`, so removing it would break existing callers with no gain.
- `run.bat`: Windows convenience wrapper for the canonical `main.py` paper-mode command.
- `run.ps1`: PowerShell convenience wrapper for the canonical `main.py` command with mode selection and dependency checks.
- `scripts/run_paper.ps1`: environment-aware paper launcher that loads `.env` and then runs `main.py`.
- `run_paper_trading.bat`: kept for now because it looks like a legacy Phase 6 helper that starts multiple windows. It currently references `run_production_bot.py`, which is not present in the repo, so it should be treated as stale and handled in a separate cleanup ticket rather than rewritten or removed in this narrow slice.
- `scripts/phase6_validation_runner.py`: also references `run_production_bot.py` and appears tied to the same legacy validation workflow. Kept unchanged for the same reason.

## Alternate Entrypoints Already Absent

The following Issue #1 candidates are not present in the current repo tree, so there is nothing to consolidate or delete in this slice:

- `main_v2.py`
- `main_arb.py`
- `main_capital_doubler.py`
- `run_production_bot.py`

## Root Hygiene Audit

The Issue #1 junk files called out for deletion are already absent from this repo root:

- `New Text Document.txt`
- `New Text Document (2).txt`
- `New Text Document (3).txt`
- `file`
- `tmp_settle.txt`
- `tmp_audit_decimal_ctor_numeric.txt`
- `tmp_syntax.txt`
- `settlement_check.txt`
- `which_run_bot.txt`

`New folder/` is also not present, so there was nothing safe to remove.
