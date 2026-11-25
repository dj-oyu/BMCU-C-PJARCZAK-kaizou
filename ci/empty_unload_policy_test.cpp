// Host test for src/empty_unload_policy.h.

#include "empty_unload_policy.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

int main(void)
{
    // The case that hung live: pull_back with nothing at the key.
    CHECK(empty_unload::completes_at_once(_filament_motion::pull_back, 0u), 11);

    // Filament at the key in any reading: the normal pull-back sequencer runs.
    for (uint8_t ks = 1u; ks <= 3u; ++ks)
        CHECK(!empty_unload::completes_at_once(_filament_motion::pull_back, ks), 12);

    // Every other motion is untouched, empty key or not -- in particular the
    // before_pull_back prelude and the load-side motions.
    const _filament_motion others[] = {
        _filament_motion::idle, _filament_motion::send_out, _filament_motion::on_use,
        _filament_motion::before_pull_back, _filament_motion::before_on_use,
        _filament_motion::stop_on_use,
    };
    for (unsigned i = 0; i < sizeof(others) / sizeof(others[0]); ++i)
        CHECK(!empty_unload::completes_at_once(others[i], 0u), 20 + (int)i);

    return 0;
}
