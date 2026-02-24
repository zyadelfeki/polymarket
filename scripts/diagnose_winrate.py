"""Diagnose performance tracker win-rate context."""
import json

with open("logs/paper-session.log", errors="replace") as f:
    lines = f.readlines()

print("=== First performance_halt_win_rate context ===")
for i, line in enumerate(lines):
    try:
        d = json.loads(line)
        if d.get("event") == "performance_halt_win_rate":
            for j in range(max(0, i - 5), min(len(lines), i + 3)):
                try:
                    dd = json.loads(lines[j])
                    items = {k: v for k, v in list(dd.items())[:8]}
                    print(f"  [{j}] {items}")
                except Exception:
                    pass
            break
    except Exception:
        pass

print()
print("=== First performance_tracker_update ===")
for line in lines:
    try:
        d = json.loads(line)
        if d.get("event") == "performance_tracker_update":
            print(f"  {dict(list(d.items())[:12])}")
            break
    except Exception:
        pass

print()
print("=== periodic_check_failed error detail (first 3) ===")
count = 0
for line in lines:
    try:
        d = json.loads(line)
        if d.get("event") == "periodic_check_failed":
            print(f"  error={d.get('error')}  step={d.get('step')}  error_type={d.get('error_type')}")
            count += 1
            if count >= 3:
                break
    except Exception:
        pass
