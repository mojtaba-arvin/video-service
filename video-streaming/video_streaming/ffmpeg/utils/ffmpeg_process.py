import re
import time


def to_seconds(time_):
    h, m, s = time_.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def get_time(key, string, default):
    time_ = re.search(f'(?<={key})\w+:\w+:\w+', string)
    return to_seconds(time_.group(0)) if time_ else default


def time_left(start_time, unit, total):
    if unit == 0:
        return 0
    diff_time = time.time() - start_time
    return total * diff_time / unit - diff_time
