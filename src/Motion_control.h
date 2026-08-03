#pragma once
#include <stdint.h>
#include <stdbool.h>

void Motion_control_init();
void Motion_control_set_PWM(uint8_t CHx, int PWM);
void Motion_control_run(int error);
enum MotionControlFault : uint8_t
{
    MOTION_FAULT_NONE = 0u,
    MOTION_FAULT_PULL_NO_PROGRESS = 1u,
    MOTION_FAULT_PULL_TRAVEL_BUDGET = 2u,
    MOTION_FAULT_REDETECT_TIMEOUT = 3u,
};

bool Motion_control_save_dm_key_none_thresholds(void);

struct MotionControlChannelTelemetry
{
    uint16_t raw_angle;
    int16_t position_delta;
    int16_t motor_pwm;
    uint8_t sensor_online;
    uint8_t motion_fault;
    uint8_t sensor_good;
    uint8_t controller_motion;
};

bool Motion_control_get_channel_telemetry(uint8_t channel, MotionControlChannelTelemetry* output);
bool Motion_control_is_reset_safe(void);
float Motion_control_get_filament_meters(uint8_t channel);
void MC_PULL_detect_channels_inserted();

// Externy
extern float   MC_PULL_V_OFFSET[4];
extern float   MC_PULL_V_MIN[4];
extern float   MC_PULL_V_MAX[4];
extern uint8_t MC_PULL_pct[4];
extern int8_t  MC_PULL_POLARITY[4];
extern float   MC_DM_KEY_NONE_THRESH[4];
extern bool    filament_channel_inserted[4];

// platformio.ini: -DBAMBU_BUS_AMS_NUM
#ifndef BAMBU_BUS_AMS_NUM
#define BAMBU_BUS_AMS_NUM 0
#endif

// platformio.ini: -DAMS_RETRACT_LEN
#ifndef AMS_RETRACT_LEN
#define AMS_RETRACT_LEN 0.2f
#endif

// platformio.ini: -DBMCU_DM_TWO_MICROSWITCH=1 (DM dual microswitch + autoload assist)
#ifndef BMCU_DM_TWO_MICROSWITCH
#define BMCU_DM_TWO_MICROSWITCH 0
#endif

// platformio.ini: -DBMCU_ONLINE_LED_FILAMENT_RGB=1 (show filament RGB on ONLINE LED when loaded)
#ifndef BMCU_ONLINE_LED_FILAMENT_RGB
#define BMCU_ONLINE_LED_FILAMENT_RGB 0
#endif

#ifndef motion_control_ams_num
#define motion_control_ams_num BAMBU_BUS_AMS_NUM
#endif

#ifndef motion_control_pull_back_distance
#define motion_control_pull_back_distance AMS_RETRACT_LEN
#endif

#if (BAMBU_BUS_AMS_NUM < 0) || (BAMBU_BUS_AMS_NUM > 3)
#error "BAMBU_BUS_AMS_NUM must be in range 0..3"
#endif