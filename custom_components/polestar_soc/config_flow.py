"""Config flow for Polestar State of Charge."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigFlow, ConfigFlowResult
from homeassistant.exceptions import ConfigEntryAuthFailed

from .const import DOMAIN, PCCS_ACR_VALUES, PCCS_CLIENT_ID, PCCS_REDIRECT_URI, PCCS_SCOPE
from .coordinator import InvalidOTPError, PolestarAPI

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("email"): str,
        vol.Required("password"): str,
    }
)

STEP_OTP_SCHEMA = vol.Schema(
    {
        vol.Optional("otp", default=""): str,
    }
)


def _valid_token_pair(tokens: Any) -> bool:
    """Require both token values to be nonblank strings without changing them."""
    return isinstance(tokens, dict) and all(
        isinstance(tokens.get(key), str) and bool(tokens[key].strip())
        for key in ("access_token", "refresh_token")
    )


class PolestarSOCConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Polestar SOC."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._email: str = ""
        self._password: str = ""
        self._web_tokens: dict = {}
        self._pccs_session_state: dict | None = None
        self._renewal_entry: str | None = None
        self._renewal_unique_id: str | None = None

    # -- Step: user (email + password) ---------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input["email"]
            password = user_input["password"]

            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            api = PolestarAPI()
            try:
                tokens = await self.hass.async_add_executor_job(api.login, email, password)
            except ConfigEntryAuthFailed:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during login")
                errors["base"] = "cannot_connect"
            else:
                # Validate we can fetch vehicles
                try:
                    vehicles = await self.hass.async_add_executor_job(api.get_vehicles)
                except Exception:
                    _LOGGER.exception("Failed to fetch vehicles")
                    errors["base"] = "cannot_connect"
                else:
                    if not vehicles:
                        errors["base"] = "no_vehicles"
                    else:
                        # Web login succeeded — initiate PCCS 2FA
                        self._email = email
                        self._password = password
                        self._web_tokens = tokens
                        return await self._initiate_pccs_2fa(email, password)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    # -- Step: OTP -----------------------------------------------------------

    async def async_step_otp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the OTP verification step."""
        errors: dict[str, str] = {}

        if self._renewal_entry:
            entry = self.hass.config_entries.async_get_entry(self._renewal_entry)
            if not entry:
                return self.async_abort(reason="entry_missing")
            if (
                entry.domain != DOMAIN
                or entry.unique_id != self._renewal_unique_id
                or entry.unique_id != self.unique_id
            ):
                return self.async_abort(reason="unique_id_mismatch")

        if user_input is not None:
            otp_code = user_input.get("otp", "").strip()

            pccs_tokens: dict = {}
            if self._renewal_entry and not otp_code:
                errors["base"] = "otp_required"
            elif self._renewal_entry and not self._pccs_session_state:
                errors["base"] = "cannot_connect"
            elif otp_code and self._pccs_session_state:
                # User provided OTP — complete the PCCS login
                pccs_api = PolestarAPI(
                    client_id=PCCS_CLIENT_ID,
                    redirect_uri=PCCS_REDIRECT_URI,
                )
                try:
                    pccs_tokens = await self.hass.async_add_executor_job(
                        pccs_api.login_complete_2fa,
                        self._pccs_session_state,
                        otp_code,
                    )
                except InvalidOTPError:
                    errors["base"] = "invalid_otp"
                except Exception:
                    _LOGGER.warning("PCCS verification failed")
                    self._pccs_session_state = None
                    if self._renewal_entry:
                        self._clear_pending_auth()
                        return self._renewal_error("cannot_connect")
                    errors["base"] = "cannot_connect"
                else:
                    if not _valid_token_pair(pccs_tokens):
                        self._pccs_session_state = None
                        if self._renewal_entry:
                            self._clear_pending_auth()
                            return self._renewal_error("cannot_connect")
                        errors["base"] = "cannot_connect"

            if not errors:
                return await self._finish_setup(pccs_tokens)

        return self.async_show_form(
            step_id="otp",
            data_schema=STEP_OTP_SCHEMA,
            errors=errors,
        )

    # -- Step: reauth --------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle re-authentication."""
        if not self._bind_renewal_entry():
            return self.async_abort(reason="entry_missing")
        return await self.async_step_reauth_confirm()

    async def async_step_reconfigure(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Renew authentication on the existing entry using HA's supported entry point."""
        if not self._bind_renewal_entry():
            return self.async_abort(reason="entry_missing")
        return await self.async_step_reconfigure_confirm()

    async def async_step_reconfigure_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect credentials for an existing entry without creating another one."""
        return await self._renewal_confirm(user_input, "reconfigure_confirm")

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle re-authentication confirmation."""
        return await self._renewal_confirm(user_input, "reauth_confirm")

    def _bind_renewal_entry(self) -> bool:
        """Keep a stable target entry ID throughout the renewal."""
        entry_id = self.context.get("entry_id")
        entry = self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
        if not entry or entry.domain != DOMAIN or not entry.unique_id:
            return False
        self._renewal_entry = entry_id
        self._renewal_unique_id = entry.unique_id
        return True

    async def _renewal_confirm(
        self, user_input: dict[str, Any] | None, step_id: str
    ) -> ConfigFlowResult:
        """Validate the existing account and its web vehicle access before PCCS."""
        errors: dict[str, str] = {}

        if not self._renewal_entry and not self._bind_renewal_entry():
            return self.async_abort(reason="entry_missing")
        entry = self.hass.config_entries.async_get_entry(self._renewal_entry)
        if not entry:
            return self.async_abort(reason="entry_missing")
        if entry.domain != DOMAIN or entry.unique_id != self._renewal_unique_id:
            return self.async_abort(reason="unique_id_mismatch")

        if user_input is not None:
            email = user_input["email"]
            password = user_input["password"]

            await self.async_set_unique_id(email.strip().lower())
            self._abort_if_unique_id_mismatch()

            api = PolestarAPI()
            try:
                tokens = await self.hass.async_add_executor_job(api.login, email, password)
            except ConfigEntryAuthFailed:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.warning("Web renewal login failed")
                errors["base"] = "cannot_connect"
            else:
                try:
                    vehicles = await self.hass.async_add_executor_job(api.get_vehicles)
                except Exception:
                    _LOGGER.warning("Web renewal vehicle check failed")
                    errors["base"] = "cannot_connect"
                else:
                    if not vehicles:
                        errors["base"] = "no_vehicles"
                    elif not _valid_token_pair(tokens):
                        errors["base"] = "invalid_auth"
                    else:
                        self._email = email
                        self._password = password
                        self._web_tokens = tokens
                        return await self._initiate_pccs_2fa(email, password)

        return self.async_show_form(
            step_id=step_id,
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    # -- Private helpers -----------------------------------------------------

    async def _initiate_pccs_2fa(self, email: str, password: str) -> ConfigFlowResult:
        """Trigger the PCCS 2FA email and proceed to the OTP step."""
        pccs_api = PolestarAPI(
            client_id=PCCS_CLIENT_ID,
            redirect_uri=PCCS_REDIRECT_URI,
        )
        try:
            result = await self.hass.async_add_executor_job(
                pccs_api.login_start_2fa,
                email,
                password,
                PCCS_SCOPE,
                PCCS_ACR_VALUES,
            )
        except Exception:
            _LOGGER.warning("Failed to initiate PCCS 2FA login")
            self._pccs_session_state = None
            if self._renewal_entry:
                self._clear_pending_auth()
                return self._renewal_error("cannot_connect")
            # Initial setup still permits web-only configuration.
            return await self.async_step_otp()

        if not isinstance(result, dict):
            if self._renewal_entry:
                self._clear_pending_auth()
                return self._renewal_error("cannot_connect")
            return await self.async_step_otp()

        if result.get("needs_otp"):
            self._pccs_session_state = result.get("_session_state")
            if self._renewal_entry and not self._pccs_session_state:
                return self._renewal_error("cannot_connect")
        else:
            # No 2FA required — tokens returned directly, skip OTP step
            if self._renewal_entry and not _valid_token_pair(result):
                return self._renewal_error("cannot_connect")
            return await self._finish_setup(result)

        return await self.async_step_otp()

    def _renewal_error(self, error: str) -> ConfigFlowResult:
        """Return to the correct existing-entry credentials form."""
        step_id = "reconfigure_confirm" if self.source == SOURCE_RECONFIGURE else "reauth_confirm"
        return self.async_show_form(
            step_id=step_id, data_schema=STEP_USER_DATA_SCHEMA, errors={"base": error}
        )

    def _clear_pending_auth(self) -> None:
        """Discard an uncertain or failed authentication transaction."""
        self._pccs_session_state = None
        self._web_tokens = {}
        self._password = ""

    async def _finish_setup(self, pccs_tokens: dict) -> ConfigFlowResult:
        """Create or update the config entry with web + PCCS tokens."""
        if self._renewal_entry and not _valid_token_pair(pccs_tokens):
            self._clear_pending_auth()
            return self._renewal_error("cannot_connect")
        entry_data = {
            "email": self._email,
            "password": self._password,
            "access_token": self._web_tokens["access_token"],
            "refresh_token": self._web_tokens.get("refresh_token"),
            "pccs_access_token": pccs_tokens.get("access_token"),
            "pccs_refresh_token": pccs_tokens.get("refresh_token"),
        }

        if self._renewal_entry:
            entry = self.hass.config_entries.async_get_entry(self._renewal_entry)
            if not entry:
                return self.async_abort(reason="entry_missing")
            if (
                entry.domain != DOMAIN
                or entry.unique_id != self._renewal_unique_id
                or entry.unique_id != self.unique_id
            ):
                return self.async_abort(reason="unique_id_mismatch")
            return self.async_update_reload_and_abort(entry, data_updates=entry_data)

        return self.async_create_entry(title=self._email, data=entry_data)
