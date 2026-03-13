"""AST crawler: find all third-party imports in the codebase."""
import ast
import os
import sys

stdlib = set(sys.stdlib_module_names)
imports = set()
skip_dirs = {'.venv', '__pycache__', '.git', 'htmlcov', '.pytest_cache', 'node_modules'}

for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in skip_dirs]
    for f in files:
        if not f.endswith('.py'):
            continue
        try:
            src = open(os.path.join(root, f), encoding='utf-8', errors='ignore').read()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    imports.add(node.module.split('.')[0])
        except Exception:
            pass

third_party = sorted(imports - stdlib - {'__future__', ''})
for m in third_party:
    print(m)
