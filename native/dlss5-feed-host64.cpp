// dlss5-feed-host64 - the 64-bit half of DLSS5-Feeder for 32-bit games.
//
// A 32-bit game cannot load NGX or the DLSS 5 add-on (both x64-only). This little
// process can: it puts ReShade x64 (dxgi.dll) and renodx-dlss5.addon64 next to
// itself, opens a hidden 1x1 window with a minimal D3D12 swapchain -- so from the
// DLSS 5 add-on's point of view it IS a D3D12 game -- and runs the NGX DLAA
// evaluate on frames the game delivers through cross-process shared textures
// (created game-side on D3D11; see the phase-0 spike) and shared fences.
//
//   dlss5-feed-host64.exe --test   stand-alone: synthetic pattern, no game needed
//                                  (phase-1 proof: "feature 18 created" in ReShade.log)
//   dlss5-feed-host64.exe <pid>    serve the game with that PID over the pipe
//
// Logs to dlss5-feed-host.log next to the exe; the DLSS 5 add-on's own state
// appears in the host's ReShade.log.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <d3d11_4.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <d3dcompiler.h>
#include <cstdio>
#include <cstdarg>
#include <cstdint>
#include <cstring>
#include <algorithm>
#include <fcntl.h>
#include <io.h>
#include <string>
#include <vector>

#include <nvsdk_ngx.h>
#include <nvsdk_ngx_helpers.h>

#include "../src/feed_ipc.h"

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

static char g_log_path[MAX_PATH];
static bool g_show_window = false;   // visible host window = the user's door to the DLSS 5 panel
static bool g_renodx_lazy = false;   // DLSS 5 add-on is v45+ (per-present rescan, lazy adoption)
static bool g_video_mode = false;    // stdin/stdout are a binary frame protocol in this mode

static void Log(const char *fmt, ...);

// Detect the DLSS 5 add-on generation next to this exe: v45+ ('EnableHooks' marker in
// the binary) rescans every present and adopts missed features lazily, so the warm-up
// re-create is unnecessary -- and its EnableHooks key should be '2' (NGX-only) for this
// feeder, written into OUR ReShade.ini before ReShade loads and the add-on reads it.
static void DetectRenodxAddon()
{
    char dir[MAX_PATH], path[MAX_PATH], ini[MAX_PATH];
    GetModuleFileNameA(nullptr, dir, MAX_PATH);
    if (char *s = strrchr(dir, '\\')) *(s + 1) = '\0';
    sprintf_s(path, "%srenodx-dlss5.addon64", dir);
    sprintf_s(ini, "%sReShade.ini", dir);

    HANDLE f = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (f == INVALID_HANDLE_VALUE) { Log("[host] renodx-dlss5.addon64 not found next to the host"); return; }
    const DWORD size = GetFileSize(f, nullptr);
    DWORD got = 0;
    char *buf = (size > 0 && size < 8u * 1024 * 1024) ? static_cast<char *>(malloc(size)) : nullptr;
    if (buf != nullptr && ReadFile(f, buf, size, &got, nullptr) && got == size)
        for (DWORD i = 0; i + 11 < size; ++i)
            if (memcmp(buf + i, "EnableHooks", 11) == 0) { g_renodx_lazy = true; break; }
    free(buf);
    CloseHandle(f);

    char ver[48] = "?";
    DWORD dummy = 0;
    const DWORD vsize = GetFileVersionInfoSizeA(path, &dummy);
    if (vsize > 0)
    {
        void *vdata = malloc(vsize);
        VS_FIXEDFILEINFO *ffi = nullptr;
        UINT flen = 0;
        if (vdata != nullptr && GetFileVersionInfoA(path, 0, vsize, vdata) &&
            VerQueryValueA(vdata, "\\", reinterpret_cast<void **>(&ffi), &flen) && ffi != nullptr)
            sprintf_s(ver, "%u.%u.%u.%u", HIWORD(ffi->dwFileVersionMS), LOWORD(ffi->dwFileVersionMS),
                      HIWORD(ffi->dwFileVersionLS), LOWORD(ffi->dwFileVersionLS));
        free(vdata);
    }
    Log("[host] DLSS 5 add-on: v%s -- %s engine", ver,
        g_renodx_lazy ? "v45+ (lazy adoption; warm-up skipped)" : "classic (warm-up stays on)");

    if (g_renodx_lazy)
    {
        char v[16] = {};
        GetPrivateProfileStringA("RenoDX.DLSS5", "EnableHooks", "", v, sizeof(v), ini);
        if (v[0] == '\0')
        {
            WritePrivateProfileStringA("RenoDX.DLSS5", "EnableHooks", "2", ini);
            Log("[host] EnableHooks was unset; wrote EnableHooks=2 into the host's ReShade.ini");
        }
        else
            Log("[host] EnableHooks=%s (user-set; leaving it alone)", v);
    }
}

static void Log(const char *fmt, ...)
{
    char line[2048];
    va_list ap;
    va_start(ap, fmt);
    _vsnprintf_s(line, sizeof(line), _TRUNCATE, fmt, ap);
    va_end(ap);
    SYSTEMTIME st;
    GetLocalTime(&st);
    FILE *console = g_video_mode ? stderr : stdout;
    fprintf(console, "%02u:%02u:%02u.%03u  %s\n", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds, line);
    fflush(console);
    FILE *f = nullptr;
    if (!g_video_mode && fopen_s(&f, g_log_path, "a") == 0 && f != nullptr)
    {
        fprintf(f, "%02u:%02u:%02u.%03u  %s\n", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds, line);
        fclose(f);
    }
}

static const char *NgxResultName(NVSDK_NGX_Result r)
{
    switch (static_cast<unsigned>(r))
    {
    case 0x1:        return "Success";
    case 0xBAD00005: return "InvalidParameter";
    case 0xBAD00007: return "NotInitialized";
    case 0xBAD00008: return "UnsupportedInputFormat";
    case 0xBAD0000A: return "MissingInput";
    case 0xBAD0000B: return "UnableToInitializeFeature";
    case 0xBAD0000D: return "OutOfGPUMemory";
    case 0xBAD0000E: return "UnsupportedFormat";
    default:         return "?";
    }
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

struct Host
{
    HWND                       hwnd;
    IDXGISwapChain1           *swap;
    ID3D12Device              *dev;
    ID3D12CommandQueue        *queue;      // NGX work
    ID3D12CommandQueue        *pump_queue; // owns the dummy swapchain
    ID3D12GraphicsCommandList *list;
    static const int           kFrames = 3;
    ID3D12CommandAllocator    *alloc[kFrames];
    UINT64                     alloc_fence[kFrames];
    int                        frame_slot;
    ID3D12Fence               *fence;      // internal (allocator ring)
    HANDLE                     fence_event;
    UINT64                     fence_value;

    // cross-process
    ID3D12Fence *fence_in;   // game signals, host waits
    ID3D12Fence *fence_out;  // host signals, game waits

    bool                 ngx_inited;
    NVSDK_NGX_Parameter *params;
    NVSDK_NGX_Handle    *feature;

    ID3D12Resource *tex[FEED_SLOTS];
    UINT            width, height;
    DXGI_FORMAT     color_fmt, output_fmt;
};

static Host h;

using PFN_NR_InitExt = NVSDK_NGX_Result (NVSDK_CONV *)(unsigned long long, const wchar_t *, ID3D12Device *, NVSDK_NGX_Version, const NVSDK_NGX_Parameter *);
using PFN_NR_Create = NVSDK_NGX_Result (NVSDK_CONV *)(ID3D12GraphicsCommandList *, NVSDK_NGX_Feature, const NVSDK_NGX_Parameter *, NVSDK_NGX_Handle **);
using PFN_NR_Evaluate = NVSDK_NGX_Result (NVSDK_CONV *)(ID3D12GraphicsCommandList *, const NVSDK_NGX_Handle *, const NVSDK_NGX_Parameter *, PFN_NVSDK_NGX_ProgressCallback);
using PFN_NR_Release = NVSDK_NGX_Result (NVSDK_CONV *)(NVSDK_NGX_Handle *);
static HMODULE g_nr_module;
static PFN_NR_InitExt g_nr_init_ext;
static PFN_NR_Create g_nr_create;
static PFN_NR_Evaluate g_nr_evaluate;
static PFN_NR_Release g_nr_release;
static uint32_t g_create_result;
static uint32_t g_eval_count;


// ---------------------------------------------------------------------------
// Подмена архитектуры GPU для feature 18 (NS_ARCH_SPOOF=1)
//
// nvngx_dlssnr.dll отказывается создавать feature на всём, что старше
// Blackwell: внутри лежит проверка с сообщением
// "DLSSNR: Unsupported GPU architecture 0x%x, minimum required 0x%x".
// При этом скомпилированные ядра в ней есть под Turing (sm_75), Ampere
// (sm_86), Ada (sm_89) и Blackwell (sm_120) -- по всем пятнадцати fatbin'ам,
// без пропусков. Значит отказ -- это политика, а не отсутствие кода.
//
// Архитектуру библиотека узнаёт через nvapi: грузит nvapi64.dll, берёт её
// единственный экспорт nvapi_QueryInterface и запрашивает по id
// NvAPI_GPU_GetArchInfo. Подменять nvapi64.dll своей копией рядом с воркером
// бесполезно: драйвер D3D12 успевает загрузить настоящую из System32 раньше,
// и имя оказывается занято (проверено по списку модулей процесса). Поэтому
// патчим прямо в памяти своего процесса.
//
// Патч ставится с сохранением байтов: перед вызовом настоящей функции пролог
// восстанавливается, после -- возвращается на место. Так не нужен трамплин
// (для него пришлось бы разбирать длины инструкций), и любой хендл GPU
// обслуживается настоящим кодом, а не нашими догадками.
//
// Ничего из файлов NVIDIA не меняется: правка живёт только в памяти.
// Включена по умолчанию (NS_ARCH_SPOOF=0 — выключить); на Blackwell не
// делается вообще ничего.
// ---------------------------------------------------------------------------
static constexpr unsigned NVAPI_ID_INITIALIZE = 0x0150E828u;
static constexpr unsigned NVAPI_ID_ENUM_GPUS  = 0xE5AC921Fu;
static constexpr unsigned NVAPI_ID_GET_ARCH   = 0xD8265D24u;

static constexpr unsigned NV_ARCH_TURING    = 0x170u;
static constexpr unsigned NV_ARCH_AMPERE    = 0x180u;
static constexpr unsigned NV_ARCH_ADA       = 0x190u;
static constexpr unsigned NV_ARCH_BLACKWELL = 0x1B0u;

struct NvArchInfo
{
    unsigned version, architecture, implementation, revision;
};

using PFN_NvQueryInterface = void *(__cdecl *)(unsigned);
using PFN_NvGetArchInfo = int (__cdecl *)(void *, NvArchInfo *);

static PFN_NvGetArchInfo g_arch_real;
static BYTE              g_arch_saved[12];
static bool              g_arch_patched;
static CRITICAL_SECTION  g_arch_lock;
static bool              g_arch_lock_ready;
static unsigned          g_arch_real_group;   // что было на самом деле

static int __cdecl ArchInfoHook(void *gpu, NvArchInfo *info);

static bool WriteCode(void *at, const void *src, size_t bytes)
{
    DWORD old = 0;
    if (!VirtualProtect(at, bytes, PAGE_EXECUTE_READWRITE, &old)) return false;
    memcpy(at, src, bytes);
    DWORD tmp = 0;
    VirtualProtect(at, bytes, old, &tmp);
    FlushInstructionCache(GetCurrentProcess(), at, bytes);
    return true;
}

// mov rax, imm64 ; jmp rax
static bool ArchApplyPatch()
{
    BYTE code[12] = { 0x48, 0xB8 };
    void *dst = reinterpret_cast<void *>(&ArchInfoHook);
    memcpy(code + 2, &dst, sizeof(dst));
    code[10] = 0xFF; code[11] = 0xE0;
    if (!WriteCode(reinterpret_cast<void *>(g_arch_real), code, sizeof(code)))
        return false;
    g_arch_patched = true;
    return true;
}

static void ArchRemovePatch()
{
    if (WriteCode(reinterpret_cast<void *>(g_arch_real), g_arch_saved,
                  sizeof(g_arch_saved)))
        g_arch_patched = false;
}

static int __cdecl ArchInfoHook(void *gpu, NvArchInfo *info)
{
    int rc = -1;   // NVAPI_ERROR
    if (g_arch_lock_ready) EnterCriticalSection(&g_arch_lock);
    if (g_arch_real != nullptr)
    {
        ArchRemovePatch();
        rc = g_arch_real(gpu, info);
        ArchApplyPatch();
    }
    if (g_arch_lock_ready) LeaveCriticalSection(&g_arch_lock);
    if (rc != 0 || info == nullptr) return rc;
    const unsigned group = info->architecture & 0xFFFFFFF0u;
    if (group != NV_ARCH_TURING && group != NV_ARCH_AMPERE && group != NV_ARCH_ADA)
        return rc;
    // Врём согласованно: значения implementation/revision сняты с живой
    // RTX 5070 Ti, потому что проверка может смотреть не только на группу.
    info->architecture   = NV_ARCH_BLACKWELL;
    info->implementation = 0x3u;
    info->revision       = 0xA1u;
    return rc;
}

static bool ArchSpoofRequested()
{
    // Включён по умолчанию: на Blackwell хук сам себя отключает, на старых
    // картах — единственный способ получить feature 18. NS_ARCH_SPOOF=0
    // отключает явно.
    char buf[8] = {};
    const DWORD got = GetEnvironmentVariableA("NS_ARCH_SPOOF", buf, sizeof(buf));
    if (got > 0 && got < sizeof(buf) && buf[0] == '0') return false;
    return true;
}

// Возвращает: 0 — не требовалось, 1 — поставлен, -1 — не удалось.
static int SetupArchSpoof()
{
    if (!ArchSpoofRequested()) return 0;
    HMODULE nvapi = LoadLibraryW(L"nvapi64.dll");
    if (nvapi == nullptr)
    { Log("[arch] nvapi64.dll не загрузилась, err=%lu", GetLastError()); return -1; }
    auto qi = reinterpret_cast<PFN_NvQueryInterface>(
        GetProcAddress(nvapi, "nvapi_QueryInterface"));
    if (qi == nullptr) { Log("[arch] нет nvapi_QueryInterface"); return -1; }

    auto init = reinterpret_cast<int (__cdecl *)()>(qi(NVAPI_ID_INITIALIZE));
    auto enum_gpus = reinterpret_cast<int (__cdecl *)(void **, unsigned *)>(
        qi(NVAPI_ID_ENUM_GPUS));
    auto get_arch = reinterpret_cast<PFN_NvGetArchInfo>(qi(NVAPI_ID_GET_ARCH));
    if (init == nullptr || enum_gpus == nullptr || get_arch == nullptr)
    { Log("[arch] функции nvapi не разрешились по id"); return -1; }
    if (init() != 0) { Log("[arch] NvAPI_Initialize не прошёл"); return -1; }

    void *handles[64] = {};
    unsigned count = 0;
    if (enum_gpus(handles, &count) != 0 || count == 0)
    { Log("[arch] список GPU не получен"); return -1; }

    NvArchInfo info = {};
    info.version = static_cast<unsigned>(sizeof(NvArchInfo)) | (2u << 16);
    if (get_arch(handles[0], &info) != 0)
    {
        info.version = static_cast<unsigned>(sizeof(NvArchInfo)) | (1u << 16);
        if (get_arch(handles[0], &info) != 0)
        { Log("[arch] GetArchInfo не ответил"); return -1; }
    }
    g_arch_real_group = info.architecture & 0xFFFFFFF0u;
    if (g_arch_real_group >= NV_ARCH_BLACKWELL)
    {
        Log("[arch] архитектура 0x%X и так поддерживается — подмена не нужна",
            info.architecture);
        return 0;
    }

    if (!g_arch_lock_ready)
    { InitializeCriticalSection(&g_arch_lock); g_arch_lock_ready = true; }
    g_arch_real = get_arch;
    memcpy(g_arch_saved, reinterpret_cast<const void *>(get_arch),
           sizeof(g_arch_saved));
    if (!ArchApplyPatch())
    { Log("[arch] не удалось поставить патч, err=%lu", GetLastError()); return -1; }
    Log("[arch] подмена включена: 0x%X -> 0x%X (NS_ARCH_SPOOF=1)",
        info.architecture, NV_ARCH_BLACKWELL);
    return 1;
}

static bool InitDirectNr(const wchar_t *data_path)
{
    // До загрузки библиотеки NVIDIA: она спрашивает архитектуру при создании
    // feature, но пролог правим заранее, пока в процессе нет её потоков.
    SetupArchSpoof();
    g_nr_module = LoadLibraryW(L"nvngx_dlssnr.dll");
    if (!g_nr_module) { Log("[pure] LoadLibrary(nvngx_dlssnr.dll) failed %lu", GetLastError()); return false; }
    g_nr_init_ext = reinterpret_cast<PFN_NR_InitExt>(GetProcAddress(g_nr_module, "NVSDK_NGX_D3D12_Init_Ext"));
    g_nr_create = reinterpret_cast<PFN_NR_Create>(GetProcAddress(g_nr_module, "NVSDK_NGX_D3D12_CreateFeature"));
    g_nr_evaluate = reinterpret_cast<PFN_NR_Evaluate>(GetProcAddress(g_nr_module, "NVSDK_NGX_D3D12_EvaluateFeature"));
    g_nr_release = reinterpret_cast<PFN_NR_Release>(GetProcAddress(g_nr_module, "NVSDK_NGX_D3D12_ReleaseFeature"));
    if (!g_nr_init_ext || !g_nr_create || !g_nr_evaluate || !g_nr_release) return false;
    const auto r = g_nr_init_ext(0x1000000ULL, data_path, h.dev, NVSDK_NGX_Version_API, h.params);
    Log("[pure] direct DLSSNR Init_Ext -> 0x%08X (%s)", r, NgxResultName(r));
    return NVSDK_NGX_SUCCEED(r);
}

// ---------------------------------------------------------------------------
// Command submission (allocator ring), same shape as the add-on
// ---------------------------------------------------------------------------

static bool BeginCommands()
{
    const int slot = h.frame_slot;
    const UINT64 retire = h.alloc_fence[slot];
    if (retire != 0 && h.fence->GetCompletedValue() < retire)
    {
        h.fence->SetEventOnCompletion(retire, h.fence_event);
        if (WaitForSingleObject(h.fence_event, 2000) != WAIT_OBJECT_0)
        { Log("[host] GPU did not retire allocator slot %d", slot); return false; }
    }
    if (FAILED(h.alloc[slot]->Reset())) return false;
    return SUCCEEDED(h.list->Reset(h.alloc[slot], nullptr));
}

static UINT64 EndCommands()
{
    h.list->Close();
    ID3D12CommandList *lists[] = { h.list };
    h.queue->ExecuteCommandLists(1, lists);
    const UINT64 v = ++h.fence_value;
    h.queue->Signal(h.fence, v);
    h.alloc_fence[h.frame_slot] = v;
    h.frame_slot = (h.frame_slot + 1) % Host::kFrames;
    return v;
}

static bool WaitFenceValue(ID3D12Fence *f, UINT64 v, DWORD ms)
{
    if (f->GetCompletedValue() >= v) return true;
    f->SetEventOnCompletion(v, h.fence_event);
    return WaitForSingleObject(h.fence_event, ms) == WAIT_OBJECT_0;
}

static void CloseListGuarded()
{
    __try { h.list->Close(); } __except (EXCEPTION_EXECUTE_HANDLER) {}
}

static void AbortCommands()   // never execute a list NGX crashed in
{
    if (h.list == nullptr) return;
    CloseListGuarded();
    h.list->Release();
    h.list = nullptr;
    if (SUCCEEDED(h.dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, h.alloc[h.frame_slot], nullptr,
                                           __uuidof(ID3D12GraphicsCommandList), reinterpret_cast<void **>(&h.list))))
        h.list->Close();
}

static NVSDK_NGX_Result SafeCreateDLSS(NVSDK_NGX_DLSS_Create_Params *cp, DWORD *code)
{
    *code = 0;
    __try { return NGX_D3D12_CREATE_DLSS_EXT(h.list, 1, 1, &h.feature, h.params, cp); }
    __except (EXCEPTION_EXECUTE_HANDLER) { *code = GetExceptionCode(); return static_cast<NVSDK_NGX_Result>(0x7FFFFFFF); }
}

static NVSDK_NGX_Result SafeEvaluateDLSS(NVSDK_NGX_D3D12_DLSS_Eval_Params *ep, DWORD *code)
{
    *code = 0;
    __try { return NGX_D3D12_EVALUATE_DLSS_EXT(h.list, h.feature, h.params, ep); }
    __except (EXCEPTION_EXECUTE_HANDLER) { *code = GetExceptionCode(); return static_cast<NVSDK_NGX_Result>(0x7FFFFFFF); }
}

static void SafeReleaseFeature(NVSDK_NGX_Handle *f)
{
    if (f == nullptr) return;
    __try { NVSDK_NGX_D3D12_ReleaseFeature(f); }
    __except (EXCEPTION_EXECUTE_HANDLER) { Log("[host] ReleaseFeature raised 0x%08X (ignored)", GetExceptionCode()); }
}

// ---------------------------------------------------------------------------
// The disguise: hidden window + minimal D3D12 swapchain so ReShade x64 loads
// and the DLSS 5 add-on arms itself, exactly as in a real D3D12 game.
// ---------------------------------------------------------------------------

static LRESULT CALLBACK WndProc(HWND w, UINT m, WPARAM wp, LPARAM lp)
{
    if (m == WM_CLOSE) { ShowWindow(w, SW_HIDE); return 0; }   // closing only hides; the feed lives on
    return DefWindowProcW(w, m, wp, lp);
}

