#!/usr/bin/bash
# One-time setup for the Linux launcher.
# Unpacks metadata and MT-32 ROMs, not the games.
# Installs DOSBox when you say so, and adds a desktop entry.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
LAUNCHER="$HERE/exo-learning"

if [[ $# -ge 1 ]]; then
  ROOT=$(cd "$1" && pwd)
elif [[ -n "${EXO_PACK:-}" ]]; then
  ROOT=$(cd "$EXO_PACK" && pwd)
elif [[ -f "$HERE/../LaunchBox.zip" && -d "$HERE/../eXo/eXoDOS" ]]; then
  ROOT=$(cd "$HERE/.." && pwd)
else
  echo "Usage: ./setup.sh \"/path/to/eXo's Retro Learning Pack\""
  echo "The games are not in this repository. Point setup at a copy you already have."
  exit 1
fi

if [[ ! -f "$ROOT/LaunchBox.zip" || ! -d "$ROOT/eXo/eXoDOS" || ! -d "$ROOT/eXo/eXoWin3x" ]]; then
  echo "$ROOT does not look like eXo's Retro Learning Pack."
  echo "Expected LaunchBox.zip, eXo/eXoDOS, and eXo/eXoWin3x."
  exit 1
fi

echo "eXo's Retro Learning Pack"
echo "Pack: $ROOT"
echo

missing=()
command -v python3 >/dev/null || missing+=(python)
command -v unzip >/dev/null || missing+=(unzip)
command -v dosbox >/dev/null || missing+=(dosbox)
command -v dosbox-staging >/dev/null || missing+=(dosbox-staging)
command -v dosbox-x >/dev/null || missing+=(dosbox-x)

if ((${#missing[@]})); then
  echo "Missing: ${missing[*]}"
  echo "dosbox is in the Arch repos. dosbox-staging and dosbox-x are built with yay."
  if [[ -t 0 ]] && command -v yay >/dev/null; then
    read -r -p "Install them with yay now? [y/N] " answer
    if [[ $answer == [Yy]* ]]; then
      yay -S --needed "${missing[@]}"
    fi
  else
    echo "Install those packages, then run setup again."
  fi
else
  echo "DOSBox builds are already installed."
fi

if [[ ! -d "$ROOT/eXo/eXoDOS/!dos" ]]; then
  echo "Extracting DOS metadata…"
  unzip -q -o "$ROOT/!DOSmetadata.zip" -d "$ROOT"
else
  echo "DOS metadata is already extracted."
fi

if [[ ! -d "$ROOT/eXo/eXoWin3x/!win3x" ]]; then
  echo "Extracting Windows 3.1 metadata…"
  unzip -q -o "$ROOT/!Win3xmetadata.zip" -d "$ROOT"
else
  echo "Windows 3.1 metadata is already extracted."
fi

if [[ ! -d "$ROOT/eXo/mt32" ]]; then
  echo "Extracting MT-32 ROMs…"
  tmp=$(mktemp)
  unzip -p "$ROOT/eXo/util/util.zip" mt32.zip > "$tmp"
  unzip -q -o "$tmp" -d "$ROOT/eXo"
  rm -f "$tmp"
else
  echo "MT-32 ROMs are already extracted."
fi

# The kid can create an installed folder here and cannot delete root-owned zips.
chmod +t "$ROOT/eXo/eXoDOS" "$ROOT/eXo/eXoWin3x" || echo "Could not set the sticky bit. Continuing."

echo "Building the title catalog…"
"$LAUNCHER" --pack "$ROOT" rebuild-cache

python3 - "$LAUNCHER" << 'PY'
import sys
from pathlib import Path

launcher = Path(sys.argv[1]).resolve()
reserved = set(" \t\"'\\`$<>~|&;$*?#()")

def escape(text: str) -> str:
    return "".join(("\\" + char if char in reserved else char) for char in text)

template = (launcher.parent / "exo-learning.desktop").read_text(encoding="utf-8")
desktop = template.replace("@EXEC@", escape(str(launcher)))
dest = Path.home() / ".local" / "share" / "applications" / "exo-learning.desktop"
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text(desktop, encoding="utf-8")
print(f"Desktop entry: {dest}")
PY

echo
echo "Setup is done. Games stay packed until you play one."
echo "Open \"eXo's Retro Learning Pack\" from the app menu, or run:"
echo "  $LAUNCHER"
