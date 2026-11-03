import os, tempfile, unittest, threading, json
import launchpad as L
L.DB = os.path.join(tempfile.mkdtemp(), "t.db"); L.PAGE_DELAY = 0
ENG = dict(name="Acme", ats="greenhouse", slug="acme", tab="eng"); FIN = dict(name="Q", ats="lever", slug="q", tab="fin")
def J(role, desc=""): return dict(role=role, url="https://x.example/" + role.replace(" ", "-"), loc="London", opened="2026-09-01", desc=desc)
class T(unittest.TestCase):
    def ok(self, f, role, desc=""): return L.classify(f, J(role, desc))[0] is not None
    def test_exclusions(self):
        for r in ["Graduate Engineer", "Engineering Apprentice", "Industrial Placement Engineer", "Year in Industry Intern", "Mechanical Intern 12 month placement", "Senior Intern Lead", "PhD Intern"]:
            self.assertFalse(self.ok(ENG, r), r)
        self.assertTrue(self.ok(ENG, "Mechanical Engineering Summer Internship"))
    def test_duration(self):
        self.assertFalse(self.ok(ENG, "Aerospace Intern", "The internship lasts 6 months."))
        self.assertTrue(self.ok(ENG, "Aerospace Intern", "Duration: 10 weeks."))
        self.assertTrue(self.ok(ENG, "Aerospace Intern", "Duration: 3 months."))
    def test_restricted_and_finance(self):
        self.assertFalse(self.ok(FIN, "Women in Quant Spring Week", "quant"))
        self.assertFalse(self.ok(FIN, "Spring Week Insight", "Our recruitment team"))
        self.assertTrue(self.ok(FIN, "Quantitative Trading Internship"))
    def test_eligibility(self):
        c, _ = L.classify(ENG, J("Civil Summer Internship", "Open to penultimate year students. We offer a bursary."))
        self.assertIn("penultimate", c["elig"])
    def fakeget(self, status=200, body="<p>Closing date: 30 November 2026</p>"):
        L.get = lambda u, limit=0: (status, u, body); L.allowed = lambda u: True
    def test_sync_and_cached_fallback(self):
        con = L.db(); self.fakeget()
        L.ADAPTERS["greenhouse"] = lambda s: [J("Civil Summer Internship"), J("Graduate Engineer")]
        L.sync_firm(con, ENG); con.commit()
        r = con.execute("SELECT * FROM listings WHERE active=1").fetchall(); self.assertEqual(len(r), 1); self.assertEqual(r[0]["deadline"], "2026-11-30")
        def boom(s): raise OSError("429")
        L.ADAPTERS["greenhouse"] = boom; L.sync_firm(con, ENG)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM listings WHERE active=1").fetchone()[0], 1)
        L.ADAPTERS["greenhouse"] = lambda s: []; L.sync_firm(con, ENG)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM listings WHERE active=1").fetchone()[0], 1)
        self.fakeget(body="<p>This vacancy has closed.</p>"); L.ADAPTERS["greenhouse"] = lambda s: [J("Civil Summer Internship")]; L.sync_firm(con, ENG)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM listings WHERE active=1").fetchone()[0], 0)
    def test_dead_link_rejected(self):
        con = L.db(); self.fakeget(status=404); L.ADAPTERS["lever"] = lambda s: [J("Quant Trading Internship")]
        L.sync_firm(con, FIN); self.assertEqual(con.execute("SELECT COUNT(*) FROM listings WHERE firm='Q'").fetchone()[0], 0)
    def test_sync_lock(self):
        L.LOCK.acquire(); self.assertFalse(L.sync()); L.LOCK.release()
    def test_http_security(self):
        import urllib.request, urllib.error
        urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
        srv = L.ThreadingHTTPServer(("127.0.0.1", 0), L.H); p = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start()
        u = f"http://127.0.0.1:{p}"
        self.assertEqual(urllib.request.urlopen(u + "/api/listings").status, 200)
        with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(urllib.request.Request(u + "/api/state", b"{}", method="POST"))
        self.assertEqual(e.exception.code, 403)
        r = urllib.request.urlopen(urllib.request.Request(u + "/api/state", b'{"st":{},"w":{"a":1}}', {"X-Launchpad": "1"}, method="POST")); self.assertEqual(r.status, 200)
        self.assertEqual(json.load(urllib.request.urlopen(u + "/api/state"))["w"], {"a": 1})
        h = urllib.request.urlopen(u + "/").read().decode(); self.assertIn("nonce=", h)
        with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(u + "/launchpad.py")
        srv.shutdown()
