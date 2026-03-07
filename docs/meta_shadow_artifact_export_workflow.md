# Meta Shadow Artifact Export Workflow

This document is the operator-facing workflow for exporting the Ticket 4.7/4.8 meta shadow artifacts.

Scope:
- observational only
- no `main.py` dependency
- no live execution changes
- no promotion, sizing, or permissioning changes

## What The CLI Is For

Use `scripts/export_meta_shadow_artifacts.py` to generate two JSON artifacts:

1. `session-summary`
   Produces a structured shadow session summary.

2. `replay-agreement`
   Produces a structured replay agreement report that compares runtime shadow scoring with replayed offline scoring over a batch of feature payloads.

The CLI is intentionally separate from live trading. It reads feature payloads and promoted shadow artifacts, then writes reviewable JSON outputs.

## Stable Workflow

Use this workflow when you want a repeatable operator path without remembering the full command shape.

### Step 1: Prepare replay input

Create a replay input file in one of the supported formats:

- JSON array of feature objects
- JSON object containing `feature_batches`
- JSONL with one feature object per line

Required expectation:
- the feature payloads must match the promoted bundle's expected feature schema

Typical location:
- `results/meta_shadow_cli_samples/sample_replay_input.json`
- `results/meta_shadow_cli_samples/sample_replay_input.jsonl`

### Step 2: Identify the promoted offline artifacts

You need both:

- the promoted bundle path
- the training report path

Typical paths:

- `models/meta_gate/staging/final/promotable_model_bundle.joblib`
- `models/meta_gate/staging/final/training_report.json`

Or, for sample workspace outputs:

- `results/meta_shadow_cli_samples/artifacts/final/promotable_model_bundle.joblib`
- `results/meta_shadow_cli_samples/artifacts/final/training_report.json`

### Step 3: Export the session summary

Recommended stable operator pattern:
- run `session-summary` with `--replay-input-path`
- always pass `--expected-feature-schema-version`
- always pass explicit output path, session id, and timestamp when generating review artifacts

PowerShell example:

```powershell
$PYTHON = "c:/Users/zyade/polymarket/.venv/Scripts/python.exe"
$BUNDLE = "results/meta_shadow_cli_samples/artifacts/final/promotable_model_bundle.joblib"
$REPORT = "results/meta_shadow_cli_samples/artifacts/final/training_report.json"
$INPUT = "results/meta_shadow_cli_samples/sample_replay_input.json"
$OUTPUT = "results/meta_shadow_cli_samples/session_summary.json"

& $PYTHON scripts/export_meta_shadow_artifacts.py session-summary `
  --output-path $OUTPUT `
  --session-id sample-cli-session `
  --exported-at 2026-03-07T13:30:00Z `
  --replay-input-path $INPUT `
  --replay-input-format json `
  --expected-feature-schema-version meta_candidate_v1 `
  --promotable-model-bundle-path $BUNDLE `
  --promotion-report-path $REPORT
```

What this does:
- loads the promoted shadow bundle
- replays the batch through the observational shadow scorer
- exports a durable session summary JSON

### Step 4: Export the replay agreement report

Use the same replay batch and promoted artifact pair.

PowerShell example:

```powershell
$PYTHON = "c:/Users/zyade/polymarket/.venv/Scripts/python.exe"
$BUNDLE = "results/meta_shadow_cli_samples/artifacts/final/promotable_model_bundle.joblib"
$REPORT = "results/meta_shadow_cli_samples/artifacts/final/training_report.json"
$INPUT = "results/meta_shadow_cli_samples/sample_replay_input.jsonl"
$OUTPUT = "results/meta_shadow_cli_samples/replay_agreement.json"

& $PYTHON scripts/export_meta_shadow_artifacts.py replay-agreement `
  --input-path $INPUT `
  --input-format jsonl `
  --output-path $OUTPUT `
  --expected-feature-schema-version meta_candidate_v1 `
  --report-id sample-cli-replay `
  --exported-at 2026-03-07T13:35:00Z `
  --promotable-model-bundle-path $BUNDLE `
  --promotion-report-path $REPORT
```

What this does:
- loads the replay batch
- scores it through the runtime shadow path and the direct replay path
- exports a structured agreement report

## Supported Commands

### `session-summary`

Purpose:
- export a shadow session summary JSON artifact

