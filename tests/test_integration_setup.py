"""Set up the integration in a real Home Assistant instance.

Only the network is replaced (`meteoswiss_api`); config entry setup, the
coordinators, entities, registries and services all run for real. Needs
pytest-homeassistant-custom-component (Linux) and runs in CI.
"""

from __future__ import annotations

import dataclasses

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meteoswiss_forecast_warnings.const import (
    CONF_FORECAST_NAME,
    CONF_FORECAST_POINT_TYPE,
    CONF_GROUP_STATION_CODES,
    CONF_POLLEN_STATION_CODE,
    CONF_POLLEN_STATION_NAME,
    CONF_POST_CODE,
    CONF_STATION_CODE,
    CONF_STATION_NAME,
    CONF_WARNINGS_ENABLED,
    DOMAIN,
)
from custom_components.meteoswiss_forecast_warnings.meteo import FORECAST_URL

FULL = {
    CONF_POST_CODE: "800100",
    CONF_FORECAST_POINT_TYPE: "2",
    CONF_FORECAST_NAME: "Zürich",
    CONF_STATION_CODE: "SMA",
    CONF_STATION_NAME: "Zürich / Fluntern ZH",
    CONF_POLLEN_STATION_CODE: "PZH",
    CONF_POLLEN_STATION_NAME: "Zürich-Flughafen ZH",
    CONF_WARNINGS_ENABLED: True,
}


async def _setup(hass: HomeAssistant, data: dict) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        unique_id=f"swissweather_{data[CONF_POST_CODE]}",
        version=2,
        title="MeteoSwiss Zürich",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entities(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, er.RegistryEntry]:
    return {
        e.unique_id: e
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }


async def test_full_setup_creates_three_devices_and_live_entities(hass, meteoswiss_api):
    entry = await _setup(hass, FULL)

    assert entry.state is ConfigEntryState.LOADED
    # Setup aligns device names with the live station metadata.
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert sorted(d.name for d in devices) == ["Zürich", "Zürich / Fluntern ZH", "Zürich ZH"]

    entities = _entities(hass, entry)
    disabled = sorted(uid for uid, e in entities.items() if e.disabled_by is not None)
    assert disabled == sorted(
        [
            "800100.pressure_qnh",
            "800100.time",
            "pollen-800100.pollen-time",
            *(f"800100.warning_{rank}" for rank in range(4, 11)),
        ]
    )
    states = {
        uid: hass.states.get(e.entity_id)
        for uid, e in entities.items()
        if e.disabled_by is None
    }
    assert all(state is not None for state in states.values())
    assert not [uid for uid, s in states.items() if s.state == STATE_UNAVAILABLE]

    # Values come from the recorded payloads.
    assert float(states["800100.temperature"].state) == 16.0
    weather = states["swiss_weather.800100"]
    assert weather.domain == "weather"
    # Current gust and dew point come from the station row (fu3010z1, tde200s0).
    assert weather.attributes["wind_gust_speed"] == 7.6
    assert weather.attributes["dew_point"] == 12.1
    assert "800100.has_active_warnings" in entities
    assert "pollen-800100.birch" in entities


async def test_station_without_qff_enables_qnh_instead(hass, meteoswiss_api):
    """Jungfraujoch reports QNH but no QFF, like most MeteoSwiss stations."""
    entry = await _setup(
        hass, {**FULL, CONF_STATION_CODE: "JUN", CONF_STATION_NAME: "Jungfraujoch VS"}
    )

    entities = _entities(hass, entry)
    assert entities["800100.pressure_qff"].disabled_by is not None
    assert entities["800100.pressure_qnh"].disabled_by is None
    assert float(hass.states.get(entities["800100.pressure_qnh"].entity_id).state) == 1038.4


async def test_wind_and_pressure_from_another_station(hass, meteoswiss_api):
    """Schmerikon measures wind and pressure, Zürich / Fluntern the rest."""
    entry = await _setup(
        hass,
        {
            **FULL,
            CONF_GROUP_STATION_CODES["wind"]: "SCM",
            CONF_GROUP_STATION_CODES["pressure"]: "SCM",
        },
    )
    # Setup fills in the station names from the station list.
    assert entry.data["windStationName"] == "Schmerikon SG"

    entities = _entities(hass, entry)

    def state(key):
        return hass.states.get(entities[f"800100.{key}"].entity_id)

    assert float(state("wind_speed").state) == 16.2
    assert state("wind_speed").attributes["station"] == "Schmerikon SG"
    assert float(state("pressure_qnh").state) == 1000.9
    assert float(state("temperature").state) == 16.0
    assert state("temperature").attributes["station"] == "Zürich / Fluntern ZH"
    # Schmerikon reports QNH but no QFF, so QNH is the enabled sea-level pressure.
    assert entities["800100.pressure_qff"].disabled_by is not None
    # All station sensors stay on the weather station device.
    devices = {e.device_id for uid, e in entities.items() if uid.split(".")[1] in ("wind_speed", "temperature", "pressure_qnh")}
    assert len(devices) == 1

    weather = hass.states.get(entities["swiss_weather.800100"].entity_id)
    assert weather.attributes["wind_speed"] == 16.2
    assert "wind: Schmerikon SG" in weather.attributes["attribution"]


