/* SPDX-License-Identifier: GPL-3.0-only
 * Copyright (C) 2026 Zackery Hodges
 */
#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0602

#include <windows.h>
#include <shellapi.h>
#include <shobjidl.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#define MAX_PAIRING_URI 4096
#define PATH_BUFFER 32768
#define KEY_NAME_BUFFER 256
#define ARRAY_LENGTH(value) (sizeof(value) / sizeof((value)[0]))

#ifdef VEMALS_EARS
#define APP_PRODUCT_NAME L"Vemal's Ears"
#define APP_MODEL_ID L"VemalsEars.Companion"
#define APP_SCRIPT_PATH L"%ls\\app\\Vemals-Ears.pyw"
#define APP_PAIRING_ARGUMENT L"--vemals-ears-pair"
#define APP_PAIRING_PREFIX L"vemalsears://pair?"
#define APP_REQUIRES_BUNDLED_RUNTIME 1
#elif defined(DOWNTIME_LITE)
#define APP_PRODUCT_NAME L"Downtime Lite"
#define APP_MODEL_ID L"Downtime.Companion"
#define APP_SCRIPT_PATH L"%ls\\WoW-Companion.pyw"
#define APP_PAIRING_ARGUMENT L"--downtime-pair"
#define APP_PAIRING_PREFIX L"downtime://pair?"
#define APP_REQUIRES_BUNDLED_RUNTIME 0
#else
#define APP_PRODUCT_NAME L"Downtime"
#define APP_MODEL_ID L"Downtime.Companion"
#define APP_SCRIPT_PATH L"%ls\\WoW-Companion.pyw"
#define APP_PAIRING_ARGUMENT L"--downtime-pair"
#define APP_PAIRING_PREFIX L"downtime://pair?"
#define APP_REQUIRES_BUNDLED_RUNTIME 0
#endif

/* The secure CRT's default answer to a bad argument - a destination
   buffer too small, a NULL pointer - is to terminate the process through
   __fastfail, which shows the user nothing at all. Every *_s call in this
   file already checks its return value, so an empty handler turns those
   checks into the error handling they were written to be, and a failure
   becomes a reported one instead of a window that never appears. */
static void __cdecl quiet_invalid_parameter(
    const wchar_t *expression,
    const wchar_t *function,
    const wchar_t *file,
    unsigned int line,
    uintptr_t reserved
) {
    (void)expression;
    (void)function;
    (void)file;
    (void)line;
    (void)reserved;
}

typedef int (__cdecl *PyMainFunction)(int, wchar_t **);
typedef const char *(__cdecl *PyGetVersionFunction)(void);

typedef struct PythonRuntime {
    wchar_t executable[PATH_BUFFER];
    wchar_t library[PATH_BUFFER];
    unsigned long major;
    unsigned long minor;
} PythonRuntime;

static const wchar_t PAIRING_URI_PREFIX[] = APP_PAIRING_PREFIX;

static int fail(const wchar_t *message) {
    MessageBoxW(
        NULL,
        message,
        APP_PRODUCT_NAME,
        MB_OK | MB_ICONERROR | MB_SETFOREGROUND
    );
    return 1;
}

static int regular_file(const wchar_t *path) {
    DWORD attributes = GetFileAttributesW(path);
    return attributes != INVALID_FILE_ATTRIBUTES
        && !(attributes & FILE_ATTRIBUTE_DIRECTORY);
}

static int absolute_windows_path(const wchar_t *path) {
    int drive_letter =
        ((path[0] >= L'A' && path[0] <= L'Z')
         || (path[0] >= L'a' && path[0] <= L'z'))
        && path[1] == L':'
        && (path[2] == L'\\' || path[2] == L'/');
    int unc_path =
        (path[0] == L'\\' || path[0] == L'/')
        && (path[1] == L'\\' || path[1] == L'/');
    return drive_letter || unc_path;
}

static int pythonw_file(const wchar_t *path) {
    const wchar_t *name = wcsrchr(path, L'\\');
    if (name == NULL) {
        name = wcsrchr(path, L'/');
    }
    name = name == NULL ? path : name + 1;
    return _wcsicmp(name, L"pythonw.exe") == 0;
}

