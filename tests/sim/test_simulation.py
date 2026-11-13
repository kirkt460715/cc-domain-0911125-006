import copy
import unittest
from sealvault.codec import VaultError, loads, canonical
from sealvault.sim import read_model, run, compare, explain, scenario_summary
from sealvault.sim.model import PRIORITIES


def base_model(mode='historical_replay'):
    shift = [{'weekday': 0, 'start': '09:00', 'end': '12:00', 'headcount': 1}]
    return {
        'format': 'sealvault-simulation-model', 'version': 1, 'mode': mode,
        'source': {'kind': 'synthetic_workload', 'reference': 'tests/sim/synthetic.json'},
        'simulation_end': '2026-11-09T06:00:00Z',
        'streams': {'format': 'sealvault-simulation-stream', 'version': 1,
                    'seed': 'test-seed', 'algorithm': 'pcg64-via-python-random'},
        'calendars': {'intake': {'utc_offset': '+08:00', 'shifts': shift}},
        'routes': {'format': 'sealvault-simulation-routes', 'version': 1,
                   'routes': {'receive': {'steps': [{'stage_id': 'receive', 'role': 'intake'}],
                                          'rework': []}}},
        'service': {'format': 'sealvault-simulation-service', 'version': 1,
                    'coverage': {'observed_pairs': 0, 'open_operations': [],
                                 'excluded_wait_seconds': 0},
                    'durations': {'receive/receive': {'kind': 'fixed', 'seconds': 900,
                                                      'source': 'explicit_assumption',
                                                      'assumption_id': 'assume-15m'}}},
        'arrivals': {'format': 'sealvault-simulation-arrivals', 'version': 1, 'mode': mode,
                     'window': {'observed_start': '2026-11-09T00:00:00Z',
                                'observed_end': '2026-11-09T06:00:00Z'},
                     'arrivals': [
                         {'case_id': 'a1', 'arrived_at': '2026-11-09T01:00:00Z',
                          'priority': 'standard', 'route': 'receive', 'evidence': 'observed',
                          'observed_first_stage_at': None},
                     ]},
        'plans': [{'plan_id': 'single', 'description': 'one intake seat', 'cross_shift': 'interrupt',
                   'staffing': {'intake': {'source_role': 'intake'}},
                   'service_target_seconds': {'urgent': 1800, 'standard': 3600, 'deferred': 5400},
                   'staff_cost_per_shift': 1.0}],
    }


