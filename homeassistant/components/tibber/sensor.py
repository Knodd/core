"""Support for Tibber sensors."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from random import randrange

import aiohttp

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ELECTRIC_CURRENT_AMPERE,
    ELECTRIC_POTENTIAL_VOLT,
    ENERGY_KILO_WATT_HOUR,
    EVENT_HOMEASSISTANT_STOP,
    PERCENTAGE,
    POWER_WATT,
    SIGNAL_STRENGTH_DECIBELS,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.device_registry import async_get as async_get_dev_reg
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import async_get as async_get_entity_reg
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import Throttle, dt as dt_util

from .const import DOMAIN as TIBBER_DOMAIN, MANUFACTURER

_LOGGER = logging.getLogger(__name__)

ICON = "mdi:currency-usd"
SCAN_INTERVAL = timedelta(minutes=1)
MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=5)
PARALLEL_UPDATES = 0


RT_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="averagePower",
        name="average power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=POWER_WATT,
    ),
    SensorEntityDescription(
        key="power",
        name="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=POWER_WATT,
    ),
    SensorEntityDescription(
        key="powerProduction",
        name="power production",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=POWER_WATT,
    ),
    SensorEntityDescription(
        key="minPower",
        name="min power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=POWER_WATT,
    ),
    SensorEntityDescription(
        key="maxPower",
        name="max power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=POWER_WATT,
    ),
    SensorEntityDescription(
        key="accumulatedConsumption",
        name="accumulated consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
    ),
    SensorEntityDescription(
        key="accumulatedConsumptionLastHour",
        name="accumulated consumption current hour",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="estimatedHourConsumption",
        name="Estimated consumption current hour",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="accumulatedProduction",
        name="accumulated production",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
    ),
    SensorEntityDescription(
        key="accumulatedProductionLastHour",
        name="accumulated production current hour",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="lastMeterConsumption",
        name="last meter consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="lastMeterProduction",
        name="last meter production",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="voltagePhase1",
        name="voltage phase1",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=ELECTRIC_POTENTIAL_VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="voltagePhase2",
        name="voltage phase2",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=ELECTRIC_POTENTIAL_VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="voltagePhase3",
        name="voltage phase3",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=ELECTRIC_POTENTIAL_VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="currentL1",
        name="current L1",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=ELECTRIC_CURRENT_AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="currentL2",
        name="current L2",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=ELECTRIC_CURRENT_AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="currentL3",
        name="current L3",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=ELECTRIC_CURRENT_AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="signalStrength",
        name="signal strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="accumulatedReward",
        name="accumulated reward",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="accumulatedCost",
        name="accumulated cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="powerFactor",
        name="power factor",
        device_class=SensorDeviceClass.POWER_FACTOR,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
)

SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="month_cost",
        name="Monthly cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="peak_hour",
        name="Monthly peak hour consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="peak_hour_time",
        name="Time of max hour consumption",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    SensorEntityDescription(
        key="month_cons",
        name="Monthly net consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Tibber sensor."""

    tibber_connection = hass.data[TIBBER_DOMAIN]

    entity_registry = async_get_entity_reg(hass)
    device_registry = async_get_dev_reg(hass)

    coordinator: TibberDataCoordinator | None = None
    entities: list[TibberSensor] = []
    for home in tibber_connection.get_homes(only_active=False):
        try:
            await home.update_info()
        except asyncio.TimeoutError as err:
            _LOGGER.error("Timeout connecting to Tibber home: %s ", err)
            raise PlatformNotReady() from err
        except aiohttp.ClientError as err:
            _LOGGER.error("Error connecting to Tibber home: %s ", err)
            raise PlatformNotReady() from err

        if home.has_active_subscription:
            entities.append(TibberSensorElPrice(home))
            if coordinator is None:
                coordinator = TibberDataCoordinator(hass, tibber_connection)
            for entity_description in SENSORS:
                entities.append(TibberDataSensor(home, coordinator, entity_description))

        if home.has_real_time_consumption:
            await home.rt_subscribe(
                TibberRtDataCoordinator(
                    async_add_entities, home, hass
                ).async_set_updated_data
            )

        # migrate
        old_id = home.info["viewer"]["home"]["meteringPointData"]["consumptionEan"]
        if old_id is None:
            continue

        # migrate to new device ids
        old_entity_id = entity_registry.async_get_entity_id(
            "sensor", TIBBER_DOMAIN, old_id
        )
        if old_entity_id is not None:
            entity_registry.async_update_entity(
                old_entity_id, new_unique_id=home.home_id
            )

        # migrate to new device ids
        device_entry = device_registry.async_get_device({(TIBBER_DOMAIN, old_id)})
        if device_entry and entry.entry_id in device_entry.config_entries:
            device_registry.async_update_device(
                device_entry.id, new_identifiers={(TIBBER_DOMAIN, home.home_id)}
            )

    async_add_entities(entities, True)