static int pairing_arguments_are_safe(int argc, wchar_t **argv) {
    size_t length;
    size_t index;
    const wchar_t *prefix = PAIRING_URI_PREFIX;
    size_t prefix_length = ARRAY_LENGTH(PAIRING_URI_PREFIX) - 1;

    if (argc == 1) {
        return 1;
    }
    if (argc != 3) {
        return 0;
    }
    if (wcscmp(argv[1], APP_PAIRING_ARGUMENT) != 0) {
#ifndef VEMALS_EARS
        /* Older Downtime installs registered this flag and scheme. Keep
           their invitations working, but route them through Downtime's
           own credential store in the Python application. */
        if (wcscmp(argv[1], L"--vemals-ears-pair") == 0) {
            prefix = L"vemalsears://pair?";
            prefix_length = wcslen(prefix);
        } else {
            return 0;
        }
#else
        return 0;
#endif
    }
    length = wcslen(argv[2]);
    if (
        length == 0
        || length > MAX_PAIRING_URI
        || _wcsnicmp(
            argv[2],
            prefix,
            prefix_length
        ) != 0
    ) {
        return 0;
    }
    for (index = 0; index < length; index++) {
        if (argv[2][index] < 32 || argv[2][index] == 127) {
            return 0;
        }
    }
    return 1;
}

static int parse_python_version(
    const wchar_t *text,
    unsigned long *major,
    unsigned long *minor
) {
    wchar_t *separator;
    wchar_t *end;
    unsigned long parsed_major;
    unsigned long parsed_minor;

    parsed_major = wcstoul(text, &separator, 10);
    if (separator == text || *separator != L'.') {
        return 0;
    }
    parsed_minor = wcstoul(separator + 1, &end, 10);
    if (end == separator + 1 || (*end != L'\0' && *end != L'-' && *end != L't')) {
        return 0;
    }
    if (parsed_major != 3 || parsed_minor < 10) {
        return 0;
    }
    *major = parsed_major;
    *minor = parsed_minor;
    return 1;
}

static int query_registry_string(
    HKEY key,
    const wchar_t *value_name,
    wchar_t *destination,
    DWORD destination_count
) {
    DWORD bytes = destination_count * (DWORD)sizeof(wchar_t);
    LSTATUS status;

    destination[0] = L'\0';
    status = RegGetValueW(
        key,
        NULL,
        value_name,
        RRF_RT_REG_SZ | RRF_RT_REG_EXPAND_SZ,
        NULL,
        destination,
        &bytes
    );
    if (status != ERROR_SUCCESS || destination[0] == L'\0') {
        destination[0] = L'\0';
        return 0;
    }
    destination[destination_count - 1] = L'\0';
    return 1;
}

static int parent_directory(
    const wchar_t *path,
    wchar_t *destination,
    size_t destination_count
) {
    wchar_t *separator;

    if (wcscpy_s(destination, destination_count, path) != 0) {
        return 0;
    }
    separator = wcsrchr(destination, L'\\');
    if (separator == NULL || separator == destination) {
        return 0;
    }
    *separator = L'\0';
    return 1;
}

static HMODULE load_python_library(const wchar_t *path) {
    return LoadLibraryExW(
        path,
        NULL,
        LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32
    );
}

static int actual_runtime_version(
    const wchar_t *library,
    unsigned long *major,
    unsigned long *minor
) {
    HMODULE module;
    PyGetVersionFunction get_version;
    const char *version;
    unsigned long parsed_major = 0;
    unsigned long parsed_minor = 0;

    module = load_python_library(library);
    if (module == NULL) {
        return 0;
    }
    get_version = (PyGetVersionFunction)(void *)GetProcAddress(module, "Py_GetVersion");
    if (get_version == NULL || GetProcAddress(module, "Py_Main") == NULL) {
        FreeLibrary(module);
        return 0;
    }
    version = get_version();
    if (
        version == NULL
        || sscanf_s(version, "%lu.%lu", &parsed_major, &parsed_minor) != 2
        || parsed_major != 3
        || parsed_minor < 10
    ) {
        FreeLibrary(module);
        return 0;
    }
    FreeLibrary(module);
    *major = parsed_major;
    *minor = parsed_minor;
    return 1;
}

