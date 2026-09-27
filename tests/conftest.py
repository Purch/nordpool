"""Shared test fixtures: stub the homeassistant modules used by the
nordpool integration so tests run on plain pytest + pytest-asyncio."""

import sys
import types
from datetime import datetime
from unittest.mock import MagicMock

import pytest


def _install_ha_stubs():
    """Install lightweight stub modules for the homeassistant surface the
    nordpool integration imports. Idempotent; returns a dict of the key
    mocks so tests can patch them further."""

    # homeassistant core ---------------------------------------------------
    ha = types.ModuleType("homeassistant")
    ha.__version__ = "2026.9.0"

    # homeassistant.util.dt
    ha_util = types.ModuleType("homeassistant.util")
    ha_util_dt = types.ModuleType("homeassistant.util.dt")

    class _Zone:
        def __init__(self, name):
            self.name = name

        def localize(self, dt):
            return dt.replace(tzinfo=self)

        def normalize(self, dt):
            return dt

        def __repr__(self):
            return f"Zone({self.name})"

    _ZONES = {"Europe/Stockholm": _Zone("Europe/Stockholm")}

    def _now():
        return datetime.now().astimezone()

    ha_util_dt.now = _now
    ha_util_dt.utcnow = _now
    ha_util_dt.as_local = lambda dt: dt
    ha_util_dt.UTC = _Zone("UTC")

    async def _get_zone(name, verify=True):
        return _ZONES.get(name, _Zone(name))

    ha_util_dt.async_get_time_zone = _get_zone

    # homeassistant.core
    ha_core = types.ModuleType("homeassistant.core")

    def _callback(f):
        return f

    ha_core.callback = _callback
    ha_core.HomeAssistant = MagicMock
    ha_core.ServiceCall = MagicMock
    ha_core.SupportsResponse = MagicMock
    ha_core.CALLBACK_TYPE = MagicMock
    ha_core.HassJob = MagicMock
    ha_core.Event = MagicMock

    # homeassistant.loader
    ha_loader = types.ModuleType("homeassistant.loader")
    ha_loader.bind_hass = lambda f: f

    # homeassistant.helpers.dispatcher
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_disp = types.ModuleType("homeassistant.helpers.dispatcher")
    disp_mock = MagicMock()
    ha_disp.async_dispatcher_send = disp_mock
    ha_disp.async_dispatcher_connect = MagicMock(
        return_value=MagicMock()
    )

    # homeassistant.helpers.aiohttp_client
    ha_aiohttp_client = types.ModuleType(
        "homeassistant.helpers.aiohttp_client"
    )
    ha_aiohttp_client.async_get_clientsession = MagicMock(
        return_value=MagicMock()
    )

    # homeassistant.helpers.event
    ha_event = types.ModuleType("homeassistant.helpers.event")
    ha_event.async_track_time_interval = MagicMock(
        return_value=MagicMock()
    )
    ha_event.async_track_point_in_utc_time = MagicMock(
        return_value=MagicMock()
    )
    ha_event.async_track_time_change = MagicMock(return_value=MagicMock())

    # homeassistant.helpers.typing
    ha_typing = types.ModuleType("homeassistant.helpers.typing")
    ha_typing.ConfigType = dict

    # homeassistant.helpers.config_validation
    class _CVModule(types.ModuleType):
        """Stub cv module: any validator name resolves to a no-op that
        passes the value through (optionally configurable)."""

        _passthrough = {
            "date": lambda v: v,
            "ensure_list": lambda v: v if isinstance(v, list) else [v],
            "template": lambda v: v,
            "string": lambda v: v,
            "small_float": lambda v: v,
            "boolean": lambda v: v,
            "datetime": lambda v: v,
            "positive_int": lambda v: v,
            "matches_regex": lambda regex: (lambda v: v),
        }

        def __getattr__(self, name):
            if name in self._passthrough:
                return self._passthrough[name]
            if name.startswith("__"):
                raise AttributeError(name)
            # Generic no-op validator factory / passthrough
            return lambda v: v

    ha_cv = _CVModule("homeassistant.helpers.config_validation")

    # homeassistant.const
    ha_const = types.ModuleType("homeassistant.const")
    ha_const.Platform = type("Platform", (), {"SENSOR": "sensor"})
    ha_const.CONF_REGION = "region"

    # homeassistant.config_entries
    ha_ce = types.ModuleType("homeassistant.config_entries")
    ha_ce.ConfigEntry = MagicMock

    # homeassistant.components.sensor
    ha_comp = types.ModuleType("homeassistant.components")
    ha_comp_sensor = types.ModuleType("homeassistant.components.sensor")
    ha_comp_sensor_const = types.ModuleType(
        "homeassistant.components.sensor.const"
    )
    ha_comp_sensor_const.SensorDeviceClass = type(
        "SensorDeviceClass", (), {"MONETARY": "monetary"}
    )
    ha_comp_sensor_const.SensorStateClass = type(
        "SensorStateClass", (), {"TOTAL": "total"}
    )

    class _SensorEntity:
        """Minimal SensorEntity stand-in."""

        _attr_native_value = None

        def async_write_ha_state(self):
            pass

        async def async_added_to_hass(self):
            """No-op stand-in for Entity.async_added_to_hass."""

        def async_on_remove(self, func):
            """No-op stand-in; tests may override on the instance."""

        @property
        def _attr_suggested_display_precision(self):
            return None

    ha_comp_sensor.SensorEntity = _SensorEntity
    ha_comp_sensor.PLATFORM_SCHEMA = MagicMock()

    # homeassistant.components.sensor import in sensor.py uses:
    #   from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
    #   from homeassistant.components.sensor.const import (...)
    # covered above.

    # homeassistant.helpers.template
    ha_tpl = types.ModuleType("homeassistant.helpers.template")
    ha_tpl.Template = MagicMock

    # homeassistant.exceptions
    ha_exc = types.ModuleType("homeassistant.exceptions")

    # Register everything into sys.modules BEFORE importing the component
    modules = {
        "homeassistant": ha,
        "homeassistant.util": ha_util,
        "homeassistant.util.dt": ha_util_dt,
        "homeassistant.core": ha_core,
        "homeassistant.loader": ha_loader,
        "homeassistant.helpers": ha_helpers,
        "homeassistant.helpers.dispatcher": ha_disp,
        "homeassistant.helpers.aiohttp_client": ha_aiohttp_client,
        "homeassistant.helpers.event": ha_event,
        "homeassistant.helpers.typing": ha_typing,
        "homeassistant.helpers.config_validation": ha_cv,
        "homeassistant.const": ha_const,
        "homeassistant.config_entries": ha_ce,
        "homeassistant.components": ha_comp,
        "homeassistant.components.sensor": ha_comp_sensor,
        "homeassistant.components.sensor.const": ha_comp_sensor_const,
        "homeassistant.helpers.template": ha_tpl,
        "homeassistant.exceptions": ha_exc,
    }
    sys.modules.update(modules)

    return {"dispatcher_send": disp_mock, "ha_util_dt": ha_util_dt}


_INSTALL = _install_ha_stubs()
dispatcher_send = _INSTALL["dispatcher_send"]


@pytest.fixture
def nordpool_init():
    """Import the integration package fresh (with HA stubs in place)."""
    import importlib

    sys.path.insert(0, str(REPO_ROOT / "custom_components"))
    nordpool = importlib.import_module("nordpool")
    nordpool = importlib.reload(nordpool)
    return nordpool


REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent