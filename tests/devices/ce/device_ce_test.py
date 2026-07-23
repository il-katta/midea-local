"""Test CE Device (standard and Clivet VMC)."""

from unittest.mock import patch

import pytest

from midealocal.const import ProtocolVersion
from midealocal.devices.ce import (
    ClivetVMCDevice,
    DeviceAttributes,
    MideaAppliance,
    MideaCEDevice,
)
from midealocal.devices.ce.clivet import (
    ClivetVMCMessageBody,
    ClivetVMCMessageSet,
)

# Real captures from the Clivet Elfofresh EVO 171120H4 (8-byte body).
# 2025-10: ventilation, setpoint 17, ambient 27
BODY_VENTILATION = bytearray([0x01, 0x01, 0x03, 0x00, 0x11, 0x1B, 0x2C, 0x00])
# 2026-07-22: cooling, setpoint 24, ambient 27 (live read)
BODY_COOLING = bytearray([0x01, 0x01, 0x01, 0x00, 0x18, 0x1B, 0x2C, 0x00])
# Synthetic from the byte map verified 2025-10-26: heating, silent fan
BODY_HEATING_SILENT = bytearray([0x01, 0x05, 0x02, 0x01, 0x16, 0x1B, 0x2C, 0x00])
# 2026-07-23: auto mode actively cooling, C3 filter alarm active (live read)
BODY_AUTO_COOLING = bytearray([0x01, 0x03, 0x04, 0x00, 0x11, 0x1A, 0x2C, 0x01])

DEVICE_KWARGS = {
    "name": "Test Device",
    "device_id": 1,
    "ip_address": "192.168.1.100",
    "port": 6444,
    "token": "AA",
    "key": "BB",
    "device_protocol": ProtocolVersion.V3,
    "subtype": 0,
    "customize": "",
}


class TestDeviceAttributes:
    """Attribute naming regressions."""

    def test_aux_heating_attribute_name_has_no_apostrophe(self) -> None:
        """Test aux_heating enum value."""
        assert DeviceAttributes.aux_heating.value == "aux_heating"


class TestClivetVMCMessageBody:
    """Decoding of the short Clivet body (fixtures: real captures)."""

    def test_ventilation_frame(self) -> None:
        """Test 2025-10 capture: ventilation, normal fan, setpoint 17, ambient 27."""
        body = ClivetVMCMessageBody(BODY_VENTILATION)
        assert body.power is True
        assert body.mode == "ventilation"
        assert body.fan_level == "normal"
        assert body.target_temperature == 17.0
        assert body.current_temperature == 27.0

    def test_cooling_frame(self) -> None:
        """Test 2026-07-22 live read: cooling, setpoint 24, ambient 27."""
        body = ClivetVMCMessageBody(BODY_COOLING)
        assert body.mode == "cooling"
        assert body.target_temperature == 24.0
        assert body.current_temperature == 27.0

    def test_error_code_from_byte6(self) -> None:
        """Test byte[6] is the error code (44 shows as the C3 filter alarm in the app).

        Confirmed by the official lua parser: mytable["error_code"] = messageBytes[5].
        """
        assert ClivetVMCMessageBody(BODY_COOLING).error_code == 44

    def test_auto_frame_reports_auto_function_and_run_mode(self) -> None:
        """Test 2026-07-23 live read: auto mode, cooling under auto control."""
        body = ClivetVMCMessageBody(BODY_AUTO_COOLING)
        assert body.mode == "auto"
        assert body.auto_set_function is True
        assert body.run_mode_under_auto_control == 1  # same encoding as mode: cool
        assert body.error_code == 44

    def test_power_is_bit0_of_flags(self) -> None:
        """Test power comes from bit0 of the flags byte, not assumed always on."""
        powered_off = bytearray([0x01, 0x00, 0x03, 0x00, 0x11, 0x1A, 0x00, 0x00])
        assert ClivetVMCMessageBody(powered_off).power is False
        assert ClivetVMCMessageBody(BODY_COOLING).power is True

    def test_current_temperature_is_signed(self) -> None:
        """Test temperatures >= 128 decode as negative (official lua behaviour)."""
        cold = bytearray([0x01, 0x01, 0x02, 0x00, 0x16, 0xF6, 0x00, 0x00])
        assert ClivetVMCMessageBody(cold).current_temperature == -10.0

    def test_fan_speed_reports_selector_position(self) -> None:
        """Test fan_speed: selector position percentage, not absolute airflow."""
        assert ClivetVMCMessageBody(BODY_COOLING).fan_speed == 100
        assert ClivetVMCMessageBody(BODY_HEATING_SILENT).fan_speed == 33

    def test_no_fabricated_fields(self) -> None:
        """Test the body only reports what the 8-byte frame carries.

        Regression: filter_change_reminder was hardcoded False while the real
        device showed the C3 filter alarm in the app.
        """
        body = ClivetVMCMessageBody(BODY_COOLING)
        for fabricated in (
            "co2",
            "pm25",
            "filter_change_reminder",
            "filter_cleaning_reminder",
            "eco_mode",
            "sleep_mode",
            "child_lock",
            "unknown_byte6",
        ):
            assert not hasattr(body, fabricated), fabricated

    def test_heating_silent_frame(self) -> None:
        """Test synthetic frame: heating with silent fan."""
        body = ClivetVMCMessageBody(BODY_HEATING_SILENT)
        assert body.mode == "heating"
        assert body.fan_level == "silent"

    def test_silence_raw_fields_are_exposed(self) -> None:
        """Test the two official silence fields are readable alongside fan_level."""
        silent = ClivetVMCMessageBody(BODY_HEATING_SILENT)
        assert silent.silence_function_state is True
        assert silent.silence_function_level is True
        normal = ClivetVMCMessageBody(BODY_COOLING)
        assert normal.silence_function_state is False
        assert normal.silence_function_level is False

    def test_unknown_mode_reads_as_none_with_raw_value(self) -> None:
        """Test unmapped mode: no silent 'ventilation' default, raw value kept."""
        odd = bytearray([0x01, 0x01, 0x05, 0x00, 0x11, 0x1B, 0x00, 0x00])
        body = ClivetVMCMessageBody(odd)
        assert body.mode is None
        assert body.mode_raw == 5

    def test_timer_subtype_body_sets_no_status_attributes(self) -> None:
        """Test a day-timer body (subtype 0x02) is not misread as status."""
        daytimer = bytearray([0x02] + [0] * 25)
        body = ClivetVMCMessageBody(daytimer)
        assert not hasattr(body, "mode")
        assert not hasattr(body, "power")

    def test_truncated_body_sets_no_attributes(self) -> None:
        """Test truncated frames contribute nothing instead of default values."""
        body = ClivetVMCMessageBody(bytearray([0x01, 0x01]))
        assert not hasattr(body, "power")
        assert not hasattr(body, "mode")


