"""Index engine: memory cards pass through (SPEC §15.13).

Camera cards (XDROOT, PRIVATE, DCIM …) are no locations at all – the import helper handles
them. Other small hot-plug cards without projects (a sound recorder's MUSIC folder) are searched
while they are in and forgotten a little after they come out, so new cards – and every format
of a card, which gives it a new serial – do not pile up as offline locations."""

from projektsog import indexer
from tests._index_engine_fixtures import EngineTestCase, module_env, project

_env = None
GB = 1024 ** 3


def setUpModule():
    global _env
    _env = module_env()


def tearDownModule():
    _env.cleanup()


class CardTests(EngineTestCase):
    def names(self, ix):
        return sorted((s["display_name"], s["online"]) for s in ix.list_sources())

    def take_out(self, ix, vol):
        self.world.volumes.remove(vol)
        self.rediscover(ix)

    def test_a_camera_card_is_no_location(self):
        self.world.volume("fx9", "7E3A0001", drive="E:", hotplug=True, size=109 * GB,
                          tree=["XDROOT\\Clip\\FX9_0001.MXF", "XDROOT\\MEDIAPRO.XML",
                                "PRIVATE\\M4ROOT\\CLIP\\C0001.MP4", "DCIM\\100MSDCF\\DSC0001.JPG",
                                "Noter\\liste.txt"])
        ix = self.start()
        self.settled(ix)
        self.assertEqual([s["display_name"] for s in ix.list_sources()], ["Noter"])
        self.assertEqual(ix.search("FX9_0001")["results"], [])

    def test_card_folders_on_a_fixed_disk_are_ordinary_folders(self):
        self.world.volume("data", "D15C0001", drive="D:", tree=project("XDROOT\\Rikke Lindholm"))
        ix = self.start()
        self.settled(ix)
        self.assertEqual(self.source(ix, "XDROOT")["included"], True)

    def test_a_recorder_card_is_forgotten_after_it_comes_out(self):
        card = self.world.volume("dr40", "D4400001", label="DR-40", drive="E:", hotplug=True,
                                 size=32 * GB, tree=["MUSIC\\DR0001.WAV"])
        ix = self.start()
        self.settled(ix)
        music = self.source(ix, "MUSIC")
        self.assertEqual((music["included"], music["auto_reason"]), (True, "Mediefiler fundet"))
        self.assertEqual(len(ix.search("DR0001")["results"]), 1)       # searchable while it is in
        self.take_out(ix, card)
        self.assertEqual(self.names(ix), [("MUSIC", False)])            # a short hiccup: kept
        self.world.clock.advance(indexer.PASSING_CARD_GRACE_S + 1)
        self.rediscover(ix)
        self.assertEqual(self.names(ix), [])
        self.wait_until(lambda: ix.search("DR0001")["results"] == [], message="its entries gone")

    def test_what_stays_when_a_card_comes_out(self):
        project_card = self.world.volume("p", "AAAA0001", drive="E:", hotplug=True, size=64 * GB,
                                         tree=project("Kunder\\Rikke Lindholm"))
        chosen = self.world.volume("c", "AAAA0002", drive="F:", hotplug=True, size=64 * GB,
                                   tree=["Lyd\\take1.wav"])
        big = self.world.volume("b", "AAAA0003", drive="G:", hotplug=True, tree=["Footage\\a.mp4"])
        ix = self.start()
        self.settled(ix)
        ix.set_source_mode(self.source(ix, "Lyd")["id"], "include")        # "Medtag altid"
        for vol in (project_card, chosen, big):
            self.world.volumes.remove(vol)
        self.rediscover(ix)
        self.world.clock.advance(indexer.PASSING_CARD_GRACE_S + 1)
        self.rediscover(ix)
        self.assertEqual(self.names(ix), [("Footage", False), ("Kunder", False), ("Lyd", False)])

    def test_cards_that_came_out_before_an_upgrade_are_tidied_up(self):
        self.world.volume("old", "0LD00001", drive="E:", hotplug=True, size=128 * GB,
                          tree=["MUSIC\\x.wav"])
        ix = self.start()
        self.settled(ix)
        ix.stop()
        self.world.volumes.clear()
        self.world.clock.advance(3600)
        ix = self.start()
        self.rediscover(ix)
        self.assertEqual(self.names(ix), [])
