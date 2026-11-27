#include "crc_bus.h"

// Nibble tables, two lookups per byte: the same CRC-8 (poly 0x39) and CRC-16
// (poly 0x1021) as the 256-entry tables they replace, in 48 bytes instead of
// 768. Each entry is the byte table's entry for that index, whose top four
// bits are zero, so the first four shifts of the byte step carry no feedback.

static const uint8_t g_crc8_nib[16] = { 0x00u, 0x39u, 0x72u, 0x4Bu, 0xE4u, 0xDDu, 0x96u, 0xAFu, 0xF1u, 0xC8u, 0x83u, 0xBAu, 0x15u, 0x2Cu, 0x67u, 0x5Eu };
static const uint16_t g_crc16_nib[16] = { 0x0000u, 0x1021u, 0x2042u, 0x3063u, 0x4084u, 0x50A5u, 0x60C6u, 0x70E7u, 0x8108u, 0x9129u, 0xA14Au, 0xB16Bu, 0xC18Cu, 0xD1ADu, 0xE1CEu, 0xF1EFu };

void bus_crc_init(void)
{
}

uint8_t bus_crc8(const uint8_t* data, uint32_t len)
{
    uint8_t r = 0x66u;
    for (uint32_t i = 0; i < len; i++)
    {
        r ^= data[i];
        r = (uint8_t)((r << 4) ^ g_crc8_nib[r >> 4]);
        r = (uint8_t)((r << 4) ^ g_crc8_nib[r >> 4]);
    }
    return r;
}

uint16_t bus_crc16(const uint8_t* data, uint32_t len)
{
    uint16_t r = 0x913Du;
    for (uint32_t i = 0; i < len; i++)
    {
        r ^= (uint16_t)((uint16_t)data[i] << 8);
        r = (uint16_t)((r << 4) ^ g_crc16_nib[r >> 12]);
        r = (uint16_t)((r << 4) ^ g_crc16_nib[r >> 12]);
    }
    return r;
}
