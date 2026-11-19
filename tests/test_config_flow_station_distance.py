"""Station dropdown: distance labels and ordering when coordinates are missing."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("homeassistant")

from custom_components.meteoswiss_forecast_warnings.config_flow import ConfigFlow
from custom_components.meteoswiss_forecast_warnings.const import (
    CONF_FORECAST_POINT_ID,
    CONF_POST_CODE,
)
from custom_components.meteoswiss_forecast_warnings.forecast_points import ForecastPoint
from custom_components.meteoswiss_forecast_warnings.station_lookup import WeatherStation

TEMPERATURE = frozenset({"Temperature"})
NEAR = WeatherStation("Zürich / Kloten", "KLO", 426, 47.48, 8.54, "ZH", TEMPERATURE)
FAR = WeatherStation("Lugano", "LUG", 273, 46.0, 8.96, "TI", TEMPERATURE)
# Pollen metadata can lack coordinates; the dataclass does not enforce floats.
NO_COORDS = WeatherStation("Unbekannt", "UNK", None, None, None, "ZH")


def _flow(latitude, longitude) -> ConfigFlow:
    flow = ConfigFlow()
    flow.hass = SimpleNamespace(
        config=SimpleNamespace(latitude=latitude, longitude=longitude)
    )
    return flow


def test_station_without_coordinates_is_listed_last_without_distance():
    """Regression: a pollen station without coordinates crashed the dropdown."""
    flow = _flow(47.46, 8.44)
    flow._async_get_pollen_stations = AsyncMock(return_value=[NO_COORDS, FAR, NEAR])

    options = asyncio.run(flow._get_pollen_station_options())

    codes = [option["value"] for option in options[1:]]
    assert codes == ["KLO", "LUG", "UNK"]
    assert options[-1]["label"] == "Unbekannt (ZH)"
    assert options[1]["label"] == "Zürich / Kloten (ZH) · 7.9 km · 426 m"


def test_unknown_home_location_shows_no_distance():
    """Regression: an unknown HA location showed every station as "999 km away"."""
    flow = _flow(None, None)

    assert flow.format_station_name_for_dropdown(NEAR) == "Zürich / Kloten (ZH) · 426 m"


FLUMS = ForecastPoint("789000", "2", "8890", "Flums", "ZIP", 441, 47.09, 9.34)
WALENSTADT = WeatherStation("Walenstadt", "WAE", 424, 47.12, 9.31, "SG", TEMPERATURE)


def _flow_at_home(source: str = "user", entry_data: dict | None = None) -> ConfigFlow:
    """A flow whose Home Assistant location is near Kloten, far from Flums."""
    flow = _flow(47.46, 8.44)
    flow.context = {"source": source}
    flow._async_get_forecast_points = AsyncMock(return_value=[FLUMS])
    flow._async_get_weather_stations = AsyncMock(return_value=[NEAR, WALENSTADT, FAR])
    if entry_data is not None:
        flow._get_reconfigure_entry = lambda: SimpleNamespace(data=entry_data)
    return flow


async def _codes(flow: ConfigFlow) -> list[str]:
    flow._reference_location = await flow._async_reference_location()
    return [option["value"] for option in (await flow._get_weather_station_options())[1:]]


def test_stations_are_sorted_by_distance_to_the_chosen_place():
    flow = _flow_at_home()
    flow._selected_forecast_point_id = "789000"

    assert asyncio.run(_codes(flow)) == ["WAE", "KLO", "LUG"]
    assert flow.format_station_name_for_dropdown(WALENSTADT) == "Walenstadt (SG) · 4.0 km · 424 m"


def test_reconfigure_measures_from_the_entry_forecast_point():
    flow = _flow_at_home(
        "reconfigure", {CONF_POST_CODE: "8890", CONF_FORECAST_POINT_ID: "789000"}
    )

    assert asyncio.run(_codes(flow)) == ["WAE", "KLO", "LUG"]


def test_unknown_place_falls_back_to_the_home_location():
    flow = _flow_at_home()
    flow._selected_forecast_point_id = "999999"

    assert asyncio.run(_codes(flow)) == ["KLO", "WAE", "LUG"]


def test_canton_in_the_station_name_is_shown_once():
    flow = _flow(None, None)
    buchs = WeatherStation("Buchs, SG", "PBU", 446, None, None, "SG")

    assert flow.format_station_name_for_dropdown(buchs) == "Buchs (SG) · 446 m"


def test_unavailable_forecast_points_fall_back_to_the_home_location():
    from custom_components.meteoswiss_forecast_warnings.forecast_points import (
        ForecastPointMetadataLoadError,
    )

    flow = _flow_at_home()
    flow._selected_forecast_point_id = "789000"
    flow._async_get_forecast_points = AsyncMock(
        side_effect=ForecastPointMetadataLoadError("offline")
    )

    assert asyncio.run(_codes(flow)) == ["KLO", "WAE", "LUG"]