class T2(unittest.TestCase):
    def test_level_and_type(self):
        c, _ = L.classify(ENG, J("Summer Internship", "Open to penultimate year and final year students."))
        self.assertEqual(c["level"], "Penultimate year, Final year"); self.assertEqual(c["ptype"], "Summer internship")
    def test_workday_posted(self):
        self.assertEqual(L._posted("Posted Yesterday"), (L.date.today() - L.timedelta(days=1)).isoformat()); self.assertIsNone(L._posted("Posted 30+ Days Ago"))
    def test_discover(self):
        h = '<a href="https://boards.greenhouse.io/acme">x</a><a href="https://jobs.lever.co/foo-bar/1">y</a><a href="https://bank.wd3.myworkdayjobs.com/en-GB/External/job/x">z</a>'
        f = L.discover_html(h); self.assertIn({"ats": "greenhouse", "slug": "acme"}, f); self.assertIn({"ats": "workday", "slug": "bank|wd3|External"}, f)
    def test_migration_of_old_db(self):
        import sqlite3; p = L.DB; L.DB = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(L.DB); c.execute("CREATE TABLE listings(id TEXT PRIMARY KEY,firm,role,tab,cat,loc,opened,deadline,rolling INT,url,elig,evidence,source,verified_at,active INT)"); c.commit(); c.close()
        cols = [r[1] for r in L.db().execute("PRAGMA table_info(listings)")]; L.DB = p; self.assertIn("level", cols)
class T3(unittest.TestCase):
    def test_slugs(self):
        s = [x for x, _ in L.slugs("BAE Systems")]; self.assertIn("baesystems", s); self.assertIn("bae-systems", s); self.assertEqual(L.slugs("Arm"), [("arm", True)])
    def test_import_and_recheck(self):
        import sqlite3, urllib.error
        L.DB = os.path.join(tempfile.mkdtemp(), "fresh.db")
        p = os.path.join(tempfile.mkdtemp(), "old.sqlite3"); c = sqlite3.connect(p)
        c.execute("CREATE TABLE listings(company,title,apply_url,location,deadline,verified)")
        c.executemany("INSERT INTO listings VALUES(?,?,?,?,?,?)", [("A", "Summer Intern", "https://a.example/1", "UK", "2026-12-01", "2026-10-14T10:00:00"), ("B", "Graduate Engineer", "https://b.example/2", "UK", None, 1),
                      ("C", "Summer Intern", "https://c.example/3", "UK", None, 0), ("D", "Summer Intern", "https://d.example/4", "UK", None, 1), ("E", "Summer Intern", "https://e.example/5", "UK", None, 1)]); c.commit(); c.close()
        L.import_old(p); con = L.db()
        ids = lambda: sorted(r[0] for r in con.execute("SELECT firm FROM listings WHERE source LIKE 'Imported%' AND active=1"))
        self.assertEqual(ids(), ["A", "D", "E"])
        def fake(u, limit=0, data=None):
            if "d.example" in u: raise urllib.error.HTTPError(u, 404, "x", {}, None)
            if "e.example" in u: raise OSError("timeout")
            return 200, u, "<p>Closing date: 30 November 2026</p>"
        L.get = fake; L.allowed = lambda u: True; L.recheck(con)
        self.assertEqual(ids(), ["A", "E"])
class T4(unittest.TestCase):
    def test_discipline_from_title_first(self):
        c, _ = L.classify(ENG, J("Knowledge Management Intern", "You will manage materials and training content."))
        self.assertEqual(c["cat"], "Materials")
        c, _ = L.classify(ENG, J("Aerodynamics Summer Intern", "Work with materials and software."))
        self.assertEqual(c["cat"], "Aerospace")
