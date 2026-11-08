import logging
import math
from collections import defaultdict
from operator import itemgetter
from statistics import mean
from decimal import Decimal
from datetime import datetime, timedelta

import pytz
from homeassistant.util import dt as dt_util
from pytz import timezone

from .const import tzs as _AREA_TIMEZONES

UTC = pytz.utc

__all__ = [
    "is_new",
    "has_junk",
    "extract_attrs",
    "start_of",
    "end_of",
    "stock",
    "add_junk",
    "day_coverage",
    "AREA_TZINFO",
    "is_valid_price",
]

_LOGGER = logging.getLogger(__name__)

stockholm_tz = timezone("Europe/Stockholm")
AREA_TZINFO = {area: timezone(zone_name) for area, zone_name in _AREA_TIMEZONES.items()}


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


def is_valid_price(value) -> bool:
    """Accept finite numeric prices, including zero and negative prices."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def day_coverage(values, target_date, tzinfo) -> bool:
    """Return whether usable intervals cover one complete local delivery day."""
    if not values:
        return False

    try:
        day_start = tzinfo.localize(
            datetime(target_date.year, target_date.month, target_date.day)
        ).astimezone(UTC)
        day_end = tzinfo.localize(
            datetime(target_date.year, target_date.month, target_date.day)
            + timedelta(days=1)
        ).astimezone(UTC)
        intervals = []
        for item in values:
            start, end = item["start"], item["end"]
            if (
                start.utcoffset() is None
                or end.utcoffset() is None
                or not is_valid_price(item["value"])
            ):
                return False
            intervals.append((start.astimezone(UTC), end.astimezone(UTC)))
        intervals.sort()
    except (AttributeError, KeyError, TypeError, ValueError):
        return False

    cursor = day_start
    for start, end in intervals:
        if end <= start or end <= day_start:
            continue
        if start >= day_end:
            break
        start = max(start, day_start)
        end = min(end, day_end)
        if start > cursor:
            return False
        cursor = max(cursor, end)
        if cursor >= day_end:
            return True
    return False


def start_of(d, typ_="hour"):
    if typ_ == "hour":
        return d.replace(minute=0, second=0, microsecond=0)
    elif typ_ == "day":
        return d.replace(hour=0, minute=0, microsecond=0)


def time_in_range(start, end, x):
    """Return true if x is in the range [start, end]"""
    if start <= end:
        return start <= x <= end
    else:
        return start <= x or x <= end


def end_of(d, typ_="hour"):
    """Return end our hour"""
    if typ_ == "hour":
        return d.replace(minute=59, second=59, microsecond=999999)
    elif typ_ == "day":
        return d.replace(hour=23, minute=59, second=59, microsecond=999999)


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
