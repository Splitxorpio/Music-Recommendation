"""The single-user pipeline's in-process window scheduler (net.py) — regression for the clean-up bug that
let bookings made several windows ahead erase the open window's count."""
from collections import Counter

import net


def test_booking_ahead_never_overfills_a_window():
    sv = net._Service("t_aligned", rate=None, concurrency=2, timeout=1, window=(3, 2.0),
                      aligned={"phase": 0.0, "after_reset": 0.1, "end_guard": 0.3})
    sv.win_counts = {}
    times = [sv._aligned_turn(1.0) for _ in range(15)]   # books 5 windows ahead without sending
    per = Counter(int(t // 2.0) for t in times)
    assert max(per.values()) == 3 and len(per) == 5, per
