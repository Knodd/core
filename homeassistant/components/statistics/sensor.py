"""Support for statistics for sensor values."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
import contextlib
from datetime import datetime, timedelta
import logging
import statistics
from typing import Any, Literal, cast

import voluptuous as vol

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.recorder import get_instance, history
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_UNIQUE_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    HomeAssistant,
    State,
    callback,
    split_entity_id,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.start import async_at_start
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType, StateType
from homeassistant.util import dt as dt_util

from . import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

# Stats for attributes only
STAT_AGE_COVERAGE_RATIO = "age_coverage_ratio"
STAT_BUFFER_USAGE_RATIO = "buffer_usage_ratio"
STAT_SOURCE_VALUE_VALID = "source_value_valid"

# All sensor statistics
STAT_AVERAGE_LINEAR = "average_linear"
STAT_AVERAGE_STEP = "average_step"
STAT_AVERAGE_TIMELESS = "average_timeless"
STAT_CHANGE = "change"
STAT_CHANGE_SAMPLE = "change_sample"
STAT_CHANGE_SECOND = "change_second"
STAT_COUNT = "count"
STAT_DATETIME_NEWEST = "datetime_newest"
STAT_DATETIME_OLDEST = "datetime_oldest"
STAT_DISTANCE_95P = "distance_95_percent_of_values"
STAT_DISTANCE_99P = "distance_99_percent_of_values"
STAT_DISTANCE_ABSOLUTE = "distance_absolute"
STAT_MEAN = "mean"
STAT_MEDIAN = "median"
STAT_NOISINESS = "noisiness"
STAT_QUANTILES = "quantiles"
STAT_STANDARD_DEVIATION = "standard_deviation"
STAT_TOTAL = "total"
STAT_VALUE_MAX = "value_max"
STAT_VALUE_MIN = "value_min"
STAT_VARIANCE = "variance"

DEPRECATION_WARNING_CHARACTERISTIC = (
    "The configuration parameter 'state_characteristic' will become "
    "mandatory in a future release of the statistics integration. "
    "Please add 'state_characteristic: %s' to the configuration of "
    "sensor '%s' to keep the current behavior. Read the documentation "
    "for further details: "
    "https://www.home-assistant.io/integrations/statistics/"
)

# Statistics supported by a sensor source (numeric)
STATS_NUMERIC_SUPPORT = (
    STAT_AVERAGE_LINEAR,
    STAT_AVERAGE_STEP,
    STAT_AVERAGE_TIMELESS,
    STAT_CHANGE_SAMPLE,
    STAT_CHANGE_SECOND,
    STAT_CHANGE,
    STAT_COUNT,
    STAT_DATETIME_NEWEST,
    STAT_DATETIME_OLDEST,
    STAT_DISTANCE_95P,
    STAT_DISTANCE_99P,
    STAT_DISTANCE_ABSOLUTE,
    STAT_MEAN,
    STAT_MEDIAN,
    STAT_NOISINESS,
    STAT_QUANTILES,
    STAT_STANDARD_DEVIATION,
    STAT_TOTAL,
    STAT_VALUE_MAX,
    STAT_VALUE_MIN,
    STAT_VARIANCE,
)

# Statistics supported by a binary_sensor source
STATS_BINARY_SUPPORT = (
    STAT_AVERAGE_STEP,
    STAT_AVERAGE_TIMELESS,
    STAT_COUNT,
    STAT_MEAN,
)

STATS_NOT_A_NUMBER = (
    STAT_DATETIME_NEWEST,
    STAT_DATETIME_OLDEST,
    STAT_QUANTILES,
)

STATS_DATETIME = (
    STAT_DATETIME_NEWEST,
    STAT_DATETIME_OLDEST,
)

CONF_STATE_CHARACTERISTIC = "state_characteristic"
CONF_SAMPLES_MAX_BUFFER_SIZE = "sampling_size"
CONF_MAX_AGE = "max_age"
CONF_PRECISION = "precision"
CONF_QUANTILE_INTERVALS = "quantile_intervals"
CONF_QUANTILE_METHOD = "quantile_method"

DEFAULT_NAME = "Stats"
DEFAULT_BUFFER_SIZE = 20
DEFAULT_PRECISION = 2
DEFAULT_QUANTILE_INTERVALS = 4
DEFAULT_QUANTILE_METHOD = "exclusive"
ICON = "mdi:calculator"


def valid_state_characteristic_configuration(config: dict[str, Any]) -> dict[str, Any]:
    """Validate that the characteristic selected is valid for the source sensor type, throw if it isn't."""
    is_binary = split_entity_id(config[CONF_ENTITY_ID])[0] == BINARY_SENSOR_DOMAIN

    if config.get(CONF_STATE_CHARACTERISTIC) is None:
        config[CONF_STATE_CHARACTERISTIC] = STAT_COUNT if is_binary else STAT_MEAN
        _LOGGER.warning(
            DEPRECATION_WARNING_CHARACTERISTIC,
            config[CONF_STATE_CHARACTERISTIC],
            config[CONF_NAME],
        )

    characteristic = cast(str, config[CONF_STATE_CHARACTERISTIC])
    if (is_binary and characteristic not in STATS_BINARY_SUPPORT) or (
        not is_binary and characteristic not in STATS_NUMERIC_SUPPORT
    ):
        raise vol.ValueInvalid(
            "The configured characteristic '{}' is not supported for the configured source sensor".format(
                characteristic
            )
        )
    return config


