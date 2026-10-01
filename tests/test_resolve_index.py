"""ResolveBridge against the real Indexer and scan worker (RES2-1, R3-RES-1, SPEC §15.12).

The bridge re-maps the last walk's clip paths when a location it depends on finishes a scan.
The shallow scans after the window is shown (and the first scan of a new disk) make a new
project folder findable within seconds; the bridge must notice them through the real
``Source`` keys - not only through the test fakes' idea of them. Its follow notification tells
a folder that is gone from a mounted disk or an answering computer (``volume_present`` of the
real SourceRef) from a disk that is unplugged or a computer that is off.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any
from unittest import mock

from projektsog import indexer, resolve_bridge as rb
from projektsog.config import Config
from tests._index_engine_fixtures import EngineTestCase, make_tree, module_env, project
from tests._resolve_fakes import (
    ChildFactory, FakeClip, FakeClock, FakeFolder, FakeProject, FakeResolve, FakeWinui,
    source_ref, source_row)

_env: Any = None


def setUpModule() -> None:
    global _env
    _env = module_env()


def tearDownModule() -> None:
    _env.cleanup()


class BridgeEngineTestCase(EngineTestCase):
    """A ResolveBridge on the real Indexer (in-process helper; fake Resolve and Explorer)."""

    def bridge(self, ix: indexer.Indexer, resolve: FakeResolve, clock: FakeClock,
               follow: str = "off") -> rb.ResolveBridge:
        cfg = Config(path=os.path.join(tempfile.mkdtemp(dir=self.tmp), "config.json"))
        cfg.update({"resolve_follow": follow, "resolve_poll_s": 3})
        winui = FakeWinui()
        bridge = rb.ResolveBridge(
            cfg, self.bus, ix, process_running=winui.process_running,
            process_uptime=winui.process_uptime, open_folder=winui.open_folder,
            explorer_window_for=winui.explorer_window_for,
            call_with_timeout=winui.call_with_timeout,
            spawn_child=ChildFactory(lambda: resolve, clock), clock=clock)
        self.addCleanup(bridge.stop)
        return bridge


class ShallowScanRemapTests(BridgeEngineTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(indexer, "WINDOW_SHOWN_MIN_AGE_S", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start_disk(self) -> tuple[indexer.Indexer, str]:
        """A settled Indexer with one disk holding the project 'Gammelt projekt'."""
        vol = self.world.volume("disk", "11112222", label="Kunder 2026", drive="X:",
                                tree=project("Gammelt projekt", "Musik\\signatur.wav"))
        ix = self.start()
        self.settled(ix)
        return ix, vol["root"].rstrip("\\")

    def disk_source(self, ix: indexer.Indexer) -> dict[str, Any]:
        found = [s for s in ix.list_sources() if s["volume_serial"] == "11112222"]
        self.assertEqual(len(found), 1, ix.list_sources())
        return found[0]

    def test_fake_sources_use_the_real_source_keys(self) -> None:
        """The fakes may only use keys the registry really has (the bridge once read a
        'last_shallow_scan' that Indexer.list_sources() did not return), and their SourceRefs
        have the real SourceRef keys (they once lacked 'volume_present', R3-RES-1)."""
        ix, root = self.start_disk()
        real = set(self.disk_source(ix))
        self.assertLessEqual({"last_scan_end", "last_shallow_scan", "root_is_project",
                              "volume_present"}, real)
        self.assertLessEqual(set(source_row(1, "x", "C:\\x")), real)
        mapped = ix.map_paths([root + "\\Gammelt projekt\\Musik\\signatur.wav"])["folders"]
        self.assertEqual(set(source_ref()), set(mapped[0]["source"]))

    def test_shallow_scan_alone_remaps_the_media(self) -> None:
        ix, root = self.start_disk()
        old_clip = root + "\\Gammelt projekt\\Musik\\signatur.wav"
        new_clips = [root + "\\Nyt projekt\\Klip\\" + name for name in ("a.mov", "b.mov")]
        pool = FakeFolder("Master", [FakeClip(path) for path in (old_clip, *new_clips)])
        clock = FakeClock()
        bridge = self.bridge(ix, FakeResolve(FakeProject("Nyt projekt", pool)), clock)
        bridge._tick()
        state = bridge.state()
        self.assertEqual((state["primary"]["name"], state["primary"]["match"]),
                         ("Gammelt projekt", "media"))
        self.assertEqual([d["path"] for d in state["other_dirs"]], [root + "\\Nyt projekt\\Klip"])

        make_tree(root, project("Nyt projekt", "Klip\\a.mov", "Klip\\b.mov"))
        before = self.disk_source(ix)
        ix.on_window_shown()                # the window round: shallow scans only
        self.wait_until(lambda: ix.map_paths(new_clips)["folders"]
                        and self.disk_source(ix)["last_shallow_scan"]
                        != before["last_shallow_scan"], message="the shallow scan")
        after = self.disk_source(ix)
        self.assertEqual(after["last_scan_end"], before["last_scan_end"], "no deep scan ran")

        clock.advance(rb.REMAP_MIN_INTERVAL_S)
        bridge._tick()
        state = bridge.state()
        self.assertEqual((state["primary"]["name"], state["primary"]["match"]),
                         ("Nyt projekt", "media"))
        self.assertEqual(state["other_dirs"], [])
        self.assertEqual(pool.listed, 1, "re-mapped without a media pool walk")


class GoneFolderFollowTests(BridgeEngineTestCase):
    """R3-RES-1: the follow notification ('notify', the default) for clips in a folder that is
    gone - moved or renamed - while its disk stays mounted or its computer keeps answering:
    the UI's 'folder gone' line, no disk to connect and no computer to switch on."""

    def follow(self, ix: indexer.Indexer, name: str,
               clips: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
        """(the notification, the state) once the Resolve project ``name`` holding ``clips``
        has been open for FOLLOW_STABLE_S."""
        clock = FakeClock()
        pool = FakeFolder("Master", [FakeClip(path) for path in clips])
        bridge = self.bridge(ix, FakeResolve(FakeProject(name, pool)), clock, follow="notify")
        bridge._tick()
        clock.advance(rb.FOLLOW_STABLE_S)
        bridge._tick()
        notes = self.events_of("notify")
        self.assertEqual(len(notes), 1, notes)
        return notes[0], bridge.state()

    def by_id(self, ix: indexer.Indexer, sid: int) -> dict[str, Any]:
        return next(s for s in ix.list_sources() if s["id"] == sid)

    def test_folder_renamed_on_a_connected_disk(self) -> None:
        vol = self.world.volume("disk", "33334444", label="Arbejdsdisk", drive="D:",
                                tree=project("Kunder 2025\\Kunde X", "Klip\\a.mov", "Klip\\b.mov",
                                             "Klip\\c.mov") + ["Andet\\note.txt"])
        ix = self.start()
        self.settled(ix)
        root = vol["root"].rstrip("\\")
        clips = [os.path.join(root, "Kunder 2025", "Kunde X", "Klip", n)
                 for n in ("a.mov", "b.mov", "c.mov")]
        self.wait_until(lambda: ix.map_paths(clips)["folders"], message="the scan")
        kunder = self.source(ix, "Kunder 2025")
        os.rename(os.path.join(root, "Kunder 2025"), os.path.join(root, "Arkiv 2025"))
        self.rediscover(ix)                          # the year-end move; the disk stays
        gone = self.by_id(ix, kunder["id"])
        self.assertEqual((gone["online"], gone["volume_present"]), (False, True))

        note, state = self.follow(ix, "Kunde X", clips)
        self.assertEqual(note["text"], "Projektmappe: Kunde X\n3 klip ligger i mappen "
                                       "‘Kunder 2025’, som ikke findes længere")
        self.assertEqual((state["offline_clips"], state["offline_disks"]), (3, []))

    def test_share_no_longer_shared_by_a_computer_that_answers(self) -> None:
        folder = self.world.share("NAS", "Projekter", project(
            "Kunde Y", "Klip\\a.mov", "Klip\\b.mov", "Klip\\c.mov", "Klip\\d.mov"))
        self.world.share("NAS", "Andet", project("Noget andet", "Klip\\z.mov"))
        self.cfg.update({"hosts": ["NAS"]})
        ix = self.start()
        clips = [os.path.join(folder, "Kunde Y", "Klip", n)
                 for n in ("a.mov", "b.mov", "c.mov", "d.mov")]
        self.wait_until(lambda: ix.map_paths(clips)["folders"], message="the share's scan")
        projekter = self.source(ix, "Projekter")
        self.world.remote["NAS"] = ["Andet"]         # 'Projekter' is no longer shared
        self.rediscover(ix, "NAS")                   # ... and NAS still answers
        gone = self.by_id(ix, projekter["id"])
        self.assertEqual((gone["online"], gone["volume_present"]), (False, True))

        note, state = self.follow(ix, "Kunde Y", clips)
        self.assertEqual(note["text"], "Projektmappe: Kunde Y\n4 klip ligger i mappen "
                                       "‘Projekter’, som ikke findes længere")
        self.assertEqual((state["offline_clips"], state["offline_disks"]), (4, []))
