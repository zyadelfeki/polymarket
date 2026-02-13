"""Benchmark standard json vs orjson"""
import time
import json as stdlib_json
from decimal import Decimal
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utils.json_helpers import dumps as orjson_dumps, loads as orjson_loads


def benchmark_serialization(iterations=10000):
    """Test serialization speed"""
    test_data = {
        'equity': Decimal("13.98"),
        'balance': Decimal("100.00"),
        'price': Decimal("96543.21"),
        'markets': [
            {'id': f'market_{i}', 'price': Decimal(f"{95000 + i}")}
            for i in range(10)
        ]
    }

    print("=" * 60)
    print("JSON SERIALIZATION BENCHMARK")
    print("=" * 60)

    class DecimalEncoder(stdlib_json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, Decimal):
                return str(obj)
            return super().default(obj)

    start = time.time()
    for _ in range(iterations):
        stdlib_json.dumps(test_data, cls=DecimalEncoder)
    stdlib_time = time.time() - start

    start = time.time()
    for _ in range(iterations):
        orjson_dumps(test_data)
    orjson_time = time.time() - start

    print(f"Iterations: {iterations}")
    print(f"\nStandard json: {stdlib_time:.3f}s ({iterations/stdlib_time:.0f} ops/sec)")
    print(f"orjson:        {orjson_time:.3f}s ({iterations/orjson_time:.0f} ops/sec)")
    print(f"\nSpeedup: {stdlib_time/orjson_time:.2f}x faster")
    print("=" * 60)

    if orjson_time < stdlib_time:
        print("✅ orjson is faster")
    else:
        print("⚠️  Standard json is faster (unexpected)")


if __name__ == "__main__":
    benchmark_serialization()