class TibberSensor(SensorEntity):
    """Representation of a generic Tibber sensor."""

    def __init__(self, *args, tibber_home, **kwargs):
        """Initialize the sensor."""
        super().__init__(*args, **kwargs)
        self._tibber_home = tibber_home
        self._home_name = tibber_home.info["viewer"]["home"]["appNickname"]
        if self._home_name is None:
            self._home_name = tibber_home.info["viewer"]["home"]["address"].get(
                "address1", ""
            )
        self._device_name = None
        self._model = None

    @property
    def device_info(self):
        """Return the device_info of the device."""
        device_info = DeviceInfo(
            identifiers={(TIBBER_DOMAIN, self._tibber_home.home_id)},
            name=self._device_name,
            manufacturer=MANUFACTURER,
        )
        if self._model is not None:
            device_info["model"] = self._model
        return device_info


class TibberSensorElPrice(TibberSensor):
    """Representation of a Tibber sensor for el price."""

    def __init__(self, tibber_home):
        """Initialize the sensor."""
        super().__init__(tibber_home=tibber_home)
        self._last_updated = None
        self._spread_load_constant = randrange(3600)

        self._attr_available = False
        self._attr_extra_state_attributes = {
            "app_nickname": None,
            "grid_company": None,
            "estimated_annual_consumption": None,
            "price_level": None,
            "max_price": None,
            "avg_price": None,
            "min_price": None,
            "off_peak_1": None,
            "peak": None,
            "off_peak_2": None,
            "today": None,
            "raw_today": None,
            "tomorrow_valid": False,
            "tomorrow": None,
            "raw_tomorrow": None,
        }
        self._attr_icon = ICON
        self._attr_name = f"Electricity price {self._home_name}"
        self._attr_unique_id = self._tibber_home.home_id
        self._model = "Price Sensor"

        self._device_name = self._home_name

    async def async_update(self):
        """Get the latest data and updates the states."""
        now = dt_util.now()
        if (
            not self._tibber_home.last_data_timestamp
            or (self._tibber_home.last_data_timestamp - now).total_seconds()
            < 8 * 3600 + self._spread_load_constant
            or not self.available
        ):
            _LOGGER.debug("Asking for new data")
            await self._fetch_data()

        elif (
            self._tibber_home.current_price_total
            and self._last_updated
            and self._last_updated.hour == now.hour
            and self._tibber_home.last_data_timestamp
        ):
            return

        res = self._tibber_home.current_price_data()
        self._attr_native_value, price_level, self._last_updated = res
        self._attr_extra_state_attributes["price_level"] = price_level

        priceinfo = self._tibber_home.info["viewer"]["home"]["currentSubscription"][
            "priceInfo"
        ]
        # todays priceInfo is for today. Add todays and tomorrows priceInfo
        if (
            priceinfo["today"]
            and dt_util.parse_datetime(priceinfo["today"][1]["startsAt"]).date()
            == dt_util.now().date()
        ):
            # iterate through todays prices and add list with only prices
            if priceinfo["today"]:
                self._attr_extra_state_attributes["raw_today"] = priceinfo["today"]
                local_today = []
                for entry in priceinfo["today"]:
                    local_today.append(entry["total"])

                self._attr_extra_state_attributes["today"] = local_today
                _LOGGER.debug("Today priceInfo array set")
            else:
                self._attr_extra_state_attributes["raw_today"] = []
                self._attr_extra_state_attributes["today"] = []
                _LOGGER.debug("Today priceInfo missing")

            # iterate through tomorrows prices and add list with only prices
            if priceinfo["tomorrow"]:
                self._attr_extra_state_attributes["raw_tomorrow"] = priceinfo[
                    "tomorrow"
                ]
                local_tomorrow = []
                local_tomorrow_valid = True
                for entry in priceinfo["tomorrow"]:
                    local_tomorrow.append(entry["total"])
                    if not entry["total"]:
                        local_tomorrow_valid = False

                self._attr_extra_state_attributes["tomorrow"] = local_tomorrow
                _LOGGER.debug("Tomorrow priceInfo array set")
                # if no empty values and 24 entries, mark tomorrow as valid
                if len(local_tomorrow) == 24 and local_tomorrow_valid:
                    self._attr_extra_state_attributes["tomorrow_valid"] = True
            else:
                self._attr_extra_state_attributes["raw_tomorrow"] = []
                self._attr_extra_state_attributes["tomorrow"] = []
                _LOGGER.debug("Tomorrow priceInfo missing")

        # tomorrows priceInfo is for today. Add tomorrows priceInfo as today
        elif (
            priceinfo["tomorrow"]
            and dt_util.parse_datetime(priceinfo["tomorrow"][1]["startsAt"]).date()
            == dt_util.now().date()
        ):
            _LOGGER.debug("Cached tomorrow priceInfo from yesterday is now today")
            self._attr_extra_state_attributes["raw_tomorrow"] = []
            self._attr_extra_state_attributes["tomorrow"] = []
            self._attr_extra_state_attributes["tomorrow_valid"] = False

            # iterate through todays prices and add list with only prices
            if priceinfo["tomorrow"]:
                self._attr_extra_state_attributes["raw_today"] = priceinfo["tomorrow"]
                local_today = []
                for entry in priceinfo["tomorrow"]:
                    local_today.append(entry["total"])
                self._attr_extra_state_attributes["today"] = local_today
                _LOGGER.debug("Today priceInfo array set")
            else:
                self._attr_extra_state_attributes["raw_today"] = []
                self._attr_extra_state_attributes["today"] = []
                _LOGGER.debug("Today (tomorrow) priceInfo missing")

        # no cached priceInfo valid for today
        else:
            self._attr_extra_state_attributes["today"] = []
            self._attr_extra_state_attributes["tomorrow"] = []
            self._attr_extra_state_attributes["raw_today"] = []
            self._attr_extra_state_attributes["raw_tomorrow"] = []
            _LOGGER.debug("priceInfo missing")

        attrs = self._tibber_home.current_attributes()
        self._attr_extra_state_attributes.update(attrs)
        self._attr_available = self._attr_native_value is not None
        self._attr_native_unit_of_measurement = self._tibber_home.price_unit

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def _fetch_data(self):
        _LOGGER.debug("Fetching data")
        try:
            await self._tibber_home.update_info_and_price_info()
        except (asyncio.TimeoutError, aiohttp.ClientError):
            return
        data = self._tibber_home.info["viewer"]["home"]
        self._attr_extra_state_attributes["app_nickname"] = data["appNickname"]
        self._attr_extra_state_attributes["grid_company"] = data["meteringPointData"][
            "gridCompany"
        ]
        self._attr_extra_state_attributes["estimated_annual_consumption"] = data[
            "meteringPointData"
        ]["estimatedAnnualConsumption"]


