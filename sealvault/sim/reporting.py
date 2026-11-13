"""Report construction, plan comparison and case-level explanation."""

import math
from sealvault.codec import fail
from sealvault.sim.engine import MAX_EVENT_LOG
from sealvault.sim.model import PRIORITIES
from sealvault.sim.timeutil import MICRO, format_instant, us_to_seconds

Z95 = 1.959963984540054
METRIC_LABELS = ('service_target_violation_rate', 'backlog_rate', 'mean_completion_seconds',
                 'mean_queue_wait_seconds', 'total_staff_cost', 'cost_per_completed_case')
T_CRITICAL_95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
                 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
                 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
                 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
                 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


def _critical(samples):
    if samples >= 31:
        return Z95
    return T_CRITICAL_95.get(samples, Z95)


def _clean(value):
    return round(value, 6)


def _interval(values):
    count = len(values)
    if not count:
        return None
    mean = sum(values) / count
    if count == 1:
        return {'mean': _clean(mean), 'standard_error': None, 'interval_95': None, 'samples': 1}
    variance = sum((value - mean) ** 2 for value in values) / (count - 1)
    standard_error = math.sqrt(variance / count)
    half = _critical(count) * standard_error
    return {'mean': _clean(mean), 'standard_error': _clean(standard_error),
            'interval_95': [_clean(mean - half), _clean(mean + half)], 'samples': count,
            'degenerate': standard_error < 1e-9}


def _horizon_windows(calendar, low, high):
    """Windows clipped to the analysis horizon, so a forward calendar lookahead
    does not inflate scheduled time or person-shift counts."""
    rows = []
    for start, end, count in calendar.windows():
        if end <= low or start >= high:
            continue
        rows.append((max(start, low), min(end, high), count))
    return rows


def role_utilization(desk):
    rows = {}
    for role, calendar in desk.calendars.items():
        windows = _horizon_windows(calendar, desk.horizon_start, desk.cutoff)
        scheduled = sum((end - start) * count for start, end, count in windows)
        booked = min(desk.booked_us[role], scheduled)
        if booked < 0 or booked > scheduled:
            fail('sim_resource', 'Booked time outside [0, scheduled] for role', role=role,
                 booked=booked, scheduled=scheduled)
        rows[role] = {
            'scheduled_headcount_seconds': us_to_seconds(scheduled, 1),
            'occupied_headcount_seconds': us_to_seconds(booked, 1),
            'utilization': round(booked / scheduled, 4) if scheduled else 0.0,
            'person_shifts': sum(count for _, _, count in windows),
            'staffed_windows': sum(1 for _, _, count in windows if count > 0),
            'horizon': {'start': format_instant(desk.horizon_start),
                        'end': format_instant(desk.cutoff)},
            'services_started': desk.started[role],
            'services_resumed': desk.resumed[role],
            'services_completed': desk.finished[role],
            'overtime_seconds': us_to_seconds(desk.overtime_us[role], 1),
            'queued_at_cutoff': len(desk.queues[role]),
            'paused_at_cutoff': len(desk.paused[role]),
            'headcount_at_cutoff': calendar.headcount(desk.cutoff),
            'next_window_start': (format_instant(calendar.next_start(desk.cutoff))
                                  if calendar.next_start(desk.cutoff) is not None else None),
        }
    return rows


def staff_cost(plan, utilization):
    if isinstance(plan['staff_cost_per_shift'], dict):
        rate = {role: plan['staff_cost_per_shift'].get(role, 1.0) for role in utilization}
    else:
        rate = {role: plan['staff_cost_per_shift'] for role in utilization}
    roles = {role: {'person_shifts': row['person_shifts'], 'rate': rate[role],
                    'cost': row['person_shifts'] * rate[role]} for role, row in utilization.items()}
    return {'roles': roles, 'total': sum(row['cost'] for row in roles.values()),
            'basis': 'per_person_shift_not_per_worked_hour'}


