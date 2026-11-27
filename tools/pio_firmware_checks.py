# PlatformIO extra script (env:base). Two jobs, both about the 60 KB budget:
#
# 1. Link-time options that cannot go through build_flags, because PlatformIO
#    routes those to the compiler only. With LTO the code is generated at link
#    time, so these are the options that count.
#    -flto-partition=none lets LTO see the whole program as one unit; it
#    measured 440 B smaller than the default.
#    -msmall-data-limit=24 (the platform passes 8) puts more globals in
#    .sdata/.sbss, next to the global pointer, where an access is one
#    gp-relative instruction instead of lui + access. Measured: 8 and 12 are the
#    same, 16/20 save ~350 B, 24 ~390 B, 32 and 64 less again (the large
#    objects crowd the 4 KB gp window).
#
# 2. Fail the build when the image pulls in a libgcc routine this firmware has
#    deliberately designed out: single/double soft-float (~4.7 KB with the
#    count-leading-zeros table they share) and 64-bit division (~1 KB). One
#    stray `float` expression or int64 `/` brings them back silently; the size
#    report would only show a few hundred bytes more. Floating-point literals in
#    constexpr context are fine -- they never reach the image.
Import("env")  # noqa: F821  (PlatformIO injects it)

import re
import subprocess

env.Append(LINKFLAGS=["-flto-partition=none", "-msmall-data-limit=24"])  # noqa: F821

FORBIDDEN = re.compile(
    r"^__(?:"
    r"(?:add|sub|mul|div|neg)[sd]f3"
    r"|(?:fix|fixuns)[sd]f[sd]i"
    r"|float(?:un)?[sd]i[sd]f"
    r"|(?:eq|ne|lt|le|gt|ge|unord)[sd]f2"
    r"|extendsfdf2|truncdfsf2"
    r"|(?:u?div|u?mod)di3"
    r")$"
)


def check_forbidden_symbols(target, source, env):
    elf = str(target[0])
    nm = env.subst("$CC").replace("gcc", "nm")
    out = subprocess.run([nm, "--defined-only", elf], check=True,
                         capture_output=True, text=True).stdout
    found = sorted({line.split()[-1] for line in out.splitlines()
                    if line.split() and FORBIDDEN.match(line.split()[-1])})
    if found:
        print("\nERROR: the firmware links runtime routines it must not use:")
        for name in found:
            print("  " + name)
        print("A float/double expression or a 64-bit division reached the image."
              " Use the fixed-point helpers in src/sensor_fixed.h, or make the"
              " constant constexpr.\n")
        env.Exit(1)


env.AddPostAction("$BUILD_DIR/${PROGNAME}.elf", check_forbidden_symbols)  # noqa: F821
