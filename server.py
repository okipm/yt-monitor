#!/usr/bin/env python3
"""YT Monitor: local dashboard for public stats of my channels. Run: python3 server.py → http://localhost:8765"""
import html, json, re, time, urllib.error, urllib.request, xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).parent
CHANNELS = ["@SejarahMasCaping", "@GoldenParachuteClub", "@bumi.ganjil", "@strangeplanetarchive", "@MapPebbles"]
HISTORY = HERE / "history.json"  # one snapshot per channel per hour, for "change since yesterday"
CACHE_SECONDS = 600
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "m": "http://search.yahoo.com/mrss/"}
_cache = {"t": 0, "data": None}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def parse_subs(text):
    """'1.2K subscribers' -> 1200; None if not found."""
    m = re.search(r"([\d.,]+)\s*([KMB]?)\s+subscribers?", text or "")
    if not m:
        return None
    return round(float(m.group(1).replace(",", "")) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)])


def fetch_channel(handle):
    # shortcut: subscriber count is scraped from the channel page HTML (rounded by YouTube, may break if YouTube changes markup); upgrade to YouTube Data API v3 if it breaks
    page = get(f"https://www.youtube.com/{handle}")
    cid = re.search(r'"externalId":"(UC[^"]+)"', page).group(1)
    title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    subs_m = re.search(r"[\d.,]+[KMB]? subscribers?", page)
    vids_m = re.search(r'"content":"([\d.,]+[KMB]?) videos?"', page)  # header chip; plain "1 video" also appears in a UI string table
    avatar = re.search(r'<meta property="og:image" content="([^"]*)"', page)
    # uploads playlist UU… has siblings UULF… (long videos only) and UUSH… (Shorts only)
    long_, shorts = fetch_feed("UULF" + cid[2:]), fetch_feed("UUSH" + cid[2:])
    return {
        "handle": handle, "id": cid,
        "title": html.unescape(title.group(1)) if title else handle,
        "avatar": avatar.group(1) if avatar else "",
        "subsText": subs_m.group(0) if subs_m else "?",
        "subs": parse_subs(subs_m.group(0) if subs_m else ""),
        "videoCount": vids_m.group(1) if vids_m else "?",
        "long": long_, "shorts": shorts, "videos": long_ + shorts,
        "longViews": sum(v["views"] for v in long_),
        "shortsViews": sum(v["views"] for v in shorts),
    }


def fetch_feed(playlist_id):
    try:
        feed = ET.fromstring(get(f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"))
    except urllib.error.HTTPError as ex:
        if ex.code == 404:  # channel has no videos of this kind yet
            return []
        raise
    videos = []
    for e in feed.findall("a:entry", NS):  # RSS holds the latest 15 only
        vid = e.findtext("yt:videoId", namespaces=NS)
        stats = e.find("m:group/m:community/m:statistics", NS)
        videos.append({
            "id": vid,
            "title": e.findtext("a:title", namespaces=NS),
            "published": e.findtext("a:published", namespaces=NS),
            "views": int(stats.get("views")) if stats is not None else 0,
            "url": e.find("a:link", NS).get("href"),
            "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        })
    return videos


def safe_fetch(handle):
    try:
        return fetch_channel(handle)
    except Exception as ex:  # one broken channel must not blank the whole dashboard
        return {"handle": handle, "error": str(ex)}


def add_history(data):
    hist = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    now = time.time()
    if not hist or now - hist[-1]["t"] > 3600:
        hist.append({"t": now, "ch": {c["handle"]: {"subs": c.get("subs"), "views": {v["id"]: v["views"] for v in c.get("videos", [])}} for c in data if "error" not in c}})
        HISTORY.write_text(json.dumps(hist[-24 * 60:]))  # keep ~60 days
    # baseline = newest snapshot at least 20h old, else the oldest one we have
    base = next((h for h in reversed(hist) if now - h["t"] >= 20 * 3600), hist[0])
    for c in data:
        b = base["ch"].get(c["handle"])
        if "error" in c or not b:
            continue
        c["since"] = base["t"]
        c["subsDelta"] = (c["subs"] - b["subs"]) if c["subs"] is not None and b["subs"] is not None else None
        for v in c["videos"]:
            v["delta"] = v["views"] - b["views"][v["id"]] if v["id"] in b["views"] else None


def load(force=False):
    if force or not _cache["data"] or time.time() - _cache["t"] > CACHE_SECONDS:
        with ThreadPoolExecutor(len(CHANNELS)) as ex:
            data = list(ex.map(safe_fetch, CHANNELS))
        add_history(data)
        _cache.update(t=time.time(), data=data)
    return {"fetchedAt": _cache["t"], "channels": _cache["data"]}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(HERE), **k)

    def do_GET(self):
        if self.path.startswith("/api/data"):
            body = json.dumps(load(force="refresh=1" in self.path)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


if __name__ == "__main__":
    import sys
    assert parse_subs("19 subscribers") == 19 and parse_subs("1.2K subscribers") == 1200 and parse_subs("1 subscriber") == 1 and parse_subs("") is None
    if "--build" in sys.argv:  # GitHub Actions: write data.json for the static GitHub Pages copy
        data = load(force=True)
        assert any("error" not in c for c in data["channels"]), "all channels failed"
        (HERE / "data.json").write_text(json.dumps(data))
        sys.exit()
    print("YT Monitor → http://localhost:8765  (HP, Wi-Fi sama: http://<IP-Mac>:8765)")
    HTTPServer(("0.0.0.0", 8765), Handler).serve_forever()
