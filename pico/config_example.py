# Copy this file to config.py on the Pico if defaults need changing.
# Values are Pico GPIO numbers, not BMCU header pin numbers.

UART_ID = 0
UART_TX_PIN = 0
UART_RX_PIN = 1
UART_BAUDRATE = 115200
WEB_PORT = 80
BRIDGE_ID = "pico-bmcu-bridge"
PICO_FIRMWARE_VERSION = "alpha.3"
BMCU_LINKS = (
    {"id": "bmcu-a", "uart": 0, "tx": 0, "rx": 1},
    {"id": "bmcu-b", "uart": 1, "tx": 4, "rx": 5},
)

# Legacy UART_* values remain the single-link fallback when BMCU_LINKS is empty.

DEBUG_USB = False
