/* Local loader for a separately supplied Most Wanted PC LAN server.dll.
 * Build for 32-bit Windows. The working directory must contain server.dll and
 * the owner's server.cfg; no game binaries are distributed with this source.
 */
#include <windows.h>
#include <stdio.h>

typedef unsigned char (__cdecl *start_fn)(const char *, int, void *, void *);
typedef void (__cdecl *stop_fn)(void);
typedef int (__cdecl *running_fn)(void);

static volatile LONG stop_requested;

static BOOL WINAPI on_control(DWORD event) {
    if (event == CTRL_C_EVENT || event == CTRL_BREAK_EVENT || event == CTRL_CLOSE_EVENT) {
        InterlockedExchange(&stop_requested, 1);
        return TRUE;
    }
    return FALSE;
}

int main(void) {
    HMODULE dll = LoadLibraryA("server.dll");
    if (!dll) { fprintf(stderr, "Cannot load server.dll: %lu\n", GetLastError()); return 1; }
    start_fn start = (start_fn)GetProcAddress(dll, "StartServer");
    stop_fn stop = (stop_fn)GetProcAddress(dll, "StopServer");
    running_fn running = (running_fn)GetProcAddress(dll, "IsServerRunning");
    if (!start || !stop || !running) {
        fprintf(stderr, "server.dll is missing required exports\n");
        FreeLibrary(dll);
        return 1;
    }
    SetConsoleCtrlHandler(on_control, TRUE);
    if (!start("NFSMW", 0, NULL, NULL)) {
        fprintf(stderr, "Original server failed to start\n");
        FreeLibrary(dll);
        return 1;
    }
    puts("Original MW server started");
    fflush(stdout);
    while (!InterlockedCompareExchange(&stop_requested, 0, 0) && running()) Sleep(250);
    stop();
    FreeLibrary(dll);
    return 0;
}
