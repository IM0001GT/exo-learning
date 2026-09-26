"""Install the DOSBox builds a title needs, after the user agrees."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

from exo_launcher.catalog import cache_dir

INSTALL_SCRIPT = """#!/usr/bin/bash
# Install DOSBox builds. Package names arrive as arguments.
set -u
fail=0
repo=()
aur=()
for pkg in "$@"; do
  if pacman -Si "$pkg" >/dev/null 2>&1; then
    repo+=("$pkg")
  else
    aur+=("$pkg")
  fi
done
if ((${#repo[@]})); then
  echo "Installing from the Arch repositories: ${repo[*]}"
  sudo pacman -S --needed --noconfirm "${repo[@]}" || fail=1
fi
if ((${#aur[@]})); then
  echo "Installing from the AUR: ${aur[*]}"
  if command -v yay >/dev/null; then
    yay -S --needed --noconfirm --answerdiff None --answeredit None --answerclean None --removemake "${aur[@]}" || fail=1
  elif command -v paru >/dev/null; then
    paru -S --needed --noconfirm "${aur[@]}" || fail=1
  else
    echo "These packages are not in the Arch repositories: ${aur[*]}"
    echo "Install yay, then try again."
    fail=1
  fi
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


def missing_emulators(which=shutil.which) -> list[Emulator]:
    return [item for item in EMULATORS if which(item.binary) is None]


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
    if shutil.which(binary):
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
    if shutil.which(binary):
        return True
    print(f"{info.label} is still not installed.", file=sys.stderr)
    return False
