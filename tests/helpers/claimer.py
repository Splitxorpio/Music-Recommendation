"""Run in a separate process by the tests: claim and finish jobs until none are left; print what it did."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from frequency import db, jobs  # noqa: E402

db.use(os.environ["DATABASE_URL"])
done = []
idle_since = time.time()
while time.time() - idle_since < 1.5:
    j = jobs.claim(["work"], lease_s=30)
    if not j:
        time.sleep(0.02)
        continue
    idle_since = time.time()
    time.sleep(0.005)
    jobs.finish(j, result={"by": os.getpid()})
    done.append(j["id"])
print(json.dumps(done))