def case_rows(desk, plan):
    rows = []
    for case in sorted(desk.cases.values(), key=lambda item: item['case_id']):
        target = plan['service_target_seconds'][case['priority']]
        waits = [job['wait_us'] for job in case['jobs'] if job['started_us'] is not None]
        open_waits = [desk.open_wait(job) for job in case['jobs'] if job['started_us'] is None]
        completion = None if case['finished_us'] is None else case['finished_us'] - case['arrived_us']
        rows.append({
            'case_id': case['case_id'], 'priority': case['priority'], 'route': case['route'],
            'evidence': case['evidence'], 'assumption_id': case['assumption_id'],
            'arrived_at': case['arrived_at'],
            'first_work_at': format_instant(case['first_work_us']) if case['first_work_us'] is not None else None,
            'arrival_to_first_work_seconds': (None if case['first_work_us'] is None
                                              else us_to_seconds(case['first_work_us'] - case['arrived_us'])),
            'completion_seconds': None if completion is None else us_to_seconds(completion),
            'queue_wait_seconds': us_to_seconds(sum(waits), 1) if waits else 0.0,
            'censored_wait_seconds': (us_to_seconds(sum(open_waits), 1) if open_waits else 0.0),
            'working_seconds': us_to_seconds(sum(job['worked_us'] for job in case['jobs']), 1),
            'rework_count': case['rework_count'],
            'status': 'completed' if completion is not None else 'censored_at_cutoff',
            'censored': completion is None,
            'service_target_seconds': target,
            'target_violated': completion is None or completion > target * MICRO,
            'stages': [{'stage_id': job['stage_id'], 'role': job['role'], 'attempt': job['attempt'],
                        'ready_at': format_instant(job['ready_us']),
                        'started_at': format_instant(job['started_us']) if job['started_us'] is not None else None,
                        'finished_at': format_instant(job['finished_us']) if job['finished_us'] is not None else None,
                        'status': job['status'],
                        'queue_wait_seconds': us_to_seconds(job['wait_us']) if job['started_us'] is not None else None,
                        'worked_seconds': us_to_seconds(job['worked_us'], 1),
                        'segments': job['segments'],
                        'pauses': job['interruptions']}
                       for job in case['jobs']],
            'trace': case['trace'],
        })
    return rows