// --- banner: "32-bit DLSS 5 Feeder" rendered once with GDI, copied into every frame ---

static ID3D12Resource             *g_banner;
static IDXGISwapChain3            *g_swap3;
static ID3D12CommandAllocator     *g_pump_alloc;
static ID3D12GraphicsCommandList  *g_pump_list;
static ID3D12Fence                *g_pump_fence;
static UINT64                      g_pump_val;
static HANDLE                      g_pump_ev;

static bool BeginCommands();
static UINT64 EndCommands();
static bool WaitFenceValue(ID3D12Fence *f, UINT64 v, DWORD ms);

static void InitBanner()
{
    const int W = 960, H = 540;

    // 1. Render the text with GDI into a 32-bit DIB.
    BITMAPINFO bi = {};
    bi.bmiHeader.biSize        = sizeof(bi.bmiHeader);
    bi.bmiHeader.biWidth       = W;
    bi.bmiHeader.biHeight      = -H;   // top-down
    bi.bmiHeader.biPlanes      = 1;
    bi.bmiHeader.biBitCount    = 32;
    bi.bmiHeader.biCompression = BI_RGB;
    void *bits = nullptr;
    HDC dc = CreateCompatibleDC(nullptr);
    HBITMAP bmp = CreateDIBSection(dc, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
    if (dc == nullptr || bmp == nullptr || bits == nullptr) return;
    HGDIOBJ old_bmp = SelectObject(dc, bmp);

    RECT full = { 0, 0, W, H };
    HBRUSH bg = CreateSolidBrush(RGB(18, 18, 22));
    FillRect(dc, &full, bg);
    DeleteObject(bg);
    SetBkMode(dc, TRANSPARENT);

    HFONT fnt_big   = CreateFontW(64, 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE, DEFAULT_CHARSET, 0, 0,
                                  CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
    HFONT fnt_small = CreateFontW(26, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE, DEFAULT_CHARSET, 0, 0,
                                  CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
    HGDIOBJ old_font = SelectObject(dc, fnt_big);
    SetTextColor(dc, RGB(118, 185, 0));
    RECT r1 = { 0, 150, W, 240 };
    DrawTextW(dc, L"32-bit DLSS 5 Feeder", -1, &r1, DT_CENTER | DT_SINGLELINE | DT_VCENTER);
    SelectObject(dc, fnt_small);
    SetTextColor(dc, RGB(200, 200, 205));
    RECT r2 = { 0, 260, W, 300 };
    DrawTextW(dc, L"DLSS 5 neural rendering runs here for your 32-bit game.", -1, &r2,
              DT_CENTER | DT_SINGLELINE | DT_VCENTER);
    RECT r3 = { 0, 305, W, 345 };
    DrawTextW(dc, L"Press  Home  in this window to tune it  \x2022  closing only hides the window", -1, &r3,
              DT_CENTER | DT_SINGLELINE | DT_VCENTER);
    SelectObject(dc, old_font);
    DeleteObject(fnt_big);
    DeleteObject(fnt_small);
    GdiFlush();

    // 2. Upload it (BGRA -> RGBA) and keep it as a copy source.
    D3D12_HEAP_PROPERTIES up = {};
    up.Type = D3D12_HEAP_TYPE_UPLOAD;
    const UINT pitch = (W * 4 + 255) & ~255u;
    D3D12_RESOURCE_DESC bd = {};
    bd.Dimension        = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width            = static_cast<UINT64>(pitch) * H;
    bd.Height           = 1;
    bd.DepthOrArraySize = 1;
    bd.MipLevels        = 1;
    bd.SampleDesc.Count = 1;
    bd.Layout           = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12Resource *staging = nullptr;
    D3D12_HEAP_PROPERTIES def = {};
    def.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC td = {};
    td.Dimension        = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    td.Width            = W;
    td.Height           = H;
    td.DepthOrArraySize = 1;
    td.MipLevels        = 1;
    td.Format           = DXGI_FORMAT_R8G8B8A8_UNORM;
    td.SampleDesc.Count = 1;
    td.Layout           = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    if (FAILED(h.dev->CreateCommittedResource(&up, D3D12_HEAP_FLAG_NONE, &bd, D3D12_RESOURCE_STATE_GENERIC_READ,
                                              nullptr, __uuidof(ID3D12Resource), reinterpret_cast<void **>(&staging))) ||
        FAILED(h.dev->CreateCommittedResource(&def, D3D12_HEAP_FLAG_NONE, &td, D3D12_RESOURCE_STATE_COPY_DEST,
                                              nullptr, __uuidof(ID3D12Resource), reinterpret_cast<void **>(&g_banner))))
    { SelectObject(dc, old_bmp); DeleteObject(bmp); DeleteDC(dc); return; }

    BYTE *dst = nullptr;
    staging->Map(0, nullptr, reinterpret_cast<void **>(&dst));
    const BYTE *srcp = static_cast<const BYTE *>(bits);
    for (int y = 0; y < H; ++y)
        for (int x = 0; x < W; ++x)
        {
            const BYTE *p = srcp + (static_cast<size_t>(y) * W + x) * 4;   // GDI: BGRA
            BYTE *q = dst + static_cast<size_t>(y) * pitch + static_cast<size_t>(x) * 4;
            q[0] = p[2]; q[1] = p[1]; q[2] = p[0]; q[3] = 0xFF;
        }
    staging->Unmap(0, nullptr);
    SelectObject(dc, old_bmp);
    DeleteObject(bmp);
    DeleteDC(dc);

    if (BeginCommands())
    {
        D3D12_TEXTURE_COPY_LOCATION src = {}, dcl = {};
        src.pResource = staging;
        src.Type      = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        src.PlacedFootprint.Footprint.Format   = DXGI_FORMAT_R8G8B8A8_UNORM;
        src.PlacedFootprint.Footprint.Width    = W;
        src.PlacedFootprint.Footprint.Height   = H;
        src.PlacedFootprint.Footprint.Depth    = 1;
        src.PlacedFootprint.Footprint.RowPitch = pitch;
        dcl.pResource = g_banner;
        dcl.Type      = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        h.list->CopyTextureRegion(&dcl, 0, 0, 0, &src, nullptr);
        D3D12_RESOURCE_BARRIER b = {};
        b.Type                   = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        b.Transition.pResource   = g_banner;
        b.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
        b.Transition.StateAfter  = D3D12_RESOURCE_STATE_COPY_SOURCE;
        b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        h.list->ResourceBarrier(1, &b);
        const UINT64 v = EndCommands();
        WaitFenceValue(h.fence, v, 2000);
    }
    staging->Release();

    // 3. A tiny allocator/list/fence pair on the pump queue for the per-frame copy.
    h.dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator),
                                  reinterpret_cast<void **>(&g_pump_alloc));
    if (g_pump_alloc != nullptr)
        h.dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, g_pump_alloc, nullptr,
                                 __uuidof(ID3D12GraphicsCommandList), reinterpret_cast<void **>(&g_pump_list));
    if (g_pump_list != nullptr) g_pump_list->Close();
    h.dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), reinterpret_cast<void **>(&g_pump_fence));
    g_pump_ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    h.swap->QueryInterface(__uuidof(IDXGISwapChain3), reinterpret_cast<void **>(&g_swap3));
    Log("[host] banner ready");
}

typedef HRESULT (WINAPI *PFN_D3D12CreateDevice_)(IUnknown *, D3D_FEATURE_LEVEL, REFIID, void **);
typedef HRESULT (WINAPI *PFN_CreateDXGIFactory1_)(REFIID, void **);

static void PumpPresent()
{
    MSG msg;
    while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); }
    if (h.swap == nullptr) return;

    // Paint the banner into the backbuffer (ReShade's overlay composites on top at Present).
    if (g_banner != nullptr && g_pump_list != nullptr && g_swap3 != nullptr)
    {
        ID3D12Resource *bb = nullptr;
        if (SUCCEEDED(g_swap3->GetBuffer(g_swap3->GetCurrentBackBufferIndex(), __uuidof(ID3D12Resource),
                                         reinterpret_cast<void **>(&bb))) && bb != nullptr)
        {
            if (SUCCEEDED(g_pump_alloc->Reset()) && SUCCEEDED(g_pump_list->Reset(g_pump_alloc, nullptr)))
            {
                D3D12_RESOURCE_BARRIER b = {};
                b.Type                   = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
                b.Transition.pResource   = bb;
                b.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
                b.Transition.StateAfter  = D3D12_RESOURCE_STATE_COPY_DEST;
                b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
                g_pump_list->ResourceBarrier(1, &b);
                g_pump_list->CopyResource(bb, g_banner);
                b.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
                b.Transition.StateAfter  = D3D12_RESOURCE_STATE_PRESENT;
                g_pump_list->ResourceBarrier(1, &b);
                g_pump_list->Close();
                ID3D12CommandList *lists[] = { g_pump_list };
                h.pump_queue->ExecuteCommandLists(1, lists);
                h.pump_queue->Signal(g_pump_fence, ++g_pump_val);
                if (g_pump_fence->GetCompletedValue() < g_pump_val && g_pump_ev != nullptr)
                {
                    g_pump_fence->SetEventOnCompletion(g_pump_val, g_pump_ev);
                    WaitForSingleObject(g_pump_ev, 100);
                }
            }
            bb->Release();
        }
    }
    h.swap->Present(0, 0);
}

static bool InitDisguise()
{
    // Pure D3D12 setup. No window, swapchain, ReShade, RenoDX, or DLSS carrier.
    HMODULE dxgi = LoadLibraryW(L"dxgi.dll");
    HMODULE d3d12 = LoadLibraryW(L"d3d12.dll");
    auto create_device  = d3d12 ? reinterpret_cast<PFN_D3D12CreateDevice_>(GetProcAddress(d3d12, "D3D12CreateDevice")) : nullptr;
    auto create_factory = dxgi ? reinterpret_cast<PFN_CreateDXGIFactory1_>(GetProcAddress(dxgi, "CreateDXGIFactory1")) : nullptr;
    if (create_device == nullptr || create_factory == nullptr) { Log("[host] dxgi/d3d12 exports missing"); return false; }

    IDXGIFactory2 *factory = nullptr;
    HRESULT hr = create_factory(__uuidof(IDXGIFactory2), reinterpret_cast<void **>(&factory));
    if (FAILED(hr)) { Log("[host] CreateDXGIFactory1 failed 0x%08X", hr); return false; }

    // Hybrid laptops/desktops commonly expose the integrated adapter first. NGX initialization
    // then fails even when a supported RTX card is present, so explicitly select NVIDIA.
    IDXGIAdapter1 *nvidia = nullptr;
    for (UINT i = 0; ; ++i)
    {
        IDXGIAdapter1 *candidate = nullptr;
        if (factory->EnumAdapters1(i, &candidate) == DXGI_ERROR_NOT_FOUND) break;
        DXGI_ADAPTER_DESC1 desc = {};
        candidate->GetDesc1(&desc);
        Log("[host] adapter %u: %ls vendor=0x%04X", i, desc.Description, desc.VendorId);
        if (desc.VendorId == 0x10DE && !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE))
        { nvidia = candidate; break; }
        candidate->Release();
    }
    if (nvidia == nullptr) { factory->Release(); Log("[host] no NVIDIA adapter found"); return false; }

    hr = create_device(nvidia, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device),
                       reinterpret_cast<void **>(&h.dev));
    nvidia->Release();
    if (FAILED(hr)) { Log("[host] D3D12CreateDevice failed 0x%08X", hr); return false; }

    factory->Release();
    D3D12_COMMAND_QUEUE_DESC qd = {};
    h.dev->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), reinterpret_cast<void **>(&h.queue));
    if (h.queue == nullptr) { Log("[host] queue creation failed"); return false; }
    Log("[pure] standalone D3D12 device ready; no swapchain or carrier modules");

    // Ring + internal fence for our own submissions.
    for (int i = 0; i < Host::kFrames; ++i)
        h.dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator),
                                      reinterpret_cast<void **>(&h.alloc[i]));
    if (h.alloc[0] != nullptr)
        h.dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, h.alloc[0], nullptr,
                                 __uuidof(ID3D12GraphicsCommandList), reinterpret_cast<void **>(&h.list));
    if (h.list != nullptr) h.list->Close();
    h.dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), reinterpret_cast<void **>(&h.fence));
    h.fence_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (h.list == nullptr || h.fence == nullptr) { Log("[host] list/fence creation failed"); return false; }

    return true;
}

static bool InitNgx()
{
    wchar_t data_path[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, data_path, MAX_PATH);
    if (wchar_t *s = wcsrchr(data_path, L'\\')) *(s + 1) = L'\0';

    NVSDK_NGX_Result r = NVSDK_NGX_D3D12_Init(0x1000000ULL, data_path, h.dev, nullptr, NVSDK_NGX_Version_API);
    Log("[host] NVSDK_NGX_D3D12_Init -> 0x%08X (%s)", r, NgxResultName(r));
    if (NVSDK_NGX_FAILED(r))
    {
        r = NVSDK_NGX_D3D12_Init_with_ProjectID("a0f57b54-1daf-4934-90ae-c4035c19df04", NVSDK_NGX_ENGINE_TYPE_CUSTOM,
                                                "1.0", data_path, h.dev, nullptr, NVSDK_NGX_Version_API);
        Log("[host] Init_with_ProjectID -> 0x%08X (%s)", r, NgxResultName(r));
    }
    if (NVSDK_NGX_FAILED(r)) return false;
    h.ngx_inited = true;

    r = NVSDK_NGX_D3D12_AllocateParameters(&h.params);
    if (NVSDK_NGX_FAILED(r) || h.params == nullptr) { Log("[host] AllocateParameters failed 0x%08X", r); return false; }
    return InitDirectNr(data_path);
}

// Модель NR выбирается подсказкой при создании feature. Стояла 0 (default) и
// никогда не проверялась; у родственного Ray Reconstruction под номерами лежат
// разные трансформерные модели с разной ценой. Значение берём из NS_NR_PRESET,
// чтобы перебирать без пересборки.
static UINT NrPresetHint()
{
    static int cached = -1;
    if (cached < 0)
    {
        char buf[16] = {};
        const DWORD got = GetEnvironmentVariableA("NS_NR_PRESET", buf, sizeof(buf));
        cached = (got > 0 && got < sizeof(buf)) ? atoi(buf) : 0;
        if (cached < 0) cached = 0;
    }
    return static_cast<UINT>(cached);
}

static bool CreateFeature(UINT w, UINT h_, int flags, NVSDK_NGX_Result *out_r, UINT full_w = 0, UINT full_h = 0)
{
    (void)flags;
    // New mode: the client sends full-res frames (full_w x full_h) and the feature
    // itself downsamples to the work resolution (w x h_), runs NR, and upsamples back.
    const bool upscale = (full_w > 0 && full_h > 0 && (full_w != w || full_h != h_));
    h.params->Reset();
    h.params->Set("CreationNodeMask", 1u);
    h.params->Set("VisibilityNodeMask", 1u);
    h.params->Set("DLSSNR.Width", w); h.params->Set("DLSSNR.Height", h_);
    h.params->Set("DLSSNR.InputWidth", upscale ? full_w : w);
    h.params->Set("DLSSNR.InputHeight", upscale ? full_h : h_);
    h.params->Set("DLSSNR.OutputWidth", upscale ? full_w : w);
    h.params->Set("DLSSNR.OutputHeight", upscale ? full_h : h_);
    h.params->Set("DLSSNR.Output.Width", upscale ? full_w : w);
    h.params->Set("DLSSNR.Output.Height", upscale ? full_h : h_);
    h.params->Set("DLSSNR.Upscaling", upscale ? 1u : 0u);
    h.params->Set("DLSSNR.Scale", upscale ? static_cast<float>(w) / static_cast<float>(full_w) : 1.0f);
    h.params->Set("DLSSNR.ScalingRatio", upscale ? static_cast<float>(w) / static_cast<float>(full_w) : 1.0f);
    h.params->Set("DLSSNR.Hint.Render.Preset", NrPresetHint());
    h.params->Set("DLSS.Feature.Create.Flags", 0u);

    if (!BeginCommands()) return false;
    DWORD ccode = 0;
    NVSDK_NGX_Result rf = static_cast<NVSDK_NGX_Result>(0x7FFFFFFF);
    __try { rf = g_nr_create(h.list, NVSDK_NGX_Feature_Reserved18, h.params, &h.feature); }
    __except (EXCEPTION_EXECUTE_HANDLER) { ccode = GetExceptionCode(); }
    g_create_result = static_cast<uint32_t>(rf);
    if (out_r != nullptr) *out_r = rf;
    if (ccode != 0)
    {
        AbortCommands();
        // NGX may have partially written *OutHandle before the fault; never trust it.
        h.feature = nullptr;
        Log("[host] CreateFeature raised 0x%08X (caught; nothing submitted)", ccode);
        return false;
    }
    const UINT64 v = EndCommands();
    if (!WaitFenceValue(h.fence, v, 30000)) { Log("[pure] feature create did not complete"); return false; }
    if (NVSDK_NGX_FAILED(rf) || h.feature == nullptr)
    { Log("[pure] direct feature 18 create failed 0x%08X (%s)", rf, NgxResultName(rf)); h.feature = nullptr; return false; }
    Log("[pure] direct feature 18 ready: %ux%u%s preset=%u result=0x%08X", w, h_,
        upscale ? " (upscaling full->work->full)" : "", NrPresetHint(), rf);
    return true;
}

// A crashed CreateFeature can leave NGX's own internal state broken (seen in BioShock
// Remastered: the add-on faulted once during a resolution/HDR change, and every following
// create failed too, with the SEH catching a different exception each time -- NGX was
// never going to recover on its own). Reset NGX itself as a last resort so the feed can
// come back without the user having to restart the game.
static bool ReinitNgx()
{
    Log("[host] NGX looks corrupted after repeated failures; reinitializing");
    if (h.params != nullptr) { NVSDK_NGX_D3D12_DestroyParameters(h.params); h.params = nullptr; }
    if (h.ngx_inited) { NVSDK_NGX_D3D12_Shutdown1(h.dev); h.ngx_inited = false; }
    h.feature = nullptr;
    return InitNgx();
}

static bool Evaluate(ID3D12Resource *color, ID3D12Resource *output, ID3D12Resource *depth, ID3D12Resource *mv,
                     UINT w, UINT h_, int reset, float mvsx, float mvsy)
{
    if (!BeginCommands()) return false;

    NVSDK_NGX_D3D12_DLSS_Eval_Params ep = {};
    ep.Feature.pInColor  = color;
    ep.Feature.pInOutput = output;
    ep.pInDepth          = depth;
    ep.pInMotionVectors  = mv;
    ep.InRenderSubrectDimensions.Width  = w;
    ep.InRenderSubrectDimensions.Height = h_;
    ep.InReset           = reset;
    ep.InMVScaleX        = mvsx;
    ep.InMVScaleY        = mvsy;
    ep.InPreExposure     = 1.0f;
    ep.InExposureScale   = 1.0f;

    DWORD ecode = 0;
    NVSDK_NGX_Result re = SafeEvaluateDLSS(&ep, &ecode);
    if (ecode != 0) { AbortCommands(); Log("[host] evaluate raised 0x%08X (caught; nothing submitted)", ecode); return false; }
    EndCommands();
    if (NVSDK_NGX_FAILED(re)) { Log("[host] evaluate failed 0x%08X (%s)", re, NgxResultName(re)); return false; }
    return true;
}

// ---------------------------------------------------------------------------
// --test: prove the whole stack with no game attached
// ---------------------------------------------------------------------------

static ID3D12Resource *MakeTex(UINT w, UINT h_, DXGI_FORMAT fmt, bool uav)
{
    D3D12_HEAP_PROPERTIES hp = {};
    hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC rd = {};
    rd.Dimension        = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    rd.Width            = w;
    rd.Height           = h_;
    rd.DepthOrArraySize = 1;
    rd.MipLevels        = 1;
    rd.Format           = fmt;
    rd.SampleDesc.Count = 1;
    rd.Layout           = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    rd.Flags            = uav ? D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS : D3D12_RESOURCE_FLAG_NONE;
    ID3D12Resource *t = nullptr;
    h.dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, D3D12_RESOURCE_STATE_COMMON, nullptr,
                                   __uuidof(ID3D12Resource), reinterpret_cast<void **>(&t));
    return t;
}

static int RunTest()
{
    const UINT W = 640, H = 360;
    Log("[host] --test: %ux%u synthetic DLAA", W, H);

    ID3D12Resource *color  = MakeTex(W, H, DXGI_FORMAT_R8G8B8A8_UNORM, false);
    ID3D12Resource *output = MakeTex(W, H, DXGI_FORMAT_R8G8B8A8_UNORM, true);
    ID3D12Resource *depth  = MakeTex(W, H, DXGI_FORMAT_R32_FLOAT, false);
    ID3D12Resource *mv     = MakeTex(W, H, DXGI_FORMAT_R16G16_FLOAT, false);
    if (!color || !output || !depth || !mv) { Log("[host] test texture creation failed"); return 1; }

    // Give the DLSS 5 add-on its hook-arming time, with the swapchain pumping.
    for (int i = 0; i < 120; ++i) { PumpPresent(); Sleep(8); }

    int flags = NVSDK_NGX_DLSS_Feature_Flags_MVLowRes | NVSDK_NGX_DLSS_Feature_Flags_AutoExposure |
                NVSDK_NGX_DLSS_Feature_Flags_DepthInverted;
    NVSDK_NGX_Result rf = NVSDK_NGX_Result_Fail;
    if (!CreateFeature(W, H, flags, &rf)) return 1;

    int good = 0;
    for (int i = 0; i < 300; ++i)
    {
        PumpPresent();
        if (Evaluate(color, output, depth, mv, W, H, i == 0 ? 1 : 0, 1.0f, 1.0f)) ++good;
        else break;
        if (i == 180)   // the warm-up re-create, same medicine as in-game
        {
            Log("[host] warm-up: re-creating the feature once");
            NVSDK_NGX_Handle *old = h.feature;
            h.feature = nullptr;
            if (!CreateFeature(W, H, flags, &rf)) { h.feature = old; Log("[host] keeping the previous feature"); }
            else SafeReleaseFeature(old);
        }
    }
    Log("[host] --test finished: %d/300 evaluates succeeded", good);
    Log("[host] check the host's ReShade.log for 'feature 18 created' / 'evaluation succeeded'");
    return good >= 250 ? 0 : 1;
}