_PLATFORM_SCHEMA_BASE = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ENTITY_ID): cv.entity_id,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Optional(CONF_STATE_CHARACTERISTIC): cv.string,
        vol.Optional(
            CONF_SAMPLES_MAX_BUFFER_SIZE, default=DEFAULT_BUFFER_SIZE
        ): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional(CONF_MAX_AGE): cv.time_period,
        vol.Optional(CONF_PRECISION, default=DEFAULT_PRECISION): vol.Coerce(int),
        vol.Optional(
            CONF_QUANTILE_INTERVALS, default=DEFAULT_QUANTILE_INTERVALS
        ): vol.All(vol.Coerce(int), vol.Range(min=2)),
        vol.Optional(CONF_QUANTILE_METHOD, default=DEFAULT_QUANTILE_METHOD): vol.In(
            ["exclusive", "inclusive"]
        ),
    }
)
PLATFORM_SCHEMA = vol.All(
    _PLATFORM_SCHEMA_BASE,
    valid_state_characteristic_configuration,
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Statistics sensor."""

    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)

    async_add_entities(
        new_entities=[
            StatisticsSensor(
                source_entity_id=config[CONF_ENTITY_ID],
                name=config[CONF_NAME],
                unique_id=config.get(CONF_UNIQUE_ID),
                state_characteristic=config[CONF_STATE_CHARACTERISTIC],
                samples_max_buffer_size=config[CONF_SAMPLES_MAX_BUFFER_SIZE],
                samples_max_age=config.get(CONF_MAX_AGE),
                precision=config[CONF_PRECISION],
                quantile_intervals=config[CONF_QUANTILE_INTERVALS],
                quantile_method=config[CONF_QUANTILE_METHOD],
            )
        ],
        update_before_add=True,
    )


class StatisticsSensor(SensorEntity):
    """Representation of a Statistics sensor."""

    def __init__(
        self,
        source_entity_id: str,
        name: str,
        unique_id: str | None,
        state_characteristic: str,
        samples_max_buffer_size: int,
        samples_max_age: timedelta | None,
        precision: int,
        quantile_intervals: int,
        quantile_method: str,
    ) -> None:
        """Initialize the Statistics sensor."""
        self._attr_icon: str = ICON
        self._attr_name: str = name
        self._attr_should_poll: bool = False
        self._attr_unique_id: str | None = unique_id
        self._source_entity_id: str = source_entity_id
        self.is_binary: bool = (
            split_entity_id(self._source_entity_id)[0] == BINARY_SENSOR_DOMAIN
        )
        self._state_characteristic: str = state_characteristic
        self._samples_max_buffer_size: int = samples_max_buffer_size
        self._samples_max_age: timedelta | None = samples_max_age
        self._precision: int = precision
        self._quantile_intervals: int = quantile_intervals
        self._quantile_method: str = quantile_method
        self._value: StateType | datetime = None
        self._unit_of_measurement: str | None = None
        self._available: bool = False
        self.states: deque[float | bool] = deque(maxlen=self._samples_max_buffer_size)
        self.ages: deque[datetime] = deque(maxlen=self._samples_max_buffer_size)
        self.attributes: dict[str, StateType] = {
            STAT_AGE_COVERAGE_RATIO: None,
            STAT_BUFFER_USAGE_RATIO: None,
            STAT_SOURCE_VALUE_VALID: None,
        }

        self._state_characteristic_fn: Callable[[], StateType | datetime]
        if self.is_binary:
            self._state_characteristic_fn = getattr(
                self, f"_stat_binary_{self._state_characteristic}"
            )
        else:
            self._state_characteristic_fn = getattr(
                self, f"_stat_{self._state_characteristic}"
            )

        self._update_listener: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""

        @callback
        def async_stats_sensor_state_listener(event: Event) -> None:
            """Handle the sensor state changes."""
            if (new_state := event.data.get("new_state")) is None:
                return
            self._add_state_to_queue(new_state)
            self.async_schedule_update_ha_state(True)

        async def async_stats_sensor_startup(_: HomeAssistant) -> None:
            """Add listener and get recorded state."""
            _LOGGER.debug("Startup for %s", self.entity_id)

            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._source_entity_id],
                    async_stats_sensor_state_listener,
                )
            )

            if "recorder" in self.hass.config.components:
                self.hass.async_create_task(self._initialize_from_database())

        async_at_start(self.hass, async_stats_sensor_startup)

    def _add_state_to_queue(self, new_state: State) -> None:
        """Add the state to the queue."""
        self._available = new_state.state != STATE_UNAVAILABLE
        if new_state.state == STATE_UNAVAILABLE:
            self.attributes[STAT_SOURCE_VALUE_VALID] = None
            return
        if new_state.state in (STATE_UNKNOWN, None):
            self.attributes[STAT_SOURCE_VALUE_VALID] = False
            return

        try:
            if self.is_binary:
                assert new_state.state in ("on", "off")
                self.states.append(new_state.state == "on")
            else:
                self.states.append(float(new_state.state))
            self.ages.append(new_state.last_updated)
            self.attributes[STAT_SOURCE_VALUE_VALID] = True
        except ValueError:
            self.attributes[STAT_SOURCE_VALUE_VALID] = False
            _LOGGER.error(
                "%s: parsing error, expected number and received %s",
                self.entity_id,
                new_state.state,
            )
            return

        self._unit_of_measurement = self._derive_unit_of_measurement(new_state)

    def _derive_unit_of_measurement(self, new_state: State) -> str | None:
        base_unit: str | None = new_state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        unit: str | None
        if self.is_binary and self._state_characteristic in (
            STAT_AVERAGE_STEP,
            STAT_AVERAGE_TIMELESS,
            STAT_MEAN,
        ):
            unit = "%"
        elif not base_unit:
            unit = None
        elif self._state_characteristic in (
            STAT_AVERAGE_LINEAR,
            STAT_AVERAGE_STEP,
            STAT_AVERAGE_TIMELESS,
            STAT_CHANGE,
            STAT_DISTANCE_95P,
            STAT_DISTANCE_99P,
            STAT_DISTANCE_ABSOLUTE,
            STAT_MEAN,
            STAT_MEDIAN,
            STAT_NOISINESS,
            STAT_STANDARD_DEVIATION,
            STAT_TOTAL,
            STAT_VALUE_MAX,
            STAT_VALUE_MIN,
        ):
            unit = base_unit
        elif self._state_characteristic in STATS_NOT_A_NUMBER:
            unit = None
        elif self._state_characteristic == STAT_COUNT:
            unit = None
        elif self._state_characteristic == STAT_VARIANCE:
            unit = base_unit + "²"
        elif self._state_characteristic == STAT_CHANGE_SAMPLE:
            unit = base_unit + "/sample"
        elif self._state_characteristic == STAT_CHANGE_SECOND:
            unit = base_unit + "/s"
        return unit

    @property
    def device_class(self) -> Literal[SensorDeviceClass.TIMESTAMP] | None:
        """Return the class of this device."""
        if self._state_characteristic in STATS_DATETIME:
            return SensorDeviceClass.TIMESTAMP
        return None

    @property
    def state_class(self) -> Literal[SensorStateClass.MEASUREMENT] | None:
        """Return the state class of this entity."""
        if self._state_characteristic in STATS_NOT_A_NUMBER:
            return None
        return SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> StateType | datetime:
        """Return the state of the sensor."""
        return self._value

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit the value is expressed in."""
        return self._unit_of_measurement

    @property
    def available(self) -> bool:
        """Return the availability of the sensor linked to the source sensor."""
        return self._available

    @property
    def extra_state_attributes(self) -> dict[str, StateType] | None:
        """Return the state attributes of the sensor."""
        return {
            key: value for key, value in self.attributes.items() if value is not None
        }

    def _purge_old_states(self, max_age: timedelta) -> None:
        """Remove states which are older than a given age."""
        now = dt_util.utcnow()

        _LOGGER.debug(
            "%s: purging records older then %s(%s)",
            self.entity_id,
            dt_util.as_local(now - max_age),
            self._samples_max_age,
        )

        while self.ages and (now - self.ages[0]) > max_age:
            _LOGGER.debug(
                "%s: purging record with datetime %s(%s)",
                self.entity_id,
                dt_util.as_local(self.ages[0]),
                (now - self.ages[0]),
            )
            self.ages.popleft()
            self.states.popleft()

    def _next_to_purge_timestamp(self) -> datetime | None:
        """Find the timestamp when the next purge would occur."""
        if self.ages and self._samples_max_age:
            # Take the oldest entry from the ages list and add the configured max_age.
            # If executed after purging old states, the result is the next timestamp
            # in the future when the oldest state will expire.
            return self.ages[0] + self._samples_max_age
        return None

    async def async_update(self) -> None:
        """Get the latest data and updates the states."""
        _LOGGER.debug("%s: updating statistics", self.entity_id)
        if self._samples_max_age is not None:
            self._purge_old_states(self._samples_max_age)

        self._update_attributes()
        self._update_value()

        # If max_age is set, ensure to update again after the defined interval.
        next_to_purge_timestamp = self._next_to_purge_timestamp()
        if next_to_purge_timestamp:
            _LOGGER.debug(
                "%s: scheduling update at %s", self.entity_id, next_to_purge_timestamp
            )
            if self._update_listener:
                self._update_listener()
                self._update_listener = None

            @callback
            def _scheduled_update(now: datetime) -> None:
                """Timer callback for sensor update."""
                _LOGGER.debug("%s: executing scheduled update", self.entity_id)
                self.async_schedule_update_ha_state(True)
                self._update_listener = None

            self._update_listener = async_track_point_in_utc_time(
                self.hass, _scheduled_update, next_to_purge_timestamp
            )

    def _fetch_states_from_database(self) -> list[State]:
        """Fetch the states from the database."""
        _LOGGER.debug("%s: initializing values from the database", self.entity_id)
        lower_entity_id = self._source_entity_id.lower()
        if self._samples_max_age is not None:
            start_date = (
                dt_util.utcnow() - self._samples_max_age - timedelta(microseconds=1)
            )
            _LOGGER.debug(
                "%s: retrieve records not older then %s",
                self.entity_id,
                start_date,
            )
        else:
            start_date = datetime.fromtimestamp(0, tz=dt_util.UTC)
            _LOGGER.debug("%s: retrieving all records", self.entity_id)
        entity_states = history.state_changes_during_period(
            self.hass,
            start_date,
            entity_id=lower_entity_id,
            descending=True,
            limit=self._samples_max_buffer_size,
            include_start_time_state=False,
        )
        # Need to cast since minimal responses is not passed in
        return cast(list[State], entity_states.get(lower_entity_id, []))

    async def _initialize_from_database(self) -> None:
        """Initialize the list of states from the database.

        The query will get the list of states in DESCENDING order so that we
        can limit the result to self._sample_size. Afterwards reverse the
        list so that we get it in the right order again.

        If MaxAge is provided then query will restrict to entries younger then
        current datetime - MaxAge.
        """
        if states := await get_instance(self.hass).async_add_executor_job(
            self._fetch_states_from_database
        ):
            for state in reversed(states):
                self._add_state_to_queue(state)

        self.async_schedule_update_ha_state(True)

        _LOGGER.debug("%s: initializing from database completed", self.entity_id)

    def _update_attributes(self) -> None:
        """Calculate and update the various attributes."""
        self.attributes[STAT_BUFFER_USAGE_RATIO] = round(
            len(self.states) / self._samples_max_buffer_size, 2
        )

        if len(self.states) >= 1 and self._samples_max_age is not None:
            self.attributes[STAT_AGE_COVERAGE_RATIO] = round(
                (self.ages[-1] - self.ages[0]).total_seconds()
                / self._samples_max_age.total_seconds(),
                2,
            )
        else:
            self.attributes[STAT_AGE_COVERAGE_RATIO] = None

    def _update_value(self) -> None:
        """Front to call the right statistical characteristics functions.

        One of the _stat_*() functions is represented by self._state_characteristic_fn().
        """

        value = self._state_characteristic_fn()

        if self._state_characteristic not in STATS_NOT_A_NUMBER:
            with contextlib.suppress(TypeError):
                value = round(cast(float, value), self._precision)
                if self._precision == 0:
                    value = int(value)
        self._value = value

    # Statistics for numeric sensor

    def _stat_average_linear(self) -> StateType:
        if len(self.states) >= 2:
            area: float = 0
            for i in range(1, len(self.states)):
                area += (
                    0.5
                    * (self.states[i] + self.states[i - 1])
                    * (self.ages[i] - self.ages[i - 1]).total_seconds()
                )
            age_range_seconds = (self.ages[-1] - self.ages[0]).total_seconds()
            return area / age_range_seconds
        return None

    def _stat_average_step(self) -> StateType:
        if len(self.states) >= 2:
            area: float = 0
            for i in range(1, len(self.states)):
                area += (
                    self.states[i - 1]
                    * (self.ages[i] - self.ages[i - 1]).total_seconds()
                )
            age_range_seconds = (self.ages[-1] - self.ages[0]).total_seconds()
            return area / age_range_seconds
        return None

    def _stat_average_timeless(self) -> StateType:
        return self._stat_mean()

    def _stat_change(self) -> StateType:
        if len(self.states) > 0:
            return self.states[-1] - self.states[0]
        return None

    def _stat_change_sample(self) -> StateType:
        if len(self.states) > 1:
            return (self.states[-1] - self.states[0]) / (len(self.states) - 1)
        return None

    def _stat_change_second(self) -> StateType:
        if len(self.states) > 1:
            age_range_seconds = (self.ages[-1] - self.ages[0]).total_seconds()
            if age_range_seconds > 0:
                return (self.states[-1] - self.states[0]) / age_range_seconds
        return None

    def _stat_count(self) -> StateType:
        return len(self.states)

    def _stat_datetime_newest(self) -> datetime | None:
        if len(self.states) > 0:
            return self.ages[-1]
        return None

    def _stat_datetime_oldest(self) -> datetime | None:
        if len(self.states) > 0:
            return self.ages[0]
        return None

    def _stat_distance_95_percent_of_values(self) -> StateType:
        if len(self.states) >= 2:
            return 2 * 1.96 * cast(float, self._stat_standard_deviation())
        return None

    def _stat_distance_99_percent_of_values(self) -> StateType:
        if len(self.states) >= 2:
            return 2 * 2.58 * cast(float, self._stat_standard_deviation())
        return None

    def _stat_distance_absolute(self) -> StateType:
        if len(self.states) > 0:
            return max(self.states) - min(self.states)
        return None

    def _stat_mean(self) -> StateType:
        if len(self.states) > 0:
            return statistics.mean(self.states)
        return None

    def _stat_median(self) -> StateType:
        if len(self.states) > 0:
            return statistics.median(self.states)
        return None

    def _stat_noisiness(self) -> StateType:
        if len(self.states) >= 2:
            diff_sum = sum(
                abs(j - i) for i, j in zip(list(self.states), list(self.states)[1:])
            )
            return diff_sum / (len(self.states) - 1)
        return None

    def _stat_quantiles(self) -> StateType:
        if len(self.states) > self._quantile_intervals:
            return str(
                [
                    round(quantile, self._precision)
                    for quantile in statistics.quantiles(
                        self.states,
                        n=self._quantile_intervals,
                        method=self._quantile_method,
                    )
                ]
            )
        return None

    def _stat_standard_deviation(self) -> StateType:
        if len(self.states) >= 2:
            return statistics.stdev(self.states)
        return None

    def _stat_total(self) -> StateType:
        if len(self.states) > 0:
            return sum(self.states)
        return None

    def _stat_value_max(self) -> StateType:
        if len(self.states) > 0:
            return max(self.states)
        return None

    def _stat_value_min(self) -> StateType:
        if len(self.states) > 0:
            return min(self.states)
        return None

    def _stat_variance(self) -> StateType:
        if len(self.states) >= 2:
            return statistics.variance(self.states)
        return None

    # Statistics for binary sensor

    def _stat_binary_average_step(self) -> StateType:
        if len(self.states) >= 2:
            on_seconds: float = 0
            for i in range(1, len(self.states)):
                if self.states[i - 1] is True:
                    on_seconds += (self.ages[i] - self.ages[i - 1]).total_seconds()
            age_range_seconds = (self.ages[-1] - self.ages[0]).total_seconds()
            return 100 / age_range_seconds * on_seconds
        return None

    def _stat_binary_average_timeless(self) -> StateType:
        return self._stat_binary_mean()

    def _stat_binary_count(self) -> StateType:
        return len(self.states)

    def _stat_binary_mean(self) -> StateType:
        if len(self.states) > 0:
            return 100.0 / len(self.states) * self.states.count(True)
        return None