class TestClivetVMCMessageSet:
    """Building of the control command (official 5-byte wire body)."""

    def test_wire_body_matches_manufacturer_format(self) -> None:
        """Test heating + silent + 17°C against the frame captured from the app.

        Official frame: aa 0f ce 00..00 02 | 01 05 02 01 11 | 07 — the wire
        body is [subtype, flags, mode, silence_level, setpoint], 5 bytes.
        """
        message = ClivetVMCMessageSet(ProtocolVersion.V3)
        message.mode = "heating"
        message.fan_level = "silent"
        message.target_temperature = 17
        assert message.body == bytearray([0x01, 0x05, 0x02, 0x01, 0x11])

    def test_wire_body_has_no_duplicate_subtype_nor_padding(self) -> None:
        """Test the subtype appears once (from MessageRequest) and no trailing zeros."""
        message = ClivetVMCMessageSet(ProtocolVersion.V3)
        body = message.body
        assert len(body) == 5
        assert body[0] == 0x01
        assert body[1] != 0x01 or body[1] & 0x01  # flags, not a second subtype

    def test_reduced_sets_silence_state_without_level(self) -> None:
        """Test reduced fan: silence state bit on, level stays level_1."""
        message = ClivetVMCMessageSet(ProtocolVersion.V3)
        message.fan_level = "reduced"
        body = message.body
        assert body[1] & 0x04
        assert body[3] == 0x00

    def test_set_rejects_out_of_range_target(self) -> None:
        """Test out-of-range setpoint raises instead of silently clamping."""
        message = ClivetVMCMessageSet(ProtocolVersion.V3)
        message.target_temperature = 35
        with pytest.raises(ValueError, match="target"):
            _ = message.body

    def test_set_rejects_unknown_mode_and_fan_level(self) -> None:
        """Test invalid values raise instead of silently becoming ventilation."""
        message = ClivetVMCMessageSet(ProtocolVersion.V3)
        message.mode = "banana"
        with pytest.raises(ValueError, match="mode"):
            _ = message.body
        message.mode = "heating"
        message.fan_level = "turbo"
        with pytest.raises(ValueError, match="fan_level"):
            _ = message.body


class TestMideaApplianceDispatch:
    """Device selection based on the model."""

    def test_dispatches_clivet_for_known_model(self) -> None:
        """Test Clivet model -> ClivetVMCDevice."""
        device = MideaAppliance(model="171120H4", **DEVICE_KWARGS)
        assert isinstance(device, ClivetVMCDevice)

    def test_standard_ce_for_other_models(self) -> None:
        """Test other models -> standard CE."""
        device = MideaAppliance(model="test_model", **DEVICE_KWARGS)
        assert isinstance(device, MideaCEDevice)
        assert not isinstance(device, ClivetVMCDevice)