// ---------------------------------------------------------------------------
// --video: portable video-frame protocol for the Gradio front end
// ---------------------------------------------------------------------------

static constexpr uint32_t VIDEO_MAGIC     = 0x32563544u; // "D5V2" -- legacy 56-byte header
static constexpr uint32_t VIDEO_MAGIC_EXT = 0x33563544u; // "D5V3" -- 64-byte header with full_w/full_h
static constexpr uint32_t FRAME_MAGIC = 0x314D5246u; // "FRM1"
static constexpr uint32_t OUT_MAGIC   = 0x3154554Fu; // "OUT1"
static constexpr uint32_t RESIZE_MAGIC    = 0x5A534E52u; // "RNSZ" -- reconfigure on the fly (work size + params)
static constexpr uint32_t RESIZE_ACK_MAGIC = 0x4B434152u; // "RACK" -- worker -> client reply to RNSZ
static constexpr uint32_t SHM_MAGIC     = 0x494D4853u; // "SHMI" -- client -> worker: frame payload lives in shared memory
static constexpr uint32_t SHM_ACK_MAGIC = 0x4B434153u; // "SACK" -- worker -> client reply to SHMI
static constexpr uint32_t WINDOW_MAGIC     = 0x4F444E57u; // "WNDO" -- client -> worker: present results yourself
static constexpr uint32_t WINDOW_ACK_MAGIC = 0x4B434157u; // "WACK" -- worker -> client reply to WNDO
static constexpr uint32_t MOTION_MAGIC     = 0x53544F4Du; // "MOTS" -- client -> worker: motion arrives at this reduced size
static constexpr uint32_t MOTION_ACK_MAGIC = 0x4B43414Du; // "MACK" -- worker -> client reply to MOTS
static constexpr uint32_t DDA_MAGIC        = 0x31414444u; // "DDA1" -- client -> worker: worker takes over capture
static constexpr uint32_t DDA_ACK_MAGIC    = 0x4B434144u; // "DACK" -- worker -> client reply to DDA1
static constexpr uint32_t GRAY_MAGIC       = 0x59415247u; // "GRAY" -- client -> worker: gray goes back into this mapping
static constexpr uint32_t GRAY_ACK_MAGIC   = 0x4B434147u; // "GAK"  -- worker -> client reply to GRAY
// WNDO flags
static constexpr uint32_t WINDOW_FLAG_CAPTURABLE = 0x1u; // debug: do NOT hide the window from screen capture
static constexpr uint32_t WINDOW_FLAG_DISABLE    = 0x2u; // tear the window down, go back to sending pixels
// VideoFrameHeader.reserved bit 0: payload is in the mapping, not in the pipe.
static constexpr uint32_t FRAME_FLAG_SHM = 0x1u;
// bit 1: even while presenting into the overlay, send the pixels back for
// this one frame (the client needs them for a screenshot).
static constexpr uint32_t FRAME_FLAG_WANT_PIXELS = 0x2u;
// bit 2: the motion payload of this frame is at the reduced size agreed by
// MOTS; the worker upscales it to the work resolution on the GPU.
static constexpr uint32_t FRAME_FLAG_MOTION_SMALL = 0x4u;
// bit 3: DDA capture is active — this frame carries NO colour payload.
// The worker takes the colour from Desktop Duplication itself and reads
// only the motion block from the pipe.
static constexpr uint32_t FRAME_FLAG_NO_COLOR = 0x8u;
// bit 4: NR OFF — skip the NGX evaluate and present the raw captured
// colour instead. Keeps the overlay alive (picture + HUD) while the
// neural pass is disabled; the next non-bypass frame resumes NGX.
static constexpr uint32_t FRAME_FLAG_BYPASS = 0x10u;
// bit 5: показать кадр шторкой «до/после» — слева сырой захват, справа
// результат NGX. Позиция шторки лежит в старших 16 битах reserved
// (0..65535 -> 0..1 ширины кадра): отдельного поля в заголовке нет, а менять
// его размер ради одного числа значит ломать протокол на обеих сторонах.
static constexpr uint32_t FRAME_FLAG_SPLIT = 0x20u;

static UINT SplitXFromFlags(uint32_t reserved, UINT width)
{
    const uint32_t frac = (reserved >> 16) & 0xFFFFu;
    return static_cast<UINT>((static_cast<uint64_t>(width) * frac) / 0xFFFFu);
}
static constexpr size_t   VIDEO_HEADER_LEGACY_SIZE = 56; // magic..skin_structure (no full_w/full_h)

#pragma pack(push, 1)
struct VideoHeader
{
    uint32_t magic, width, height, warmup, frame_count, profile, preset, style, auto_mask, ui_correction;
    float intensity, local_tone, local_structure, skin_structure;
    uint32_t full_w, full_h;   // full-res input/output frame size (new mode); 0 = legacy 1:1
};
struct VideoFrameHeader
{
    uint32_t magic, index, reset, reserved;
    int64_t pts;
};
struct VideoResultHeader
{
    uint32_t magic, index, ok, bytes, ngx_result;
    int64_t pts;
};
// RNSZ: client -> worker, between frames. Same field layout as the D5V3
// header (magic..skin_structure + full_w/full_h) so the client can reuse
// its header builder. The worker tears down the feature/textures, creates
// new ones at the new work size and replies with a fixed-size RACK.
struct VideoResizeCmd
{
    uint32_t magic, width, height, warmup, frame_count, profile, preset, style, auto_mask, ui_correction;
    float intensity, local_tone, local_structure, skin_structure;
    uint32_t full_w, full_h;
};
struct VideoResizeAck
{
    uint32_t magic, ok, ngx_result, reserved;
    int64_t pts;
};
// SHMI: client -> worker, once per worker lifetime (right after the stream
// header). Hands over the name of a named section that holds the input frame.
// Layout inside the mapping is fixed and does NOT depend on work_scale:
//     [0 .. color_bytes)                  RGBA8, full-res (capacity, not the
//                                         per-frame size)
//     [color_bytes .. +motion_bytes)      motion float16, work-res
// so RNSZ never needs to renegotiate. The first 24 bytes share the layout of
// VideoFrameHeader, which is how ReadVideoMessage can peek the magic first.
struct VideoShmCmd
{
    uint32_t magic, color_bytes, motion_bytes, flags;
    int64_t pts;
    char name[64];
};
struct VideoShmAck
{
    uint32_t magic, ok, reserved0, reserved1;
    int64_t pts;
};
// WNDO: client -> worker. "Open a borderless click-through overlay of this size
// and show the NGX result in it yourself." Same 24-byte shape as
// VideoFrameHeader, so no extra read is needed. While the window is up, OUT1
// carries bytes=0: the pixels never come back to the client at all.
struct VideoWindowCmd
{
    uint32_t magic, width, height, flags;
    int64_t pts;
};
struct VideoWindowAck
{
    uint32_t magic, ok, reserved0, reserved1;
    int64_t pts;
};
// MOTS: client -> worker. "From now on the motion field arrives at this size;
// stretch it to the work resolution yourself." Sending 0x0 turns it off and
// motion goes back to arriving at the work resolution. Same 24-byte shape as
// VideoFrameHeader.
struct VideoMotionCmd
{
    uint32_t magic, width, height, flags;
    int64_t pts;
};
struct VideoMotionAck
{
    uint32_t magic, ok, reserved0, reserved1;
    int64_t pts;
};
// DDA1: client -> worker. "Take over capture from the desktop yourself."
// width/height = required capture size (usually the full output size);
// flags: bit0 WANT pixels back (screenshot), bit1 = present overlay stays on.
// The worker opens Desktop Duplication (D3D11) and feeds the NGX pipeline
// straight from GPU textures; the client no longer sends FRM1 frames while
// active. Sending DDA1 with width=0 stops capture and reverts to pipe frames.
struct VideoDdaCmd
{
    uint32_t magic, width, height, flags;
    int64_t pts;
};
struct VideoDdaAck
{
    uint32_t magic, ok, reserved0, reserved1;
    int64_t pts;
};
// GRAY: client -> worker. "Downsample the captured colour to luminance
// (width x height, typically 320x180 = flow size) and write it into this
// named mapping — the client needs it for the optical-flow guides. The
// DDA capture must be active; the block is computed after the swizzle."
// Layout: width*height bytes, R8 (single channel), client-owned mapping.
struct VideoGrayCmd
{
    uint32_t magic, width, height, flags;
    int64_t pts;
    char name[64];
};
struct VideoGrayAck
{
    uint32_t magic, ok, reserved0, reserved1;
    int64_t pts;
};
#pragma pack(pop)

struct VideoTex
{
    ID3D12Resource *tex = nullptr;
    ID3D12Resource *upload = nullptr;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT fp = {};
    UINT rows = 0;
    UINT64 row_size = 0;
};

struct VideoState
{
    UINT w = 0, hgt = 0;          // work resolution (NGX feature resolution)
    UINT full_w = 0, full_h = 0;  // full-res frame size (0 = legacy 1:1 mode)
    bool upscale = false;         // full_w > 0 && full_w != w: feature does full->work->full
    VideoTex color, depth, mv, mask;
    ID3D12Resource *output = nullptr;
    ID3D12Resource *readback = nullptr;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT out_fp = {};
    UINT out_rows = 0;
    UINT64 out_row_size = 0;
    bool inputs_ready = false;
};

static VideoHeader g_video_options = {};
static uint32_t g_last_eval_result = 0;
static bool g_live_force = false;   // --live: treat the stream as unbounded even with frame_count > 0

// Shared input frame (SHMI). Read-only view of the client's named section:
// the frame is uploaded to the GPU straight from here, so the payload never
// travels through the pipe and is never copied into a std::vector.
static HANDLE g_shm_handle = nullptr;
static const BYTE *g_shm_base = nullptr;
static size_t g_shm_bytes = 0;         // mapped length
static size_t g_shm_motion_off = 0;    // offset of the motion block (= colour capacity)

static void CloseSharedInput()
{
    if (g_shm_base != nullptr) { UnmapViewOfFile(g_shm_base); g_shm_base = nullptr; }
    if (g_shm_handle != nullptr) { CloseHandle(g_shm_handle); g_shm_handle = nullptr; }
    g_shm_bytes = 0;
    g_shm_motion_off = 0;
}

// ---------------------------------------------------------------------------
// Presentation window: the worker shows the NGX result itself, so the frame
// never goes back to the client. Kills the GPU->CPU readback, the return pipe
// and the client-side blit in one move.
// ---------------------------------------------------------------------------
#ifndef WDA_EXCLUDEFROMCAPTURE
#define WDA_EXCLUDEFROMCAPTURE 0x00000011
#endif

// Определена ниже, рядом с UploadVideoFrame.
static D3D12_RESOURCE_BARRIER Transition(ID3D12Resource *r, D3D12_RESOURCE_STATES before,
                                         D3D12_RESOURCE_STATES after);

static HWND                       g_present_hwnd;
static IDXGISwapChain3           *g_present_swap;
static HANDLE                     g_present_thread;
static DWORD                      g_present_tid;
static UINT                       g_present_w, g_present_h;
static bool                       g_present_capturable;  // debug flag from WNDO
static volatile LONG              g_present_state;       // 0 = pending, 1 = window up, -1 = failed

static LRESULT CALLBACK PresentWndProc(HWND w, UINT m, WPARAM wp, LPARAM lp)
{
    switch (m)
    {
    // Clicks and hover fall through to whatever sits underneath. This replaces
    // WS_EX_TRANSPARENT: that style only makes a window click-through when it is
    // also WS_EX_LAYERED, and a layered window cannot host a flip-model
    // swapchain (CreateSwapChainForHwnd fails with DXGI_ERROR_INVALID_CALL).
    case WM_NCHITTEST:     return HTTRANSPARENT;
    case WM_MOUSEACTIVATE: return MA_NOACTIVATE;
    case WM_ERASEBKGND:    return 1;
    case WM_CLOSE:         ShowWindow(w, SW_HIDE); return 0;
    default: break;
    }
    return DefWindowProcW(w, m, wp, lp);
}

// The window lives on its own thread with its own message loop: the main thread
// spends its life blocked in ReadExact on stdin and would never pump messages,
// which Windows reports as a hung window after a few seconds.
static DWORD WINAPI PresentWindowThread(LPVOID)
{
    WNDCLASSEXW wc = {};
    wc.cbSize        = sizeof(wc);
    wc.lpfnWndProc   = PresentWndProc;
    wc.hInstance     = GetModuleHandleW(nullptr);
    wc.lpszClassName = L"NeuralScreenPresent";
    RegisterClassExW(&wc);   // duplicate registration is harmless: it just fails

    // WS_EX_TRANSPARENT в дополнение к HTTRANSPARENT: одного хит-теста
    // системе не хватило — клик всё равно доставался оверлею (замерено
    // инжекцией клика, _work/test_present_clickthrough.py).
    g_present_hwnd = CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT,
        wc.lpszClassName, L"NeuralScreen", WS_POPUP,
        0, 0, static_cast<int>(g_present_w), static_cast<int>(g_present_h),
        nullptr, nullptr, wc.hInstance, nullptr);
    if (g_present_hwnd == nullptr)
    {
        Log("[present] CreateWindowEx failed, err=%lu", GetLastError());
        InterlockedExchange(&g_present_state, -1);
        return 0;
    }
    if (!g_present_capturable)
    {
        // Without this Desktop Duplication captures our own output and the
        // pipeline feeds on itself.
        if (!SetWindowDisplayAffinity(g_present_hwnd, WDA_EXCLUDEFROMCAPTURE))
            Log("[present] SetWindowDisplayAffinity failed, err=%lu", GetLastError());
    }
    else
        Log("[present] capturable window (debug): NOT hidden from screen capture");

    ShowWindow(g_present_hwnd, SW_SHOWNOACTIVATE);
    SetWindowPos(g_present_hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                 SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
    InterlockedExchange(&g_present_state, 1);

    MSG msg;
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) { TranslateMessage(&msg); DispatchMessageW(&msg); }
    return 0;
}

static void ClosePresent()
{
    if (g_present_swap != nullptr) { g_present_swap->Release(); g_present_swap = nullptr; }
    if (g_present_tid != 0) PostThreadMessageW(g_present_tid, WM_QUIT, 0, 0);
    if (g_present_hwnd != nullptr) { PostMessageW(g_present_hwnd, WM_CLOSE, 0, 0); }
    if (g_present_thread != nullptr)
    {
        WaitForSingleObject(g_present_thread, 2000);
        CloseHandle(g_present_thread);
        g_present_thread = nullptr;
    }
    if (g_present_hwnd != nullptr) { DestroyWindow(g_present_hwnd); g_present_hwnd = nullptr; }
    g_present_tid = 0;
    g_present_w = g_present_h = 0;
    g_present_state = 0;
}

static bool OpenPresent(UINT width, UINT height, uint32_t flags)
{
    ClosePresent();
    if (width < 64 || height < 64 || width > 7680 || height > 4320)
    {
        Log("[present] refused: implausible window size %ux%u", width, height);
        return false;
    }
    g_present_w = width;
    g_present_h = height;
    g_present_capturable = (flags & WINDOW_FLAG_CAPTURABLE) != 0;
    g_present_state = 0;
    g_present_thread = CreateThread(nullptr, 0, PresentWindowThread, nullptr, 0, &g_present_tid);
    if (g_present_thread == nullptr) { Log("[present] CreateThread failed"); return false; }
    for (int i = 0; i < 500 && g_present_state == 0; ++i) Sleep(4);   // до 2 c на создание окна
    if (g_present_state != 1) { Log("[present] window did not come up"); ClosePresent(); return false; }

    HMODULE dxgi = LoadLibraryW(L"dxgi.dll");
    auto create_factory = dxgi ? reinterpret_cast<PFN_CreateDXGIFactory1_>(
                                     GetProcAddress(dxgi, "CreateDXGIFactory1")) : nullptr;
    if (create_factory == nullptr) { Log("[present] CreateDXGIFactory1 missing"); ClosePresent(); return false; }
    IDXGIFactory2 *factory = nullptr;
    HRESULT hr = create_factory(__uuidof(IDXGIFactory2), reinterpret_cast<void **>(&factory));
    if (FAILED(hr)) { Log("[present] CreateDXGIFactory1 failed 0x%08X", hr); ClosePresent(); return false; }

    DXGI_SWAP_CHAIN_DESC1 sd = {};
    sd.Width       = width;
    sd.Height      = height;
    sd.Format      = DXGI_FORMAT_R8G8B8A8_UNORM;   // must match VideoState::output for CopyResource
    sd.SampleDesc.Count = 1;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.BufferCount = 2;
    sd.SwapEffect  = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    sd.AlphaMode   = DXGI_ALPHA_MODE_IGNORE;
    IDXGISwapChain1 *sc1 = nullptr;
    // The swapchain must be created on the very queue that will write the back
    // buffer, which is the same queue NGX submits on.
    hr = factory->CreateSwapChainForHwnd(h.queue, g_present_hwnd, &sd, nullptr, nullptr, &sc1);
    if (FAILED(hr) || sc1 == nullptr)
    {
        Log("[present] CreateSwapChainForHwnd failed 0x%08X", hr);
        factory->Release(); ClosePresent(); return false;
    }
    factory->MakeWindowAssociation(g_present_hwnd, DXGI_MWA_NO_ALT_ENTER | DXGI_MWA_NO_WINDOW_CHANGES);
    factory->Release();
    hr = sc1->QueryInterface(__uuidof(IDXGISwapChain3), reinterpret_cast<void **>(&g_present_swap));
    sc1->Release();
    if (FAILED(hr) || g_present_swap == nullptr)
    {
        Log("[present] IDXGISwapChain3 unavailable 0x%08X", hr);
        ClosePresent(); return false;
    }
    // Click-through. Порядок повторяет рабочий рецепт из display.py:
    // стиль -> SetLayeredWindowAttributes -> SWP_FRAMECHANGED. Ни HTTRANSPARENT,
    // ни WS_EX_TRANSPARENT по отдельности не пропускают клик (замерено
    // инжекцией, _work/test_present_clickthrough.py) — нужен WS_EX_LAYERED.
    // Стили ставятся ПОСЛЕ создания swapchain: CreateSwapChainForHwnd отказывает
    // на layered-окне, а уже созданная цепочка продолжает работать.
    const LONG ex = GetWindowLongW(g_present_hwnd, GWL_EXSTYLE);
    SetWindowLongW(g_present_hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT);
    if (!SetLayeredWindowAttributes(g_present_hwnd, 0, 255, LWA_ALPHA))
        Log("[present] SetLayeredWindowAttributes failed, err=%lu", GetLastError());
    SetWindowPos(g_present_hwnd, nullptr, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED);

    Log("[present] overlay %ux%u ready (flip-discard, layered click-through%s)",
        width, height, g_present_capturable ? ", CAPTURABLE" : ", hidden from capture");
    return true;
}

// True when the result can go straight to the overlay instead of the client.
static bool PresentModeActive(const VideoState &v)
{
    if (g_present_swap == nullptr) return false;
    const UINT ow = v.upscale ? v.full_w : v.w;
    const UINT oh = v.upscale ? v.full_h : v.hgt;
    if (ow != g_present_w || oh != g_present_h)
    {
        static bool warned = false;
        if (!warned)
        {
            warned = true;
            Log("[present] output %ux%u does not match overlay %ux%u -- falling back to "
                "sending pixels to the client", ow, oh, g_present_w, g_present_h);
        }
        return false;
    }
    return true;
}

