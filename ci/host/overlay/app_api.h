#pragma once
#include <stdint.h>

// Host stand-in for src/app_api.h.
//
// The real header is not itself a hardware dependency -- every declaration in
// it is plain C++. It is unbuildable on a host for a reason no symbol grep
// finds: it includes ws2812.h for `extern WS2812_class RGBOUT[4]`, and
// WS2812_class has a `GPIO_TypeDef*` member, so ws2812.h includes
// ch32v20x.h. The whole vendor register header is dragged in to satisfy a
// pointer type, and it arrives through a file whose own contents look
// portable.
//
// bambu_bus_ams.cpp uses none of the RGB entry points, so this stand-in drops
// them rather than faking them. What it keeps is the merger API -- the four
// edges and two queries -- with the declarations byte-identical to the real
// header. That matters: these are the seams the routing tests assert on, and
// if a signature here drifted from the shipped one the tests would be
// exercising a different interface than the firmware calls. The commentary
// that justifies each edge lives in the real src/app_api.h and in
// src/ams_merger_policy.h and is not duplicated here.
//
// The implementations behind these live in main.cpp on target and in
// ci/host/bambu_bus_host.cpp on the host -- see the note there about why the
// funnel is reimplemented over the real policy rather than mocked.

void ams_datas_set_need_to_save_filament(uint8_t filament_idx);

void ams_state_set_loaded(uint8_t filament_ch);
void ams_state_set_unloaded(uint8_t filament_ch);
void ams_state_preempt(uint8_t claiming_ch);
void ams_state_set_tail(void);
uint8_t ams_state_get_loaded(void);
bool ams_state_is_tail(void);
