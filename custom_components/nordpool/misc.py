import logging
from collections import defaultdict
from operator import itemgetter
from statistics import mean
from decimal import Decimal
from datetime import datetime, timedelta

import pytz
from homeassistant.util import dt as dt_util
from pytz import timezone

UTC = pytz.utc

__all__ = [
    "is_new",
    "has_junk",
    "extract_attrs",
    "start_of",
    "end_of",
    "stock",
    "add_junk",
]

_LOGGER = logging.getLogger(__name__)

stockholm_tz = timezone("Europe/Stockholm")


def exceptions_raiser():
    """Utility to check that all exceptions are raised."""
    import aiohttp
    import random

    exs = [KeyError, aiohttp.ClientError, None, None, None]
    got = random.choice(exs)
    if got is None:
        pass
    else:
        raise got


def round_decimal(number, decimal_places=3):
    decimal_value = Decimal(number)
    return decimal_value.quantize(Decimal(10) ** -decimal_places)


def add_junk(d):
    for key in ["Average", "Min", "Max", "Off-peak 1", "Off-peak 2", "Peak"]:
        d[key] = float("inf")

    return d


def stock(d):
    """convert datetime to stocholm time."""
    return d.astimezone(stockholm_tz)


def day_coverage(values, target_date, tzinfo) -> bool:
    """Check that a day's values fully cover the target date.

    Nord Pool moved to a 15-minute MTU for delivery day 2025-10-01, so a
    normal day now has 96 quarter-hour rows (DST days 92 or 100) instead
    of the old 24 hourly rows. A fixed row-count check can accept a short
    partial answer as complete, which would stall the self-heal retry
    (code review finding F1). Instead we verify that the sorted
    intervals cover the whole local day contiguously:
    local midnight -> next local midnight, no gaps or overlaps.

    values: list of {"start": datetime, "end": datetime, ...} (tz-aware)
    target_date: the local date the dataset should cover (date object)
    tzinfo: tzinfo/ZoneInfo of the area's timezone
    """
    if not values:
        return False
    naive_start = datetime(target_date.year, target_date.month, target_date.day)
    naive_next = naive_start + timedelta(days=1)
    try:
        if hasattr(tzinfo, "localize"):  # pytz zones need localize()
            day_start = tzinfo.localize(naive_start)
            day_end = tzinfo.localize(naive_next)
        else:
            # zoneinfo/ZoneInfo: attach by wall clock, offset resolved by fold
            day_start = naive_start.replace(tzinfo=tzinfo)
            day_end = naive_next.replace(tzinfo=tzinfo)
    except (AttributeError, TypeError, ValueError):
        return False
    cursor = day_start
    for item in sorted(values, key=lambda v: v["start"]):
        start = item["start"]
        end = item["end"]
        try:
            start = start.astimezone(tzinfo)
            end = end.astimezone(tzinfo)
        except (AttributeError, ValueError, TypeError):
            return False
        if end <= start:
            # DST days can contain zero-length rows; skip them.
            continue
        if start > cursor:
            return False  # gap
        if end > day_end:
            return False  # row belongs to the next day
        cursor = end
        if cursor >= day_end:
            return True
    return cursor >= day_end


def start_of(d, typ_="hour"):
    if typ_ == "hour":
        return d.replace(minute=0, second=0, microsecond=0)
    elif typ_ == "day":
        return d.replace(hour=0, minute=0, second=0, microsecond=0)


def time_in_range(start, end, x):
    """Return true if x is in the range [start, end]"""
    if start <= end:
        return start <= x <= end
    else:
        return start <= x or x <= end


def is_new(date=None, typ="day") -> bool:
    """Utility to check if its a new hour or day."""
    # current = pendulum.now()
    current = dt_util.now()
    if typ == "day":
        if date.date() != current.date():
            _LOGGER.debug("Its a new day!")
            return True
        return False

    elif typ == "hour":
        if current.hour != date.hour:
            _LOGGER.debug("Its a new hour!")
            return True
        return False


def is_inf(d):
    if d == float("inf"):
        return True
    return False


def has_junk(data) -> bool:
    """Check if data has some infinity values.

    Args:
        data (dict): Holds the data from the api.

    Returns:
        TYPE: True if there is any infinity values else False
    """
    cp = dict(data)
    cp.pop("values", None)
    if any(map(is_inf, cp.values())):
        return True
    return False


def extract_attrs(data) -> dict:
    """extract attrs"""
    d = defaultdict(list)
    items = [i.get("value") for i in data]

    if len(data):
        data = sorted(data, key=itemgetter("start"))
        offpeak1 = [i.get("value") for i in data[0:8]]
        peak = [i.get("value") for i in data[8:20]]
        offpeak2 = [i.get("value") for i in data[20:]]

        d["Peak"] = mean(peak)
        d["Off-peak 1"] = mean(offpeak1)
        d["Off-peak 2"] = mean(offpeak2)
        d["Average"] = mean(items)
        d["Min"] = min(items)
        d["Max"] = max(items)

        return d

    return data
