"""Public entry points for the custody scheduling simulation."""

from sealvault.codec import fail
from sealvault.sim import engine, model as model_module, reporting
from sealvault.sim.timeutil import instant

__all__ = ['read_model', 'run', 'compare', 'explain', 'scenario_summary']


def read_model(value, mode=None):
    parsed = model_module.read(value, mode)
    parsed['entry_start_us'] = min(item['arrived_us'] for item in parsed['arrivals']['arrivals'])
    parsed['entry_end_us'] = instant(parsed['simulation_end'])
    return parsed


def _parsed(model):
    if not isinstance(model, dict) or 'entry_end_us' not in model:
        fail('sim_model', 'Simulation model must be read with read_model first')
    return model


def run(model, plan_id, seed=None):
    model = _parsed(model)
    seed = seed if seed is not None else model['streams']['seed']
    plan = next((item for item in model['plans'] if item['plan_id'] == plan_id), None)
    if plan is None:
        fail('sim_plan', 'Unknown staffing plan', plan=plan_id)
    desk = engine.Desk(model, plan, seed).run()
    return reporting.report(desk)


def explain(model, plan_id, case_id, seed=None):
    model = _parsed(model)
    seed = seed if seed is not None else model['streams']['seed']
    plan = next((item for item in model['plans'] if item['plan_id'] == plan_id), None)
    if plan is None:
        fail('sim_plan', 'Unknown staffing plan', plan=plan_id)
    desk = engine.Desk(model, plan, seed).run()
    return reporting.explain(desk, case_id)


def scenario_summary(model):
    model = _parsed(model)
    coverage = model['service']['coverage']
    rows = model['arrivals']['arrivals']
    summary = {
        'mode': model['mode'],
        'source': model['source'],
        'arrival_count': len(rows),
        'arrivals_by_evidence': {state: sum(1 for item in rows if item['evidence'] == state)
                                  for state in ('observed', 'scenario')},
        'arrivals_by_priority': {name: sum(1 for item in rows if item['priority'] == name)
                                 for name in ('urgent', 'standard', 'deferred')},
        'arrivals_by_route': {route: sum(1 for item in rows if item['route'] == route)
                              for route in model['routes']},
        'observation_window': model['arrivals'].get('window'),
        'simulation_end': model['simulation_end'],
        'assumption_ids': sorted({item['assumption_id'] for item in rows if item['assumption_id']}),
        'service_coverage': coverage,
        'duration_rule': 'complete paired start/end observation or explicit assumption',
        'wait_excluded_from_service': True,
        'roles': sorted(model['calendars']),
        'routes': {route_id: {'steps': [step['stage_id'] for step in item['steps']],
                              'roles': [step['role'] for step in item['steps']],
                              'rework': item['rework']}
                   for route_id, item in model['routes'].items()},
        'plans': [{'plan_id': item['plan_id'], 'label': item['label'],
                   'cross_shift': item['cross_shift'], 'staffing': item['staffing']}
                  for item in model['plans']],
        'evidence_content_read': False,
    }
    if model['mode'] == 'historical_replay':
        summary['historical_claim'] = ('Replays a frozen observed arrival trace. Completion and handoff records '
                                       'were used only to estimate duration pairs, never to infer arrival times.')
    else:
        summary['historical_claim'] = ('Future scenario. Demand outside the observed window is an explicit '
                                       'scenario assumption, not a forecast, and no accuracy claim is made.')
    return summary


def compare(model, plan_ids, repetitions=20, seed=None, basis_plan_id=None):
    model = _parsed(model)
    seed = seed if seed is not None else model['streams']['seed']
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or not 2 <= repetitions <= 500:
        fail('sim_compare', 'Repetitions must be an integer between two and five hundred')
    if not plan_ids:
        plan_ids = [item['plan_id'] for item in model['plans']]
    basis = basis_plan_id or plan_ids[0]
    reports = []
    for repetition in range(repetitions):
        repetition_seed = seed + '|repetition-' + str(repetition)
        for plan_id in plan_ids:
            plan = next((item for item in model['plans'] if item['plan_id'] == plan_id), None)
            if plan is None:
                fail('sim_plan', 'Unknown staffing plan', plan=plan_id)
            desk = engine.Desk(model, plan, repetition_seed).run()
            report_data = reporting.report(desk)
            reports.append({'plan_id': plan_id, 'label': plan['label'], 'repetition': repetition,
                            'seed': repetition_seed, 'metrics': report_data['summary']})
    return reporting.compare(reports, basis)