def summary(desk, plan, cases):
    total = len(cases)
    completed = [row for row in cases if not row['censored']]
    censored = [row for row in cases if row['censored']]
    by_priority = {name: {'arrivals': 0, 'completed': 0, 'violations': 0, 'censored': 0}
                   for name in PRIORITIES}
    violations = 0
    for row in cases:
        bucket = by_priority[row['priority']]
        bucket['arrivals'] += 1
        if row['censored']:
            bucket['censored'] += 1
            bucket['violations'] += 1
            violations += 1
        else:
            bucket['completed'] += 1
            if row['target_violated']:
                bucket['violations'] += 1
                violations += 1
    for bucket in by_priority.values():
        bucket['violation_rate'] = round(bucket['violations'] / max(1, bucket['arrivals']), 4)
        bucket['completion_rate'] = round(bucket['completed'] / max(1, bucket['arrivals']), 4)
    utilization = role_utilization(desk)
    cost = staff_cost(plan, utilization)
    budget = desk.model.get('personnel_budget')
    person_shifts = sum(row['person_shifts'] for row in utilization.values())
    budget_row = None
    if budget is not None:
        within = person_shifts <= budget['person_shifts']
        if budget.get('cost_ceiling') is not None and cost['total'] > budget['cost_ceiling']:
            within = False
        budget_row = {
            'planned_person_shifts': person_shifts,
            'budget_person_shifts': budget['person_shifts'],
            'cost_ceiling': budget.get('cost_ceiling'),
            'within_budget': within,
            'budget_assumption_id': budget.get('assumption_id'),
            'basis': 'person_shifts_total_equals_sum_of_role_person_shifts',
        }
    completed_rows = completed
    mean_completion = (sum(row['completion_seconds'] for row in completed_rows) / len(completed_rows)
                       if completed_rows else None)
    return {
        'arrivals': total,
        'completed': len(completed),
        'censored_at_cutoff': len(censored),
        'backlog_count': len(censored),
        'backlog_denominator': total,
        'backlog_rate': round(len(censored) / max(1, total), 4),
        'backlog_definition': 'unfinished cases at cutoff divided by all arrivals, including queued and paused',
        'service_target_violations': violations,
        'service_target_violation_rate': round(violations / max(1, total), 4),
        'violation_rule': 'case elapsed time above target; censored cases count as violations',
        'by_priority': by_priority,
        'mean_completion_seconds': None if mean_completion is None else round(mean_completion, 2),
        'mean_completion_censored_excluded': True,
        'censoring_note': ('Completion mean uses completed cases only; censored cases are not imputed and '
                           'are carried in backlog and violation counts.'),
        'mean_queue_wait_seconds': round(sum(row['queue_wait_seconds'] for row in cases) / max(1, total), 2),
        'mean_censored_wait_seconds': round(sum(row['censored_wait_seconds'] for row in cases) / max(1, total), 2),
        'censored_wait_note': ('Cases still queued or paused at the cutoff accrue waiting up to the cutoff; '
                               'their waiting is reported separately and never mixed into service time.'),
        'utilization': utilization,
        'minimum_utilization': min((row['utilization'] for row in utilization.values()), default=0.0),
        'mean_utilization': round(sum(row['utilization'] for row in utilization.values()) /
                                  max(1, len(utilization)), 4),
        'person_shifts': person_shifts,
        'personnel_budget': budget_row,
        'total_staff_cost': round(cost['total'], 4),
        'cost_breakdown': cost,
        'cost_per_completed_case': round(cost['total'] / len(completed), 4) if completed else None,
        'queue_at_cutoff': {role: {'queued': row['queued_at_cutoff'], 'paused': row['paused_at_cutoff'],
                                    'headcount_at_cutoff': row['headcount_at_cutoff'],
                                    'next_window_start': row['next_window_start']}
                            for role, row in utilization.items()},
        'evidence_content_read': False,
    }


def report(desk):
    plan = desk.plan
    model = desk.model
    cases = case_rows(desk, plan)
    coverage = model['service']['coverage']
    rows = summary(desk, plan, cases)
    return {
        'format': 'sealvault-simulation-report', 'version': 1,
        'mode': model['mode'],
        'plan': {'plan_id': plan['plan_id'], 'label': plan['label'], 'description': plan['description'],
                 'cross_shift': plan['cross_shift'],
                 'priority_weights': plan['priority_weights'],
                 'service_target_seconds': plan['service_target_seconds'],
                 'staffing': plan['staffing'],
                 'staff_cost_per_shift': plan['staff_cost_per_shift']},
        'model': {'digest': model['digest'], 'source': model['source'], 'mode': model['mode'],
                  'notes': model['notes'],
                  'simulation_end': model['simulation_end'],
                  'arrival_trace': {'digest': model['arrivals'].get('digest'),
                                    'count': len(model['arrivals']['arrivals']),
                                    'evidence_states': sorted({item['evidence']
                                                               for item in model['arrivals']['arrivals']}),
                                    'window': model['arrivals'].get('window')},
                  'calendars': {role: {'utc_offset': value['utc_offset'],
                                       'shifts': value['shifts'],
                                       'label': value['label'],
                                       'assumption_id': value['assumption_id']}
                                for role, value in model['calendars'].items()},
                  'routes': {route_id: {'steps': value['steps'], 'rework': value['rework'],
                                        'description': value['description']}
                             for route_id, value in model['routes'].items()},
                  'service': {'coverage': coverage,
                              'durations': model['service']['durations'],
                              'duration_rule': 'paired_start_end or explicit_assumption only',
                              'wait_excluded_from_service_time': True,
                              'censored_operations': len(coverage['open_operations'])}},
        'simulation': {'seed': desk.seed, 'random': desk.draws.signature(),
                       'tie_order': {'arrival': 0, 'completion': 1, 'shift_window': 2, 'dispatch': 3},
                       'event_log': desk.log_rows,
                       'event_log_truncated': len(desk.log_rows) >= MAX_EVENT_LOG,
                       'decisions': desk.decisions,
                       'horizon': {'start': format_instant(min(item['arrived_us'] for item in model['arrivals']['arrivals'])),
                                   'end': model['simulation_end']}},
        'calendars': {role: {'utc_offset': calendar.offset // MICRO,
                             'windows': [{'start': format_instant(start), 'end': format_instant(end),
                                          'headcount': count}
                                         for start, end, count in _horizon_windows(calendar,
                                                                                   desk.horizon_start,
                                                                                   desk.cutoff)]}
                      for role, calendar in desk.calendars.items()},
        'summary': rows,
        'cases': cases,
        'schedule_integrity': desk.integrity(),
    }


