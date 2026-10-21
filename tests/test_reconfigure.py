"""Existing-entry authentication renewal through Home Assistant's config flow."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.polestar_soc.config_flow import PolestarSOCConfigFlow
from custom_components.polestar_soc.coordinator import InvalidOTPError, PolestarAPI

WEB = {"access_token": "new-web", "refresh_token": "new-web-refresh"}
PCCS = {"access_token": "new-pccs", "refresh_token": "new-pccs-refresh"}
ORIGINAL = {
    "email": "test@polestar.com",
    "password": "old-password",
    "access_token": "old-web",
    "refresh_token": "old-web-refresh",
    "pccs_access_token": "old-pccs",
    "pccs_refresh_token": "old-pccs-refresh",
    "custom_setting": "keep-me",
}


@pytest.fixture
def existing(hass: HomeAssistant):
    """A configured account with unrelated data and settings."""
    entry = MockConfigEntry(
        domain="polestar_soc",
        unique_id="test@polestar.com",
        data=ORIGINAL,
        options={"setting": "keep-this-too"},
    )
    entry.add_to_hass(hass)
    return entry


def renewal_flow(hass, entry, source=config_entries.SOURCE_RECONFIGURE):
    flow = PolestarSOCConfigFlow()
    flow.hass = hass
    flow.context = {"source": source, "entry_id": entry.entry_id}
    return flow


def mock_apis(*, vehicles=None, start=None, complete=None):
    web = MagicMock()
    web.login.return_value = WEB
    web.get_vehicles.return_value = vehicles if vehicles is not None else [{"vin": "TESTVIN"}]
    mobile = MagicMock()
    mobile.login_start_2fa.return_value = (
        start
        if start is not None
        else {
            "needs_otp": True,
            "_session_state": {"session": MagicMock(), "otp_resume": "url", "resume_url": "url"},
        }
    )
    finish = MagicMock()
    finish.login_complete_2fa.return_value = complete if complete is not None else PCCS
    return web, mobile, finish


async def test_reconfigure_renews_existing_entry_with_one_reload(hass: HomeAssistant, existing):
    """Standard reconfigure keeps the entry and settings while renewing both logins."""
    flow = renewal_flow(hass, existing)
    web, mobile, finish = mock_apis()
    with (
        patch(
            "custom_components.polestar_soc.config_flow.PolestarAPI",
            side_effect=[web, mobile, finish],
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        start = await flow.async_step_reconfigure(existing.data)
        assert start["type"] is FlowResultType.FORM
        assert start["step_id"] == "reconfigure_confirm"
        challenge = await flow.async_step_reconfigure_confirm(
            {"email": "test@polestar.com", "password": "new-password"}
        )
        assert challenge["step_id"] == "otp"
        done = await flow.async_step_otp({"otp": "123456"})
        assert done["type"] is FlowResultType.ABORT
        assert done["reason"] == "reconfigure_successful"
        reload.assert_called_once_with(existing.entry_id)
    web.get_vehicles.assert_called_once()
    finish.login_complete_2fa.assert_called_once()
    assert existing.data == {
        **ORIGINAL,
        "password": "new-password",
        "access_token": WEB["access_token"],
        "refresh_token": WEB["refresh_token"],
        "pccs_access_token": PCCS["access_token"],
        "pccs_refresh_token": PCCS["refresh_token"],
    }
    assert existing.unique_id == "test@polestar.com"
    assert existing.options == {"setting": "keep-this-too"}


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_RECONFIGURE, config_entries.SOURCE_REAUTH]
)
async def test_wrong_account_aborts_before_provider_login(hass, existing, source):
    flow = renewal_flow(hass, existing, source)
    with patch("custom_components.polestar_soc.config_flow.PolestarAPI") as provider:
        start = await (
            flow.async_step_reconfigure(existing.data)
            if source == config_entries.SOURCE_RECONFIGURE
            else flow.async_step_reauth(existing.data)
        )
        assert start["type"] is FlowResultType.FORM
        with pytest.raises(Exception) as abort:
            await (
                flow.async_step_reconfigure_confirm(
                    {"email": "other@polestar.com", "password": "secret"}
                )
                if source == config_entries.SOURCE_RECONFIGURE
                else flow.async_step_reauth_confirm(
                    {"email": "other@polestar.com", "password": "secret"}
                )
            )
        assert getattr(abort.value, "reason", None) == "unique_id_mismatch"
        provider.assert_not_called()
    assert existing.data == ORIGINAL


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_RECONFIGURE, config_entries.SOURCE_REAUTH]
)
async def test_renewal_requires_vehicle_access(hass, existing, source):
    flow = renewal_flow(hass, existing, source)
    web, mobile, _ = mock_apis()
    web.get_vehicles.return_value = []
    with patch("custom_components.polestar_soc.config_flow.PolestarAPI", side_effect=[web, mobile]):
        await (
            flow.async_step_reconfigure(existing.data)
            if source == config_entries.SOURCE_RECONFIGURE
            else flow.async_step_reauth(existing.data)
        )
        result = await (
            flow.async_step_reconfigure_confirm(
                {"email": "test@polestar.com", "password": "secret"}
            )
            if source == config_entries.SOURCE_RECONFIGURE
            else flow.async_step_reauth_confirm(
                {"email": "test@polestar.com", "password": "secret"}
            )
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "no_vehicles"
    mobile.login_start_2fa.assert_not_called()
    assert existing.data == ORIGINAL


async def test_incomplete_web_tokens_cannot_begin_mobile_auth(hass, existing):
    flow = renewal_flow(hass, existing)
    web, mobile, _ = mock_apis()
    web.login.return_value = {"access_token": "new-web"}
    with patch("custom_components.polestar_soc.config_flow.PolestarAPI", side_effect=[web, mobile]):
        await flow.async_step_reconfigure(existing.data)
        result = await flow.async_step_reconfigure_confirm(
            {"email": "test@polestar.com", "password": "secret"}
        )
    assert result["step_id"] == "reconfigure_confirm"
    assert result["errors"]["base"] == "invalid_auth"
    mobile.login_start_2fa.assert_not_called()
    assert existing.data == ORIGINAL


@pytest.mark.parametrize("field", ["access_token", "refresh_token"])
@pytest.mark.parametrize("value", [True, 123, {"token": "value"}, "   ", "", None])
@pytest.mark.parametrize("stage", ["web", "pccs_direct", "pccs_otp", "final_update"])
async def test_malformed_renewal_tokens_never_update_or_reload(hass, existing, stage, field, value):
    flow = renewal_flow(hass, existing)
    web, mobile, finish = mock_apis()
    malformed = {**(WEB if stage == "web" else PCCS), field: value}
    if stage == "web":
        web.login.return_value = malformed
    elif stage == "pccs_direct":
        mobile.login_start_2fa.return_value = malformed
    elif stage == "pccs_otp":
        finish.login_complete_2fa.return_value = malformed
    with (
        patch(
            "custom_components.polestar_soc.config_flow.PolestarAPI",
            side_effect=[web, mobile, finish],
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await flow.async_step_reconfigure(existing.data)
        if stage == "final_update":
            flow._email = "test@polestar.com"
            flow._password = "new-password"
            flow._web_tokens = WEB
            await flow.async_set_unique_id("test@polestar.com")
            result = await flow._finish_setup(malformed)
        else:
            result = await flow.async_step_reconfigure_confirm(
                {"email": "test@polestar.com", "password": "new-password"}
            )
            if stage == "pccs_otp":
                assert result["step_id"] == "otp"
                result = await flow.async_step_otp({"otp": "123456"})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reconfigure_confirm"
        assert result["errors"]["base"] == ("invalid_auth" if stage == "web" else "cannot_connect")
        reload.assert_not_called()
    assert existing.data == ORIGINAL


@pytest.mark.parametrize("stage", ["pccs_direct", "pccs_otp"])
async def test_nonblank_token_values_are_preserved_without_normalization(hass, existing, stage):
    flow = renewal_flow(hass, existing)
    web, mobile, finish = mock_apis()
    padded_web = {"access_token": " web access ", "refresh_token": " web refresh "}
    padded_pccs = {"access_token": " pccs access ", "refresh_token": " pccs refresh "}
    web.login.return_value = padded_web
    if stage == "pccs_direct":
        mobile.login_start_2fa.return_value = padded_pccs
    else:
        finish.login_complete_2fa.return_value = padded_pccs
    with (
        patch(
            "custom_components.polestar_soc.config_flow.PolestarAPI",
            side_effect=[web, mobile, finish],
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await flow.async_step_reconfigure(existing.data)
        result = await flow.async_step_reconfigure_confirm(
            {"email": "test@polestar.com", "password": "new-password"}
        )
        if stage == "pccs_otp":
            result = await flow.async_step_otp({"otp": "123456"})
        assert result["reason"] == "reconfigure_successful"
        reload.assert_called_once_with(existing.entry_id)
    assert existing.data["access_token"] == padded_web["access_token"]
    assert existing.data["refresh_token"] == padded_web["refresh_token"]
    assert existing.data["pccs_access_token"] == padded_pccs["access_token"]
    assert existing.data["pccs_refresh_token"] == padded_pccs["refresh_token"]


async def test_blank_renewal_otp_keeps_old_tokens_and_requests_code(hass, existing):
    flow = renewal_flow(hass, existing)
    web, mobile, _ = mock_apis()
    with (
        patch("custom_components.polestar_soc.config_flow.PolestarAPI", side_effect=[web, mobile]),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await flow.async_step_reconfigure(existing.data)
        await flow.async_step_reconfigure_confirm(
            {"email": "test@polestar.com", "password": "secret"}
        )
        result = await flow.async_step_otp({"otp": "   "})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "otp_required"
    assert existing.data == ORIGINAL
    reload.assert_not_called()


@pytest.mark.parametrize(
    "failure", [RuntimeError("canary session?token=secret"), {"access_token": "only-one"}]
)
async def test_pccs_init_failure_or_incomplete_tokens_cannot_complete(
    hass, existing, failure, caplog
):
    flow = renewal_flow(hass, existing)
    web, mobile, _ = mock_apis()
    if isinstance(failure, Exception):
        mobile.login_start_2fa.side_effect = failure
    else:
        mobile.login_start_2fa.return_value = failure
    with (
        patch("custom_components.polestar_soc.config_flow.PolestarAPI", side_effect=[web, mobile]),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await flow.async_step_reconfigure(existing.data)
        result = await flow.async_step_reconfigure_confirm(
            {"email": "test@polestar.com", "password": "secret"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]["base"] == "cannot_connect"
        assert result["step_id"] == "reconfigure_confirm"
    assert existing.data == ORIGINAL
    reload.assert_not_called()
    assert "canary session" not in caplog.text


async def test_malformed_pccs_initiation_fails_closed_without_provider_error_leak(
    hass, existing, caplog
):
    flow = renewal_flow(hass, existing)
    web, mobile, _ = mock_apis()
    mobile.login_start_2fa.return_value = None
    with (
        patch("custom_components.polestar_soc.config_flow.PolestarAPI", side_effect=[web, mobile]),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await flow.async_step_reconfigure(existing.data)
        result = await flow.async_step_reconfigure_confirm(
            {"email": "test@polestar.com", "password": "secret-canary"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_confirm"
    assert result["errors"]["base"] == "cannot_connect"
    assert "secret-canary" not in caplog.text
    assert existing.data == ORIGINAL
    reload.assert_not_called()


@pytest.mark.parametrize("complete", [None, {}, {"access_token": "one", "refresh_token": ""}])
async def test_pccs_completion_requires_both_tokens(hass, existing, complete):
    flow = renewal_flow(hass, existing)
    web, mobile, finish = mock_apis(complete=complete)
    finish.login_complete_2fa.return_value = complete
    with (
        patch(
            "custom_components.polestar_soc.config_flow.PolestarAPI",
            side_effect=[web, mobile, finish],
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await flow.async_step_reconfigure(existing.data)
        await flow.async_step_reconfigure_confirm(
            {"email": "test@polestar.com", "password": "secret"}
        )
        result = await flow.async_step_otp({"otp": "123456"})
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]["base"] == "cannot_connect"
        assert result["step_id"] == "reconfigure_confirm"
        assert flow._pccs_session_state is None
    assert existing.data == ORIGINAL
    reload.assert_not_called()


async def test_deleted_entry_at_completion_fails_closed(hass, existing):
    flow = renewal_flow(hass, existing)
    web, mobile, finish = mock_apis()
    with (
        patch(
            "custom_components.polestar_soc.config_flow.PolestarAPI",
            side_effect=[web, mobile, finish],
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await flow.async_step_reconfigure(existing.data)
        await flow.async_step_reconfigure_confirm(
            {"email": "test@polestar.com", "password": "secret"}
        )
        with patch.object(hass.config_entries, "async_get_entry", return_value=None):
            result = await flow.async_step_otp({"otp": "123456"})
    assert result["reason"] == "entry_missing"
    reload.assert_not_called()
    finish.login_complete_2fa.assert_not_called()


async def test_unexpected_otp_failure_discards_challenge_and_redacts_logs(hass, existing, caplog):
    flow = renewal_flow(hass, existing)
    web, mobile, finish = mock_apis()
    finish.login_complete_2fa.side_effect = RuntimeError("secret-otp?token=canary")
    with (
        patch(
            "custom_components.polestar_soc.config_flow.PolestarAPI",
            side_effect=[web, mobile, finish],
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await flow.async_step_reconfigure(existing.data)
        await flow.async_step_reconfigure_confirm(
            {"email": "test@polestar.com", "password": "secret"}
        )
        result = await flow.async_step_otp({"otp": "secret-otp"})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reconfigure_confirm"
        assert result["errors"]["base"] == "cannot_connect"
        assert flow._pccs_session_state is None
        assert flow._web_tokens == {}
        assert flow._password == ""
    finish.login_complete_2fa.assert_called_once()
    assert "secret-otp" not in caplog.text
    assert "canary" not in caplog.text
    assert existing.data == ORIGINAL
    reload.assert_not_called()


async def test_confirmed_invalid_otp_reuses_same_challenge(hass, existing):
    flow = renewal_flow(hass, existing)
    web, mobile, bad = mock_apis()
    bad.login_complete_2fa.side_effect = InvalidOTPError("invalid code")
    good = MagicMock()
    good.login_complete_2fa.return_value = PCCS
    with (
        patch(
            "custom_components.polestar_soc.config_flow.PolestarAPI",
            side_effect=[web, mobile, bad, good],
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await flow.async_step_reconfigure(existing.data)
        await flow.async_step_reconfigure_confirm(
            {"email": "test@polestar.com", "password": "secret"}
        )
        session = flow._pccs_session_state
        response = await flow.async_step_otp({"otp": "bad"})
        assert response["errors"]["base"] == "invalid_otp"
        assert flow._pccs_session_state is session
        done = await flow.async_step_otp({"otp": "good"})
        assert done["reason"] == "reconfigure_successful"
        reload.assert_called_once_with(existing.entry_id)
    assert bad.login_complete_2fa.call_args.args[0] is session
    assert good.login_complete_2fa.call_args.args[0] is session


async def test_invalid_otp_page_missing_csrf_discards_renewal_challenge(hass, existing):
    flow = renewal_flow(hass, existing)
    web, mobile, _ = mock_apis()
    state = mobile.login_start_2fa.return_value["_session_state"]
    state["otp_resume"] = (
        "https://polestarid.eu.polestar.com/as/otp789/resume/as/authorization.ping"
    )
    state["csrf_token"] = "old-csrf"
    state["code_verifier"] = "verifier"
    state["session"].post.return_value = MagicMock(
        status_code=200,
        text=('<form action="/as/otp789/resume/as/authorization.ping">Invalid OTP code</form>'),
        headers={},
    )
    with (
        patch(
            "custom_components.polestar_soc.config_flow.PolestarAPI",
            side_effect=[web, mobile, PolestarAPI()],
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await flow.async_step_reconfigure(existing.data)
        await flow.async_step_reconfigure_confirm(
            {"email": "test@polestar.com", "password": "secret"}
        )
        result = await flow.async_step_otp({"otp": "wrong"})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reconfigure_confirm"
        assert result["errors"]["base"] == "cannot_connect"
        assert flow._pccs_session_state is None
        assert flow._web_tokens == {}
        reload.assert_not_called()
    assert state["csrf_token"] == "old-csrf"
    assert existing.data == ORIGINAL


async def test_reauth_renews_same_entry_with_one_reload(hass, existing):
    flow = renewal_flow(hass, existing, config_entries.SOURCE_REAUTH)
    web, mobile, finish = mock_apis()
    with (
        patch(
            "custom_components.polestar_soc.config_flow.PolestarAPI",
            side_effect=[web, mobile, finish],
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        start = await flow.async_step_reauth(existing.data)
        assert start["step_id"] == "reauth_confirm"
        challenge = await flow.async_step_reauth_confirm(
            {"email": "TEST@polestar.com", "password": "updated"}
        )
        assert challenge["step_id"] == "otp"
        done = await flow.async_step_otp({"otp": "123456"})
        assert done["reason"] == "reauth_successful"
        reload.assert_called_once_with(existing.entry_id)
    assert existing.data["custom_setting"] == "keep-me"
    assert existing.options == {"setting": "keep-this-too"}
    assert existing.unique_id == "test@polestar.com"


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_RECONFIGURE, config_entries.SOURCE_REAUTH]
)
async def test_missing_entry_at_start_aborts_before_provider(hass, existing, source):
    flow = renewal_flow(hass, existing, source)
    with (
        patch.object(hass.config_entries, "async_get_entry", return_value=None),
        patch("custom_components.polestar_soc.config_flow.PolestarAPI") as provider,
    ):
        result = await (
            flow.async_step_reconfigure(existing.data)
            if source == config_entries.SOURCE_RECONFIGURE
            else flow.async_step_reauth(existing.data)
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_missing"
    provider.assert_not_called()


async def test_entry_identity_changed_mid_flow_aborts_without_reload(hass, existing):
    flow = renewal_flow(hass, existing)
    web, mobile, finish = mock_apis()
    with (
        patch(
            "custom_components.polestar_soc.config_flow.PolestarAPI",
            side_effect=[web, mobile, finish],
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await flow.async_step_reconfigure(existing.data)
        await flow.async_step_reconfigure_confirm(
            {"email": "test@polestar.com", "password": "secret"}
        )
        hass.config_entries.async_update_entry(existing, unique_id="other@polestar.com")
        done = await flow.async_step_otp({"otp": "123456"})
        assert done["reason"] == "unique_id_mismatch"
        reload.assert_not_called()
    assert existing.data == ORIGINAL
    finish.login_complete_2fa.assert_not_called()


async def test_entry_identity_changed_before_submission_rejects_new_account_without_login(
    hass, existing
):
    flow = renewal_flow(hass, existing)
    await flow.async_step_reconfigure(existing.data)
    hass.config_entries.async_update_entry(existing, unique_id="other@polestar.com")
    with patch("custom_components.polestar_soc.config_flow.PolestarAPI") as provider:
        result = await flow.async_step_reconfigure_confirm(
            {"email": "other@polestar.com", "password": "new-secret"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    provider.assert_not_called()
    assert existing.data == ORIGINAL


async def test_direct_pccs_tokens_can_renew_without_otp(hass, existing):
    flow = renewal_flow(hass, existing)
    web, mobile, _ = mock_apis(start=PCCS)
    with (
        patch("custom_components.polestar_soc.config_flow.PolestarAPI", side_effect=[web, mobile]),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await flow.async_step_reconfigure(existing.data)
        done = await flow.async_step_reconfigure_confirm(
            {"email": "test@polestar.com", "password": "secret"}
        )
        assert done["reason"] == "reconfigure_successful"
        reload.assert_called_once_with(existing.entry_id)
    assert existing.data["pccs_refresh_token"] == PCCS["refresh_token"]


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json"])
def test_renewal_ui_has_complete_english_labels(filename):
    path = Path(__file__).parents[1] / "custom_components/polestar_soc" / filename
    text = json.loads(path.read_text())["config"]
    assert "reconfigure_confirm" in text["step"]
    for reason in (
        "entry_missing",
        "unique_id_mismatch",
        "reauth_successful",
        "reconfigure_successful",
    ):
        assert reason in text["abort"]
    assert "otp_required" in text["error"]
    assert "renew" in text["step"]["otp"]["description"].lower()


def test_manifest_marks_new_immutable_patch():
    path = Path(__file__).parents[1] / "custom_components/polestar_soc/manifest.json"
    assert json.loads(path.read_text())["version"] == "1.1.0-simon.2"
