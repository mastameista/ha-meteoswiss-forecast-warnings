"""Measurement groups taken from another station than the weather station.

Uses the recorded current measurements: Zürich / Fluntern (SMA) measures
everything, Schmerikon (SCM) only wind and pressure (sea level as QNH, no
QFF), the case these groups exist for.
"""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("homeassistant")

from custom_components.meteoswiss_forecast_warnings import (
    coordinator as coordinator_module,
    station_lookup,
)
from custom_components.meteoswiss_forecast_warnings.const import (
    CONF_GROUP_STATION_CODES,
    CONF_GROUP_STATION_NAMES,
    CONF_POST_CODE,
    CONF_STATION_CODE,
    STATION_GROUPS,
)
from custom_components.meteoswiss_forecast_warnings.coordinator import (
    SwissWeatherDataCoordinator,
)
from custom_components.meteoswiss_forecast_warnings.entity import group_station_names
from custom_components.meteoswiss_forecast_warnings.meteo import (
    STATION_GROUP_FIELDS,
    CurrentWeather,
    MeteoClient,
    apply_station_groups,
)
from custom_components.meteoswiss_forecast_warnings.sensor import (
    SENSOR_STATION_GROUPS,
    SENSORS,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _client() -> tuple[MeteoClient, AsyncMock]:
    rows = list(
        csv.DictReader(
            (FIXTURES / "vqha80_sample.csv").read_text(encoding="utf-8").splitlines(),
            delimiter=";",
        )
    )
    client = MeteoClient(MagicMock(), "en")
    download = AsyncMock(return_value=rows)
    client._async_get_csv_dictionary_for_url = download  # type: ignore[method-assign]
    return client, download


def _stations() -> dict[str, CurrentWeather | None]:
    client, _ = _client()
    return asyncio.run(client.async_get_current_weather_for_stations(["SMA", "SCM"]))


def test_one_download_serves_every_station():
    client, download = _client()

    weather = asyncio.run(
        client.async_get_current_weather_for_stations(["SMA", "scm", "NOPE"])
    )

    download.assert_awaited_once()
    assert set(weather) == {"SMA", "scm", "NOPE"}
    assert weather["SMA"].airTemperature == (16.0, "°C")
    assert weather["scm"].windSpeed == (16.2, "km/h")
    assert weather["NOPE"] is None


def test_a_group_takes_its_values_only_from_its_station():
    stations = _stations()

    current = apply_station_groups(
        stations["SMA"], {"wind": stations["SCM"], "pressure": stations["SCM"]}
    )

    assert current.station == "SMA"
    assert current.airTemperature == (16.0, "°C")
    assert current.windSpeed == (16.2, "km/h")
    assert current.gustPeak1s == (28.8, "km/h")
    assert current.windDirection == (300.0, "°")
    assert current.pressureStationLevel == (953.3, "hPa")
    assert current.pressureSeaLevelAtStandardAtmosphere == (1000.9, "hPa")
    # Schmerikon reports no QFF. The value of Fluntern must not appear as if
    # Schmerikon had measured it.
    assert current.pressureSeaLevel == (None, "hPa")


def test_a_group_station_without_data_gives_no_values():
    stations = _stations()

    current = apply_station_groups(stations["SMA"], {"wind": None})

    assert current.windSpeed == (None, "km/h")
    assert current.gustPeak1s == (None, "km/h")
    assert current.airTemperature == (16.0, "°C")


def test_groups_cover_current_weather_fields_and_station_sensors():
    fields = set(CurrentWeather.__dataclass_fields__)
    assert set(STATION_GROUP_FIELDS) == set(STATION_GROUPS)
    for group_fields in STATION_GROUP_FIELDS.values():
        assert set(group_fields) <= fields
    # Every measured value belongs to a group; the measurement time belongs
    # to the weather station itself.
    assert set(SENSOR_STATION_GROUPS) == {entry.key for entry in SENSORS} - {"time"}
    assert set(SENSOR_STATION_GROUPS.values()) == set(STATION_GROUPS)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({}, {}),
        # Groups only apply with a weather station.
        ({CONF_GROUP_STATION_CODES["wind"]: "SCM"}, {}),
        (
            {
                CONF_STATION_CODE: "SMA",
                CONF_GROUP_STATION_CODES["wind"]: "SCM",
                CONF_GROUP_STATION_NAMES["wind"]: "Schmerikon SG",
                # The weather station itself is no other station.
                CONF_GROUP_STATION_CODES["temperature"]: "SMA",
                # A cleared group is stored as None.
                CONF_GROUP_STATION_CODES["sunshine"]: None,
                # Without a stored name the code stands in.
                CONF_GROUP_STATION_CODES["pressure"]: "QUI",
            },
            {"wind": "Schmerikon SG", "pressure": "QUI"},
        ),
    ],
)
def test_group_station_names(data, expected):
    assert group_station_names(data) == expected


