// Host test for src/motion_fault_policy.h.
// The call site lives in motor_motion_switch in Motion_control.cpp, which does
// not compile on the host, so the release decision is checked here.

#include "motion_fault_policy.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

// The 2026-08-07 deadlock: a printer-commanded load on the selected channel
// must release the fault. pull_back keeps its historical behaviour.
static int test_selected_release_motions(void)
{
    CHECK(motion_fault::releases_pull_fault(true, _filament_motion::pull_back), 11);
    CHECK(motion_fault::releases_pull_fault(true, _filament_motion::send_out), 12);
    CHECK(motion_fault::releases_pull_fault(true, _filament_motion::before_on_use), 13);
    return 0;
}

// States the printer can only reach after a load handshake, plus rest states,
// keep the veto.
static int test_selected_non_release_motions(void)
{
    CHECK(!motion_fault::releases_pull_fault(true, _filament_motion::idle), 21);
    CHECK(!motion_fault::releases_pull_fault(true, _filament_motion::on_use), 22);
    CHECK(!motion_fault::releases_pull_fault(true, _filament_motion::stop_on_use), 23);
    CHECK(!motion_fault::releases_pull_fault(true, _filament_motion::before_pull_back), 24);
    return 0;
}

// The selected channel's motion must never release another channel's fault:
// motor_motion_switch evaluates the same (num, motion) pair for all four
// channels, and only i == num expresses printer intent about channel i.
static int test_unselected_never_releases(void)
{
    for (uint8_t m = 0; m <= 6; ++m)
    {
        CHECK(!motion_fault::releases_pull_fault(
                  false, static_cast<_filament_motion>(m)), 30 + m);
    }
    return 0;
}

int main(void)
{
    int code = 0;
    if ((code = test_selected_release_motions()) != 0) return code;
    if ((code = test_selected_non_release_motions()) != 0) return code;
    if ((code = test_unselected_never_releases()) != 0) return code;
    return 0;
}
