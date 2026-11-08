"""Small Home Assistant surface used by the focused regression tests."""

import sys
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock


def _install_ha_stubs():
    homeassistant = types.ModuleType("homeassistant")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    dt.now = lambda: datetime.now().astimezone()
    dt.utcnow = dt.now
    dt.as_local = lambda value: value

    async def async_get_time_zone(name, verify=True):
        from pytz import timezone

        return timezone(name)

    dt.async_get_time_zone = async_get_time_zone
    dt.parse_time_expression = lambda value, minimum, maximum: value
    dt.find_next_time_expression_time = lambda now, seconds, minutes, hours: now

    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = MagicMock
    core.ServiceCall = MagicMock
    core.SupportsResponse = type("SupportsResponse", (), {"OPTIONAL": "optional"})
    core.CALLBACK_TYPE = MagicMock
    core.HassJob = MagicMock
    core.callback = lambda function: function

    loader = types.ModuleType("homeassistant.loader")
    loader.bind_hass = lambda function: function

    helpers = types.ModuleType("homeassistant.helpers")
    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = MagicMock(return_value=MagicMock())

    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    dispatcher.async_dispatcher_send = MagicMock()
    dispatcher.async_dispatcher_connect = MagicMock(return_value=MagicMock())

    event = types.ModuleType("homeassistant.helpers.event")
    event.async_track_time_change = MagicMock(return_value=MagicMock())
    event.async_track_time_interval = MagicMock(return_value=MagicMock())
    event.async_track_point_in_utc_time = MagicMock(return_value=MagicMock())

    typing = types.ModuleType("homeassistant.helpers.typing")
    typing.ConfigType = dict

    class ConfigValidation(types.ModuleType):
        def __getattr__(self, name):
            validators = {
                "date": lambda value: value,
                "ensure_list": lambda value: value,
                "template": lambda value: value,
                "string": lambda value: value,
                "small_float": lambda value: value,
                "boolean": lambda value: value,
                "positive_int": lambda value: value,
                "matches_regex": lambda regex: (lambda value: value),
            }
            if name in validators:
                return validators[name]
            raise AttributeError(name)

    config_validation = ConfigValidation("homeassistant.helpers.config_validation")

    template = types.ModuleType("homeassistant.helpers.template")
    template.Template = MagicMock

    const = types.ModuleType("homeassistant.const")
    const.Platform = type("Platform", (), {"SENSOR": "sensor"})
    const.CONF_REGION = "region"

    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = MagicMock

    components = types.ModuleType("homeassistant.components")
    sensor = types.ModuleType("homeassistant.components.sensor")
    sensor_const = types.ModuleType("homeassistant.components.sensor.const")
    sensor_const.SensorDeviceClass = type(
        "SensorDeviceClass", (), {"MONETARY": "monetary"}
    )
    sensor_const.SensorStateClass = type("SensorStateClass", (), {"TOTAL": "total"})

    class RestoreSensor:
        _attr_native_value = None

        async def async_added_to_hass(self):
            pass

        async def async_get_last_sensor_data(self):
            return None

        def async_write_ha_state(self):
            pass

    sensor.RestoreSensor = RestoreSensor
    sensor.PLATFORM_SCHEMA = MagicMock()

    modules = {
        "homeassistant": homeassistant,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt,
        "homeassistant.core": core,
        "homeassistant.loader": loader,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.aiohttp_client": aiohttp_client,
        "homeassistant.helpers.dispatcher": dispatcher,
        "homeassistant.helpers.event": event,
        "homeassistant.helpers.typing": typing,
        "homeassistant.helpers.config_validation": config_validation,
        "homeassistant.helpers.template": template,
        "homeassistant.const": const,
        "homeassistant.config_entries": config_entries,
        "homeassistant.components": components,
        "homeassistant.components.sensor": sensor,
        "homeassistant.components.sensor.const": sensor_const,
    }
    sys.modules.update(modules)
    return dispatcher.async_dispatcher_send


DISPATCHER_SEND = _install_ha_stubs()
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "custom_components"))
