"""Library window and the install, uninstall, and play commands."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from exo_launcher.catalog import (
    Ambiguous,
    Catalog,
    NotFound,
    PackNotFound,
    Title,
    cache_dir,
    find_title,
    format_bytes,
    load_catalog,
    load_settings,
    materialize_member,
    pack_root,
    remember_pack,
    save_settings,
)
from exo_launcher.launch import MissingEmulator, launch_title, prepare_conf
from exo_launcher.library import (
    PartialInstall,
    cleanup_partial,
    install_state,
    install_title,
    platform_directory,
    uninstall_title,
)

FILTERS = ("All", "MS-DOS", "Windows 3.1", "Installed", "Not installed")


def _die(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def _catalog() -> Catalog:
    print("Reading the pack…", file=sys.stderr)
    return load_catalog(pack_root(), cache_dir())


def _require_title(catalog: Catalog, name: str) -> Title | None:
    try:
        return find_title(catalog, name)
    except Ambiguous as error:
        print(error, file=sys.stderr)
        for title in error.matches:
            print(f"  {title.platform_label}: {title.title}", file=sys.stderr)
        return None
    except NotFound as error:
        print(error, file=sys.stderr)
        return None


def command_list(catalog: Catalog, installed_only: bool) -> int:
    pack = pack_root()
    shown = 0
    for title in catalog.titles:
        state = install_state(pack, title)
        if installed_only and state != "installed":
            continue
        mark = {"installed": "*", "partial": "!", "missing": " ", "unavailable": "x"}[state]
        year = title.year or "    "
        print(f"{mark} {title.platform_label:<12} {year:<4}  {title.title}")
        shown += 1
    print(f"{shown} titles", file=sys.stderr)
    return 0


def command_show(catalog: Catalog, name: str) -> int:
    title = _require_title(catalog, name)
    if title is None:
        return 1
    pack = pack_root()
    state = install_state(pack, title)
    print(title.title)
    print(f"Platform: {title.platform_label}")
    if title.year:
        print(f"Year: {title.year}")
    if title.developer:
        print(f"Developer: {title.developer}")
    print(f"State: {state}")
    print(f"Emulator: {title.emulator_bin} ({title.emulator_package})")
    if title.zip_name:
        print(f"Zip: {title.zip_name}")
    if title.available:
        print(f"Packed: {format_bytes(title.packed_bytes)}")
        print(f"Unpacked: {format_bytes(title.unpacked_bytes)}")
        print(f"Folder: eXo/{title.platform_dir}/{title.install_name}")
    else:
        print("This title's zip is not in the pack.")
    if title.notes:
        print()
        print(title.notes)
    return 0


def command_install(catalog: Catalog, name: str) -> int:
    title = _require_title(catalog, name)
    if title is None:
        return 1
    return install_one(title)


def install_one(title: Title) -> int:
    pack = pack_root()
    state = install_state(pack, title)
    if state == "unavailable":
        return _die(f"{title.title} has no zip in this pack.")
    if state == "partial":
        print(f"Removing the partial copy of {title.title}…")
        cleanup_partial(pack, title)
    if state == "installed":
        print(f"{title.title} is already installed.")
        return 0

    def progress(done: int, total: int) -> None:
        print(f"\rUnpacking {title.title}… {done}/{total}", end="", file=sys.stderr)

    try:
        install_title(pack, title, progress=progress)
    except Exception as error:
        print(file=sys.stderr)
        return _die(str(error))
    print(file=sys.stderr)
    print(f"Installed {title.title} ({format_bytes(title.unpacked_bytes)}).")
    return 0


def command_uninstall(catalog: Catalog, name: str) -> int:
    title = _require_title(catalog, name)
    if title is None:
        return 1
    pack = pack_root()
    state = install_state(pack, title)
    if state in {"missing", "unavailable"}:
        print(f"{title.title} is not installed.")
        return 0
    try:
        freed = uninstall_title(pack, title)
    except Exception as error:
        return _die(str(error))
    print(f"Uninstalled {title.title}. Freed {format_bytes(freed)}.")
    return 0


def command_play(catalog: Catalog, name: str, fullscreen: bool, auto_install: bool) -> int:
    title = _require_title(catalog, name)
    if title is None:
        return 1
    pack = pack_root()
    state = install_state(pack, title)
    if state == "unavailable":
        return _die(f"{title.title} has no zip in this pack.")
    if state != "installed":
        if not auto_install:
            return _die(f"{title.title} is not installed. Install it first, or use play --install.")
        if install_one(title) != 0:
            return 1
    try:
        process = launch_title(pack, title, fullscreen, cache_dir(), wait=True)
    except MissingEmulator as error:
        return _die(str(error), 2)
    except Exception as error:
        return _die(str(error))
    if process.returncode not in (0, None):
        return _die(f"{title.title} closed with status {process.returncode}. See {cache_dir() / 'last-launch.log'}")
    return 0


def command_conf(catalog: Catalog, name: str, fullscreen: bool) -> int:
    title = _require_title(catalog, name)
    if title is None:
        return 1
    path = prepare_conf(pack_root(), title, fullscreen, cache_dir())
    sys.stdout.write(path.read_text(encoding="utf-8"))
    return 0


def _remember_pack_arg(path: str) -> int | None:
    try:
        resolved = remember_pack(Path(path))
    except PackNotFound as error:
        return _die(str(error))
    os.environ["EXO_PACK"] = str(resolved)
    return None


def run_gui() -> int:
    try:
        pack_root()
    except PackNotFound as error:
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
            return _missing_pack_window(str(error))
        return _die(str(error))
    if not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        return _die("No display is available. Use install, uninstall, or play from a terminal.")
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, GLib, Gtk, Pango

    class TitleRow(Gtk.ListBoxRow):
        def __init__(self, title: Title):
            super().__init__()
            self.item = title
            self._state = "missing"
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(10)
            box.set_margin_end(10)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text.set_hexpand(True)
            self.name = Gtk.Label(label=title.title, xalign=0, hexpand=True)
            self.name.set_max_width_chars(42)
            self.name.set_ellipsize(Pango.EllipsizeMode.END)
            year = title.year or "—"
            self.meta = Gtk.Label(label=f"{year}  ·  {title.platform_label}", xalign=0)
            self.meta.add_css_class("dim")
            text.append(self.name)
            text.append(self.meta)
            self.badge = Gtk.Label(label="", xalign=1)
            self.badge.add_css_class("dim")
            box.append(text)
            box.append(self.badge)
            self.set_child(box)

        def set_state(self, state: str) -> None:
            self._state = state
            self.badge.set_label(
                {"installed": "Installed", "partial": "Partial", "unavailable": "Missing zip"}.get(state, "")
            )

    class LibraryWindow(Gtk.ApplicationWindow):
        def __init__(self, application, pack: Path):
            super().__init__(application=application, title="Retro Learning Pack")
            self.pack = pack
            self.catalog: Catalog | None = None
            self.rows: dict[tuple[str, str], TitleRow] = {}
            self.current: Title | None = None
            self.busy = False
            self.game_process = None
            self._art_token = None
            self.settings = load_settings()
            self.set_default_size(1100, 720)

            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.set_child(root)

            bar = Gtk.Box(spacing=8)
            bar.set_margin_top(8)
            bar.set_margin_bottom(8)
            bar.set_margin_start(8)
            bar.set_margin_end(8)
            self.search = Gtk.SearchEntry()
            self.search.set_placeholder_text("Search titles, makers, or descriptions")
            self.search.set_hexpand(True)
            self.filter = Gtk.DropDown.new_from_strings(list(FILTERS))
            self.filter.connect("notify::selected", lambda *_: self.listbox.invalidate_filter())
            self.full_switch = Gtk.Switch()
            self.full_switch.set_active(bool(self.settings.get("fullscreen")))
            self.full_switch.set_valign(Gtk.Align.CENTER)
            self.full_switch.connect("notify::active", self._fullscreen_changed)
            bar.append(self.search)
            bar.append(self.filter)
            bar.append(Gtk.Label(label="Fullscreen"))
            bar.append(self.full_switch)
            root.append(bar)

            self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            self.paned.set_vexpand(True)
            self.paned.set_position(460)
            root.append(self.paned)

            self.listbox = Gtk.ListBox()
            self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
            self.listbox.set_filter_func(self._filter_row)
            self.listbox.connect("row-selected", self._row_selected)
            self.listbox.connect("row-activated", lambda _box, row: self._play(row.item))
            self.search.connect("search-changed", lambda *_: self.listbox.invalidate_filter())
            scrolled = Gtk.ScrolledWindow()
            scrolled.set_child(self.listbox)
            scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.paned.set_start_child(scrolled)
            self.paned.set_shrink_start_child(False)

            detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            detail.set_margin_top(12)
            detail.set_margin_bottom(12)
            detail.set_margin_start(16)
            detail.set_margin_end(16)
            self.paned.set_end_child(detail)

            self.picture = Gtk.Picture()
            self.picture.set_size_request(180, 240)
            self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            self.picture.set_can_shrink(True)
            detail.append(self.picture)

            self.heading = Gtk.Label(label="Reading the pack…", xalign=0)
            self.heading.add_css_class("title-1")
            self.heading.set_wrap(True)
            detail.append(self.heading)

            self.subhead = Gtk.Label(label="", xalign=0)
            self.subhead.add_css_class("dim")
            self.subhead.set_wrap(True)
            detail.append(self.subhead)

            notes_scroll = Gtk.ScrolledWindow()
            notes_scroll.set_vexpand(True)
            notes_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.notes = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD)
            self.notes.set_left_margin(2)
            self.notes.set_right_margin(2)
            notes_scroll.set_child(self.notes)
            detail.append(notes_scroll)

            self.extras = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            detail.append(self.extras)

            actions = Gtk.Box(spacing=8)
            self.play_button = Gtk.Button(label="Play")
            self.play_button.add_css_class("suggested-action")
            self.play_button.connect("clicked", lambda *_: self._play(self.current))
            self.uninstall_button = Gtk.Button(label="Uninstall")
            self.uninstall_button.connect("clicked", lambda *_: self._uninstall(self.current))
            actions.append(self.play_button)
            actions.append(self.uninstall_button)
            detail.append(actions)

            self.status = Gtk.Label(label="Reading the pack…", xalign=0)
            self.status.set_margin_start(8)
            self.status.set_margin_end(8)
            self.status.set_margin_bottom(8)
            self.status.add_css_class("dim")
            root.append(self.status)
            self._set_actions(False)

        def _fullscreen_changed(self, switch, _param) -> None:
            self.settings["fullscreen"] = bool(switch.get_active())
            save_settings(self.settings)

        def _set_actions(self, enabled: bool) -> None:
            self.play_button.set_sensitive(enabled and not self.busy)
            self.uninstall_button.set_sensitive(enabled and not self.busy)

        def show_error(self, text: str) -> bool:
            self.busy = False
            self.status.set_label(text)
            dialog = Gtk.AlertDialog(message="Couldn't finish that", detail=text, buttons=["OK"])
            dialog.choose(self, None, lambda *_args: None)
            self._refresh_current()
            return False

        def set_catalog(self, catalog: Catalog) -> bool:
            self.catalog = catalog
            installed = 0
            for title in catalog.titles:
                row = TitleRow(title)
                state = install_state(self.pack, title)
                row.set_state(state)
                if state == "installed":
                    installed += 1
                self.rows[(title.family, title.short_id)] = row
                self.listbox.append(row)
            self.status.set_label(f"{len(catalog.titles)} titles  ·  {installed} installed")
            first = self.listbox.get_row_at_index(0)
            if first is not None:
                self.listbox.select_row(first)
            return False

        def _filter_row(self, row: TitleRow) -> bool:
            title = row.item
            query = self.search.get_text().strip().casefold()
            if query and query not in title.haystack():
                return False
            selected = self.filter.get_selected()
            choice = FILTERS[selected] if 0 <= selected < len(FILTERS) else "All"
            if choice == "MS-DOS" and title.family != "dos":
                return False
            if choice == "Windows 3.1" and title.family != "win3x":
                return False
            if choice in {"Installed", "Not installed"}:
                state = install_state(self.pack, title)
                if choice == "Installed" and state != "installed":
                    return False
                if choice == "Not installed" and state == "installed":
                    return False
            return True

        def _row_selected(self, _box, row) -> None:
            if row is None:
                self.current = None
                self._set_actions(False)
                return
            self._show(row.item)

        def _show(self, title: Title) -> None:
            self.current = title
            state = install_state(self.pack, title)
            self.heading.set_label(title.title)
            bits = [title.platform_label]
            if title.year:
                bits.append(title.year)
            if title.developer:
                bits.append(title.developer)
            self.subhead.set_label("  ·  ".join(bits))
            notes = title.notes or "No description came with this title."
            self.notes.get_buffer().set_text(notes)
            self._fill_extras(title)
            self._show_art(title)
            self._update_buttons(title, state)

        def _update_buttons(self, title: Title, state: str) -> None:
            emulator_ready = shutil.which(title.emulator_bin) is not None
            if not title.available:
                self.play_button.set_label("Not in this pack")
                self.play_button.set_sensitive(False)
                self.uninstall_button.set_sensitive(False)
                return
            if not emulator_ready:
                self.play_button.set_label(f"Needs {title.emulator_package}")
                self.play_button.set_sensitive(False)
            elif state == "installed":
                self.play_button.set_label("Play")
                self.play_button.set_sensitive(not self.busy)
            else:
                self.play_button.set_label("Install and play")
                self.play_button.set_sensitive(not self.busy)
            self.uninstall_button.set_sensitive(state in {"installed", "partial"} and not self.busy)

        def _fill_extras(self, title: Title) -> None:
            child = self.extras.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                self.extras.remove(child)
                child = next_child
            for member in title.extras:
                button = Gtk.Button(label=Path(member).name)
                button.set_halign(Gtk.Align.START)
                button.connect("clicked", self._open_extra, title, member)
                self.extras.append(button)

        def _open_extra(self, _button, title: Title, member: str) -> None:
            try:
                path = materialize_member(self.pack, title.meta_zip, member, cache_dir())
                subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as error:
                self.show_error(str(error))

        def _show_art(self, title: Title) -> None:
            token = object()
            self._art_token = token
            if self.catalog is None:
                return
            member = self.catalog.art_for(title)
            if not member:
                self.picture.set_paintable(None)
                return

            def work() -> None:
                try:
                    path = materialize_member(self.pack, "XODOSMetadata.zip", member, cache_dir())
                except Exception:
                    path = None
                GLib.idle_add(self._apply_art, token, path)

            threading.Thread(target=work, daemon=True).start()

        def _apply_art(self, token, path) -> bool:
            if token is not self._art_token:
                return False
            if path is None:
                self.picture.set_paintable(None)
            else:
                self.picture.set_paintable(Gdk.Texture.new_from_filename(str(path)))
            return False

        def _refresh_current(self) -> None:
            if self.current is None:
                return
            state = install_state(self.pack, self.current)
            row = self.rows.get((self.current.family, self.current.short_id))
            if row is not None:
                row.set_state(state)
            self.listbox.invalidate_filter()
            self._update_buttons(self.current, state)
            installed = sum(1 for item in self.rows.values() if item._state == "installed")
            total = len(self.rows)
            self.status.set_label(f"{total} titles  ·  {installed} installed")

        def _confirm(self, message: str, detail: str, accept: str, callback) -> None:
            dialog = Gtk.AlertDialog(message=message, detail=detail, buttons=["Cancel", accept])
            dialog.set_cancel_button(0)
            dialog.set_default_button(0)

            def done(source, result) -> None:
                try:
                    choice = source.choose_finish(result)
                except Exception:
                    return
                if choice == 1:
                    callback()

            dialog.choose(self, None, done)

        def _play(self, title: Title | None) -> None:
            if title is None or self.busy:
                return
            if not title.available:
                self.show_error(f"{title.title} has no zip in this pack.")
                return
            if shutil.which(title.emulator_bin) is None:
                self.show_error(str(MissingEmulator(title.emulator_bin, title.emulator_package)))
                return
            state = install_state(self.pack, title)
            if state == "partial":
                self._confirm(
                    f"Finish installing {title.title}?",
                    "The last unpack did not finish. The partial copy will be removed, then the title will install again.",
                    "Remove and install",
                    lambda: self._cleanup_then_install(title),
                )
                return
            if state != "installed":
                detail = (
                    f"This unpacks about {format_bytes(title.unpacked_bytes)} "
                    f"({format_bytes(title.packed_bytes)} packed). "
                    "The zip stays in the pack. Uninstall later deletes only the unpacked copy, "
                    "including any saved progress in this title."
                )
                self._confirm(
                    f"Install {title.title}?",
                    detail,
                    "Install and play",
                    lambda: self._install_then_play(title),
                )
                return
            self._launch(title)

        def _cleanup_then_install(self, title: Title) -> None:
            try:
                cleanup_partial(self.pack, title)
            except Exception as error:
                self.show_error(str(error))
                return
            self._install_then_play(title)

        def _install_then_play(self, title: Title) -> None:
            self.busy = True
            self._update_buttons(title, "missing")
            self.status.set_label(f"Unpacking {title.title}…")

            def progress(done: int, total: int) -> None:
                GLib.idle_add(self._progress, title, done, total)

            def work() -> None:
                try:
                    install_title(self.pack, title, progress=progress)
                except Exception as error:
                    GLib.idle_add(self.show_error, str(error))
                    return
                GLib.idle_add(self._launch, title)

            threading.Thread(target=work, daemon=True).start()

        def _progress(self, title: Title, done: int, total: int) -> bool:
            self.status.set_label(f"Unpacking {title.title}… {done} / {total} files")
            return False

        def _launch(self, title: Title) -> bool:
            self.busy = False
            try:
                process = launch_title(
                    self.pack,
                    title,
                    bool(self.full_switch.get_active()),
                    cache_dir(),
                    wait=False,
                )
            except Exception as error:
                return self.show_error(str(error))
            self.game_process = process
            self.status.set_label(f"Playing {title.title}. Ctrl+F9 closes the game. Ctrl+F10 releases the mouse.")
            self._refresh_current()

            def watch() -> None:
                code = process.wait()
                GLib.idle_add(self._closed, title, code)

            threading.Thread(target=watch, daemon=True).start()
            return False

        def _closed(self, title: Title, code: int) -> bool:
            if code == 0:
                self.status.set_label(f"Closed {title.title}.")
            else:
                self.status.set_label(
                    f"{title.title} closed with status {code}. Details are in {cache_dir() / 'last-launch.log'}."
                )
            self._refresh_current()
            return False

        def _uninstall(self, title: Title | None) -> None:
            if title is None or self.busy:
                return
            if self.game_process is not None and self.game_process.poll() is None:
                self.show_error("Close the game before uninstalling it.")
                return
            state = install_state(self.pack, title)
            if state not in {"installed", "partial"}:
                return
            folder = platform_directory(self.pack, title) / title.install_name
            estimate = title.unpacked_bytes
            if folder.is_dir():
                estimate = max(estimate, 0)
            detail = (
                f"This deletes the unpacked copy of {title.title} and any saved progress in it. "
                f"The packed zip stays, so you can install it again the next time you play. "
                f"About {format_bytes(estimate)} will be freed."
            )
            self._confirm(f"Uninstall {title.title}?", detail, "Uninstall", lambda: self._do_uninstall(title))

        def _do_uninstall(self, title: Title) -> None:
            self.busy = True
            self.status.set_label(f"Removing {title.title}…")

            def work() -> None:
                try:
                    freed = uninstall_title(self.pack, title)
                except Exception as error:
                    GLib.idle_add(self.show_error, str(error))
                    return
                GLib.idle_add(self._uninstalled, title, freed)

            threading.Thread(target=work, daemon=True).start()

        def _uninstalled(self, title: Title, freed: int) -> bool:
            self.busy = False
            self.status.set_label(f"Uninstalled {title.title}. Freed {format_bytes(freed)}.")
            self._refresh_current()
            if self.current is title:
                self._show(title)
            return False

    class LearningApp(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="com.retroexo.learningpack")
            self.pack = pack_root()

        def do_startup(self):
            Gtk.Application.do_startup(self)
            provider = Gtk.CssProvider()
            provider.load_from_string(
                """
                .dim { opacity: 0.72; }
                .title-1 { font-weight: 700; font-size: 1.4em; }
                """
            )
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        def do_activate(self):
            window = LibraryWindow(self, self.pack)
            window.present()

            def load() -> None:
                try:
                    catalog = load_catalog(self.pack, cache_dir())
                except Exception as error:
                    GLib.idle_add(window.show_error, str(error))
                    return
                GLib.idle_add(window.set_catalog, catalog)

            threading.Thread(target=load, daemon=True).start()

    app = LearningApp()
    return app.run(None)


def _missing_pack_window(message: str) -> int:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    class MissingPack(Gtk.Application):
        def do_activate(self):
            window = Gtk.ApplicationWindow(application=self, title="Retro Learning Pack")
            window.set_default_size(560, 220)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.set_margin_top(16)
            box.set_margin_bottom(16)
            box.set_margin_start(16)
            box.set_margin_end(16)
            label = Gtk.Label(label=message, xalign=0, wrap=True, selectable=True)
            close = Gtk.Button(label="Close")
            close.connect("clicked", lambda *_: self.quit())
            box.append(label)
            box.append(close)
            window.set_child(box)
            window.present()

    return MissingPack(application_id="com.retroexo.learningpack").run(None)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(prog="exo-learning", description="Play eXo's Retro Learning Pack on Linux.")
    parser.add_argument("--pack", help="Folder that already contains your copy of the pack")
    parser.add_argument("--fullscreen", action="store_true", help="Start games fullscreen")
    parser.add_argument("--windowed", action="store_true", help="Start games in a window")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("rebuild-cache", help="Reread the pack metadata")
    list_parser = sub.add_parser("list", help="List titles")
    list_parser.add_argument("--installed", action="store_true")

    for name, help_text in (
        ("show", "Show one title"),
        ("install", "Unpack one title"),
        ("uninstall", "Delete one unpacked title"),
        ("conf", "Print the DOSBox config that would be used"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("name")

    play = sub.add_parser("play", help="Launch an installed title")
    play.add_argument("name")
    play.add_argument("--install", action="store_true", help="Unpack first if it is still packed")

    args = parser.parse_args(argv)
    if args.pack and _remember_pack_arg(args.pack):
        return 1
    if not args.command:
        return run_gui()

    settings = load_settings()
    fullscreen = bool(settings.get("fullscreen"))
    if args.fullscreen:
        fullscreen = True
    if args.windowed:
        fullscreen = False

    try:
        if args.command == "rebuild-cache":
            print("Reading the pack…", file=sys.stderr)
            catalog = load_catalog(pack_root(), cache_dir(), refresh=True)
            print(f"Cached {len(catalog.titles)} titles.")
            return 0

        catalog = _catalog()
        if args.command == "list":
            return command_list(catalog, args.installed)
        if args.command == "show":
            return command_show(catalog, args.name)
        if args.command == "install":
            return command_install(catalog, args.name)
        if args.command == "uninstall":
            return command_uninstall(catalog, args.name)
        if args.command == "play":
            return command_play(catalog, args.name, fullscreen, args.install)
        if args.command == "conf":
            return command_conf(catalog, args.name, fullscreen)
    except PackNotFound as error:
        return _die(str(error))
    return _die(f"Unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