static int find_runtime_library(
    const wchar_t *directory,
    unsigned long registry_major,
    unsigned long registry_minor,
    wchar_t *library,
    unsigned long *actual_major,
    unsigned long *actual_minor
) {
    wchar_t candidate[PATH_BUFFER];
    int written;

    written = swprintf_s(
        candidate,
        ARRAY_LENGTH(candidate),
        L"%ls\\python%lu%lu.dll",
        directory,
        registry_major,
        registry_minor
    );
    if (
        written > 0
        && regular_file(candidate)
        && actual_runtime_version(candidate, actual_major, actual_minor)
        && *actual_major == registry_major
        && *actual_minor == registry_minor
    ) {
        return wcscpy_s(library, PATH_BUFFER, candidate) == 0;
    }

    written = swprintf_s(
        candidate,
        ARRAY_LENGTH(candidate),
        L"%ls\\python%lu%lut.dll",
        directory,
        registry_major,
        registry_minor
    );
    if (
        written > 0
        && regular_file(candidate)
        && actual_runtime_version(candidate, actual_major, actual_minor)
        && *actual_major == registry_major
        && *actual_minor == registry_minor
    ) {
        return wcscpy_s(library, PATH_BUFFER, candidate) == 0;
    }
    return 0;
}

static void consider_python_tag(
    HKEY tag_key,
    const wchar_t *tag_name,
    PythonRuntime *best
) {
    HKEY install_key;
    wchar_t version[64];
    wchar_t architecture[64];
    wchar_t executable[PATH_BUFFER];
    wchar_t directory[PATH_BUFFER];
    wchar_t library[PATH_BUFFER];
    unsigned long registry_major;
    unsigned long registry_minor;
    unsigned long actual_major;
    unsigned long actual_minor;

    if (!query_registry_string(tag_key, L"SysVersion", version, ARRAY_LENGTH(version))) {
        /* Check the length BEFORE the copy. wcscpy_s does not simply return
           a non-zero code when the destination is too small: the secure CRT
           invokes the invalid parameter handler, and with none installed the
           UCRT calls __fastfail and terminates the process - so the test
           below was unreachable dead code. A registry key name may be 255
           characters and HKCU needs no administrator rights, so any program
           on the machine could leave a key here that made Downtime refuse to
           start with no window, no dialog, no exit code and nothing to
           diagnose. An over-long tag cannot be a Python version anyway, so
           skipping it is the correct handling as well as the safe one. */
        if (wcslen(tag_name) >= ARRAY_LENGTH(version)) {
            return;
        }
        if (wcscpy_s(version, ARRAY_LENGTH(version), tag_name) != 0) {
            return;
        }
    }
    if (!parse_python_version(version, &registry_major, &registry_minor)) {
        return;
    }
    if (
        query_registry_string(
            tag_key,
            L"SysArchitecture",
            architecture,
            ARRAY_LENGTH(architecture)
        )
        && _wcsicmp(architecture, L"64bit") != 0
    ) {
        return;
    }
    if (
        RegOpenKeyExW(tag_key, L"InstallPath", 0, KEY_READ, &install_key)
        != ERROR_SUCCESS
    ) {
        return;
    }
    if (!query_registry_string(
        install_key,
        L"WindowedExecutablePath",
        executable,
        ARRAY_LENGTH(executable)
    )) {
        if (
            !query_registry_string(
                install_key,
                NULL,
                directory,
                ARRAY_LENGTH(directory)
            )
            || swprintf_s(
                executable,
                ARRAY_LENGTH(executable),
                L"%ls\\pythonw.exe",
                directory
            ) < 0
        ) {
            RegCloseKey(install_key);
            return;
        }
    }
    RegCloseKey(install_key);
    if (
        !absolute_windows_path(executable)
        || !pythonw_file(executable)
        || !regular_file(executable)
        || !parent_directory(executable, directory, ARRAY_LENGTH(directory))
    ) {
        return;
    }
    if (!find_runtime_library(
        directory,
        registry_major,
        registry_minor,
        library,
        &actual_major,
        &actual_minor
    )) {
        return;
    }
    if (
        best->executable[0] != L'\0'
        && (
            actual_major < best->major
            || (actual_major == best->major && actual_minor < best->minor)
        )
    ) {
        return;
    }
    if (
        wcscpy_s(best->executable, ARRAY_LENGTH(best->executable), executable) != 0
        || wcscpy_s(best->library, ARRAY_LENGTH(best->library), library) != 0
    ) {
        return;
    }
    best->major = actual_major;
    best->minor = actual_minor;
}