Important arguments:
- `--output-path`: required
- `--session-id`: optional
- `--exported-at`: optional
- `--replay-input-path`: optional but recommended for stable operator use
- `--replay-input-format`: `auto`, `json`, `jsonl`
- `--expected-feature-schema-version`: required when `--replay-input-path` is used
- `--calibration-version`: optional, defaults to `platt_scaler_v1`
- `--promotable-model-bundle-path`: optional explicit promoted bundle path
- `--promotion-report-path`: optional explicit training report path

### `replay-agreement`

Purpose:
- export a shadow replay agreement JSON artifact from a replay batch

Important arguments:
- `--input-path`: required
- `--input-format`: `auto`, `json`, `jsonl`
- `--output-path`: required
- `--expected-feature-schema-version`: required
- `--calibration-version`: optional, defaults to `platt_scaler_v1`
- `--report-id`: optional
- `--exported-at`: optional
- `--tolerance`: optional, defaults to the runtime replay tolerance
- `--mismatch-sample-limit`: optional
- `--promotable-model-bundle-path`: optional explicit promoted bundle path
- `--promotion-report-path`: optional explicit training report path

## Supported Input Formats

### JSON array

```json
[
  {
    "selected_side_is_yes": 1.0,
    "raw_yes_prob": 0.71,
    "yes_side_raw_probability": 0.71
  }
]
```

### JSON object with `feature_batches`

```json
{
  "feature_batches": [
    {
      "selected_side_is_yes": 1.0,
      "raw_yes_prob": 0.71,
      "yes_side_raw_probability": 0.71
    }
  ]
}
```

### JSONL

```jsonl
{"selected_side_is_yes":1.0,"raw_yes_prob":0.71,"yes_side_raw_probability":0.71}
{"selected_side_is_yes":1.0,"raw_yes_prob":0.55,"yes_side_raw_probability":0.55}
```

Each replay item must be a JSON object.

## Output Artifact Paths

Recommended operator output locations:

- session summary: `results/meta_shadow_reviews/session_summary.json`
- replay agreement: `results/meta_shadow_reviews/replay_agreement.json`

Sample workspace outputs produced during Ticket 4.8:

- `results/meta_shadow_cli_samples/session_summary.json`
- `results/meta_shadow_cli_samples/replay_agreement.json`

## Common Failure Modes

### `ERROR: --expected-feature-schema-version is required when --replay-input-path is provided`

Meaning:
- you tried to generate a replay-backed session summary without explicitly stating the expected schema version

Fix:
- add `--expected-feature-schema-version meta_candidate_v1` or the correct promoted schema version

### `ERROR: unable to infer replay input format`

Meaning:
- the file extension was not enough for auto-detection

Fix:
- pass `--input-format json` or `--input-format jsonl`

### `ERROR: invalid JSON replay input ...`

Meaning:
- the JSON file is malformed

Fix:
- repair the JSON payload

### `ERROR: invalid JSONL replay input ... line N`

Meaning:
- one JSONL line is malformed

Fix:
- repair the specific line reported by the CLI

### `ERROR: replay input path does not exist`

Meaning:
- the replay input file path is wrong or missing

Fix:
- verify the file exists and the working directory is the repo root

### Shadow summary exports with zero decisions

Meaning:
- you ran `session-summary` without replay input in a fresh process, so there were no in-process shadow decisions to summarize

Fix:
- use the stable replay-backed session-summary workflow documented above

### Replay output shows `schema_mismatch_count > 0`

Meaning:
- some replay items do not match the promoted bundle's expected feature set

Fix:
- compare the replay payload keys against the promoted feature schema and repair missing or unexpected fields

### Replay output shows fallback load failures

Meaning:
- the promoted bundle or training report path is missing, invalid, or cross-reference validation failed

Fix:
- verify both artifact paths point to the same promoted artifact pair

## One Stable Operator Recipe

If you want one repeatable habit, use this:

1. Generate or collect a replay batch in JSON or JSONL.
2. Point both commands at the same promoted bundle and training report.
3. Run `session-summary` with `--replay-input-path`.
4. Run `replay-agreement` on the same batch.
5. Store both outputs under `results/meta_shadow_reviews/` for the review record.

That workflow is the current stable operator path for Ticket 4.9.