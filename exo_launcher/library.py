"""Install and uninstall one title at a time.

A title is installed when the folder inside its zip exists under eXo/eXoDOS
or eXo/eXoWin3x. Uninstall deletes only those extracted names. The zip, the
DOSBox conf, and every other title stay where they are.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import zipfile
from pathlib import Path

from exo_launcher.catalog import Title


class LibraryError(Exception):
    pass


class UnsafePath(LibraryError):
    pass


class UnsafeZip(LibraryError):
    pass


class PartialInstall(LibraryError):
    pass


class MissingZip(LibraryError):
    pass


_BANNED = {"!dos", "!win3x"}


def platform_directory(pack: Path, title: Title) -> Path:
    return pack / "eXo" / title.platform_dir


def marker_path(pack: Path, title: Title) -> Path:
    return platform_directory(pack, title) / f".exo-installing-{title.install_name}"


def removal_targets(platform_dir: Path, tops: list[str]) -> list[Path]:
    """Paths uninstall is allowed to delete. Raises UnsafePath for anything else."""
    base = platform_dir.resolve()
    if not base.is_dir():
        raise UnsafePath(f"{platform_dir} is not a directory")
    targets: list[Path] = []
    for top in tops:
        if not isinstance(top, str) or not top or top in {".", ".."}:
            raise UnsafePath(repr(top))
        if "/" in top or "\\" in top or top.startswith("."):
            raise UnsafePath(top)
        if top.casefold() in {name.casefold() for name in _BANNED}:
            raise UnsafePath(top)
        if top.lower().endswith(".zip"):
            raise UnsafePath(top)
        raw = platform_dir / top
        if raw.is_symlink():
            if raw.parent.resolve() != base:
                raise UnsafePath(top)
            targets.append(raw)
            continue
        if raw.resolve().parent != base:
            raise UnsafePath(top)
        targets.append(raw)
    return targets


def _member_ok(name: str, allowed: set[str]) -> bool:
    raw = name.replace("\\", "/")
    if not raw or raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        return False
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return False
    return parts[0] in allowed


def _safe_extract(archive_path: Path, dest: Path, tops: list[str], progress=None) -> None:
    allowed = set(tops)
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        for info in members:
            if not _member_ok(info.filename, allowed):
                raise UnsafeZip(info.filename)
        total = len(members)
        for done, info in enumerate(members, start=1):
            archive.extract(info, dest)
            if progress and (done == total or done % 40 == 0):
                progress(done, total)


def _read_marker(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"done": False}
    return data if isinstance(data, dict) else {"done": False}


def install_state(pack: Path, title: Title) -> str:
    """installed, missing, partial, or unavailable."""
    if not title.available or not title.install_name:
        return "unavailable"
    primary = platform_directory(pack, title) / title.install_name
    marker = _read_marker(marker_path(pack, title))
    if marker is not None:
        if marker.get("done") and primary.is_dir():
            marker_path(pack, title).unlink(missing_ok=True)
            return "installed"
        return "partial"
    if primary.is_dir():
        return "installed"
    return "missing"


def _directory_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            file_path = Path(dirpath) / name
            try:
                total += file_path.lstat().st_size
            except OSError:
                continue
    return total


def remove_installed(platform_dir: Path, tops: list[str]) -> int:
    freed = 0
    for raw in removal_targets(platform_dir, tops):
        if not raw.exists() and not raw.is_symlink():
            continue
        freed += _directory_size(raw)
        if raw.is_symlink() or raw.is_file():
            raw.unlink()
        else:
            shutil.rmtree(raw)
    return freed


def ensure_mt32(pack: Path) -> None:
    """Unpack the MT-32 ROMs once. MIDI titles mount them from eXo/mt32."""
    if (pack / "eXo" / "mt32").is_dir():
        return
    util = pack / "eXo" / "util" / "util.zip"
    if not util.is_file():
        return
    with zipfile.ZipFile(util) as outer:
        payload = outer.read("mt32.zip")
    with zipfile.ZipFile(io.BytesIO(payload)) as inner:
        for info in inner.infolist():
            if not _member_ok(info.filename, {"mt32"}):
                raise UnsafeZip(info.filename)
        inner.extractall(pack / "eXo")


def install_title(pack: Path, title: Title, progress=None) -> None:
    if not title.available or not title.zip_rel:
        raise MissingZip(f"{title.title} has no zip in this pack.")
    if not title.tops or not title.install_name:
        raise MissingZip(f"{title.title} has nothing to unpack.")
    state = install_state(pack, title)
    if state == "installed":
        return
    if state == "partial":
        raise PartialInstall(title.title)
    dest = platform_directory(pack, title)
    removal_targets(dest, title.tops)
    archive_path = pack / title.zip_rel
    marker = marker_path(pack, title)
    marker.write_text(
        json.dumps({"done": False, "tops": title.tops}) + "\n",
        encoding="utf-8",
    )
    try:
        _safe_extract(archive_path, dest, title.tops, progress)
        primary = dest / title.install_name
        if not primary.is_dir():
            raise LibraryError(f"Unpacking {title.title} did not create {title.install_name}.")
        marker.write_text(json.dumps({"done": True, "tops": title.tops}) + "\n", encoding="utf-8")
    except Exception:
        raise
    marker.unlink(missing_ok=True)


def cleanup_partial(pack: Path, title: Title) -> int:
    dest = platform_directory(pack, title)
    freed = remove_installed(dest, title.tops) if title.tops else 0
    marker_path(pack, title).unlink(missing_ok=True)
    return freed


def uninstall_title(pack: Path, title: Title) -> int:
    if not title.tops:
        raise MissingZip(f"{title.title} has no unpacked files to remove.")
    freed = remove_installed(platform_directory(pack, title), title.tops)
    marker_path(pack, title).unlink(missing_ok=True)
    return freed