static void find_registered_python_at(HKEY hive, PythonRuntime *best) {
    HKEY companies;
    HKEY company_key;
    HKEY tag_key;
    DWORD company_index = 0;
    DWORD tag_index;
    wchar_t company_name[KEY_NAME_BUFFER];
    wchar_t tag_name[KEY_NAME_BUFFER];
    DWORD company_length;
    DWORD tag_length;

    /* PEP 514: Software\Python\<Company>\<Tag>\InstallPath. */
    if (
        RegOpenKeyExW(
            hive,
            L"Software\\Python",
            0,
            KEY_READ | KEY_WOW64_64KEY,
            &companies
        ) != ERROR_SUCCESS
    ) {
        return;
    }
    for (;;) {
        company_length = ARRAY_LENGTH(company_name);
        if (RegEnumKeyExW(
            companies,
            company_index,
            company_name,
            &company_length,
            NULL,
            NULL,
            NULL,
            NULL
        ) != ERROR_SUCCESS) {
            break;
        }
        company_index++;
        if (RegOpenKeyExW(companies, company_name, 0, KEY_READ, &company_key) != ERROR_SUCCESS) {
            continue;
        }
        tag_index = 0;
        for (;;) {
            tag_length = ARRAY_LENGTH(tag_name);
            if (RegEnumKeyExW(
                company_key,
                tag_index,
                tag_name,
                &tag_length,
                NULL,
                NULL,
                NULL,
                NULL
            ) != ERROR_SUCCESS) {
                break;
            }
            tag_index++;
            if (RegOpenKeyExW(company_key, tag_name, 0, KEY_READ, &tag_key) != ERROR_SUCCESS) {
                continue;
            }
            consider_python_tag(tag_key, tag_name, best);
            RegCloseKey(tag_key);
        }
        RegCloseKey(company_key);
    }
    RegCloseKey(companies);
}

/* The runtime this install ships, in the "runtime" folder beside the
   launcher. Tried FIRST, and the reason is a night Shino lost: the app
   refused to start with "Downtime needs a registered 64-bit Python 3.10
   or newer installation" while a complete, correct Python 3.14 sat in
   that folder, untouched, because the launcher only ever asked the
   registry. A machine-wide registry key is not ours - an installer, an
   update or a version manager can rewrite it at any moment - and an
   application that carries its own interpreter should never be stopped
   by one.

   The version is not assumed: every python3*.dll here is offered to
   actual_runtime_version, which loads it and asks it directly, so the
   bundled runtime can be upgraded without touching this file.
   python3.dll is skipped by name - it is the stable ABI shim and
   carries no Py_Main. */
static int find_bundled_python(const wchar_t *root, PythonRuntime *runtime) {
    wchar_t directory[PATH_BUFFER];
    wchar_t pattern[PATH_BUFFER];
    wchar_t candidate[PATH_BUFFER];
    wchar_t executable[PATH_BUFFER];
    WIN32_FIND_DATAW found;
    HANDLE search;
    unsigned long major;
    unsigned long minor;

    if (swprintf_s(directory, ARRAY_LENGTH(directory), L"%ls\\runtime", root) < 0) {
        return 0;
    }
    if (
        swprintf_s(executable, ARRAY_LENGTH(executable), L"%ls\\pythonw.exe", directory) < 0
        || !regular_file(executable)
    ) {
        return 0;
    }
    if (swprintf_s(pattern, ARRAY_LENGTH(pattern), L"%ls\\python3*.dll", directory) < 0) {
        return 0;
    }
    search = FindFirstFileW(pattern, &found);
    if (search == INVALID_HANDLE_VALUE) {
        return 0;
    }
    do {
        if (found.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            continue;
        }
        if (_wcsicmp(found.cFileName, L"python3.dll") == 0) {
            continue;
        }
        if (
            swprintf_s(
                candidate,
                ARRAY_LENGTH(candidate),
                L"%ls\\%ls",
                directory,
                found.cFileName
            ) < 0
        ) {
            continue;
        }
        if (!actual_runtime_version(candidate, &major, &minor)) {
            continue;
        }
        if (
            wcscpy_s(runtime->executable, PATH_BUFFER, executable) != 0
            || wcscpy_s(runtime->library, PATH_BUFFER, candidate) != 0
        ) {
            continue;
        }
        runtime->major = major;
        runtime->minor = minor;
        FindClose(search);
        return 1;
    } while (FindNextFileW(search, &found));
    FindClose(search);
    return 0;
}

