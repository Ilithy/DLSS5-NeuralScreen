# NeuralScreen

**DLSS 5 Neural Rendering (NGX feature 18) as a desktop-wide overlay for Windows.**

NeuralScreen captures the entire desktop, runs every frame through NVIDIA's
neural rendering (the same feature 18 used by DLSS 5 games), and draws the
result over the screen in a click-through overlay window.

```
screen capture -> motion guides -> NGX worker (D3D12) -> overlay over the desktop
  worker DDA       cv2 DIS 320x180     native/nvngx.dll      worker window + HUD
```

> **Alpha v0.1.0+.** The full pipeline lives on the GPU: capture (Desktop
> Duplication), neural pass and presentation all happen inside the worker;
> Python only computes the optical-flow guides (0.1–0.2 ms/frame). Measured
> on RTX 5070 Ti, 4K desktop, work 1920×1080: **32 FPS with NR on, 64 FPS in
> NR-off bypass mode**. See "Performance".

## Requirements

- **Windows 11** (Desktop Duplication API + `WDA_EXCLUDEFROMCAPTURE`)
- **NVIDIA RTX** with DLSS 5 Neural Rendering support. Developed and tested
  on RTX 5070 Ti, driver 616.56, 4K (3840×2160)
- **Python 3.13** with packages from `requirements.txt`
- **`native/nvngx_dlssnr.dll`** — NVIDIA runtime (165 MB). Not in the repo
  (GitHub 100 MB file limit) — grab it from the release assets and put it
  into `native/`
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

> Anticheat note: a process named `nvngx.dll` plus a fullscreen overlay may
> upset EAC/BattlEye/Vanguard. Do not run NeuralScreen in competitive online
> games.

## Controls

| Key | Action |
|---|---|
| `F9` | NR on/off. **Off is a bypass**: the overlay stays on screen showing the raw capture + HUD (no neural effect), and the desktop runs twice as fast. Everything hides only on real exit. |
| `F8` | settings window |
| `Ctrl+Alt+↑` / `Ctrl+Alt+↓` | processing scale ±0.05 |
| `Ctrl+Alt+Q` | quit |

Hotkeys are registered via `RegisterHotKey`, not polled: the system delivers
the keypress **only to us and not to the active app** — F9 inside a game
toggles NR and the game never sees the key. The flip side: while
NeuralScreen runs, F8 and F9 belong to it.

Arrows and quit are on `Ctrl+Alt` on purpose: bare arrows cannot be
registered (they would stop working system-wide) and a single-key quit is
too easy to press by accident.

Tray icon: left click — settings, right click — menu (NR on/off, scale,
quit). Settings window: profile, NR parameter sliders, scale, language
(ru/en), screenshot button and **Exit**.

Three ways to close the app and what they do:

| | Effect |
|---|---|
| `Ctrl+Alt+Q`, "Exit" in settings, "Exit" in tray | terminate: worker shut down, windows closed, no processes left |
| "Close" in settings / window X | only hides the settings window, the app keeps running |

The overlay is click-through (`WS_EX_TRANSPARENT | WS_EX_LAYERED |
WS_EX_NOACTIVATE` plus `HTTRANSPARENT`) — clicks and focus go to the apps
underneath. Windows are excluded from capture (`WDA_EXCLUDEFROMCAPTURE`),
otherwise Desktop Duplication would capture our own output and the pipeline
would feed on itself. Consequence: **external screen recorders (OBS,
Lightshot) cannot see the overlay** — use the Screenshot button in settings,
it grabs the frame from the worker instead.

## config.json

| Field | Meaning |
|---|---|
| `monitor` | monitor index for capture |
| `width`, `height` | output resolution |
| `fullscreen` | borderless fullscreen window |
| `warmup` | NGX warmup frames at start |
| `work_scale` | 0.25–1.0, NGX processing resolution relative to output |
| `profile` | `Faithful`, `Natural`, `Strong / Cinematic`, `Extreme / Overdrive` |
| `intensity`, `local_tone`, `local_structure`, `skin_structure` | `null` = take from profile |
| `lang` | `ru` / `en` |
| `worker_present` | worker shows the frame in its own window (`false` — pygame output) |
| `motion_on_gpu` | worker upscales the motion field (`false` — CPU) |
| `capture_in_worker` | worker captures the desktop itself (DDA, `false` — dxcam in Python) |

The three boolean flags are emergency switches: if something misbehaves on
another machine, `false` returns to the old path (which is fully intact).

## Architecture

Two processes. Python drives settings, optical-flow guides and the HUD; the
C++ worker owns the D3D12 device, the capture, NGX and the overlay window.
They talk over stdin/stdout with a binary protocol:

| Message | Purpose |
|---|---|
| `D5V3` | stream header: sizes, profile, NR parameters |
| `SHMI` / `SACK` | shared-memory section name for the input frame |
| `WNDO` / `WACK` | raise/close the worker's output window |
| `MOTS` / `MACK` | motion arrives at reduced size, worker upscales it on GPU |
| `DDA1` / `DACK` | worker takes over capture (Desktop Duplication), colour never touches the CPU |
| `GRAY` / `GAK` | worker writes AREA-downsampled luminance (320×180) into a back-mapping for the guides |
| `FRM1` | frame: header, then either payload (RGBA8 + motion) or "in shared memory" flag |
| `OUT1` | result: RGBA8 full-res, or `bytes=0` — the worker already presented it |
| `RNSZ` / `RACK` | change work resolution on the fly, no process restart |

