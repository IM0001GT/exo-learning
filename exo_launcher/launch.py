"""Start a title in the DOSBox build its launch script asks for."""

from __future__ import annotations

import re
import subprocess
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
    if not text.endswith("\n"):
        text += "\n"
    return text


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
    rendered = render_conf(
        read_conf_text(pack, title),
        title.platform_dir,
        title.install_name,
        fullscreen,
        title.emulator_key,
        pack / "eXo",
    )
    directory = cache / "conf"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{title.id}.conf"
    path.write_text(rendered, encoding="utf-8")
    return path


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
    conf = prepare_conf(pack, title, fullscreen, cache)
    log_path = cache / "last-launch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [binary, "-conf", str(conf), "-exit"],
            cwd=pack / "eXo",
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    finally:
        log_file.close()
    if wait:
        process.wait()
    return process
