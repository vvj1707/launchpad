import unittest
from datetime import date
from dateintel import extract, reconcile, status, find_date, from_ats
T = date(2026, 10, 18)
def best(html, **k):
    r = extract(html, T); return reconcile(r["candidates"], k.get("extra", ())), r

class T1(unittest.TestCase):
    def test_labelled_uk_dates(self):
        rec, _ = best("<p>Opening date: 1 September 2026</p><p>Closing date: 31st October 2026</p>")
        self.assertEqual(rec["opens"]["value"], date(2026, 9, 1)); self.assertEqual(rec["closes"]["value"], date(2026, 10, 31))
    def test_programme_start_is_not_deadline(self):
        rec, _ = best("<p>Applications close when filled. Programme starts 3 June 2027.</p>")
        self.assertIsNone(rec["closes"])
    def test_jsonld_beats_text(self):
        h = '<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-09-02","validThrough":"2026-11-15T00:00"}</script><p>Deadline: 1 Nov 2026</p>'
        rec, _ = best(h); self.assertEqual(rec["closes"]["value"], date(2026, 11, 15)); self.assertTrue(rec["needs_review"] is False or rec["conflicts"])
        self.assertEqual(len(rec["conflicts"]), 1)
    def test_rolling_and_closed(self):
        r = extract("<p>We review applications on a rolling basis.</p>", T); self.assertTrue(r["rolling"])
        self.assertTrue(extract("<p>This vacancy has closed.</p>", T)["closed_text"])
    def test_no_year_inference_is_low_confidence(self):
        rec, _ = best("<p>Deadline: 5 January</p>")
        self.assertEqual(rec["closes"]["value"], date(2027, 1, 5)); self.assertLess(rec["closes"]["confidence"], .6); self.assertTrue(rec["needs_review"])
    def test_ambiguous_numeric_and_dayfirst(self):
        self.assertEqual(find_date("03/04/2026", T)[0], date(2026, 4, 3)); self.assertTrue(find_date("03/04/2026", T)[2])
        self.assertEqual(find_date("25/12/2026", T)[0], date(2026, 12, 25)); self.assertFalse(find_date("25/12/2026", T)[2])
    def test_aggregator_never_beats_employer(self):
        agg = [dict(kind="closes", value=date(2026, 12, 1), source="aggregator", confidence=.4, evidence="trackr")]
        rec, _ = best("<p>Closing date: 15 November 2026</p>", extra=agg)
        self.assertEqual(rec["closes"]["value"], date(2026, 11, 15)); self.assertTrue(rec["conflicts"][0]["minor"])
    def test_status(self):
        rec, r = best("<p>Closing date: 1 October 2026</p>"); self.assertEqual(status(rec, r["rolling"], r["closed_text"], T), "closed")
        rec = reconcile(from_ats(opens="2026-12-01")); self.assertEqual(status(rec, False, False, T), "upcoming")
if __name__ == "__main__": unittest.main()
