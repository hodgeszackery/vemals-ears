/* Compile for each product to test rejection without opening error dialogs. */
#include "../DowntimeLauncher.c"

int main(void) {
    wchar_t *normal[] = { L"app.exe" };
    wchar_t *valid[] = { L"app.exe", APP_PAIRING_ARGUMENT,
                        APP_PAIRING_PREFIX L"code=smoke&source=unit" };
    wchar_t *control[] = { L"app.exe", APP_PAIRING_ARGUMENT,
                          APP_PAIRING_PREFIX L"code=smoke\n" };
#ifdef VEMALS_EARS
    wchar_t *other[] = { L"app.exe", L"--downtime-pair",
                        L"downtime://pair?code=smoke" };
#else
    wchar_t *other[] = { L"app.exe", L"--vemals-ears-pair",
                        L"vemalsears://pair?code=smoke" };
#endif
    wchar_t *crossed[] = { L"app.exe", APP_PAIRING_ARGUMENT, other[2] };
    wchar_t *crossed_flag[] = { L"app.exe", other[1], valid[2] };
    if (!pairing_arguments_are_safe(1, normal)) return 1;
    if (!pairing_arguments_are_safe(3, valid)) return 2;
    if (pairing_arguments_are_safe(2, valid)) return 3;
#ifdef VEMALS_EARS
    if (pairing_arguments_are_safe(3, other)) return 4;
#else
    if (!pairing_arguments_are_safe(3, other)) return 4;
#endif
    if (pairing_arguments_are_safe(3, crossed)) return 5;
    if (pairing_arguments_are_safe(3, control)) return 6;
    if (pairing_arguments_are_safe(3, crossed_flag)) return 7;
    return 0;
}
