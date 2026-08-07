#pragma once

#include "ams.h"

// Which printer commands release a latched motion fault.
//
// All three fault codes are covered — PULL_NO_PROGRESS, PULL_TRAVEL_BUDGET,
// and REDETECT_TIMEOUT — because all three are latched by latch_pull_fault in
// the pull/redetect sequencer and vetoed by the same motor_motion_switch
// check, which force-stops the channel on every pass. That veto needs a release path, and until 2026-08-07 the only
// one was a pull_back retry on the selected channel. A printer-commanded load
// is just as explicit a statement of operator intent as a retract retry, and
// the drive it enables is separately bounded (the send path by the jam latch
// and buffer feedback, a new pull by the same no-progress latch that fired).
// Leaving it out produced a deadlock seen live: the exchange abort loop
// latched the fault, every subsequent send_out was silently stopped while
// STATUS kept reporting it accepted, and only a power cycle recovered.
//
// stop_on_use and on_use are not release paths: faults latch only in the
// pull/redetect sequencer, so arriving in those states with a fault means the
// printer skipped the load handshake — keep the veto until it sends one.
// before_pull_back is also left out deliberately: its assist drive is
// optional, and the pull_back that follows it releases the fault anyway.
//
// Header-only so the decision is host-testable; Motion_control.cpp is not.
namespace motion_fault
{

inline bool releases_pull_fault(bool selected, _filament_motion motion)
{
    if (!selected) return false;

    switch (motion)
    {
    case _filament_motion::pull_back:
    case _filament_motion::send_out:
    case _filament_motion::before_on_use:
        return true;
    default:
        return false;
    }
}

}