**Capture.** On `DDA1` the worker opens Desktop Duplication on the GPU:
each frame is copied into a cross-device shared texture and swizzled to
RGBA. Python stops capturing entirely — `grab` and `guides` in the log drop
to 0.1 ms. Fallback (dxcam + full-frame send) stays intact.

**Guides.** The optical flow needs a small gray frame. On `GRAY` the worker
computes it with an honest block average (12×12 per cell at 4K → 320×180,
matching `cv2.INTER_AREA`; bilinear would alias text and break the flow) and
writes it into a named mapping. Python reads the flow input from there —
no 4K frame ever crosses the CPU again.

**Output.** On `WNDO` the worker raises its own borderless D3D12-swapchain
window across the screen and presents the NGX result itself: `OUT1` comes
back empty, pixels never return to Python. The pygame window stays as a
HUD layer — its background is filled with a chroma key and made transparent
(`LWA_COLORKEY`); it redraws ~10 times per second instead of every frame.
The worker window is excluded from capture to avoid the DDA feedback loop.

**NR off (bypass).** `F9` does not stop the pipeline anymore. Frames are
sent with `FRAME_FLAG_BYPASS`: the worker skips the NGX evaluate and
presents the raw capture instead. The overlay (picture + HUD) stays alive
and predictable; everything is hidden only on real exit. The next
non-bypass frame resumes the neural pass.

**Screenshot** in present mode uses `FRAME_FLAG_WANT_PIXELS`: for one frame
the worker both presents and returns the pixels.

**Motion field.** On `MOTS` the client sends it at optical-flow resolution
(320×180, 0.23 MB) instead of work resolution (12 MB); the worker upscales
with a compute shader. Bilinear weights are computed manually in float32
(the hardware sampler quantizes to 1/256 texel). Formula matches
`cv2.resize(INTER_LINEAR)`. The difference against the CPU path measured on
the final NGX frame is a hundredth of a pixel.

**Shared input.** The frame goes through a named section: the worker uploads
texture data straight from the mapping, only a 24-byte header crosses the
pipe. Layout is fixed and independent of `work_scale` (RGBA8 full-res,
motion at a constant offset), so resolution changes don't require
renegotiation. One slot: the next frame may be placed only after the worker
replies to the previous one.

Two constraints that look like quirks but are mandatory:

- **The worker binary must be named `nvngx.dll`.** NGX Core returns
  `FAIL_PlatformError` on `Init_Ext` for any other process name. Verified
  experimentally.
- **Work resolution is capped at 2560×1440.** At 4K the feature 18 goes
  silent: the worker hangs on frame zero in both legacy and upscale modes.

Scale changes go through `RNSZ` (~60 ms, the worker recreates the NGX
feature in-process; window and capture untouched). If `RNSZ` fails — fall
back to a full worker restart.

## Performance

Measured on RTX 5070 Ti, 4K desktop, `work_scale` 0.5 (1920×1080), static
screen, pipeline fully on the GPU (DDA + GRAY + WNDO + MOTS):

```
NR ON :  guides 0.1ms | send 0.0ms | recv 30.3ms | show 0.7ms  -> 32.0 FPS
NR OFF (bypass):       recv 15.0ms | show 0.4ms              -> 64.0 FPS
```

The path so far (same bench, same mode):

| State | Frame | FPS |
|---|---|---|
| frame and result through stdio pipes | ~50 ms | 19.5 |
| input frame through shared memory | ~46 ms | 21.9 |
| output in the worker window | ~26 ms | 36–39 |
| motion field upscaled on GPU | ~24 ms | 38–41 |
| capture in worker (DDA) + guides from gray | ~31 ms | 32.0 |
| **NR off via bypass** | **~16 ms** | **64.0** |

Actually **NGX itself is ~1 ms**. The rest is pipeline: `recv` does not
depend on work resolution (31 ms both at 384×216 and 2560×1440). NR-on
frames run in a 2-vblank rhythm at 60 Hz (30 ms), bypass in a 1-vblank
rhythm (15 ms) — phase alignment of capture/present is the next optimization
target, see the project notes.

On real motion the guides cost rises (DISOpticalFlow on 320×180 ≈ 1.4 ms +
upscale), but stays negligible compared to `recv`.

## Building the worker

```
native\build-host.bat
```

Requires MSVC 2022 Build Tools at
`C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools`. The script
builds `dlss5-feed-host64.cpp` into `native/nvngx.dll`, linking
`native/lib/Windows_x86_64/x64/nvsdk_ngx_d.lib`. NGX headers are in
`native/include/`.

The artifact `native/nvngx.dll` is not committed to the repo.

## Known limitations

- Windows only, NVIDIA RTX with DLSS 5 NR only.
- Work resolution capped at 2560×1440 (NGX constraint, see above).
- Full-screen exclusive games: the overlay is designed for the desktop and
  borderless windowed apps; on a display-mode switch the pipeline resets.
- End-to-end latency is 40–60 ms — inherent to capture → NGX → present
  chains; visible when dragging windows.
- Anticheat: `nvngx.dll` + overlay is a red flag for EAC/BattlEye/Vanguard.
- On a static desktop the optical flow turns off by the `scene_score`
  threshold — motion is skipped and frames are cheaper.
