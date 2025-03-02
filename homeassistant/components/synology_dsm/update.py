"""Support for Synology DSM update platform."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from synology_dsm.api.core.upgrade import SynoCoreUpgrade
from yarl import URL

from homeassistant.components.update import UpdateEntity, UpdateEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SynoApi, SynologyDSMBaseEntity
from .const import COORDINATOR_CENTRAL, DOMAIN, SYNO_API, SynologyDSMEntityDescription


@dataclass
class SynologyDSMUpdateEntityEntityDescription(
    UpdateEntityDescription, SynologyDSMEntityDescription
):
    """Describes Synology DSM update entity."""


UPDATE_ENTITIES: Final = [
    SynologyDSMUpdateEntityEntityDescription(
        api_key=SynoCoreUpgrade.API_KEY,
        key="update",
        name="DSM Update",
        entity_category=EntityCategory.DIAGNOSTIC,
    )
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Synology DSM update entities."""
    data = hass.data[DOMAIN][entry.unique_id]
    api: SynoApi = data[SYNO_API]
    coordinator = data[COORDINATOR_CENTRAL]

    async_add_entities(
        SynoDSMUpdateEntity(api, coordinator, description)
        for description in UPDATE_ENTITIES
    )


class SynoDSMUpdateEntity(SynologyDSMBaseEntity, UpdateEntity):
    """Mixin for update entity specific attributes."""

    entity_description: SynologyDSMUpdateEntityEntityDescription
    _attr_title = "Synology DSM"

    @property
    def current_version(self) -> str | None:
        """Version currently in use."""
        return self._api.information.version_string  # type: ignore[no-any-return]

    @property
    def latest_version(self) -> str | None:
        """Latest version available for install."""
        if not self._api.upgrade.update_available:
            return self.current_version
        return self._api.upgrade.available_version  # type: ignore[no-any-return]

    @property
    def release_url(self) -> str | None:
        """URL to the full release notes of the latest version available."""
        if (details := self._api.upgrade.available_version_details) is None:
            return None

        url = URL("http://update.synology.com/autoupdate/whatsnew.php")
        query = {"model": self._api.information.model}
        if details.get("nano") > 0:
            query["update_version"] = f"{details['buildnumber']}-{details['nano']}"
        else:
            query["update_version"] = details["buildnumber"]

        return url.update_query(query).human_repr()