class TibberDataSensor(TibberSensor, CoordinatorEntity["TibberDataCoordinator"]):
    """Representation of a Tibber sensor."""

    def __init__(
        self,
        tibber_home,
        coordinator: TibberDataCoordinator,
        entity_description: SensorEntityDescription,
    ):
        """Initialize the sensor."""
        super().__init__(coordinator=coordinator, tibber_home=tibber_home)
        self.entity_description = entity_description

        self._attr_unique_id = (
            f"{self._tibber_home.home_id}_{self.entity_description.key}"
        )
        self._attr_name = f"{entity_description.name} {self._home_name}"
        if entity_description.key == "month_cost":
            self._attr_native_unit_of_measurement = self._tibber_home.currency

        self._device_name = self._home_name

    @property
    def native_value(self):
        """Return the value of the sensor."""
        return getattr(self._tibber_home, self.entity_description.key)


class TibberSensorRT(TibberSensor, CoordinatorEntity["TibberRtDataCoordinator"]):
    """Representation of a Tibber sensor for real time consumption."""

    def __init__(
        self,
        tibber_home,
        description: SensorEntityDescription,
        initial_state,
        coordinator: TibberRtDataCoordinator,
    ):
        """Initialize the sensor."""
        super().__init__(coordinator=coordinator, tibber_home=tibber_home)
        self.entity_description = description
        self._model = "Tibber Pulse"
        self._device_name = f"{self._model} {self._home_name}"

        self._attr_name = f"{description.name} {self._home_name}"
        self._attr_native_value = initial_state
        self._attr_unique_id = f"{self._tibber_home.home_id}_rt_{description.name}"

        if description.key in ("accumulatedCost", "accumulatedReward"):
            self._attr_native_unit_of_measurement = tibber_home.currency

    @property
    def available(self):
        """Return True if entity is available."""
        return self._tibber_home.rt_subscription_running

    @callback
    def _handle_coordinator_update(self) -> None:
        if not (live_measurement := self.coordinator.get_live_measurement()):
            return
        state = live_measurement.get(self.entity_description.key)
        if state is None:
            return
        if self.entity_description.key in (
            "accumulatedConsumption",
            "accumulatedProduction",
        ):
            # Value is reset to 0 at midnight, but not always strictly increasing due to hourly corrections
            # If device is offline, last_reset should be updated when it comes back online if the value has decreased
            ts_local = dt_util.parse_datetime(live_measurement["timestamp"])
            if ts_local is not None:
                if self.last_reset is None or (
                    state < 0.5 * self.native_value  # type: ignore[operator]  # native_value is float
                    and (
                        ts_local.hour == 0
                        or (ts_local - self.last_reset) > timedelta(hours=24)
                    )
                ):
                    self._attr_last_reset = dt_util.as_utc(
                        ts_local.replace(hour=0, minute=0, second=0, microsecond=0)
                    )
        if self.entity_description.key == "powerFactor":
            state *= 100.0
        self._attr_native_value = state
        self.async_write_ha_state()


class TibberRtDataCoordinator(DataUpdateCoordinator):
    """Handle Tibber realtime data."""

    def __init__(self, async_add_entities, tibber_home, hass):
        """Initialize the data handler."""
        self._async_add_entities = async_add_entities
        self._tibber_home = tibber_home
        self.hass = hass
        self._added_sensors = set()
        super().__init__(
            hass,
            _LOGGER,
            name=tibber_home.info["viewer"]["home"]["address"].get(
                "address1", "Tibber"
            ),
        )

        self._async_remove_device_updates_handler = self.async_add_listener(
            self._add_sensors
        )
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._handle_ha_stop)

    @callback
    def _handle_ha_stop(self, _event) -> None:
        """Handle Home Assistant stopping."""
        self._async_remove_device_updates_handler()

    @callback
    def _add_sensors(self):
        """Add sensor."""
        if not (live_measurement := self.get_live_measurement()):
            return

        new_entities = []
        for sensor_description in RT_SENSORS:
            if sensor_description.key in self._added_sensors:
                continue
            state = live_measurement.get(sensor_description.key)
            if state is None:
                continue
            entity = TibberSensorRT(
                self._tibber_home,
                sensor_description,
                state,
                self,
            )
            new_entities.append(entity)
            self._added_sensors.add(sensor_description.key)
        if new_entities:
            self._async_add_entities(new_entities)

    def get_live_measurement(self):
        """Get live measurement data."""
        if errors := self.data.get("errors"):
            _LOGGER.error(errors[0])
            return None
        return self.data.get("data", {}).get("liveMeasurement")


class TibberDataCoordinator(DataUpdateCoordinator):
    """Handle Tibber data and insert statistics."""

    def __init__(self, hass, tibber_connection):
        """Initialize the data handler."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"Tibber {tibber_connection.name}",
            update_interval=timedelta(minutes=20),
        )
        self._tibber_connection = tibber_connection

    async def _async_update_data(self):
        """Update data via API."""
        await self._tibber_connection.fetch_consumption_data_active_homes()
        await self._insert_statistics()

    async def _insert_statistics(self):
        """Insert Tibber statistics."""
        for home in self._tibber_connection.get_homes():
            if not home.hourly_consumption_data:
                continue
            for sensor_type in (
                "consumption",
                "totalCost",
            ):
                statistic_id = (
                    f"{TIBBER_DOMAIN}:energy_"
                    f"{sensor_type.lower()}_"
                    f"{home.home_id.replace('-', '')}"
                )

                last_stats = await self.hass.async_add_executor_job(
                    get_last_statistics, self.hass, 1, statistic_id, True
                )

                if not last_stats:
                    # First time we insert 5 years of data (if available)
                    hourly_consumption_data = await home.get_historic_data(5 * 365 * 24)

                    _sum = 0
                    last_stats_time = None
                else:
                    # hourly_consumption_data contains the last 30 days
                    # of consumption data.
                    # We update the statistics with the last 30 days
                    # of data to handle corrections in the data.
                    hourly_consumption_data = home.hourly_consumption_data

                    start = dt_util.parse_datetime(
                        hourly_consumption_data[0]["from"]
                    ) - timedelta(hours=1)
                    stat = await self.hass.async_add_executor_job(
                        statistics_during_period,
                        self.hass,
                        start,
                        None,
                        [statistic_id],
                        "hour",
                        True,
                    )
                    _sum = stat[statistic_id][0]["sum"]
                    last_stats_time = stat[statistic_id][0]["start"]

                statistics = []

                for data in hourly_consumption_data:
                    if data.get(sensor_type) is None:
                        continue

                    start = dt_util.parse_datetime(data["from"])
                    if last_stats_time is not None and start <= last_stats_time:
                        continue

                    _sum += data[sensor_type]

                    statistics.append(
                        StatisticData(
                            start=start,
                            state=data[sensor_type],
                            sum=_sum,
                        )
                    )

                if sensor_type == "consumption":
                    unit = ENERGY_KILO_WATT_HOUR
                else:
                    unit = home.currency
                metadata = StatisticMetaData(
                    has_mean=False,
                    has_sum=True,
                    name=f"{home.name} {sensor_type}",
                    source=TIBBER_DOMAIN,
                    statistic_id=statistic_id,
                    unit_of_measurement=unit,
                )
                async_add_external_statistics(self.hass, metadata, statistics)
