"""Set up Mantel (one per Home Assistant) and change its options."""

from __future__ import annotations

from typing import Any

try:  # Home Assistant 2026.x validates with probatio; older releases with voluptuous
    import probatio as vol
except ImportError:  # pragma: no cover
    import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TimeSelector,
)

from .const import (
    CONF_CHILD_MODE,
    CONF_CHILD_NAME,
    CONF_CHILD_NOTIFY,
    CONF_CHILD_RULES,
    CONF_DAILY_TIME,
    CONF_DASHBOARD_URL,
    CONF_HOUSE_RULES,
    CONF_KEEP_DAYS,
    CONF_NOTIFY,
    CONF_OWNER_NAME,
    CONF_WEEKLY_DAY,
    CONF_WEEKLY_TIME,
    DEFAULT_CHILD_NAME,
    DEFAULT_CHILD_RULES,
    DEFAULT_DAILY_TIME,
    DEFAULT_HOUSE_RULES,
    DEFAULT_KEEP_DAYS,
    DEFAULT_OWNER_NAME,
    DEFAULT_WEEKLY_DAY,
    DEFAULT_WEEKLY_TIME,
    DOMAIN,
    WEEKDAYS,
)

DEFAULTS: dict[str, Any] = {
    CONF_OWNER_NAME: DEFAULT_OWNER_NAME,
    CONF_NOTIFY: "",
    CONF_KEEP_DAYS: DEFAULT_KEEP_DAYS,
    CONF_DASHBOARD_URL: "",
    CONF_HOUSE_RULES: DEFAULT_HOUSE_RULES,
    CONF_CHILD_MODE: False,
    CONF_CHILD_NAME: DEFAULT_CHILD_NAME,
    CONF_CHILD_NOTIFY: "",
    CONF_CHILD_RULES: DEFAULT_CHILD_RULES,
    CONF_DAILY_TIME: DEFAULT_DAILY_TIME,
    CONF_WEEKLY_DAY: DEFAULT_WEEKLY_DAY,
    CONF_WEEKLY_TIME: DEFAULT_WEEKLY_TIME,
}


def _notify_selector(hass: HomeAssistant) -> SelectSelector:
    services = sorted(f"notify.{s}" for s in hass.services.async_services_for_domain("notify"))
    return SelectSelector(
        SelectSelectorConfig(options=services, custom_value=True, mode=SelectSelectorMode.DROPDOWN)
    )


def _check(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    for key in (CONF_NOTIFY, CONF_CHILD_NOTIFY):
        target = str(user_input.get(key) or "").strip()
        if not target:
            continue
        domain, _, service = target.partition(".")
        if domain != "notify" or not service or not hass.services.has_service(domain, service):
            errors[key] = "unknown_notify"
    url = str(user_input.get(CONF_DASHBOARD_URL) or "").strip()
    if url and not url.startswith("/"):
        errors[CONF_DASHBOARD_URL] = "relative_url"
    return errors


def _setup_schema(hass: HomeAssistant, v: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_OWNER_NAME, default=v[CONF_OWNER_NAME]): TextSelector(),
            vol.Optional(CONF_NOTIFY, description={"suggested_value": v[CONF_NOTIFY]}): _notify_selector(hass),
            vol.Optional(CONF_KEEP_DAYS, default=v[CONF_KEEP_DAYS]): NumberSelector(
                NumberSelectorConfig(min=1, max=365, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="days")
            ),
            vol.Optional(CONF_CHILD_MODE, default=v[CONF_CHILD_MODE]): BooleanSelector(),
        }
    )


def _options_schema(hass: HomeAssistant, v: dict[str, Any]) -> vol.Schema:
    multiline = TextSelector(TextSelectorConfig(multiline=True))
    return vol.Schema(
        {
            vol.Optional(CONF_OWNER_NAME, default=v[CONF_OWNER_NAME]): TextSelector(),
            vol.Optional(CONF_NOTIFY, description={"suggested_value": v[CONF_NOTIFY]}): _notify_selector(hass),
            vol.Optional(CONF_DASHBOARD_URL, description={"suggested_value": v[CONF_DASHBOARD_URL]}): TextSelector(),
            vol.Optional(CONF_KEEP_DAYS, default=v[CONF_KEEP_DAYS]): NumberSelector(
                NumberSelectorConfig(min=1, max=365, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="days")
            ),
            vol.Optional(CONF_HOUSE_RULES, default=v[CONF_HOUSE_RULES]): multiline,
            vol.Optional(CONF_CHILD_MODE, default=v[CONF_CHILD_MODE]): BooleanSelector(),
            vol.Optional(CONF_CHILD_NAME, default=v[CONF_CHILD_NAME]): TextSelector(),
            vol.Optional(CONF_CHILD_NOTIFY, description={"suggested_value": v[CONF_CHILD_NOTIFY]}): _notify_selector(hass),
            vol.Optional(CONF_CHILD_RULES, default=v[CONF_CHILD_RULES]): multiline,
            vol.Optional(CONF_DAILY_TIME, default=v[CONF_DAILY_TIME]): TimeSelector(),
            vol.Optional(CONF_WEEKLY_DAY, default=v[CONF_WEEKLY_DAY]): SelectSelector(
                SelectSelectorConfig(options=WEEKDAYS, translation_key="weekday", mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Optional(CONF_WEEKLY_TIME, default=v[CONF_WEEKLY_TIME]): TimeSelector(),
        }
    )


def _tidy(user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    if CONF_KEEP_DAYS in out:
        out[CONF_KEEP_DAYS] = int(out[CONF_KEEP_DAYS])
    for key in (CONF_NOTIFY, CONF_CHILD_NOTIFY, CONF_DASHBOARD_URL):
        out[key] = str(out.get(key) or "").strip()
    return out


class MantelConfigFlow(ConfigFlow, domain=DOMAIN):
    """One Mantel per Home Assistant."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _check(self.hass, user_input)
            if not errors:
                return self.async_create_entry(title="Mantel", data={}, options={**DEFAULTS, **_tidy(user_input)})
        values = {**DEFAULTS, **(user_input or {})}
        return self.async_show_form(step_id="user", data_schema=_setup_schema(self.hass, values), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MantelOptionsFlow()


class MantelOptionsFlow(OptionsFlow):
    """Who gets alerts, how long posts stay, the house rules and child mode."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _check(self.hass, user_input)
            if not errors:
                # cleared optional fields arrive missing; store them as empty
                return self.async_create_entry(
                    data={**DEFAULTS, CONF_NOTIFY: "", CONF_CHILD_NOTIFY: "", CONF_DASHBOARD_URL: "", **_tidy(user_input)}
                )
        values = {**DEFAULTS, **self.config_entry.options, **(user_input or {})}
        return self.async_show_form(step_id="init", data_schema=_options_schema(self.hass, values), errors=errors)