static int find_python(const wchar_t *root, PythonRuntime *runtime) {
    ZeroMemory(runtime, sizeof(*runtime));
    if (find_bundled_python(root, runtime)) {
        return 1;
    }
    if (APP_REQUIRES_BUNDLED_RUNTIME) {
        return 0;
    }
    find_registered_python_at(HKEY_LOCAL_MACHINE, runtime);
    find_registered_python_at(HKEY_CURRENT_USER, runtime);
    return runtime->executable[0] != L'\0' && runtime->library[0] != L'\0';
}

int WINAPI wWinMain(
    HINSTANCE instance,
    HINSTANCE previous_instance,
    PWSTR command_line,
    int show_command
) {
    wchar_t executable[PATH_BUFFER];
    wchar_t root[PATH_BUFFER];
    wchar_t script[PATH_BUFFER];
    wchar_t **launcher_argv;
    wchar_t **python_argv;
    wchar_t ignore_environment[] = L"-E";
    wchar_t no_user_site[] = L"-s";
    wchar_t no_bytecode[] = L"-B";
    int launcher_argc;
    int python_argc;
    int index;
    DWORD executable_length;
    PythonRuntime runtime;
    HMODULE python_module;
    PyMainFunction python_main;
    int exit_code;

    (void)instance;
    (void)previous_instance;
    (void)command_line;
    (void)show_command;

    _set_invalid_parameter_handler(quiet_invalid_parameter);

    SetCurrentProcessExplicitAppUserModelID(APP_MODEL_ID);

    executable_length = GetModuleFileNameW(NULL, executable, ARRAY_LENGTH(executable));
    if (executable_length == 0 || executable_length >= ARRAY_LENGTH(executable)) {
        return fail(APP_PRODUCT_NAME L" could not find its files.");
    }
    if (!parent_directory(executable, root, ARRAY_LENGTH(root))) {
        return fail(APP_PRODUCT_NAME L" could not find its files.");
    }
    if (
        swprintf_s(script, ARRAY_LENGTH(script), APP_SCRIPT_PATH, root) < 0
        || !regular_file(script)
    ) {
        return fail(APP_PRODUCT_NAME L" needs to be repaired or extracted again.");
    }

    launcher_argv = CommandLineToArgvW(GetCommandLineW(), &launcher_argc);
    if (launcher_argv == NULL) {
        return fail(APP_PRODUCT_NAME L" could not read the launch request.");
    }
    if (!pairing_arguments_are_safe(launcher_argc, launcher_argv)) {
        LocalFree(launcher_argv);
        return fail(L"The " APP_PRODUCT_NAME L" launch request is invalid.");
    }
    if (!find_python(root, &runtime)) {
        LocalFree(launcher_argv);
        return fail(
            APP_PRODUCT_NAME L" could not find its Python runtime. Reinstall "
            APP_PRODUCT_NAME L" or extract the complete application download."
        );
    }

    python_module = load_python_library(runtime.library);
    if (python_module == NULL) {
        LocalFree(launcher_argv);
        return fail(APP_PRODUCT_NAME L" could not load its Python runtime.");
    }
    python_main = (PyMainFunction)(void *)GetProcAddress(python_module, "Py_Main");
    if (python_main == NULL) {
        LocalFree(launcher_argv);
        return fail(APP_PRODUCT_NAME L" found an incompatible Python runtime.");
    }

    python_argc = launcher_argc + 4;
    python_argv = calloc((size_t)python_argc + 1, sizeof(*python_argv));
    if (python_argv == NULL) {
        LocalFree(launcher_argv);
        return fail(APP_PRODUCT_NAME L" could not start because memory is unavailable.");
    }
    /*
     * Host Python in the product executable, but give Py_Main pythonw as
     * argv[0]. The window belongs to that product while sys.executable
     * remains usable by the companion's child-worker launch code.
     */
    python_argv[0] = runtime.executable;
    python_argv[1] = ignore_environment;
    python_argv[2] = no_user_site;
    python_argv[3] = no_bytecode;
    python_argv[4] = script;
    for (index = 1; index < launcher_argc; index++) {
        python_argv[index + 4] = launcher_argv[index];
    }

    if (!SetCurrentDirectoryW(root)) {
        free(python_argv);
        LocalFree(launcher_argv);
        return fail(APP_PRODUCT_NAME L" could not open its application folder.");
    }
    exit_code = python_main(python_argc, python_argv);

    free(python_argv);
    LocalFree(launcher_argv);
    return exit_code;
}