class TestClivetVMCDevice:
    """Clivet device behaviour."""

    @pytest.fixture(autouse=True)
    def _setup_device(self) -> None:
        self.device = ClivetVMCDevice(model="171120H4", **DEVICE_KWARGS)

    def test_initial_attributes(self) -> None:
        """Test initial attributes: nothing claimed before the first frame."""
        assert self.device.attributes[DeviceAttributes.power] is None
        assert self.device.attributes[DeviceAttributes.mode] is None
        assert self.device.attributes[DeviceAttributes.target_temperature] is None
        assert self.device.attributes[DeviceAttributes.fan_level] is None
        assert self.device.attributes[DeviceAttributes.error_code] is None
        assert self.device.attributes[DeviceAttributes.auto_set_function] is None
        assert self.device.attributes[DeviceAttributes.run_mode_under_auto_control] is None

    def test_unsupported_attributes_are_absent(self) -> None:
        """Test attributes the frame cannot report are absent, not lying defaults."""
        for absent in (
            DeviceAttributes.co2,
            DeviceAttributes.pm25,
            DeviceAttributes.filter_change_reminder,
            DeviceAttributes.filter_cleaning_reminder,
            DeviceAttributes.eco_mode,
            DeviceAttributes.sleep_mode,
            DeviceAttributes.child_lock,
            DeviceAttributes.scheduled,
            DeviceAttributes.link_to_ac,
            DeviceAttributes.powerful_purify,
            DeviceAttributes.aux_heating,
            DeviceAttributes.current_humidity,
            DeviceAttributes.hcho,
        ):
            assert absent not in self.device.attributes, absent

    def test_modes(self) -> None:
        """Test Clivet modes."""
        assert self.device.preset_modes == [
            "cooling",
            "heating",
            "ventilation",
            "auto",
        ]

    def test_process_message_updates_clivet_attributes(self) -> None:
        """Test process_message: Clivet attributes updated, no sleep/eco synthesis."""
        with patch(
            "midealocal.devices.ce.MessageClivetVMCResponse",
        ) as mock_response:
            message = mock_response.return_value
            message.protocol_version = ProtocolVersion.V3
            message.mode = "heating"
            message.fan_level = "reduced"
            message.target_temperature = 21.0
            message.current_temperature = 19.0
            message.sleep_mode = True  # must not turn mode into "Sleep mode"
            new_status = self.device.process_message(b"")
            assert new_status[DeviceAttributes.mode.value] == "heating"
            assert new_status[DeviceAttributes.fan_level.value] == "reduced"
            assert new_status[DeviceAttributes.target_temperature.value] == 21.0
            assert new_status[DeviceAttributes.current_temperature.value] == 19.0

    def _seed_state(  # noqa: PLR0913
        self,
        power: bool = True,
        auto_set_function: bool = False,
        mode: str = "auto",
        fan_level: str = "normal",
        target: float = 22.0,
    ) -> None:
        """Simula lo stato appreso dal primo frame."""
        with patch(
            "midealocal.devices.ce.MessageClivetVMCResponse",
        ) as mock_response:
            message = mock_response.return_value
            message.protocol_version = ProtocolVersion.V3
            message.power = power
            message.auto_set_function = auto_set_function
            message.mode = mode
            message.fan_level = fan_level
            message.target_temperature = target
            self.device.process_message(b"")

    def test_set_attribute_builds_clivet_set(self) -> None:
        """Test set_attribute: 5-byte wire body, not the standard CE set."""
        self._seed_state()
        with patch.object(self.device, "build_send") as mock_build_send:
            self.device.set_attribute("mode", "heating")
            message = mock_build_send.call_args[0][0]
            assert isinstance(message, ClivetVMCMessageSet)
            assert message.body[2] == 0x02  # heating in the mode slot

    def test_set_attribute_preserves_power_and_auto_function(self) -> None:
        """Test partial set: power off and ImpAuto survive a setpoint change.

        Regression: the set message defaults (power=True, auto off) used to
        overwrite the real device state on every partial set.
        """
        self._seed_state(power=False, auto_set_function=True, mode="auto")
        with patch.object(self.device, "build_send") as mock_build_send:
            self.device.set_attribute("target_temperature", 24)
            body = mock_build_send.call_args[0][0].body
            assert body[1] & 0x01 == 0  # power stays off
            assert body[1] & 0x02  # auto_set_function stays on
            assert body[2] == 0x04  # mode stays auto
            assert body[4] == 24

    def test_set_attribute_refuses_unknown_state(self) -> None:
        """Test set before the first status frame: refuse, don't invent state."""
        with pytest.raises(ValueError, match="state"):
            self.device.set_attribute("target_temperature", 24)


class TestMideaCEDeviceStandard:
    """Standard CE: no literal "None" string as mode."""

    @pytest.fixture(autouse=True)
    def _setup_device(self) -> None:
        self.device = MideaCEDevice(model="test_model", **DEVICE_KWARGS)

    def test_mode_falls_back_to_normal(self) -> None:
        """Test synthesized mode: "Normal" when neither sleep nor eco."""
        with patch("midealocal.devices.ce.MessageCEResponse") as mock_response:
            message = mock_response.return_value
            message.protocol_version = ProtocolVersion.V3
            message.sleep_mode = False
            message.eco_mode = False
            new_status = self.device.process_message(b"")
            assert new_status[DeviceAttributes.mode.value] == "Normal"