def test_station_list_offers_stations_without_temperature_but_no_rain_gauges():
    response = MagicMock()
    response.text = AsyncMock(
        return_value=(FIXTURES / "weather_stations.csv").read_text(encoding="latin-1")
    )

    async def fake_get_with_retry(session, url, *, logger, response_handler, timeout):
        return await response_handler(response)

    with patch.object(station_lookup, "async_get_with_retry", fake_get_with_retry):
        stations = asyncio.run(
            station_lookup.async_load_weather_station_list(object(), raise_on_error=True)
        )

    by_code = {station.code: station for station in stations}
    # Precipitation stations are missing from the current measurements.
    assert "AFI" not in by_code
    schmerikon = by_code["SCM"]
    assert schmerikon.measures("Wind")
    assert schmerikon.measures("Pressure")
    assert not schmerikon.measures("Temperature")
    assert by_code["SMA"].measures("Temperature")
    assert station_lookup.station_display_name(schmerikon) == "Schmerikon SG"


class _FakeConfigEntry:
    def __init__(self, data: dict) -> None:
        self.data = data

    def async_on_unload(self, _callback) -> None:
        return None


def _coordinator(monkeypatch, data: dict, stations=None, error=None):
    requested: list[list[str]] = []
    forecast = SimpleNamespace(
        current=SimpleNamespace(currentTemperature=(9.0, "°C")), warnings=[]
    )

    class _FakeMeteoClient:
        def __init__(self, session, language="en"):
            pass

        async def async_get_current_weather_for_stations(self, station_codes):
            requested.append(list(station_codes))
            if error is not None:
                raise error
            return {code: (stations or {}).get(code) for code in station_codes}

        async def async_get_forecast(self, post_code, forecast_point_type):
            return forecast

    monkeypatch.setattr(coordinator_module, "async_get_clientsession", lambda hass: object())
    monkeypatch.setattr(coordinator_module, "MeteoClient", _FakeMeteoClient)
    coordinator = SwissWeatherDataCoordinator(
        SimpleNamespace(config=SimpleNamespace(language="en")),
        _FakeConfigEntry({CONF_POST_CODE: "800100", **data}),
    )
    return coordinator, requested


def test_coordinator_takes_groups_from_their_stations(monkeypatch):
    coordinator, requested = _coordinator(
        monkeypatch,
        {
            CONF_STATION_CODE: "SMA",
            CONF_GROUP_STATION_CODES["wind"]: "SCM",
            CONF_GROUP_STATION_CODES["temperature"]: "SMA",
            CONF_GROUP_STATION_CODES["sunshine"]: None,
        },
        stations=_stations(),
    )

    current = asyncio.run(coordinator._async_update_data()).current_weather

    # One download for the weather station and the wind station.
    assert requested == [["SMA", "SCM"]]
    assert current.windSpeed == (16.2, "km/h")
    assert current.airTemperature == (16.0, "°C")
    assert current.pressureSeaLevel == (1027.8, "hPa")


def test_coordinator_ignores_groups_without_a_weather_station(monkeypatch):
    coordinator, requested = _coordinator(
        monkeypatch, {CONF_GROUP_STATION_CODES["wind"]: "SCM"}, stations=_stations()
    )

    current = asyncio.run(coordinator._async_update_data()).current_weather

    assert requested == []
    assert current.airTemperature == (9.0, "°C")
    assert current.windSpeed is None


def test_failed_download_keeps_the_forecast_temperature(monkeypatch):
    """A temperature group must not blank out the forecast fallback."""
    from custom_components.meteoswiss_forecast_warnings.meteo import (
        MeteoSwissConnectionError,
    )

    coordinator, _ = _coordinator(
        monkeypatch,
        {CONF_STATION_CODE: "SMA", CONF_GROUP_STATION_CODES["temperature"]: "SCM"},
        error=MeteoSwissConnectionError("offline"),
    )

    current = asyncio.run(coordinator._async_update_data()).current_weather

    assert current.airTemperature == (9.0, "°C")


def test_missing_weather_station_row_still_applies_groups(monkeypatch):
    """The forecast stands in for the weather station; the groups still apply."""
    stations = _stations()
    coordinator, _ = _coordinator(
        monkeypatch,
        {CONF_STATION_CODE: "GONE", CONF_GROUP_STATION_CODES["wind"]: "SCM"},
        stations={"SCM": stations["SCM"]},
    )

    current = asyncio.run(coordinator._async_update_data()).current_weather

    assert current.airTemperature == (9.0, "°C")
    assert current.windSpeed == (16.2, "km/h")
