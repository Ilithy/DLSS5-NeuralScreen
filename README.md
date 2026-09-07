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
   and unpack it anywhere.
2. From the same release page, download **`nvngx_dlssnr.dll`** and put it in
   the `native` folder. This is NVIDIA's own file, 165 MB — too big for GitHub
   to keep in the repository, which is why it is separate.
3. Run **`NeuralScreen.exe`**.

Windows will probably warn you about an unknown publisher the first time — the
program is not signed with a paid certificate. Click *More info* → *Run
anyway*. If you would rather not, `NeuralScreen.vbs` next to it does the same
thing.

There is no installer and nothing is written outside the folder. To remove it,
delete the folder.

> **Do not use it in competitive online games.** A process named `nvngx.dll`
> plus a fullscreen overlay is exactly what anti-cheat systems look for.

## Using it

The program sits in the tray and draws over your desktop. Press **F8** for the
menu.

| Key | What it does |
|---|---|
| **F8** | open / close the menu |
| **F9** | neural rendering on / off |
| **F7** | screenshot |
| **Insert** | start / stop recording, with sound |
| **Ctrl+Alt+Q** | quit |

Every key can be reassigned in the menu, under the gear icon.

While the menu is open it takes the mouse and keyboard, so it works on top of a
game. Closed, clicks go straight through it as if it were not there.

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
- **Process at reduced resolution** — roughly **50% more frames**, at the cost
  of a slightly softer picture. Off by default. Turn it on, look at your own
  screen and decide; the slider under it controls how far it goes.

Everything else — which monitor, whether the menu opens on launch, starting
with Windows, key assignments — is behind the gear.

## Recording and screenshots

**Insert** records what you see, with the system sound, into an MP4 in
`recordings`. **F7** saves a screenshot. If the menu is open it appears in
both, on purpose.

Recording has to be done from inside the program: OBS, ShadowPlay and NVIDIA
App cannot see the overlay. That is deliberate — the program captures your
screen in order to process it, so if its own output were visible to capture it
would feed on itself.

## If something is not working

**Nothing appears after launch.** Check `NeuralScreen.log` next to the program;
it says what happened in plain text. The most common cause is a missing
`native\nvngx_dlssnr.dll`.

**The overlay is invisible in a game.** Games running in true fullscreen cannot
have anything drawn over them — that is a Windows rule, not a bug here. Switch
the game to *borderless* or *windowed fullscreen*, which almost all modern
games have.

**The picture is soft.** Turn off *Process at reduced resolution* in the menu.

**A key does nothing.** Something else on the machine has claimed it. Reassign
it in the menu under the gear.

## Under the hood

How it works, what was measured and why the decisions went the way they did:
**[docs/TECHNICAL.md](docs/TECHNICAL.md)**.

Русская версия: **[README.ru.md](README.ru.md)**.

## License

The code here is MIT. NVIDIA's `nvngx_dlssnr.dll` is not mine and is not
included — it comes from NVIDIA under their own terms.
