"""Not a test: prints how long the guard takes on a 200 KB argument (the number quoted in BUILD_REPORT.md)."""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import embargo  # noqa: E402

embargo.init(Path(tempfile.mkdtemp(prefix="embargo-bench-")))
embargo.tool_add({"text": "Acme Robotics said it closed a Series B round led by Northwind Capital " * 40, "until": "+1h", "label": "Bench"}, {})
filler = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor " * 2600
args = {"command": filler}
embargo.guard("terminal", args)  # warm
runs = []
for _ in range(5):
    t = time.perf_counter()
    assert embargo.guard("terminal", args) is None
    runs.append((time.perf_counter() - t) * 1000)
print(f"arg {len(filler) / 1000:.0f} KB: guard median {sorted(runs)[2]:.1f} ms, max {max(runs):.1f} ms")
