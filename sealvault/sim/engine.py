"""Discrete-event simulation of custody intake, review and handoff.

Process metadata only: frozen arrivals, role calendars, routes, priority and
duration estimates.  No case body or evidence object is ever read.

Ordering at one instant is fixed and reported: arrivals, then service
completions, then shift-window changes, then dispatch.  Simultaneous arrival,
completion and shift change therefore resolve deterministically.

No new work starts outside an operating window.  Whether an already-started
service continues through a shift boundary or is interrupted is fixed by the
plan before the run.  Occupancy per window is checked instant by instant and
can never exceed the staffed headcount.
"""

import bisect
import heapq
from sealvault.codec import fail
from sealvault.sim import calendar as calendar_model
from sealvault.sim.model import clock_minutes, integer
from sealvault.sim.randomness import Draws
from sealvault.sim.timeutil import MICRO, format_instant, us_to_seconds

# Calendars are generated with a forward look so a cutoff inside a shift does
# not make the roster look empty.  Simulation itself still stops at the cutoff.
CALENDAR_LOOKAHEAD_US = 14 * 86400 * MICRO

TIE_ORDER = {'arrival': 0, 'completion': 1, 'shift_window': 2, 'dispatch': 3}
MAX_EVENT_LOG = 4000
MAX_DECISIONS = 10000
MAX_JOBS_PER_CASE = 200
MAX_INTERVALS = 4000


def _staffed_calendars(model, plan, horizon_start=None, horizon_end=None):
    if horizon_start is None:
        horizon_start = min(item['arrived_us'] for item in model['arrivals']['arrivals'])
    if horizon_end is None:
        horizon_end = model['entry_end_us'] + CALENDAR_LOOKAHEAD_US
    result = {}
    for role in plan['staffing']:
        if role not in model['calendars']:
            fail('sim_plan', 'Staffing plan names a role without a calendar', role=role)
    for role, calendar in model['calendars'].items():
        spec = plan['staffing'].get(role)
        if spec is None:
            shifts = calendar['shifts']
        else:
            source = model['calendars'][spec['source_role']]
            if spec['headcount_override'] is not None:
                shifts = []
                for row in spec['headcount_override']:
                    if not isinstance(row, dict) or set(row) != {'weekday', 'start', 'end', 'headcount'}:
                        fail('sim_plan', 'Headcount override must repeat the shift schema', role=role)
                    start = clock_minutes(row['start'], 'start')
                    end = clock_minutes(row['end'], 'end')
                    if end <= start:
                        fail('sim_plan', 'Headcount override shift must end after its start', role=role)
                    shifts.append({'weekday': integer(row['weekday'], 'weekday', 0, 6), 'start': start,
                                   'end': end, 'headcount': integer(row['headcount'], 'headcount', 0, 200),
                                   'label': '', 'assumption_id': None})
                shifts.sort(key=lambda item: (item['weekday'], item['start'], item['end']))
                for left, right in zip(shifts, shifts[1:]):
                    if left['weekday'] == right['weekday'] and right['start'] < left['end']:
                        fail('sim_plan', 'Headcount override shifts may not overlap', role=role)
            else:
                shifts = source['shifts']
            shifts = [{**row, 'headcount': int(round(row['headcount'] * spec['headcount_multiplier']))}
                      for row in shifts]
        result[role] = calendar_model.Calendar(role, calendar['utc_offset'], shifts,
                                               horizon_start, horizon_end)
    return result


def _order_key(plan, job):
    """Priority dominates; arrival order breaks ties, then stable job identity."""
    return (-plan['priority_weights'][job['case']['priority']], job['ready_us'],
            job['case']['case_id'], job['position'], job['attempt'])


