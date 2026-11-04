"""Tests for forecast-point search helpers."""

import pytest

from custom_components.meteoswiss_forecast_warnings.forecast_points import (
    ForecastPoint,
    find_forecast_point_for_postal_code,
    search_forecast_points,
)


def _zip_point(point_id: str, postal_code: str, name: str) -> ForecastPoint:
    return ForecastPoint(point_id, "2", postal_code, name, "ZIP", 0, 0.0, 0.0)


POSTAL_POINTS = [
    _zip_point("300400", "3004", "Bern"),
    _zip_point("301100", "3011", "Bern"),
    _zip_point("310000", "3100", "Thun"),
    _zip_point("100026", "1000", "Lausanne 26"),
    _zip_point("100025", "1000", "Lausanne 25"),
    _zip_point("900000", "9000", "St. Gallen"),
]


@pytest.mark.parametrize(
    ("postal_code", "point_id"),
    [
        ("9000", "900000"),  # the usual "<postal code>00"
        ("1000", "100025"),  # no centre point, but other points of the code
        ("3000", "300400"),  # city-wide code without points: its hundred block
        ("3200", None),  # nothing in the 32xx block
        ("3005", None),  # only city-wide codes fall back
    ],
)
def test_point_for_postal_code(postal_code, point_id):
    point = find_forecast_point_for_postal_code(POSTAL_POINTS, postal_code)

    assert (point.point_id if point else None) == point_id


def test_search_forecast_points_matches_umlaut_variants() -> None:
    points = [
        ForecastPoint("800000", "2", "8000", "Zürich", "ZIP", 408, 47.37, 8.54),
        ForecastPoint("810700", "2", "8107", "Buchs ZH", "ZIP", 535, 47.46, 8.44),
    ]

    assert [point.point_id for point in search_forecast_points(points, "Zürich")] == [
        "800000"
    ]
    assert [point.point_id for point in search_forecast_points(points, "Zurich")] == [
        "800000"
    ]
    assert [point.point_id for point in search_forecast_points(points, "Zuerich")] == [
        "800000"
    ]


def test_search_forecast_points_still_matches_plain_substrings() -> None:
    points = [
        ForecastPoint("650000", "2", "6500", "Bellinzona", "ZIP", 238, 46.19, 9.02),
        ForecastPoint("650300", "2", "6503", "Bellinzona", "ZIP", 238, 46.19, 9.02),
    ]

    assert [point.point_id for point in search_forecast_points(points, "Bellin")] == [
        "650000",
        "650300",
    ]
