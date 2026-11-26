// Host test for src/pull_back_end_policy.h.

#include "pull_back_end_policy.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

int main(void)
{
    // The inner switch still has filament: keep pulling.
    CHECK(!pull_back_end::inner_released(1u), 11);   // both
    CHECK(!pull_back_end::inner_released(3u), 12);   // inner only

    // The tip has left the inner switch.
    CHECK(pull_back_end::inner_released(0u), 21);    // none: the previous end
    // Outer lever still on the strand, AMS-B slot 3 2026-11-26: the gear
    // would pull the tip behind itself and never reach none.
    CHECK(pull_back_end::inner_released(2u), 22);

    // Out-of-range readings never end a pull back.
    for (unsigned ks = 4u; ks <= 255u; ++ks)
        CHECK(!pull_back_end::inner_released((uint8_t)ks), 30);

    // Filament at the inner switch, on either loader: both, or inner only.
    CHECK(pull_back_end::inner_present(1u), 41);
    CHECK(pull_back_end::inner_present(3u), 42);
    CHECK(!pull_back_end::inner_present(0u), 43);
    CHECK(!pull_back_end::inner_present(2u), 44);   // outer only: tip short of it
    // Auto-unload's old test was ks != 1, which called a strand resting at
    // inner only (3) empty before it moved; check 42 is the case it got wrong.

    // Reseat only a retract that ran the key to none.
    CHECK(pull_back_end::reseat_after_retract(true, 0u), 51);
    for (uint8_t ks = 1u; ks <= 3u; ++ks)
        CHECK(!pull_back_end::reseat_after_retract(true, ks), 52);
    for (uint8_t ks = 0u; ks <= 3u; ++ks)
        CHECK(!pull_back_end::reseat_after_retract(false, ks), 53);

    return 0;
}
