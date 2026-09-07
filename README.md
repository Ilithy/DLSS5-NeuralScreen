# NeuralScreen — DLSS 5 Neural Rendering Overlay for Windows Desktop

**NVIDIA DLSS 5 Neural Rendering (NGX Feature 18), applied to your whole
Windows desktop in real time.** The screen is captured, run through the same
neural renderer that DLSS 5 games use, and drawn back in a click-through
overlay. RTX 50-series only.

<table>
<tr>
<td><img src="docs/menu-light.png" alt="Menu, light theme" width="420"></td>
<td><img src="docs/menu-dark.png" alt="Menu, dark theme" width="420"></td>
</tr>
</table>

Everything is in one menu inside the overlay: `F8` opens it, and while it is
open the overlay takes mouse and keyboard, so it works on top of a game. The
dot next to the GPU name is green when Neural Rendering actually runs on this
card, red when it does not.

```
desktop capture -> motion guides -> NGX worker (D3D12) -> overlay on top of the screen
  worker DDA        cv2 DIS 320x180    native/nvngx.dll        worker window + menu layer
```

> **v1.1.** The whole pipeline lives on the GPU — capture, neural pass and
> presentation all happen inside the worker; Python only computes
> optical-flow guides (0.1 ms/frame). RTX 5070 Ti, 2560×1600 desktop, work at
> the 2304×1440 cap: **102 FPS with NR on**. Of the 9.8 ms frame, 8.0 ms is
> GPU time inside the NGX evaluate — measured with D3D12 timestamps on the
> queue, so the rest of the pipeline costs 1.8 ms.

## Requirements

- **Windows 11** (Desktop Duplication API + `WDA_EXCLUDEFROMCAPTURE`)
- **NVIDIA RTX 50-series** for the officially supported path. Developed and
  tested on RTX 5070 Ti, driver 616.56. For 20/30/40-series see
  "Older GPUs" below — the kernels are there, the gate is a policy check.
- The release archive bundles a **portable Python runtime** (embedded
  CPython 3.13 + PyAV + OpenCV + pygame) — no Python installation needed.
- **`native/nvngx_dlssnr.dll`** — NVIDIA runtime (165 MB). Not in the repo
  (GitHub 100 MB file limit) — grab it from the release assets and put it
  into `native/`.

## Quick start

1. Download the release archive and unpack it anywhere.
2. Put `native/nvngx_dlssnr.dll` from the release assets into `native/`
   (or follow the README section "Requirements").
3. Double-click **`NeuralScreen.vbs`** — no console windows appear.
   The app runs in the tray, logs go to `NeuralScreen.log`.
4. `NeuralScreen.bat` is a debug launcher with a console — use it when you
   need to see FPS/timing output live.

> Anticheat note: a process named `nvngx.dll` plus a fullscreen overlay may
> upset EAC/BattlEye/Vanguard. Do not run NeuralScreen in competitive online
> games.

## Controls

| Key | Action |
|---|---|
| `F9` | NR on/off. **Off is a bypass**: the overlay stays on screen showing the raw capture (no neural effect), and the desktop runs faster. Everything hides only on real exit. |
| `F8` | open/close the menu. While it is open the overlay takes mouse and keyboard, so the menu works on top of a game; closed, it is click-through again and the desktop behaves normally. |
| `Insert` | start/stop video recording (MP4, AV1 NVENC, 60 fps, ~64 Mbps). Also a **Record** button in the menu |
| `Ctrl+Alt+↑` / `Ctrl+Alt+↓` | processing scale ±0.05 |
| `Ctrl+Alt+Q` | quit |

Hotkeys are registered via `RegisterHotKey`, not polled: the system delivers
the keypress **only to us and not to the active app** — F9 inside a game
toggles NR and the game never sees the key. The flip side: while
NeuralScreen runs, F8, F9 and Insert belong to it.

Tray icon: left click opens the same menu, right click gives NR on/off,
scale and quit.

