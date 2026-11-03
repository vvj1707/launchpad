Launchpad

Run locally:  py launchpad.py   (Python 3.10+, no installs)
Open: http://localhost:8765
Public hosting: this folder includes `render.yaml` for Render. Push the folder to a GitHub repository, create a Render Web Service from that repository, and use the included Blueprint. The service listens on Render's `PORT` and stores SQLite data on a persistent `/var/data` disk.
Sync order: known employers first, then a one-off search for new employers from firm_names.txt (Name|eng or Name|fin).
The app keeps the last verified listings when a source is temporarily empty or unavailable; explicitly closed postings are removed. Finance shows quant/trading spring-week and insight-style openings rather than summer-internship rows.
Each run also reads Trackr's public UK Engineering 2027 summer-internship and UK Finance 2027 spring-week feeds, follows only public HTTPS employer links, and skips LinkedIn URLs.
When Trackr has programme metadata but no individual posting URL, Launchpad keeps the employer's public careers page as a clearly labeled fallback Apply destination; it does not pretend that page is an individually verified posting.
Paste a link: use the box at the top of the page for employer apply links found elsewhere.
Import a Trackr snapshot or any JSON: py launchpad.py --import-json "path\to\trackr-response.json"
Import the previous app: py launchpad.py --import-old "path\to\old\launchpad.sqlite3"
Imported and manually added rows are kept when employer feeds refresh, re-importing the same URL reactivates it, and the UI keeps these rows visible even when their source date has passed.
Find a firm's job board manually: py launchpad.py --discover https://firm.com/careers
Test: py -m unittest test_launchpad test_dateintel
