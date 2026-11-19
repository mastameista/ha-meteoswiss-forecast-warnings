"""Helpers to load and resolve station metadata."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import logging

from aiohttp import ClientError, ClientResponse, ClientSession

from .naming import format_station_display_name
from .pollen import PollenClient, PollenClientError
from .request import REQUEST_TIMEOUT, async_get_with_retry

_LOGGER = logging.getLogger(__name__)

STATION_LIST_URL = "https://data.geo.admin.ch/ch.meteoschweiz.messnetz-automatisch/ch.meteoschweiz.messnetz-automatisch_en.csv"


class WeatherStationMetadataLoadError(Exception):
    """Raised when weather station metadata cannot be loaded reliably."""


class PollenStationMetadataLoadError(Exception):
    """Raised when pollen station metadata cannot be loaded reliably."""


@dataclass
class WeatherStation:
    """Describes a single weather or pollen station."""

    name: str
    code: str
    altitude: int | None
    lat: float | None
    lng: float | None
    canton: str | None
    # What the station measures, as named in the station list ("Temperature",
    # "Wind", ...). Empty for pollen stations.
    measurements: frozenset[str] = frozenset()

    def measures(self, measurement: str) -> bool:
        """Return whether the station measures the given quantity."""
        return measurement in self.measurements


def _int_or_none(val: str | None) -> int | None:
    """Parse an optional CSV number; empty or unreadable fields give None."""
    number = _float_or_none(val)
    return None if number is None else int(number)


def _float_or_none(val: str | None) -> float | None:
    """Parse an optional CSV number; empty or unreadable fields give None.

    One malformed row must not fail the whole station list.
    """
    if val is None or not val.strip():
        return None
    try:
        return float(val)
    except ValueError:
        return None


async def async_load_weather_station_list(
    session: ClientSession,
    encoding: str = "ISO-8859-1",
    *,
    raise_on_error: bool = False,
) -> list[WeatherStation]:
    """Load the list of MeteoSwiss weather stations."""
    _LOGGER.info("Requesting station list data...")
    try:

        async def _parse_csv(response: ClientResponse) -> list[WeatherStation]:
            text = await response.text(encoding=encoding)
            lines = text.splitlines()
            reader = csv.DictReader(lines, delimiter=";")
            stations = []
            for row in reader:
                name = row.get("Station")
                code = row.get("Abbr.")
                measurements = row.get("Measurements")
                # Precipitation stations are not in the current measurements
                # (VQHA80), so they could never deliver a value.
                if (
                    not name
                    or code is None
                    or measurements is None
                    or row.get("Station type") == "Precipitation station"
                ):
                    continue

                stations.append(
                    WeatherStation(
                        name,
                        code,
                        _int_or_none(row.get("Station height m a. sea level")),
                        _float_or_none(row.get("Latitude")),
                        _float_or_none(row.get("Longitude")),
                        row.get("Canton"),
                        frozenset(
                            part.strip() for part in measurements.split(",") if part.strip()
                        ),
                    )
                )
            return stations

        stations = await async_get_with_retry(
            session,
            STATION_LIST_URL,
            logger=_LOGGER,
            response_handler=_parse_csv,
            timeout=REQUEST_TIMEOUT,
        )
    except (
        ClientError,
        TimeoutError,
        UnicodeDecodeError,
        csv.Error,
        ValueError,
    ) as err:
        _LOGGER.warning("Failed to load MeteoSwiss station metadata", exc_info=True)
        if raise_on_error:
            raise WeatherStationMetadataLoadError(
                "Failed to load MeteoSwiss station metadata"
            ) from err
        return []

    _LOGGER.info("Retrieved %d weather stations.", len(stations))
    return stations


async def async_load_pollen_station_list(
    pollen_client: PollenClient,
    *,
    raise_on_error: bool = False,
) -> list[WeatherStation]:
    """Load the list of pollen stations."""
    _LOGGER.info("Requesting pollen station list data...")
    try:
        pollen_station_list = await pollen_client.async_get_pollen_station_list()
    except (PollenClientError, Exception) as err:
        _LOGGER.warning(
            "Failed to load MeteoSwiss pollen station metadata", exc_info=True
        )
        if raise_on_error:
            raise PollenStationMetadataLoadError(
                "Failed to load MeteoSwiss pollen station metadata"
            ) from err
        return []

    if pollen_station_list is None:
        return []

    stations = []
    for station in pollen_station_list:
        try:
            stations.append(
                WeatherStation(
                    station.name,
                    station.abbreviation,
                    int(station.altitude) if station.altitude is not None else None,
                    station.lat,
                    station.lng,
                    station.canton,
                )
            )
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Skipping invalid MeteoSwiss pollen station metadata row for %s",
                station.abbreviation,
                exc_info=True,
            )
    return stations


def find_station_by_code(
    stations: list[WeatherStation], code: str | None
) -> WeatherStation | None:
    """Return the station with the given code, if any."""
    if code is None:
        return None
    return next((station for station in stations if station.code == code), None)


def station_display_name(station: WeatherStation) -> str | None:
    """Return a station's name as stored in entries, e.g. "Pfäffikon ZH"."""
    place, canton = split_place_and_canton(station.name)
    return format_station_display_name(
        place, canton or station.canton, include_canton=True
    )


def split_place_and_canton(name: str | None) -> tuple[str | None, str | None]:
    """Split values like 'Pfäffikon, ZH' into name and canton."""
    if name is None:
        return (None, None)
    if "," not in name:
        return (name, None)
    place, canton = name.rsplit(",", 1)
    return (place.strip(), canton.strip())
