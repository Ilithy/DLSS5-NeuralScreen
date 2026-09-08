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
- **An NVIDIA RTX card.** 50-series is the officially supported one. 20, 30 and
  40-series work too — the kernels are there and NVIDIA's own check is what
  blocks them; NeuralScreen works around it. Confirmed working on a 40-series.
- **Nothing installed.** The release archive brings its own Python.

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

### One window instead of the screen

**Num5** points everything at a single window — the one under the mouse
cursor (falling back to the last focused window) — and the overlay sits on
that window and follows it. **Num5** again goes back to the whole screen.

There is one practical reason to use it: in this mode **OBS and the NVIDIA App
can see the processed picture**. Whole-screen mode has to hide the overlay from
screen capture, otherwise the program would capture its own output and feed on
it — and with the NVIDIA App that hiding does not merely make the overlay
invisible, it stops the recording from starting at all. One window has no such
loop, so nothing has to hide.

**To record the processed picture with the NVIDIA App:** point the mouse at
the window you want (a game in windowed/borderless mode, a browser, anything),
press **Num5**, then start the recording. The overlay and the processed picture
are now part of the screen capture. Press **Num5** again to go back to the
whole screen.

The desktop itself is not a window: pressing **Num5** while pointing at the
wallpaper captures the last real window instead.

Minimise the window and processing stops with it; restore it and the picture
comes back.

## The menu

<table>
<tr>
<td><img src="docs/menu-light.png" alt="Main page" width="380"></td>
<td><img src="docs/menu-settings.png" alt="Settings page" width="380"></td>
</tr>
</table>

The dot next to your graphics card is green when neural rendering is actually
running on it, red when it is not.

The settings worth touching:

- **Profile** — how strong the effect is, from *Faithful* to *Extreme*. Start
  at *Strong / Cinematic* and go from there. The four sliders underneath are
  the same thing in detail.
- **Before / after wipe** — leaves the left part of the screen untouched so you
  can see what the effect is actually doing. Set it back to 0 when you are done
  looking.
- **Resolution the network runs at** — one slider. At the top it is your whole
  screen, which is the default and the best picture. Every step down hands the
  network a smaller frame and scales the result back up: with the slider off the
  default (full screen) the network is already at its best, and every step down
  means **roughly 50% more frames** at 2560×1440 on a 4K screen, with a softer
  picture. Look at your own screen and pick a step.

Everything else — which monitor, whether the menu opens on launch, starting
with Windows, key assignments — is behind the sliders icon.

## Recording and screenshots

**Num0** records what you see, with the system sound, into an MP4 in
`recordings`. **Num3** saves a screenshot. If the menu is open it appears in
both, on purpose.

Recording has to be done from inside the program: OBS, ShadowPlay and NVIDIA
App cannot see the overlay. That is deliberate — the program captures your
screen in order to process it, so if its own output were visible to capture it
would feed on itself.

In **one-window mode** (Num5) the input is a single window instead of the
desktop, so there is no self-capture loop: the overlay stops hiding from
screen capture, and external recorders (OBS display capture, NVIDIA App)
see the processed picture. Full-screen mode keeps hiding it.

## If something is not working

**Nothing appears after launch.** Check `NeuralScreen.log` next to the program;
it says what happened in plain text. The most common cause is a missing
`native\nvngx_dlssnr.dll`.

**The overlay is invisible in a game.** Games running in true fullscreen cannot
have anything drawn over them — that is a Windows rule, not a bug here. Switch
the game to *borderless* or *windowed fullscreen*, which almost all modern
games have.

**The menu pointer is missing or frozen.** A fullscreen game hides the system
cursor, and the game's own cursor (drawn into its frames) freezes when the game
loses focus to the menu. The overlay only shows the system cursor; a game that
hides it leaves the menu without a pointer. Switching the game to borderless
fixes it.

**The numpad hotkeys do nothing.** They need *Num Lock* to be on. With Num Lock
off the numpad sends Insert/End/arrows and the keys simply do not exist. The
program writes this to the log and shows it on screen.

**The picture is soft.** Put *Resolution the network runs at* back to the top
of its slider.

**A key does nothing.** Something else on the machine has claimed it. Reassign
it in the menu under the sliders icon.

**The menu is slow to react in a heavy game.** At 4K with a demanding scene
the pipeline can take up to a second per frame, and the hotkeys are processed
between frames — a press may feel lost. The picture itself is the priority;
the menu catches up when the load drops.

## Known limitations

- **True fullscreen games** cannot have the overlay drawn over them — that is
  a Windows rule. Borderless or windowed only.
- **A second instance of the program is not guarded.** Two copies fight over
  the screen capture; the tests refuse to run while one is up, but the
  program itself does not stop you. Close the first one before starting
  another.
- **The window list** in the menu shows every visible window; picking one
  switches to it. Num5 still takes the window under the cursor for the
  quick path.
- **A second monitor** is supported but was not tested with a window between
  monitors.
- **The menu position in window mode** starts in the bottom-right corner of
  the screen on every open; drag it where you want and it is remembered on
  close.

## Under the hood

How it works, what was measured and why the decisions went the way they did:
**[docs/TECHNICAL.md](docs/TECHNICAL.md)**.

Русская версия: **[README.ru.md](README.ru.md)**.

## License

The code here is MIT. NVIDIA's `nvngx_dlssnr.dll` is not mine and is not
included — it comes from NVIDIA under their own terms.
