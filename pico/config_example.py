# Copy this file to config.py on the Pico if defaults need changing.
# Values are Pico GPIO numbers, not BMCU header pin numbers.

UART_ID = 0
UART_TX_PIN = 0
UART_RX_PIN = 1
UART_BAUDRATE = 115200
# Default UART RX ring size in bytes, overridable per link with "rxbuf".
# The MicroPython default of 256 bytes overflows in ~22 ms at 115200 baud,
# and provides margin for two links while bounded UDP/UI work is serviced.
UART_RXBUF = 4096
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
# Stream destination (docs/BMCU_MONITOR_STREAM_V2.md): records go over UDP,
# unacknowledged and unauthenticated, to this host and port. Both can be
# replaced later in the local UI; persisted UI values take precedence.
BMCU_BINARY_HOST = "192.168.1.10"
BMCU_BINARY_PORT = 8799
BMCU_BINARY_DEVICE_ID = "pico-bmcu-bridge"
# RAM queue that bridges a Wi-Fi outage. There is no flash journal any more.
BMCU_BINARY_QUEUE_SLOTS = 128
# One HEARTBEAT (identity + liveness) every this many ms.
BMCU_STREAM_HEARTBEAT_MS = 5000
# Datagrams sent per main-loop pass, so the UART drain keeps its share.
BMCU_STREAM_DATAGRAMS_PER_LOOP = 4
BMCU_UART_DRAIN_BUDGET = 4096
BMCU_UART_DRAIN_CHUNK = 512

# SSD1306 status panel. GPIO6/7 are I2C1 SDA/SCL and are free in the default
# link map above, which uses GPIO0/1 and GPIO4/5 for the two BMCU UARTs. Power
# the module from 3V3(OUT) on physical pin 36.
OLED_ENABLED = True
OLED_I2C_ID = 1
OLED_SDA_PIN = 6
OLED_SCL_PIN = 7
OLED_I2C_FREQ = 400000
OLED_ADDRESS = 0x3C
OLED_WIDTH = 128
OLED_HEIGHT = 64
# Kept below full drive because the header and body rows are a static image for
# hours: 0xFF on an always-on panel is how OLEDs burn in.
OLED_CONTRAST = 0x7F
# Pages rotate overview -> flow -> one detail page per answering link ->
# network/health. Every key below is optional; the values shown are the
# defaults.
#   OLED_OVERVIEW_MS    overview (both BMCUs side by side, one word per channel)
#   OLED_FLOW_MS        animated flow page (spools, buffer, printer head);
#                       0 removes it from the rotation
#   OLED_FLOW_FRAME_MS  one animation frame per this many ms. Each frame
#                       rewrites the six body pages over I2C, so raise it if the
#                       loop time on the network page grows
#   OLED_PAGE_MS        each detail page and the network page
#   OLED_REFRESH_MS     how often a static page may redraw (only on change)
OLED_OVERVIEW_MS = 10000
OLED_FLOW_MS = 10000
OLED_FLOW_FRAME_MS = 200
OLED_PAGE_MS = 5000
OLED_REFRESH_MS = 1000
# Marquee speed: pixels per step, one step per interval. 4 px / 120 ms reads at
# about four characters a second.
OLED_MARQUEE_MS = 120
OLED_MARQUEE_STEP_PX = 4