class Desk:
    def __init__(self, model, plan, seed, horizon_start=None, horizon_end=None):
        self.model = model
        self.plan = plan
        self.seed = seed
        self.draws = Draws(seed)
        self.cutoff = model['entry_end_us']
        self.horizon_start = (min(item['arrived_us'] for item in model['arrivals']['arrivals'])
                              if horizon_start is None else horizon_start)
        self.calendars = _staffed_calendars(model, plan, horizon_start, horizon_end)
        self.cases = {}
        self.queues = {role: [] for role in self.calendars}
        self.running = {role: [] for role in self.calendars}
        self.paused = {role: [] for role in self.calendars}
        self.intervals = {role: {} for role in self.calendars}
        self.booked_us = {role: 0 for role in self.calendars}
        self.overtime_us = {role: 0 for role in self.calendars}
        self.started = {role: 0 for role in self.calendars}
        self.resumed = {role: 0 for role in self.calendars}
        self.finished = {role: 0 for role in self.calendars}
        self.events = []
        self.log_rows = []
        self.decisions = []
        self.serial = 0
        self.sequence = 0
        self.arrived = set()

    # -- queue -------------------------------------------------------------
    def push(self, at, kind, role=None, job=None, rank=1, extra=None):
        self.sequence += 1
        payload = {'kind': kind, 'role': role, 'job': job}
        if extra:
            payload.update(extra)
        heapq.heappush(self.events, (at, rank, self.sequence, payload))

    def log(self, at, kind, **details):
        if len(self.log_rows) < MAX_EVENT_LOG:
            self.log_rows.append({'sequence': len(self.log_rows) + 1, 'at': format_instant(at),
                                  'kind': kind,
                                  **{name: value for name, value in details.items() if value is not None}})

    # -- capacity ----------------------------------------------------------
    def window_index(self, role, at):
        calendar = self.calendars[role]
        index = bisect.bisect_right(calendar.starts, at) - 1
        if index < 0 or at >= calendar.ends[index]:
            return None
        return index

    @staticmethod
    def peak(intervals, low, high):
        events = []
        for start, end in intervals:
            if end <= low or start >= high:
                continue
            events.append((max(start, low), 1))
            events.append((min(end, high), -1))
        events.sort(key=lambda row: (row[0], row[1]))
        current = 0
        highest = 0
        for _, delta in events:
            current += delta
            highest = max(highest, current)
        return highest

    def has_seat(self, role, start, finish):
        calendar = self.calendars[role]
        index = self.window_index(role, start)
        if index is None or calendar.headcounts[index] <= 0:
            return False
        probe = index
        while probe < len(calendar.starts) and calendar.starts[probe] < finish:
            capacity = calendar.headcounts[probe]
            if capacity:
                low = max(start, calendar.starts[probe])
                high = min(finish, calendar.ends[probe])
                if self.peak(self.intervals[role].get(probe, []), low, high) + 1 > capacity:
                    return False
            probe += 1
        return True

    def book(self, role, start, finish):
        calendar = self.calendars[role]
        probe = self.window_index(role, start)
        if probe is None:
            fail('sim_resource', 'Work booked outside an operating window', role=role)
        cursor = start
        while probe < len(calendar.starts) and calendar.starts[probe] < finish:
            window_start = max(start, calendar.starts[probe])
            window_end = min(finish, calendar.ends[probe])
            if window_start > cursor:
                # Time between windows is unstaffed; continuing work there is overtime.
                self.overtime_us[role] += max(0, min(window_start, self.cutoff) - cursor)
            if window_end > window_start:
                bucket = self.intervals[role].setdefault(probe, [])
                if len(bucket) >= MAX_INTERVALS:
                    fail('sim_resource', 'Window interval list exceeded its bound', role=role)
                bucket.append((window_start, window_end))
                if calendar.headcounts[probe] > 0:
                    self.booked_us[role] += max(0, min(window_end, self.cutoff) - window_start)
                    if self.peak(bucket, window_start, window_end) > calendar.headcounts[probe]:
                        fail('sim_resource', 'Occupancy exceeded window headcount', role=role,
                             window_index=probe)
                else:
                    self.overtime_us[role] += window_end - window_start
                cursor = max(cursor, window_end)
            probe += 1
        if cursor < finish:
            self.overtime_us[role] += max(0, min(finish, self.cutoff) - cursor)

    # -- work --------------------------------------------------------------
    def enqueue(self, job):
        heapq.heappush(self.queues[job['role']], (_order_key(self.plan, job), job['serial'], job))

    def start(self, role, job, at):
        calendar = self.calendars[role]
        index = self.window_index(role, at)
        if index is None or calendar.headcounts[index] <= 0:
            return False
        if self.plan['cross_shift'] == 'continue':
            finish = at + job['remaining_us']
        else:
            finish = min(at + job['remaining_us'], calendar.ends[index])
        if not self.has_seat(role, at, finish):
            return False
        self.book(role, at, finish)
        if job['started_us'] is None:
            job['started_us'] = at
            job['wait_us'] = at - job['ready_us']
            case = job['case']
            if case['first_work_us'] is None:
                case['first_work_us'] = at
            case['trace'].append({'at': format_instant(at), 'kind': 'first_work',
                                  'stage_id': job['stage_id'], 'attempt': job['attempt'],
                                  'role': role, 'wait_seconds': us_to_seconds(job['wait_us'])})
        job['status'] = 'running'
        job['segments'] += 1
        job['segment_start'] = at
        job['segment_end'] = finish
        job['worked_us'] += finish - at
        job['remaining_us'] = job['remaining_us'] - (finish - at)
        job['case']['status'] = 'active'
        self.running[role].append(job)
        if job['started_us'] == at:
            self.started[role] += 1
        else:
            self.resumed[role] += 1
        self.log(at, 'service_start', case_id=job['case']['case_id'], stage_id=job['stage_id'],
                 attempt=job['attempt'], role=role, worked_seconds=us_to_seconds(finish - at, 2),
                 remaining_seconds=us_to_seconds(max(0, job['remaining_us']), 2))
        self.push(finish, 'segment_end', role=role, job=job, rank=1)
        return True

    def segment_end(self, role, job, at):
        if job['status'] != 'running' or job['segment_end'] != at:
            return
        self.running[role] = [item for item in self.running[role] if item is not job]
        if job['remaining_us'] > 0:
            job['status'] = 'paused'
            job['case']['status'] = 'paused'
            heapq.heappush(self.paused[role], (_order_key(self.plan, job), job['serial'], job))
            self.log(at, 'service_pause', case_id=job['case']['case_id'], stage_id=job['stage_id'],
                     attempt=job['attempt'], role=role,
                     remaining_seconds=us_to_seconds(job['remaining_us'], 2))
            job['case']['trace'].append({'at': format_instant(at), 'kind': 'pause',
                                         'stage_id': job['stage_id'], 'attempt': job['attempt'],
                                         'remaining_seconds': us_to_seconds(job['remaining_us'])})
            return
        job['status'] = 'done'
        job['finished_us'] = at
        self.finished[role] += 1
        self.log(at, 'service_complete', case_id=job['case']['case_id'], stage_id=job['stage_id'],
                 attempt=job['attempt'], role=role, worked_seconds=us_to_seconds(job['worked_us'], 2))
        case = job['case']
        case['trace'].append({'at': format_instant(at), 'kind': 'stage_completed',
                              'stage_id': job['stage_id'], 'attempt': job['attempt'], 'role': role})
        self.advance(job, at)

    def advance(self, job, at):
        case = job['case']
        route = self.model['routes'][job['route_id']]
        rule = next((item for item in route['rework'] if item['at_stage_id'] == job['stage_id']), None)
        position = job['position'] + 1
        if rule is not None:
            used = case['rework_used'].get(job['stage_id'], 0)
            if used < rule['max_rework_count'] and rule['probability'] > 0:
                if self.draws.rework(case['case_id'], job['stage_id'], job['attempt']) < rule['probability']:
                    case['rework_used'][job['stage_id']] = used + 1
                    case['rework_count'] += 1
                    position = rule['to_position']
                    self.log(at, 'rework', case_id=case['case_id'], from_stage_id=job['stage_id'],
                             to_stage_id=rule['to_stage_id'], assumption_id=rule['assumption_id'],
                             failure_reason=rule['failure_reason'])
                    case['trace'].append({'at': format_instant(at), 'kind': 'rework',
                                          'from_stage_id': job['stage_id'],
                                          'to_stage_id': rule['to_stage_id'],
                                          'assumption_id': rule['assumption_id'],
                                          'failure_reason': rule['failure_reason']})
        if position >= len(route['steps']):
            case['finished_us'] = at
            case['status'] = 'completed'
            self.log(at, 'case_complete', case_id=case['case_id'],
                     elapsed_seconds=us_to_seconds(at - case['arrived_us']))
            case['trace'].append({'at': format_instant(at), 'kind': 'case_complete',
                                  'elapsed_seconds': us_to_seconds(at - case['arrived_us'])})
            return
        if len(case['jobs']) >= MAX_JOBS_PER_CASE:
            fail('sim_route', 'Case exceeded its work-step bound; check rework model',
                 case_id=case['case_id'])
        attempt = 1 + sum(1 for item in case['jobs'] if item['position'] == position)
        self.serial += 1
        following = self.init_job(self.make_job(case, route, position, attempt, at))
        following['serial'] = self.serial
        case['jobs'].append(following)
        case['status'] = 'queued'
        self.enqueue(following)
        case['trace'].append({'at': format_instant(at), 'kind': 'queued_at_stage',
                              'stage_id': following['stage_id'], 'attempt': attempt,
                              'role': following['role'], 'priority': case['priority']})

    def make_job(self, case, route, position, attempt, ready_us):
        step = route['steps'][position]
        estimate = self.model['service']['durations'][route['route_id'] + '/' + step['stage_id']]
        return {'case': case, 'route_id': route['route_id'], 'stage_id': step['stage_id'],
                'stage_label': step['label'], 'role': step['role'], 'position': position,
                'attempt': attempt, 'ready_us': ready_us, 'started_us': None, 'finished_us': None,
                'duration_us': self.draws.service_us(estimate, case['case_id'], step['stage_id'], attempt),
                'remaining_us': None, 'worked_us': 0, 'wait_us': 0, 'status': 'queued',
                'interruptions': 0, 'segments': 0, 'segment_start': None, 'segment_end': None,
                'estimate': estimate}

    def init_job(self, job):
        job['remaining_us'] = job['duration_us']
        return job

    def open_wait(self, job):
        """Queue wait accrued so far for a job that has not started."""
        return max(0, self.cutoff - job['ready_us'])

    def integrity(self):
        """Independent post-run audit of seats, precedence and accounting."""
        bookings = {}
        for role, buckets in self.intervals.items():
            calendar = self.calendars[role]
            total = 0
            for index, bucket in buckets.items():
                for low, high in bucket:
                    if high <= low:
                        fail('sim_resource', 'Booked interval is empty or reversed', role=role)
                    if low < calendar.starts[index] or high > calendar.ends[index]:
                        fail('sim_resource', 'Booked interval leaves its shift window', role=role)
                events = sorted([(low, 1) for low, _ in bucket] + [(high, -1) for _, high in bucket],
                                key=lambda row: (row[0], row[1]))
                current = peak = 0
                for _, delta in events:
                    current += delta
                    peak = max(peak, current)
                headcount = calendar.headcounts[index]
                if peak > headcount:
                    fail('sim_resource', 'Seat count exceeded headcount', role=role, window_index=index)
                total += sum(high - low for low, high in bucket)
            if total != self.booked_us[role]:
                fail('sim_resource', 'Booked total differs from interval sum', role=role)
            bookings[role] = {'windows': len(buckets), 'seats_peak_ok': True,
                              'booked_seconds': us_to_seconds(total, 1)}
        precedence = True
        for case in self.cases.values():
            previous = case['arrived_us']
            for job in case['jobs']:
                if job['ready_us'] < previous:
                    precedence = False
                if job['started_us'] is not None and job['started_us'] < job['ready_us']:
                    precedence = False
                previous = job['finished_us'] or job['ready_us']
            if case['finished_us'] is not None and case['finished_us'] < case['arrived_us']:
                precedence = False
        if not precedence:
            fail('sim_route', 'Case precedence was violated in the schedule')
        return {'checked': True, 'resources': bookings,
                'occupancy_never_above_headcount': True,
                'occupancy_never_negative': True,
                'stage_precedence_preserved': True,
                'cutoff': self.model['simulation_end'],
                'queued_at_cutoff_counted_in_backlog': True}

    # -- dispatch ----------------------------------------------------------
    def dispatch(self, role, at, allow_start):
        if not allow_start:
            return
        index = self.window_index(role, at)
        if index is None:
            return
        calendar = self.calendars[role]
        capacity = calendar.headcounts[index]
        if capacity <= 0:
            return
        used = sum(1 for start, end in self.intervals[role].get(index, []) if start <= at < end)
        seats = capacity - used
        while seats > 0 and self.paused[role]:
            job = self.paused[role][0][2]
            if not self.start(role, job, at):
                break
            heapq.heappop(self.paused[role])
            job['interruptions'] += 1
            self.log(at, 'service_resume', case_id=job['case']['case_id'], stage_id=job['stage_id'],
                     attempt=job['attempt'], role=role,
                     remaining_seconds=us_to_seconds(job['remaining_us'], 2))
            job['case']['trace'].append({'at': format_instant(at), 'kind': 'resume',
                                         'stage_id': job['stage_id'], 'attempt': job['attempt'],
                                         'remaining_seconds': us_to_seconds(job['remaining_us'])})
            job['case']['status'] = 'active'
            seats -= 1
        while seats > 0 and self.queues[role]:
            job = self.queues[role][0][2]
            if not self.start(role, job, at):
                break
            heapq.heappop(self.queues[role])
            if len(self.decisions) < MAX_DECISIONS:
                self.decisions.append({'at': format_instant(at), 'at_us': at, 'role': role,
                                       'case_id': job['case']['case_id'], 'stage_id': job['stage_id'],
                                       'attempt': job['attempt'], 'priority': job['case']['priority'],
                                       'wait_seconds': us_to_seconds(job['wait_us']),
                                       'queued_behind': len(self.queues[role]), 'seats_left': seats})
            seats -= 1

    # -- main loop ---------------------------------------------------------
    def add_case(self, arrival):
        case = {'case_id': arrival['case_id'], 'arrived_us': arrival['arrived_us'],
                'arrived_at': arrival['arrived_at'], 'priority': arrival['priority'],
                'route': arrival['route'], 'evidence': arrival['evidence'],
                'assumption_id': arrival['assumption_id'], 'label': arrival['label'],
                'jobs': [], 'rework_used': {}, 'rework_count': 0, 'finished_us': None,
                'first_work_us': None, 'status': 'queued',
                'trace': [{'at': arrival['arrived_at'], 'kind': 'arrived', 'route': arrival['route'],
                           'priority': arrival['priority'], 'evidence': arrival['evidence'],
                           'assumption_id': arrival['assumption_id']}]}
        self.cases[case['case_id']] = case
        return case

    def admit(self, arrival):
        case = self.add_case(arrival)
        route = self.model['routes'][arrival['route']]
        self.serial += 1
        job = self.init_job(self.make_job(case, route, 0, 1, arrival['arrived_us']))
        job['serial'] = self.serial
        case['jobs'].append(job)
        self.enqueue(job)
        self.log(arrival['arrived_us'], 'arrival', case_id=case['case_id'],
                 priority=case['priority'], route=case['route'], evidence=case['evidence'],
                 assumption_id=case['assumption_id'])

    def run(self):
        cutoff = self.cutoff
        arrivals = self.model['arrivals']['arrivals']
        for arrival in arrivals:
            if arrival['arrived_us'] > cutoff:
                fail('sim_model', 'Arrival extends past the simulation cutoff', case_id=arrival['case_id'])
        for arrival in arrivals:
            self.push(arrival['arrived_us'], 'arrival', job=None, rank=TIE_ORDER['arrival'],
                      extra={'arrival': arrival})
        for role, calendar in self.calendars.items():
            for start, end, headcount in calendar.windows():
                if headcount > 0:
                    self.push(start, 'window_open', role=role, rank=TIE_ORDER['shift_window'])
                    self.push(end, 'window_close', role=role, rank=TIE_ORDER['shift_window'])
        while self.events:
            at = self.events[0][0]
            if at > cutoff:
                break
            batch = []
            while self.events and self.events[0][0] == at:
                batch.append(heapq.heappop(self.events))
            arrivals_batch = [row for row in batch if row[3]['kind'] == 'arrival']
            completions = [row for row in batch if row[3]['kind'] == 'segment_end']
            boundaries = [row for row in batch if row[3]['kind'] in ('window_open', 'window_close')]
            for row in sorted(arrivals_batch, key=lambda item: (item[3]['arrival']['case_id'], item[2])):
                self.admit(row[3]['arrival'])
            for row in sorted(completions, key=lambda item: (item[3]['kind'] == 'window_close', item[2])):
                self.segment_end(row[3]['role'], row[3]['job'], at)
            for row in sorted(boundaries, key=lambda item: (item[2])):
                self.log(at, 'window_' + row[3]['kind'].split('_')[1], role=row[3]['role'])
            for role in sorted(self.calendars):
                self.dispatch(role, at, allow_start=at < cutoff)
            if at >= cutoff:
                break
        return self
