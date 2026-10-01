import unittest

from projektsog.textutil import (alt, fold, fold_alt, fold_with_map, highlight_ranges,
                                 token_matches, tokenize)

SAMPLES = [
    "Forår 2026 RØD",
    "FORAAR_2026-RØD",
    "60 års Fødselsdag Råmateriale",
    "Dækcentret Årsmøde 2024",
    "Møblér med Malte - Møbler UDSALG.mp4",
    "FX9_7912.MXF",
    "(Z) Kunder 2026 (STUDIO)",
    "1. KUNDENAVN",
    "Straße ﬁle – test",
    "  __leading and trailing--  ",
    "Aabenraa aaa aaaa",
    "Ø-Vind",
    "Skærmbillede 3. sep. 2026, 13.22.26.png",
    "Crème brûlée Ångström",
    "",
    "æøåÆØÅ",
]


class FoldTests(unittest.TestCase):
    def test_fast_equals_reference(self):
        for s in SAMPLES:
            with self.subTest(s=s):
                ref, index_map = fold_with_map(s)
                self.assertEqual(fold(s), ref)
                self.assertEqual(len(ref), len(index_map))

    def test_danish_equivalences(self):
        self.assertEqual(fold("Forår 2026 RØD"), fold("forar 2026 rod"))
        self.assertEqual(fold("Forår"), fold("FORAAR"))
        self.assertEqual(fold("Dækcentret"), "daekcentret")
        self.assertEqual(fold("Ø-Vind"), "o vind")
        self.assertEqual(fold("Ø-Vindmølle"), "o vindmolle")

    def test_separators(self):
        self.assertEqual(fold("FX9_7912.MXF"), "fx9 7912 mxf")
        self.assertEqual(fold("(Z) Kunder 2026 (STUDIO)"), "z kunder 2026 studio")

    def test_tokenize(self):
        self.assertEqual(tokenize("Rikke  LINDHOLM rikke"), ["rikke", "lindholm"])
        self.assertEqual(tokenize("  "), [])
        self.assertEqual(tokenize("fx9_7912"), ["fx9", "7912"])

    def test_highlight(self):
        name = "Rikke Lindholm"
        self.assertEqual(highlight_ranges(name, ["lindholm"]), [[6, 14]])
        self.assertEqual(highlight_ranges("Forår 2026", ["forar"]), [[0, 5]])
        self.assertEqual(highlight_ranges("Dækcentret", ["daek"]), [[0, 3]])
        self.assertEqual(highlight_ranges("Dækcentret", ["centret"]), [[3, 10]])
        self.assertEqual(highlight_ranges("abc", []), [])

    def test_alt_oe_spelling(self):
        # ASCII "oe" for ø matches in both directions ...
        self.assertTrue(token_matches(fold("infomøde"), fold("Infomoede Skolen Kolding.mp4")))
        self.assertTrue(token_matches(fold("boegely"), fold("Bøgely Jul 2024")))
        self.assertTrue(token_matches(fold("moebler"), fold("Møbler med Malte")))
        self.assertTrue(token_matches(fold("køb"), fold("05b Koeb billet.webm")))
        # ... without breaking compounds that contain a real "o" + "e".
        self.assertTrue(token_matches(fold("eksport"), fold("Videoeksport")))
        self.assertTrue(token_matches(fold("effekt"), fold("Logoeffekt")))
        self.assertFalse(token_matches(fold("bagely"), fold("Bøgely")))
        self.assertEqual(fold_alt("Infomoede"), "infomode")
        self.assertEqual(alt("videoeksport"), "videoksport")
        self.assertEqual(fold_alt("Bøgely"), fold("Bøgely"))

    def test_highlight_alt(self):
        self.assertEqual(highlight_ranges("Infomoede", ["infomode"]), [[0, 9]])
        self.assertEqual(highlight_ranges("Bøgely", ["boegely"]), [[0, 6]])
        self.assertEqual(highlight_ranges("Videoeksport", ["eksport"]), [[5, 12]])


if __name__ == "__main__":
    unittest.main()
