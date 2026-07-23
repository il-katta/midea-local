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

# Protocol constants from the official lua parser (T_0000_CE_171120H4_9.lua)
SUBTYPE_STATUS = 0x01
SUBTYPE_DAY_TIMERS = 0x02
SUBTYPE_WEEK_TIMERS = 0x03
STATUS_BODY_LEN = 8

POWER_BIT = 0x01
AUTO_FUNCTION_BIT = 0x02
SILENCE_STATE_BIT = 0x04
SILENCE_LEVEL_2 = 0x01

SIGNED_TEMPERATURE_THRESHOLD = 128

MODE_NAMES = {0x01: "cooling", 0x02: "heating", 0x03: "ventilation", 0x04: "auto"}
MODE_VALUES = {name: value for value, name in MODE_NAMES.items()}
FAN_LEVELS = ("normal", "reduced", "silent")

# Range of the device's own setpoint UI (not enforced by the lua; sending
# values outside it is untested territory, so we refuse rather than clamp)
TARGET_TEMP_MIN = 16
TARGET_TEMP_MAX = 28


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

        # Only complete status frames contribute attributes: truncated frames
        # or timer subtypes would otherwise be misread as status (and absent
        # attributes mean "no update" for the device, not invented defaults).
        if len(body) != STATUS_BODY_LEN or body[0] != SUBTYPE_STATUS:
            return

        # Byte 1: flags (official: power, auto function, silence state)
        self.power = (body[1] & POWER_BIT) > 0
        self.auto_set_function = (body[1] & AUTO_FUNCTION_BIT) > 0
        self.silence_function_state = (body[1] & SILENCE_STATE_BIT) > 0

        # Byte 2: mode — unknown values stay None (a silent "ventilation"
        # default is how the shifted-frame bug went unnoticed for months)
        self.mode_raw = body[2]
        self.mode = MODE_NAMES.get(body[2])

        # Byte 3: silence function level
        self.silence_function_level = body[3] == SILENCE_LEVEL_2

        # Derived convenience view of the two silence fields
        if self.silence_function_state and self.silence_function_level:
            self.fan_level = "silent"
        elif self.silence_function_state:
            self.fan_level = "reduced"
        else:
            self.fan_level = "normal"

        # Byte 4: target temperature (setpoint) in °C
        self.target_temperature = float(body[4])

        # Byte 5: current/ambient temperature in °C, signed per official lua
        raw_temperature = body[5]
        self.current_temperature = float(
            raw_temperature - 256
            if raw_temperature >= SIGNED_TEMPERATURE_THRESHOLD
            else raw_temperature,
        )

        # Byte 6: error code (the app renders 44 as "C3", the filter alarm)
        self.error_code = body[6]

        # Byte 7: what AUTO is actually doing right now (mode encoding, 0=idle)
        self.run_mode_under_auto_control = body[7]

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
    """Clivet VMC control command.

    Official wire body (lua jsonToData, controltype 0x01) is 5 bytes:
    [subtype 0x01, flags, mode, silence_level, setpoint]. The subtype is
    prepended by MessageRequest.body from body_type, so _body carries only
    the 4 payload bytes — an earlier version duplicated the subtype and
    padded with zeros, shifting every field by one on the device (the
    flags value landed in the mode slot).
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
        self.power = True
        self.auto_set_function = False
        self.mode = "ventilation"  # cooling, heating, ventilation, auto
        self.fan_level = "normal"  # normal, reduced, silent
        self.target_temperature = 22  # 16-28°C

    @property
    def _body(self) -> bytearray:
        """Build the 4-byte control payload (subtype added by MessageRequest).

        Invalid values raise instead of being silently coerced: a wrong
        command reaching the device is worse than an exception.
        """
        if self.mode not in MODE_VALUES:
            msg = f"unknown mode {self.mode!r}, expected one of {sorted(MODE_VALUES)}"
            raise ValueError(msg)
        if self.fan_level not in FAN_LEVELS:
            msg = f"unknown fan_level {self.fan_level!r}, expected one of {FAN_LEVELS}"
            raise ValueError(msg)
        target = int(self.target_temperature)
        if not TARGET_TEMP_MIN <= target <= TARGET_TEMP_MAX:
            msg = (
                f"target_temperature {target} outside the device range "
                f"{TARGET_TEMP_MIN}-{TARGET_TEMP_MAX}"
            )
            raise ValueError(msg)

        flags = POWER_BIT if self.power else 0x00
        if self.auto_set_function:
            flags |= AUTO_FUNCTION_BIT
        if self.fan_level in ("reduced", "silent"):
            flags |= SILENCE_STATE_BIT
        silence_level = SILENCE_LEVEL_2 if self.fan_level == "silent" else 0x00

        return bytearray([flags, MODE_VALUES[self.mode], silence_level, target])


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