def explain(desk, case_id):
    report_data = report(desk)
    row = next((item for item in report_data['cases'] if item['case_id'] == case_id), None)
    if row is None:
        fail('sim_case', 'Case is absent from the simulated arrival trace', case_id=case_id)
    case = desk.cases[case_id]
    stage_rows = []
    blocking = []
    for job in case['jobs']:
        role = job['role']
        calendar = desk.calendars[role]
        entry = {'stage_id': job['stage_id'], 'role': role, 'attempt': job['attempt'],
                 'status': job['status'], 'ready_at': format_instant(job['ready_us']),
                 'queue_wait_seconds': us_to_seconds(job['wait_us']) if job['started_us'] is not None else None,
                 'worked_seconds': us_to_seconds(job['worked_us'], 1),
                 'segments': job['segments'],
                 'headcount_at_ready': calendar.headcount(job['ready_us']),
                 'next_window_after_ready': (format_instant(calendar.next_start(job['ready_us']))
                                             if calendar.next_start(job['ready_us']) is not None else None)}
        stage_rows.append(entry)
        if job['started_us'] is None:
            window = None
            now = job['ready_us']
            starts = [start for start in calendar.starts if start >= now]
            if starts:
                window = format_instant(starts[0])
            ahead = [item for item in desk.decisions
                     if item['role'] == role and item['at_us'] >= job['ready_us']
                     and item['case_id'] != case_id]
            stronger = [item for item in ahead
                        if desk.plan['priority_weights'][item['priority']]
                        >= desk.plan['priority_weights'][case['priority']]]
            blocking.append({
                'stage_id': job['stage_id'], 'role': role,
                'reason': 'never_started_before_cutoff',
                'queued_from': format_instant(job['ready_us']),
                'role_headcount_at_ready': calendar.headcount(job['ready_us']),
                'next_window_start': window,
                'services_admitted_while_waiting': len(ahead),
                'admitted_with_equal_or_higher_priority': len(stronger),
                'admitted_case_ids': [item['case_id'] for item in ahead[:10]],
                'role_headcount_at_cutoff': calendar.headcount(desk.cutoff),
                'role_next_window_at_cutoff': (format_instant(calendar.next_start(desk.cutoff))
                                               if calendar.next_start(desk.cutoff) is not None else None),
                'queued_behind_at_cutoff': len(desk.queues[role]),
            })
    return {
        'format': 'sealvault-simulation-explain', 'version': 1,
        'case_id': case_id, 'plan_id': desk.plan['plan_id'], 'seed': desk.seed,
        'status': row['status'], 'arrived_at': row['arrived_at'],
        'arrival_to_first_work_seconds': row['arrival_to_first_work_seconds'],
        'completion_seconds': row['completion_seconds'],
        'service_target_seconds': row['service_target_seconds'],
        'target_violated': row['target_violated'],
        'rework_count': row['rework_count'],
        'evidence': row['evidence'],
        'stages': stage_rows,
        'trace': row['trace'],
        'blocks': blocking,
        'note': ('A stage left queued at cutoff is scheduled only after a window with free headcount opens; '
                 'the simulation never starts work outside a shift or without a seat.'),
    }


