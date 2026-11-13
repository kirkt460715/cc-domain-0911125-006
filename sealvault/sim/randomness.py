"""Counter-based random draws for plan-independent common random numbers.

A draw is generated from the recorded seed and the identity of the logical
work item (case, stage, attempt, purpose), never from stream position.  Two
plans therefore see exactly the same service duration and rework decision for
the same operation, even if their queues reorder unrelated cases.  Arrival
times are frozen input and are never sampled.
"""

import hashlib
import math
import random
from sealvault.codec import fail
from sealvault.sim.timeutil import MICRO

MAX_DRAW_KEY = 500
MAX_DRAWS = 100000


class Draws:
    def __init__(self, seed):
        if not isinstance(seed, str) or not 1 <= len(seed) <= 300:
            fail('sim_stream', 'Random seed must be bounded non-empty text')
        self.seed = seed
        self.count = 0
        self.channels = {'service': 0, 'rework': 0}

    def _random(self, channel, *parts):
        key = '|'.join([self.seed, channel] + [str(part) for part in parts])
        if len(key) > MAX_DRAW_KEY:
            fail('sim_stream', 'Random draw key exceeds supported length')
        self.count += 1
        if self.count > MAX_DRAWS:
            fail('sim_stream', 'Simulation exceeded its random draw budget')
        digest = hashlib.sha256(key.encode('utf-8')).hexdigest()
        if channel in self.channels:
            self.channels[channel] += 1
        return random.Random(int(digest[:32], 16))

    def uniform(self, channel, *parts):
        return self._random(channel, *parts).random()

    def normal(self, channel, *parts):
        return self._random(channel, *parts).gauss(0.0, 1.0)

    def service_us(self, estimate, case_id, stage_id, attempt):
        if estimate['kind'] == 'fixed':
            return int(round(estimate['seconds'] * MICRO))
        median = estimate['median_seconds']
        sigma = estimate['sigma']
        sample = math.exp(math.log(median) + sigma * self.normal('service', case_id, stage_id, attempt))
        return max(MICRO, int(round(sample * MICRO)))

    def rework(self, case_id, stage_id, attempt):
        return self.uniform('rework', case_id, stage_id, attempt)

    def signature(self):
        return {'seed': self.seed, 'algorithm': 'sha256-keyed-random.Random',
                'common_random_numbers': 'keyed_by_case_stage_attempt',
                'draws': self.count, 'channels': dict(self.channels)}
