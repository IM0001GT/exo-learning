# eXo's Retro Learning Pack for Linux

A Linux library for [eXo's Retro Learning Pack](https://www.retro-exo.com/). It plays the MS-DOS and Windows 3.1 educational titles from a copy of the pack you already have.

This repository is only the launcher. It does not contain the games, the metadata archives, or the artwork. You supply the pack yourself.

Titles stay zipped until you play them. The first launch unpacks that one game. Uninstall deletes the unpacked copy, including its saves, and leaves the zip so you can install it again later.

## What you need

A folder that already looks like the pack:

- `LaunchBox.zip`
- `!DOSmetadata.zip` and `!Win3xmetadata.zip`
- `XODOSMetadata.zip`
- `eXo/eXoDOS/` and `eXo/eXoWin3x/` full of the per-title zips
- `eXo/util/util.zip`

On Arch and Omarchy, the programs are:

```bash
sudo pacman -S python gtk4 python-gobject unzip dosbox
yay -S --needed dosbox-staging dosbox-x
```

`dosbox` runs the DOS titles written for DOSBox 0.74. `dosbox-staging` runs the other DOS titles. `dosbox-x` runs the Windows 3.1 titles. A title whose program is missing stays in the list and names the package to install.

## Setup

```bash
git clone https://github.com/IM0001GT/exo-learning.git
cd exo-learning
./setup.sh "/path/to/eXo's Retro Learning Pack"
```

Setup extracts the descriptions, manuals, and MT-32 music ROMs into the pack. It does not unpack the games. It also adds an application menu entry named **eXo's Retro Learning Pack**.

If this launcher is already sitting in a `linux/` folder inside the pack, `./setup.sh` with no path uses that pack.

## Play

Open the menu entry, or from this directory:

```bash
./exo-learning
```

The first time the library opens, it asks to install any of DOSBox, DOSBox Staging, and DOSBox-X that are missing. DOSBox comes from the Arch repositories. The other two are built with yay. The same question appears if you play a title whose emulator is not installed yet. Install opens a terminal so you can enter your password.

Search or filter the list, then choose **Install and play**. **Uninstall** is on the same title. From a terminal:

```bash
./exo-learning list
./exo-learning install "The Oregon Trail"
./exo-learning play "The Oregon Trail"
./exo-learning uninstall "The Oregon Trail"
```

Inside a game, Ctrl+F9 closes it and Ctrl+F10 releases the mouse. The Fullscreen switch in the window applies the next time a title starts.

## Where things go

| Path | What it is |
|---|---|
| `~/.config/exo-learning/settings.json` | Pack location and the fullscreen switch |
| `~/.cache/exo-learning/` | Title catalog and box-art cache |
| `eXo/eXoDOS/<folder>/` or `eXo/eXoWin3x/<folder>/` | One unpacked title |

`EXO_PACK` overrides the saved pack location for a single run.
