#include "printer_rx_framer.h"
#include "crc_bus.h"

namespace
{
constexpr uint8_t kBambuSync = 0x3Du;
constexpr uint8_t kAHubSync = 0x33u;
constexpr uint16_t kMaxFrameLength = 1280u;

PrinterRxFramerResult result(PrinterRxFramerEvent event, uint16_t length = 0u,
                             uint8_t package_type = 0u)
{
    PrinterRxFramerResult value = {event, length, package_type};
    return value;
}
}

PrinterRxFramer::PrinterRxFramer()
{
    reset();
}

void PrinterRxFramer::reset()
{
    index_ = 0u;
    expected_length_ = 999u;
    data_length_index_ = 0u;
    crc8_index_ = 0u;
    package_type_ = 0u;
    active_ = false;
}

PrinterRxFramerResult PrinterRxFramer::push(uint8_t value)
{
    if (!active_)
    {
        if (value != kBambuSync && value != kAHubSync)
            return result(PrinterRxFramerEvent::none);

        active_ = true;
        index_ = 1u;
        expected_length_ = 999u;
        data_length_index_ = 4u;
        crc8_index_ = 6u;
        package_type_ = value;
        header_[0] = value;
        return result(PrinterRxFramerEvent::frame_started, 0u, package_type_);
    }

    const uint16_t index = index_;
    if (index < sizeof(header_)) header_[index] = value;

    if (index == 1u)
    {
        if (value & 0x80u)
        {
            data_length_index_ = 2u;
            crc8_index_ = 3u;
        }
        else
        {
            crc8_index_ = 6u;
            data_length_index_ = package_type_ == kBambuSync ? 5u : 4u;
        }
    }

    if (index == data_length_index_)
    {
        if (package_type_ == kBambuSync)
            expected_length_ = data_length_index_ == 2u
                ? value : (uint16_t)(header_[4] | ((uint16_t)value << 8));
        else
            expected_length_ = (uint16_t)(((uint16_t)value << 2) + 12u);

        if (expected_length_ <= crc8_index_ || expected_length_ > kMaxFrameLength)
        {
            const uint8_t package_type = package_type_;
            reset();
            return result(PrinterRxFramerEvent::bad_length, 0u, package_type);
        }
    }

    if (index == crc8_index_ && value != bus_crc8(header_, crc8_index_))
    {
        const uint8_t package_type = package_type_;
        reset();
        return result(PrinterRxFramerEvent::header_crc_error, 0u, package_type);
    }

    index_ = (uint16_t)(index + 1u);
    if (index_ < expected_length_) return result(PrinterRxFramerEvent::none);

    const uint16_t length = expected_length_;
    const uint8_t package_type = package_type_;
    const bool heartbeat = package_type == kBambuSync && length >= 6u &&
                           header_[1] == 0xC5u && header_[4] == 0x20u;
    reset();
    return result(heartbeat ? PrinterRxFramerEvent::heartbeat_complete
                            : PrinterRxFramerEvent::frame_complete,
                  length, package_type);
}
