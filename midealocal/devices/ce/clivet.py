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
    """Clivet VMC message body (8 bytes).

    Based on reverse engineering of Clivet Elfofresh EVO model 171120H4.

    Byte structure (VERIFIED 2025-10-26):
    [0] = Message type (always 0x01)
    [1] = Flags (bit 2 = reduced/silent speed)
    [2] = Mode (0x01=cooling, 0x02=heating, 0x03=ventilation, 0x04=auto)
    [3] = Silent flag (0x01=silent, 0x00=normal/reduced)
    [4] = Target temperature / Setpoint (°C) - can be set by user
    [5] = Current/ambient temperature (°C) - sensor reading
    [6] = Reserved (0x00)
    [7] = AUTO substate? (0x00=normal, 0x02=heating in auto mode)

    Key findings:
    - byte[4] changes when user adjusts setpoint (21→22→23→24°C)
    - byte[5] stays constant (ambient temp from sensor, e.g., 21°C)
    - In ventilation mode, byte[4] may be ignored or fixed
    """

    def __init__(self, body: bytearray) -> None:
        """Initialize Clivet VMC message body."""
        super().__init__(body)

        # Initialize defaults
        self.power = True  # VMC is always "on" when communicating
        self.mode = "ventilation"
        self.fan_level = "normal"
        self.target_temperature: float | None = None
        self.current_temperature: float | None = None

        # Parse only if we have enough bytes
        body_len = len(body)
        if body_len < 6:
            return

        # Byte 1: Flags
        # Bit 2 = reduced/silent speed (0=normal, 1=reduced or silent)
        reduced_or_silent = (body[1] & 0x04) > 0

        # Byte 2: Mode
        mode_map = {
            0x01: "cooling",       # Raffrescamento
            0x02: "heating",       # Riscaldamento
            0x03: "ventilation",   # Solo Ventilazione
            0x04: "auto",          # Auto (VERIFIED 2025-10-26)
        }
        self.mode = mode_map.get(body[2], "ventilation")

        # Byte 3: Silent flag
        silent_flag = body[3] == 0x01 if body_len >= 4 else False

        # Determine fan level
        if silent_flag:
            self.fan_level = "silent"      # Silenzioso
        elif reduced_or_silent:
            self.fan_level = "reduced"     # Ridotto
        else:
            self.fan_level = "normal"      # Normale

        # Byte 4: TARGET temperature (setpoint) in °C
        # This is the temperature the user sets
        # Can be adjusted in cooling/heating/auto modes
        self.target_temperature = float(body[4])

        # Byte 5: CURRENT/AMBIENT temperature (sensor reading) in °C
        # This is the actual room temperature
        self.current_temperature = float(body[5])

        # Byte 7: AUTO substate (only relevant in AUTO mode)
        # In AUTO mode, byte[7] may indicate what action AUTO is taking:
        # 0x00 = idle/ventilation?, 0x02 = heating active?
        # Needs more testing to confirm
        self.auto_substate = body[7] if body_len >= 8 else 0

        # For compatibility with climate entities that use "temperature"
        # Use target for modes with setpoint, current for ventilation
        if self.mode in ["cooling", "heating", "auto"]:
            self.temperature = self.target_temperature
        else:
            self.temperature = self.current_temperature

        # Legacy attributes (for compatibility with standard CE)
        # These don't actually exist on Clivet VMC
        self.pm25 = 0
        self.co2 = 0
        self.current_humidity: float | None = None
        self.hcho: float | None = None
        self.child_lock = False
        self.scheduled = False
        self.aux_heating: bool | None = None
        self.link_to_ac = False
        self.sleep_mode = False
        self.eco_mode = False
        self.powerful_purify = False
        self.filter_cleaning_reminder = False
        self.filter_change_reminder = False
        self.error_code = 0

        # Map fan_level to numeric speed (0-100) for compatibility
        # This is an approximation since Clivet has only 3 discrete levels
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
