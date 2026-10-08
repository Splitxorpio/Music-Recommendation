"""Where everything lives. FREQUENCY_CACHE=/some/dir points a run (or the dashboard) at another
cache folder, e.g. for benchmarking without touching your real data."""
import os
from pathlib import Path

ROOT = Path(__file__).parent
CACHE = Path(os.environ.get("FREQUENCY_CACHE") or ROOT / "cache")
CACHE.mkdir(parents=True, exist_ok=True)
