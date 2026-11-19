"""Active vs. upcoming warnings: snapshot, entities and registry cleanup."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from custom_components.meteoswiss_forecast_warnings import __init__ as init_module
from custom_components.meteoswiss_forecast_warnings.__init__ import (
    _async_cleanup_legacy_warning_entities,
)
from custom_components.meteoswiss_forecast_warnings.binary_sensor import (
    SwissWeatherHasActiveWarningsBinarySensor,
    SwissWeatherHasWarningsBinarySensor,
)
from custom_components.meteoswiss_forecast_warnings.binary_sensor import (
    async_setup_entry as async_setup_binary_sensor_entry,
)
from custom_components.meteoswiss_forecast_warnings.const import (
    CONF_FORECAST_NAME,
    CONF_POST_CODE,
    CONF_WARNINGS_ENABLED,
    DOMAIN,
)
from custom_components.meteoswiss_forecast_warnings.coordinator import (
    WeatherData,
    build_warning_snapshot,
)
from custom_components.meteoswiss_forecast_warnings.sensor import (
    SwissWeatherHighestWarningLevelSensor,
    SwissWeatherWarningCountSensor,
    SwissWeatherWarningSlotSensor,
)

from .test_warnings import make_warning

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
DEVICE = {"identifiers": {(DOMAIN, "entry-1-forecast")}, "name": "Buchs ZH"}


def _active(**kwargs):
    return make_warning(
        valid_from=NOW - timedelta(hours=1), valid_to=NOW + timedelta(hours=2), **kwargs
    )


def _upcoming(hours_ahead: int, **kwargs):
    return make_warning(
        valid_from=NOW + timedelta(hours=hours_ahead),
        valid_to=NOW + timedelta(hours=hours_ahead + 6),
        **kwargs,
    )


def _coordinator(warnings):
    return SimpleNamespace(
        data=WeatherData(
            current_weather=None,
            forecast=None,
            warning_snapshot=build_warning_snapshot(warnings, now=NOW),
        )
    )


def test_snapshot_separates_active_and_upcoming_warnings():
    active = _active(ordering="a")
    later = _upcoming(8, ordering="b")
    sooner = _upcoming(3, ordering="c")
    expired = make_warning(
        valid_from=NOW - timedelta(hours=5), valid_to=NOW - timedelta(hours=1)
    )

    snapshot = build_warning_snapshot([later, active, expired, sooner], now=NOW)

    assert snapshot.active_warnings == [active]
    assert snapshot.upcoming_warnings == [later, sooner]
    assert snapshot.next_warning_start == NOW + timedelta(hours=3)
    assert snapshot.count == 3


def test_snapshot_without_upcoming_warnings_has_no_next_start():
    snapshot = build_warning_snapshot([_active()], now=NOW)

    assert snapshot.upcoming_warnings == []
    assert snapshot.next_warning_start is None


def test_has_active_warnings_ignores_announced_warnings():
    coordinator = _coordinator([_upcoming(3)])

    any_warning = SwissWeatherHasWarningsBinarySensor("8107", "Buchs ZH", DEVICE, coordinator)
    active_warning = SwissWeatherHasActiveWarningsBinarySensor(
        "8107", "Buchs ZH", DEVICE, coordinator
    )

    assert any_warning.is_on is True
    assert active_warning.is_on is False


def test_has_active_warnings_is_on_for_warning_in_force():
    entity = SwissWeatherHasActiveWarningsBinarySensor(
        "8107", "Buchs ZH", DEVICE, _coordinator([_active()])
    )

    assert entity.is_on is True
    assert entity.unique_id == "8107.has_active_warnings"
    assert entity.translation_key == "has_active_warnings"


def test_has_active_warnings_is_unknown_without_data():
    """No data must not read as an all-clear."""
    entity = SwissWeatherHasActiveWarningsBinarySensor(
        "8107", "Buchs ZH", DEVICE, SimpleNamespace(data=None)
    )

    assert entity.is_on is None


def test_binary_sensor_setup_adds_both_warning_sensors():
    added = []
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={
            CONF_POST_CODE: "8107",
            CONF_FORECAST_NAME: "Buchs ZH",
            CONF_WARNINGS_ENABLED: True,
        },
        runtime_data=SimpleNamespace(weather_coordinator=SimpleNamespace(data=None)),
    )

    asyncio.run(async_setup_binary_sensor_entry(SimpleNamespace(), entry, added.extend))

    assert [entity.unique_id for entity in added] == [
        "8107.has_warnings",
        "8107.has_active_warnings",
    ]


def test_warning_count_reports_active_and_upcoming_split():
    sensor = SwissWeatherWarningCountSensor(
        "8107",
        "Buchs ZH",
        DEVICE,
        _coordinator([_active(), _upcoming(3), _upcoming(8)]),
    )

    assert sensor.native_value == 3
    assert sensor.extra_state_attributes == {
        "active_count": 1,
        "upcoming_count": 2,
        "next_warning_start": NOW + timedelta(hours=3),
    }


def test_slot_sensors_flag_whether_their_warning_is_in_force():
    from custom_components.meteoswiss_forecast_warnings.meteo import WarningLevel

    # The upcoming warning outranks the active one by level, so it takes slot 1.
    coordinator = _coordinator(
        [
            _active(warning_level=WarningLevel.MODERATE_HAZARD, raw_level=2),
            _upcoming(3, warning_level=WarningLevel.SEVERE_HAZARD, raw_level=4),
        ]
    )

    primary = SwissWeatherWarningSlotSensor("8107", "Buchs ZH", 0, DEVICE, coordinator)
    secondary = SwissWeatherWarningSlotSensor("8107", "Buchs ZH", 1, DEVICE, coordinator)
    tertiary = SwissWeatherWarningSlotSensor("8107", "Buchs ZH", 2, DEVICE, coordinator)

    assert primary.extra_state_attributes["warning_level"] == 4
    assert primary.extra_state_attributes["active"] is False
    assert secondary.extra_state_attributes["active"] is True
    assert tertiary.extra_state_attributes["active"] is None


def test_ranked_slots_after_the_third_show_the_following_warnings():
    from custom_components.meteoswiss_forecast_warnings.meteo import WarningType

    warnings = [
        _active(warning_type=warning_type)
        for warning_type in (
            WarningType.WIND,
            WarningType.THUNDERSTORMS,
            WarningType.RAIN,
            WarningType.HEAT_WAVES,
            WarningType.DROUGHT,
        )
    ]
    coordinator = _coordinator(warnings)
    ranked = coordinator.data.warning_snapshot.sorted_warnings

    fourth = SwissWeatherWarningSlotSensor("8107", "Buchs ZH", 3, DEVICE, coordinator)
    fifth = SwissWeatherWarningSlotSensor("8107", "Buchs ZH", 4, DEVICE, coordinator)
    sixth = SwissWeatherWarningSlotSensor(
        "8107", "Buchs ZH", 5, DEVICE, coordinator, enabled_default=False
    )

    assert fourth.unique_id == "8107.warning_4"
    assert fourth.entity_description.translation_key == "ranked_warning"
    assert fourth.translation_placeholders == {"rank": "4"}
    assert fourth.native_value == ranked[3].type_state
    assert fourth.extra_state_attributes["rank"] == 4
    assert fifth.native_value == ranked[4].type_state
    assert sixth.native_value == "none"
    assert sixth.extra_state_attributes["rank"] == 6
    assert sixth.entity_description.entity_registry_enabled_default is False
    # The named slots keep their keys, so existing entity IDs stay.
    primary = SwissWeatherWarningSlotSensor("8107", "Buchs ZH", 0, DEVICE, coordinator)
    assert primary.unique_id == "8107.primary_warning"
    assert primary.entity_description.translation_key == "primary_warning"


@pytest.mark.parametrize(
    ("shown", "enabled"),
    [(None, 3), (0, 3), (1, 3), (3, 3), (6, 6), (10, 10), (25, 10), ("7", 7)],
)
def test_enabled_warning_slots_follow_the_swiss_weather_setting(shown, enabled):
    from custom_components.meteoswiss_forecast_warnings.__init__ import (
        _enabled_warning_slots,
    )
    from custom_components.meteoswiss_forecast_warnings.const import (
        CONF_WEATHER_WARNINGS_NUMBER,
    )

    data = {CONF_POST_CODE: "8107"}
    if shown is not None:
        data[CONF_WEATHER_WARNINGS_NUMBER] = shown

    assert _enabled_warning_slots(SimpleNamespace(data=data)) == enabled


class _FakeEntityRegistry:
    def __init__(self, entities: list[SimpleNamespace]) -> None:
        self.entities = {entity.entity_id: entity for entity in entities}
        self.removed: list[str] = []

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)


def _registry_entry(entity_id: str, unique_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry-1",
        entity_id=entity_id,
        unique_id=unique_id,
    )


def test_legacy_cleanup_keeps_current_entities_matching_legacy_patterns(monkeypatch):
    """Regression: entity_ids of the current model can match the legacy regexes.

    HA derives "binary_sensor.buchs_zh_has_active_weather_warnings" from the
    device and entity names; it ends in "_weather_warnings" like the legacy
    summary entity did. Only the unique_id tells them apart.
    """
    registry = _FakeEntityRegistry(
        [
            _registry_entry(
                "binary_sensor.buchs_zh_has_weather_warnings", "8107.has_warnings"
            ),
            _registry_entry(
                "binary_sensor.buchs_zh_has_active_weather_warnings",
                "8107.has_active_warnings",
            ),
            # Ranked slots from 4 on get legacy-looking ids by default.
            _registry_entry("sensor.buchs_zh_weather_warning_4", "8107.warning_4"),
            _registry_entry("sensor.buchs_zh_weather_warning_10", "8107.warning_10"),
            # A current entity the user renamed into a legacy-looking id.
            _registry_entry("sensor.home_weather_warnings", "8107.primary_warning"),
            # Genuine legacy entities.
            _registry_entry("sensor.buchs_zh_weather_warnings", "8107.warnings"),
            _registry_entry("sensor.buchs_weather_warning_1", "8107.warning.1"),
        ]
    )
    monkeypatch.setattr(init_module.er, "async_get", lambda hass: registry)
    entry = SimpleNamespace(entry_id="entry-1", data={CONF_POST_CODE: "8107"})

    asyncio.run(_async_cleanup_legacy_warning_entities(SimpleNamespace(), entry))

    assert sorted(registry.removed) == [
        "sensor.buchs_weather_warning_1",
        "sensor.buchs_zh_weather_warnings",
    ]


def test_unreadable_warnings_make_the_snapshot_unavailable():
    snapshot = build_warning_snapshot(None, now=NOW)

    assert snapshot.available is False
    assert snapshot.count == 0
    assert build_warning_snapshot([], now=NOW).available is True


def _unreadable_warnings_coordinator():
    coordinator = _coordinator(None)
    coordinator.last_update_success = True
    return coordinator


def test_warning_entities_are_unavailable_when_warnings_are_unreadable():
    """A failed warning source is not an all-clear."""
    coordinator = _unreadable_warnings_coordinator()
    entities = [
        SwissWeatherHasWarningsBinarySensor("8107", "Buchs ZH", DEVICE, coordinator),
        SwissWeatherHasActiveWarningsBinarySensor("8107", "Buchs ZH", DEVICE, coordinator),
        SwissWeatherWarningCountSensor("8107", "Buchs ZH", DEVICE, coordinator),
        SwissWeatherHighestWarningLevelSensor("8107", "Buchs ZH", DEVICE, coordinator),
        SwissWeatherWarningSlotSensor("8107", "Buchs ZH", 0, DEVICE, coordinator),
    ]

    assert [entity.available for entity in entities] == [False] * 5


def test_warning_entities_stay_available_with_no_warnings():
    coordinator = _coordinator([])
    coordinator.last_update_success = True

    entity = SwissWeatherHasWarningsBinarySensor("8107", "Buchs ZH", DEVICE, coordinator)

    assert entity.available is True
    assert entity.is_on is False
