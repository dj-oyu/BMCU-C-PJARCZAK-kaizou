#pragma once
#include <stdint.h>

enum class PrinterRxFramerEvent : uint8_t
{
    none = 0,
    frame_started,
    frame_complete,
    heartbeat_complete,
    bad_length,
    header_crc_error,
};

struct PrinterRxFramerResult
{
    PrinterRxFramerEvent event;
    uint16_t frame_length;
    uint8_t package_type;
};

class PrinterRxFramer
{
public:
    PrinterRxFramer();
    void reset();
    PrinterRxFramerResult push(uint8_t value);
    bool active() const { return active_; }

private:
    uint16_t index_;
    uint16_t expected_length_;
    uint8_t data_length_index_;
    uint8_t crc8_index_;
    uint8_t package_type_;
    uint8_t header_[7];
    bool active_;
};
