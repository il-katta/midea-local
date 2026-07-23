"""Clivet VMC specific message handling.

Clivet Elfofresh EVO uses a simplified 8-byte protocol instead of
the standard 25-byte CE protocol.
"""

from midealocal.const import DeviceType
from midealocal.message import (
    ListTypes,
    MessageBody,
    MessageRequest,
    MessageResponse,
    MessageType,
)


class ClivetVMCMessageBody(MessageBody):
    """Clivet VMC status message body (subtype 0x01, 8 bytes).

    Byte map confirmed against the official Midea lua parser for model
    171120H4 (T_0000_CE_171120H4_9.lua, binToModel):
    [0] = Message subtype (0x01=status; 0x02=day timers, 0x03=week timers
          exist too, with different layouts — not parsed here)
    [1] = Flags: bit0=power, bit1=auto_set_function_state,
          bit2=silence_function_state
    [2] = Mode (0x01=cooling, 0x02=heating, 0x03=ventilation/fan, 0x04=auto)
    [3] = Silence function level (lua: level_1/level_2)
    [4] = Target temperature / setpoint (°C)
    [5] = Current/ambient temperature (°C), SIGNED: >=128 means negative
    [6] = Error code (e.g. 44 is shown as the C3 filter alarm in the app)
    [7] = Run mode under auto control, same encoding as mode
          (0=idle, 1=cooling, 2=heating, 3=ventilation)
    """

    def __init__(self, body: bytearray) -> None:
        """Initialize Clivet VMC message body."""
        super().__init__(body)

        # Initialize defaults
        self.power = True
        self.auto_set_function = False
        self.mode = "ventilation"
        self.fan_level = "normal"
        self.target_temperature: float | None = None
        self.current_temperature: float | None = None
        self.error_code: int | None = None
        self.run_mode_under_auto_control: int | None = None

        # Parse only if we have enough bytes
        body_len = len(body)
        if body_len < 6:
            return

        # Byte 1: flags (official: power, auto function, silence state)
        self.power = (body[1] & 0x01) > 0
        self.auto_set_function = (body[1] & 0x02) > 0
        silence_state = (body[1] & 0x04) > 0

        # Byte 2: mode
        mode_map = {
            0x01: "cooling",
            0x02: "heating",
            0x03: "ventilation",
            0x04: "auto",
        }
        self.mode = mode_map.get(body[2], "ventilation")

        # Byte 3: silence function level, meaningful with silence_state on
        silence_level = body[3] == 0x01 if body_len >= 4 else False

        if silence_level:
            self.fan_level = "silent"
        elif silence_state:
            self.fan_level = "reduced"
        else:
            self.fan_level = "normal"

        # Byte 4: target temperature (setpoint) in °C
        self.target_temperature = float(body[4])

        # Byte 5: current/ambient temperature in °C, signed per official lua
        raw_temperature = body[5]
        self.current_temperature = float(
            raw_temperature - 256 if raw_temperature >= 128 else raw_temperature,
        )

        # Byte 6: error code (the app renders 44 as "C3", the filter alarm)
        self.error_code = body[6] if body_len >= 7 else None

        # Byte 7: what AUTO is actually doing right now (mode encoding, 0=idle)
        self.run_mode_under_auto_control = body[7] if body_len >= 8 else None

        # For compatibility with climate entities that use "temperature"
        # Use target for modes with setpoint, current for ventilation
        if self.mode in ["cooling", "heating", "auto"]:
            self.temperature = self.target_temperature
        else:
            self.temperature = self.current_temperature

        # Deliberately NO fabricated standard-CE fields (co2, pm25,
        # filter reminders, error_code, ...): the 8-byte frame does not
        # carry them, and hardcoded defaults proved actively harmful —
        # filter_change_reminder was reported False while the real device
        # showed the C3 filter alarm in the app.

        # Selector POSITION as a percentage (for slider-style UIs), not an
        # absolute airflow: the real flow scale is the installer "base"
        # parameter set from the panel, which this frame does not carry.
        fan_speed_map = {
            "silent": 33,    # ~33%
            "reduced": 66,   # ~66%
            "normal": 100,   # 100%
        }
        self.fan_speed = fan_speed_map.get(self.fan_level, 100)


class ClivetVMCMessageSet(MessageRequest):
    """Clivet VMC message set command.

    Used to send control commands to Clivet VMC devices.
    """

    def __init__(self, protocol_version: int) -> None:
        """Initialize Clivet VMC set message."""
        super().__init__(
            device_type=DeviceType.CE,
            protocol_version=protocol_version,
            message_type=MessageType.set,
            body_type=ListTypes.X01,
        )

        # Clivet-specific parameters
        self.power = True  # VMC is always "on"
        self.mode = "ventilation"  # cooling, heating, ventilation, auto
        self.fan_level = "normal"  # normal, reduced, silent
        self.target_temperature = 22  # 16-28°C

    @property
    def _body(self) -> bytearray:
        """Build the 8-byte command body for Clivet VMC.

        Returns:
            8-byte command body
        """
        # Byte 0: Message type (always 0x01)
        byte0 = 0x01

        # Byte 1: Flags (bit 2 = reduced/silent speed)
        # 0x01 = normal, 0x05 = reduced/silent
        byte1 = 0x05 if self.fan_level in ["reduced", "silent"] else 0x01

        # Byte 2: Mode
        mode_map = {
            "cooling": 0x01,
            "heating": 0x02,
            "ventilation": 0x03,
            "auto": 0x04,
        }
        byte2 = mode_map.get(self.mode, 0x03)

        # Byte 3: Silent flag
        # 0x01 if silent, 0x00 otherwise
        byte3 = 0x01 if self.fan_level == "silent" else 0x00

        # Byte 4: Target temperature (setpoint)
        # Clamp to reasonable range
        target = max(16, min(28, int(self.target_temperature)))
        byte4 = target

        # Byte 5: Current temperature (we set to 0x00, device ignores it)
        byte5 = 0x00

        # Byte 6-7: Reserved
        byte6 = 0x00
        byte7 = 0x00

        return bytearray([byte0, byte1, byte2, byte3, byte4, byte5, byte6, byte7])


class MessageClivetVMCResponse(MessageResponse):
    """Clivet VMC message response.

    Parses the short 8-byte body with Clivet semantics instead of the
    standard CE layout (which would misread mode as fan_speed and the
    temperatures as pm25/co2).
    """

    def __init__(self, message: bytes) -> None:
        """Initialize Clivet VMC message response."""
        super().__init__(bytearray(message))
        if len(super().body) > 0:
            self.set_body(ClivetVMCMessageBody(super().body))
        self.set_attr()


def is_clivet_vmc(model: str) -> bool:
    """Check if device is a Clivet VMC.

    Args:
        model: Device model string

    Returns:
        True if this is a Clivet VMC that needs special handling
    """
    # Known Clivet VMC models
    clivet_models = [
        "171120H4",  # Elfofresh EVO
    ]

    return model in clivet_models
