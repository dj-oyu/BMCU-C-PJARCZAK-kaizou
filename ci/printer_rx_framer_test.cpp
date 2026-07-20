#include "printer_rx_framer.h"
#include "crc_bus.h"

#include <stdint.h>

namespace
{
struct Frame
{
    uint8_t data[16];
    uint8_t length;
};

int check(bool condition, int line)
{
    return condition ? 0 : line;
}

PrinterRxFramerResult feed(PrinterRxFramer& framer, const uint8_t* bytes, uint8_t length)
{
    PrinterRxFramerResult last = {PrinterRxFramerEvent::none, 0u, 0u};
    for (uint8_t i = 0u; i < length; ++i)
    {
        const PrinterRxFramerResult current = framer.push(bytes[i]);
        if (current.event != PrinterRxFramerEvent::none &&
            current.event != PrinterRxFramerEvent::frame_started)
            last = current;
    }
    return last;
}

Frame short_frame(uint8_t command)
{
    Frame frame = {{0x3Du, 0xC5u, 8u, 0u, command, 0u, 0u, 0u}, 8u};
    frame.data[3] = bus_crc8(frame.data, 3u);
    return frame;
}

Frame long_frame()
{
    Frame frame = {};
    frame.length = 12u;
    frame.data[0] = 0x3Du;
    frame.data[1] = 0x04u;
    frame.data[4] = 12u;
    frame.data[6] = bus_crc8(frame.data, 6u);
    return frame;
}

Frame ahub_frame()
{
    Frame frame = {};
    frame.length = 16u;
    frame.data[0] = 0x33u;
    frame.data[4] = 1u;
    frame.data[6] = bus_crc8(frame.data, 6u);
    return frame;
}
}

int main()
{
    PrinterRxFramer framer;

    Frame frame = short_frame(0x21u);
    PrinterRxFramerResult value = feed(framer, frame.data, frame.length);
    if (int error = check(value.event == PrinterRxFramerEvent::frame_complete, __LINE__)) return error;
    if (int error = check(value.frame_length == 8u && value.package_type == 0x3Du, __LINE__)) return error;

    frame = short_frame(0x20u);
    value = feed(framer, frame.data, frame.length);
    if (int error = check(value.event == PrinterRxFramerEvent::heartbeat_complete, __LINE__)) return error;

    frame = long_frame();
    value = feed(framer, frame.data, frame.length);
    if (int error = check(value.event == PrinterRxFramerEvent::frame_complete, __LINE__)) return error;
    if (int error = check(value.frame_length == 12u, __LINE__)) return error;

    frame = ahub_frame();
    value = feed(framer, frame.data, frame.length);
    if (int error = check(value.event == PrinterRxFramerEvent::frame_complete, __LINE__)) return error;
    if (int error = check(value.frame_length == 16u && value.package_type == 0x33u, __LINE__)) return error;

    const uint8_t bad_length[] = {0x3Du, 0x80u, 3u};
    value = feed(framer, bad_length, sizeof(bad_length));
    if (int error = check(value.event == PrinterRxFramerEvent::bad_length, __LINE__)) return error;

    frame = short_frame(0x21u);
    frame.data[3] ^= 0x01u;
    value = feed(framer, frame.data, frame.length);
    if (int error = check(value.event == PrinterRxFramerEvent::header_crc_error, __LINE__)) return error;

    const uint8_t noise[] = {0x11u, 0x22u};
    feed(framer, noise, sizeof(noise));
    frame = short_frame(0x21u);
    value = feed(framer, frame.data, frame.length);
    if (int error = check(value.event == PrinterRxFramerEvent::frame_complete, __LINE__)) return error;

    framer.push(0x3Du);
    if (int error = check(framer.active(), __LINE__)) return error;
    framer.reset();
    if (int error = check(!framer.active(), __LINE__)) return error;
    return 0;
}