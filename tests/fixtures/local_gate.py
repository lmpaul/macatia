"""A scripted local gate: each call takes the next result from a JSON list; the last repeats.

A failing call prints a long log and exits with secrets on stderr, so a test can
check that Ralph keeps the complete streams and redacts them.
"""

import json
import pathlib
import sys


results_path = pathlib.Path(sys.argv[1])
results = json.loads(results_path.read_text(encoding="utf-8"))
result = results.pop(0) if len(results) > 1 else results[0]
results_path.write_text(json.dumps(results), encoding="utf-8")
if result == "fail":
    print("AssertionError: expected local value 42, received 0")
    print("\n".join(f"local progress {index}" for index in range(100)))
    print("last local log line")
    sys.exit(
        'fixture gate failed: password=super-secret token: "hidden-token" '
        "Bearer bearer-secret https://user:password@example.test ```escaped fence"
    )
