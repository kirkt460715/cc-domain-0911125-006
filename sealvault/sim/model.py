"""Validation of the metadata-only simulation model.

The model describes an arrival trace, role calendars, work routes, queue
priorities and service duration estimates.  Evidence bytes never enter the
model: a payload-looking key is rejected before any other processing.  Service
durations are accepted only when they are backed by complete paired start/end
observations or by an explicit assumption; unfinished operations are carried
as censored rows rather than silently dropped.
"""

import math
import re
from sealvault.codec import digest, fail, fields, integer, key, text, timestamp

PRIORITIES = ('urgent', 'standard', 'deferred')
MODES = ('historical_replay', 'future_scenario')
CROSS_SHIFT = ('continue', 'interrupt')
PAIRED = 'paired_start_end'
ASSUMED = 'explicit_assumption'
CONTENT_KEYS = {'content', 'base64', 'bytes', 'blob', 'payload', 'evidence_bytes', 'object_content'}
MAX_ROLES = 8
MAX_ROUTES = 50
MAX_STEPS = 12
MAX_REWORK = 12
MAX_ARRIVALS = 5000
MAX_PLANS = 50
MAX_CALENDAR_SHIFTS = 200


def number(value, name, low=0.0, high=None, exclusive_low=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        fail('sim_number', 'Expected finite numeric process value', field=name)
    if (value <= low) if exclusive_low else (value < low):
        fail('sim_number', 'Numeric value outside supported range', field=name, minimum=low, exclusive=exclusive_low)
    if high is not None and value > high:
        fail('sim_number', 'Numeric value above supported range', field=name, maximum=high)
    return value


def forbid_content(value, where='input'):
    if isinstance(value, dict):
        for name, item in value.items():
            if isinstance(name, str) and name.lower() in CONTENT_KEYS:
                fail('sim_evidence_access', 'Simulation accepts process metadata only', field=where + '.' + name)
            forbid_content(item, where + '.' + name)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            forbid_content(item, where + '[' + str(index) + ']')


def utc_offset(value):
    if not isinstance(value, str) or not re.fullmatch(r'[+-]?[0-2][0-9]:[0-5][0-9]', value):
        fail('sim_calendar', 'UTC offset must be HH:MM with an optional sign')
    sign = -1 if value.startswith('-') else 1
    core = value.lstrip('+-')
    hours, minutes = (int(part) for part in core.split(':'))
    if hours > 14 or minutes >= 60:
        fail('sim_calendar', 'UTC offset exceeds supported range')
    return sign * (hours * 3600 + minutes * 60)


def clock_minutes(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[0-2][0-9]:[0-5][0-9]', value):
        fail('sim_calendar', 'Shift boundary must be local HH:MM', field=name)
    hours, minutes = (int(part) for part in value.split(':'))
    if hours > 24 or (hours == 24 and minutes):
        fail('sim_calendar', 'Shift boundary outside one day', field=name)
    return hours * 60 + minutes


def calendar(value, role):
    fields(value, {'utc_offset', 'shifts'}, {'label', 'calendar_id', 'assumption_id'})
    if 'calendar_id' in value and value['calendar_id'] != role:
        fail('sim_calendar', 'Calendar identifier differs from its role key', role=role)
    if 'label' in value:
        text(value['label'], 100)
    if 'assumption_id' in value:
        key(value['assumption_id'])
    offset = utc_offset(value['utc_offset'])
    shifts = value['shifts']
    if not isinstance(shifts, list) or not 1 <= len(shifts) <= MAX_CALENDAR_SHIFTS:
        fail('sim_calendar', 'Role calendar requires a bounded shift list', role=role)
    normalized = []
    for index, shift in enumerate(shifts):
        fields(shift, {'weekday', 'start', 'end', 'headcount'}, {'label', 'assumption_id'})
        integer(shift['weekday'], 'weekday', 0, 6)
        start = clock_minutes(shift['start'], 'start')
        end = clock_minutes(shift['end'], 'end')
        if end <= start:
            fail('sim_calendar', 'Shift must end after its start on the same local day', role=role, index=index)
        headcount = integer(shift['headcount'], 'headcount', 0, 200)
        if 'label' in shift:
            text(shift['label'], 100)
        normalized.append({'weekday': shift['weekday'], 'start': start, 'end': end,
                           'headcount': headcount, 'label': shift.get('label', ''),
                           'assumption_id': shift.get('assumption_id')})
    normalized.sort(key=lambda row: (row['weekday'], row['start'], row['end']))
    for left, right in zip(normalized, normalized[1:]):
        if left['weekday'] == right['weekday'] and right['start'] < left['end']:
            fail('sim_calendar', 'Role shifts may not overlap within one weekday', role=role, weekday=left['weekday'])
    return {'role': role, 'utc_offset': offset, 'shifts': normalized,
            'label': value.get('label', ''), 'assumption_id': value.get('assumption_id')}


def calendars(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= MAX_ROLES:
        fail('sim_calendar', 'Role calendars must be an object of at most eight roles')
    result = {}
    for role, item in value.items():
        key(role)
        result[role] = calendar(item, role)
    return result


def service_estimate(value, name):
    fields(value, {'kind', 'source'}, {'seconds', 'median_seconds', 'sigma', 'sample_count', 'assumption_id'})
    if value['kind'] not in ('fixed', 'lognormal'):
        fail('sim_service', 'Duration kind must be fixed or lognormal', field=name)
    if value['source'] not in (PAIRED, ASSUMED):
        fail('sim_service', 'Duration must come from a complete start/end pair or an explicit assumption', field=name)
    if value['source'] == ASSUMED and 'assumption_id' not in value:
        fail('sim_service', 'Explicit duration assumption requires an identifier', field=name)
    if value['source'] == PAIRED and 'sample_count' not in value:
        fail('sim_service', 'Paired duration requires the observed pair count', field=name)
    if 'assumption_id' in value:
        key(value['assumption_id'])
    if 'sample_count' in value:
        integer(value['sample_count'], 'sample_count', 1, 1000000)
    if value['kind'] == 'fixed':
        if 'seconds' not in value:
            fail('sim_service', 'Fixed duration requires seconds', field=name)
        if 'median_seconds' in value or 'sigma' in value:
            fail('sim_service', 'Fixed duration cannot carry distribution parameters', field=name)
        seconds = number(value['seconds'], name + '.seconds', 0.0, 86400.0, exclusive_low=True)
        return {'kind': 'fixed', 'seconds': seconds, 'source': value['source'],
                'median_seconds': seconds, 'sigma': 0.0,
                'sample_count': value.get('sample_count'), 'assumption_id': value.get('assumption_id')}
    if 'median_seconds' not in value or 'sigma' not in value:
        fail('sim_service', 'Lognormal duration requires median_seconds and sigma', field=name)
    if 'seconds' in value:
        fail('sim_service', 'Lognormal duration cannot also declare fixed seconds', field=name)
    median = number(value['median_seconds'], name + '.median_seconds', 0.0, 86400.0, exclusive_low=True)
    sigma = number(value['sigma'], name + '.sigma', 0.0, 2.0)
    return {'kind': 'lognormal', 'seconds': median, 'source': value['source'],
            'median_seconds': median, 'sigma': sigma,
            'sample_count': value.get('sample_count'), 'assumption_id': value.get('assumption_id')}


def _pair_stats(rows, routes, open_ids):
    if not isinstance(rows, list) or len(rows) > 5000:
        fail('sim_service', 'Complete operation pairs must be a bounded list')
    values = {}
    seen = set()
    for row in rows:
        fields(row, {'operation_id', 'route_id', 'stage_id', 'started_at', 'finished_at'})
        key(row['operation_id'])
        key(row['route_id'])
        key(row['stage_id'])
        if row['operation_id'] in seen:
            fail('sim_service', 'Operation pair identifier repeats', operation_id=row['operation_id'])
        seen.add(row['operation_id'])
        if row['operation_id'] in open_ids:
            fail('sim_service', 'An open operation cannot also appear as a complete pair',
                 operation_id=row['operation_id'])
        if row['route_id'] not in routes:
            fail('sim_service', 'Pair names an undefined route', route=row['route_id'])
        if row['stage_id'] not in {step['stage_id'] for step in routes[row['route_id']]['steps']}:
            fail('sim_service', 'Pair names an undefined stage', stage_id=row['stage_id'])
        start = timestamp(row['started_at'])
        end = timestamp(row['finished_at'])
        duration = instant(end) - instant(start)
        if duration <= 0 or duration > 86400 * 1000000:
            fail('sim_service', 'Complete pair must have positive duration under one day',
                 operation_id=row['operation_id'])
        values.setdefault((row['route_id'], row['stage_id']), []).append(duration / 1000000.0)
    groups = {}
    for compound, items in values.items():
        ordered = sorted(items)
        count = len(ordered)
        median = ordered[count // 2] if count % 2 else (ordered[count // 2 - 1] + ordered[count // 2]) / 2.0
        if count > 1 and all(item > 0 for item in ordered):
            logs = [math.log(item) for item in ordered]
            mean = sum(logs) / count
            variance = sum((value - mean) ** 2 for value in logs) / (count - 1)
            sigma = math.sqrt(variance)
        else:
            sigma = 0.0
        groups[compound] = {'count': count, 'median_seconds': round(median, 6),
                            'log_sigma': round(sigma, 6)}
    return {'total': sum(len(items) for items in values.values()), 'groups': groups}


def service(value, routes):
    fields(value, {'format', 'version', 'durations', 'coverage'})
    if value['format'] != 'sealvault-simulation-service' or value['version'] != 1:
        fail('sim_service', 'Unsupported service model format')
    coverage = value['coverage']
    fields(coverage, {'observed_pairs', 'open_operations', 'excluded_wait_seconds'}, {'note', 'pairs'})
    integer(coverage['observed_pairs'], 'observed_pairs', 0, 1000000)
    number(coverage['excluded_wait_seconds'], 'excluded_wait_seconds', 0.0, 100000000.0)
    openings = coverage['open_operations']
    if not isinstance(openings, list) or len(openings) > 5000:
        fail('sim_service', 'Open operation log must be a bounded list')
    censored = []
    open_ids = set()
    open_keys = set()
    for row in openings:
        fields(row, {'operation_id', 'stage_id', 'started_at'}, {'closed_at_not_observed', 'route_id'})
        key(row['operation_id'])
        key(row['stage_id'])
        timestamp(row['started_at'])
        if 'closed_at_not_observed' in row and row['closed_at_not_observed'] is not True:
            fail('sim_service', 'An unfinished operation cannot be declared closed', operation_id=row['operation_id'])
        if 'route_id' in row:
            key(row['route_id'])
            open_keys.add((row['route_id'], row['stage_id']))
        open_ids.add(row['operation_id'])
        censored.append({'operation_id': row['operation_id'], 'stage_id': row['stage_id'],
                         'route_id': row.get('route_id'), 'started_at': row['started_at'],
                         'censored': True})
    if 'pairs' in coverage:
        pairs = _pair_stats(coverage['pairs'], routes, open_ids)
        if coverage['observed_pairs'] != pairs['total']:
            fail('sim_service', 'observed_pairs must equal the number of complete pairs supplied',
                 declared=coverage['observed_pairs'], actual=pairs['total'])
    else:
        pairs = {'total': 0, 'groups': {}}
    durations = value['durations']
    if not isinstance(durations, dict) or not durations:
        fail('sim_service', 'Durations must be keyed as route/stage')
    models = {}
    for compound, item in durations.items():
        if not isinstance(compound, str) or '/' not in compound:
            fail('sim_service', 'Duration key must be route/stage')
        route_id, stage_id = compound.split('/', 1)
        if route_id not in routes:
            fail('sim_service', 'Duration names an undefined route', route=route_id)
        stages = {step['stage_id'] for step in routes[route_id]['steps']}
        if stage_id not in stages:
            fail('sim_service', 'Duration names an undefined stage', route=route_id, stage_id=stage_id)
        models[compound] = service_estimate(item, compound)
        if models[compound]['source'] == PAIRED:
            group = pairs['groups'].get((route_id, stage_id))
            if group is None:
                fail('sim_service', 'Paired duration has no complete start/end pairs', field=compound)
            if models[compound]['sample_count'] != group['count']:
                fail('sim_service', 'Paired sample count differs from supplied complete pairs',
                     field=compound, declared=models[compound]['sample_count'], actual=group['count'])
            if models[compound]['kind'] == 'fixed':
                if abs(models[compound]['seconds'] - group['median_seconds']) > 1e-6:
                    fail('sim_service', 'Fixed duration must equal the median of its pairs',
                         field=compound, declared=models[compound]['seconds'], actual=group['median_seconds'])
            else:
                if abs(models[compound]['median_seconds'] - group['median_seconds']) > 1e-6:
                    fail('sim_service', 'Lognormal median must equal the median of its pairs',
                         field=compound, declared=models[compound]['median_seconds'],
                         actual=group['median_seconds'])
                if abs(models[compound]['sigma'] - group['log_sigma']) > 1e-6:
                    fail('sim_service', 'Lognormal sigma must equal the spread of its pairs',
                         field=compound, declared=models[compound]['sigma'], actual=group['log_sigma'])
    for route_id, item in routes.items():
        for step in item['steps']:
            if route_id + '/' + step['stage_id'] not in models:
                fail('sim_service', 'Every route stage needs an explicit duration', route=route_id, stage_id=step['stage_id'])
    return {'durations': models, 'format': value['format'], 'version': 1,
            'coverage': {'observed_pairs': coverage['observed_pairs'],
                         'open_operations': censored,
                         'open_operations_censored': len(censored),
                         'excluded_wait_seconds': coverage['excluded_wait_seconds'],
                         'note': coverage.get('note', ''),
                         'pairs_supplied': pairs['total'],
                         'pairs': [{'route_id': key_name[0], 'stage_id': key_name[1],
                                    'count': group['count'], 'median_seconds': group['median_seconds'],
                                    'log_sigma': group['log_sigma']}
                                   for key_name, group in sorted(pairs['groups'].items())]}}


def route(value, route_id, roles):
    fields(value, {'steps', 'rework'}, {'description', 'assumption_id'})
    steps = value['steps']
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        fail('sim_route', 'Route requires one to twelve steps', route=route_id)
    if 'description' in value:
        text(value['description'], 300)
    normalized = []
    index_of = {}
    for position, step in enumerate(steps):
        fields(step, {'stage_id', 'role'}, {'label'})
        key(step['stage_id'])
        if step['stage_id'] in index_of:
            fail('sim_route', 'Stage identifier repeats inside one route', route=route_id, stage_id=step['stage_id'])
        if step['role'] not in roles:
            fail('sim_route', 'Route step names a role without a calendar', route=route_id, role=step['role'])
        index_of[step['stage_id']] = position
        normalized.append({'stage_id': step['stage_id'], 'role': step['role'],
                           'label': text(step.get('label', step['stage_id']), 100), 'position': position})
    rework = value['rework']
    if not isinstance(rework, list) or len(rework) > MAX_REWORK:
        fail('sim_route', 'Rework rules must be a bounded list', route=route_id)
    rules = []
    seen = set()
    for item in rework:
        fields(item, {'at_stage_id', 'to_stage_id', 'probability'},
                {'max_rework_count', 'assumption_id', 'failure_reason'})
        key(item['at_stage_id'])
        key(item['to_stage_id'])
        if item['at_stage_id'] not in index_of or item['to_stage_id'] not in index_of:
            fail('sim_route', 'Rework rule names a stage outside the route', route=route_id)
        if item['at_stage_id'] in seen:
            fail('sim_route', 'At most one rework rule per stage', route=route_id, stage_id=item['at_stage_id'])
        seen.add(item['at_stage_id'])
        origin = index_of[item['at_stage_id']]
        destination = index_of[item['to_stage_id']]
        if destination >= origin:
            fail('sim_route', 'Rework must return to an earlier step and never bypass a prerequisite',
                 route=route_id, at_stage_id=item['at_stage_id'])
        probability = number(item['probability'], route_id + '.' + item['at_stage_id'] + '.probability', 0.0, 1.0)
        limit = integer(item.get('max_rework_count', 3), 'max_rework_count', 1, 10)
        assumption = key(item['assumption_id']) if 'assumption_id' in item else None
        if probability and assumption is None:
            fail('sim_route', 'Rework requires an explicit assumption identifier', route=route_id, stage_id=item['at_stage_id'])
        rules.append({'at_stage_id': item['at_stage_id'], 'to_stage_id': item['to_stage_id'],
                      'to_position': destination, 'probability': probability,
                      'max_rework_count': limit, 'assumption_id': assumption,
                      'failure_reason': text(item.get('failure_reason', 'unspecified rework assumption'), 200)})
    return {'route_id': route_id, 'steps': normalized, 'rework': rules,
            'description': value.get('description', ''), 'assumption_id': value.get('assumption_id')}


def routes(value, roles):
    fields(value, {'format', 'version', 'routes'})
    if value['format'] != 'sealvault-simulation-routes' or value['version'] != 1:
        fail('sim_route', 'Unsupported work route format')
    if not isinstance(value['routes'], dict) or not 1 <= len(value['routes']) <= MAX_ROUTES:
        fail('sim_route', 'Require one to fifty routes')
    result = {}
    for route_id, item in value['routes'].items():
        key(route_id)
        result[route_id] = route(item, route_id, roles)
    return result


def instant(value):
    from sealvault.sim.timeutil import instant as parse
    return parse(value)


def arrival_window(value):
    fields(value, {'observed_start', 'observed_end'})
    start = instant(value['observed_start'])
    end = instant(value['observed_end'])
    if end <= start:
        fail('sim_arrivals', 'Observation window must end after it starts')
    return {'observed_start': value['observed_start'], 'observed_end': value['observed_end'],
            'start_us': start, 'end_us': end}


def arrivals(value, mode, route_ids):
    fields(value, {'format', 'version', 'mode', 'arrivals'}, {'window', 'note'})
    if value['format'] != 'sealvault-simulation-arrivals' or value['version'] != 1:
        fail('sim_arrivals', 'Unsupported arrival trace format')
    if value['mode'] not in MODES:
        fail('sim_arrivals', 'Unknown arrival mode')
    if value['mode'] != mode:
        fail('sim_arrivals', 'Arrival trace mode differs from requested simulation mode')
    window = None
    if 'window' in value:
        window = arrival_window(value['window'])
    if 'note' in value:
        text(value['note'], 1000)
    rows = value['arrivals']
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ARRIVALS:
        fail('sim_arrivals', 'Require one to five thousand frozen arrivals')
    result = []
    seen = set()
    for row in rows:
        fields(row, {'case_id', 'arrived_at', 'priority', 'route', 'evidence'},
               {'assumption_id', 'label', 'observed_first_stage_at'})
        key(row['case_id'])
        if row['case_id'] in seen:
            fail('sim_arrivals', 'Case identifier repeats in arrival trace', case_id=row['case_id'])
        seen.add(row['case_id'])
        at = timestamp(row['arrived_at'])
        if row['priority'] not in PRIORITIES:
            fail('sim_arrivals', 'Unknown priority', case_id=row['case_id'])
        if row['route'] not in route_ids:
            fail('sim_arrivals', 'Arrival route is undefined', case_id=row['case_id'], route=row['route'])
        if row['evidence'] not in ('observed', 'scenario'):
            fail('sim_arrivals', 'Unknown arrival evidence state', case_id=row['case_id'])
        if value['mode'] == 'historical_replay' and row['evidence'] != 'observed':
            fail('sim_arrivals', 'Historical replay accepts observed arrivals only', case_id=row['case_id'])
        if value['mode'] == 'future_scenario' and row['evidence'] != 'scenario':
            fail('sim_arrivals', 'Future scenario must declare scenario arrivals explicitly', case_id=row['case_id'])
        assumption = None
        if value['mode'] == 'future_scenario':
            if 'assumption_id' not in row:
                fail('sim_arrivals', 'Scenario arrival requires an assumption identifier', case_id=row['case_id'])
            assumption = key(row['assumption_id'])
        elif 'assumption_id' in row:
            key(row['assumption_id'])
        if row.get('observed_first_stage_at') is not None:
            timestamp(row['observed_first_stage_at'])
        first_stage = None
        if row.get('observed_first_stage_at') is not None:
            first_stage = timestamp(row['observed_first_stage_at'])
            if instant(first_stage) < instant(at):
                fail('sim_arrivals', 'First stage cannot precede the recorded arrival', case_id=row['case_id'])
        result.append({'case_id': row['case_id'], 'arrived_at': at, 'arrived_us': instant(at),
                       'priority': row['priority'], 'route': row['route'], 'evidence': row['evidence'],
                       'assumption_id': assumption, 'label': str(row.get('label', '')),
                       'observed_first_stage_at': first_stage})
    result.sort(key=lambda item: (item['arrived_us'], item['case_id']))
    if window is not None and value['mode'] == 'historical_replay':
        outside = [item['case_id'] for item in result
                   if not window['start_us'] <= item['arrived_us'] <= window['end_us']]
        if outside:
            fail('sim_arrivals', 'Observed arrivals must lie inside the observation window', cases=outside[:10])
    return {'mode': value['mode'], 'arrivals': result, 'window': window}


def plan(value, roles, calendars):
    fields(value, {'plan_id', 'description', 'cross_shift', 'staffing', 'service_target_seconds'},
            {'priority_weights', 'staff_cost_per_shift', 'label', 'assumption_id'})
    key(value['plan_id'])
    text(value['description'], 500)
    if value['cross_shift'] not in CROSS_SHIFT:
        fail('sim_plan', 'cross_shift must be fixed as continue or interrupt')
    assumption = key(value['assumption_id']) if 'assumption_id' in value else None
    targets = value['service_target_seconds']
    if not isinstance(targets, dict) or set(targets) != set(PRIORITIES):
        fail('sim_plan', 'Service targets must cover every priority')
    for name, item in targets.items():
        number(item, 'service_target_seconds.' + name, 0.0, 31536000.0, exclusive_low=True)
    weights = value.get('priority_weights', {'urgent': 3, 'standard': 2, 'deferred': 1})
    if not isinstance(weights, dict) or set(weights) != set(PRIORITIES):
        fail('sim_plan', 'Priority weights must cover every priority')
    for name, item in weights.items():
        number(item, 'priority_weights.' + name, 0.0, 100.0, exclusive_low=True)
    staffing = value['staffing']
    if not isinstance(staffing, dict) or not staffing:
        fail('sim_plan', 'Staffing plan must name at least one role')
    normalized = {}
    for role, item in staffing.items():
        key(role)
        if role not in calendars:
            fail('sim_plan', 'Staffing plan names a role without a calendar', role=role)
        fields(item, {'source_role'}, {'headcount_override', 'headcount_multiplier', 'note'})
        key(item['source_role'])
        if item['source_role'] not in calendars:
            fail('sim_plan', 'Staffing plan maps an unknown source role', role=role)
        override = item.get('headcount_override')
        if override is not None:
            if not isinstance(override, list) or len(override) > MAX_CALENDAR_SHIFTS:
                fail('sim_plan', 'headcount_override must be a bounded shift list', role=role)
            override = [dict(row) for row in override]
        multiplier = item.get('headcount_multiplier', 1.0)
        number(multiplier, 'headcount_multiplier', 0.0, 100.0)
        normalized[role] = {'source_role': item['source_role'], 'headcount_override': override,
                            'headcount_multiplier': multiplier, 'note': str(item.get('note', ''))}
    costs = value.get('staff_cost_per_shift', 1.0)
    if isinstance(costs, dict):
        for role, item in costs.items():
            if role not in calendars:
                fail('sim_plan', 'Staff cost names an unknown role', role=role)
            number(item, 'staff_cost_per_shift.' + role, 0.0, 1000000.0)
    else:
        number(costs, 'staff_cost_per_shift', 0.0, 1000000.0)
    return {'plan_id': value['plan_id'], 'label': value.get('label', value['plan_id']),
            'description': value['description'], 'cross_shift': value['cross_shift'],
            'priority_weights': dict(weights), 'service_target_seconds': dict(targets),
            'staffing': normalized, 'staff_cost_per_shift': costs,
            'assumption_id': assumption}


def read(value, mode=None):
    forbid_content(value)
    if not isinstance(value, dict):
        fail('sim_model', 'Simulation model must be an object')
    fields(value, {'format', 'version', 'mode', 'arrivals', 'calendars', 'routes', 'service',
                   'simulation_end', 'streams'},
           {'plans', 'notes', 'source', 'personnel_budget'})
    if value['format'] != 'sealvault-simulation-model' or value['version'] != 1:
        fail('sim_model', 'Unsupported simulation model format')
    if value['mode'] not in MODES:
        fail('sim_model', 'Unknown simulation mode')
    if mode is not None and value['mode'] != mode:
        fail('sim_model', 'Model mode differs from requested mode')
    streams = value['streams']
    fields(streams, {'format', 'version', 'seed', 'algorithm'})
    if streams['format'] != 'sealvault-simulation-stream' or streams['version'] != 1:
        fail('sim_stream', 'Unsupported random stream record')
    if streams['algorithm'] != 'pcg64-via-python-random':
        fail('sim_stream', 'Unsupported random stream algorithm')
    text(streams['seed'], 200)
    role_calendars = calendars(value['calendars'])
    work_routes = routes(value['routes'], role_calendars)
    durations = service(value['service'], work_routes)
    trace = arrivals(value['arrivals'], value['mode'], work_routes)
    trace['digest'] = digest(value['arrivals'])
    end = timestamp(value['simulation_end'])
    if instant(end) < trace['arrivals'][0]['arrived_us']:
        fail('sim_model', 'Simulation horizon ends before the first arrival')
    budget = None
    if 'personnel_budget' in value:
        budget = value['personnel_budget']
        fields(budget, {'person_shifts'}, {'note', 'assumption_id', 'cost_ceiling'})
        integer(budget['person_shifts'], 'person_shifts', 0, 100000)
        if 'cost_ceiling' in budget:
            number(budget['cost_ceiling'], 'cost_ceiling', 0.0, 1000000000.0)
        if 'assumption_id' in budget:
            key(budget['assumption_id'])
        if 'note' in budget:
            text(budget['note'], 500)
    plans = []
    if 'plans' in value:
        if not isinstance(value['plans'], list) or len(value['plans']) > MAX_PLANS:
            fail('sim_plan', 'Plans must be a bounded list')
        for item in value['plans']:
            plans.append(plan(item, role_calendars, role_calendars))
    if 'notes' in value:
        text(value['notes'], 2000)
    source = None
    if 'source' in value:
        fields(value['source'], {'kind', 'reference'}, {'observed_window'})
        if value['source']['kind'] not in ('historical_export', 'synthetic_workload'):
            fail('sim_model', 'Unknown model source kind')
        text(value['source']['reference'], 500)
        source = dict(value['source'])
    return {'format': value['format'], 'version': 1, 'mode': value['mode'],
            'calendars': role_calendars, 'routes': work_routes, 'service': durations,
            'arrivals': trace, 'simulation_end': end, 'plans': plans,
            'streams': {'seed': streams['seed'], 'algorithm': streams['algorithm']},
            'notes': value.get('notes', ''), 'source': source,
            'personnel_budget': budget, 'digest': digest(value)}