**The menu** lives inside the overlay itself — there is no separate settings
window (there used to be one; it was a second interface over the same
values, it stole focus from games, and it dragged the whole tcl/tk runtime
along). It holds: live counters (FPS, resolution, work size, frames,
recording time), NR on/off, profile (Faithful / Natural / Strong / Extreme),
the four NR parameters, the before/after wipe, language (ru/en), light/dark
theme, "open menu on launch", and the buttons Screenshot / Help / Record /
Exit / Close. A status line shows the GPU, its architecture and whether
Neural Rendering works on it.

Drag it by the title bar, resize it by the bottom-right corner — both are
highlighted when you point at them, and both are remembered in
`config.json` (`menu_offset`, `menu_scale`).

On launch the menu opens by itself. NeuralScreen draws on top of the desktop
and otherwise gives no sign of life, so without this you could not tell
whether it had started. Turn the toggle off and you get a short alert
instead.

**Three ways to close the app** (they differ, hence different names):

| | Effect |
|---|---|
| `Ctrl+Alt+Q`, "Exit" in the menu, "Exit" in tray | terminate: worker shut down, windows closed, no processes left |
| "Close" in the menu, `F8`, `Esc` | only hides the menu, the app keeps running |

## Recording (Insert)

`Insert` starts/stops recording of the **NR-processed frame** into
`recordings/neuralscreen-<timestamp>.mp4`:

- AV1 NVENC hardware encoding at your desktop resolution, **60 fps**,
  quality-targeted VBR (`cq 16`, ~64 Mbps in practice, ceiling 250 Mbps),
  preset p6 + tune hq, sRGB/BT.709 color tags (metadata written both on the
  stream and on every frame — players render colors identical to the screen).
- The bitrate is a **ceiling, not a target**: on fast motion the encoder is
  allowed to spend more instead of dropping quality to hit a fixed number.
  Raising quality costs no encoding time — that is dominated by the colour
  conversion, not by the preset (measured: 1.6–1.7 s per 3 s of video at
  every setting tried).
- Recording runs at **60 fps** because the pipeline delivers ~55 frames per
  second. The previous 30 fps time base could not represent them: frames were
  squeezed into half as many ticks, which is what made fast motion fall apart
  regardless of bitrate.
- **Only the open menu is burned into the recording** — nothing else. Our
  own layer is hidden from external capture, so anything that must reach the
  file is drawn onto the frame before encoding. The same applies to
  screenshots. The HUD panel and the watermark used to be burned in as well;
  they are gone from the screen, and in a file they read as someone else's
  caption.
- Recording works in both NR ON and NR OFF (bypass) modes; the file duration
  matches real time (PTS is built from the wall clock).

## config.json

| Field | Meaning |
|---|---|
| `monitor` | monitor index for capture |
| `width`, `height` | output resolution (**actual monitor resolution is used automatically when config is stale**) |
| `fullscreen` | borderless fullscreen window |
| `warmup` | NGX warmup frames at start |
| `work_scale` | 0.25–1.0, NGX processing resolution relative to output. **Higher is free** — see Performance |
| `profile` | `Faithful`, `Natural`, `Strong / Cinematic`, `Extreme / Overdrive` |
| `intensity`, `local_tone`, `local_structure`, `skin_structure` | `null` = take from profile |
| `lang` | `ru` / `en` |
| `worker_present` | worker shows the frame in its own window (`false` — pygame output) |
| `motion_on_gpu` | worker upscales the motion field (`false` — CPU) |
| `capture_in_worker` | worker captures the desktop itself (DDA, `false` — dxcam in Python) |

## Architecture

Two processes. Python drives settings, optical-flow guides and the menu
layer; the C++ worker owns the D3D12 device, the capture, NGX and the
overlay window.
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

**Capture.** On `DDA1` the worker opens Desktop Duplication on the GPU: each
frame is copied into a cross-device shared texture and swizzled to RGBA.
Python stops capturing entirely — `grab` and `guides` drop to 0.1 ms.
Fallback (dxcam + full-frame send) stays intact.

