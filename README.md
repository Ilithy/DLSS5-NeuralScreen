# NeuralScreen

**DLSS 5 Neural Rendering (NGX feature 18) as a desktop-wide overlay for Windows.**

NeuralScreen captures the entire desktop, runs every frame through NVIDIA's
neural rendering (the same feature 18 used by DLSS 5 games), and draws the
result over the screen in a click-through overlay window.

```
screen capture -> motion guides -> NGX worker (D3D12) -> overlay on top of the desktop
   dxcam            cv2 DIS         native/nvngx.dll        pygame, click-through
```

> **Alpha.** This is the first public build. The pipeline works end-to-end,
> but performance is transport-bound (~19.5 FPS at 4K) and the NGX feature is
> limited to 2560×1440 work resolution. See "Known limitations".

## Requirements

- **Windows 11** (Desktop Duplication API + `WDA_EXCLUDEFROMCAPTURE`)
- **NVIDIA RTX** with DLSS 5 Neural Rendering support. Developed and tested
  on RTX 5070 Ti, driver 616.56, 4K (3840×2160)
- **Python 3.13** with packages from `requirements.txt`
- **`native/nvngx_dlssnr.dll`** — NVIDIA runtime (165 MB). Not in the repo
  (GitHub 100 MB file limit) — grab it from the release assets or from the
  driver/DLSS SDK and put it into `native/`
- To rebuild the worker — **MSVC 2022 Build Tools** (see "Building")

## Quick start

1. Download the release archive (or clone the repo and add
   `native/nvngx_dlssnr.dll` from the release assets).
2. Double-click `NeuralScreen.bat`. The launcher looks for Python in this
   order: `NEURALSCREEN_PYTHON` env var → `runtime\python.exe` next to the
   app → dev path → `python` from `PATH`. If `native/nvngx.dll` is missing,
   the launcher builds it automatically.
3. The console stays open on purpose: FPS and pipeline timings are printed
   there.

## Controls

| Key | Action |
|---|---|
| `Esc` | quit |
| `F9` | NR on/off (when off, the window hides and capture stops) |
| `F8` | settings window |
| `↑` / `↓` | processing scale ±0.05 |

Tray icon: left click — settings, right click — menu (NR on/off, scale,
quit). Settings window: profile, NR parameter sliders, scale, language
(ru/en) and a screenshot button.

The overlay window is click-through (`WS_EX_TRANSPARENT | WS_EX_LAYERED |
WS_EX_NOACTIVATE`) — clicks and focus go to the apps underneath, so control
is via global hotkeys. The window itself is excluded from capture
(`WDA_EXCLUDEFROMCAPTURE`), otherwise dxcam would capture its own output.

## config.json

| Field | Meaning |
|---|---|
| `monitor` | monitor index to capture |
| `width`, `height` | output resolution |
| `fullscreen` | borderless window over the whole monitor |
| `warmup` | NGX warmup frames at startup |
| `work_scale` | 0.1–1.0, NGX processing resolution relative to output |
| `profile` | `Faithful`, `Natural`, `Strong / Cinematic`, `Extreme / Overdrive` |
| `intensity`, `local_tone`, `local_structure`, `skin_structure` | `null` = take from profile |
| `lang` | `ru` / `en` |

## Architecture

Two processes. Python does capture, motion guides and output; the C++ worker
owns the D3D12 device and NGX. They talk over stdin/stdout with a binary
protocol:

| Message | Purpose |
|---|---|
| `D5V3` | stream header: sizes, profile, NR parameters |
| `FRM1` | frame: RGBA8 full-res + motion float16 work-res |
| `OUT1` | result: RGBA8 full-res |
| `RNSZ` / `RACK` | change work resolution on the fly, no process restart |

Two constraints that look like quirks but are mandatory:

- **The worker must be named `nvngx.dll`.** NGX Core returns
  `FAIL_PlatformError` on `Init_Ext` for any other process name. Verified
  experimentally.
- **Work resolution is capped at 2560×1440.** At 4K feature 18 goes silent:
  the worker hangs on frame zero in both legacy and upscale modes.

Scale changes go through `RNSZ` (~60 ms, the worker recreates the NGX feature
in-process; window and capture are untouched). If `RNSZ` fails — fallback to
a full worker process restart.

## Performance

Measured on RTX 5070 Ti, 4K, `work_scale` 0.5 (1920×1080):

```
grab 2.3 ms | guides 2.4 ms | send 9.5 ms | recv 31.8 ms | show 4.5 ms  = ~50 ms -> 19.5 FPS
```

Of that, **NGX itself is about 1 ms**. The rest is pixel movement: a 33 MB
frame crosses the CPU/GPU boundary four times per pass and goes through
stdio pipes twice. Breakdown (regression by work resolution at fixed full +
separate pipe benchmark):

| Item | Cost per frame |
|---|---|
| stdio pipes round-trip | ~14 ms |
| upload CPU→GPU, readback GPU→CPU, copies in the worker | ~25 ms |
| pygame output | ~4.5 ms |
| **NGX** | **~1 ms** |

The bottleneck is transport, not the neural network. The roadmap: move
output (and later capture) into the worker so pixels never leave the GPU.

## Building the worker

```
native\build-host.bat
```

Requires MSVC 2022 Build Tools at
`C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools`. The script
builds `dlss5-feed-host64.cpp` into `native/nvngx.dll`, linking against
`native/lib/Windows_x86_64/x64/nvsdk_ngx_d.lib`. NGX headers are in
`native/include/`.

The `native/nvngx.dll` artifact is not committed to the repo.

## Known limitations

- Windows only, NVIDIA RTX with DLSS 5 NR only.
- Work resolution capped at 2560×1440 (NGX constraint, see above).
- FPS is transport-bound (~19.5 at 4K), not GPU-bound.
- On a static desktop the optical flow is disabled by the `scene_score`
  threshold — motion is not computed, frames are cheaper.

## License

Experimental prototype. NVIDIA DLSS 5 Neural Rendering runtime
(`nvngx_dlssnr.dll`) is NVIDIA's proprietary redistributable.
