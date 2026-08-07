"""Minimal SSD1306 I2C driver with per-page flushing.

Vendored rather than installed with mip: the bridge is deployed by copying
pico/*.py onto the device, so a driver that only exists after a network install
would leave a Pico that boots into an ImportError with no display to say so.

Only whole pages are written. A full 128x64 frame is 1024 bytes, about 23 ms on
a 400 kHz bus with interrupts free but the loop blocked -- the same order as the
journal commit the main loop already schedules apart. One page is 128 bytes, so
the caller can spread a frame over eight loop iterations instead of stalling the
UART drain in a single one.
"""

try:
    import framebuf
except ImportError:  # CPython, where only the ticker's render helpers are used
    framebuf = None

SET_MEMORY_ADDRESSING = 0x20
SET_COLUMN_ADDRESS = 0x21
SET_PAGE_ADDRESS = 0x22
SET_START_LINE = 0x40
SET_CONTRAST = 0x81
SET_CHARGE_PUMP = 0x8D
SET_SEGMENT_REMAP = 0xA1
SET_ENTIRE_ON = 0xA4
SET_NORMAL_DISPLAY = 0xA6
SET_MULTIPLEX = 0xA8
SET_DISPLAY_OFF = 0xAE
SET_DISPLAY_ON = 0xAF
SET_COM_SCAN_DEC = 0xC8
SET_DISPLAY_OFFSET = 0xD3
SET_DISPLAY_CLOCK = 0xD5
SET_PRECHARGE = 0xD9
SET_COM_PIN_CONFIG = 0xDA
SET_VCOM_DESELECT = 0xDB

# The header and the body rows are a static image for hours at a time, so the
# panel runs well below full drive: 0xFF on a permanently-on status display is
# how OLEDs get burned in.
DEFAULT_CONTRAST = 0x7F


class SSD1306_I2C:
    def __init__(self, i2c, width=128, height=64, address=0x3C,
                 external_vcc=False, contrast=DEFAULT_CONTRAST):
        if framebuf is None:
            raise RuntimeError("framebuf is unavailable on this port")
        self.i2c = i2c
        self.width = width
        self.height = height
        self.address = address
        self.external_vcc = external_vcc
        self.pages = height // 8
        self.buffer = bytearray(self.pages * width)
        self.frame = framebuf.FrameBuffer(
            self.buffer, width, height, framebuf.MONO_VLSB)
        # Everything a flush touches is built once here. MicroPython has no
        # reference counting, so a slice or a memoryview built per flush would
        # sit in the heap until the next collection -- and this runs about
        # fifteen times a second for the whole uptime.
        self._command = bytearray([0x80, 0x00])
        view = memoryview(self.buffer)
        prefix = b"\x40"
        self._vectors = tuple(
            (prefix, view[page * width:(page + 1) * width])
            for page in range(self.pages))
        # writevto gathers the prefix and the page without copying. Ports
        # without it fall back to a staging buffer, which is one slice
        # assignment per flush rather than a fresh 129-byte object.
        self._chunk = None if hasattr(i2c, "writevto") \
            else bytearray(width + 1)
        if self._chunk is not None:
            self._chunk[0] = 0x40
        self.init_display(contrast)

    def write_command(self, command):
        self._command[1] = command
        self.i2c.writeto(self.address, self._command)

    def init_display(self, contrast=DEFAULT_CONTRAST):
        for command in (
            SET_DISPLAY_OFF,
            SET_MEMORY_ADDRESSING, 0x00,  # horizontal addressing
            SET_START_LINE | 0x00,
            SET_SEGMENT_REMAP,
            SET_MULTIPLEX, self.height - 1,
            SET_COM_SCAN_DEC,
            SET_DISPLAY_OFFSET, 0x00,
            # 0x12 for a 64-row panel, 0x02 for the 32-row one: getting this
            # wrong shows every other row, which reads as a broken panel.
            SET_COM_PIN_CONFIG, 0x02 if self.height == 32 else 0x12,
            SET_DISPLAY_CLOCK, 0x80,
            SET_PRECHARGE, 0x22 if self.external_vcc else 0xF1,
            SET_VCOM_DESELECT, 0x30,
            SET_CONTRAST, contrast & 0xFF,
            SET_ENTIRE_ON,
            SET_NORMAL_DISPLAY,
            SET_CHARGE_PUMP, 0x10 if self.external_vcc else 0x14,
            SET_DISPLAY_ON,
        ):
            self.write_command(command)
        self.frame.fill(0)
        self.show()

    def contrast(self, value):
        self.write_command(SET_CONTRAST)
        self.write_command(value & 0xFF)

    def power(self, on):
        self.write_command(SET_DISPLAY_ON if on else SET_DISPLAY_OFF)

    def show_page(self, page):
        """Push one 8-pixel-tall band of the framebuffer."""
        if page < 0 or page >= self.pages:
            return
        self.write_command(SET_COLUMN_ADDRESS)
        self.write_command(0)
        self.write_command(self.width - 1)
        self.write_command(SET_PAGE_ADDRESS)
        self.write_command(page)
        self.write_command(page)
        vector = self._vectors[page]
        if self._chunk is None:
            self.i2c.writevto(self.address, vector)
        else:
            self._chunk[1:] = vector[1]
            self.i2c.writeto(self.address, self._chunk)

    def show(self):
        for page in range(self.pages):
            self.show_page(page)
