#pragma once

#include <stdint.h>

#include "ams.h"

// What to do when the printer commands a retract on a channel whose online key
// already reads empty.
//
// The printer only unloads a channel it believes is in use, and it believes
// that from our replies: now_filament_num plus filament_use_flag 0x04. That
// belief can outlive the filament -- an operator pulls the strand out by hand,
// or the session is restored from flash at boot (main.cpp) for a strand that
// was removed while the printer was off. The printer then starts a normal
// unload: cutter, extruder retract, and a pull_back command to the AMS.
//
// motor_motion_switch only dispatches a channel's motion while its key reads
// filament, so with the key empty the pull_back was accepted on the wire and
// then nothing happened: the motion stayed pull_back, use_flag stayed set, the
// meters never moved, and the printer reported "AMS failed to pull back
// filament" / "Failed to pull out the filament from the extruder" for a path
// that was already empty. Observed live on 2026-11-25.
//
// There is nothing for the BMCU to pull, so the retract is finished at once
// the same way a successful one finishes (the filament_redetect exit in
// Motion_control.cpp): AMS motion back to idle and use_flag cleared. The merger
// itself is already released by the command (bambu_bus_ams.cpp), so this only
// stops reporting a pull that can never complete. Build with
// -DBMCU_EMPTY_UNLOAD_COMPLETE=0 for the previous behaviour.
//
// before_pull_back is left alone: it is the printer's prelude, its assist
// drive is optional, and the pull_back that follows is the step that has to
// be answered.
//
// Header-only so it is host-testable; Motion_control.cpp is not.
#ifndef BMCU_EMPTY_UNLOAD_COMPLETE
#define BMCU_EMPTY_UNLOAD_COMPLETE 1
#endif

namespace empty_unload
{

inline bool completes_at_once(_filament_motion motion, uint8_t key_state)
{
    return (key_state == 0u) && (motion == _filament_motion::pull_back);
}

}