class SimulationErrors(unittest.TestCase):
    def assert_error(self, code, function, *args):
        with self.assertRaises(VaultError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_evidence_payload_is_rejected(self):
        model = base_model()
        model['arrivals']['arrivals'][0]['content'] = 'c2VjcmV0'
        self.assert_error('sim_evidence_access', read_model, model)

    def test_historical_replay_rejects_scenario_arrivals(self):
        model = base_model()
        model['arrivals']['arrivals'][0]['evidence'] = 'scenario'
        self.assert_error('sim_arrivals', read_model, model)

    def test_future_scenario_requires_assumption_id(self):
        model = base_model('future_scenario')
        model['arrivals']['mode'] = 'future_scenario'
        model['arrivals']['arrivals'][0]['evidence'] = 'scenario'
        self.assert_error('sim_arrivals', read_model, model)
        model['arrivals']['arrivals'][0]['assumption_id'] = 'surge-assumption'
        self.assert_error('sim_model', read_model, model, 'historical_replay')
        parsed = read_model(model, 'future_scenario')
        self.assertEqual(parsed['arrivals']['arrivals'][0]['assumption_id'], 'surge-assumption')
        summary = scenario_summary(parsed)
        self.assertEqual(summary['arrivals_by_evidence'], {'observed': 0, 'scenario': 1})
        self.assertIn('not a forecast', summary['historical_claim'])

    def test_service_duration_must_not_come_from_waiting(self):
        model = base_model()
        model['service']['durations']['receive/receive']['source'] = 'derived_from_queue_wait'
        self.assert_error('sim_service', read_model, model)
        model['service']['durations']['receive/receive'] = {'kind': 'fixed', 'seconds': 900,
                                                            'source': 'explicit_assumption'}
        self.assert_error('sim_service', read_model, model)

    def test_paired_durations_must_match_complete_pairs(self):
        model = base_model()
        model['service']['coverage']['observed_pairs'] = 2
        model['service']['coverage']['pairs'] = [
            {'operation_id': 'p1', 'route_id': 'receive', 'stage_id': 'receive',
             'started_at': '2026-11-09T01:00:00Z', 'finished_at': '2026-11-09T01:20:00Z'},
            {'operation_id': 'p2', 'route_id': 'receive', 'stage_id': 'receive',
             'started_at': '2026-11-09T01:20:00Z', 'finished_at': '2026-11-09T01:40:00Z'},
        ]
        model['service']['durations']['receive/receive'] = {'kind': 'fixed', 'seconds': 900,
                                                            'source': 'paired_start_end',
                                                            'sample_count': 2}
        self.assert_error('sim_service', read_model, model)
        model['service']['durations']['receive/receive']['seconds'] = 1200
        parsed = read_model(model)
        self.assertEqual(parsed['service']['coverage']['pairs_supplied'], 2)
        self.assertEqual(parsed['service']['coverage']['pairs'][0]['median_seconds'], 1200.0)

    def test_open_operations_stay_censored_and_cannot_be_closed(self):
        model = base_model()
        model['service']['coverage']['open_operations'] = [
            {'operation_id': 'open-1', 'stage_id': 'receive',
             'started_at': '2026-11-09T02:00:00Z'}
        ]
        parsed = read_model(model)
        coverage = parsed['service']['coverage']
        self.assertEqual(coverage['open_operations_censored'], 1)
        self.assertTrue(coverage['open_operations'][0]['censored'])
        report = run(parsed, 'single')
        self.assertEqual(report['model']['service']['coverage']['open_operations_censored'], 1)

        model['service']['coverage']['open_operations'][0]['closed_at_not_observed'] = False
        self.assert_error('sim_service', read_model, model)

    def test_rework_must_return_to_an_earlier_step(self):
        model = base_model()
        model['routes']['routes']['receive']['steps'] = [
            {'stage_id': 'receive', 'role': 'intake'},
            {'stage_id': 'review', 'role': 'intake'},
            {'stage_id': 'handoff', 'role': 'intake'},
        ]
        model['service']['durations'] = {
            'receive/receive': {'kind': 'fixed', 'seconds': 900, 'source': 'explicit_assumption',
                                'assumption_id': 'a1'},
            'receive/review': {'kind': 'fixed', 'seconds': 900, 'source': 'explicit_assumption',
                               'assumption_id': 'a2'},
            'receive/handoff': {'kind': 'fixed', 'seconds': 900, 'source': 'explicit_assumption',
                                'assumption_id': 'a3'},
        }
        model['routes']['routes']['receive']['rework'] = [
            {'at_stage_id': 'receive', 'to_stage_id': 'review', 'probability': 0.5,
             'assumption_id': 'b1'}
        ]
        self.assert_error('sim_route', read_model, model)

    def test_shift_window_must_be_single_day(self):
        model = base_model()
        model['calendars']['intake']['shifts'][0]['end'] = '25:00'
        self.assert_error('sim_calendar', read_model, model)


class SingleDesk(unittest.TestCase):
    def setUp(self):
        raw = base_model('historical_replay')
        raw['arrivals']['arrivals'] = [
            {'case_id': cid, 'arrived_at': '2026-11-09T01:00:00Z', 'priority': priority,
             'route': 'receive', 'evidence': 'observed'}
            for cid, priority in [('u1', 'urgent'), ('s1', 'standard'),
                                  ('s2', 'standard'), ('d1', 'deferred')]
        ]
        self.raw = raw
        self.model = read_model(raw)

    def shortened(self, end):
        raw = copy.deepcopy(self.raw)
        raw['simulation_end'] = end
        return read_model(raw)

    def test_priority_queue_and_utilisation_are_hand_checkable(self):
        report = run(self.model, 'single')
        self.assertEqual(report['summary']['completed'], 4)
        self.assertEqual(report['summary']['mean_completion_seconds'], 2250.0)
        self.assertEqual(report['summary']['mean_queue_wait_seconds'], 1350.0)
        urgency = {row['case_id']: row for row in report['cases']}
        self.assertEqual(urgency['u1']['completion_seconds'], 900.0)
        self.assertEqual(urgency['d1']['completion_seconds'], 3600.0)
        intake = report['summary']['utilization']['intake']
        self.assertEqual(intake['occupied_headcount_seconds'], 3600.0)
        self.assertEqual(intake['scheduled_headcount_seconds'], 10800.0)
        self.assertEqual(intake['utilization'], 0.3333)
        self.assertEqual(report['summary']['person_shifts'], 1)
        self.assertTrue(report['schedule_integrity']['occupancy_never_above_headcount'])

    def test_report_records_ordering_and_event_sequence(self):
        report = run(self.model, 'single')
        self.assertEqual(report['simulation']['tie_order'],
                         {'arrival': 0, 'completion': 1, 'shift_window': 2, 'dispatch': 3})
        kinds = {row['kind'] for row in report['simulation']['event_log']}
        self.assertIn('service_start', kinds)
        self.assertIn('service_complete', kinds)
        self.assertIn('window_open', kinds)
        decisions = report['simulation']['decisions']
        self.assertEqual([row['case_id'] for row in decisions][0], 'u1')

    def test_headcount_two_never_exceeds_seat_limit(self):
        raw = copy.deepcopy(self.raw)
        raw['plans'][0]['staffing']['intake']['headcount_multiplier'] = 2
        report = run(read_model(raw), 'single')
        self.assertEqual(report['summary']['mean_completion_seconds'], 1350.0)
        self.assertEqual(report['summary']['person_shifts'], 2)
        self.assertTrue(report['schedule_integrity']['occupancy_never_above_headcount'])

    def test_cutoff_queued_cases_enter_backlog_denominator(self):
        model = self.shortened('2026-11-09T01:30:00Z')
        report = run(model, 'single')
        summary = report['summary']
        self.assertEqual(summary['arrivals'], 4)
        self.assertEqual(summary['backlog_denominator'], 4)
        self.assertEqual(summary['censored_at_cutoff'], 2)
        self.assertEqual(summary['backlog_rate'], 0.5)
        self.assertEqual(summary['service_target_violation_rate'], 0.5)
        self.assertTrue(summary['backlog_definition'].startswith('unfinished cases at cutoff divided by all arrivals'))
        self.assertEqual(summary['mean_completion_seconds'], 1350.0)

    def test_arrival_at_a_completion_instant_joins_the_same_dispatch_tick(self):
        raw = base_model('historical_replay')
        raw['arrivals']['arrivals'] = [
            {'case_id': 'first', 'arrived_at': '2026-11-09T01:00:00Z', 'priority': 'standard',
             'route': 'receive', 'evidence': 'observed'},
            {'case_id': 'late', 'arrived_at': '2026-11-09T01:15:00Z', 'priority': 'urgent',
             'route': 'receive', 'evidence': 'observed'},
        ]
        report = run(read_model(raw), 'single')
        late = next(row for row in report['cases'] if row['case_id'] == 'late')
        self.assertEqual(late['arrival_to_first_work_seconds'], 0.0)
        self.assertEqual(late['stages'][0]['started_at'], '2026-11-09T01:15:00.000000Z')
        event_kinds = [row['kind'] for row in report['simulation']['event_log']]
        self.assertLess(event_kinds.index('service_complete'), event_kinds.index('service_start',
                                                                                 event_kinds.index('service_complete')))

    def test_no_new_work_starts_at_the_cutoff(self):
        model = self.shortened('2026-11-09T01:00:00Z')
        report = run(model, 'single')
        self.assertEqual(report['summary']['completed'], 0)
        self.assertEqual(report['summary']['backlog_rate'], 1.0)
        self.assertEqual(report['summary']['utilization']['intake']['occupied_headcount_seconds'], 0.0)
        for row in report['cases']:
            for stage in row['stages']:
                self.assertIsNone(stage['started_at'])

    def test_event_log_is_ordered_and_monotone(self):
        report = run(self.model, 'single')
        rows = report['simulation']['event_log']
        self.assertEqual([row['sequence'] for row in rows], list(range(1, len(rows) + 1)))
        instants = [row['at'] for row in rows]
        self.assertEqual(instants, sorted(instants))

    def test_censored_cases_report_waiting_without_imputing_service_time(self):
        model = self.shortened('2026-11-09T01:30:00Z')
        report = run(model, 'single')
        censored = [row for row in report['cases'] if row['censored']]
        self.assertEqual(len(censored), 2)
        for row in censored:
            self.assertGreater(row['censored_wait_seconds'], 0)
            self.assertEqual(row['queue_wait_seconds'], 0.0)
        self.assertGreater(report['summary']['mean_censored_wait_seconds'], 0)
        self.assertEqual(report['summary']['utilization']['intake']['services_started'], 2)
        self.assertEqual(report['summary']['utilization']['intake']['services_resumed'], 0)

    def test_explanation_names_the_blocking_role_and_window(self):
        model = self.shortened('2026-11-09T01:30:00Z')
        explanation = explain(model, 'single', 'd1')
        self.assertEqual(explanation['status'], 'censored_at_cutoff')
        self.assertEqual(explanation['blocks'][0]['stage_id'], 'receive')
        self.assertEqual(explanation['blocks'][0]['reason'], 'never_started_before_cutoff')
        self.assertEqual(explanation['blocks'][0]['role_headcount_at_cutoff'], 1)
        self.assertGreater(explanation['blocks'][0]['services_admitted_while_waiting'], 0)


class CrossShift(unittest.TestCase):
    def model(self, cross_shift, duration=900):
        """Two adjacent Monday shifts with a one-hour unstaffed gap between them."""
        raw = base_model('historical_replay')
        raw['calendars']['intake']['shifts'] = [
            {'weekday': 0, 'start': '09:00', 'end': '12:00', 'headcount': 1},
            {'weekday': 0, 'start': '13:00', 'end': '16:00', 'headcount': 1},
        ]
        raw['service']['durations']['receive/receive'] = {
            'kind': 'fixed', 'seconds': duration, 'source': 'explicit_assumption',
            'assumption_id': 'assume-long-service'}
        raw['arrivals']['arrivals'][0]['arrived_at'] = '2026-11-09T03:00:00Z'  # 11:00 local
        raw['simulation_end'] = '2026-11-09T12:00:00Z'
        raw['plans'][0]['cross_shift'] = cross_shift
        return read_model(raw)

    def test_continue_across_shift_books_unstaffed_gap_as_overtime(self):
        report = run(self.model('continue', 18000), 'single')  # five hours from 11:00 local
        intake = report['summary']['utilization']['intake']
        self.assertEqual(intake['overtime_seconds'], 3600.0)  # 12:00-13:00 gap
        self.assertEqual(intake['occupied_headcount_seconds'], 14400.0)  # 4 staffed hours
        self.assertEqual(intake['utilization'], 1.0)
        self.assertEqual(report['cases'][0]['completion_seconds'], 18000.0)
        self.assertEqual(report['cases'][0]['status'], 'completed')

    def test_interrupt_pauses_at_boundary_and_resumes_next_window(self):
        report = run(self.model('interrupt', 9000), 'single')  # two and a half hours
        case = report['cases'][0]
        self.assertEqual(case['stages'][0]['segments'], 2)
        self.assertEqual(case['stages'][0]['pauses'], 1)
        self.assertEqual(case['stages'][0]['status'], 'done')
        self.assertEqual(case['completion_seconds'], 12600.0)  # 11:00 -> 14:30
        intake = report['summary']['utilization']['intake']
        self.assertEqual(intake['overtime_seconds'], 0.0)
        self.assertLessEqual(intake['occupied_headcount_seconds'],
                             intake['scheduled_headcount_seconds'])
        trace_kinds = [row['kind'] for row in case['trace']]
        self.assertIn('pause', trace_kinds)
        self.assertIn('resume', trace_kinds)

    def test_rework_returns_to_earlier_stage_within_case(self):
        raw = base_model('historical_replay')
        raw['routes']['routes'] = {'full': {
            'steps': [{'stage_id': 'receive', 'role': 'intake'},
                      {'stage_id': 'review', 'role': 'intake'}],
            'rework': [{'at_stage_id': 'review', 'to_stage_id': 'receive', 'probability': 1.0,
                        'max_rework_count': 1, 'assumption_id': 'always-rework',
                        'failure_reason': 'forced rework for the test'}]}}
        raw['service']['durations'] = {
            'full/receive': {'kind': 'fixed', 'seconds': 900, 'source': 'explicit_assumption',
                             'assumption_id': 'a1'},
            'full/review': {'kind': 'fixed', 'seconds': 900, 'source': 'explicit_assumption',
                            'assumption_id': 'a2'}}
        raw['arrivals']['arrivals'][0]['route'] = 'full'
        report = run(read_model(raw), 'single')
        case = report['cases'][0]
        self.assertEqual(case['rework_count'], 1)
        positions = [row['stage_id'] for row in case['stages']]
        self.assertEqual(positions, ['receive', 'review', 'receive', 'review'])
        self.assertTrue(report['schedule_integrity']['stage_precedence_preserved'])


class SimulationCli(unittest.TestCase):
    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from sealvault.cli import main
        out, error = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            code = main(list(argv))
        return code, out.getvalue(), error.getvalue()

    def test_describe_and_simulate_read_no_database(self):
        code, out, error = self.run_cli('describe', 'fixtures/simulation/night-shift.json')
        self.assertEqual(code, 0)
        self.assertFalse(loads(out)['evidence_content_read'])
        code, out, error = self.run_cli('simulate', 'fixtures/simulation/queue-single.json',
                                        '--plan', 'one-seat')
        self.assertEqual(code, 0)
        report = loads(out)
        self.assertEqual(report['summary']['mean_completion_seconds'], 2250.0)
        self.assertFalse(report['summary']['evidence_content_read'])

    def test_unknown_plan_returns_structured_error(self):
        code, out, error = self.run_cli('simulate', 'fixtures/simulation/queue-single.json',
                                        '--plan', 'missing')
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(loads(error)['error']['code'], 'sim_plan')


class FutureScenario(unittest.TestCase):
    def raw(self):
        from pathlib import Path
        return loads(Path('fixtures/simulation', 'future-surge.json').read_text(encoding='utf-8'))

    def load(self):
        return read_model(self.raw())

    def test_scenario_arrivals_require_assumptions_and_never_claim_accuracy(self):
        model = self.load()
        summary = scenario_summary(model)
        self.assertEqual(summary['mode'], 'future_scenario')
        self.assertEqual(summary['arrivals_by_evidence']['observed'], 0)
        self.assertEqual(summary['arrivals_by_evidence']['scenario'], 12)
        self.assertEqual(summary['assumption_ids'], ['assume-surge-3x-baseline'])
        self.assertIn('not a forecast', summary['historical_claim'])
        self.assertFalse(summary['evidence_content_read'])

    def test_scenario_rows_reject_observed_claims(self):
        raw = self.raw()
        raw['arrivals']['arrivals'][0]['evidence'] = 'observed'
        with self.assertRaises(VaultError) as caught:
            read_model(raw)
        self.assertEqual(caught.exception.code, 'sim_arrivals')


class PlanComparison(unittest.TestCase):
    def setUp(self):
        raw = base_model('historical_replay')
        raw['arrivals']['arrivals'] = [
            {'case_id': 'c' + str(index), 'arrived_at': '2026-11-09T01:00:00Z',
             'priority': 'standard' if index % 2 else 'urgent', 'route': 'receive',
             'evidence': 'observed'}
            for index in range(6)
        ]
        raw['service']['durations']['receive/receive'] = {
            'kind': 'lognormal', 'median_seconds': 900, 'sigma': 0.2,
            'source': 'explicit_assumption', 'assumption_id': 'assume-lognormal'}
        raw['plans'].append({
            'plan_id': 'double', 'description': 'two intake seats',
            'cross_shift': 'interrupt',
            'staffing': {'intake': {'source_role': 'intake', 'headcount_multiplier': 2}},
            'service_target_seconds': {'urgent': 1800, 'standard': 3600, 'deferred': 5400},
            'staff_cost_per_shift': 1.0})
        self.model = read_model(raw)

    def test_repeated_runs_are_deterministic(self):
        first = run(self.model, 'single', 'fixed-seed')
        second = run(self.model, 'single', 'fixed-seed')
        self.assertEqual(canonical(first['summary']), canonical(second['summary']))
        self.assertEqual(canonical(first['cases']), canonical(second['cases']))

    def test_paired_comparison_reports_interval_and_monte_carlo_error(self):
        result = compare(self.model, ['single', 'double'], repetitions=6, seed='cmp-seed',
                         basis_plan_id='single')
        self.assertEqual(result['repetitions'], 6)
        self.assertTrue(result['identical_arrival_path'])
        self.assertTrue(result['identical_random_draws_per_repetition'])
        paired = result['comparisons']['double']['paired_differences']['mean_completion_seconds']
        self.assertEqual(paired['samples'], 6)
        self.assertIsNotNone(paired['interval_95'])
        self.assertIsNotNone(paired['standard_error'])
        self.assertLessEqual(paired['interval_95'][0], paired['mean'])
        self.assertGreaterEqual(paired['interval_95'][1], paired['mean'])

    def test_same_draws_are_reused_across_plans(self):
        single = run(self.model, 'single', 'shared-seed')
        double = run(self.model, 'double', 'shared-seed')
        self.assertEqual(single['simulation']['random']['draws'], double['simulation']['random']['draws'])
        self.assertEqual(single['model']['digest'], double['model']['digest'])


class NightShiftFixture(unittest.TestCase):
    def load(self, name):
        from pathlib import Path
        return read_model(loads(Path('fixtures/simulation', name + '.json').read_text(encoding='utf-8')))

    def test_more_receivers_alone_do_not_fix_a_review_bottleneck(self):
        model = self.load('night-shift')
        baseline = run(model, 'day-only')['summary']
        late = run(model, 'late-receive-no-review')['summary']
        both = run(model, 'late-receive-and-review')['summary']
        self.assertGreaterEqual(late['service_target_violation_rate'],
                                baseline['service_target_violation_rate'])
        self.assertGreater(late['total_staff_cost'], baseline['total_staff_cost'])
        self.assertLess(both['service_target_violation_rate'], baseline['service_target_violation_rate'])

    def test_budget_is_enforced_per_plan(self):
        model = self.load('night-shift')
        self.assertTrue(run(model, 'day-only')['summary']['personnel_budget']['within_budget'])
        self.assertTrue(run(model, 'late-receive-no-review')['summary']['personnel_budget']['within_budget'])
        self.assertFalse(run(model, 'late-receive-and-review')['summary']['personnel_budget']['within_budget'])

    def test_review_is_censored_under_night_plan(self):
        model = self.load('night-shift')
        report = run(model, 'late-receive-no-review')
        coverage = report['model']['service']['coverage']
        self.assertEqual(coverage['open_operations_censored'], 2)
        self.assertTrue(all(row['censored'] for row in coverage['open_operations']))

    def test_scenarios_are_declared_not_observed_in_future_mode(self):
        model = self.load('night-shift')
        summary = scenario_summary(model)
        self.assertEqual(summary['arrivals_by_evidence'], {'observed': len(model['arrivals']['arrivals']), 'scenario': 0})
        self.assertEqual(summary['mode'], 'historical_replay')


if __name__ == '__main__':
    unittest.main()
