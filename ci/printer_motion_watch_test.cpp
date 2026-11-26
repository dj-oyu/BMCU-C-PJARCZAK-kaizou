// Host test for src/printer_motion_watch.h.

#include "printer_motion_watch.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

int main(void)
{
    using namespace printer_motion_watch;

    CHECK(pack(kKindMotion, 1u, 2u, 0x09u, 0x3Fu) == 0x0102093Fu, 1);
    CHECK(pack(kKindStuMotion, 0u, 0xFFu, 0x03u, 0x00u) == 0x80FF0300u, 2);

    State s;
    reset(s);

    // First command for a unit is always recorded, even an all-zero one.
    CHECK(note(s, kKindMotion, 0u, 0u), 10);
    CHECK(!note(s, kKindMotion, 0u, 0u), 11);

    // Repeats are suppressed; a change is recorded once.
    const uint32_t before_pull = pack(kKindMotion, 1u, 2u, 0x09u, 0x3Fu);
    const uint32_t pull = pack(kKindMotion, 1u, 0xFFu, 0x03u, 0x00u);
    CHECK(note(s, kKindMotion, 1u, before_pull), 20);
    CHECK(!note(s, kKindMotion, 1u, before_pull), 21);
    CHECK(note(s, kKindMotion, 1u, pull), 22);
    CHECK(!note(s, kKindMotion, 1u, pull), 23);
    CHECK(note(s, kKindMotion, 1u, before_pull), 24);

    // The two frame kinds are tracked apart, so interleaved motion and
    // stu_motion frames carrying different flags do not record every frame.
    const uint32_t stu = pack(kKindStuMotion, 1u, 2u, 0x07u, 0x7Fu);
    CHECK(note(s, kKindStuMotion, 1u, stu), 30);
    CHECK(!note(s, kKindMotion, 1u, before_pull), 31);
    CHECK(!note(s, kKindStuMotion, 1u, stu), 32);

    // Units are tracked independently: AMS 0 traffic does not mask AMS 1.
    CHECK(note(s, kKindMotion, 0u, pack(kKindMotion, 0u, 1u, 0x07u, 0x7Fu)), 40);
    CHECK(!note(s, kKindMotion, 1u, before_pull), 41);

    // Out-of-range unit numbers are ignored.
    CHECK(!note(s, kKindMotion, 4u, 1u), 50);
    CHECK(!note(s, kKindStuMotion, 0xFFu, 1u), 51);

    reset(s);
    CHECK(note(s, kKindMotion, 1u, before_pull), 60);
    return 0;
}
