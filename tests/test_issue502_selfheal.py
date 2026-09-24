"""Unit tests for the issue #502 self-heal patch.

Covers:
- NordpoolData.tomorrow_valid() detection of missing/partial data
- NordpoolData.maybe_refetch_tomorrow() throttled retry after publication
- NordpoolData.rotate_to_new_day() not wiping good today data
- NordpoolSensor.handle_new_hr() state-hold when price data is missing
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "custom_components"

sys.path.insert(0, str(COMPONENT_DIR))

from conftest import dispatcher_send  # noqa: E402


def _make_area_values(count=24, value=50.0, start_day=None):
    """Build a values list shaped like AioPrices output.

    Datetimes are timezone-aware (the integration compares them against
    dt_utils.now()); the default start covers the current local day so
    "now" always falls inside the generated hours.
    """
    if start_day is None:
        start_day = datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:
        start_day = start_day.astimezone()
    values = []
    for i in range(count):
        start = start_day + timedelta(hours=i)
        values.append(
            {
                "start": start,
                "end": start + timedelta(hours=1),
                "value": value,
            }
        )
    return {"values": values}


@pytest.fixture
def init_mod():
    import importlib

    nordpool = importlib.import_module("nordpool")
    nordpool = importlib.reload(nordpool)
    return nordpool


@pytest.fixture
def sensor_mod():
    import importlib

    nordpool = importlib.import_module("nordpool")
    nordpool = importlib.reload(nordpool)
    sensor = importlib.import_module("nordpool.sensor")
    sensor = importlib.reload(sensor)
    return sensor


@pytest.fixture
def api(init_mod):
    a = init_mod.NordpoolData(MagicMock())
    a.currency = ["EUR"]
    a.areas = ["FI"]
    return a


# ---------------------------------------------------------------------------
# tomorrow_valid
# ---------------------------------------------------------------------------


def test_tomorrow_valid_full_data(api):
    api._data["EUR"]["tomorrow"] = {"FI": _make_area_values(24)}
    assert api.tomorrow_valid() is True


def test_tomorrow_valid_missing(api):
    api._data["EUR"]["tomorrow"] = {}
    assert api.tomorrow_valid() is False


def test_tomorrow_valid_partial(api):
    """A 4-value partial dataset (issue #528) must not count as valid."""
    api._data["EUR"]["tomorrow"] = {"FI": _make_area_values(4)}
    assert api.tomorrow_valid() is False


def test_tomorrow_valid_no_currency(init_mod):
    a = init_mod.NordpoolData(MagicMock())
    assert a.tomorrow_valid() is False


def test_tomorrow_valid_multiple_areas(api):
    api.areas = ["FI", "SE1"]
    api._data["EUR"]["tomorrow"] = {"FI": _make_area_values(24)}
    assert api.tomorrow_valid() is False  # SE1 missing
    api._data["EUR"]["tomorrow"]["SE1"] = _make_area_values(24)
    assert api.tomorrow_valid() is True


# ---------------------------------------------------------------------------
# maybe_refetch_tomorrow
# ---------------------------------------------------------------------------


def _after_publication():
    """A datetime clearly after the 13:xx Stockholm publication window."""
    return datetime(2026, 9, 24, 15, 0, 0).astimezone()


def _before_publication():
    return datetime(2026, 9, 24, 10, 0, 0).astimezone()


@pytest.mark.asyncio
async def test_refetch_not_before_publication(api):
    api.update_tomorrow = AsyncMock()
    assert await api.maybe_refetch_tomorrow(_before_publication()) is False
    api.update_tomorrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_refetch_fetches_when_missing(api):
    async def _fetch_ok(areas=None):
        api._data["EUR"]["tomorrow"] = {"FI": _make_area_values(24)}

    api.update_tomorrow = AsyncMock(side_effect=_fetch_ok)
    assert await api.maybe_refetch_tomorrow(_after_publication()) is True
    api.update_tomorrow.assert_awaited_once()
    assert dispatcher_send.called


@pytest.mark.asyncio
async def test_refetch_throttled(api):
    async def _fetch_no_data(areas=None):
        pass  # fetch runs but data stays missing

    api.update_tomorrow = AsyncMock(side_effect=_fetch_no_data)
    now = _after_publication()
    await api.maybe_refetch_tomorrow(now)
    assert api.update_tomorrow.await_count == 1
    # 5 min later: throttled
    await api.maybe_refetch_tomorrow(now + timedelta(minutes=5))
    assert api.update_tomorrow.await_count == 1
    # 16 min later: retries
    await api.maybe_refetch_tomorrow(now + timedelta(minutes=16))
    assert api.update_tomorrow.await_count == 2


@pytest.mark.asyncio
async def test_refetch_skipped_when_valid(api):
    api._data["EUR"]["tomorrow"] = {"FI": _make_area_values(24)}
    api.update_tomorrow = AsyncMock()
    assert await api.maybe_refetch_tomorrow(_after_publication()) is False
    api.update_tomorrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_refetch_handles_invalid_value_exception(api):
    from nordpool.aio_price import InvalidValueException

    api.update_tomorrow = AsyncMock(side_effect=InvalidValueException("junk"))
    now = _after_publication()
    assert await api.maybe_refetch_tomorrow(now) is False
    assert api.update_tomorrow.await_count == 1
    # Retry after the throttle interval
    assert await api.maybe_refetch_tomorrow(now + timedelta(minutes=16)) is False
    assert api.update_tomorrow.await_count == 2


@pytest.mark.asyncio
async def test_refetch_handles_aiohttp_error(api):
    import aiohttp

    api.update_tomorrow = AsyncMock(side_effect=aiohttp.ClientError("down"))
    assert await api.maybe_refetch_tomorrow(_after_publication()) is False
    assert api.update_tomorrow.await_count == 1


@pytest.mark.asyncio
async def test_refetch_unexpected_error_swallowed(api):
    api.update_tomorrow = AsyncMock(side_effect=RuntimeError("boom"))
    assert await api.maybe_refetch_tomorrow(_after_publication()) is False
    assert api.update_tomorrow.await_count == 1


# ---------------------------------------------------------------------------
# rotate_to_new_day
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotate_promotes_valid_tomorrow(api):
    api._data["EUR"]["today"] = {"FI": _make_area_values(24, value=10.0)}
    api._data["EUR"]["tomorrow"] = {
        "FI": _make_area_values(24, value=20.0, start_day=datetime(2026, 9, 25))
    }
    await api.rotate_to_new_day()
    assert api._data["EUR"]["today"]["FI"]["values"][0]["value"] == 20.0
    assert api._data["EUR"]["tomorrow"] == {}


@pytest.mark.asyncio
async def test_rotate_keeps_today_when_tomorrow_invalid(api):
    """Issue #528 regression: invalid/partial tomorrow must not wipe the
    existing today data."""
    api._data["EUR"]["today"] = {"FI": _make_area_values(24, value=10.0)}
    api._data["EUR"]["tomorrow"] = {"FI": _make_area_values(4, value=20.0)}
    api.update_today = AsyncMock()  # fetch succeeds but changes nothing

    await api.rotate_to_new_day()
    api.update_today.assert_awaited_once()
    assert api._data["EUR"]["today"]["FI"]["values"][0]["value"] == 10.0
    assert len(api._data["EUR"]["today"]["FI"]["values"]) == 24
    assert api._data["EUR"]["tomorrow"] == {}


@pytest.mark.asyncio
async def test_rotate_today_fetch_failure_keeps_old_data(api):
    """If update_today itself fails, old today data must survive."""
    import aiohttp

    api._data["EUR"]["today"] = {"FI": _make_area_values(24, value=10.0)}
    api._data["EUR"]["tomorrow"] = {}
    api.update_today = AsyncMock(side_effect=aiohttp.ClientError("down"))

    await api.rotate_to_new_day()
    assert api._data["EUR"]["today"]["FI"]["values"][0]["value"] == 10.0


# ---------------------------------------------------------------------------
# Sensor state-hold
# ---------------------------------------------------------------------------


def _make_sensor(sensor_mod, *, current_price, data_today):
    sensor = sensor_mod.NordpoolSensor.__new__(sensor_mod.NordpoolSensor)
    sensor._area = "FI"
    sensor._currency = "EUR"
    sensor._price_type = "kWh"
    sensor._precision = 3
    sensor._low_price_cutoff = 1.0
    sensor._use_cents = False
    sensor._vat = 0
    sensor._api = MagicMock()
    sensor._hass = MagicMock()
    sensor._current_price = current_price
    sensor._data_today = data_today
    sensor._data_tomorrow = None
    sensor._average = None
    sensor._max = None
    sensor._min = None
    sensor._mean = None
    sensor._off_peak_1 = None
    sensor._off_peak_2 = None
    sensor._peak = None
    sensor._additional_costs_value = None
    sensor._attr_native_value = current_price
    sensor._ad_template = MagicMock()
    sensor._ad_template.async_render = MagicMock(return_value=0.0)
    sensor.async_write_ha_state = MagicMock()
    return sensor


@pytest.mark.asyncio
async def test_sensor_state_hold(sensor_mod):
    """handle_new_hr must keep the last known price instead of writing
    unavailable when no data can be refreshed."""
    sensor = _make_sensor(
        sensor_mod,
        current_price=42.0,
        data_today={"FI": _make_area_values(24, value=42.0)},
    )

    async def _today_none(area, currency):
        return None

    async def _tomorrow_none(area, currency):
        return None

    sensor._api.today = _today_none
    sensor._api.tomorrow = _tomorrow_none

    await sensor_mod.NordpoolSensor.handle_new_hr.__get__(sensor)()
    sensor.async_write_ha_state.assert_called_once()
    # _calc_price converts: value/1000 * (1+VAT); 42.0 MWh -> 0.042 kWh
    assert sensor._attr_native_value == pytest.approx(42.0 / 1000)


@pytest.mark.asyncio
async def test_sensor_never_had_price_writes_no_state(sensor_mod):
    """Fresh sensor without any data: no state write (same as upstream,
    entity will show unknown until first successful fetch)."""
    sensor = _make_sensor(sensor_mod, current_price=None, data_today=None)

    async def _today_none(area, currency):
        return None

    async def _tomorrow_none(area, currency):
        return None

    sensor._api.today = _today_none
    sensor._api.tomorrow = _tomorrow_none

    await sensor_mod.NordpoolSensor.handle_new_hr.__get__(sensor)()
    sensor.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_sensor_updates_normally_when_data(sensor_mod):
    """With data available the state must update normally."""
    sensor = _make_sensor(
        sensor_mod,
        current_price=42.0,
        data_today={"FI": _make_area_values(24, value=50.0)},
    )

    async def _today_ok(area, currency):
        return _make_area_values(24, value=50.0)

    async def _tomorrow_none(area, currency):
        return None

    sensor._api.today = _today_ok
    sensor._api.tomorrow = _tomorrow_none

    await sensor_mod.NordpoolSensor.handle_new_hr.__get__(sensor)()
    sensor.async_write_ha_state.assert_called_once()
    # 50.0 EUR/MWh -> kWh with VAT 0: 50.0 / 1000 = 0.05
    assert sensor._attr_native_value == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Regression: new_day_cb no longer assigns None to today
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_day_cb_does_not_wipe_today(api):
    """The old new_day_cb did: today = await update_today() -> today became
    None on a failed fetch. rotate_to_new_day must not reproduce that."""
    api._data["EUR"]["today"] = {"FI": _make_area_values(24, value=10.0)}
    api._data["EUR"]["tomorrow"] = {}
    api.update_today = AsyncMock(return_value=None)  # failed fetch
    await api.rotate_to_new_day()
    # today data survived
    assert api._data["EUR"]["today"]["FI"]["values"][0]["value"] == 10.0