class T5(unittest.TestCase):
    def test_clean_url(self):
        self.assertEqual(L.clean("https://c.example/job/R1/Intern?utm_source=linkedin&utm_medium=phenom-feeds&x=1"), "https://c.example/job/R1/Intern?x=1")
    def test_sitemap_adapter(self):
        ld = '<script type="application/ld+json">{"@type":"JobPosting","title":"Summer Placement Mechanical Engineer","datePosted":"2026-10-01","validThrough":"2026-12-01","description":"<p>Duration 10 weeks.</p>","jobLocation":{"address":{"addressLocality":"Newcastle upon Tyne","addressCountry":"GB"}}}</script>'
        pages = {"https://c.example/robots.txt": "Sitemap: https://c.example/sm.xml", "https://c.example/sm.xml": "<urlset><url><loc>https://c.example/job/R1/Summer-Placement-Mechanical-Engineer</loc></url><url><loc>https://c.example/job/R2/Senior-Engineer</loc></url><url><loc>https://c.example/job/R3/Summer-Intern-Civil</loc></url><url><loc>https://evil.example/job/R4/Summer-Intern</loc></url></urlset>",
                 "https://c.example/job/R1/Summer-Placement-Mechanical-Engineer": "<html>" + ld + "</html>", "https://c.example/job/R3/Summer-Intern-Civil": "<html><p>Shell page only</p></html>"}
        L.get = lambda u, limit=0, data=None: (200, u, pages[u]); L.allowed = lambda u: True
        r = L.a_sitemap("c.example"); self.assertEqual(len(r), 1); self.assertEqual(r[0]["loc"], "Newcastle upon Tyne, GB"); self.assertEqual(r[0]["opened"], "2026-10-01")
        L.DB = os.path.join(tempfile.mkdtemp(), "s.db"); con = L.db(); L.ADAPTERS["sitemap"] = lambda s: r
        L.sync_firm(con, dict(name="C", ats="sitemap", slug="c.example", tab="eng")); row = con.execute("SELECT deadline FROM listings WHERE active=1").fetchone(); self.assertEqual(row[0], "2026-12-01")
class T6(unittest.TestCase):
    def test_import_trackr_style_json(self):
        L.DB = os.path.join(tempfile.mkdtemp(), "j.db"); p = os.path.join(tempfile.mkdtemp(), "t.json")
        rec = lambda n, t, u: {"company": {"name": n}, "title": t, "applyUrl": u, "closingDate": 1796083200000, "location": "UK"}
        json.dump({"data": {"opportunities": [rec("Acme", "Summer Intern", "https://a.example/1?utm_source=x"), rec("Beta", "Graduate Engineer", "https://b.example/2"), rec("Gamma", "Summer Placement", "http://c.example/3"), rec("Delta", "Summer Intern", "https://d.example/4")]}}, open(p, "w"))
        self.assertEqual(L.import_json(p), 2); r = L.db().execute("SELECT firm,url,deadline FROM listings ORDER BY firm").fetchall()
        self.assertEqual([x[0] for x in r], ["Acme", "Delta"]); self.assertEqual(r[0][1], "https://a.example/1"); self.assertTrue(r[0][2].startswith("2026-"))
    def test_add_url_rules(self):
        self.assertFalse(L.add_url("https://www.linkedin.com/jobs/view/1", "eng")[0]); self.assertFalse(L.add_url("http://x.example/a", "eng")[0])
        self.assertFalse(L.public_host("127.0.0.1")); self.assertFalse(L.add_url("https://127.0.0.1/job", "eng")[0])
    def test_add_url_success_and_rejection(self):
        L.DB = os.path.join(tempfile.mkdtemp(), "a.db"); L.public_host = lambda h: True; L.allowed = lambda u: True
        ld = '<script type="application/ld+json">{"@type":"JobPosting","title":"Summer Placement Mechanical Engineer","datePosted":"2026-10-01","hiringOrganization":{"name":"Leonardo"},"jobLocation":{"address":{"addressLocality":"Newcastle upon Tyne"}}}</script>'
        L.get = lambda u, limit=0, data=None: (200, u, "<html>" + ld + "<p>Closing date: 30 November 2026</p></html>")
        ok, msg = L.add_url("https://c.example/job/R1/Summer-Placement?utm_source=linkedin", "eng"); self.assertTrue(ok, msg)
        r = L.db().execute("SELECT firm,loc,deadline,url FROM listings").fetchone(); self.assertEqual(tuple(r), ("Leonardo", "Newcastle upon Tyne", "2026-11-30", "https://c.example/job/R1/Summer-Placement"))
        L.get = lambda u, limit=0, data=None: (200, u, ld.replace("Summer Placement Mechanical Engineer", "Graduate Mechanical Engineer"))
        self.assertFalse(L.add_url("https://c.example/job/R2/Graduate", "eng")[0])
