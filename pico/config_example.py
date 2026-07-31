# Copy this file to config.py on the Pico if defaults need changing.
# Values are Pico GPIO numbers, not BMCU header pin numbers.

UART_ID = 0
UART_TX_PIN = 0
UART_RX_PIN = 1
UART_BAUDRATE = 115200
# Default UART RX ring size in bytes, overridable per link with "rxbuf".
# The MicroPython default of 256 bytes overflows in ~22 ms at 115200 baud,
# and provides margin while bounded TCP/journal/UI work is serviced.
UART_RXBUF = 2048
WEB_PORT = 80
BRIDGE_ID = "pico-bmcu-bridge"
PICO_FIRMWARE_VERSION = "alpha.3"
BMCU_LINKS = (
    # Optional per-link keys: "baudrate", "rxbuf".
    {"id": "bmcu-a", "uart": 0, "tx": 0, "rx": 1},
    {"id": "bmcu-b", "uart": 1, "tx": 4, "rx": 5},
)

# Legacy UART_* values remain the single-link fallback when BMCU_LINKS is empty.

DEBUG_USB = False
# Required BMB1 transport configuration.
BMCU_BINARY_HOST = "192.168.1.10"
BMCU_BINARY_PORT = 8766
BMCU_BINARY_DEVICE_ID = "pico-bmcu-bridge"
BMCU_BINARY_DEVICE_KEY = ""
BMCU_BINARY_JOURNAL_PATH = "bmcu_history"
BMCU_BINARY_QUEUE_SLOTS = 128
BMCU_BINARY_JOURNAL_STAGING_SLOTS = 4
BMCU_UART_DRAIN_BUDGET = 1024
BMCU_UART_DRAIN_CHUNK = 128
