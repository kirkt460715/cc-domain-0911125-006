import datetime
from sealvault.codec import timestamp, fail

MICRO = 1000000


def instant(value, name='time'):
    """Return UTC microseconds since the epoch for a canonical timestamp."""
    text = timestamp(value)
    parsed = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    delta = parsed - epoch
    return delta.days * 86400 * MICRO + delta.seconds * MICRO + delta.microseconds


def format_instant(value):
    if type(value) is not int or value < 0:
        fail('sim_time', 'Instant must be nonnegative integer microseconds')
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    parsed = epoch + datetime.timedelta(microseconds=value)
    return parsed.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def epoch_day(value):
    """Whole UTC days since 1970-01-01 for an instant."""
    if type(value) is not int:
        fail('sim_time', 'Instant must be integer microseconds')
    return value // (86400 * MICRO)


def seconds_to_us(value):
    if type(value) is not int or value < 0:
        fail('sim_duration', 'Duration must be a nonnegative integer number of seconds')
    return value * MICRO


def us_to_seconds(value, places=3):
    if type(value) is not int or value < 0:
        fail('sim_duration', 'Duration must be nonnegative integer microseconds')
    return round(value / MICRO, places)


def overlap(left_start, left_end, right_start, right_end):
    return max(0, min(left_end, right_end) - max(left_start, right_start))
