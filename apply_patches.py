with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Patch 1: regime_changed duplicate event= kwarg
assert '                    logger.info(\n                        "regime_changed",\n                        event="regime_changed",\n                        old_regime=self._dynamic_regime,' in content, 'PATCH 1 NOT FOUND'
content = content.replace(
    '                    logger.info(\n                        "regime_changed",\n                        event="regime_changed",\n                        old_regime=self._dynamic_regime,',
    '                    logger.info(\n                        "regime_changed",\n                        old_regime=self._dynamic_regime,', 1)

# Patch 2: regime_size_adjustment duplicate event= kwarg
assert '                    logger.info(\n                        "regime_size_adjustment",\n                        event="regime_size_adjustment",\n                        market_id=market_id,' in content, 'PATCH 2 NOT FOUND'
content = content.replace(
    '                    logger.info(\n                        "regime_size_adjustment",\n                        event="regime_size_adjustment",\n                        market_id=market_id,',
    '                    logger.info(\n                        "regime_size_adjustment",\n                        market_id=market_id,', 1)

# Patch 3: _last_model_mtime READ on method -> instance attr
assert '                    if getattr(self._periodic_maintenance, "_last_model_mtime", 0) < _mgp_mtime:' in content, 'PATCH 3 NOT FOUND'
content = content.replace(
    '                    if getattr(self._periodic_maintenance, "_last_model_mtime", 0) < _mgp_mtime:',
    '                    if self._meta_gate_last_model_mtime < _mgp_mtime:', 1)

# Patch 4: _last_model_mtime WRITE on method -> instance attr
assert '                        self._periodic_maintenance._last_model_mtime = _mgp_mtime  # type: ignore[attr-defined]' in content, 'PATCH 4 NOT FOUND'
content = content.replace(
    '                        self._periodic_maintenance._last_model_mtime = _mgp_mtime  # type: ignore[attr-defined]',
    '                        self._meta_gate_last_model_mtime = _mgp_mtime', 1)

# Patch 5: Add instance attr to __init__
assert '        self._dynamic_regime_ts: float = 0.0   # monotonic; gate against rapid updates' in content, 'PATCH 5 NOT FOUND'
content = content.replace(
    '        self._dynamic_regime_ts: float = 0.0   # monotonic; gate against rapid updates',
    '        self._dynamic_regime_ts: float = 0.0   # monotonic; gate against rapid updates\n        self._meta_gate_last_model_mtime: float = 0.0  # hot-reload mtime on instance, not method', 1)

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('All 5 patches applied.')