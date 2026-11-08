import asyncio
import logging
from collections import defaultdict
from datetime import timedelta, timezone


import aiohttp
import backoff
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
from .misc import AREA_TZINFO, day_coverage, stock

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
        self._today_update_lock = asyncio.Lock()
        self._tomorrow_update_lock = asyncio.Lock()

    async def _update(self, type_="today", dt=None, areas=None) -> bool:
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
        updated = False
        for currency in self.currency:
            spot = AioPrices(currency, client)
            data = await spot.hourly(
                end_date=dt, areas=self.areas if len(self.areas) > 0 else None
            )
            if data:
                self._data[currency][type_] = data["areas"]
                updated = True
        return updated

    def _day_valid_for_currency(
        self, currency, day, target_offset_days, now=None
    ) -> bool:
        """Return whether usable cached prices cover the target delivery day."""
        areas = self._data.get(currency, {}).get(day) or {}
        now = now or dt_utils.now()
        for area in self.areas:
            tzinfo = AREA_TZINFO.get(area)
            if tzinfo is None:
                return False
            target_date = now.astimezone(tzinfo).date() + timedelta(
                days=target_offset_days
            )
            values = (areas.get(area) or {}).get("values") or []
            if not day_coverage(values, target_date, tzinfo):
                return False
        return bool(self.areas)

    def _tomorrow_valid_for_currency(
        self, currency, target_offset_days=1, now=None
    ) -> bool:
        """Check day-ahead data for tomorrow or the midnight promotion target."""
        return self._day_valid_for_currency(
            currency, "tomorrow", target_offset_days, now
        )

    def today_valid(self, now=None) -> bool:
        """Return whether today's usable prices cover all active areas."""
        return bool(self.currency) and all(
            self._day_valid_for_currency(currency, "today", 0, now)
            for currency in self.currency
        )

    def tomorrow_valid(self, now=None) -> bool:
        """Return whether tomorrow prices are complete for all active sensors."""
        return bool(self.currency) and all(
            self._tomorrow_valid_for_currency(currency, now=now)
            for currency in self.currency
        )

    async def update_today(self, areas=None, only_if_missing=False, now=None) -> bool:
        """Update today's prices, optionally skipping an already complete cache."""
        _LOGGER.debug("Updating today's prices.")
        if areas is not None:
            self.areas += [area for area in areas if area not in self.areas]
        async with self._today_update_lock:
            if only_if_missing and self.today_valid(now):
                return False
            return await self._update(
                "today", areas=self.areas if len(self.areas) > 0 else None
            )

    async def update_tomorrow(
        self, areas=None, only_if_missing=False, now=None
    ) -> bool:
        """Update tomorrow's prices."""
        _LOGGER.debug("Updating tomorrow's prices.")
        if areas is not None:
            self.areas += [area for area in areas if area not in self.areas]
        async with self._tomorrow_update_lock:
            if only_if_missing and self.tomorrow_valid(now):
                return False
            return await self._update(
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
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.warning("Initial today price fetch failed: %s", err)
            try:
                await self.update_tomorrow(areas=self.areas)
            except InvalidValueException:
                _LOGGER.debug("No data available for tomorrow, retrying later")
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.warning("Initial tomorrow price fetch failed: %s", err)

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

    async def maybe_refetch_today(self, now) -> bool:
        """Recover missing current-day data without fetching a complete cache."""
        if self.today_valid(now):
            return False

        try:
            updated = await self.update_today(only_if_missing=True, now=now)
        except InvalidValueException:
            _LOGGER.debug("Today prices are not available yet")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Today price retry failed: %s", err)
        else:
            if updated:
                # A partial response can still provide the current interval.
                async_dispatcher_send(self._hass, EVENT_NEW_HOUR)
            return self.today_valid(now)
        return False

    async def maybe_refetch_tomorrow(self, now) -> bool:
        """Retry delayed tomorrow prices after the publication time."""
        stockholm_now = stock(now)
        # Finland changes delivery day before Stockholm. The previous day's
        # publication window must not trigger a fetch for the new tomorrow.
        if any(
            now.astimezone(AREA_TZINFO[area]).date() > stockholm_now.date()
            for area in self.areas
            if area in AREA_TZINFO
        ):
            return False
        publication = stockholm_now.replace(
            hour=13, minute=RANDOM_MINUTE, second=RANDOM_SECOND
        )
        if stockholm_now < publication or self.tomorrow_valid(now):
            return False

        _LOGGER.debug("Tomorrow prices incomplete; retrying the fetch")
        try:
            await self.update_tomorrow(only_if_missing=True, now=now)
        except InvalidValueException:
            _LOGGER.debug("Tomorrow prices are not available yet")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Tomorrow price retry failed: %s", err)
        else:
            if self.tomorrow_valid(now):
                _LOGGER.info("Tomorrow prices are now available")
                async_dispatcher_send(self._hass, EVENT_NEW_PRICE)
                return True
        return False

    async def rotate_to_new_day(self, now, areas=None) -> None:
        """Promote each affected area's complete prices for its new local day."""
        areas = self.areas if areas is None else areas
        needs_today_fetch = False
        promoted = {}
        async with self._today_update_lock, self._tomorrow_update_lock:
            for currency in self.currency:
                promoted[currency] = {}
                today = self._data[currency].setdefault("today", {})
                tomorrow = self._data[currency].get("tomorrow") or {}
                for area in areas:
                    zone = AREA_TZINFO.get(area)
                    data = tomorrow.pop(area, None)
                    values = (data or {}).get("values") or []
                    if zone and day_coverage(values, now.astimezone(zone).date(), zone):
                        today[area] = data
                        promoted[currency][area] = data
                    else:
                        today.pop(area, None)
                        needs_today_fetch = True
                self._data[currency]["tomorrow"] = tomorrow

        if needs_today_fetch:
            try:
                await self.update_today()
            except InvalidValueException:
                _LOGGER.debug("Today prices are not available at day change")
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.warning("Today price fetch failed at day change: %s", err)
            for currency, data in promoted.items():
                self._data[currency].setdefault("today", {}).update(data)


async def _dry_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up using yaml config file."""
    if DOMAIN not in hass.data:
        api = NordpoolData(hass)
        hass.data[DOMAIN] = api
        _LOGGER.debug("Added %s to hass.data", DOMAIN)
        await async_setup_services(hass)

        tick_lock = asyncio.Lock()

        async def update_tick():
            """Run rollover before publication, once per quarter-hour tick."""
            async with tick_lock:
                now = dt_utils.now()
                tick = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
                if api._last_tick == tick:
                    return
                areas = [
                    area
                    for area in api.areas
                    if area in AREA_TZINFO
                    and now.astimezone(AREA_TZINFO[area]).hour == 0
                    and now.minute == 0
                ]
                if areas:
                    await api.rotate_to_new_day(now, areas)
                    async_dispatcher_send(hass, EVENT_NEW_DAY, areas)
                    remaining = [area for area in api.areas if area not in areas]
                    if remaining:
                        async_dispatcher_send(hass, EVENT_NEW_HOUR, remaining)
                else:
                    async_dispatcher_send(hass, EVENT_NEW_HOUR)
                    await api.maybe_refetch_today(dt_utils.now())
                api._last_tick = tick
                await api.maybe_refetch_tomorrow(dt_utils.now())

        async def new_day_cb(now):
            """Check area-local midnight through the shared serialized tick."""
            await update_tick()

        async def new_hr(_):
            """Update cached current prices and retry missing data every 15 minutes."""
            _LOGGER.debug("Called new_hr callback")
            await update_tick()

        @backoff.on_exception(
            backoff.constant,
            (InvalidValueException),
            logger=_LOGGER,
            interval=600,
            max_time=7200,
            jitter=None,
        )
        async def new_data_cb(now):
            """Fetch day-ahead prices at the Europe/Stockholm publication check."""
            try:
                await api.update_tomorrow(only_if_missing=True, now=now)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.warning("Scheduled tomorrow price fetch failed: %s", err)
                return
            if api.tomorrow_valid(now):
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

        cb_new_day = async_track_time_change(hass, new_day_cb, minute=0, second=0)

        cb_new_hr = async_track_time_change(
            hass, new_hr, minute=[0, 15, 30, 45], second=0
        )

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
