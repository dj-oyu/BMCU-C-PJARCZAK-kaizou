#pragma once
#include <stdint.h>
#include "ws2812.h"

extern WS2812_class RGBOUT[4];

static inline __attribute__((always_inline))
void MC_STU_RGB_set(uint8_t ch, uint8_t r, uint8_t g, uint8_t b)
{
    if (ch < 4) RGBOUT[ch].set_RGB(r, g, b, 0);
}

static inline __attribute__((always_inline))
void MC_PULL_ONLINE_RGB_set(uint8_t ch, uint8_t r, uint8_t g, uint8_t b, bool filament = false)
{
    if (ch < 4) RGBOUT[ch].set_RGB_online(r, g, b, 1, filament);
}

void ams_datas_set_need_to_save_filament(uint8_t filament_idx);

// The merger mutex. See src/ams_merger_policy.h for the state machine; these
// are the three edges the rest of the firmware drives it through.
//
// ams_state_get_loaded names the owning channel in LOADED and in TAIL alike,
// because both are ownership. Callers asking "may this channel still be moved"
// -- the allow_any and allow_stop gates in bambu_bus_ams.cpp -- want exactly
// that and need no further test. Only a caller that specifically wants to know
// whether the strand can still be seen at the online key should ask
// ams_state_is_tail as well.
void ams_state_set_loaded(uint8_t filament_ch);

// A printer-commanded retract: before_pull_back, or the read_num 0xFF unload.
// The printer is pulling this filament out, so this takes TAIL -- it is the
// state's main exit. `filament_ch >= 4` names nobody and has no caller.
void ams_state_set_unloaded(uint8_t filament_ch);

// The printer's session went quiet: the 0xFF/statu-0x01 idle frame and the full
// idle reset. Nothing is being pulled, so this must not take TAIL, whether or
// not it names the channel that holds it.
//
// Distinct from set_unloaded for the same reason preempt is: both name a real
// channel and both mean "let go", so the state machine cannot tell them apart
// from the arguments alone. The difference is entirely in what the printer was
// doing, which only the call site knows. Design row 10.
void ams_state_session_idle(uint8_t filament_ch);

// `claiming_ch` is beginning a load and is taking the merger, TAIL included.
// Distinct from set_unloaded(0xFF) because send_out and the idle reset both
// spell "no particular channel" yet mean opposite things: one is another
// channel claiming the tube, the other is the session ending. This is also the
// only escape from a stale TAIL, which is what lets TAIL have no timeout.
void ams_state_preempt(uint8_t claiming_ch);
// LOADED -> TAIL. Takes no channel: the merger already knows its owner, and
// that is the only channel this edge could ever apply to.
void ams_state_set_tail(void);
uint8_t ams_state_get_loaded(void);
bool ams_state_is_tail(void);
