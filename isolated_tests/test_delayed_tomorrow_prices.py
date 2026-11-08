"""Regression tests for delayed Nord Pool day-ahead prices."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
import aiohttp

from conftest import DISPATCHER_SEND

import nordpool
import nordpool.sensor as sensor_module
from nordpool.misc import AREA_TZINFO, day_coverage
from nordpool.aio_price import AioPrices, InvalidValueException

HELSINKI = ZoneInfo("Europe/Helsinki")
FIXED_NOW = datetime(2026, 9, 27, 15, 0, tzinfo=HELSINKI)


def make_values(day, *, count=96, value=50.0, zone=HELSINKI):
    current = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(
        timezone.utc
    )
    values = []
    for _ in range(count):
        next_value = current + timedelta(minutes=15)
        values.append(
            {
                "start": current.astimezone(zone),
                "end": next_value.astimezone(zone),
                "value": value,
            }
        )
        current = next_value
    return {"values": values}


def make_api():
    api = nordpool.NordpoolData(MagicMock())
    api.currency = ["EUR"]
    api.areas = ["FI"]
    return api


def make_sensor(*, current_price, native_value):
    sensor = sensor_module.NordpoolSensor.__new__(sensor_module.NordpoolSensor)
    sensor._area = "FI"
    sensor._currency = "EUR"
    sensor._price_type = "kWh"
    sensor._precision = 3
    sensor._low_price_cutoff = 1.0
    sensor._use_cents = False
    sensor._vat = 0
    sensor._current_price = current_price
    sensor._data_today = None
    sensor._data_tomorrow = None
    sensor._average = None
    sensor._max = None
    sensor._min = None
    sensor._mean = None
    sensor._off_peak_1 = None
    sensor._off_peak_2 = None
    sensor._peak = None
    sensor._additional_costs_value = None
    sensor._attr_native_value = native_value
    sensor._ad_template = MagicMock()
    sensor._ad_template.async_render.return_value = 0.0
    sensor._api = MagicMock()
    sensor._api._hass = MagicMock()
    sensor.async_write_ha_state = MagicMock()
    return sensor


def test_complete_day_requires_all_intervals(monkeypatch):
    tomorrow = FIXED_NOW.date() + timedelta(days=1)
    values = make_values(tomorrow)["values"]
    assert day_coverage(values, tomorrow, AREA_TZINFO["FI"]) is True
    assert day_coverage(values[:23], tomorrow, AREA_TZINFO["FI"]) is False

    sensor = sensor_module.NordpoolSensor.__new__(sensor_module.NordpoolSensor)
    sensor._area = "FI"
    monkeypatch.setattr(sensor_module.dt_utils, "now", lambda: FIXED_NOW)
    sensor._data_tomorrow = {"values": values}
    assert sensor.tomorrow_valid is True
    sensor._data_tomorrow = {"values": values[:23]}
    assert sensor.tomorrow_valid is False


@pytest.mark.parametrize(
    ("day", "interval_count"),
    [
        (datetime(2027, 3, 28).date(), 92),
        (datetime(2027, 10, 31).date(), 100),
    ],
)
def test_complete_day_handles_dst(day, interval_count):
    assert (
        day_coverage(
            make_values(day, count=interval_count)["values"],
            day,
            AREA_TZINFO["FI"],
        )
        is True
    )


@pytest.mark.asyncio
async def test_delayed_tomorrow_prices_are_retried(monkeypatch):
    api = make_api()
    tomorrow = FIXED_NOW.date() + timedelta(days=1)

    async def fetch_tomorrow(*args, **kwargs):
        api._data["EUR"]["tomorrow"] = {"FI": make_values(tomorrow)}
        return True

    monkeypatch.setattr(api, "update_tomorrow", fetch_tomorrow)
    DISPATCHER_SEND.reset_mock()

    assert await api.maybe_refetch_tomorrow(FIXED_NOW) is True
    DISPATCHER_SEND.assert_called_once_with(api._hass, nordpool.EVENT_NEW_PRICE)


@pytest.mark.asyncio
async def test_partial_tomorrow_data_keeps_retrying(monkeypatch):
    api = make_api()
    tomorrow = FIXED_NOW.date() + timedelta(days=1)
    api._data["EUR"]["tomorrow"] = {"FI": make_values(tomorrow, count=23)}
    update = AsyncMock(return_value=True)
    monkeypatch.setattr(api, "update_tomorrow", update)

    assert await api.maybe_refetch_tomorrow(FIXED_NOW) is False
    update.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_retries_make_only_one_fetch(monkeypatch):
    api = make_api()
    tomorrow = FIXED_NOW.date() + timedelta(days=1)
    calls = 0

    async def update(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        api._data["EUR"]["tomorrow"] = {"FI": make_values(tomorrow)}
        return True

    monkeypatch.setattr(api, "_update", update)

    results = await asyncio.gather(
        api.update_tomorrow(only_if_missing=True, now=FIXED_NOW),
        api.update_tomorrow(only_if_missing=True, now=FIXED_NOW),
    )

    assert calls == 1
    assert results == [True, False]


@pytest.mark.asyncio
async def test_day_change_never_replaces_today_with_none():
    api = make_api()
    old_today = {"FI": make_values(FIXED_NOW.date(), value=42.0)}
    api._data["EUR"]["today"] = old_today
    api._data["EUR"]["tomorrow"] = {}
    api.update_today = AsyncMock(return_value=None)

    await api.rotate_to_new_day(FIXED_NOW + timedelta(days=1))

    assert api._data["EUR"]["today"] == {}


@pytest.mark.asyncio
async def test_sensor_keeps_last_numeric_price_when_refresh_has_no_data(
    monkeypatch,
):
    sensor = make_sensor(current_price=42.0, native_value=0.042)
    sensor._api.today = AsyncMock(return_value=None)
    sensor._api.tomorrow = AsyncMock(return_value=None)
    monkeypatch.setattr(sensor_module.dt_utils, "now", lambda: FIXED_NOW)

    await sensor.handle_new_hr()

    assert sensor._attr_native_value == 0.042
    sensor.async_write_ha_state.assert_called_once_with()


@pytest.mark.asyncio
async def test_sensor_restores_numeric_price_after_reload(monkeypatch):
    sensor = make_sensor(current_price=None, native_value=None)
    sensor.async_get_last_sensor_data = AsyncMock(
        return_value=SimpleNamespace(native_value=0.123)
    )
    sensor._api.today = AsyncMock(return_value=None)
    sensor._api.tomorrow = AsyncMock(return_value=None)
    monkeypatch.setattr(sensor_module.dt_utils, "now", lambda: FIXED_NOW)

    await sensor.async_added_to_hass()

    assert sensor._attr_native_value == 0.123
    assert sensor.extra_state_attributes["current_price"] == 0.123
    sensor.async_write_ha_state.assert_called_once_with()


async def make_timer_api(monkeypatch):
    """Capture the actual integration callbacks without running an HA timer."""
    hass = MagicMock()
    hass.data = {}
    callbacks = {}

    def track(hass, action, **kwargs):
        callbacks[action.__name__] = action
        return MagicMock()

    monkeypatch.setattr(nordpool, "async_track_time_change", track)
    monkeypatch.setattr(nordpool, "async_track_time_change_in_tz", track)
    await nordpool._dry_setup(hass, {})
    api = hass.data[nordpool.DOMAIN]
    api.currency = ["EUR"]
    api.areas = ["FI"]
    return api, callbacks


@pytest.mark.asyncio
async def test_midnight_promotes_cached_prices_without_fetching(monkeypatch):
    now = (FIXED_NOW + timedelta(days=1)).replace(hour=0, minute=0)
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: now)
    api, callbacks = await make_timer_api(monkeypatch)
    api._data["EUR"]["today"] = {"FI": make_values(FIXED_NOW.date())}
    promoted = {"FI": make_values(now.date(), value=100.0)}
    api._data["EUR"]["tomorrow"] = promoted
    fetch = AsyncMock()
    monkeypatch.setattr(AioPrices, "hourly", fetch)
    sensor = make_sensor(current_price=50.0, native_value=0.05)
    sensor._api = api
    sensor._data_tomorrow = promoted["FI"]

    await callbacks["new_day_cb"](now)
    await sensor.handle_new_day()
    await callbacks["new_hr"](now)

    fetch.assert_not_awaited()
    assert api._data["EUR"]["today"] == {"FI": sensor._data_today}
    assert api._data["EUR"]["tomorrow"] == {}
    assert sensor.raw_today[0]["value"] == 0.1
    assert sensor.raw_tomorrow == []
    assert sensor.tomorrow_valid is False
    assert sensor.current_price == sensor._attr_native_value == 0.1


@pytest.mark.asyncio
async def test_midnight_tick_before_day_change_rolls_over_once(monkeypatch):
    """Either timer owns the shared tick; the second must do nothing."""
    now = (FIXED_NOW + timedelta(days=1)).replace(hour=0, minute=0)
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: now)
    api, callbacks = await make_timer_api(monkeypatch)
    api._data["EUR"]["today"] = {"FI": make_values(FIXED_NOW.date())}
    promoted = {"FI": make_values(now.date(), value=100.0)}
    api._data["EUR"]["tomorrow"] = promoted
    fetch = AsyncMock()
    monkeypatch.setattr(AioPrices, "hourly", fetch)
    DISPATCHER_SEND.reset_mock()

    await callbacks["new_hr"](now)

    fetch.assert_not_awaited()
    DISPATCHER_SEND.assert_called_once_with(api._hass, nordpool.EVENT_NEW_DAY, ["FI"])

    await callbacks["new_day_cb"](now)

    fetch.assert_not_awaited()
    assert api._data["EUR"]["today"]["FI"]["values"][0]["value"] == 100.0
    DISPATCHER_SEND.assert_called_once_with(api._hass, nordpool.EVENT_NEW_DAY, ["FI"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [InvalidValueException("not published"), TimeoutError(), aiohttp.ClientError()],
)
async def test_missing_today_recovers_after_midnight(monkeypatch, failure):
    now = (FIXED_NOW + timedelta(days=1)).replace(hour=0, minute=0)
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: now)
    api, callbacks = await make_timer_api(monkeypatch)
    old_today = {"FI": make_values(FIXED_NOW.date(), value=42.0)}
    api._data["EUR"]["today"] = old_today
    api._data["EUR"]["tomorrow"] = {}
    recovered = {"FI": make_values(now.date(), value=100.0)}
    fetch = AsyncMock(side_effect=[failure, failure, {"areas": recovered}])
    monkeypatch.setattr(AioPrices, "hourly", fetch)
    sensor = make_sensor(current_price=42.0, native_value=0.042)
    sensor._api = api
    DISPATCHER_SEND.reset_mock()

    await callbacks["new_day_cb"](now)
    await sensor.handle_new_day()
    assert api._data["EUR"]["today"] == {}
    assert sensor.current_price == sensor._attr_native_value == 0.042
    assert sensor.raw_tomorrow == []
    DISPATCHER_SEND.assert_called_with(api._hass, nordpool.EVENT_NEW_DAY, ["FI"])

    now += timedelta(minutes=15)
    await callbacks["new_hr"](now)
    await sensor.handle_new_hr()
    assert sensor.current_price == sensor._attr_native_value == 0.042

    now += timedelta(minutes=15)
    await callbacks["new_hr"](now)
    await sensor.handle_new_hr()
    assert fetch.await_count == 3
    assert sensor.current_price == sensor._attr_native_value == 0.1
    assert api._data["EUR"]["today"] is recovered

    now += timedelta(minutes=15)
    await callbacks["new_hr"](now)
    assert fetch.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), aiohttp.ClientError()])
async def test_future_fetch_failure_does_not_block_current_dispatch(
    monkeypatch, failure
):
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: FIXED_NOW)
    api, callbacks = await make_timer_api(monkeypatch)
    api._data["EUR"]["today"] = {"FI": make_values(FIXED_NOW.date())}
    fetch = AsyncMock(side_effect=failure)
    monkeypatch.setattr(AioPrices, "hourly", fetch)
    DISPATCHER_SEND.reset_mock()

    await callbacks["new_hr"](FIXED_NOW)

    DISPATCHER_SEND.assert_called_once_with(api._hass, nordpool.EVENT_NEW_HOUR)
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_future_fetch_dispatches_current_price_first(monkeypatch):
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: FIXED_NOW)
    api, callbacks = await make_timer_api(monkeypatch)
    api._data["EUR"]["today"] = {"FI": make_values(FIXED_NOW.date())}
    started = asyncio.Event()
    finish = asyncio.Event()

    async def fetch(*args, **kwargs):
        started.set()
        await finish.wait()
        return None

    monkeypatch.setattr(AioPrices, "hourly", fetch)
    DISPATCHER_SEND.reset_mock()
    task = asyncio.create_task(callbacks["new_hr"](FIXED_NOW))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert task.done() is False
        DISPATCHER_SEND.assert_called_once_with(api._hass, nordpool.EVENT_NEW_HOUR)
    finally:
        finish.set()
        await task


@pytest.mark.asyncio
async def test_concurrent_today_retries_fetch_once(monkeypatch):
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: FIXED_NOW)
    api = make_api()

    async def fetch(*args, **kwargs):
        await asyncio.sleep(0)
        return {"areas": {"FI": make_values(FIXED_NOW.date())}}

    fetch_mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(AioPrices, "hourly", fetch_mock)

    await asyncio.gather(
        api.maybe_refetch_today(FIXED_NOW),
        api.maybe_refetch_today(FIXED_NOW),
    )

    fetch_mock.assert_awaited_once()
    assert api.today_valid(FIXED_NOW) is True


@pytest.mark.asyncio
async def test_partial_today_keeps_retrying_until_complete(monkeypatch):
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: FIXED_NOW)
    api = make_api()
    partial = make_values(FIXED_NOW.date())
    partial["values"].pop(60)
    api._data["EUR"]["today"] = {"FI": partial}
    fetch = AsyncMock(
        side_effect=[
            {"areas": {"FI": partial}},
            {"areas": {"FI": make_values(FIXED_NOW.date())}},
        ]
    )
    monkeypatch.setattr(AioPrices, "hourly", fetch)

    assert await api.maybe_refetch_today(FIXED_NOW) is False
    assert await api.maybe_refetch_today(FIXED_NOW) is True
    assert await api.maybe_refetch_today(FIXED_NOW) is False
    assert fetch.await_count == 2


@pytest.mark.parametrize(
    "value", [None, True, "50", float("nan"), float("inf"), -float("inf")]
)
def test_complete_day_rejects_invalid_price(value):
    tomorrow = FIXED_NOW.date() + timedelta(days=1)
    values = make_values(tomorrow)["values"]
    values[60]["value"] = value
    assert day_coverage(values, tomorrow, AREA_TZINFO["FI"]) is False


@pytest.mark.parametrize("value", [0.0, -50.0])
def test_complete_day_accepts_zero_and_negative_prices(value):
    tomorrow = FIXED_NOW.date() + timedelta(days=1)
    assert (
        day_coverage(
            make_values(tomorrow, value=value)["values"], tomorrow, AREA_TZINFO["FI"]
        )
        is True
    )


def test_complete_day_rejects_gap_wrong_day_and_naive_timestamps():
    tomorrow = FIXED_NOW.date() + timedelta(days=1)
    values = make_values(tomorrow)["values"]
    assert day_coverage(values[:60] + values[61:], tomorrow, AREA_TZINFO["FI"]) is False
    assert (
        day_coverage(values, tomorrow + timedelta(days=1), AREA_TZINFO["FI"]) is False
    )
    values[0]["start"] = values[0]["start"].replace(tzinfo=None)
    assert day_coverage(values, tomorrow, AREA_TZINFO["FI"]) is False


@pytest.mark.asyncio
async def test_invalid_tomorrow_prices_continue_retrying(monkeypatch):
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: FIXED_NOW)
    api = make_api()
    tomorrow = FIXED_NOW.date() + timedelta(days=1)
    fetch = AsyncMock(
        side_effect=[
            {"areas": {"FI": make_values(tomorrow, value=float("nan"))}},
            {"areas": {"FI": make_values(tomorrow)}},
        ]
    )
    monkeypatch.setattr(AioPrices, "hourly", fetch)

    assert await api.maybe_refetch_tomorrow(FIXED_NOW) is False
    assert await api.maybe_refetch_tomorrow(FIXED_NOW) is True
    assert await api.maybe_refetch_tomorrow(FIXED_NOW) is False
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_restored_price_is_not_converted_twice_and_recovers(monkeypatch):
    now = FIXED_NOW
    monkeypatch.setattr(sensor_module.dt_utils, "now", lambda: now)
    sensor = make_sensor(current_price=None, native_value=None)
    sensor._vat = 0.255
    sensor._use_cents = True
    sensor._ad_template.async_render.return_value = 0.01
    sensor.async_get_last_sensor_data = AsyncMock(
        return_value=SimpleNamespace(native_value=12.345)
    )
    sensor._api.today = AsyncMock(return_value=None)
    sensor._api.tomorrow = AsyncMock(return_value=None)

    await sensor.async_added_to_hass()
    assert sensor.current_price == sensor._attr_native_value == 12.345
    assert sensor.extra_state_attributes["current_price"] == 12.345
    assert sensor._current_price is None

    sensor._api.today.return_value = make_values(now.date(), value=100.0)
    await sensor.handle_new_hr()
    assert sensor.current_price == sensor._attr_native_value == 13.55
    assert sensor._current_price == 100.0

    now += timedelta(days=1)
    sensor._ad_template.async_render.return_value = 0.02
    await sensor.handle_new_hr()
    assert sensor.current_price == sensor._attr_native_value == 13.55


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [None, True, "50", float("nan"), float("inf"), -float("inf")]
)
async def test_invalid_current_interval_preserves_public_price(monkeypatch, value):
    monkeypatch.setattr(sensor_module.dt_utils, "now", lambda: FIXED_NOW)
    sensor = make_sensor(current_price=42.0, native_value=0.042)
    data = make_values(FIXED_NOW.date())
    for item in data["values"]:
        if item["start"] <= FIXED_NOW < item["end"]:
            item["value"] = value
    sensor._api.today = AsyncMock(return_value=data)

    await sensor.handle_new_hr()

    assert sensor.current_price == sensor._attr_native_value == 0.042
    assert sensor.extra_state_attributes["current_price"] == 0.042


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["maybe_refetch_today", "maybe_refetch_tomorrow"])
async def test_retry_does_not_swallow_cancellation(monkeypatch, method):
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: FIXED_NOW)
    api = make_api()
    monkeypatch.setattr(
        AioPrices, "hourly", AsyncMock(side_effect=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        await getattr(api, method)(FIXED_NOW)


@pytest.mark.asyncio
async def test_first_start_without_known_price_does_not_invent_one(monkeypatch):
    monkeypatch.setattr(sensor_module.dt_utils, "now", lambda: FIXED_NOW)
    sensor = make_sensor(current_price=None, native_value=None)
    sensor.async_get_last_sensor_data = AsyncMock(return_value=None)
    sensor._api.today = AsyncMock(return_value=None)
    sensor._api.tomorrow = AsyncMock(return_value=None)

    await sensor.async_added_to_hass()

    assert sensor.current_price is None
    assert sensor._attr_native_value is None
    sensor.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_complete_cache_skips_all_scheduled_fetches(monkeypatch):
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: FIXED_NOW)
    api, callbacks = await make_timer_api(monkeypatch)
    api._data["EUR"]["today"] = {"FI": make_values(FIXED_NOW.date())}
    api._data["EUR"]["tomorrow"] = {
        "FI": make_values(FIXED_NOW.date() + timedelta(days=1))
    }
    fetch = AsyncMock()
    monkeypatch.setattr(AioPrices, "hourly", fetch)

    await callbacks["new_data_cb"](FIXED_NOW)
    await callbacks["new_hr"](FIXED_NOW)
    await callbacks["new_hr"](FIXED_NOW)

    fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("hour,minute", [(0, 0), (0, 45), (1, 0), (14, 0)])
async def test_tomorrow_retry_waits_for_its_own_publication_day(
    monkeypatch, hour, minute
):
    now = FIXED_NOW.replace(hour=hour, minute=minute)
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: now)
    api = make_api()
    fetch = AsyncMock()
    monkeypatch.setattr(AioPrices, "hourly", fetch)

    assert await api.maybe_refetch_tomorrow(now) is False
    fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), aiohttp.ClientError()])
async def test_scheduled_future_failure_is_retried_on_next_tick(monkeypatch, failure):
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: FIXED_NOW)
    api, callbacks = await make_timer_api(monkeypatch)
    api._data["EUR"]["today"] = {"FI": make_values(FIXED_NOW.date())}
    tomorrow = {"FI": make_values(FIXED_NOW.date() + timedelta(days=1))}
    fetch = AsyncMock(side_effect=[failure, {"areas": tomorrow}])
    monkeypatch.setattr(AioPrices, "hourly", fetch)

    await callbacks["new_data_cb"](FIXED_NOW)
    await callbacks["new_hr"](FIXED_NOW)

    assert fetch.await_count == 2
    assert api.tomorrow_valid(FIXED_NOW) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [TimeoutError(), aiohttp.ClientError(), InvalidValueException()]
)
async def test_initial_api_failure_keeps_restored_price(monkeypatch, failure):
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: FIXED_NOW)
    api = nordpool.NordpoolData(MagicMock())
    sensor = make_sensor(current_price=None, native_value=None)
    sensor._api = api
    sensor.async_get_last_sensor_data = AsyncMock(
        return_value=SimpleNamespace(native_value=0.123)
    )
    fetch = AsyncMock(side_effect=failure)
    monkeypatch.setattr(AioPrices, "hourly", fetch)

    await sensor.async_added_to_hass()

    assert fetch.await_count == 2
    assert sensor.current_price == sensor._attr_native_value == 0.123
    assert sensor.extra_state_attributes["current_price"] == 0.123


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [None, True, "50", float("nan"), float("inf"), -float("inf")]
)
async def test_invalid_restored_value_is_not_published(monkeypatch, value):
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: FIXED_NOW)
    sensor = make_sensor(current_price=None, native_value=None)
    sensor.async_get_last_sensor_data = AsyncMock(
        return_value=SimpleNamespace(native_value=value)
    )
    sensor._api.today = AsyncMock(return_value=None)
    sensor._api.tomorrow = AsyncMock(return_value=None)

    await sensor.async_added_to_hass()

    assert sensor.current_price is None
    sensor.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -float("inf")])
async def test_nonfinite_calculated_price_does_not_replace_last_value(
    monkeypatch, cost
):
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: FIXED_NOW)
    sensor = make_sensor(current_price=42.0, native_value=0.042)
    sensor._api.today = AsyncMock(return_value=make_values(FIXED_NOW.date()))
    sensor._ad_template.async_render.return_value = cost

    await sensor.handle_new_hr()

    assert sensor.current_price == sensor._attr_native_value == 0.042


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["new_hr", "new_day_cb", "concurrent"])
@pytest.mark.parametrize(
    "area,ha_zone",
    [("FI", "Europe/Stockholm"), ("SE3", "Europe/Helsinki"), ("FI", "UTC")],
)
async def test_area_midnight_orders_callbacks_once(monkeypatch, first, area, ha_zone):
    zone = ZoneInfo("Europe/Helsinki" if area == "FI" else "Europe/Stockholm")
    local_now = datetime(2026, 11, 9, tzinfo=zone)
    now = local_now.astimezone(ZoneInfo(ha_zone))
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: now)
    api, callbacks = await make_timer_api(monkeypatch)
    api.areas = [area]
    data = make_values(local_now.date(), zone=zone)
    api._data["EUR"]["today"] = {
        area: make_values(local_now.date() - timedelta(days=1), zone=zone)
    }
    api._data["EUR"]["tomorrow"] = {area: data}
    fetch = AsyncMock()
    monkeypatch.setattr(AioPrices, "hourly", fetch)
    DISPATCHER_SEND.reset_mock()
    if first == "concurrent":
        await asyncio.gather(callbacks["new_hr"](now), callbacks["new_day_cb"](now))
    else:
        second = "new_day_cb" if first == "new_hr" else "new_hr"
        await callbacks[first](now)
        await callbacks[second](now)
    fetch.assert_not_awaited()
    assert api._data["EUR"]["today"][area] is data
    DISPATCHER_SEND.assert_called_once_with(api._hass, nordpool.EVENT_NEW_DAY, [area])


@pytest.mark.asyncio
async def test_two_areas_roll_over_at_separate_midnights(monkeypatch):
    now = datetime(2026, 11, 9, tzinfo=HELSINKI)
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: now)
    api, callbacks = await make_timer_api(monkeypatch)
    api.areas = ["FI", "SE3"]
    stockholm = ZoneInfo("Europe/Stockholm")
    fi_new = make_values(now.date())
    se_old = make_values(now.date() - timedelta(days=1), zone=stockholm)
    se_new = make_values(now.date(), zone=stockholm)
    api._data["EUR"]["today"] = {
        "FI": make_values(now.date() - timedelta(days=1)),
        "SE3": se_old,
    }
    api._data["EUR"]["tomorrow"] = {"FI": fi_new, "SE3": se_new}
    fetch = AsyncMock()
    monkeypatch.setattr(AioPrices, "hourly", fetch)
    DISPATCHER_SEND.reset_mock()
    await callbacks["new_hr"](now)
    assert api._data["EUR"]["today"] == {"FI": fi_new, "SE3": se_old}
    assert api._data["EUR"]["tomorrow"] == {"SE3": se_new}
    assert DISPATCHER_SEND.call_args_list[-1].args == (
        api._hass,
        nordpool.EVENT_NEW_HOUR,
        ["SE3"],
    )
    now += timedelta(hours=1)
    await callbacks["new_day_cb"](now)
    assert api._data["EUR"]["today"] == {"FI": fi_new, "SE3": se_new}
    assert api._data["EUR"]["tomorrow"] == {}
    fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "day,count", [(datetime(2027, 3, 28), 92), (datetime(2027, 10, 31), 100)]
)
async def test_area_rollover_promotes_dst_delivery_day(monkeypatch, day, count):
    now = day.replace(tzinfo=HELSINKI)
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: now)
    api, callbacks = await make_timer_api(monkeypatch)
    data = make_values(now.date(), count=count)
    api._data["EUR"]["tomorrow"] = {"FI": data}
    fetch = AsyncMock()
    monkeypatch.setattr(AioPrices, "hourly", fetch)
    await callbacks["new_hr"](now)
    assert api._data["EUR"]["today"]["FI"] is data
    assert api.today_valid(now)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_rollover_clears_old_lists_but_retains_price(monkeypatch):
    now = datetime(2026, 11, 9, tzinfo=HELSINKI)
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: now)
    api, callbacks = await make_timer_api(monkeypatch)
    old = make_values(now.date() - timedelta(days=1))
    api._data["EUR"]["today"] = {"FI": old}
    api._data["EUR"]["tomorrow"] = {"FI": make_values(now.date(), count=23)}
    monkeypatch.setattr(AioPrices, "hourly", AsyncMock(side_effect=TimeoutError()))
    sensor = make_sensor(current_price=42.0, native_value=0.042)
    sensor._api = api
    sensor._data_today = old
    sensor._average = 123
    await callbacks["new_hr"](now)
    await sensor.handle_new_day(["FI"])
    assert sensor.raw_today == []
    assert sensor.raw_tomorrow == []
    assert sensor._average is None
    assert sensor.current_price == 0.042
    sensor.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_area_notifications_ignore_other_sensors():
    sensor = make_sensor(current_price=42.0, native_value=0.042)
    await sensor.handle_new_day(["SE3"])
    await sensor.handle_new_hr(["SE3"])
    sensor._api.today.assert_not_called()
    sensor.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_partial_rollover_recovery_keeps_complete_promoted_area(monkeypatch):
    now = datetime(2026, 11, 9, tzinfo=HELSINKI)
    monkeypatch.setattr(nordpool.dt_utils, "now", lambda: now)
    api = make_api()
    api.areas = ["FI", "EE"]
    complete = make_values(now.date())
    partial = make_values(now.date(), count=1)
    api._data["EUR"]["tomorrow"] = {"FI": complete}
    monkeypatch.setattr(
        AioPrices, "hourly", AsyncMock(return_value={"areas": {"EE": partial}})
    )
    await api.rotate_to_new_day(now)
    assert api._data["EUR"]["today"] == {"FI": complete, "EE": partial}
    assert not api.today_valid(now)
