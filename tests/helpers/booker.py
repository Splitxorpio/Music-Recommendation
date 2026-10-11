"""Run in a separate process by the tests: book N slots in a quota bucket, print the granted send times."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from frequency import db, quota  # noqa: E402

db.use(os.environ["DATABASE_URL"])
bucket, n, cost = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
print(json.dumps([quota.acquire(bucket, cost, max_wait=600) for _ in range(n)]))