class T7(unittest.TestCase):
    def test_sync_does_not_deactivate_imported_rows(self):
        L.DB = os.path.join(tempfile.mkdtemp(), "preserve.db")
        con = L.db()
        con.execute("INSERT INTO listings(id,firm,role,tab,cat,url,source,active,ptype) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("imported-1", "Acme", "Mechanical Summer Intern", "eng", "Mechanical", "https://a.example/imported", "Imported from a Trackr snapshot, link rechecked on sync", 1, "Summer internship"))
        L.allowed = lambda u: True
        L.get = lambda u, limit=0, data=None: (200, u, "<p>Closing date: 30 November 2026</p>")
        L.ADAPTERS["greenhouse"] = lambda s: [J("Civil Summer Internship")]
        L.sync_firm(con, ENG)
        self.assertEqual(con.execute("SELECT active FROM listings WHERE id='imported-1'").fetchone()[0], 1)

    def test_reimport_reactivates_existing_url(self):
        L.DB = os.path.join(tempfile.mkdtemp(), "reactivate.db")
        con = L.db()
        url = "https://a.example/imported"
        con.execute("INSERT INTO listings(id,firm,role,tab,cat,url,source,active,ptype) VALUES(?,?,?,?,?,?,?,?,?)",
                    (L.hashlib.sha1(url.encode()).hexdigest()[:16], "Old", "Old title", "eng", "Other", url, "Imported from a Trackr snapshot, link rechecked on sync", 0, "Internship"))
        rows = [{"company": "Acme", "title": "Mechanical Summer Intern", "applyUrl": url}]
        self.assertEqual(L.ingest(rows, "a Trackr snapshot"), 1)
        self.assertEqual(con.execute("SELECT active,firm,role FROM listings WHERE url=?", (url,)).fetchone()[:], (1, "Acme", "Mechanical Summer Intern"))

    def test_trackr_public_feed_skips_linkedin(self):
        old = L.get
        try:
            payload = {"programmes": [
                {"name": "Summer Engineering Intern", "url": "https://jobs.example/1", "company": {"name": "Aero"}, "categories": ["Aerospace"], "locations": ["London"]},
                {"name": "Engineer Intern", "url": "https://www.linkedin.com/jobs/view/1", "company": {"name": "LinkedIn Co"}}
            ]}
            L.get = lambda u, limit=0, data=None: (200, u, json.dumps(payload))
            rows = L.a_trackr("UK|Engineering|2027|summer-internships")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["firm"], "Aero")
            self.assertNotIn("linkedin", rows[0]["url"])
        finally:
            L.get = old
    def test_trackr_public_feed_uses_employer_careers_fallback(self):
        old = L.get
        try:
            payload = {"programmes": [
                {"id": "pg-mech-2027", "name": "Mechanical Engineering Internship 2027", "url": None,
                 "company": {"name": "Procter & Gamble", "careersSite": "https://www.pgcareers.com/uk/en"},
                 "categories": ["Mechanical"], "locations": ["Reading"]}
            ]}
            L.get = lambda u, limit=0, data=None: (200, u, json.dumps(payload))
            rows = L.a_trackr("UK|Engineering|2027|summer-internships")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["firm"], "Procter & Gamble")
            self.assertEqual(rows[0]["trackr_fallback"], True)
            self.assertIn("launchpad_trackr=pg-mech-2027", rows[0]["url"])
        finally:
            L.get = old
if __name__ == "__main__": unittest.main()
