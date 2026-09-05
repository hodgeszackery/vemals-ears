/* Keep the installed caption UI in VemalsEars.exe for Windows identity.
   Both products share the CPython host, with separate entrypoints,
   pairing schemes, and AppUserModelIDs. The VE installation requires
   its own private runtime and never falls back to a registered Python. */
#define VEMALS_EARS
#include "../Windows/DowntimeLauncher.c"
