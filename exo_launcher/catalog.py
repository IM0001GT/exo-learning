"""Read the pack's LaunchBox and eXo metadata without unpacking the games."""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.etree import ElementTree

CACHE_VERSION = 1

EMULATORS = {
    "074": ("dosbox", "dosbox"),
    "staging": ("dosbox-staging", "dosbox-staging"),
    "x": ("dosbox-x", "dosbox-x"),
}

PLATFORM_DIR = {"dos": "eXoDOS", "win3x": "eXoWin3x"}
PLATFORM_LABEL = {"dos": "MS-DOS", "win3x": "Windows 3.1"}
META_ZIP = {"dos": "!DOSmetadata.zip", "win3x": "!Win3xmetadata.zip"}


class PackNotFound(Exception):
    """The launcher is installed, and the game pack is somewhere else."""


def looks_like_pack(path: Path) -> bool:
    return (
        (path / "LaunchBox.zip").is_file()
        and (path / "eXo" / "eXoDOS").is_dir()
        and (path / "eXo" / "eXoWin3x").is_dir()
    )


def find_pack() -> Path | None:
    """EXO_PACK, then the saved setting, then a checkout that sits inside the pack."""
    env = os.environ.get("EXO_PACK")
    if env:
        return Path(env).expanduser().resolve()
    configured = _configured_pack()
    if configured:
        return Path(configured).expanduser().resolve()
    nested = Path(__file__).resolve().parents[2]
    if looks_like_pack(nested):
        return nested
    return None


def pack_root() -> Path:
    found = find_pack()
    if found is None:
        raise PackNotFound(
            "This repository does not include the games.\n"
            "Point it at a copy of eXo's Retro Learning Pack you already have:\n"
            "  ./setup.sh \"/path/to/eXo's Retro Learning Pack\""
        )
    if not looks_like_pack(found):
        raise PackNotFound(
            f"{found} is not eXo's Retro Learning Pack.\n"
            "The folder needs LaunchBox.zip plus eXo/eXoDOS and eXo/eXoWin3x."
        )
    return found


