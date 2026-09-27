import asyncio
import logging
from collections import defaultdict
from datetime import timedelta

import aiohttp
import backoff
from pytz import timezone
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_utils

from .aio_price import AioPrices, InvalidValueException
from .events import async_track_time_change_in_tz
from .services import async_setup_services
from .misc import stock, day_coverage, AREA_TZINFO

from .const import (
    NAME,
    VERSION,
    ISSUEURL,
    DOMAIN,
    EVENT_NEW_DAY,
    EVENT_NEW_HOUR,
    EVENT_NEW_PRICE,
    _CURRENCY_LIST,
    RANDOM_MINUTE,
    RANDOM_SECOND,
)


STARTUP = f"""
-------------------------------------------------------------------
{NAME}
Version: {VERSION}
This is a custom component
If you have any issues with this you need to open an issue here:
{ISSUEURL}
-------------------------------------------------------------------
"""

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


class NordpoolData:
    """Holds the data"""

    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self._last_tick = None
        self._data = defaultdict(dict)
        self.currency = []
        self.listeners = []
        self.areas = []
        # Throttle for the hourly "if tomorrow is still invalid, try again"
        # self-heal fetch (issue #502: silent failure on empty API response
        # left the sensor without tomorrows prices until a reload).
        self._tomorrow_retry_after = None
        # Same retry state for today's dataset if the midnight fetch failed
        # (code review finding F2: without this, a midnight fetch failure
        # could leave the sensor showing yesterday's price all day).
        self._today_retry_after = None
        # One lock guards all API refreshes (review F4): publication fetch,
        # self-heal retries and day-change fetches must not overlap, or two
        # parallel three-day fetch groups could hammer the API.
        self._update_lock = asyncio.Lock()
        # Snapshot of what the API last served, used to decide whether the
        # today data actually changed before dispatching EVENT_NEW_PRICE.
        self._today_data_hash = None

    def _area_day_target(self, area: str, offset_days: int = 0):
        """Local target date (date object) for a given area, offset from today."""
        zone_name = tzs.get(area)
        if zone_name is None:
            return None
        local_now = stock(dt_utils.now())
        return (local_now + timedelta(days=offset_days)).date()

    async def _update(self, type_="today", dt=None, areas=None):
        _LOGGER.debug("calling _update %s %s %s", type_, dt, areas)
        hass = self._hass
        client = async_get_clientsession(hass)

        if dt is None:
            dt = dt_utils.now()

        if areas is not None:
            self.areas += [area for area in areas if area not in self.areas]
        # We dont really need today and morrow
        # when the region is in another timezone
        # as we request data for 3 days anyway.
        # Keeping this for now, but this should be changed.
        for currency in self.currency:
            spot = AioPrices(currency, client)
            data = await spot.hourly(
                end_date=dt, areas=self.areas if len(self.areas) > 0 else None
            )
            if data:
                self._data[currency][type_] = data["areas"]

    def tomorrow_valid(self) -> bool:
        """Check if we have a full set of tomorrows prices for all currencies.

        Used by the hourly self-heal check so a failed fetch at the
        13:00ish CET publication time (issue #502) is retried instead of
        leaving the sensor without tomorrows prices until a manual reload.
        """
        if not self.currency:
            return False
        return all(
            self._tomorrow_valid_for_currency(currency)
            for currency in self.currency
        )

    def _tomorrow_valid_for_currency(self, currency) -> bool:
        """Check if tomorrows prices for one currency fully cover the target
        day for every registered area (review F1: with the 15-minute MTU a
        day has 96 rows, so the old fixed >= 23 rows check accepted a
        5h45m partial answer as complete)."""
        areas = self._data.get(currency, {}).get("tomorrow") or {}
        for area in self.areas:
            tzinfo = AREA_TZINFO.get(area)
            if tzinfo is None:
                return False
            target = (stock(dt_utils.now()) + timedelta(days=1)).date()
            values = (areas.get(area) or {}).get("values") or []
            if not day_coverage(values, target, tzinfo):
                return False
        return True

    def _today_fresh(self) -> bool:
        """Check if today's prices still cover the current moment for all
        currencies/areas (review F2: midnight fetch failure used to leave
        yesterday's data with no retry for the rest of the day)."""
        if not self.currency:
            return False
        local_now = dt_utils.now()
        for currency in self.currency:
            areas = self._data.get(currency, {}).get("today") or {}
            for area in self.areas:
                values = (areas.get(area) or {}).get("values") or []
                covering = [
                    v for v in values
                    if v.get("start") <= local_now < v.get("end")
                ]
                if not covering:
                    return False
        return True

    async def update_today(self, areas=None):
        """Update today's prices"""
        _LOGGER.debug("Updating today's prices.")
        if areas is not None:
            self.areas += [area for area in areas if area not in self.areas]
        await self._update("today", areas=self.areas if len(self.areas) > 0 else None)

    async def update_tomorrow(self, areas=None):
        """Update tomorrows prices."""
        _LOGGER.debug("Updating tomorrows prices.")
        if areas is not None:
            self.areas += [area for area in areas if area not in self.areas]
        await self._update(
            type_="tomorrow",
            dt=dt_utils.now() + timedelta(hours=24),
            areas=self.areas if len(self.areas) > 0 else None,
        )

    async def _someday(self, area: str, currency: str, day: str):
        """Returns today's or tomorrow's prices in an area in the currency"""
        if currency not in _CURRENCY_LIST:
            raise ValueError(
                "%s is an invalid currency, possible values are %s"
                % (currency, ", ".join(_CURRENCY_LIST))
            )

        if area not in self.areas:
            self.areas.append(area)
        # This is needed as the currency is
        # set in the sensor.
        if currency not in self.currency:
            self.currency.append(currency)
            try:
                await self.update_today(areas=self.areas)
            except InvalidValueException:
                _LOGGER.debug("No data available for today, retrying later")
            try:
                await self.update_tomorrow(areas=self.areas)
            except InvalidValueException:
                _LOGGER.debug("No data available for tomorrow, retrying later")

            # Send a new data request after new data is updated for this first run
            # This way if the user has multiple sensors they will all update
            async_dispatcher_send(self._hass, EVENT_NEW_HOUR)

        return self._data.get(currency, {}).get(day, {}).get(area)

    async def today(self, area: str, currency: str) -> dict:
        """Returns today's prices in an area in the requested currency"""
        return await self._someday(area, currency, "today")

    async def tomorrow(self, area: str, currency: str):
        """Returns tomorrow's prices in an area in the requested currency"""
        return await self._someday(area, currency, "tomorrow")

    async def maybe_refetch_tomorrow(self, now) -> bool:
        """Self-heal for issue #502: refetch tomorrows prices if missing.

        If fetching tomorrows prices at the 13:00ish CET publication time
        silently failed (empty API response), retry at most once every 15
        minutes until the data is available, so a manual reload is not
        needed. Returns True if a valid dataset was fetched.
        """
        stockholm_now = stock(now)
        publication = stockholm_now.replace(
            hour=13, minute=RANDOM_MINUTE, second=RANDOM_SECOND
        )
        if stockholm_now < publication:
            # Before publication, only a missing today dataset needs healing.
            return await self._maybe_refetch_today(now)
        if self.tomorrow_valid():
            # Tomorrow is fine; today may still need healing (e.g. restart
            # after a midnight fetch failure).
            return await self._maybe_refetch_today(now)
        if self._tomorrow_retry_after is not None and now < self._tomorrow_retry_after:
            return await self._maybe_refetch_today(now)

        self._tomorrow_retry_after = now + timedelta(minutes=15)
        _LOGGER.debug("Self-heal: refetching tomorrows prices")
        try:
            async with self._update_lock:
                await self.update_tomorrow()
        except InvalidValueException:
            _LOGGER.debug("Self-heal: no valid tomorrow data yet")
        except aiohttp.ClientError as err:
            _LOGGER.warning("Self-heal: fetch failed: %s", err)
        except Exception:  # pylint: disable=broad-except
            # Review F5: log the full traceback so programming errors are
            # not silently converted into "expected" retries.
            _LOGGER.exception("Self-heal: unexpected error")
        else:
            if self.tomorrow_valid():
                _LOGGER.info("Self-heal: tomorrows prices now available")
                async_dispatcher_send(self._hass, EVENT_NEW_PRICE)
                return True
        return await self._maybe_refetch_today(now)

    async def _maybe_refetch_today(self, now) -> bool:
        """Retry today's dataset if it does not cover the current moment.

        Review F2: a midnight fetch failure used to leave the sensor on
        stale data for the whole day with no retry path. Throttled to one
        fetch per 15 minutes, guarded by the shared update lock.
        """
        if self._today_fresh():
            return False
        if self._today_retry_after is not None and now < self._today_retry_after:
            return False
        self._today_retry_after = now + timedelta(minutes=15)
        _LOGGER.debug("Self-heal: refetching todays prices")
        try:
            async with self._update_lock:
                await self.update_today()
        except InvalidValueException:
            _LOGGER.debug("Self-heal: no valid today data yet")
        except aiohttp.ClientError as err:
            _LOGGER.warning("Self-heal: today fetch failed: %s", err)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Self-heal: unexpected error in today fetch")
        else:
            if self._today_fresh():
                _LOGGER.info("Self-heal: todays prices now available")
                async_dispatcher_send(self._hass, EVENT_NEW_PRICE)
                return True
        return False

    async def rotate_to_new_day(self) -> None:
        """Day-change housekeeping: promote tomorrows data to today.

        If the tomorrow dataset is invalid or missing (publication failed
        the previous day), keep the old today data instead of overwriting
        it with None, and clear the tomorrow slot for the new cycle.

        Review F3: promote per currency, but fetch the missing today data
        only once per rotation (update_today walks all currencies anyway,
        so calling it per invalid currency would cause N x N fetches).
        """
        promoted = {}
        needs_fetch = False
        for curr in self.currency:
            if self._tomorrow_valid_for_currency(curr):
                promoted[curr] = self._data[curr]["tomorrow"]
            else:
                needs_fetch = True
        if needs_fetch:
            try:
                async with self._update_lock:
                    await self.update_today()
            except InvalidValueException:
                _LOGGER.debug("No valid data for today at day change")
            except aiohttp.ClientError as err:
                _LOGGER.warning("Failed to update today at day change: %s", err)
        for curr in self.currency:
            if curr in promoted:
                self._data[curr]["today"] = promoted[curr]
            self._data[curr]["tomorrow"] = {}