def metric_summary(report_data):
    rows = report_data['summary']
    return {name: rows.get(name) for name in ('service_target_violation_rate', 'backlog_rate',
                                              'mean_completion_seconds', 'mean_queue_wait_seconds',
                                              'total_staff_cost', 'cost_per_completed_case',
                                              'minimum_utilization', 'mean_utilization')}


def compare(reports, basis_plan_id):
    """Paired comparison across repeated runs, shared arrivals and shared draws."""
    if not reports:
        fail('sim_compare', 'Comparison requires at least one repeated run')
    plan_ids = sorted({item['plan_id'] for item in reports})
    if basis_plan_id not in plan_ids:
        fail('sim_compare', 'Baseline plan missing from reports', plan_id=basis_plan_id)
    by_plan = {plan_id: [] for plan_id in plan_ids}
    for item in reports:
        by_plan[item['plan_id']].append(item)
    base = by_plan[basis_plan_id]
    result_plans = {}
    for plan_id, rows in by_plan.items():
        metrics = {}
        for name in METRIC_LABELS + ('minimum_utilization', 'mean_utilization', 'person_shifts'):
            metrics[name] = _interval([row['metrics'][name] for row in rows
                                       if row['metrics'].get(name) is not None])
        budgets = [row['metrics'].get('personnel_budget') for row in rows
                   if row['metrics'].get('personnel_budget') is not None]
        result_plans[plan_id] = {'runs': len(rows), 'metrics': metrics,
                                 'labels': rows[0]['label'] if rows else plan_id,
                                 'within_budget_all_runs': (all(item['within_budget'] for item in budgets)
                                                            if budgets else None),
                                 'within_budget_runs': (sum(item['within_budget'] for item in budgets)
                                                        if budgets else None),
                                 'budget_runs': len(budgets)}
    comparisons = {}
    for plan_id, rows in by_plan.items():
        if plan_id == basis_plan_id:
            continue
        paired = {}
        for name in METRIC_LABELS + ('minimum_utilization', 'mean_utilization', 'person_shifts'):
            differences = []
            for index, row in enumerate(rows):
                base_value = base[index]['metrics'].get(name)
                value = row['metrics'].get(name)
                if value is None or base_value is None:
                    continue
                differences.append(value - base_value)
            interval = _interval(differences)
            if interval is not None:
                baseline_values = [base[index]['metrics'].get(name) for index in range(len(base))]
                baseline_values = [value for value in baseline_values if value is not None]
                baseline_mean = sum(baseline_values) / len(baseline_values) if baseline_values else None
                interval['baseline_mean'] = baseline_mean
                interval['relative_change'] = (round(interval['mean'] / baseline_mean, 4)
                                               if baseline_mean not in (None, 0) else None)
            paired[name] = interval
        comparisons[plan_id] = {'basis_plan_id': basis_plan_id, 'paired_differences': paired}
    return {'format': 'sealvault-simulation-comparison', 'version': 1,
            'basis_plan_id': basis_plan_id, 'repetitions': len(base),
            'identical_arrival_path': True,
            'identical_random_draws_per_repetition': True,
            'plans': result_plans, 'comparisons': comparisons,
            'note': ('Each repetition draws service and rework values from the same keyed stream for the '
                     'same case, stage and attempt across plans; only staffing differs. Intervals are '
                     'Monte Carlo confidence intervals, not guarantees about unseen demand.')}