def remember_pack(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not looks_like_pack(resolved):
        raise PackNotFound(
            f"{resolved} is not eXo's Retro Learning Pack.\n"
            "The folder needs LaunchBox.zip plus eXo/eXoDOS and eXo/eXoWin3x."
        )
    settings = load_settings()
    settings["pack"] = str(resolved)
    save_settings(settings)
    return resolved


def _configured_pack() -> str | None:
    path = settings_path()
    if not path.is_file():
        return None
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pack = stored.get("pack") if isinstance(stored, dict) else None
    return pack if isinstance(pack, str) and pack.strip() else None


def cache_dir() -> Path:
    override = os.environ.get("EXO_CACHE")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "exo-learning"


def settings_path() -> Path:
    override = os.environ.get("EXO_CONFIG")
    base = Path(override) if override else Path.home() / ".config" / "exo-learning"
    return base / "settings.json"


def load_settings() -> dict:
    path = settings_path()
    data = {"fullscreen": False}
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            stored = {}
        if isinstance(stored, dict):
            data.update(stored)
    data["fullscreen"] = bool(data.get("fullscreen", False))
    return data


def save_settings(settings: dict) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp437")


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def norm_name(value: str) -> str:
    text = value.casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


@dataclass
class Title:
    id: str
    title: str
    family: str
    platform_label: str
    year: str
    developer: str
    publisher: str
    notes: str
    short_id: str
    zip_name: str
    zip_rel: str | None
    install_name: str
    tops: list[str]
    packed_bytes: int
    unpacked_bytes: int
    emulator_key: str
    conf_in_zip: str
    meta_zip: str
    extras: list[str]
    platform_dir: str

    @property
    def available(self) -> bool:
        return bool(self.zip_rel)

    @property
    def emulator_bin(self) -> str:
        return EMULATORS[self.emulator_key][0]

    @property
    def emulator_package(self) -> str:
        return EMULATORS[self.emulator_key][1]

    def haystack(self) -> str:
        return " ".join(
            part.casefold()
            for part in (self.title, self.developer, self.publisher, self.year, self.notes, self.platform_label)
            if part
        )


@dataclass
class Catalog:
    titles: list[Title]
    art: dict[str, dict[str, str]]
    art_tokens: dict[str, dict[str, str]]

    def art_for(self, title: Title) -> str | None:
        index = self.art.get(title.family, {})
        name = norm_name(title.title)
        if name in index:
            return index[name]
        if name.startswith("the ") and name[4:] in index:
            return index[name[4:]]
        tokens = " ".join(sorted(name.split()))
        return self.art_tokens.get(title.family, {}).get(tokens)


class CatalogError(Exception):
    pass


class NotFound(CatalogError):
    pass


class Ambiguous(CatalogError):
    def __init__(self, query: str, matches: list[Title]):
        self.query = query
        self.matches = matches
        names = ", ".join(f"{item.title} ({item.platform_label})" for item in matches[:8])
        super().__init__(f"More than one title matches {query!r}: {names}")


def resolve_zip(directory: Path, zip_name: str) -> Path | None:
    direct = directory / zip_name
    if direct.is_file():
        return direct
    folded = zip_name.casefold()
    if not directory.is_dir():
        return None
    for entry in directory.iterdir():
        if entry.is_file() and entry.name.casefold() == folded:
            return entry
    return None


def _xml_text(game: ElementTree.Element, tag: str) -> str:
    element = game.find(tag)
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def _year(value: str) -> str:
    return value[:4] if len(value) >= 4 and value[:4].isdigit() else ""


def parse_platform_xml(data: bytes) -> dict[str, dict]:
    root = ElementTree.fromstring(data)
    found: dict[str, dict] = {}
    for game in root.findall("Game"):
        root_folder = _xml_text(game, "RootFolder")
        parts = root_folder.replace("/", "\\").split("\\")
        short = parts[-1] if parts else ""
        if not short:
            application = _xml_text(game, "ApplicationPath")
            parts = application.replace("/", "\\").split("\\")
            short = parts[-2] if len(parts) >= 2 else ""
        if not short:
            continue
        application = _xml_text(game, "ApplicationPath").replace("\\", "/")
        found[short.casefold()] = {
            "id": _xml_text(game, "ID") or short,
            "title": _xml_text(game, "Title") or short,
            "notes": _xml_text(game, "Notes"),
            "developer": _xml_text(game, "Developer"),
            "publisher": _xml_text(game, "Publisher"),
            "year": _year(_xml_text(game, "ReleaseDate")),
            "launch_name": Path(application).name.casefold(),
        }
    return found


_UNZIP_RE = re.compile(r'unzip\s+"([^"]+)"', re.IGNORECASE)
_EXIST_RE = re.compile(r'if\s+exist\s+"([^"]+)"', re.IGNORECASE)
_DOSBOX_RE = re.compile(r'"([^"]*dosbox\.exe)"', re.IGNORECASE)


def parse_install_bat(text: str) -> tuple[str, str]:
    unzip_match = _UNZIP_RE.search(text)
    exist_match = _EXIST_RE.search(text)
    zip_name = unzip_match.group(1) if unzip_match else ""
    exist_name = ""
    if exist_match:
        exist_name = exist_match.group(1).replace("/", "\\").rstrip("\\").split("\\")[-1]
    return zip_name, exist_name


def emulator_key(bat_text: str, family: str) -> str:
    chosen = ""
    for line in bat_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("rem "):
            continue
        match = _DOSBOX_RE.search(stripped)
        if match:
            chosen = match.group(1)
    if not chosen:
        return "x" if family == "win3x" else "074"
    path = chosen.replace("/", "\\").lower()
    if path.startswith(".\\dosbox\\ece\\"):
        return "staging" if family == "dos" else "x"
    if path.startswith(".\\dosbox\\dosbox.exe"):
        return "074"
    return "x"


def _title_from_bat(filename: str) -> tuple[str, str]:
    stem = Path(filename).stem
    match = re.match(r"^(.*)\s+\((\d{4})\)$", stem)
    if match:
        return match.group(1).strip(), match.group(2)
    return stem, ""


def inspect_zip(path: Path) -> tuple[list[str], int]:
    tops: list[str] = []
    seen: set[str] = set()
    unpacked = 0
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            unpacked += max(info.file_size, 0)
            name = info.filename.replace("\\", "/")
            if not name:
                continue
            top = name.split("/", 1)[0]
            if top and top not in seen:
                seen.add(top)
                tops.append(top)
    return tops, unpacked


def _scan_family(pack: Path, family: str) -> list[dict]:
    archive_path = pack / META_ZIP[family]
    platform_dir = PLATFORM_DIR[family]
    disk_dir = pack / "eXo" / platform_dir
    groups: dict[str, dict] = {}
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        for name in names:
            parts = name.split("/")
            if len(parts) < 5 or not parts[3]:
                continue
            short = parts[3]
            rel = "/".join(parts[4:])
            group = groups.setdefault(
                short,
                {"prefix": "/".join(parts[:4]), "conf": "", "install": "", "bats": [], "extras": []},
            )
            lowered = rel.lower()
            if rel == "dosbox.conf":
                group["conf"] = name
            elif lowered == "install.bat":
                group["install"] = name
            elif lowered.endswith(".bat") and "/" not in rel:
                group["bats"].append(name)
            elif lowered.startswith("extras/") and not name.endswith("/") and not lowered.endswith(".bat"):
                group["extras"].append(name)
        records = []
        for short, group in groups.items():
            if not group["install"] or not group["conf"]:
                continue
            install_text = decode_text(archive.read(group["install"]))
            zip_name, exist_name = parse_install_bat(install_text)
            bats = [(Path(name).name, decode_text(archive.read(name))) for name in group["bats"]]
            records.append(
                {
                    "short_id": short,
                    "zip_name": zip_name,
                    "exist_name": exist_name or short,
                    "bats": bats,
                    "conf_in_zip": group["conf"],
                    "extras": group["extras"],
                    "disk_dir": disk_dir,
                    "family": family,
                }
            )
    return records


def _stamp(pack: Path) -> str:
    digest = hashlib.sha256()
    for rel in ("LaunchBox.zip", "!DOSmetadata.zip", "!Win3xmetadata.zip", "XODOSMetadata.zip"):
        path = pack / rel
        stat = path.stat()
        digest.update(f"{rel}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    for family in ("dos", "win3x"):
        directory = pack / "eXo" / PLATFORM_DIR[family]
        names = sorted(name for name in os.listdir(directory) if name.lower().endswith(".zip"))
        for name in names:
            stat = (directory / name).stat()
            digest.update(f"{family}/{name}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    digest.update(f"version:{CACHE_VERSION}".encode())
    return digest.hexdigest()


def _build_art(pack: Path) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    folders = {
        "dos": "Images/MS-DOS/Box - Front/",
        "win3x": "Images/Win3x/Box - Front/",
    }
    exact: dict[str, dict[str, str]] = {"dos": {}, "win3x": {}}
    with zipfile.ZipFile(pack / "XODOSMetadata.zip") as archive:
        for name in archive.namelist():
            for family, prefix in folders.items():
                if not name.startswith(prefix) or name.endswith("/"):
                    continue
                stem = Path(name).stem
                match = re.match(r"^(.*)-(\d+)$", stem)
                base = match.group(1) if match else stem
                number = int(match.group(2)) if match else 1
                key = norm_name(base)
                current = exact[family].get(key)
                if current is None or number < _art_rank(current):
                    exact[family][key] = name
    tokens: dict[str, dict[str, str]] = {"dos": {}, "win3x": {}}
    for family, mapping in exact.items():
        buckets: dict[str, list[str]] = defaultdict(list)
        for key, path in mapping.items():
            buckets[" ".join(sorted(key.split()))].append(path)
        for key, paths in buckets.items():
            unique = list(dict.fromkeys(paths))
            if len(unique) == 1:
                tokens[family][key] = unique[0]
    return exact, tokens


def _art_rank(path: str) -> int:
    match = re.search(r"-(\d+)\.[^.]+$", path)
    return int(match.group(1)) if match else 1


def _overlay_xml(pack: Path) -> dict[str, dict[str, dict]]:
    with zipfile.ZipFile(pack / "LaunchBox.zip") as archive:
        return {
            "dos": parse_platform_xml(archive.read("Data/Platforms/MS-DOS.xml")),
            "win3x": parse_platform_xml(archive.read("Data/Platforms/Win3x.xml")),
        }


def build_catalog(pack: Path) -> Catalog:
    xml = _overlay_xml(pack)
    raw_records: list[dict] = []
    for family in ("dos", "win3x"):
        raw_records.extend(_scan_family(pack, family))

    zip_jobs: list[tuple[int, Path]] = []
    pending: list[dict] = []
    for record in raw_records:
        info = xml[record["family"]].get(record["short_id"].casefold(), {})
        bats = record["bats"]
        chosen_name, chosen_text = bats[0] if bats else ("", "")
        wanted = info.get("launch_name", "")
        for name, text in bats:
            if name.casefold() == wanted:
                chosen_name, chosen_text = name, text
                break
        guessed_title, guessed_year = _title_from_bat(chosen_name) if chosen_name else (record["short_id"], "")
        zip_path = resolve_zip(record["disk_dir"], record["zip_name"]) if record["zip_name"] else None
        pending.append(
            {
                **record,
                "xml": info,
                "zip_path": zip_path,
                "bat_text": chosen_text,
                "guessed_title": guessed_title,
                "guessed_year": guessed_year,
            }
        )
        if zip_path is not None:
            zip_jobs.append((len(pending) - 1, zip_path))

    inspected: dict[int, tuple[list[str], int]] = {}
    if zip_jobs:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(inspect_zip, path): index for index, path in zip_jobs}
            for future, index in futures.items():
                inspected[index] = future.result()

    titles: list[Title] = []
    for index, record in enumerate(pending):
        info = record["xml"]
        zip_path: Path | None = record["zip_path"]
        tops: list[str] = []
        unpacked = 0
        if index in inspected:
            tops, unpacked = inspected[index]
        exist_name = record["exist_name"]
        install_name = exist_name
        if tops:
            matched = next((top for top in tops if top.casefold() == exist_name.casefold()), None)
            install_name = matched or (tops[0] if len(tops) == 1 else exist_name)
        title_name = info.get("title") or record["guessed_title"] or record["short_id"]
        titles.append(
            Title(
                id=info.get("id") or record["short_id"],
                title=title_name,
                family=record["family"],
                platform_label=PLATFORM_LABEL[record["family"]],
                year=info.get("year") or record["guessed_year"],
                developer=info.get("developer", ""),
                publisher=info.get("publisher", ""),
                notes=info.get("notes", ""),
                short_id=record["short_id"],
                zip_name=record["zip_name"],
                zip_rel=str(zip_path.relative_to(pack)) if zip_path else None,
                install_name=install_name,
                tops=tops,
                packed_bytes=zip_path.stat().st_size if zip_path else 0,
                unpacked_bytes=unpacked,
                emulator_key=emulator_key(record["bat_text"], record["family"]),
                conf_in_zip=record["conf_in_zip"],
                meta_zip=META_ZIP[record["family"]],
                extras=record["extras"],
                platform_dir=PLATFORM_DIR[record["family"]],
            )
        )
    titles.sort(key=lambda item: norm_name(re.sub(r"^the ", "", item.title.casefold())))
    art, art_tokens = _build_art(pack)
    return Catalog(titles=titles, art=art, art_tokens=art_tokens)


def _catalog_from_cache(payload: dict) -> Catalog:
    titles = [Title(**item) for item in payload["titles"]]
    return Catalog(titles=titles, art=payload["art"], art_tokens=payload["art_tokens"])


def load_catalog(pack: Path | None = None, cache: Path | None = None, refresh: bool = False) -> Catalog:
    pack = pack or pack_root()
    cache = cache if cache is not None else cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    cache_file = cache / "catalog.json"
    stamp = _stamp(pack)
    if not refresh and cache_file.is_file():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = None
        if payload and payload.get("version") == CACHE_VERSION and payload.get("stamp") == stamp:
            return _catalog_from_cache(payload)
    catalog = build_catalog(pack)
    payload = {
        "version": CACHE_VERSION,
        "stamp": stamp,
        "titles": [asdict(title) for title in catalog.titles],
        "art": catalog.art,
        "art_tokens": catalog.art_tokens,
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    return catalog


def find_title(catalog: Catalog, query: str) -> Title:
    folded = query.casefold().strip()
    if not folded:
        raise NotFound("Name a title.")

    def unique(matches: list[Title]) -> Title | None:
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise Ambiguous(query, matches)
        return None

    exact = unique([title for title in catalog.titles if title.title.casefold() == folded])
    if exact:
        return exact
    zip_match = unique(
        [title for title in catalog.titles if title.zip_name and title.zip_name.casefold() == folded]
    )
    if zip_match:
        return zip_match
    stem_query = folded.removesuffix(".zip")
    stem = unique(
        [
            title
            for title in catalog.titles
            if title.zip_name and title.zip_name.casefold().removesuffix(".zip") == stem_query
        ]
    )
    if stem:
        return stem
    short = unique([title for title in catalog.titles if title.short_id.casefold() == folded])
    if short:
        return short
    contains = [title for title in catalog.titles if folded in title.title.casefold()]
    found = unique(contains)
    if found:
        return found
    raise NotFound(f"No title matches {query!r}.")


def read_conf_text(pack: Path, title: Title) -> str:
    member = title.conf_in_zip
    extracted = pack / member
    if extracted.is_file():
        return decode_text(extracted.read_bytes())
    with zipfile.ZipFile(pack / title.meta_zip) as archive:
        return decode_text(archive.read(member))


def materialize_member(pack: Path, archive_name: str, member: str, cache: Path | None = None) -> Path:
    extracted = pack / member
    if extracted.is_file():
        return extracted
    cache = cache if cache is not None else cache_dir()
    dest = cache / "files" / member
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(pack / archive_name) as archive:
            dest.write_bytes(archive.read(member))
    return dest