**Guides.** The optical flow needs a small gray frame. On `GRAY` the worker
computes it with an honest block average (12×12 per cell at 4K → 320×180,
matching `cv2.INTER_AREA`; bilinear would alias text and break the flow) and
writes it into a named mapping. No 4K frame ever crosses the CPU.

**Output.** On `WNDO` the worker raises its own borderless D3D12-swapchain
window across the screen and presents the NGX result itself: pixels never
return to Python. The pygame window stays as the menu layer — its background
is filled with a chroma key and made transparent (`LWA_COLORKEY`). While the
menu is open the window's global alpha (`LWA_ALPHA`) goes to 255, otherwise
the bright frame underneath bleeds through the panel.

**NR off (bypass).** `F9` does not stop the pipeline anymore. Frames are
sent with `FRAME_FLAG_BYPASS`: the worker skips the NGX evaluate and
presents the raw capture instead. The overlay stays alive; everything is
hidden only on real exit.

**Recording path.** Frames are requested from the worker with
`FRAME_FLAG_WANT_PIXELS` (the same mechanism as screenshots), the open menu
is drawn onto the frame with `draw_capture_overlay()`, then PyAV encodes
AV1 NVENC.

Two constraints that look like quirks but are mandatory:

- **The worker binary must be named `nvngx.dll`.** NGX Core returns
  `FAIL_PlatformError` on `Init_Ext` for any other process name. Verified
  experimentally.
- **Work resolution is capped at 2560×1440.** At 4K the feature 18 goes
  silent: the worker hangs on frame zero in both legacy and upscale modes.

Scale changes go through `RNSZ` (~60 ms, the worker recreates the NGX
feature in-process). If `RNSZ` fails — fall back to a full worker restart.

## Performance

Measured on RTX 5070 Ti, 4K desktop, `work_scale` 0.5 (1920×1080), pipeline
fully on the GPU (DDA + GRAY + WNDO + MOTS). Worker-side phase breakdown
(enable with `NS_PHASE=1`):

```
                 acq   dda  upload   eval  present   frame     FPS
NR ON            0.0   0.7     0.1   16.6      0.5    17.9      55
NR OFF (bypass)  ~3    ~4      0.1      -      ~3      7.3  121-133
```

**NGX evaluation is 16.6 ms — 93% of an NR frame.** Everything else together
costs 1.3 ms, so the practical ceiling on this hardware is set by NGX, not by
the plumbing. In bypass mode NGX is skipped and the loop waits on the desktop
actually changing (`acq`), which is why it runs several times faster.

An earlier revision of this section claimed "NGX itself is ~1 ms". That was a
measurement error: the figure came from regressing round-trip time against
work resolution, and such a regression only sees the resolution-dependent part
(0.46 ms/MPix). NGX's large constant cost was invisible to it and got
attributed to transport.

The 1440p figures previously published here (64 FPS) predate the DDA fence
fix and understate current performance; they have not been re-measured.

### work_scale costs nothing

NGX evaluation time does not depend on the work resolution at all. Measured
across the whole slider range on a 4K desktop:

```
work          MPix   eval ms    FPS
960x540       0.52    16.13    53.8
1344x756      1.02    15.90    55.4
1728x972      1.68    15.93    56.8
2112x1188     2.51    15.96    55.7
2496x1404     3.50    15.98    55.3
```

Fit: `eval = 15.97 ms + (-0.01) ms/MPix`, R² = 0.011 — i.e. noise, not a
trend. Seven times more input pixels cost 0.2 ms, which is within the
measurement spread.

**So run the slider at its maximum — the default is now `1.0`.** Lowering
`work_scale` buys no performance and only costs sharpness. 1.0 is safe on any
monitor because the work size is clamped to the 2560×1440 NGX cap anyway, so
the slider always lands exactly at the cap: 2560×1440 from a 4K desktop,
2304×1440 from 2560×1600. The previous default of 0.65 was picked to sit just
under the cap **on a 4K screen** — on anything smaller it quietly threw
resolution away.

