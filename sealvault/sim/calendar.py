"""Shift windows generated from weekday rules in a fixed UTC offset.

Every window starts and ends on the same local day, so overnight coverage is
expressed as two adjacent local shifts on adjacent weekdays.  A window
transition is an explicit event: no new task starts outside a window and a
service that reaches a transition either continues across the boundary or is
interrupted, according to the plan fixed before the run.
"""

import bisect
from sealvault.sim.timeutil import MICRO, epoch_day

DAY = 86400 * MICRO


class Calendar:
    def __init__(self, role, offset_seconds, shifts, horizon_start, horizon_end):
        self.role = role
        self.offset = offset_seconds * MICRO
        self.horizon_start = horizon_start
        self.horizon_end = horizon_end
        self.starts = []
        self.ends = []
        self.headcounts = []

        first = epoch_day(horizon_start + self.offset)
        last = epoch_day(horizon_end + self.offset)
        for day in range(first, last + 1):
            weekday = (day + 3) % 7  # epoch day zero is a Thursday, so Monday maps to 0
            for shift in shifts:
                if shift['weekday'] != weekday:
                    continue
                start = day * DAY + shift['start'] * 60 * MICRO - self.offset
                end = day * DAY + shift['end'] * 60 * MICRO - self.offset
                if end <= start or end <= horizon_start or start >= horizon_end:
                    continue
                self.starts.append(max(start, horizon_start))
                self.ends.append(min(end, horizon_end))
                self.headcounts.append(shift['headcount'])
        order = sorted(range(len(self.starts)), key=lambda index: (self.starts[index], self.ends[index]))
        self.starts = [self.starts[index] for index in order]
        self.ends = [self.ends[index] for index in order]
        self.headcounts = [self.headcounts[index] for index in order]

    def headcount(self, at):
        index = bisect.bisect_right(self.starts, at) - 1
        if index < 0 or at >= self.ends[index]:
            return 0
        return self.headcounts[index]

    def window_end(self, at):
        index = bisect.bisect_right(self.starts, at) - 1
        if index < 0 or at >= self.ends[index]:
            return None
        return self.ends[index]

    def next_start(self, at):
        index = bisect.bisect_right(self.starts, at)
        return self.starts[index] if index < len(self.starts) else None

    def windows(self):
        return list(zip(self.starts, self.ends, self.headcounts))

    def scheduled_us(self, low, high):
        """Staffed headcount-microseconds restricted to [low, high)."""
        total = 0
        for start, end, count in self.windows():
            if end <= low or start >= high:
                continue
            total += (min(end, high) - max(start, low)) * count
        return total


def build(calendars, horizon_start, horizon_end):
    return {role: Calendar(role, value['utc_offset'], value['shifts'], horizon_start, horizon_end)
            for role, value in calendars.items()}