async def _dry_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up using yaml config file."""
    if DOMAIN not in hass.data:
        api = NordpoolData(hass)
        hass.data[DOMAIN] = api
        _LOGGER.debug("Added %s to hass.data", DOMAIN)
        await async_setup_services(hass)

        async def new_day_cb(_):
            """Cb to handle some house keeping when it a new day."""
            _LOGGER.debug("Called new_day_cb callback")

            # Reset the self-heal throttles for the new publication cycle.
            api._tomorrow_retry_after = None
            api._today_retry_after = None
            await api.rotate_to_new_day()

            async_dispatcher_send(hass, EVENT_NEW_DAY)

        async def new_hr(_):
            """Callback to tell the sensors to update on a new hour."""
            _LOGGER.debug("Called new_hr callback")

            # Self-heal for issue #502: retry missing tomorrows prices at
            # most once every 15 minutes after publication time. Sensors
            # only read tomorrow data on EVENT_NEW_PRICE, which
            # maybe_refetch_tomorrow() sends once the retry succeeds.
            await api.maybe_refetch_tomorrow(dt_utils.now())

            async_dispatcher_send(hass, EVENT_NEW_HOUR)

        @backoff.on_exception(
            backoff.constant,
            (InvalidValueException),
            logger=_LOGGER,
            interval=600,
            max_time=7200,
            jitter=None,
        )
        async def new_data_cb(_):
            """Callback to fetch new data for tomorrows prices at 1300ish CET
            and notify any sensors, about the new data
            """
            # _LOGGER.debug("Called new_data_cb")
            await api.update_tomorrow()
            async_dispatcher_send(hass, EVENT_NEW_PRICE)

        # Handles futures updates
        cb_update_tomorrow = async_track_time_change_in_tz(
            hass,
            new_data_cb,
            hour=13,
            minute=RANDOM_MINUTE,
            second=RANDOM_SECOND,
            tz=await dt_utils.async_get_time_zone("Europe/Stockholm"),
        )

        cb_new_day = async_track_time_change(
            hass, new_day_cb, hour=0, minute=0, second=0
        )

        cb_new_hr = async_track_time_change(hass, new_hr, minute=[0, 15, 30, 45], second=0)

        api.listeners.append(cb_update_tomorrow)
        api.listeners.append(cb_new_hr)
        api.listeners.append(cb_new_day)

    return True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up using yaml config file."""
    return await _dry_setup(hass, config)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up nordpool as config entry."""
    res = await _dry_setup(hass, entry.data)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.add_update_listener(async_reload_entry)
    return res


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # This is an issue if you have multiple sensors as everything related to DOMAIN
        # is removed, regardless if you have multiple sensors or not. Doesn't seem to
        # create a big issue for now #TODO
        if DOMAIN in hass.data:
            for unsub in hass.data[DOMAIN].listeners:
                unsub()
        hass.data.pop(DOMAIN)

        return True

    return False


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
