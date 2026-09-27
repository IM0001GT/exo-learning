"""Start a title in the DOSBox build its launch script asks for."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from exo_launcher.catalog import Title, cache_dir, read_conf_text
from exo_launcher.library import ensure_mt32
from exo_launcher.packages import find_emulator

_HOST_PATH = re.compile(r"(?i)\b(eXo(?:DOS|Win3x))/([^/\s]+)")


class MissingEmulator(Exception):
    def __init__(self, binary: str, package: str):
        self.binary = binary
        self.package = package
        super().__init__(
            f"{binary} is not installed. Install the {package} package, then try again."
        )


# Thinkin' Things 3 is stable. 1 crashes (illegal opcode on cputype=auto)
# and 2 cuts speech short (cycles=auto). Match 3's CPU.
_TITLE_CPU = {
    "TTC1": {"core": "normal", "cputype": "486_slow", "cycles": "65000"},
    "TTC2": {"core": "normal", "cputype": "486_slow", "cycles": "65000"},
}


def set_conf_values(text: str, section: str, values: dict[str, str]) -> str:
    lines = text.splitlines(keepends=True)
    header = re.compile(rf"(?i)^\[{re.escape(section)}\]\s*$")
    any_header = re.compile(r"(?i)^\[[^\]]+\]\s*$")
    start = next((index for index, raw in enumerate(lines) if header.match(raw.strip())), None)
    if start is None:
        added = f"\n[{section}]\n" + "".join(f"{key}={value}\n" for key, value in values.items())
        return text.rstrip() + added + ("\n" if text.endswith("\n") else "")
    end = next(
        (index for index in range(start + 1, len(lines)) if any_header.match(lines[index].strip())),
        len(lines),
    )
    remaining = dict(values)
    block = [lines[start]]
    for line in lines[start + 1 : end]:
        matched = False
        for key, value in list(remaining.items()):
            if re.match(rf"(?i){re.escape(key)}\s*=", line.strip()):
                ending = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
                block.append(f"{key}={value}{ending}")
                del remaining[key]
                matched = True
                break
        if not matched:
            block.append(line)
    for key, value in remaining.items():
        block.append(f"{key}={value}\n")
    return "".join(lines[:start] + block + lines[end:])


def apply_title_overrides(text: str, short_id: str) -> str:
    cpu = _TITLE_CPU.get(short_id)
    if not cpu:
        return text
    return set_conf_values(text, "cpu", cpu)


def render_conf(
    text: str,
    platform_dir: str,
    install_name: str,
    fullscreen: bool,
    emulator_key: str,
    exo_dir: Path | None = None,
) -> str:
    """Host paths use the real folder spelling. Guest paths keep forward slashes.

    Mount paths become absolute. A title's autoexec may `cd ..` before mount,
    and the pack directory itself can contain spaces.
    """
    text = text.replace("\\", "/").replace("\r", "")

    def fix(match: re.Match[str]) -> str:
        platform, segment = match.group(1), match.group(2)
        if platform.casefold() != platform_dir.casefold():
            return match.group(0)
        folder = install_name if segment.casefold() == install_name.casefold() else segment
        return f"{platform_dir}/{folder}"

    text = _HOST_PATH.sub(fix, text)
    if exo_dir is not None:
        text = _absolutize_mounts(text, exo_dir)
        # Those cd .. lines were only there so a relative mount could see eXo.
        # On Linux they leave the pack and walk into the home directory.
        text = _drop_leading_directory_ups(text)
    fullscreen_value = "true" if fullscreen else "false"
    text = re.sub(
        r"(?im)^(fullscreen\s*=\s*)\S+",
        rf"\g<1>{fullscreen_value}",
        text,
        count=1,
    )
    # DOSBox 0.74 still has output=overlay. Staging and DOSBox-X use OpenGL.
    if emulator_key in {"staging", "x"}:
        text = re.sub(r"(?im)^(output\s*=\s*)\S+", r"\g<1>opengl", text, count=1)
    if emulator_key == "x":
        text = _dosbox_x_no_folder_prompt(text)
    if not text.endswith("\n"):
        text += "\n"
    return text


def _dosbox_x_no_folder_prompt(text: str) -> str:
    """DOSBox-X asks for a working folder on every start unless this is set."""
    option = "working directory option=noprompt"
    if re.search(r"(?im)^working directory option\s*=", text):
        return re.sub(r"(?im)^working directory option\s*=.*$", option, text)
    if re.search(r"(?im)^\[dosbox\]", text):
        return re.sub(r"(?im)^(\[dosbox\][^\n]*)", rf"\1\n{option}", text, count=1)
    return text.rstrip() + f"\n\n[dosbox]\n{option}\n"


_MOUNT_PATH = re.compile(
    r"(?im)^(?P<prefix>\s*@?(?:img)?mount\s+\S+\s+)(?P<path>\"?[^\s\"]+\"?)(?P<rest>.*)$"
)


def _absolutize_mounts(text: str, exo_dir: Path) -> str:
    def fix(match: re.Match[str]) -> str:
        raw = match.group("path").strip('"')
        relative = raw[2:] if raw.startswith("./") else raw
        first = relative.split("/", 1)[0]
        if first.casefold() not in {"exodos", "exowin3x", "mt32"}:
            return match.group(0)
        absolute = (exo_dir / relative).as_posix()
        if re.search(r"""[\s'"]""", absolute):
            absolute = '"' + absolute.replace('"', "") + '"'
        return f"{match.group('prefix')}{absolute}{match.group('rest')}"

    return _MOUNT_PATH.sub(fix, text)


def _drop_leading_directory_ups(text: str) -> str:
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        output.append(lines[index])
        if lines[index].strip().lower() != "[autoexec]":
            index += 1
            continue
        index += 1
        while index < len(lines) and lines[index].strip() == "":
            output.append(lines[index])
            index += 1
        while index < len(lines) and re.fullmatch(r"@?cd\s+\.\.", lines[index].strip(), re.I):
            index += 1
    return "".join(output)


def prepare_conf(pack: Path, title: Title, fullscreen: bool, cache: Path | None = None) -> Path:
    cache = cache if cache is not None else cache_dir()
    rendered = apply_title_overrides(
        render_conf(
            read_conf_text(pack, title),
            title.platform_dir,
            title.install_name,
            fullscreen,
            title.emulator_key,
            pack / "eXo",
        ),
        title.short_id,
    )
    directory = cache / "conf"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{title.id}.conf"
    path.write_text(rendered, encoding="utf-8")
    return path


def launch_argv(binary: str, conf: Path, emulator_key: str, exo_dir: Path) -> list[str]:
    argv = [binary]
    if emulator_key == "x":
        argv.append("-nopromptfolder")
    if emulator_key == "staging":
        argv.extend(["--working-dir", str(exo_dir)])
    argv.extend(["-conf", str(conf), "-exit"])
    return argv


def compositor_fullscreen_available() -> bool:
    if shutil.which("hyprctl") is None:
        return False
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return True
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
    return runtime.is_dir() and any(runtime.glob("hypr/*/hyprland.lock"))


def _process_tree(pid: int) -> set[int]:
    found = {pid}
    growing = True
    while growing:
        growing = False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            child = int(entry.name)
            if child in found:
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
                parent = int(stat.rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            if parent in found:
                found.add(child)
                growing = True
    return found


def fullscreen_on_compositor(pid: int) -> None:
    """Same result as Super+F: let Hyprland scale the game window."""
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        tree = _process_tree(pid)
        try:
            clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"], text=True))
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
            return
        match = next((client for client in clients if client.get("pid") in tree), None)
        if match and match.get("address"):
            address = match["address"]
            subprocess.run(
                ["hyprctl", "dispatch", "focuswindow", f"address:{address}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["hyprctl", "dispatch", "fullscreen", "fullscreen"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        time.sleep(0.1)


def launch_title(
    pack: Path,
    title: Title,
    fullscreen: bool,
    cache: Path | None = None,
    wait: bool = False,
) -> subprocess.Popen[bytes]:
    binary = find_emulator(title.emulator_bin)
    if not binary:
        raise MissingEmulator(title.emulator_bin, title.emulator_package)
    ensure_mt32(pack)
    cache = cache if cache is not None else cache_dir()
    use_compositor = fullscreen and compositor_fullscreen_available()
    conf = prepare_conf(pack, title, fullscreen and not use_compositor, cache)
    log_path = cache / "last-launch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            launch_argv(binary, conf, title.emulator_key, pack / "eXo"),
            cwd=pack / "eXo",
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    finally:
        log_file.close()
    if use_compositor:
        threading.Thread(target=fullscreen_on_compositor, args=(process.pid,), daemon=True).start()
    if wait:
        process.wait()
    return process
