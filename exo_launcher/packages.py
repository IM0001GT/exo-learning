"""Install the DOSBox builds a title needs, after the user agrees.

DOSBox Staging's Arch package installs /usr/bin/dosbox and conflicts with the
DOSBox 0.74 package. The two have to live side by side, so Staging is unpacked
into the home directory instead of being installed with pacman.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from exo_launcher.catalog import cache_dir

INSTALL_SCRIPT = """#!/usr/bin/bash
# Install DOSBox builds. Package names arrive as arguments.
# dosbox-staging conflicts with the dosbox package, so it is unpacked
# into the home directory as ~/.local/bin/dosbox-staging.
set -u
fail=0
want_dosbox=0
want_x=0
want_staging=0
for pkg in "$@"; do
  case "$pkg" in
    dosbox) want_dosbox=1 ;;
    dosbox-x) want_x=1 ;;
    dosbox-staging) want_staging=1 ;;
  esac
done

if ((want_dosbox)) && [[ ! -x /usr/bin/dosbox ]]; then
  echo "Installing DOSBox from the Arch repositories."
  sudo pacman -S --needed --noconfirm dosbox || fail=1
fi

if ((want_x)) && [[ ! -x /usr/bin/dosbox-x && ! -x "$HOME/.local/bin/dosbox-x" ]]; then
  echo "Installing DOSBox-X."
  if command -v yay >/dev/null; then
    yay -S --needed --noconfirm --answerdiff None --answeredit None --answerclean None --removemake dosbox-x || fail=1
  elif command -v paru >/dev/null; then
    paru -S --needed --noconfirm dosbox-x || fail=1
  else
    echo "DOSBox-X is not in the Arch repositories. Install yay, then try again."
    fail=1
  fi
fi

place_private() {
  local name="$1" inner="$2" share="$3" cache="$4"
  local pkg root
  pkg=$(find "$cache" -maxdepth 1 -type f -name "${name}-*.pkg.tar.*" ! -name "${name}-debug-*" | head -n 1)
  if [[ -z "$pkg" ]]; then
    echo "No built $name package was found in $cache."
    return 1
  fi
  root="$HOME/.local/opt/$name"
  rm -rf "$root"
  mkdir -p "$root" "$HOME/.local/bin" "$HOME/.local/share"
  bsdtar -xf "$pkg" -C "$root"
  ln -sfn "$root/usr/bin/$inner" "$HOME/.local/bin/$name"
  ln -sfn "$root/usr/share/$share" "$HOME/.local/share/$share"
  echo "Placed $name at $HOME/.local/bin/$name"
}

if ((want_staging)) && [[ ! -x "$HOME/.local/bin/dosbox-staging" ]]; then
  echo "Placing DOSBox Staging next to DOSBox. The Arch package conflicts with DOSBox 0.74."
  if [[ ! -d "$HOME/.cache/yay/dosbox-staging" ]]; then
    if command -v yay >/dev/null; then
      mkdir -p "$HOME/.cache/yay"
      (cd "$HOME/.cache/yay" && yay -G dosbox-staging && cd dosbox-staging && makepkg -s --noconfirm)
    else
      echo "Install yay, then try again."
      fail=1
    fi
  fi
  place_private dosbox-staging dosbox dosbox-staging "$HOME/.cache/yay/dosbox-staging" || fail=1
fi

echo
if ((fail)); then
  echo "Install did not finish."
  read -r -p "Press Enter to close."
  exit 1
fi
echo "Installed."
sleep 1
"""


@dataclass(frozen=True)
class Emulator:
    binary: str
    package: str
    label: str
    blurb: str


EMULATORS = (
    Emulator(
        "dosbox",
        "dosbox",
        "DOSBox",
        "Most MS-DOS titles use DOSBox 0.74. It comes from the Arch repositories.",
    ),
    Emulator(
        "dosbox-staging",
        "dosbox-staging",
        "DOSBox Staging",
        "The other MS-DOS titles use DOSBox Staging. It is built from the AUR.",
    ),
    Emulator(
        "dosbox-x",
        "dosbox-x",
        "DOSBox-X",
        "Windows 3.1 titles use DOSBox-X. It is built from the AUR.",
    ),
)

BY_BINARY = {item.binary: item for item in EMULATORS}


class InstallError(Exception):
    pass


def find_emulator(binary: str) -> str | None:
    """Prefer a system binary, then one unpacked into the home directory."""
    system = Path("/usr/bin") / binary
    if system.is_file() and os.access(system, os.X_OK):
        return str(system)
    private = Path.home() / ".local" / "bin" / binary
    if private.is_file() and os.access(private, os.X_OK):
        return str(private)
    return shutil.which(binary)


def missing_emulators(finder=find_emulator) -> list[Emulator]:
    return [item for item in EMULATORS if finder(item.binary) is None]


def emulator_named(binary: str) -> Emulator:
    try:
        return BY_BINARY[binary]
    except KeyError as error:
        raise InstallError(f"{binary} is not one of the launcher's emulators.") from error


def normalize_packages(packages: list[str]) -> list[str]:
    allowed = {item.package for item in EMULATORS}
    chosen: list[str] = []
    for package in packages:
        if package not in allowed:
            raise InstallError(f"{package} is not one of the launcher's emulators.")
        if package not in chosen:
            chosen.append(package)
    if not chosen:
        raise InstallError("Nothing to install.")
    return chosen


def terminal_command(script: str, packages: list[str]) -> list[str]:
    if shutil.which("xdg-terminal-exec"):
        return ["xdg-terminal-exec", "--title=Install emulators", script, *packages]
    for program in ("foot", "kitty", "alacritty", "ghostty", "x-terminal-emulator"):
        if shutil.which(program):
            return [program, script, *packages]
    raise InstallError("No terminal is available to run the install.")


def start_install(packages: list[str]) -> subprocess.Popen[bytes]:
    chosen = normalize_packages(packages)
    directory = cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "install-emulators.sh"
    script.write_text(INSTALL_SCRIPT, encoding="utf-8")
    script.chmod(0o755)
    return subprocess.Popen(terminal_command(str(script), chosen))


def ensure_emulator(binary: str) -> bool:
    """Ask on a terminal, install if the user agrees, and report whether it is available."""
    if find_emulator(binary):
        return True
    info = BY_BINARY.get(binary)
    if info is None:
        return False
    print(info.blurb, file=sys.stderr)
    if not sys.stdin.isatty():
        print(f"Install the {info.package} package, then try again.", file=sys.stderr)
        return False
    answer = input(f"Install {info.label} now? [y/N] ")
    if answer.strip().lower() not in {"y", "yes"}:
        return False
    process = start_install([info.package])
    process.wait()
    if find_emulator(binary):
        return True
    print(f"{info.label} is still not installed.", file=sys.stderr)
    return False