async def test_weather_entity_serves_hourly_and_daily_forecasts(
    hass, meteoswiss_api, freezer
):
    # Hourly forecasts start at the current hour; the recorded payload starts
    # at 2026-10-03 22:00 UTC.
    freezer.move_to("2026-10-03T22:30:00+00:00")
    entry = await _setup(hass, FULL)
    weather_id = _entities(hass, entry)["swiss_weather.800100"].entity_id

    for kind in ("hourly", "daily"):
        response = await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": weather_id, "type": kind},
            blocking=True,
            return_response=True,
        )
        forecast = response[weather_id]["forecast"]
        assert forecast, kind
        assert "temperature" in forecast[0], kind


async def test_forecast_subscribers_receive_updated_forecasts(
    hass, meteoswiss_api, hass_ws_client
):
    """Open forecast cards must get new forecasts, not keep the first one."""
    # Daily forecasts are not filtered by the current time, so the clock can
    # stay real (a frozen clock would also invalidate the websocket token).
    entry = await _setup(hass, FULL)
    weather_id = _entities(hass, entry)["swiss_weather.800100"].entity_id
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {
            "type": "weather/subscribe_forecast",
            "forecast_type": "daily",
            "entity_id": weather_id,
        }
    )
    assert (await client.receive_json())["success"]
    first = (await client.receive_json())["event"]["forecast"]
    assert len(first) > 1

    coordinator = entry.runtime_data.weather_coordinator
    forecast = coordinator.data.forecast
    coordinator.async_set_updated_data(
        dataclasses.replace(
            coordinator.data,
            forecast=dataclasses.replace(
                forecast, dailyForecast=forecast.dailyForecast[:1]
            ),
        )
    )
    await hass.async_block_till_done()

    updated = (await client.receive_json())["event"]["forecast"]
    assert updated == first[:1]


async def test_setup_without_optional_stations_has_only_the_forecast_device(
    hass, meteoswiss_api
):
    data = {
        **FULL,
        CONF_STATION_CODE: None,
        CONF_STATION_NAME: None,
        CONF_POLLEN_STATION_CODE: None,
        CONF_POLLEN_STATION_NAME: None,
    }
    entry = await _setup(hass, data)

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.name for d in devices] == ["Zürich"]
    assert not any(uid.startswith("pollen-") for uid in _entities(hass, entry))


async def test_disabled_warnings_create_no_warning_entities(hass, meteoswiss_api):
    entry = await _setup(hass, {**FULL, CONF_WARNINGS_ENABLED: False})

    assert not [uid for uid in _entities(hass, entry) if "warning" in uid]


async def test_unreachable_forecast_service_retries_setup(hass, meteoswiss_api):
    meteoswiss_api.fail(FORECAST_URL.format("800100"))

    entry = await _setup(hass, FULL)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_and_diagnostics(hass, meteoswiss_api, hass_client):
    from pytest_homeassistant_custom_component.components.diagnostics import (
        get_diagnostics_for_config_entry,
    )

    entry = await _setup(hass, FULL)

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diagnostics["entry"]["data"][CONF_POST_CODE] == "**REDACTED**"
    assert diagnostics["weather"]["warning_snapshot"]["available"] is True

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize("failing", ["forecast_points", "stations", "pollen_stations"])
async def test_setup_survives_unavailable_metadata(hass, meteoswiss_api, failing):
    """Metadata only refines names and repairs; it must not block setup."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.meteoswiss_forecast_warnings.forecast_points import FORECAST_POINT_LIST_URL
    from custom_components.meteoswiss_forecast_warnings.pollen import POLLEN_STATIONS_URL
    from custom_components.meteoswiss_forecast_warnings.station_lookup import STATION_LIST_URL

    url = {
        "forecast_points": FORECAST_POINT_LIST_URL,
        "stations": STATION_LIST_URL,
        "pollen_stations": POLLEN_STATIONS_URL,
    }[failing]
    meteoswiss_api.fail(url)

    entry = await _setup(hass, FULL)

    assert entry.state is ConfigEntryState.LOADED
    # Unknown metadata is not evidence that a configured place disappeared.
    assert not [
        issue for (domain, _), issue in ir.async_get(hass).issues.items() if domain == DOMAIN
    ]


async def test_missing_station_raises_a_repair_issue(hass, meteoswiss_api):
    from homeassistant.helpers import issue_registry as ir

    entry = await _setup(hass, {**FULL, CONF_STATION_CODE: "XXX"})

    assert entry.state is ConfigEntryState.LOADED
    issues = [issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN]
    assert any("station" in issue_id for issue_id in issues), issues


async def test_legacy_entry_gets_its_forecast_point_type(hass, meteoswiss_api):
    data = {key: value for key, value in FULL.items() if key != CONF_FORECAST_POINT_TYPE}

    entry = await _setup(hass, data)

    assert entry.data[CONF_FORECAST_POINT_TYPE] == "2"
