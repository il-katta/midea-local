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

    def test_heating_silent_frame(self) -> None:
        """Test synthetic frame: heating with silent fan."""
        body = ClivetVMCMessageBody(BODY_HEATING_SILENT)
        assert body.mode == "heating"
        assert body.fan_level == "silent"


class TestClivetVMCMessageSet:
    """Building of the 8-byte set command."""

    def test_set_heating_body(self) -> None:
        """Test heating command with setpoint."""
        message = ClivetVMCMessageSet(ProtocolVersion.V3)
        message.mode = "heating"
        message.target_temperature = 22
        body = message._body  # noqa: SLF001
        assert body[0] == 0x01
        assert body[2] == 0x02  # heating
        assert body[4] == 22

    def test_set_silent_body(self) -> None:
        """Test command with silent fan."""
        message = ClivetVMCMessageSet(ProtocolVersion.V3)
        message.fan_level = "silent"
        body = message._body  # noqa: SLF001
        assert body[1] == 0x05
        assert body[3] == 0x01

    def test_set_clamps_target_temperature(self) -> None:
        """Test setpoint clamping to the 16-28 range."""
        message = ClivetVMCMessageSet(ProtocolVersion.V3)
        message.target_temperature = 35
        assert message._body[4] == 28  # noqa: SLF001


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
        """Test initial attributes: always on, no fabricated values."""
        assert self.device.attributes[DeviceAttributes.power] is True
        assert self.device.attributes[DeviceAttributes.mode] is None
        assert self.device.attributes[DeviceAttributes.target_temperature] is None
        assert self.device.attributes[DeviceAttributes.fan_level] is None

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

    def test_set_attribute_builds_clivet_set(self) -> None:
        """Test set_attribute: 8-byte Clivet command, not the standard CE set."""
        with patch.object(self.device, "build_send") as mock_build_send:
            self.device.set_attribute("mode", "heating")
            message = mock_build_send.call_args[0][0]
            assert isinstance(message, ClivetVMCMessageSet)
            assert message._body[2] == 0x02  # noqa: SLF001


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
