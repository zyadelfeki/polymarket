"""Find the exact log line for predict_15min_move_called_directly."""
import json

with open("logs/paper-session.log", errors="replace") as f:
    for i, line in enumerate(f):
        try:
            d = json.loads(line)
            ev = d.get("event", "")
            if "predict_15min" in ev:
                print(f"Line {i}: {d}")
                break
        except Exception:
            pass
