"""Test CE Device (standard e Clivet VMC)."""

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

# Catture reali dal Clivet Elfofresh EVO 171120H4 (body a 8 byte).
# 2025-10: ventilazione, setpoint 17, ambiente 27 (docs/issues/02)
BODY_VENTILATION = bytearray([0x01, 0x01, 0x03, 0x00, 0x11, 0x1B, 0x2C, 0x00])
# 2026-07-22: cooling, setpoint 24, ambiente 27 (lettura live cli.py state)
BODY_COOLING = bytearray([0x01, 0x01, 0x01, 0x00, 0x18, 0x1B, 0x2C, 0x00])
# Sintetico dalla mappa verificata 2025-10-26: heating, silenzioso
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
    """Issue 01: nome attributo aux_heating."""

    def test_aux_heating_attribute_name_has_no_apostrophe(self) -> None:
        """Test aux_heating enum value."""
        assert DeviceAttributes.aux_heating.value == "aux_heating"


class TestClivetVMCMessageBody:
    """Decodifica del body corto Clivet (fixture: catture reali)."""

    def test_ventilation_frame(self) -> None:
        """Test cattura 2025-10: ventilazione, normale, setpoint 17, ambiente 27."""
        body = ClivetVMCMessageBody(BODY_VENTILATION)
        assert body.power is True
        assert body.mode == "ventilation"
        assert body.fan_level == "normal"
        assert body.target_temperature == 17.0
        assert body.current_temperature == 27.0

    def test_cooling_frame(self) -> None:
        """Test lettura live 2026-07-22: cooling, setpoint 24, ambiente 27."""
        body = ClivetVMCMessageBody(BODY_COOLING)
        assert body.mode == "cooling"
        assert body.target_temperature == 24.0
        assert body.current_temperature == 27.0

    def test_heating_silent_frame(self) -> None:
        """Test frame sintetico: heating con fan silenzioso."""
        body = ClivetVMCMessageBody(BODY_HEATING_SILENT)
        assert body.mode == "heating"
        assert body.fan_level == "silent"


class TestClivetVMCMessageSet:
    """Costruzione del comando set a 8 byte."""

    def test_set_heating_body(self) -> None:
        """Test comando heating con setpoint."""
        message = ClivetVMCMessageSet(ProtocolVersion.V3)
        message.mode = "heating"
        message.target_temperature = 22
        body = message._body  # noqa: SLF001
        assert body[0] == 0x01
        assert body[2] == 0x02  # heating
        assert body[4] == 22

    def test_set_silent_body(self) -> None:
        """Test comando con fan silenzioso."""
        message = ClivetVMCMessageSet(ProtocolVersion.V3)
        message.fan_level = "silent"
        body = message._body  # noqa: SLF001
        assert body[1] == 0x05
        assert body[3] == 0x01

    def test_set_clamps_target_temperature(self) -> None:
        """Test clamp del setpoint nel range 16-28."""
        message = ClivetVMCMessageSet(ProtocolVersion.V3)
        message.target_temperature = 35
        assert message._body[4] == 28  # noqa: SLF001


class TestMideaApplianceDispatch:
    """Selezione del device in base al modello."""

    def test_dispatches_clivet_for_known_model(self) -> None:
        """Test modello Clivet -> ClivetVMCDevice."""
        device = MideaAppliance(model="171120H4", **DEVICE_KWARGS)
        assert isinstance(device, ClivetVMCDevice)

    def test_standard_ce_for_other_models(self) -> None:
        """Test altri modelli -> CE standard."""
        device = MideaAppliance(model="test_model", **DEVICE_KWARGS)
        assert isinstance(device, MideaCEDevice)
        assert not isinstance(device, ClivetVMCDevice)


class TestClivetVMCDevice:
    """Comportamento del device Clivet."""

    @pytest.fixture(autouse=True)
    def _setup_device(self) -> None:
        self.device = ClivetVMCDevice(model="171120H4", **DEVICE_KWARGS)

    def test_initial_attributes(self) -> None:
        """Test attributi iniziali: sempre acceso, niente valori inventati."""
        assert self.device.attributes[DeviceAttributes.power] is True
        assert self.device.attributes[DeviceAttributes.mode] is None
        assert self.device.attributes[DeviceAttributes.target_temperature] is None
        assert self.device.attributes[DeviceAttributes.fan_level] is None

    def test_modes(self) -> None:
        """Test modalità Clivet."""
        assert self.device.preset_modes == [
            "cooling",
            "heating",
            "ventilation",
            "auto",
        ]

    def test_process_message_updates_clivet_attributes(self) -> None:
        """Test process_message: attributi Clivet aggiornati, senza sintesi sleep/eco."""
        with patch(
            "midealocal.devices.ce.MessageClivetVMCResponse",
        ) as mock_response:
            message = mock_response.return_value
            message.protocol_version = ProtocolVersion.V3
            message.mode = "heating"
            message.fan_level = "reduced"
            message.target_temperature = 21.0
            message.current_temperature = 19.0
            message.sleep_mode = True  # non deve trasformare mode in "Sleep mode"
            new_status = self.device.process_message(b"")
            assert new_status[DeviceAttributes.mode.value] == "heating"
            assert new_status[DeviceAttributes.fan_level.value] == "reduced"
            assert new_status[DeviceAttributes.target_temperature.value] == 21.0
            assert new_status[DeviceAttributes.current_temperature.value] == 19.0

    def test_set_attribute_builds_clivet_set(self) -> None:
        """Test set_attribute: comando Clivet a 8 byte, non il set CE standard."""
        with patch.object(self.device, "build_send") as mock_build_send:
            self.device.set_attribute("mode", "heating")
            message = mock_build_send.call_args[0][0]
            assert isinstance(message, ClivetVMCMessageSet)
            assert message._body[2] == 0x02  # noqa: SLF001


class TestMideaCEDeviceStandard:
    """Issue 03 (parte standard): niente stringa 'None' come mode."""

    @pytest.fixture(autouse=True)
    def _setup_device(self) -> None:
        self.device = MideaCEDevice(model="test_model", **DEVICE_KWARGS)

    def test_mode_falls_back_to_normal(self) -> None:
        """Test mode sintetizzato: 'Normal' quando non sleep/eco."""
        with patch("midealocal.devices.ce.MessageCEResponse") as mock_response:
            message = mock_response.return_value
            message.protocol_version = ProtocolVersion.V3
            message.sleep_mode = False
            message.eco_mode = False
            new_status = self.device.process_message(b"")
            assert new_status[DeviceAttributes.mode.value] == "Normal"