static bool PresentFrame(VideoState &v)
{
    ID3D12Resource *bb = nullptr;
    if (FAILED(g_present_swap->GetBuffer(g_present_swap->GetCurrentBackBufferIndex(),
                                         __uuidof(ID3D12Resource),
                                         reinterpret_cast<void **>(&bb))) || bb == nullptr)
    { Log("[present] GetBuffer failed"); return false; }

    bool ok = false;
    if (BeginCommands())
    {
        D3D12_RESOURCE_BARRIER pre[] = {
            Transition(bb, D3D12_RESOURCE_STATE_PRESENT, D3D12_RESOURCE_STATE_COPY_DEST),
            Transition(v.output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE),
        };
        h.list->ResourceBarrier(_countof(pre), pre);
        h.list->CopyResource(bb, v.output);
        D3D12_RESOURCE_BARRIER post[] = {
            Transition(bb, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_PRESENT),
            Transition(v.output, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
        };
        h.list->ResourceBarrier(_countof(post), post);
        const UINT64 fv = EndCommands();
        if (WaitFenceValue(h.fence, fv, 2000))
            ok = SUCCEEDED(g_present_swap->Present(0, 0));
        else
            Log("[present] fence wait timed out");
    }
    bb->Release();
    return ok;
}

// NR OFF (FRAME_FLAG_BYPASS): показать сырой захват (v.color) вместо NGX-результата.
// v.color — full-res в upscale-режиме и work-res в 1:1 — совпадает с окном,
// которое PresentModeActive уже проверил по output-размеру.
static bool PresentBypass(VideoState &v)
{
    ID3D12Resource *bb = nullptr;
    if (FAILED(g_present_swap->GetBuffer(g_present_swap->GetCurrentBackBufferIndex(),
                                         __uuidof(ID3D12Resource),
                                         reinterpret_cast<void **>(&bb))) || bb == nullptr)
    { Log("[present] bypass GetBuffer failed"); return false; }

    bool ok = false;
    if (BeginCommands())
    {
        D3D12_RESOURCE_BARRIER pre[] = {
            Transition(bb, D3D12_RESOURCE_STATE_PRESENT, D3D12_RESOURCE_STATE_COPY_DEST),
            Transition(v.color.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                       D3D12_RESOURCE_STATE_COPY_SOURCE),
        };
        h.list->ResourceBarrier(_countof(pre), pre);
        h.list->CopyResource(bb, v.color.tex);
        D3D12_RESOURCE_BARRIER post[] = {
            Transition(bb, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_PRESENT),
            Transition(v.color.tex, D3D12_RESOURCE_STATE_COPY_SOURCE,
                       D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        };
        h.list->ResourceBarrier(_countof(post), post);
        const UINT64 fv = EndCommands();
        if (WaitFenceValue(h.fence, fv, 2000))
            ok = SUCCEEDED(g_present_swap->Present(0, 0));
        else
            Log("[present] bypass fence wait timed out");
    }
    bb->Release();
    return ok;
}

static bool ReadExact(FILE *f, void *p, size_t n)
{
    BYTE *dst = static_cast<BYTE *>(p);
    while (n != 0)
    {
        const size_t got = fread(dst, 1, n, f);
        if (got == 0) return false;
        dst += got; n -= got;
    }
    return true;
}

static bool WriteExact(FILE *f, const void *p, size_t n)
{
    const BYTE *src = static_cast<const BYTE *>(p);
    while (n != 0)
    {
        const size_t put = fwrite(src, 1, n, f);
        if (put == 0) return false;
        src += put; n -= put;
    }
    fflush(f);
    return true;
}

static bool CreateVideoTex(VideoTex &v, UINT w, UINT hgt, DXGI_FORMAT fmt, UINT packed_pitch,
                           D3D12_RESOURCE_FLAGS res_flags = D3D12_RESOURCE_FLAG_NONE)
{
    D3D12_HEAP_PROPERTIES def = {};
    def.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC td = {};
    td.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    td.Width = w; td.Height = hgt; td.DepthOrArraySize = 1; td.MipLevels = 1;
    td.Format = fmt; td.SampleDesc.Count = 1; td.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    td.Flags = res_flags;
    if (FAILED(h.dev->CreateCommittedResource(&def, D3D12_HEAP_FLAG_NONE, &td, D3D12_RESOURCE_STATE_COPY_DEST,
        nullptr, __uuidof(ID3D12Resource), reinterpret_cast<void **>(&v.tex)))) return false;

    UINT64 total = 0;
    h.dev->GetCopyableFootprints(&td, 0, 1, 0, &v.fp, &v.rows, &v.row_size, &total);
    if (v.rows != hgt || v.row_size < packed_pitch) return false;
    D3D12_HEAP_PROPERTIES up = {};
    up.Type = D3D12_HEAP_TYPE_UPLOAD;
    D3D12_RESOURCE_DESC bd = {};
    bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = total; bd.Height = 1; bd.DepthOrArraySize = 1; bd.MipLevels = 1;
    bd.SampleDesc.Count = 1; bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    return SUCCEEDED(h.dev->CreateCommittedResource(&up, D3D12_HEAP_FLAG_NONE, &bd,
        D3D12_RESOURCE_STATE_GENERIC_READ, nullptr, __uuidof(ID3D12Resource),
        reinterpret_cast<void **>(&v.upload)));
}

static bool CreateVideoResources(VideoState &v, UINT w, UINT hgt, UINT full_w = 0, UINT full_h = 0)
{
    v.w = w; v.hgt = hgt;
    v.full_w = full_w; v.full_h = full_h;
    v.upscale = (full_w > 0 && full_h > 0 && (full_w != w || full_h != hgt));
    const UINT cw = v.upscale ? full_w : w;   // color texture: full-res in upscale mode
    const UINT ch = v.upscale ? full_h : hgt;
    if (!CreateVideoTex(v.color, cw, ch, DXGI_FORMAT_R8G8B8A8_UNORM, cw * 4) ||
        // ALLOW_UNORDERED_ACCESS: в неё пишет шейдер растяжения поля движения
        !CreateVideoTex(v.mv, w, hgt, DXGI_FORMAT_R16G16_FLOAT, w * 4,
                        D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS))
        return false;

    D3D12_HEAP_PROPERTIES def = {};
    def.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC td = {};
    td.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    td.Width = cw; td.Height = ch; td.DepthOrArraySize = 1; td.MipLevels = 1;
    td.Format = DXGI_FORMAT_R8G8B8A8_UNORM; td.SampleDesc.Count = 1;
    td.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    td.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    if (FAILED(h.dev->CreateCommittedResource(&def, D3D12_HEAP_FLAG_NONE, &td,
        D3D12_RESOURCE_STATE_UNORDERED_ACCESS, nullptr, __uuidof(ID3D12Resource),
        reinterpret_cast<void **>(&v.output)))) return false;

    UINT64 total = 0;
    h.dev->GetCopyableFootprints(&td, 0, 1, 0, &v.out_fp, &v.out_rows, &v.out_row_size, &total);
    D3D12_HEAP_PROPERTIES rb = {};
    rb.Type = D3D12_HEAP_TYPE_READBACK;
    D3D12_RESOURCE_DESC bd = {};
    bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = total; bd.Height = 1; bd.DepthOrArraySize = 1; bd.MipLevels = 1;
    bd.SampleDesc.Count = 1; bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    return SUCCEEDED(h.dev->CreateCommittedResource(&rb, D3D12_HEAP_FLAG_NONE, &bd,
        D3D12_RESOURCE_STATE_COPY_DEST, nullptr, __uuidof(ID3D12Resource),
        reinterpret_cast<void **>(&v.readback)));
}

static bool FillUpload(VideoTex &v, const BYTE *src, UINT src_pitch, UINT hgt)
{
    BYTE *dst = nullptr;
    if (FAILED(v.upload->Map(0, nullptr, reinterpret_cast<void **>(&dst)))) return false;
    for (UINT y = 0; y < hgt; ++y)
        memcpy(dst + static_cast<size_t>(y) * v.fp.Footprint.RowPitch,
               src + static_cast<size_t>(y) * src_pitch, src_pitch);
    v.upload->Unmap(0, nullptr);
    return true;
}

static void CopyUpload(ID3D12GraphicsCommandList *list, VideoTex &v)
{
    D3D12_TEXTURE_COPY_LOCATION src = {}, dst = {};
    src.pResource = v.upload;
    src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    src.PlacedFootprint = v.fp;
    dst.pResource = v.tex;
    dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
}

static D3D12_RESOURCE_BARRIER Transition(ID3D12Resource *r, D3D12_RESOURCE_STATES before,
                                         D3D12_RESOURCE_STATES after)
{
    D3D12_RESOURCE_BARRIER b = {};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition.pResource = r;
    b.Transition.StateBefore = before;
    b.Transition.StateAfter = after;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    return b;
}

// ---------------------------------------------------------------------------
// Compute-шейдер масштабирования. Пока используется для растяжения поля
// движения (клиент шлёт его в разрешении оптического потока, ~320x180, вместо
// work-разрешения) — это снимало с CPU ~8 мс на кадр: 6 миллионов значений,
// resize плюс конвертация во float16.
//
// Билинейная выборка с зажатием по краям — та же интерполяция, что делал
// cv2.resize(INTER_LINEAR): пиксельные центры считаются как (i + 0.5) / size.
// ---------------------------------------------------------------------------
// Bilinear is done by hand instead of through the hardware sampler: sampler
// weights are quantised to 1/256 of a texel, and over a sevenfold upscale
// that showed up as up to 21 levels of difference in the final NGX frame
// against the cv2 path (measured). Here the weights are float32 and the
// mapping is exactly cv2.resize(INTER_LINEAR):
//     src = (dst + 0.5) * srcSize / dstSize - 0.5, edges clamped.
static const char kScaleHlsl[] =
    "Texture2D<float2>   gSrc : register(t0);\n"
    "RWTexture2D<float2> gDst : register(u0);\n"
    "cbuffer Sizes : register(b0) { uint gDstW; uint gDstH; uint gSrcW; uint gSrcH; };\n"
    "[numthreads(8, 8, 1)]\n"
    "void CSMain(uint3 id : SV_DispatchThreadID)\n"
    "{\n"
    "    if (id.x >= gDstW || id.y >= gDstH) return;\n"
    "    float2 src = (float2(id.xy) + 0.5f) * float2(gSrcW, gSrcH)\n"
    "                 / float2(gDstW, gDstH) - 0.5f;\n"
    "    float2 f  = frac(src);\n"
    "    int2   p0 = int2(floor(src));\n"
    "    int2   hi = int2(gSrcW - 1, gSrcH - 1);\n"
    "    int2   a  = clamp(p0,              int2(0, 0), hi);\n"
    "    int2   b  = clamp(p0 + int2(1, 1), int2(0, 0), hi);\n"
    "    float2 v00 = gSrc[int2(a.x, a.y)];\n"
    "    float2 v10 = gSrc[int2(b.x, a.y)];\n"
    "    float2 v01 = gSrc[int2(a.x, b.y)];\n"
    "    float2 v11 = gSrc[int2(b.x, b.y)];\n"
    "    gDst[id.xy] = lerp(lerp(v00, v10, f.x), lerp(v01, v11, f.x), f.y);\n"
    "}\n";

typedef HRESULT(WINAPI *PFN_D3DCompile_)(LPCVOID, SIZE_T, LPCSTR, const void *, void *,
                                         LPCSTR, LPCSTR, UINT, UINT, ID3DBlob **, ID3DBlob **);
typedef HRESULT(WINAPI *PFN_D3D12SerializeRootSignature_)(const D3D12_ROOT_SIGNATURE_DESC *,
                                                          D3D_ROOT_SIGNATURE_VERSION,
                                                          ID3DBlob **, ID3DBlob **);

static ID3D12RootSignature  *g_scale_rs;
static ID3D12PipelineState  *g_scale_pso;
static ID3D12DescriptorHeap *g_scale_heap;
static ID3D12Resource       *g_scale_src_bound;   // для каких ресурсов уже созданы дескрипторы
static ID3D12Resource       *g_scale_dst_bound;
static VideoTex              g_motion_small;      // поле движения в разрешении потока
static UINT                  g_motion_w, g_motion_h;  // 0 = motion приходит в work-разрешении

static void ReleaseVideoTex(VideoTex &t)
{
    if (t.tex != nullptr) { t.tex->Release(); t.tex = nullptr; }
    if (t.upload != nullptr) { t.upload->Release(); t.upload = nullptr; }
}

static void CloseMotionScaler()
{
    ReleaseVideoTex(g_motion_small);
    g_motion_w = g_motion_h = 0;
    g_scale_src_bound = nullptr;
    g_scale_dst_bound = nullptr;
}

// Компиляция шейдера и root signature — один раз за жизнь процесса.
static bool EnsureScalePipeline()
{
    if (g_scale_pso != nullptr) return true;

    HMODULE compiler = LoadLibraryW(L"d3dcompiler_47.dll");
    auto compile = compiler ? reinterpret_cast<PFN_D3DCompile_>(
                                  GetProcAddress(compiler, "D3DCompile")) : nullptr;
    HMODULE d3d12 = GetModuleHandleW(L"d3d12.dll");
    auto serialize = d3d12 ? reinterpret_cast<PFN_D3D12SerializeRootSignature_>(
                                 GetProcAddress(d3d12, "D3D12SerializeRootSignature")) : nullptr;
    if (compile == nullptr || serialize == nullptr)
    { Log("[scale] D3DCompile/D3D12SerializeRootSignature unavailable"); return false; }

    ID3DBlob *code = nullptr, *errors = nullptr;
    HRESULT hr = compile(kScaleHlsl, sizeof(kScaleHlsl) - 1, "scale.hlsl", nullptr, nullptr,
                         "CSMain", "cs_5_0", 0, 0, &code, &errors);
    if (FAILED(hr) || code == nullptr)
    {
        Log("[scale] shader compile failed 0x%08X: %s", hr,
            errors ? static_cast<const char *>(errors->GetBufferPointer()) : "(no log)");
        if (errors) errors->Release();
        return false;
    }
    if (errors) errors->Release();

    D3D12_DESCRIPTOR_RANGE ranges[2] = {};
    ranges[0].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
    ranges[0].NumDescriptors = 1;
    ranges[0].BaseShaderRegister = 0;
    ranges[0].OffsetInDescriptorsFromTableStart = 0;
    ranges[1].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
    ranges[1].NumDescriptors = 1;
    ranges[1].BaseShaderRegister = 0;
    ranges[1].OffsetInDescriptorsFromTableStart = 1;

    D3D12_ROOT_PARAMETER params[2] = {};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[0].Constants.ShaderRegister = 0;
    params[0].Constants.Num32BitValues = 4;
    params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[1].DescriptorTable.NumDescriptorRanges = 2;
    params[1].DescriptorTable.pDescriptorRanges = ranges;
    params[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

    D3D12_STATIC_SAMPLER_DESC samp = {};
    samp.Filter = D3D12_FILTER_MIN_MAG_MIP_LINEAR;
    samp.AddressU = samp.AddressV = samp.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    samp.MaxLOD = D3D12_FLOAT32_MAX;
    samp.ShaderRegister = 0;
    samp.ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

    D3D12_ROOT_SIGNATURE_DESC rsd = {};
    rsd.NumParameters = _countof(params);
    rsd.pParameters = params;
    rsd.NumStaticSamplers = 1;
    rsd.pStaticSamplers = &samp;

    ID3DBlob *rs_blob = nullptr;
    hr = serialize(&rsd, D3D_ROOT_SIGNATURE_VERSION_1, &rs_blob, &errors);
    if (FAILED(hr) || rs_blob == nullptr)
    {
        Log("[scale] root signature serialize failed 0x%08X: %s", hr,
            errors ? static_cast<const char *>(errors->GetBufferPointer()) : "(no log)");
        if (errors) errors->Release();
        code->Release();
        return false;
    }
    if (errors) errors->Release();
    hr = h.dev->CreateRootSignature(0, rs_blob->GetBufferPointer(), rs_blob->GetBufferSize(),
                                    __uuidof(ID3D12RootSignature),
                                    reinterpret_cast<void **>(&g_scale_rs));
    rs_blob->Release();
    if (FAILED(hr)) { Log("[scale] CreateRootSignature failed 0x%08X", hr); code->Release(); return false; }

    D3D12_COMPUTE_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = g_scale_rs;
    pd.CS.pShaderBytecode = code->GetBufferPointer();
    pd.CS.BytecodeLength = code->GetBufferSize();
    hr = h.dev->CreateComputePipelineState(&pd, __uuidof(ID3D12PipelineState),
                                           reinterpret_cast<void **>(&g_scale_pso));
    code->Release();
    if (FAILED(hr)) { Log("[scale] CreateComputePipelineState failed 0x%08X", hr); return false; }

    D3D12_DESCRIPTOR_HEAP_DESC hd = {};
    hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    hd.NumDescriptors = 2;
    hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    hr = h.dev->CreateDescriptorHeap(&hd, __uuidof(ID3D12DescriptorHeap),
                                     reinterpret_cast<void **>(&g_scale_heap));
    if (FAILED(hr)) { Log("[scale] CreateDescriptorHeap failed 0x%08X", hr); return false; }

    Log("[scale] compute pipeline ready (bilinear, clamp)");
    return true;
}

// Дескрипторы пересоздаются только при смене ресурсов (RNSZ, MOTS).
static void BindScaleDescriptors(ID3D12Resource *src, ID3D12Resource *dst)
{
    if (src == g_scale_src_bound && dst == g_scale_dst_bound) return;
    const UINT stride = h.dev->GetDescriptorHandleIncrementSize(
        D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    D3D12_CPU_DESCRIPTOR_HANDLE cpu = g_scale_heap->GetCPUDescriptorHandleForHeapStart();

    D3D12_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_R16G16_FLOAT;
    sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    sd.Texture2D.MipLevels = 1;
    h.dev->CreateShaderResourceView(src, &sd, cpu);

    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_R16G16_FLOAT;
    ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    cpu.ptr += stride;
    h.dev->CreateUnorderedAccessView(dst, nullptr, &ud, cpu);

    g_scale_src_bound = src;
    g_scale_dst_bound = dst;
}

// Открыть путь «motion приходит уменьшенным». 0x0 — закрыть.
static bool OpenMotionScaler(UINT w, UINT hgt)
{
    CloseMotionScaler();
    if (w == 0 || hgt == 0) return true;   // выключение — это успех
    if (w < 8 || hgt < 8 || w > 7680 || hgt > 4320)
    { Log("[scale] MOTS rejected: implausible motion size %ux%u", w, hgt); return false; }
    if (!EnsureScalePipeline()) return false;
    if (!CreateVideoTex(g_motion_small, w, hgt, DXGI_FORMAT_R16G16_FLOAT, w * 4))
    { Log("[scale] motion texture %ux%u creation failed", w, hgt); return false; }
    g_motion_w = w;
    g_motion_h = hgt;
    Log("[scale] motion arrives at %ux%u, stretched on the GPU", w, hgt);
    return true;
}

// Загрузить уменьшенное поле движения и растянуть его в v.mv.tex.
// Вызывается внутри уже открытого списка команд.
static bool ScaleMotionInto(VideoState &v, const BYTE *mv, bool mv_was_ready)
{
    if (!FillUpload(g_motion_small, mv, g_motion_w * 4, g_motion_h)) return false;
    D3D12_RESOURCE_BARRIER to_dst = Transition(
        g_motion_small.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
        D3D12_RESOURCE_STATE_COPY_DEST);
    if (g_scale_src_bound == g_motion_small.tex)  // не первый кадр: текстура была SRV
        h.list->ResourceBarrier(1, &to_dst);
    CopyUpload(h.list, g_motion_small);

    D3D12_RESOURCE_BARRIER pre[] = {
        Transition(g_motion_small.tex, D3D12_RESOURCE_STATE_COPY_DEST,
                   D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(v.mv.tex,
                   mv_was_ready ? D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE
                                : D3D12_RESOURCE_STATE_COPY_DEST,
                   D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
    };
    h.list->ResourceBarrier(_countof(pre), pre);

    BindScaleDescriptors(g_motion_small.tex, v.mv.tex);
    ID3D12DescriptorHeap *heaps[] = { g_scale_heap };
    h.list->SetDescriptorHeaps(1, heaps);
    h.list->SetComputeRootSignature(g_scale_rs);
    h.list->SetPipelineState(g_scale_pso);
    const UINT sizes[4] = { v.w, v.hgt, g_motion_w, g_motion_h };
    h.list->SetComputeRoot32BitConstants(0, 4, sizes, 0);
    h.list->SetComputeRootDescriptorTable(
        1, g_scale_heap->GetGPUDescriptorHandleForHeapStart());
    h.list->Dispatch((v.w + 7) / 8, (v.hgt + 7) / 8, 1);

    D3D12_RESOURCE_BARRIER post = Transition(v.mv.tex, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                             D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    h.list->ResourceBarrier(1, &post);
    return true;
}

// ---------------------------------------------------------------------------
// Desktop Duplication capture (DDA1). When active, the colour frame is taken
// by this worker straight from the desktop (D3D11 DDA -> NT shared texture ->
// D3D12), swizzled BGRA->RGBA into v.color.tex, and the client keeps sending
// only motion. All guarded by g_dda_active, the pipe path stays untouched.
// ---------------------------------------------------------------------------
static bool                    g_dda_active = false;   // DDA1 with w>0 has been acked
static UINT                    g_dda_w = 0, g_dda_h = 0;
static ID3D11Device           *g_dda_d11 = nullptr;
static ID3D11DeviceContext    *g_dda_ctx = nullptr;
static IDXGIOutputDuplication *g_dda_dup = nullptr;
static ID3D11Texture2D        *g_dda_shared = nullptr;
static HANDLE                  g_dda_nt = nullptr;
static ID3D12Resource         *g_dda_d12 = nullptr;
static ID3D12Fence            *g_dda_signal = nullptr;   // shared, D3D11 side signals
static ID3D11Fence            *g_dda_signal11 = nullptr;
static HANDLE                  g_dda_fence_ev = nullptr;  // событийное ожидание копии D3D11
static UINT64                  g_dda_fence_value = 1;
static bool                    g_dda_ready = false;      // current frame is in v.color
// Gray downsample (GRAY): write luminance (flow size) into a client mapping.
static HANDLE                  g_gray_file = nullptr;   // client's mapping handle
static BYTE                   *g_gray_map = nullptr;    // mapped view
static size_t                  g_gray_bytes = 0;
static UINT                    g_gray_w = 0, g_gray_h = 0;
static ID3D12Resource         *g_gray_readback = nullptr; // R8 buffer for gray
static ID3D12Resource         *g_gray_uav = nullptr;      // R8 UAV texture (area kernel writes)
static bool                    g_gray_mapped = false;

static void CloseGray()
{
    g_gray_mapped = false;
    if (g_gray_map) { UnmapViewOfFile(g_gray_map); g_gray_map = nullptr; }
    if (g_gray_file) { CloseHandle(g_gray_file); g_gray_file = nullptr; }
    g_gray_bytes = 0;
    g_gray_w = g_gray_h = 0;
}

static void CloseDda()
{
    g_dda_active = false;
    g_dda_ready = false;
    if (g_dda_d12) { g_dda_d12->Release(); g_dda_d12 = nullptr; }
    if (g_dda_nt)  { CloseHandle(g_dda_nt); g_dda_nt = nullptr; }
    if (g_dda_shared) { g_dda_shared->Release(); g_dda_shared = nullptr; }
    if (g_dda_dup) { g_dda_dup->Release(); g_dda_dup = nullptr; }
    if (g_dda_ctx) { g_dda_ctx->Release(); g_dda_ctx = nullptr; }
    if (g_dda_d11) { g_dda_d11->Release(); g_dda_d11 = nullptr; }
    if (g_dda_signal) { g_dda_signal->Release(); g_dda_signal = nullptr; }
    if (g_dda_signal11) { g_dda_signal11->Release(); g_dda_signal11 = nullptr; }
    if (g_dda_fence_ev) { CloseHandle(g_dda_fence_ev); g_dda_fence_ev = nullptr; }
}

// Compute pipeline to swizzle BGRA->RGBA (the DDA frame and v.color are both
// RGBA; DDA gives B8G8R8A8). Shares the worker's D3D12 device.
static ID3D12RootSignature  *g_dda_rs = nullptr;
static ID3D12PipelineState  *g_dda_pso = nullptr;
static ID3D12DescriptorHeap *g_dda_heap = nullptr;
static ID3D12Resource       *g_dda_dst = nullptr;       // swizzled RGBA into color
static const char kDdaSwizzleHlsl[] =
    "Texture2D<float4>   gSrc : register(t0);\n"
    "RWTexture2D<float4> gDst : register(u0);\n"
    "[numthreads(8, 8, 1)]\n"
    "void CSMain(uint3 id : SV_DispatchThreadID)\n"
    "{\n"
    "    uint2 sz; gDst.GetDimensions(sz.x, sz.y);\n"
    "    if (id.x >= sz.x || id.y >= sz.y) return;\n"
    "    float4 c = gSrc.Load(int3(id.xy, 0));\n"
    // B8G8R8A8 SRV уже декодируется HLSL в RGBA-семантику правильных
    // компонентов: c.r = красный, c.b = синий. Никакого свопа делать
    // НЕЛЬЗЯ — float4(c.b,...) давал перепутанные каналы (двойной своп).
    "    gDst[id.xy] = c;\n"
    "}\n";

static bool EnsureDdaSwizzle()
{
    if (g_dda_pso) return true;
    ID3DBlob *code = nullptr, *err = nullptr;
    HRESULT hr = D3DCompile(kDdaSwizzleHlsl, sizeof(kDdaSwizzleHlsl) - 1, "dda-swizzle.hlsl",
                            nullptr, nullptr, "CSMain", "cs_5_0", 0, 0, &code, &err);
    if (FAILED(hr)) { Log("[dda] swizzle compile failed: %s", err ? (char *)err->GetBufferPointer() : "?"); return false; }
    D3D12_ROOT_PARAMETER prm[2] = {};
    D3D12_DESCRIPTOR_RANGE r0 = { D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0 };
    D3D12_DESCRIPTOR_RANGE r1 = { D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0 };
    prm[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    prm[0].DescriptorTable.NumDescriptorRanges = 1; prm[0].DescriptorTable.pDescriptorRanges = &r0;
    prm[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    prm[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    prm[1].DescriptorTable.NumDescriptorRanges = 1; prm[1].DescriptorTable.pDescriptorRanges = &r1;
    prm[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    D3D12_ROOT_SIGNATURE_DESC rsd = {};
    rsd.NumParameters = 2; rsd.pParameters = prm;
    ID3DBlob *sig = nullptr;
    if (FAILED(D3D12SerializeRootSignature(&rsd, D3D_ROOT_SIGNATURE_VERSION_1, &sig, &err)))
    { Log("[dda] RS serialize failed"); return false; }
    if (FAILED(h.dev->CreateRootSignature(0, sig->GetBufferPointer(), sig->GetBufferSize(),
                                          __uuidof(ID3D12RootSignature),
                                          reinterpret_cast<void **>(&g_dda_rs))))
    { Log("[dda] RS create failed"); return false; }
    sig->Release();
    D3D12_COMPUTE_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = g_dda_rs;
    pd.CS.pShaderBytecode = code->GetBufferPointer(); pd.CS.BytecodeLength = code->GetBufferSize();
    if (FAILED(h.dev->CreateComputePipelineState(&pd, __uuidof(ID3D12PipelineState),
                                                 reinterpret_cast<void **>(&g_dda_pso))))
    { Log("[dda] PSO create failed"); return false; }
    code->Release();
    D3D12_DESCRIPTOR_HEAP_DESC hd = {};
    hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    hd.NumDescriptors = 2; hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    if (FAILED(h.dev->CreateDescriptorHeap(&hd, __uuidof(ID3D12DescriptorHeap),
                                           reinterpret_cast<void **>(&g_dda_heap))))
    { Log("[dda] heap create failed"); return false; }
    Log("[dda] swizzle pipeline ready");
    return true;
}

static void BindDdaDescriptors(ID3D12Resource *src)
{
    const UINT stride = h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    D3D12_CPU_DESCRIPTOR_HANDLE cpu = g_dda_heap->GetCPUDescriptorHandleForHeapStart();
    D3D12_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_B8G8R8A8_UNORM; sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    sd.Texture2D.MipLevels = 1;
    h.dev->CreateShaderResourceView(src, &sd, cpu);
    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_R8G8B8A8_UNORM; ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    cpu.ptr += stride;
    h.dev->CreateUnorderedAccessView(g_dda_dst, nullptr, &ud, cpu);
}

// ---------------------------------------------------------------------------
// Gray downsample (GRAY): честное блочное усреднение RGBA -> R8 luminance.
// Блок ровно ceil(src/dst) (12x12 при 3840x2160 -> 320x180) — то, что делает
// cv2.resize(INTER_AREA). Билинейная выборка НЕ годится: алиасинг на тексте
// ломает оптический поток (проверено Клодом).
// ---------------------------------------------------------------------------
static ID3D12RootSignature  *g_gray_rs = nullptr;
static ID3D12PipelineState  *g_gray_pso = nullptr;
static const char kGrayHlsl[] =
    "Texture2D<float4>   gSrc : register(t0);\n"
    "RWTexture2D<float>  gDst : register(u0);\n"
    "cbuffer Sizes : register(b0) { uint gSrcW; uint gSrcH; uint gDstW; uint gDstH; };\n"
    "[numthreads(8, 8, 1)]\n"
    "void CSMain(uint3 id : SV_DispatchThreadID)\n"
    "{\n"
    "    if (id.x >= gDstW || id.y >= gDstH) return;\n"
    "    uint bx = (gSrcW + gDstW - 1) / gDstW;\n"
    "    uint by = (gSrcH + gDstH - 1) / gDstH;\n"
    "    uint x0 = id.x * bx, y0 = id.y * by;\n"
    "    uint x1 = min(x0 + bx, gSrcW), y1 = min(y0 + by, gSrcH);\n"
    "    float sum = 0.0f;\n"
    "    for (uint y = y0; y < y1; ++y)\n"
    "        for (uint x = x0; x < x1; ++x)\n"
    "        {\n"
    "            float4 c = gSrc.Load(int3(x, y, 0));\n"
    "            sum += dot(c.rgb, float3(0.299f, 0.587f, 0.114f));\n"
    "        }\n"
    "    gDst[id.xy] = sum / (float)((x1 - x0) * (y1 - y0));\n"
    "}\n";

static bool EnsureGrayPipeline()
{
    if (g_gray_pso) return true;
    ID3DBlob *code = nullptr, *err = nullptr;
    HRESULT hr = D3DCompile(kGrayHlsl, sizeof(kGrayHlsl) - 1, "gray-area.hlsl",
                            nullptr, nullptr, "CSMain", "cs_5_0", 0, 0, &code, &err);
    if (FAILED(hr)) { Log("[gray] compile failed: %s", err ? (char *)err->GetBufferPointer() : "?"); return false; }
    D3D12_DESCRIPTOR_RANGE r0 = { D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0 };
    D3D12_DESCRIPTOR_RANGE r1 = { D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0 };
    D3D12_ROOT_PARAMETER prm[3] = {};
    prm[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    prm[0].Constants.Num32BitValues = 4; prm[0].Constants.ShaderRegister = 0;
    prm[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    prm[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    prm[1].DescriptorTable.NumDescriptorRanges = 1; prm[1].DescriptorTable.pDescriptorRanges = &r0;
    prm[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    prm[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    prm[2].DescriptorTable.NumDescriptorRanges = 1; prm[2].DescriptorTable.pDescriptorRanges = &r1;
    prm[2].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    D3D12_ROOT_SIGNATURE_DESC rsd = {};
    rsd.NumParameters = 3; rsd.pParameters = prm;
    ID3DBlob *sig = nullptr;
    if (FAILED(D3D12SerializeRootSignature(&rsd, D3D_ROOT_SIGNATURE_VERSION_1, &sig, &err)))
    { Log("[gray] RS serialize failed"); return false; }
    if (FAILED(h.dev->CreateRootSignature(0, sig->GetBufferPointer(), sig->GetBufferSize(),
                                          __uuidof(ID3D12RootSignature),
                                          reinterpret_cast<void **>(&g_gray_rs))))
    { Log("[gray] RS create failed"); return false; }
    sig->Release();
    D3D12_COMPUTE_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = g_gray_rs;
    pd.CS.pShaderBytecode = code->GetBufferPointer(); pd.CS.BytecodeLength = code->GetBufferSize();
    if (FAILED(h.dev->CreateComputePipelineState(&pd, __uuidof(ID3D12PipelineState),
                                                 reinterpret_cast<void **>(&g_gray_pso))))
    { Log("[gray] PSO create failed"); return false; }
    code->Release();
    Log("[gray] AREA pipeline ready");
    return true;
}

// Открыть обратный маппинг клиента (GRAY). w/h — размер luminance (320x180).
static bool OpenGray(const VideoGrayCmd &gc)
{
    CloseGray();
    char name[64] = {};
    memcpy(name, gc.name, sizeof(name) - 1);
    if (gc.width == 0 || gc.height == 0) { Log("[gray] off"); return true; }
    const size_t need = static_cast<size_t>(gc.width) * gc.height;
    g_gray_file = OpenFileMappingA(FILE_MAP_WRITE, FALSE, name);
    if (g_gray_file == nullptr) { Log("[gray] OpenFileMapping('%s') failed %lu", name, GetLastError()); return false; }
    g_gray_map = static_cast<BYTE *>(MapViewOfFile(g_gray_file, FILE_MAP_WRITE, 0, 0, need));
    if (g_gray_map == nullptr) { Log("[gray] MapViewOfFile(%zu) failed %lu", need, GetLastError()); CloseHandle(g_gray_file); g_gray_file = nullptr; return false; }
    g_gray_bytes = need; g_gray_w = gc.width; g_gray_h = gc.height;
    if (!EnsureGrayPipeline()) return false;
    // R8 UAV текстура
    D3D12_HEAP_PROPERTIES def = { D3D12_HEAP_TYPE_DEFAULT };
    D3D12_RESOURCE_DESC td = {};
    td.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    td.Width = gc.width; td.Height = gc.height; td.DepthOrArraySize = 1; td.MipLevels = 1;
    td.Format = DXGI_FORMAT_R8_UNORM; td.SampleDesc.Count = 1;
    td.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    td.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    if (FAILED(h.dev->CreateCommittedResource(&def, D3D12_HEAP_FLAG_NONE, &td,
                                              D3D12_RESOURCE_STATE_UNORDERED_ACCESS, nullptr,
                                              __uuidof(ID3D12Resource),
                                              reinterpret_cast<void **>(&g_gray_uav))))
    { Log("[gray] UAV tex failed"); return false; }
    // readback buffer ровно need байт
    D3D12_HEAP_PROPERTIES rb = { D3D12_HEAP_TYPE_READBACK };
    D3D12_RESOURCE_DESC bd = {};
    bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = need; bd.Height = 1; bd.DepthOrArraySize = 1; bd.MipLevels = 1;
    bd.SampleDesc.Count = 1; bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    if (FAILED(h.dev->CreateCommittedResource(&rb, D3D12_HEAP_FLAG_NONE, &bd,
                                              D3D12_RESOURCE_STATE_COPY_DEST, nullptr,
                                              __uuidof(ID3D12Resource),
                                              reinterpret_cast<void **>(&g_gray_readback))))
    { Log("[gray] readback failed"); return false; }
    g_gray_mapped = true;
    Log("[gray] mapping '%s' %ux%u (%zu B) active", gc.name, gc.width, gc.height, need);
    return true;
}

// Выполнить AREA-усреднение g_dda_dst -> gray и записать в маппинг клиента.
// Вызывается в DdaGrab после swizzle (в том же Begin/End блоке нельзя —
// нужен отдельный fence), поэтому здесь собственный Begin/End.
static bool AreaToGray()
{
    if (!g_gray_mapped || !g_dda_dst) return false;
    if (!BeginCommands()) return false;
    // g_dda_dst в COPY_SOURCE после copy в v.color? Нет — после swizzle он
    // возвращается в UNORDERED_ACCESS (см. DdaGrab). Читаем как SRV.
    D3D12_RESOURCE_BARRIER to_srv = Transition(g_dda_dst, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                               D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    h.list->ResourceBarrier(1, &to_srv);
    // дескрипторы: 0 = SRV g_dda_dst, 1 = UAV g_gray_uav
    const UINT stride = h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    D3D12_CPU_DESCRIPTOR_HANDLE cpu = g_dda_heap->GetCPUDescriptorHandleForHeapStart();
    D3D12_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM; sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    sd.Texture2D.MipLevels = 1;
    h.dev->CreateShaderResourceView(g_dda_dst, &sd, cpu);
    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_R8_UNORM; ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    D3D12_CPU_DESCRIPTOR_HANDLE cpu2 = cpu; cpu2.ptr += stride;
    h.dev->CreateUnorderedAccessView(g_gray_uav, nullptr, &ud, cpu2);
    ID3D12DescriptorHeap *heaps[] = { g_dda_heap };
    h.list->SetDescriptorHeaps(1, heaps);
    h.list->SetComputeRootSignature(g_gray_rs);
    h.list->SetPipelineState(g_gray_pso);
    const UINT sizes[4] = { g_dda_w, g_dda_h, g_gray_w, g_gray_h };
    h.list->SetComputeRoot32BitConstants(0, 4, sizes, 0);
    D3D12_GPU_DESCRIPTOR_HANDLE g0 = g_dda_heap->GetGPUDescriptorHandleForHeapStart();
    D3D12_GPU_DESCRIPTOR_HANDLE g1 = g0; g1.ptr += stride;
    h.list->SetComputeRootDescriptorTable(1, g0);
    h.list->SetComputeRootDescriptorTable(2, g1);
    h.list->Dispatch((g_gray_w + 7) / 8, (g_gray_h + 7) / 8, 1);
    // gray UAV -> COPY_SOURCE, копируем в readback
    D3D12_RESOURCE_BARRIER to_copy = Transition(g_gray_uav, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                                D3D12_RESOURCE_STATE_COPY_SOURCE);
    h.list->ResourceBarrier(1, &to_copy);
    D3D12_TEXTURE_COPY_LOCATION src = {}, dst = {};
    src.pResource = g_gray_uav; src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; src.SubresourceIndex = 0;
    dst.pResource = g_gray_readback; dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dst.PlacedFootprint.Footprint.Width = g_gray_w;
    dst.PlacedFootprint.Footprint.Height = g_gray_h;
    dst.PlacedFootprint.Footprint.Depth = 1;
    dst.PlacedFootprint.Footprint.RowPitch = g_gray_w;
    dst.PlacedFootprint.Footprint.Format = DXGI_FORMAT_R8_UNORM;
    h.list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    // вернуть g_dda_dst в COPY_SOURCE? нет — он остаётся NON_PIXEL_SHADER_RESOURCE
    // для след. swizzle? В DdaGrab после copy его возвращают в UNORDERED_ACCESS.
    // Здесь мы его взяли в NON_PIXEL из UNORDERED_ACCESS — вернём обратно.
    D3D12_RESOURCE_BARRIER back_uav = Transition(g_dda_dst, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                                 D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    D3D12_RESOURCE_BARRIER back_uav2 = Transition(g_gray_uav, D3D12_RESOURCE_STATE_COPY_SOURCE,
                                                  D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    D3D12_RESOURCE_BARRIER backs[2] = { back_uav, back_uav2 };
    h.list->ResourceBarrier(2, backs);
    const UINT64 fence = EndCommands();
    if (!WaitFenceValue(h.fence, fence, 10000)) { Log("[gray] fence timeout"); return false; }
    // map readback -> memcpy в mapping клиента
    BYTE *mapped = nullptr;
    D3D12_RANGE rr = { 0, g_gray_bytes };
    if (FAILED(g_gray_readback->Map(0, &rr, reinterpret_cast<void **>(&mapped)))) return false;
    memcpy(g_gray_map, mapped, g_gray_bytes);
    g_gray_readback->Unmap(0, nullptr);
    return true;
}

// Open capture. w/h = capture size; the worker keeps its own pipe for motion.
static bool OpenDda(UINT w, UINT hgt)
{
    CloseDda();
    if (w == 0 || hgt == 0) { Log("[dda] capture off"); return true; }
    if (!EnsureDdaSwizzle()) return false;
    IDXGIFactory1 *factory = nullptr;
    HRESULT hr = CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void **)&factory);
    if (FAILED(hr)) { Log("[dda] factory failed 0x%08X", hr); return false; }
    IDXGIAdapter1 *adapter = nullptr;
    if (FAILED(factory->EnumAdapters1(0, &adapter))) { Log("[dda] no adapter"); factory->Release(); return false; }
    IDXGIOutput *output = nullptr;
    if (FAILED(adapter->EnumOutputs(0, &output))) { Log("[dda] no output"); adapter->Release(); factory->Release(); return false; }
    IDXGIOutput1 *output1 = nullptr;
    if (FAILED(output->QueryInterface(__uuidof(IDXGIOutput1), (void **)&output1)))
    { Log("[dda] no Output1"); output->Release(); adapter->Release(); factory->Release(); return false; }
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    hr = D3D11CreateDevice(adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr,
                           D3D11_CREATE_DEVICE_BGRA_SUPPORT, &fl, 1, D3D11_SDK_VERSION,
                           &g_dda_d11, nullptr, &g_dda_ctx);
    if (FAILED(hr)) { Log("[dda] D3D11 failed 0x%08X", hr); return false; }
    hr = output1->DuplicateOutput(g_dda_d11, &g_dda_dup);
    output1->Release(); output->Release(); adapter->Release(); factory->Release();
    if (FAILED(hr)) { Log("[dda] DuplicateOutput failed 0x%08X", hr); return false; }
    g_dda_w = w; g_dda_h = hgt; g_dda_active = true;
    Log("[dda] capture %ux%u active", w, hgt);
    return true;
}

// Профилировщик фаз объявлен ниже (перед RunVideo), но щуп нужен здесь —
// поэтому список фаз и прототипы вынесены вперёд.
enum { PH_ACQ, PH_DDA, PH_UPLOAD, PH_EVAL, PH_PRESENT, PH_FRAME, PH_COUNT };
static double PhaseNow();
static void PhaseAdd(int idx, double t0);
static bool PhaseEnabled();

// Grab the latest desktop frame into v.color.tex (RGBA, GPU-resident).
static bool DdaGrab(VideoState &v)
{
    if (!g_dda_active) return false;
    IDXGIResource *res = nullptr;
    DXGI_OUTDUPL_FRAME_INFO fi = {};
    const double t_acq = PhaseNow();
    HRESULT hr = g_dda_dup->AcquireNextFrame(100, &fi, &res);
    PhaseAdd(PH_ACQ, t_acq);
    if (hr == DXGI_ERROR_WAIT_TIMEOUT) return false;      // desktop unchanged
    if (FAILED(hr))
    {
        Log("[dda] acquire failed 0x%08X — recreating", hr);
        OpenDda(g_dda_w, g_dda_h);
        return false;
    }
    ID3D11Texture2D *frame = nullptr;
    if (FAILED(res->QueryInterface(__uuidof(ID3D11Texture2D), (void **)&frame)))
    {
        g_dda_dup->ReleaseFrame();
        return false;
    }
    if (g_dda_shared == nullptr)
    {
        D3D11_TEXTURE2D_DESC fd{}; frame->GetDesc(&fd);
        D3D11_TEXTURE2D_DESC sd = {};
        sd.Width = fd.Width; sd.Height = fd.Height; sd.MipLevels = 1; sd.ArraySize = 1;
        sd.Format = fd.Format; sd.SampleDesc.Count = 1; sd.Usage = D3D11_USAGE_DEFAULT;
        sd.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
        sd.MiscFlags = D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_NTHANDLE;
        if (FAILED(g_dda_d11->CreateTexture2D(&sd, nullptr, &g_dda_shared)))
        { Log("[dda] shared tex failed"); g_dda_dup->ReleaseFrame(); frame->Release(); res->Release(); return false; }
        IDXGIResource1 *r1 = nullptr;
        g_dda_shared->QueryInterface(__uuidof(IDXGIResource1), (void **)&r1);
        if (!r1 || FAILED(r1->CreateSharedHandle(nullptr, DXGI_SHARED_RESOURCE_READ, nullptr, &g_dda_nt)))
        { Log("[dda] NT handle failed"); g_dda_dup->ReleaseFrame(); frame->Release(); res->Release(); return false; }
        r1->Release();
        if (FAILED(h.dev->OpenSharedHandle(g_dda_nt, __uuidof(ID3D12Resource),
                                           (void **)&g_dda_d12)))
        { Log("[dda] OpenSharedHandle failed"); g_dda_dup->ReleaseFrame(); frame->Release(); res->Release(); return false; }
        // cross-device signal fence
        g_dda_fence_value = 1;
        if (FAILED(h.dev->CreateFence(0, D3D12_FENCE_FLAG_SHARED, __uuidof(ID3D12Fence),
                                      reinterpret_cast<void **>(&g_dda_signal))))
        { Log("[dda] signal fence failed"); g_dda_dup->ReleaseFrame(); frame->Release(); res->Release(); return false; }
        HANDLE nt_f = nullptr;
        h.dev->CreateSharedHandle(g_dda_signal, nullptr, GENERIC_ALL, nullptr, &nt_f);
        ID3D11Device5 *d5 = nullptr;
        if (g_dda_d11->QueryInterface(__uuidof(ID3D11Device5), (void **)&d5) == S_OK)
        {
            d5->OpenSharedFence(nt_f, __uuidof(ID3D11Fence), (void **)&g_dda_signal11);
            d5->Release();
        }
        if (nt_f) CloseHandle(nt_f);
        if (!g_dda_signal11) { Log("[dda] no D3D11 fence — capture invalid"); g_dda_dup->ReleaseFrame(); frame->Release(); res->Release(); return false; }
        D3D12_HEAP_PROPERTIES def = { D3D12_HEAP_TYPE_DEFAULT };
        D3D12_RESOURCE_DESC td = {};
        td.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
        td.Width = fd.Width; td.Height = fd.Height; td.DepthOrArraySize = 1; td.MipLevels = 1;
        td.Format = DXGI_FORMAT_R8G8B8A8_UNORM; td.SampleDesc.Count = 1;
        td.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
        td.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
        if (FAILED(h.dev->CreateCommittedResource(&def, D3D12_HEAP_FLAG_NONE, &td,
                                                  D3D12_RESOURCE_STATE_UNORDERED_ACCESS, nullptr,
                                                  __uuidof(ID3D12Resource),
                                                  reinterpret_cast<void **>(&g_dda_dst))))
        { Log("[dda] dst UAV failed"); g_dda_dup->ReleaseFrame(); frame->Release(); res->Release(); return false; }
        Log("[dda] shared texture %ux%u ready", (UINT)fd.Width, (UINT)fd.Height);
    }
    g_dda_ctx->CopyResource(g_dda_shared, frame);
    ID3D11DeviceContext4 *ctx4 = nullptr;
    if (g_dda_ctx->QueryInterface(__uuidof(ID3D11DeviceContext4), (void **)&ctx4) == S_OK)
    {
        ctx4->Signal(g_dda_signal11, g_dda_fence_value);
        ctx4->Release();
    }
    g_dda_ctx->Flush();
    frame->Release(); res->Release(); g_dda_dup->ReleaseFrame();

    // Ждать копию D3D11 событием, а НЕ опросом со Sleep(1). Sleep(1) при
    // стандартном разрешении таймера Windows спит до 15.6 мс, и это давало
    // 13 мс на фазу dda при работе на 1-2 мс (замерено профилировщиком фаз:
    // acq=0.0, dda=13.0). SetEventOnCompletion будит поток точно.
    const UINT64 want = g_dda_fence_value;
    const DWORD wait_ms = 5000;
    if (g_dda_fence_ev == nullptr)
        g_dda_fence_ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (g_dda_signal->GetCompletedValue() < want)
    {
        if (g_dda_fence_ev != nullptr &&
            SUCCEEDED(g_dda_signal->SetEventOnCompletion(want, g_dda_fence_ev)))
            WaitForSingleObject(g_dda_fence_ev, wait_ms);
        else
        {
            // Событие недоступно — прежний опрос как страховка.
            const DWORD start = GetTickCount();
            while (g_dda_signal->GetCompletedValue() < want &&
                   GetTickCount() - start < wait_ms) Sleep(1);
        }
    }
    if (g_dda_signal->GetCompletedValue() < want)
    { Log("[dda] signal fence timeout"); return false; }
    ++g_dda_fence_value;

    // swizzle into g_dda_dst, then copy into v.color.tex
    if (!BeginCommands()) return false;
    D3D12_RESOURCE_BARRIER to_srv = Transition(g_dda_d12, D3D12_RESOURCE_STATE_COMMON,
                                               D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    h.list->ResourceBarrier(1, &to_srv);
    BindDdaDescriptors(g_dda_d12);
    ID3D12DescriptorHeap *heaps[] = { g_dda_heap };
    h.list->SetDescriptorHeaps(1, heaps);
    h.list->SetComputeRootSignature(g_dda_rs);
    h.list->SetPipelineState(g_dda_pso);
    D3D12_GPU_DESCRIPTOR_HANDLE g0 = g_dda_heap->GetGPUDescriptorHandleForHeapStart();
    D3D12_GPU_DESCRIPTOR_HANDLE g1 = g0;
    g1.ptr += h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    h.list->SetComputeRootDescriptorTable(0, g0);
    h.list->SetComputeRootDescriptorTable(1, g1);
    h.list->Dispatch((g_dda_w + 7) / 8, (g_dda_h + 7) / 8, 1);
    // copy swizzled dst into v.color.tex
    D3D12_RESOURCE_BARRIER pre_color = Transition(v.color.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                                  D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_RESOURCE_BARRIER to_copy = Transition(g_dda_dst, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                                D3D12_RESOURCE_STATE_COPY_SOURCE);
    D3D12_RESOURCE_BARRIER pre_c[2] = { to_copy, pre_color };
    h.list->ResourceBarrier(2, pre_c);
    D3D12_TEXTURE_COPY_LOCATION src = {}, dst = {};
    src.pResource = g_dda_dst; src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; src.SubresourceIndex = 0;
    dst.pResource = v.color.tex; dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; dst.SubresourceIndex = 0;
    h.list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    D3D12_RESOURCE_BARRIER to_uav = Transition(g_dda_dst, D3D12_RESOURCE_STATE_COPY_SOURCE,
                                               D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    D3D12_RESOURCE_BARRIER to_nps = Transition(v.color.tex, D3D12_RESOURCE_STATE_COPY_DEST,
                                               D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    D3D12_RESOURCE_BARRIER to_common = Transition(g_dda_d12, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                                  D3D12_RESOURCE_STATE_COMMON);
    D3D12_RESOURCE_BARRIER post_c[3] = { to_uav, to_nps, to_common };
    h.list->ResourceBarrier(3, post_c);
    const UINT64 fence = EndCommands();
    if (!WaitFenceValue(h.fence, fence, 10000)) { Log("[dda] swizzle fence timeout"); return false; }
    // Отдать клиенту luminance-кадр (320x180) для оптического потока
    if (!AreaToGray()) { /* best effort: guides останутся без свежего кадра */ }
    g_dda_ready = true;
    return true;
}

static bool UploadVideoFrame(VideoState &v, const BYTE *color, const BYTE *mv, bool motion_small)
{
    const UINT cw = v.upscale ? v.full_w : v.w;
    const UINT ch = v.upscale ? v.full_h : v.hgt;
    if (!FillUpload(v.color, color, cw * 4, ch)) return false;
    if (!motion_small && !FillUpload(v.mv, mv, v.w * 4, v.hgt)) return false;
    if (!BeginCommands()) return false;
    if (v.inputs_ready)
    {
        // Цвет всегда идёт копией; MV в режиме уменьшенного поля переводит
        // в COPY_DEST не он, а ScaleMotionInto — там своя цепочка состояний.
        D3D12_RESOURCE_BARRIER pre[] = {
            Transition(v.color.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_DEST),
            Transition(v.mv.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_DEST),
        };
        h.list->ResourceBarrier(motion_small ? 1 : _countof(pre), pre);
    }
    CopyUpload(h.list, v.color);
    if (motion_small)
    {
        if (!ScaleMotionInto(v, mv, v.inputs_ready)) return false;
        D3D12_RESOURCE_BARRIER post = Transition(v.color.tex, D3D12_RESOURCE_STATE_COPY_DEST,
                                                 D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        h.list->ResourceBarrier(1, &post);
    }
    else
    {
        CopyUpload(h.list, v.mv);
        D3D12_RESOURCE_BARRIER post[] = {
            Transition(v.color.tex, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
            Transition(v.mv.tex, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        };
        h.list->ResourceBarrier(_countof(post), post);
    }
    const UINT64 fence = EndCommands();
    v.inputs_ready = true;
    return WaitFenceValue(h.fence, fence, 30000);
}

// DDA-режим: цвет уже лежит в v.color.tex (DdaGrab), загружаем только motion.
static bool UploadMotionOnly(VideoState &v, const BYTE *mv, bool motion_small)
{
    if (!motion_small && !FillUpload(v.mv, mv, v.w * 4, v.hgt)) return false;
    if (!BeginCommands()) return false;
    if (v.inputs_ready)
    {
        D3D12_RESOURCE_BARRIER pre = Transition(v.mv.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                                D3D12_RESOURCE_STATE_COPY_DEST);
        h.list->ResourceBarrier(1, &pre);
    }
    if (motion_small)
    {
        if (!ScaleMotionInto(v, mv, v.inputs_ready)) return false;
    }
    else
    {
        CopyUpload(h.list, v.mv);
        D3D12_RESOURCE_BARRIER post = Transition(v.mv.tex, D3D12_RESOURCE_STATE_COPY_DEST,
                                                 D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        h.list->ResourceBarrier(1, &post);
    }
    const UINT64 fence = EndCommands();
    v.inputs_ready = true;
    return WaitFenceValue(h.fence, fence, 30000);
}

// ---------------------------------------------------------------------------
// Сколько из времени Evaluate GPU реально считает
//
// PH_EVAL меряет submit + ожидание забора на CPU: в него входит и постановка
// задачи, и просыпание потока. Таймстемпы на очереди отвечают, сколько из этого
// GPU реально считает.
//
// Замерено (RTX 5070 Ti, рабочий стол 2560x1600): CPU 8.9 мс, GPU 8.0 мс —
// накладные расходы 0.9 мс, остальное настоящая работа. И GPU-время не зависит
// от разрешения входа: 7.9-8.6 мс на 0.37-3.32 МПикс, пикселей в девять раз
// больше при том же времени. Модель считает на своём внутреннем разрешении,
// поэтому work_scale ничего и не стоил.
// ---------------------------------------------------------------------------
static ID3D12QueryHeap *g_ts_heap;
static ID3D12Resource  *g_ts_readback;
static UINT64           g_ts_freq;
static int              g_ts_state;   // 0 — не пробовали, 1 — готово, -1 — не вышло
static double           g_ts_sum;
static double           g_ts_max;
static unsigned         g_ts_n;

static bool EnsureTimestamps()
{
    if (g_ts_state != 0) return g_ts_state == 1;
    g_ts_state = -1;
    if (h.dev == nullptr || h.queue == nullptr) return false;
    D3D12_QUERY_HEAP_DESC qd = {};
    qd.Type = D3D12_QUERY_HEAP_TYPE_TIMESTAMP;
    qd.Count = 2;
    if (FAILED(h.dev->CreateQueryHeap(&qd, __uuidof(ID3D12QueryHeap),
                                      reinterpret_cast<void **>(&g_ts_heap))))
    { Log("[phase] CreateQueryHeap не удался — GPU-время eval мерить нечем"); return false; }
    D3D12_HEAP_PROPERTIES rb = {};
    rb.Type = D3D12_HEAP_TYPE_READBACK;
    D3D12_RESOURCE_DESC bd = {};
    bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = sizeof(UINT64) * 2; bd.Height = 1; bd.DepthOrArraySize = 1;
    bd.MipLevels = 1; bd.SampleDesc.Count = 1;
    bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    if (FAILED(h.dev->CreateCommittedResource(&rb, D3D12_HEAP_FLAG_NONE, &bd,
        D3D12_RESOURCE_STATE_COPY_DEST, nullptr, __uuidof(ID3D12Resource),
        reinterpret_cast<void **>(&g_ts_readback)))) return false;
    if (FAILED(h.queue->GetTimestampFrequency(&g_ts_freq)) || g_ts_freq == 0) return false;
    g_ts_state = 1;
    Log("[phase] GPU-таймстемпы вокруг Evaluate включены (частота %llu Гц)", g_ts_freq);
    return true;
}

static void ReadEvalGpuTime()
{
    if (g_ts_state != 1) return;
    D3D12_RANGE r = { 0, sizeof(UINT64) * 2 };
    void *mapped = nullptr;
    if (FAILED(g_ts_readback->Map(0, &r, &mapped)) || mapped == nullptr) return;
    const UINT64 *ts = static_cast<const UINT64 *>(mapped);
    if (ts[1] > ts[0])
    {
        const double ms = static_cast<double>(ts[1] - ts[0]) * 1000.0
                          / static_cast<double>(g_ts_freq);
        g_ts_sum += ms;
        if (ms > g_ts_max) g_ts_max = ms;
        ++g_ts_n;
    }
    D3D12_RANGE none = { 0, 0 };
    g_ts_readback->Unmap(0, &none);
}

static bool EvaluateVideo(VideoState &v, int reset)
{
    if (!BeginCommands()) return false;
    const bool ts = PhaseEnabled() && EnsureTimestamps();
    if (ts) h.list->EndQuery(g_ts_heap, D3D12_QUERY_TYPE_TIMESTAMP, 0);
    const UINT cw = v.upscale ? v.full_w : v.w;
    const UINT ch = v.upscale ? v.full_h : v.hgt;
    h.params->Reset();
    h.params->Set("DLSSNR.Color", v.color.tex); h.params->Set("DLSSNR.Output", v.output);
    h.params->Set("DLSSNR.MVec", v.mv.tex);
    h.params->Set("DLSSNR.ColorSubrectBaseX", 0u); h.params->Set("DLSSNR.ColorSubrectBaseY", 0u);
    h.params->Set("DLSSNR.ColorSubrectWidth", cw); h.params->Set("DLSSNR.ColorSubrectHeight", ch);
    h.params->Set("DLSSNR.MVecSubrectBaseX", 0u); h.params->Set("DLSSNR.MVecSubrectBaseY", 0u);
    h.params->Set("DLSSNR.MVecSubrectWidth", v.w); h.params->Set("DLSSNR.MVecSubrectHeight", v.hgt);
    h.params->Set("DLSSNR.OutputSubrectBaseX", 0u); h.params->Set("DLSSNR.OutputSubrectBaseY", 0u);
    h.params->Set("DLSSNR.OutputSubrectWidth", cw); h.params->Set("DLSSNR.OutputSubrectHeight", ch);
    h.params->Set("DLSSNR.MVecScaleX", 1.0f); h.params->Set("DLSSNR.MVecScaleY", 1.0f);
    h.params->Set("DLSSNR.Enabled", 1u); h.params->Set("DLSSNR.Reset", reset);
    h.params->Set("DLSSNR.Intensity", g_video_options.intensity);
    h.params->Set("DLSSNR.LocalToneStrength", g_video_options.local_tone);
    h.params->Set("DLSSNR.LocalStructureStrength", g_video_options.local_structure);
    h.params->Set("DLSSNR.SkinStructureStrength", g_video_options.skin_structure);
    h.params->Set("DLSSNR.UseAutoMask", g_video_options.auto_mask);
    h.params->Set("DLSSNR.Style", g_video_options.style);
    h.params->Set("DLSSNR.UICorrection", g_video_options.ui_correction);
    h.params->Set("DLSS.Pre.Exposure", 1.0f); h.params->Set("DLSS.Exposure.Scale", 1.0f);
    DWORD code = 0;
    NVSDK_NGX_Result result = static_cast<NVSDK_NGX_Result>(0x7FFFFFFF);
    __try { result = g_nr_evaluate(h.list, h.feature, h.params, nullptr); }
    __except (EXCEPTION_EXECUTE_HANDLER) { code = GetExceptionCode(); }
    g_last_eval_result = static_cast<uint32_t>(result);
    if (code != 0) { AbortCommands(); Log("[pure] direct evaluate exception 0x%08X", code); return false; }
    if (ts)
    {
        h.list->EndQuery(g_ts_heap, D3D12_QUERY_TYPE_TIMESTAMP, 1);
        h.list->ResolveQueryData(g_ts_heap, D3D12_QUERY_TYPE_TIMESTAMP, 0, 2,
                                 g_ts_readback, 0);
    }
    const UINT64 fence = EndCommands();
    if (NVSDK_NGX_FAILED(result)) { Log("[pure] direct evaluate failed 0x%08X (%s)", result, NgxResultName(result)); return false; }
    ++g_eval_count;
    if (!WaitFenceValue(h.fence, fence, 60000)) return false;
    if (ts) ReadEvalGpuTime();
    return true;
}

// Разделитель шторки: узкая полоса поверх выходной текстуры.
//
// Рисуется через ClearUnorderedAccessViewFloat с прямоугольником — шейдер для
// сплошной полосы не нужен, а v.output и так лежит в UNORDERED_ACCESS, поэтому
// обходимся без барьеров. Ему требуются два дескриптора одного UAV: видимый
// шейдеру и обычный CPU-шный, отсюда две крошечные кучи.
static ID3D12DescriptorHeap *g_split_heap_gpu;
static ID3D12DescriptorHeap *g_split_heap_cpu;
static ID3D12Resource       *g_split_uav_for;   // для какого ресурса сделан UAV

static bool EnsureSplitUav(ID3D12Resource *res)
{
    if (res == nullptr) return false;
    if (g_split_uav_for == res && g_split_heap_gpu != nullptr) return true;
    if (g_split_heap_gpu != nullptr) { g_split_heap_gpu->Release(); g_split_heap_gpu = nullptr; }
    if (g_split_heap_cpu != nullptr) { g_split_heap_cpu->Release(); g_split_heap_cpu = nullptr; }
    g_split_uav_for = nullptr;

    D3D12_DESCRIPTOR_HEAP_DESC hd = {};
    hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    hd.NumDescriptors = 1;
    hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    if (FAILED(h.dev->CreateDescriptorHeap(&hd, __uuidof(ID3D12DescriptorHeap),
            reinterpret_cast<void **>(&g_split_heap_gpu)))) return false;
    hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_NONE;
    if (FAILED(h.dev->CreateDescriptorHeap(&hd, __uuidof(ID3D12DescriptorHeap),
            reinterpret_cast<void **>(&g_split_heap_cpu)))) return false;

    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    h.dev->CreateUnorderedAccessView(res, nullptr, &ud,
        g_split_heap_gpu->GetCPUDescriptorHandleForHeapStart());
    h.dev->CreateUnorderedAccessView(res, nullptr, &ud,
        g_split_heap_cpu->GetCPUDescriptorHandleForHeapStart());
    g_split_uav_for = res;
    return true;
}

// Шторка «до/после»: левую часть кадра заменяем сырым захватом.
//
// Обе текстуры полноразмерные и одного формата (NGX ужимает вход внутри себя),
// поэтому это одно копирование области, целиком на GPU. Делается ДО показа и
// до отдачи пикселей, так что в запись и скриншот шторка попадает сама.
static bool SplitCompose(VideoState &v, UINT split_x)
{
    const UINT cw = v.upscale ? v.full_w : v.w;
    const UINT ch = v.upscale ? v.full_h : v.hgt;
    if (split_x == 0 || cw == 0 || ch == 0) return true;
    if (split_x > cw) split_x = cw;
    if (!BeginCommands()) return false;
    D3D12_RESOURCE_BARRIER pre[] = {
        Transition(v.color.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                   D3D12_RESOURCE_STATE_COPY_SOURCE),
        Transition(v.output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                   D3D12_RESOURCE_STATE_COPY_DEST),
    };
    h.list->ResourceBarrier(_countof(pre), pre);
    D3D12_TEXTURE_COPY_LOCATION src = {}, dst = {};
    src.pResource = v.color.tex;
    src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; src.SubresourceIndex = 0;
    dst.pResource = v.output;
    dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; dst.SubresourceIndex = 0;
    D3D12_BOX box = { 0, 0, 0, split_x, ch, 1 };
    h.list->CopyTextureRegion(&dst, 0, 0, 0, &src, &box);
    D3D12_RESOURCE_BARRIER post[] = {
        Transition(v.color.tex, D3D12_RESOURCE_STATE_COPY_SOURCE,
                   D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(v.output, D3D12_RESOURCE_STATE_COPY_DEST,
                   D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
    };
    h.list->ResourceBarrier(_countof(post), post);

    // Разделитель — только когда шторка действительно делит кадр: на краях
    // полоса висела бы вплотную к рамке без всякого смысла.
    if (split_x < cw && EnsureSplitUav(v.output))
    {
        const UINT lw = (ch >= 1400u) ? 3u : 2u;
        LONG left = static_cast<LONG>(split_x) - static_cast<LONG>(lw / 2);
        if (left < 0) left = 0;
        LONG right = left + static_cast<LONG>(lw);
        if (right > static_cast<LONG>(cw)) { right = static_cast<LONG>(cw); left = right - static_cast<LONG>(lw); }
        // #D97757 — тот же глиняный акцент, что в меню. Текстура обычная UNORM
        // (не _SRGB), поэтому байты кладём как есть, без гамма-пересчёта.
        const FLOAT accent[4] = { 217.0f / 255.0f, 119.0f / 255.0f, 87.0f / 255.0f, 1.0f };
        const D3D12_RECT rect = { left, 0, right, static_cast<LONG>(ch) };
        ID3D12DescriptorHeap *heaps[] = { g_split_heap_gpu };
        h.list->SetDescriptorHeaps(1, heaps);
        h.list->ClearUnorderedAccessViewFloat(
            g_split_heap_gpu->GetGPUDescriptorHandleForHeapStart(),
            g_split_heap_cpu->GetCPUDescriptorHandleForHeapStart(),
            v.output, accent, 1, &rect);
    }
    const UINT64 fence = EndCommands();
    return WaitFenceValue(h.fence, fence, 30000);
}

static bool DownloadVideoFrame(VideoState &v, std::vector<BYTE> &packed,
                               ID3D12Resource *src = nullptr,
                               D3D12_RESOURCE_STATES src_before = D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                               D3D12_RESOURCE_STATES src_after = D3D12_RESOURCE_STATE_UNORDERED_ACCESS)
{
    if (src == nullptr) src = v.output;
    if (!BeginCommands()) return false;
    D3D12_RESOURCE_BARRIER a = Transition(src, src_before,
                                           D3D12_RESOURCE_STATE_COPY_SOURCE);
    h.list->ResourceBarrier(1, &a);
    D3D12_TEXTURE_COPY_LOCATION s = {}, d = {};
    s.pResource = src; s.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    d.pResource = v.readback; d.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    d.PlacedFootprint = v.out_fp;
    h.list->CopyTextureRegion(&d, 0, 0, 0, &s, nullptr);
    D3D12_RESOURCE_BARRIER b = Transition(src, D3D12_RESOURCE_STATE_COPY_SOURCE,
                                           src_after);
    h.list->ResourceBarrier(1, &b);
    const UINT64 fence = EndCommands();
    if (!WaitFenceValue(h.fence, fence, 60000)) return false;

    BYTE *mapped = nullptr;
    D3D12_RANGE read_range = { 0, static_cast<SIZE_T>(v.out_fp.Footprint.RowPitch) * v.hgt };
    if (FAILED(v.readback->Map(0, &read_range, reinterpret_cast<void **>(&mapped)))) return false;
    const UINT ow = v.upscale ? v.full_w : v.w;
    const UINT oh = v.upscale ? v.full_h : v.hgt;
    packed.resize(static_cast<size_t>(ow) * oh * 4);
    for (UINT y = 0; y < oh; ++y)
        memcpy(packed.data() + static_cast<size_t>(y) * ow * 4,
               mapped + static_cast<size_t>(y) * v.out_fp.Footprint.RowPitch,
               static_cast<size_t>(ow) * 4);
    D3D12_RANGE written = { 0, 0 };
    v.readback->Unmap(0, &written);
    return true;
}

static bool ReShadeHasFeature18()
{
    char path[MAX_PATH] = {};
    GetModuleFileNameA(nullptr, path, MAX_PATH);
    if (char *s = strrchr(path, '\\')) strcpy_s(s + 1, MAX_PATH - (s + 1 - path), "ReShade.log");
    HANDLE file = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                              nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;
    const DWORD n = GetFileSize(file, nullptr);
    std::vector<char> data(n > 0 ? static_cast<size_t>(n) + 1 : 1, 0);
    DWORD got = 0;
    if (n > 0) ReadFile(file, data.data(), n, &got, nullptr);
    CloseHandle(file);
    return strstr(data.data(), "feature 18 created") != nullptr &&
           strstr(data.data(), "inline feature 18 evaluation succeeded") != nullptr;
}

// Reads the next 24-byte message header. Returns:
//   1 = frame ready (color_ptr/mv_ptr point at the pixels, either at the
//       std::vectors filled from the pipe or straight into the shared mapping),
//   2 = resize command read into rc,
//   3 = shared-memory handover read into sc,
//   4 = presentation-window command read into wc,
//   5 = motion-size command read into mc,
//   6 = desktop-capture command read into dc,
//   7 = gray-downsample command read into gc (88 bytes: 24 hdr + 64 name),
//   0 = EOF/error.
static int ReadVideoMessage(VideoState &v, VideoFrameHeader &fh, std::vector<BYTE> &color,
                            std::vector<BYTE> &mv, VideoResizeCmd &rc, VideoShmCmd &sc,
                            VideoWindowCmd &wc, VideoMotionCmd &mc, VideoDdaCmd &dc,
                            VideoGrayCmd &gc,
                            const BYTE **color_ptr, const BYTE **mv_ptr)
{
    if (!ReadExact(stdin, &fh, sizeof(fh))) return 0;
    if (fh.magic == FRAME_MAGIC)
    {
        const bool no_color = (fh.reserved & FRAME_FLAG_NO_COLOR) != 0 && g_dda_active;
        const size_t cw = v.upscale ? v.full_w : v.w;
        const size_t ch = v.upscale ? v.full_h : v.hgt;
        const size_t color_bytes = cw * ch * 4;
        // Уменьшенное поле движения (MOTS) занимает столько, сколько занимает,
        // а не work-разрешение — иначе разъедется разбор потока.
        const bool small_mv = (fh.reserved & FRAME_FLAG_MOTION_SMALL) != 0 && g_motion_w != 0;
        const size_t mv_bytes = small_mv
            ? static_cast<size_t>(g_motion_w) * g_motion_h * 4
            : static_cast<size_t>(v.w) * v.hgt * 4;
        if (no_color)
        {
            // DDA-режим: цвет воркер снимает сам; в пайпе только motion.
            *color_ptr = nullptr;
            mv.resize(mv_bytes);
            *mv_ptr = mv.data();
            return ReadExact(stdin, mv.data(), mv.size()) ? 1 : 0;
        }
        if ((fh.reserved & FRAME_FLAG_SHM) != 0)
        {
            // Pixels are already in the mapping; the pipe carried only this header.
            if (g_shm_base == nullptr || g_shm_motion_off < color_bytes ||
                g_shm_bytes < g_shm_motion_off + mv_bytes)
            {
                Log("[video] SHM frame %u but mapping is missing or too small "
                    "(mapped=%zu, need colour=%zu at 0, motion=%zu at %zu)",
                    fh.index, g_shm_bytes, color_bytes, mv_bytes, g_shm_motion_off);
                return 0;
            }
            *color_ptr = g_shm_base;
            *mv_ptr = g_shm_base + g_shm_motion_off;
            return 1;
        }
        color.resize(color_bytes); mv.resize(mv_bytes);
        *color_ptr = color.data(); *mv_ptr = mv.data();
        return ReadExact(stdin, color.data(), color.size()) && ReadExact(stdin, mv.data(), mv.size()) ? 1 : 0;
    }
    if (fh.magic == SHM_MAGIC)
    {
        BYTE *p = reinterpret_cast<BYTE *>(&sc);
        memcpy(p, &fh, sizeof(fh));
        if (!ReadExact(stdin, p + sizeof(fh), sizeof(sc) - sizeof(fh))) return 0;
        return 3;
    }
    if (fh.magic == WINDOW_MAGIC)
    {
        // Same 24 bytes as the frame header — everything is already in fh.
        memcpy(&wc, &fh, sizeof(wc));
        return 4;
    }
    if (fh.magic == MOTION_MAGIC)
    {
        memcpy(&mc, &fh, sizeof(mc));
        return 5;
    }
    if (fh.magic == DDA_MAGIC)
    {
        memcpy(&dc, &fh, sizeof(dc));
        return 6;
    }
    if (fh.magic == GRAY_MAGIC)
    {
        // Первые 24 байта уже в fh (совпадает с VideoFrameHeader); дочитать
        // 64 байта имени — итого 88, как SHMI.
        BYTE *p = reinterpret_cast<BYTE *>(&gc);
        memcpy(p, &fh, sizeof(fh));
        if (!ReadExact(stdin, p + sizeof(fh), sizeof(gc) - sizeof(fh))) return 0;
        return 7;
    }
    if (fh.magic == RESIZE_MAGIC)
    {
        // The first 24 bytes of the 64-byte command already sit in fh; read the rest.
        BYTE *p = reinterpret_cast<BYTE *>(&rc);
        memcpy(p, &fh, sizeof(fh));
        if (!ReadExact(stdin, p + sizeof(fh), sizeof(rc) - sizeof(fh))) return 0;
        return 2;
    }
    return 0;
}

static void ReleaseVideoTextures(VideoState &v)
{
    // Дескрипторы шейдера ссылались на эти ресурсы — после освобождения
    // их надо перевыпустить (см. BindScaleDescriptors).
    if (v.mv.tex == g_scale_dst_bound) g_scale_dst_bound = nullptr;
    if (v.color.tex != nullptr) { v.color.tex->Release(); v.color.tex = nullptr; }
    if (v.color.upload != nullptr) { v.color.upload->Release(); v.color.upload = nullptr; }
    if (v.mv.tex != nullptr) { v.mv.tex->Release(); v.mv.tex = nullptr; }
    if (v.mv.upload != nullptr) { v.mv.upload->Release(); v.mv.upload = nullptr; }
    if (v.output != nullptr) { v.output->Release(); v.output = nullptr; }
    if (v.readback != nullptr) { v.readback->Release(); v.readback = nullptr; }
    v.inputs_ready = false;
}

// Forward declaration: определена ниже, вызывается в RunVideo при завершении.
static void CleanupVideoNgx();

// ---------------------------------------------------------------------------
// Профилирование фаз кадра. Задача: понять, куда уходит время, и в частности
// проверить гипотезу vblank — при 60 Гц бюджет 16.7 мс, и если Present пасует
// конвейер по кратным ему значениям, это видно по гистограмме, а не по
// среднему. Поэтому для Present считается ещё и распределение по корзинам.
// ---------------------------------------------------------------------------
static const char *kPhaseNames[PH_COUNT] =
    { "acq(ждём стол)", "dda(всего)", "upload", "eval", "present", "frame" };
static double g_ph_sum[PH_COUNT];
static double g_ph_max[PH_COUNT];
static unsigned g_ph_n[PH_COUNT];
// Корзины Present, мс: <5, 5-12, 12-20 (~1 vblank), 20-28, 28-40 (~2 vblank), 40+
static const double kPresentBins[] = { 5.0, 12.0, 20.0, 28.0, 40.0 };
static unsigned g_ph_bins[6];
static LARGE_INTEGER g_qpf;
static UINT64 g_ph_tick;

// Профилировщик включается переменной окружения NS_PHASE=1. По умолчанию
// выключен: иначе он сорит в лог строкой каждые 2 секунды всю сессию.
static int g_phase_on = -1;

static bool PhaseEnabled()
{
    if (g_phase_on < 0)
    {
        char buf[8] = {};
        const DWORD got = GetEnvironmentVariableA("NS_PHASE", buf, sizeof(buf));
        g_phase_on = (got > 0 && buf[0] == '1') ? 1 : 0;
        if (g_phase_on) Log("[phase] профилировщик фаз включён (NS_PHASE=1)");
    }
    return g_phase_on == 1;
}

static double PhaseNow()
{
    if (g_qpf.QuadPart == 0) QueryPerformanceFrequency(&g_qpf);
    LARGE_INTEGER t;
    QueryPerformanceCounter(&t);
    return static_cast<double>(t.QuadPart) * 1000.0 / static_cast<double>(g_qpf.QuadPart);
}

static void PhaseAdd(int idx, double t0)
{
    if (!PhaseEnabled()) return;
    const double ms = PhaseNow() - t0;
    g_ph_sum[idx] += ms;
    if (ms > g_ph_max[idx]) g_ph_max[idx] = ms;
    ++g_ph_n[idx];
    if (idx == PH_PRESENT)
    {
        int b = 0;
        while (b < 5 && ms >= kPresentBins[b]) ++b;
        ++g_ph_bins[b];
    }
}

static void PhaseReport(bool bypass)
{
    if (!PhaseEnabled()) return;
    const UINT64 now = GetTickCount64();
    if (g_ph_tick == 0) { g_ph_tick = now; return; }
    if (now - g_ph_tick < 2000) return;
    g_ph_tick = now;

    char line[640];
    int off = _snprintf_s(line, sizeof(line), _TRUNCATE, "[phase] %s", bypass ? "bypass" : "NR");
    for (int i = 0; i < PH_COUNT && off > 0; ++i)
    {
        if (g_ph_n[i] == 0) continue;
        off += _snprintf_s(line + off, sizeof(line) - off, _TRUNCATE, " | %s %.1f/%.1f",
                           kPhaseNames[i], g_ph_sum[i] / g_ph_n[i], g_ph_max[i]);
        g_ph_sum[i] = 0.0; g_ph_max[i] = 0.0; g_ph_n[i] = 0;
    }
    if (g_ts_n > 0)
    {
        _snprintf_s(line + off, sizeof(line) - off, _TRUNCATE,
                    " | eval на GPU %.1f/%.1f", g_ts_sum / g_ts_n, g_ts_max);
        g_ts_sum = 0.0; g_ts_max = 0.0; g_ts_n = 0;
    }
    Log("%s (среднее/макс, мс)", line);
    Log("[phase] present по корзинам мс: <5=%u 5-12=%u 12-20=%u 20-28=%u 28-40=%u 40+=%u",
        g_ph_bins[0], g_ph_bins[1], g_ph_bins[2], g_ph_bins[3], g_ph_bins[4], g_ph_bins[5]);
    for (int b = 0; b < 6; ++b) g_ph_bins[b] = 0;
}

static int RunVideo()
{
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
    VideoHeader vh = {};
    // Accept both the legacy 56-byte header (magic 0x32563544, no full_w/full_h)
    // and the extended 64-byte header (magic 0x33563544, with full_w/full_h).
    BYTE raw[sizeof(VideoHeader)] = {};
    if (!ReadExact(stdin, raw, VIDEO_HEADER_LEGACY_SIZE)) { Log("[video] no stream header"); return 2; }
    memcpy(&vh, raw, VIDEO_HEADER_LEGACY_SIZE);
    if (vh.magic == VIDEO_MAGIC_EXT)
    {
        if (!ReadExact(stdin, raw + VIDEO_HEADER_LEGACY_SIZE, sizeof(VideoHeader) - VIDEO_HEADER_LEGACY_SIZE))
        { Log("[video] truncated extended header"); return 2; }
        memcpy(&vh, raw, sizeof(VideoHeader));
    }
    if ((vh.magic != VIDEO_MAGIC && vh.magic != VIDEO_MAGIC_EXT) || vh.width < 64 || vh.height < 64 ||
        vh.width > 7680 || vh.height > 4320)
    { Log("[video] invalid stream header (magic=0x%08X)", vh.magic); return 2; }
    const bool upscale = (vh.magic == VIDEO_MAGIC_EXT) && vh.full_w > 0 && vh.full_h > 0 &&
                         (vh.full_w != vh.width || vh.full_h != vh.height);
    if (upscale && (vh.full_w < vh.width || vh.full_h < vh.height || vh.full_w > 7680 || vh.full_h > 4320))
    { Log("[video] invalid full-res size %ux%u", vh.full_w, vh.full_h); return 2; }
    g_video_options = vh;
    const bool live = g_live_force || (vh.frame_count == 0);
    std::string full_note;
    if (upscale) full_note = " (full-res frames " + std::to_string(vh.full_w) + "x" + std::to_string(vh.full_h) + ")";
    Log("[video] stream %ux%u%s, %s, warmup=%u", vh.width, vh.height, full_note.c_str(),
        live ? "LIVE (unbounded)" : "bounded", vh.warmup);

    VideoState v;
    if (!CreateVideoResources(v, vh.width, vh.height, upscale ? vh.full_w : 0, upscale ? vh.full_h : 0))
    { Log("[video] resource creation failed"); return 3; }
    int flags = NVSDK_NGX_DLSS_Feature_Flags_MVLowRes | NVSDK_NGX_DLSS_Feature_Flags_AutoExposure |
                NVSDK_NGX_DLSS_Feature_Flags_DepthInverted;
    NVSDK_NGX_Result create_result = NVSDK_NGX_Result_Fail;
    if (!CreateFeature(vh.width, vh.height, flags, &create_result,
                       upscale ? vh.full_w : 0, upscale ? vh.full_h : 0)) return 4;

    VideoFrameHeader fh = {};
    std::vector<BYTE> color, mv, output;
    bool warmup_done = false;
    uint32_t frame = 0;
    for (;;)
    {
        VideoResizeCmd rc = {};
        VideoShmCmd sc = {};
        VideoWindowCmd wc = {};
        VideoMotionCmd mc = {};
        VideoDdaCmd dc = {};
        VideoGrayCmd gc = {};
        const BYTE *color_ptr = nullptr;
        const BYTE *mv_ptr = nullptr;
        const int msg = ReadVideoMessage(v, fh, color, mv, rc, sc, wc, mc, dc, gc, &color_ptr, &mv_ptr);
        if (msg == 0)
        {
            if (live)
            {
                Log("[live] input stream closed after %u frames; %u direct evaluations", frame, g_eval_count);
                // Явная очистка NGX-ресурсов ПЕРЕД выходом: без ReleaseFeature/
                // Shutdown1 GPU-ресурсы (D3D12 device, NGX) освобождаются только
                // при смерти процесса, и быстрый рестарт нового воркера
                // конфликтует с остатками старого (exit 127 / зависание).
                CleanupVideoNgx();
                CloseSharedInput();
                ClosePresent();
                CloseMotionScaler();
                CloseDda();
                CloseGray();
                return 0;
            }
            Log("[video] truncated frame %u", frame); return 5;
        }
        if (msg == 2)
        {
            // RNSZ: reconfigure the feature at a new work size WITHOUT restarting
            // the process. The client keeps the same stream; the next frame after
            // the RACK is interpreted at the new sizes.
            if (rc.width < 64 || rc.height < 64 || rc.width > 7680 || rc.height > 4320)
            {
                Log("[video] RNSZ rejected: invalid work size %ux%u", rc.width, rc.height);
                VideoResizeAck bad = { RESIZE_ACK_MAGIC, 0u, 0xBAD00005u, 0u, fh.pts };
                if (!WriteExact(stdout, &bad, sizeof(bad))) return 10;
                continue;
            }
            const bool rup = rc.full_w > 0 && rc.full_h > 0 &&
                             (rc.full_w != rc.width || rc.full_h != rc.height);
            if (rup && (rc.full_w < rc.width || rc.full_h < rc.height ||
                        rc.full_w > 7680 || rc.full_h > 4320))
            {
                Log("[video] RNSZ rejected: invalid full size %ux%u", rc.full_w, rc.full_h);
                VideoResizeAck bad = { RESIZE_ACK_MAGIC, 0u, 0xBAD00005u, 0u, fh.pts };
                if (!WriteExact(stdout, &bad, sizeof(bad))) return 10;
                continue;
            }
            Log("[video] RNSZ: work %ux%u -> %ux%u (full %ux%u), warmup=%u",
                v.w, v.hgt, rc.width, rc.height, rc.full_w, rc.full_h, rc.warmup);
            // 1. Drain the GPU: the old feature must be idle before release.
            WaitFenceValue(h.fence, h.fence_value, 2000);
            // 2. Release the old feature and textures.
            SafeReleaseFeature(h.feature);
            h.feature = nullptr;
            ReleaseVideoTextures(v);
            // 3. New options (profile/params travel with the command).
            // VideoResizeCmd has the same packed layout as VideoHeader.
            memcpy(&g_video_options, &rc, sizeof(g_video_options));
            // 4. Recreate textures + feature at the new sizes.
            if (!CreateVideoResources(v, rc.width, rc.height, rup ? rc.full_w : 0, rup ? rc.full_h : 0))
            {
                Log("[video] RNSZ: resource creation failed at %ux%u", rc.width, rc.height);
                VideoResizeAck bad = { RESIZE_ACK_MAGIC, 0u, 0x7FFFFFFFu, 0u, fh.pts };
                if (!WriteExact(stdout, &bad, sizeof(bad))) return 3;
            }
            NVSDK_NGX_Result rr = NVSDK_NGX_Result_Fail;
            if (!CreateFeature(rc.width, rc.height, flags, &rr, rup ? rc.full_w : 0, rup ? rc.full_h : 0))
            {
                Log("[video] RNSZ: feature create failed at %ux%u", rc.width, rc.height);
                VideoResizeAck bad = { RESIZE_ACK_MAGIC, 0u, static_cast<uint32_t>(rr), 0u, fh.pts };
                if (!WriteExact(stdout, &bad, sizeof(bad))) return 4;
            }
            warmup_done = false;   // re-warm at the new resolution
            VideoResizeAck ack = { RESIZE_ACK_MAGIC, 1u, static_cast<uint32_t>(rr), 0u, fh.pts };
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            Log("[video] RNSZ applied: feature ready at %ux%u", rc.width, rc.height);
            continue;
        }
        if (msg == 3)
        {
            // SHMI: open the client's named section. From now on frames whose
            // header carries FRAME_FLAG_SHM are uploaded straight from it.
            sc.name[sizeof(sc.name) - 1] = '\0';
            CloseSharedInput();
            const size_t need = static_cast<size_t>(sc.color_bytes) +
                                static_cast<size_t>(sc.motion_bytes);
            uint32_t ok = 0;
            if (sc.color_bytes == 0 || sc.motion_bytes == 0 || need > (size_t)1 << 31)
                Log("[video] SHMI rejected: implausible sizes colour=%u motion=%u",
                    sc.color_bytes, sc.motion_bytes);
            else
            {
                g_shm_handle = OpenFileMappingA(FILE_MAP_READ, FALSE, sc.name);
                if (g_shm_handle == nullptr)
                    Log("[video] SHMI: OpenFileMapping('%s') failed, err=%lu",
                        sc.name, GetLastError());
                else
                {
                    g_shm_base = static_cast<const BYTE *>(
                        MapViewOfFile(g_shm_handle, FILE_MAP_READ, 0, 0, need));
                    if (g_shm_base == nullptr)
                    {
                        Log("[video] SHMI: MapViewOfFile(%zu) failed, err=%lu", need, GetLastError());
                        CloseHandle(g_shm_handle);
                        g_shm_handle = nullptr;
                    }
                    else
                    {
                        g_shm_bytes = need;
                        g_shm_motion_off = sc.color_bytes;
                        ok = 1;
                        Log("[video] SHMI: mapped '%s', %zu bytes (motion at +%zu)",
                            sc.name, need, g_shm_motion_off);
                    }
                }
            }
            VideoShmAck ack = { SHM_ACK_MAGIC, ok, 0u, 0u, sc.pts };
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            continue;
        }
        if (msg == 4)
        {
            // WNDO: raise (or tear down) the overlay the worker presents into.
            uint32_t ok = 0;
            if ((wc.flags & WINDOW_FLAG_DISABLE) != 0 || wc.width == 0 || wc.height == 0)
            {
                ClosePresent();
                Log("[present] overlay closed; results go back to the client");
                ok = 1;
            }
            else
                ok = OpenPresent(wc.width, wc.height, wc.flags) ? 1u : 0u;
            VideoWindowAck ack = { WINDOW_ACK_MAGIC, ok, 0u, 0u, wc.pts };
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            continue;
        }
        if (msg == 5)
        {
            // MOTS: с этого момента поле движения приходит уменьшенным,
            // растягиваем его на GPU.
            const uint32_t ok = OpenMotionScaler(mc.width, mc.height) ? 1u : 0u;
            VideoMotionAck ack = { MOTION_ACK_MAGIC, ok, 0u, 0u, mc.pts };
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            continue;
        }
        if (msg == 6)
        {
            // DDA1: взять захват на себя (width==0 — выключить, вернуться к pipe).
            uint32_t ok = 0;
            if (dc.width == 0 || dc.height == 0)
            {
                CloseDda();
                ok = 1;
            }
            else
                ok = OpenDda(dc.width, dc.height) ? 1u : 0u;
            Log("[video] DDA1 %s (%ux%u)", ok ? "OK" : "FAIL", dc.width, dc.height);
            VideoDdaAck ack = { DDA_ACK_MAGIC, ok, 0u, 0u, dc.pts };
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            continue;
        }
        if (msg == 7)
        {
            // GRAY: клиент передал обратный маппинг для luminance-кадров.
            uint32_t ok = 0;
            if (gc.width == 0 || gc.height == 0)
            {
                CloseGray();
                ok = 1;
            }
            else
                ok = OpenGray(gc) ? 1u : 0u;
            Log("[video] GRAY %s (%ux%u)", ok ? "OK" : "FAIL", gc.width, gc.height);
            VideoGrayAck ack = { GRAY_ACK_MAGIC, ok, 0u, 0u, gc.pts };
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            continue;
        }
        const double t_frame = PhaseNow();
        if (g_dda_active)
        {
            // DDA-режим: цвет берём из Desktop Duplication прямо на GPU,
            // motion — из присланного кадра (клиент продолжает шлaть пары).
            const double t_dda = PhaseNow();
            const bool got = DdaGrab(v);
            PhaseAdd(PH_DDA, t_dda);
            if (!got && !g_dda_ready)
            {
                // Ещё ни одного реального кадра с рабочего стола: держать
                // парность протокола пустым OUT1 и ждать изменений экрана,
                // НЕ запуская NGX на пустом цвете (evaluate на нулe виснет).
                VideoResultHeader empty = { OUT_MAGIC, fh.index, 1u, 0u, g_last_eval_result, fh.pts };
                if (!WriteExact(stdout, &empty, sizeof(empty))) return 10;
                continue;
            }
            const double t_up = PhaseNow();
            const bool up_ok = UploadMotionOnly(v, mv_ptr,
                                  (fh.reserved & FRAME_FLAG_MOTION_SMALL) != 0 && g_motion_w != 0);
            PhaseAdd(PH_UPLOAD, t_up);
            if (!up_ok) return 6;
        }
        else
        {
            const double t_up = PhaseNow();
            const bool up_ok = UploadVideoFrame(v, color_ptr, mv_ptr,
                                   (fh.reserved & FRAME_FLAG_MOTION_SMALL) != 0 && g_motion_w != 0);
            PhaseAdd(PH_UPLOAD, t_up);
            if (!up_ok) return 6;
        }
        if (!warmup_done)
        {
            const uint32_t warmup = (std::min)(240u, (std::max)(1u, g_video_options.warmup));
            for (uint32_t i = 0; i < warmup; ++i)
            {
                if (!EvaluateVideo(v, i == 0 ? 1 : 0)) return 7;
            }
            warmup_done = true;
            Log("[pure] direct feature 18 confirmed after %u discarded warmup frames", warmup);
        }
        const bool bypass = (fh.reserved & FRAME_FLAG_BYPASS) != 0;
        if (!bypass)
        {
            const double t_eval = PhaseNow();
            const bool ev_ok = EvaluateVideo(v, (frame == 0 || fh.reset != 0) ? 1 : 0);
            PhaseAdd(PH_EVAL, t_eval);
            if (!ev_ok) return 9;
            if ((fh.reserved & FRAME_FLAG_SPLIT) != 0)
            {
                // В bypass шторка бессмысленна: там обе половины — сырой
                // захват, поэтому только на обработанном кадре.
                const UINT cw = v.upscale ? v.full_w : v.w;
                if (!SplitCompose(v, SplitXFromFlags(fh.reserved, cw))) return 9;
            }
        }
        if (PresentModeActive(v))
        {
            // The overlay shows the result; the client gets an empty OUT1 as
            // the "frame done" signal and never sees the pixels -- unless it
            // explicitly asked for this one frame (screenshot).
            const double t_pres = PhaseNow();
            // NR OFF: показать сырой захват (v.color уже в full-res) —
            // NGX evaluate пропущен, но конвейер жив (окно, HUD).
            const bool pres_ok = bypass ? PresentBypass(v) : PresentFrame(v);
            PhaseAdd(PH_PRESENT, t_pres);
            if (!pres_ok) return 9;
            // Пиксели клиенту (скриншот/запись) — и в bypass-режиме: для
            // записи нужен ровно тот кадр, что виден (сырой захват).
            if ((fh.reserved & FRAME_FLAG_WANT_PIXELS) != 0)
            {
                bool dl_ok = false;
                if (bypass)
                    dl_ok = DownloadVideoFrame(v, output, v.color.tex,
                                               D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                               D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
                else
                    dl_ok = DownloadVideoFrame(v, output);
                if (!dl_ok) return 9;
                VideoResultHeader out = { OUT_MAGIC, fh.index, 1u,
                                          static_cast<uint32_t>(output.size()),
                                          g_last_eval_result, fh.pts };
                if (!WriteExact(stdout, &out, sizeof(out)) ||
                    !WriteExact(stdout, output.data(), output.size())) return 10;
            }
            else
            {
                VideoResultHeader out = { OUT_MAGIC, fh.index, 1u, 0u, g_last_eval_result, fh.pts };
                if (!WriteExact(stdout, &out, sizeof(out))) return 10;
            }
        }
        else
        {
            if (!DownloadVideoFrame(v, output)) return 9;
            VideoResultHeader out = { OUT_MAGIC, fh.index, 1u, static_cast<uint32_t>(output.size()), g_last_eval_result, fh.pts };
            if (!WriteExact(stdout, &out, sizeof(out)) || !WriteExact(stdout, output.data(), output.size())) return 10;
        }
        PhaseAdd(PH_FRAME, t_frame);
        PhaseReport(bypass);
        if (frame < 3 || ((frame + 1) % 30) == 0)
            Log("[video] delivered frame %u%s", frame + 1, live ? " (live)" : "");
        ++frame;
        if (!live && frame >= vh.frame_count) break;
    }
    Log("[pure] complete: %u frames delivered, %u direct evaluations", frame, g_eval_count);
    CleanupVideoNgx();
    CloseSharedInput();
    ClosePresent();
    CloseMotionScaler();
    CloseDda();
    CloseGray();
    return 0;
}

// ---------------------------------------------------------------------------
// CleanupVideoNgx: освободить NGX-ресурсы video/live-режима перед выходом.
// Без этого GPU-ресурсы (D3D12 device, NGX feature) освобождаются только
// при смерти процесса — быстрый рестарт воркера конфликтует с остатками
// (exit 127 / зависание на кадре 0). Вызывается при штатном завершении.
// ---------------------------------------------------------------------------
static void CleanupVideoNgx()
{
    if (h.feature != nullptr)
    {
        SafeReleaseFeature(h.feature);
        h.feature = nullptr;
        Log("[video] feature released");
    }
    if (h.params != nullptr)
    {
        NVSDK_NGX_D3D12_DestroyParameters(h.params);
        h.params = nullptr;
    }
    if (h.ngx_inited)
    {
        NVSDK_NGX_D3D12_Shutdown1(h.dev);
        h.ngx_inited = false;
        Log("[video] NGX shutdown");
    }
}

// ---------------------------------------------------------------------------
// Serve mode: the real pipe server for a 32-bit game
// ---------------------------------------------------------------------------

static bool ReadFull(HANDLE pipe, void *buf, DWORD len)
{
    DWORD got = 0;
    return ReadFile(pipe, buf, len, &got, nullptr) && got == len;
}

static int Serve(DWORD game_pid)
{
    char name[128];
    sprintf_s(name, FEED_PIPE_FMT, static_cast<unsigned long>(game_pid));
    HANDLE pipe = CreateNamedPipeA(name, PIPE_ACCESS_DUPLEX, PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                                   1, 1024, 1024, 0, nullptr);
    if (pipe == INVALID_HANDLE_VALUE) { Log("[host] CreateNamedPipe failed %lu", GetLastError()); return 1; }
    Log("[host] serving on %s", name);
    if (!ConnectNamedPipe(pipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED)
    { Log("[host] ConnectNamedPipe failed %lu", GetLastError()); return 1; }

    FeedHello hello = {};
    if (!ReadFull(pipe, &hello, sizeof(hello)) || hello.magic != FEED_IPC_MAGIC)
    { Log("[host] bad hello"); return 1; }
    FeedHelloAck ack = { FEED_IPC_MAGIC, FEED_IPC_VERSION };
    DWORD put = 0;
    WriteFile(pipe, &ack, sizeof(ack), &put, nullptr);
    Log("[host] game pid %u connected (protocol v%u)", hello.pid, hello.version);

    HANDLE hgame = OpenProcess(PROCESS_DUP_HANDLE, FALSE, hello.pid);
    if (hgame == nullptr) { Log("[host] OpenProcess failed %lu", GetLastError()); return 1; }

    // Shared fences live for the whole session.
    HANDLE hin = nullptr, hout = nullptr;
    h.dev->CreateFence(0, D3D12_FENCE_FLAG_SHARED, __uuidof(ID3D12Fence), reinterpret_cast<void **>(&h.fence_in));
    h.dev->CreateFence(0, D3D12_FENCE_FLAG_SHARED, __uuidof(ID3D12Fence), reinterpret_cast<void **>(&h.fence_out));
    if (h.fence_in == nullptr || h.fence_out == nullptr ||
        FAILED(h.dev->CreateSharedHandle(h.fence_in, nullptr, GENERIC_ALL, nullptr, &hin)) ||
        FAILED(h.dev->CreateSharedHandle(h.fence_out, nullptr, GENERIC_ALL, nullptr, &hout)))
    { Log("[host] shared fence creation failed"); return 1; }

    HANDLE game_in = nullptr, game_out = nullptr;
    DuplicateHandle(GetCurrentProcess(), hin, hgame, &game_in, 0, FALSE, DUPLICATE_SAME_ACCESS);
    DuplicateHandle(GetCurrentProcess(), hout, hgame, &game_out, 0, FALSE, DUPLICATE_SAME_ACCESS);

    int flags_active = 0;
    bool transport_only = false;
    float mvsx = 1.0f, mvsy = 1.0f;
    // The DLSS 5 add-on arms its NGX hooks ~150 ms after NGX init; the first create must
    // not race that (a 15 ms miss latched STANDBY in Blacklist), so hold it briefly.
    UINT64 hold_until = GetTickCount64() + 800;
    UINT64 evaluated  = 0;
    bool   warm_done  = g_renodx_lazy;   // v45+ adopts missed creates on its own
    int    build_fails = 0;

    for (;;)
    {
        // Peek the next message type by size: Build (big) vs FrameMsg (small).
        // The pipe is byte-mode from a single writer, so read the smaller header
        // first and decide -- FeedFrameMsg and FeedBuild share no prefix, so the
        // client precedes every message with a 1-byte tag instead.
        //
        // A plain blocking ReadFile here starves the message pump (and Present)
        // whenever the game stops feeding frames -- paused, loading, a menu -- and
        // Windows shows the host window as "Not Responding". Poll instead, so the
        // window (and its ReShade overlay) stays alive and clickable at all times.
        BYTE tag = 0;
        bool tag_read = false;
        for (;;)
        {
            DWORD avail = 0;
            if (!PeekNamedPipe(pipe, nullptr, 0, nullptr, &avail, nullptr)) break;   // pipe broken
            if (avail > 0) { tag_read = ReadFull(pipe, &tag, 1); break; }
            PumpPresent();
            Sleep(8);
        }
        if (!tag_read) { Log("[host] pipe closed by the game"); break; }

        if (tag == 'B')
        {
            FeedBuild b = {};
            if (!ReadFull(pipe, &b, sizeof(b))) break;
            Log("[host] build: %ux%u color=%u output=%u hdr=%d inverted=%d", b.width, b.height,
                b.color_fmt, b.output_fmt, b.hdr, b.depth_inverted);

            // Tear down the old set.
            SafeReleaseFeature(h.feature);
            h.feature = nullptr;
            for (int i = 0; i < FEED_SLOTS; ++i)
                if (h.tex[i] != nullptr) { h.tex[i]->Release(); h.tex[i] = nullptr; }

            // Open the game's textures (duplicate the handles out of the game).
            bool ok = true;
            for (int i = 0; i < FEED_SLOTS && ok; ++i)
            {
                HANDLE local = nullptr;
                if (!DuplicateHandle(hgame, reinterpret_cast<HANDLE>(static_cast<uintptr_t>(b.tex[i])),
                                     GetCurrentProcess(), &local, 0, FALSE, DUPLICATE_SAME_ACCESS))
                { Log("[host] DuplicateHandle(tex %d) failed %lu", i, GetLastError()); ok = false; break; }
                HRESULT hr = h.dev->OpenSharedHandle(local, __uuidof(ID3D12Resource),
                                                     reinterpret_cast<void **>(&h.tex[i]));
                CloseHandle(local);
                if (FAILED(hr)) { Log("[host] OpenSharedHandle(tex %d) failed 0x%08X", i, hr); ok = false; }
            }

            NVSDK_NGX_Result rf = NVSDK_NGX_Result_Fail;
            if (ok)
            {
                h.width = b.width; h.height = b.height;
                h.color_fmt  = static_cast<DXGI_FORMAT>(b.color_fmt);
                h.output_fmt = static_cast<DXGI_FORMAT>(b.output_fmt);
                mvsx = b.mv_scale_x; mvsy = b.mv_scale_y;
                transport_only = b.transport != 0;
                flags_active = NVSDK_NGX_DLSS_Feature_Flags_MVLowRes | NVSDK_NGX_DLSS_Feature_Flags_AutoExposure;
                if (b.depth_inverted) flags_active |= NVSDK_NGX_DLSS_Feature_Flags_DepthInverted;
                if (b.hdr)            flags_active |= NVSDK_NGX_DLSS_Feature_Flags_IsHDR;
                if (b.flags_override >= 0) flags_active = b.flags_override;

                if (transport_only)
                {
                    rf = static_cast<NVSDK_NGX_Result>(1);   // no NGX in the loop at all
                    Log("[host] transport-only mode: Color will be copied to Output, no evaluate");
                }
                else
                {
                    const UINT64 now = GetTickCount64();
                    if (now < hold_until) Sleep(static_cast<DWORD>(hold_until - now));  // hook-arming grace
                    ok = CreateFeature(b.width, b.height, flags_active, &rf);
                    hold_until = GetTickCount64() + 1000;   // next create not before +1 s

                    if (ok) build_fails = 0;
                    else if (++build_fails >= 2 && ReinitNgx())
                    {
                        Log("[host] retrying the create after an NGX reinit");
                        ok = CreateFeature(b.width, b.height, flags_active, &rf);
                        if (ok) build_fails = 0;
                    }
                }
            }

            evaluated = 0;
            warm_done = transport_only || g_renodx_lazy;   // no warm-up without NGX / with v45+

            FeedBuildAck back = {};
            back.ok         = ok ? 1 : 0;
            back.ngx_result = static_cast<uint32_t>(rf);
            back.fence_in   = reinterpret_cast<uint64_t>(game_in);
            back.fence_out  = reinterpret_cast<uint64_t>(game_out);
            WriteFile(pipe, &back, sizeof(back), &put, nullptr);
        }
        else if (tag == 'F')
        {
            FeedFrameMsg fm = {};
            if (!ReadFull(pipe, &fm, sizeof(fm))) break;
            if (h.feature == nullptr && !transport_only) { h.fence_out->Signal(fm.n); continue; }

            if (!WaitFenceValue(h.fence_in, fm.n, 2000))
            { Log("[host] frame %llu: in-fence never arrived", (unsigned long long)fm.n); h.fence_out->Signal(fm.n); continue; }
            h.queue->Wait(h.fence_in, fm.n);   // belt and braces on the GPU timeline

            bool done = false;
            if (transport_only)
            {
                if (BeginCommands())
                {
                    // Deliberately copy only the LEFT half: a split screen in the game is
                    // unambiguous visual proof that the host's output reaches the screen.
                    D3D12_TEXTURE_COPY_LOCATION src = {}, dst = {};
                    src.pResource = h.tex[FEED_COLOR];
                    src.Type      = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
                    dst.pResource = h.tex[FEED_OUTPUT];
                    dst.Type      = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
                    D3D12_BOX box = { 0, 0, 0, h.width / 2, h.height, 1 };
                    h.list->CopyTextureRegion(&dst, 0, 0, 0, &src, &box);
                    EndCommands();
                    done = true;
                }
            }
            else
                done = Evaluate(h.tex[FEED_COLOR], h.tex[FEED_OUTPUT], h.tex[FEED_DEPTH], h.tex[FEED_MV],
                                h.width, h.height, fm.reset ? 1 : 0, mvsx, mvsy);

            if (done)
            {
                h.queue->Signal(h.fence_out, fm.n);
                // One warm-up re-create per build: the DLSS 5 add-on misses the very first
                // create (STANDBY latch) when its hooks armed a moment too late.
                if (!warm_done && ++evaluated >= 180)
                {
                    warm_done = true;
                    Log("[host] warm-up: re-creating the feature once");
                    WaitFenceValue(h.fence, h.fence_value, 2000);
                    NVSDK_NGX_Handle *old = h.feature;
                    h.feature = nullptr;
                    NVSDK_NGX_Result rr = NVSDK_NGX_Result_Fail;
                    if (CreateFeature(h.width, h.height, flags_active, &rr)) SafeReleaseFeature(old);
                    else { h.feature = old; Log("[host] keeping the previous feature"); }
                }
            }
            else
                h.fence_out->Signal(fm.n);     // CPU-signal so the game never hangs on us

            if (fm.n <= 3 || (fm.n % 1800) == 0)
                Log("[host] frame %llu evaluated", (unsigned long long)fm.n);
            PumpPresent();
        }
        else
        {
            Log("[host] unknown tag 0x%02X", tag);
            break;
        }
    }
    return 0;
}

// ---------------------------------------------------------------------------

int main(int argc, char **argv)
{
    // Per-monitor DPI awareness BEFORE any window exists. Without it, on a
    // 125% desktop a window asked for as 3840x2160 is created 4800x2700
    // physical and the overlay is stretched (measured: GetWindowRect returned
    // 4800x2700 while the screen is 3840x2160).
    if (HMODULE u32 = GetModuleHandleW(L"user32.dll"))
    {
        typedef BOOL(WINAPI * PFN_SetDpiCtx)(HANDLE);
        auto set_ctx = reinterpret_cast<PFN_SetDpiCtx>(
            GetProcAddress(u32, "SetProcessDpiAwarenessContext"));
        if (set_ctx == nullptr || !set_ctx(reinterpret_cast<HANDLE>(-4)))  // PER_MONITOR_AWARE_V2
            SetProcessDPIAware();
    }

    for (int i = 1; i < argc; ++i)
        if (strcmp(argv[i], "--video") == 0 || strcmp(argv[i], "--live") == 0) g_video_mode = true;

    GetModuleFileNameA(nullptr, g_log_path, MAX_PATH);
    if (char *s = strrchr(g_log_path, '\\'))
        strcpy_s(s + 1, MAX_PATH - (s + 1 - g_log_path), "dlss5-feed-host.log");
    if (!g_video_mode) { FILE *f = nullptr; if (fopen_s(&f, g_log_path, "w") == 0 && f) fclose(f); }

    Log("dlss5-feed-host64 (built %s %s)", __DATE__, __TIME__);

    bool  test = false, hide = false, video = false;
    DWORD pid = 0;
    for (int i = 1; i < argc; ++i)
    {
        if      (strcmp(argv[i], "--test") == 0) test = true;
        else if (strcmp(argv[i], "--video") == 0) video = true;
        else if (strcmp(argv[i], "--live") == 0) { video = true; g_live_force = true; }
        else if (strcmp(argv[i], "--hide") == 0) hide = true;
        else pid = static_cast<DWORD>(strtoul(argv[i], nullptr, 10));
    }
    if (!test && !video && pid == 0)
    {
        Log("usage: dlss5-worker --test | --video | --live | <game pid> [--hide]");
        return 1;
    }
    g_show_window = !test && !video && !hide; // video/probe hosts remain hidden

    if (!InitDisguise()) return 1;
    if (!InitNgx()) { Log("[host] NGX unavailable"); return 1; }

    if (test) return RunTest();
    if (video) return RunVideo();
    return Serve(pid);
}
