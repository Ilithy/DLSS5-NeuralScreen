# NeuralScreen

**NVIDIA's DLSS 5 neural renderer, applied to your whole Windows desktop in
real time.** Everything on screen — games, video, photos — goes through the
same neural network that DLSS 5 games use, and comes back sharper.

<table>
<tr>
<td><img src="docs/menu-light.png" alt="Menu, light theme" width="420"></td>
<td><img src="docs/menu-dark.png" alt="Menu, dark theme" width="420"></td>
</tr>
</table>

*Everything lives in one menu inside the overlay. The **Before / after wipe**
slider splits the screen down the middle so you can see what the effect is
actually doing.*

## What you need

- **Windows 11**
- **An NVIDIA RTX card.** What works, honestly:

  | Cards | Status |
  |---|---|
  | **RTX 50** (Blackwell) | ✅ works - the officially supported generation |
  | **RTX 40** (Ada) | ✅ works - through the built-in architecture hook (the bundled runtime is NVIDIA's own sm_120 build; the hook makes it run on Ada) |
  | **RTX 30** (Ampere) | ❌ not in this release - no sm_86 kernels. See *Trying RTX 30/20* below. |
  | **RTX 20** (Turing) | ❌ cannot run the neural pass at all - below the minimum architecture (DLSS5-Feeder issue #73) |
  | **Laptops with hybrid graphics (Optimus)** | ⚠️ works only when the display is driven by the NVIDIA GPU - force the dGPU (MUX switch, or an external monitor on the dGPU port). On the iGPU it fails on the first frame |

- **Nothing installed.** The release archive brings its own Python.

### Trying RTX 30/20

The runtime is compiled per GPU architecture. The bundled build covers
RTX 40/50; the community **310.8.SF** builds add RTX 30 kernels (RTX 20
cannot work - below the minimum architecture). To try: download
`nvngx_dlssnr_310.8.SF-v2.zip` from https://github.com/RankFTW/rhi-repo/releases,
replace `native\nvngx_dlssnr.dll` (back up first), run and check the menu:
the GPU dot goes green only when feature 18 was actually created.

**Speed warning:** on RTX 30 the pass is slow - single digits to ~20 FPS at 1440p. Lower the *Resolution the network runs at* slider.

## Install

1. Download the archive from [Releases](https://github.com/perseval-BLR/DLSS5-NeuralScreen/releases)
   and unpack it anywhere. **Everything is inside** — including NVIDIA's
   `nvngx_dlssnr.dll` (165 MB, too big for GitHub to keep in the repository,
   so it ships in the archive instead).
2. Run **`NeuralScreen.exe`**.

Windows will probably warn you about an unknown publisher the first time — the
program is not signed with a paid certificate. Click *More info* → *Run
anyway*. If you would rather not, `NeuralScreen.vbs` next to it does the same
thing.

There is no installer and nothing is written outside the folder. To remove it,
delete the folder.

> **Do not use it in competitive online games.** A process named `nvngx.dll`
> plus a fullscreen overlay is exactly what anti-cheat systems look for.

## Using it

The program sits in the tray and draws over your desktop. Press **Num2** for
the menu. The hotkeys live on the numpad, so **Num Lock has to be on** - with
it off those keys send Insert/End/arrows instead and nothing happens.

| Key | What it does |
|---|---|
| **Num2** | open / close the menu |
| **Num1** | neural rendering on / off |
| **Num3** | screenshot |
| **Num0** | start / stop recording, with sound |
| **Num4** / **Num6** | processing resolution down / up |
| **Num5** | process one window instead of the whole screen |
| **Ctrl+Alt+Q** | quit |

Every key can be reassigned in the menu, under the sliders icon.

While the menu is open it takes the mouse and keyboard, so it works on top of a
game. Closed, clicks go straight through it as if it were not there.

Startup and mode switches do not flash: the overlay appears with the first
real frame, a brief blur-with-spinner covers the pipeline rebuild.

### One window instead of the screen

**Num5** points everything at a single window — the one under the mouse
cursor (falling back to the last focused window) — and the overlay sits on
that window and follows it. **Num5** again goes back to the whole screen.

The menu does the same without the hotkey: **Select window...** opens the
window list (hovering outlines the real window), **Fullscreen** in the
footer returns to the whole screen; the mode is shown under the GPU line.

There is one practical reason to use it: in this mode **OBS and the NVIDIA App
can see the processed picture**. Whole-screen mode has to hide the overlay from
screen capture, otherwise the program would capture its own output and feed on
it — and with the NVIDIA App that hiding stops the recording from starting at
all. One window has no such loop, so nothing has to hide.

**To record the processed picture with the NVIDIA App:** point the mouse at
the window you want (a game in windowed/borderless mode, a browser, anything),
press **Num5**, then start the recording. The overlay and the processed picture
are now part of the screen capture. Press **Num5** again to go back to the
whole screen. The desktop itself is not a window: pressing **Num5** while
pointing at the wallpaper captures the last real window instead. Minimise the
window and processing stops with it; restore it and the picture comes back.

## The menu

<table>
<tr>
<td><img src="docs/screenshot-main-light.png" alt="Main page, light theme" width="380"></td>
<td><img src="docs/screenshot-main-dark.png" alt="Main page, dark theme" width="380"></td>
</tr>
<tr>
<td><img src="docs/screenshot-windows.png" alt="Window list" width="380"></td>
<td><img src="docs/screenshot-settings.png" alt="Settings page" width="380"></td>
</tr>
</table>

The dot next to your graphics card is green when neural rendering is actually
running on it, red when it is not.

The settings worth touching:

- **Profile** — how strong the effect is, from *Faithful* to *Extreme*. Start
  at *Strong / Cinematic* and go from there. The four sliders underneath are
  the same thing in detail.
- **Before / after wipe** — leaves the left part of the screen untouched so
  you can see what the effect is doing. Set it back to 0 when done.
- **Resolution the network runs at** — one slider. At the top it is your whole
  screen, which is the default and the best picture. Every step down hands the
  network a smaller frame: with the slider off the default (full screen) the
  network is already at its best, and every step down means **roughly 50% more
  frames** at 2560×1440 on a 4K screen. The picture stays sharp: the network's
  result is composed onto the pristine 1:1 native frame (a matched residual
  composite), so text, edges and UI keep full resolution while the cheap
  low-res network does the relighting. Look at your own screen and pick a step.

Everything else — which monitor, whether the menu opens on launch, starting
with Windows, key assignments — is behind the sliders icon.

## Recording and screenshots

**Num0** records what you see, with the system sound, into an MP4 in
`recordings`. **Num3** saves a screenshot. If the menu is open it appears in
both, on purpose.

Recording has to be done from inside the program: OBS, ShadowPlay and NVIDIA
App cannot see the overlay. That is deliberate — the program captures your
screen in order to process it, so if its own output were visible to capture it
would feed on itself. In **one-window mode** (Num5) the input is a single
window instead of the desktop, so there is no self-capture loop: the overlay
stops hiding from screen capture, and external recorders (OBS display capture,
NVIDIA App) see the processed picture. Full-screen mode keeps hiding it.

## If something is not working

**Nothing appears after launch.** Check `NeuralScreen.log` next to the
program; the most common cause is a missing `native\nvngx_dlssnr.dll`.

**The overlay is invisible in a game.** True fullscreen cannot have anything
drawn over it — a Windows rule. Switch the game to *borderless* or
*windowed fullscreen*.

**The menu pointer is missing or frozen.** A fullscreen game hides the system
cursor; the overlay only shows the system cursor. Borderless fixes it.

**The numpad hotkeys do nothing.** They need *Num Lock* to be on. With Num
Lock off the numpad sends Insert/End/arrows and the keys simply do not exist.

**The picture is soft.** Put *Resolution the network runs at* back to the top
of its slider.

**A key does nothing.** Something else claimed it; reassign it in the menu
under the sliders icon.

**The menu is slow in a heavy game.** At 4K the pipeline can take up to a
second per frame; a press may feel lost. The picture is the priority.

## Known limitations

- **True fullscreen games** cannot have the overlay drawn over them — a Windows rule. Borderless or windowed only.
- **A second instance is not guarded** — close the first one first.
- **The window list** shows every visible window; Num5 takes the one under the cursor. **A second monitor** works but was not tested with a window between them; the menu position in window mode starts bottom-right.
- **HDR displays** are not supported: switch to SDR (Win+Alt+B).
- **Pipeline latency** is 40–60 ms — fine interactively, not competitively; **processing resolution is capped at 2560×1440** (the network refuses 4K), output is always your full native resolution.
- **The bundled `nvngx_dlssnr.dll` is NVIDIA's own leaked pre-release build** (310.8.0, sm_120 kernels) — see License below.

## Under the hood — how it works, what was measured and why: **[docs/TECHNICAL.md](docs/TECHNICAL.md)**. Русская версия: **[README.ru.md](README.ru.md)**.

## License

The code here is MIT. NVIDIA's `nvngx_dlssnr.dll` is NVIDIA's own leaked
pre-release build (310.8.0, sm_120 kernels for RTX 50). Included as-is,
unmodified by us, no guarantees; research-only.