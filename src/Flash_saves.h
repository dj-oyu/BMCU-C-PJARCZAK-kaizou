#pragma once
#include <stdint.h>

#ifndef BAMBU_BUS_AMS_NUM
#define BAMBU_BUS_AMS_NUM 0
#endif

// === NVM w OSTATNIM sektorze 4KB (CH32V203C8: flash kończy się na 0x08010000) ===
#define FLASH_NVM_BASE_ADDR   ((uint32_t)0x0800F000)   // 4KB sector

#define FLASH_NVM_CAL_ADDR    (FLASH_NVM_BASE_ADDR + 0x000) // 1x256B
#define FLASH_NVM_MOTION_ADDR (FLASH_NVM_BASE_ADDR + 0x100) // 1x256B
#define FLASH_NVM_AMS_ADDR    (FLASH_NVM_BASE_ADDR + 0x200) // 4x256B (0..3) => do +0x5FF

#define FLASH_NVM256_PAGE_SIZE (256u)
#define FLASH_NVM_TOTAL_SIZE    (4096u)
#define FLASH_NVM_PAGE_COUNT    (FLASH_NVM_TOTAL_SIZE / FLASH_NVM256_PAGE_SIZE)
#define NVM256_CRC_OFF         (252u)

// Magici / wersje
static constexpr uint32_t MAGIC_FIL = 0x314C4946u; // 'FIL1'
static constexpr uint32_t MAGIC_CAL = 0x324C4143u; // 'CAL2'
static constexpr uint32_t MAGIC_MOT = 0x31544F4Du; // 'MOT1'
static constexpr uint32_t MAGIC_STA = 0x31415453u; // 'STA1'
static constexpr uint16_t VER_1     = 0x0001u;

struct __attribute__((packed, aligned(4))) NVM256_HDR
{
    uint32_t magic;
    uint16_t ver;
    uint16_t len;
    uint32_t rsv;
};

struct __attribute__((packed, aligned(4))) Flash_FilamentInfo
{
    uint8_t  bambubus_filament_id[8];
    uint8_t  color_R;
    uint8_t  color_G;
    uint8_t  color_B;
    uint8_t  color_A;
    uint16_t temperature_min;
    uint16_t temperature_max;
    char     name[16];
};

void Flash_saves_init(void);

// AMS: 4x 256B
bool Flash_AMS_filament_read(uint8_t filament_idx, Flash_FilamentInfo* out);
bool Flash_AMS_filament_write(uint8_t filament_idx, const Flash_FilamentInfo* info);
bool Flash_AMS_filament_clear(uint8_t filament_idx);

// The merger mutex. `loaded_channel` keeps exactly the meaning it always had --
// 0..3 or 0xFF -- so a downgraded firmware reading this record gets the value it
// would have written itself. `tail` is carried by a separate record tag that
// older firmware skips; see STA_TAIL_TAG in Flash_saves.cpp.
//
// `downstream_mask` (optional) receives the newest packed strand-zone byte,
// 0 when there is none; see src/strand_zone_policy.h. It shares the ring
// with the merger records under its own tag, STA_DOWN_TAG in Flash_saves.cpp.
bool Flash_AMS_state_read(uint8_t* loaded_channel, bool* tail,
                          uint8_t* downstream_mask = nullptr);
bool Flash_AMS_state_write(uint8_t loaded_channel, bool tail);
bool Flash_AMS_downstream_write(uint8_t mask);
// After Flash_AMS_state_read: whether a zone record was found. False on the
// first boot of a firmware that keeps one, or when a page erase took it.
bool Flash_AMS_downstream_valid(void);
// True once after a write had to erase a page of the shared ring, which may
// have taken the other record kind's newest copy: the caller writes both again.
bool Flash_AMS_state_take_page_erased(void);

// CAL: 1x 256B
bool Flash_MC_PULL_cal_read(float offs[4], float vmin[4], float vmax[4], int8_t pol[4]);
bool Flash_MC_PULL_cal_write_all(const float offs[4], const float vmin[4], const float vmax[4], const int8_t pol[4]);
bool Flash_MC_PULL_cal_clear(void);
bool Flash_NVM_full_clear(void);

// MOT: 1x 256B
bool Flash_Motion_read(void* out, uint16_t bytes);
bool Flash_Motion_write(const void* in, uint16_t bytes);
bool Flash_Motion_clear(void);