Re-confirmed with D3D12 timestamps on the queue, i.e. GPU time inside
`Evaluate` rather than time around the submit: **7.9–8.6 ms flat from 0.37 to
3.32 MPix** (2560×1600 desktop, RTX 5070 Ti). Nine times the input pixels,
the same time — the model works at its own fixed internal resolution. End to
end: 102 FPS at both `0.65` and `1.00`.

## Before / after wipe

The **Before / after wipe** slider leaves the left share of the frame
unprocessed, so the raw capture and the NGX result sit side by side with an
accent-coloured divider between them. 0 turns it off.

It happens in the worker, on the GPU: one `CopyTextureRegion` of the left
strip of the input over the output, then the divider through
`ClearUnorderedAccessViewFloat` with a rect. Both run before Present and
before pixels are handed back, so the wipe lands in recordings and
screenshots by itself. The position rides in the top 16 bits of the frame
header's flag field, so moving the slider does not recreate the worker.

## Older GPUs (20/30/40-series)

`nvngx_dlssnr.dll` refuses to create the feature on anything below Blackwell.
Its own version resource says `NGXGpuArchitecture = NVSDK_NGX_GPU_Arch_Blackwell2`,
and it carries the message

```
DLSSNR: Unsupported GPU architecture 0x%x, minimum required 0x%x
```

But the compiled kernels for older cards **are in the file**. Parsing its
fatbin headers: all fifteen fatbins carry `sm_75` (Turing), `sm_86` (Ampere),
`sm_89` (Ada) and `sm_120` (Blackwell), with no gaps. So the refusal is a
policy check, not missing code.

The library learns the architecture through nvapi — it loads `nvapi64.dll`,
takes its single export `nvapi_QueryInterface` and asks for
`NvAPI_GPU_GetArchInfo` by id. Setting `NS_ARCH_SPOOF=1` makes the worker
patch that one function in **its own process memory** at startup: the prologue
is saved, replaced with a jump to our handler, and restored around every real
call, so any GPU handle is still served by NVIDIA's own code — only the
returned architecture is rewritten to Blackwell. Nothing in NVIDIA's files is
modified, and on a 50-series card the hook disables itself and does nothing.

Off by default. **Untested on real 20/30/40-series hardware** — the only card
here is a 5070 Ti, where the hook is a no-op by design. The menu's GPU dot
tells you the truth either way: it goes green only when the worker actually
created feature 18, not when the architecture merely looks right.

This likely conflicts with the license terms of NVIDIA's redistributable. It
defeats no copy protection and modifies no files, but enabling it is your
call.

## Limitations (read before buying/recording)

1. **Exclusive-fullscreen games are NOT covered.** Windows does not draw any
   windows on top of an exclusive fullscreen surface — an OS constraint, not
   an app bug. Test in **borderless windowed** mode (every modern game
   supports it). Verified: RealRTCW (windowed) — works; F.E.A.R.
   (LithTech d3d8, forces exclusive on level load) — overlay invisible in
   gameplay, visible in menus.
2. **External screen recorders (OBS, ShadowPlay, Lightshot) do NOT capture
   the overlay.** The overlay windows are excluded from capture
   (`WDA_EXCLUDEFROMCAPTURE`) to prevent the DDA pipeline from feeding on
   itself. NVIDIA App may even refuse to record the desktop while
   NeuralScreen runs ("Python prevents desktop recording"). Use the built-in
   **Insert** recording instead — it captures the actual NR frame, with the
   menu on it if the menu is open.
3. **Work resolution is capped at 2560×1440** (NGX feature 18 constraint).
4. **End-to-end latency is 40–60 ms** — inherent to capture → NGX → present
   chains; visible when dragging windows.
5. **Anticheat:** `nvngx.dll` + overlay is a red flag for EAC/BattlEye/
   Vanguard. Do not run in competitive online games.
6. **First seconds after toggling NR** are slow (NGX recalibration: FPS
   drops for ~1–3 s, then recovers).
7. On a static desktop the optical flow turns off by the `scene_score`
   threshold — motion is skipped and frames are cheaper.

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

## License

MIT. DLSS 5 Neural Rendering is a trademark of NVIDIA; NeuralScreen is an
independent open-source tool, not affiliated with NVIDIA.
