"""Install safety, DOSBox config rewriting, and the real pack catalog."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from exo_launcher.catalog import (
    PackNotFound,
    Title,
    emulator_key,
    find_title,
    load_catalog,
    looks_like_pack,
    pack_root,
    read_conf_text,
    resolve_zip,
)
from exo_launcher.launch import apply_title_overrides, launch_argv, render_conf
from exo_launcher.packages import INSTALL_SCRIPT, InstallError, missing_emulators, normalize_packages
from exo_launcher.library import (
    UnsafePath,
    UnsafeZip,
    install_state,
    install_title,
    removal_targets,
    uninstall_title,
)


def _title(**overrides) -> Title:
    data = dict(
        id="test",
        title="Example",
        family="dos",
        platform_label="MS-DOS",
        year="1983",
        developer="",
        publisher="",
        notes="",
        short_id="example",
        zip_name="Example.zip",
        zip_rel="eXo/eXoDOS/Example.zip",
        install_name="Example",
        tops=["Example"],
        packed_bytes=10,
        unpacked_bytes=10,
        emulator_key="074",
        conf_in_zip="eXo/eXoDOS/!dos/example/dosbox.conf",
        meta_zip="!DOSmetadata.zip",
        extras=[],
        platform_dir="eXoDOS",
    )
    data.update(overrides)
    return Title(**data)


class ConfTests(unittest.TestCase):
    def test_windows_mount_uses_the_zip_folder_spelling(self):
        source = (
            "[sdl]\r\nfullscreen=false\r\noutput=overlay\r\n"
            "[autoexec]\r\n"
            "mount c .\\eXoWin3X\\OTrail3x\r\n"
            "imgmount d .\\eXoWin3X\\OTrail3x\\cd\\cd.cue -t cdrom\r\n"
            "path=C:\\;c:\\windows\\\r\n"
        )
        exo = Path("/home/stealth13/eXo's Retro Learning Pack/eXo")
        rendered = render_conf(source, "eXoWin3x", "Otrail3x", False, "x", exo)
        self.assertIn(
            'mount c "/home/stealth13/eXo\'s Retro Learning Pack/eXo/eXoWin3x/Otrail3x"\n',
            rendered,
        )
        self.assertIn(
            'imgmount d "/home/stealth13/eXo\'s Retro Learning Pack/eXo/eXoWin3x/Otrail3x/cd/cd.cue" -t cdrom\n',
            rendered,
        )
        self.assertIn("output=opengl", rendered)
        self.assertIn("fullscreen=false", rendered)
        self.assertNotIn("\\", rendered)

    def test_dosbox_074_keeps_overlay_and_can_go_fullscreen(self):
        source = "fullscreen=false\noutput=overlay\nmount c .\\eXoDOS\\oregont\n"
        rendered = render_conf(source, "eXoDOS", "oregont", True, "074")
        self.assertIn("output=overlay", rendered)
        self.assertIn("fullscreen=true", rendered)
        self.assertIn("mount c ./eXoDOS/oregont", rendered)

    def test_dosbox_x_uses_the_pack_directory_without_asking(self):
        rendered = render_conf("[dosbox]\nmemsize=64\n[sdl]\nfullscreen=false\n", "eXoWin3x", "Game", False, "x")
        self.assertIn("working directory option=noprompt", rendered)
        argv = launch_argv("/usr/bin/dosbox-x", Path("/tmp/game.conf"), "x", Path("/pack/eXo"))
        self.assertIn("-nopromptfolder", argv)
        staging = launch_argv("/usr/bin/dosbox-staging", Path("/tmp/game.conf"), "staging", Path("/pack/eXo"))
        self.assertEqual(staging[staging.index("--working-dir") + 1], "/pack/eXo")
        plain = launch_argv("/usr/bin/dosbox", Path("/tmp/game.conf"), "074", Path("/pack/eXo"))
        self.assertNotIn("-nopromptfolder", plain)
        self.assertNotIn("--working-dir", plain)

    def test_thinkin_things_matches_the_stable_cpu(self):
        original = "[cpu]\ncore=auto\ncputype=auto\ncycles=auto\n[mixer]\nrate=44100\n"
        adjusted = apply_title_overrides(original, "TTC2")
        self.assertIn("core=normal", adjusted)
        self.assertIn("cputype=486_slow", adjusted)
        self.assertIn("cycles=65000", adjusted)
        self.assertIn("rate=44100", adjusted)
        self.assertEqual(apply_title_overrides(original, "TTC3"), original)

    def test_emulator_mapping(self):
        self.assertEqual(emulator_key(r'".\dosbox\dosbox.exe" -conf x', "dos"), "074")
        self.assertEqual(emulator_key(r'".\dosbox\ece\dosbox.exe" -conf x', "dos"), "staging")
        self.assertEqual(emulator_key(r'".\dosbox\ece\dosbox.exe" -conf x', "win3x"), "x")
        self.assertEqual(emulator_key(r'".\dosbox\x\dosbox.exe" -conf x', "win3x"), "x")
        bundled = 'rem ".\\dosbox\\ece\\dosbox.exe"\n".\\eXoWin3x\\SimTown\\dosbox\\dosbox.exe"\n'
        self.assertEqual(emulator_key(bundled, "win3x"), "x")


class SafetyTests(unittest.TestCase):
    def test_uninstall_refuses_metadata_zips_and_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            platform = Path(temporary)
            (platform / "!dos").mkdir()
            (platform / "keep.zip").write_text("zip", encoding="utf-8")
            (platform / "game").mkdir()
            with self.assertRaises(UnsafePath):
                removal_targets(platform, ["!dos"])
            with self.assertRaises(UnsafePath):
                removal_targets(platform, ["keep.zip"])
            with self.assertRaises(UnsafePath):
                removal_targets(platform, ["../elsewhere"])
            with self.assertRaises(UnsafePath):
                removal_targets(platform, [".exo-installing-game"])
            self.assertEqual(removal_targets(platform, ["game"]), [platform / "game"])

    def test_install_and_uninstall_touch_only_the_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dos = root / "eXo" / "eXoDOS"
            dos.mkdir(parents=True)
            (dos / "!dos").mkdir()
            (dos / "other.zip").write_bytes(b"stay")
            archive = dos / "Example.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("Example/GAME.BAT", "@echo hi\n")
                handle.writestr("Example/notes.txt", "saved")
            title = _title(zip_rel="eXo/eXoDOS/Example.zip")
            self.assertEqual(install_state(root, title), "missing")
            install_title(root, title)
            self.assertEqual(install_state(root, title), "installed")
            self.assertTrue((dos / "Example" / "GAME.BAT").is_file())
            install_title(root, title)
            self.assertEqual((dos / "other.zip").read_bytes(), b"stay")
            freed = uninstall_title(root, title)
            self.assertGreater(freed, 0)
            self.assertFalse((dos / "Example").exists())
            self.assertTrue((dos / "!dos").is_dir())
            self.assertEqual((dos / "other.zip").read_bytes(), b"stay")
            self.assertTrue(archive.is_file())
            self.assertEqual(install_state(root, title), "missing")

    def test_zip_slip_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dos = root / "eXo" / "eXoDOS"
            dos.mkdir(parents=True)
            archive = dos / "Example.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../evil.txt", "nope")
                handle.writestr("Example/ok.txt", "ok")
            title = _title(zip_rel="eXo/eXoDOS/Example.zip", tops=["Example"])
            with self.assertRaises(UnsafeZip):
                install_title(root, title)
            self.assertFalse((root / "evil.txt").exists())
            self.assertFalse((root / "eXo" / "evil.txt").exists())


class PackLocationTests(unittest.TestCase):
    def test_env_selects_a_pack_and_rejects_other_folders(self):
        previous = os.environ.get("EXO_PACK")
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                os.environ["EXO_PACK"] = str(root)
                with self.assertRaises(PackNotFound):
                    pack_root()
                (root / "LaunchBox.zip").write_bytes(b"PK")
                (root / "eXo" / "eXoDOS").mkdir(parents=True)
                (root / "eXo" / "eXoWin3x").mkdir()
                self.assertTrue(looks_like_pack(root))
                self.assertEqual(pack_root(), root.resolve())
        finally:
            if previous is None:
                os.environ.pop("EXO_PACK", None)
            else:
                os.environ["EXO_PACK"] = previous


class PackageTests(unittest.TestCase):
    def test_missing_emulators_follow_the_binaries(self):
        present = {"dosbox"}
        missing = missing_emulators(lambda name: name if name in present else None)
        self.assertEqual([item.package for item in missing], ["dosbox-staging", "dosbox-x"])

    def test_install_list_rejects_other_packages(self):
        self.assertEqual(normalize_packages(["dosbox-x", "dosbox", "dosbox-x"]), ["dosbox-x", "dosbox"])
        with self.assertRaises(InstallError):
            normalize_packages(["steam"])

    def test_staging_is_not_installed_over_dosbox(self):
        self.assertIn("conflicts with the dosbox package", INSTALL_SCRIPT)
        self.assertIn("yay -S --needed --noconfirm", INSTALL_SCRIPT)
        self.assertIn("dosbox-x", INSTALL_SCRIPT)
        self.assertNotIn('"${aur[@]}"', INSTALL_SCRIPT)


class ResolveTests(unittest.TestCase):
    def test_zip_lookup_ignores_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "Once Upon A Time - Baba Yaga (1991).zip").write_bytes(b"PK")
            found = resolve_zip(directory, "Once Upon a Time - Baba Yaga (1991).zip")
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found.name, "Once Upon A Time - Baba Yaga (1991).zip")


def _pack_available() -> bool:
    try:
        return looks_like_pack(pack_root())
    except PackNotFound:
        return False


@unittest.skipUnless(_pack_available(), "pack metadata is not here")
class PackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cache = Path(tempfile.mkdtemp(prefix="exo-test-"))
        os.environ["EXO_CACHE"] = str(cache)
        cls.cache = cache
        cls.catalog = load_catalog(pack_root(), cache, refresh=True)

    def test_counts_and_known_gaps(self):
        dos = [title for title in self.catalog.titles if title.family == "dos"]
        win = [title for title in self.catalog.titles if title.family == "win3x"]
        self.assertEqual(len(dos), 299)
        self.assertEqual(len(win), 368)
        keys = {(title.family, title.emulator_key) for title in self.catalog.titles}
        self.assertIn(("dos", "074"), keys)
        self.assertIn(("dos", "staging"), keys)
        self.assertEqual(sum(title.emulator_key == "074" for title in dos), 231)
        self.assertEqual(sum(title.emulator_key == "staging" for title in dos), 66)
        self.assertEqual(sum(title.emulator_key == "x" for title in dos), 2)
        self.assertTrue(all(title.emulator_key == "x" for title in win))

        baba = find_title(self.catalog, "Once Upon a Time - Baba Yaga (1991).zip")
        self.assertIsNotNone(baba.zip_rel)
        self.assertTrue((pack_root() / baba.zip_rel).is_file())

        dino = find_title(self.catalog, "I can be a Dinosaur Finder (1997).zip")
        self.assertTrue((pack_root() / dino.zip_rel).is_file())

        pumpkin = find_title(self.catalog, "3D Pumpkin Puzzles")
        self.assertFalse(pumpkin.available)
        self.assertEqual(install_state(pack_root(), pumpkin), "unavailable")

    def test_oregon_trail_conf_and_box_art(self):
        dos_oregon = next(
            title
            for title in self.catalog.titles
            if title.family == "dos" and title.short_id.casefold() == "oregont"
        )
        self.assertEqual(dos_oregon.title, "The Oregon Trail")
        rendered = render_conf(
            read_conf_text(pack_root(), dos_oregon),
            dos_oregon.platform_dir,
            dos_oregon.install_name,
            False,
            dos_oregon.emulator_key,
            pack_root() / "eXo",
        )
        self.assertNotIn("\\", rendered)
        self.assertNotIn("cd ..", rendered)
        exo = str((pack_root() / "eXo").as_posix())
        self.assertIn(f"{exo}/{dos_oregon.platform_dir}/{dos_oregon.install_name}", rendered)
        art = self.catalog.art_for(dos_oregon)
        self.assertIsNotNone(art)
        self.assertIn("Box - Front", art or "")

        windows = next(title for title in self.catalog.titles if title.short_id == "Otrail3x")
        rendered = render_conf(
            read_conf_text(pack_root(), windows),
            windows.platform_dir,
            windows.install_name,
            True,
            windows.emulator_key,
            pack_root() / "eXo",
        )
        self.assertIn(f"{exo}/eXoWin3x/{windows.install_name}", rendered)
        self.assertNotIn("OTrail3x", rendered)
        self.assertNotIn("\\", rendered)

    def test_cache_round_trip(self):
        again = load_catalog(pack_root(), self.cache)
        self.assertEqual(len(again.titles), len(self.catalog.titles))
        payload = json.loads((self.cache / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)


if __name__ == "__main__":
    unittest.main()
