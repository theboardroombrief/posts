"""Publish due Instagram posts from this repository.

Each post is a top-level folder containing JPEG slides and a post.json:
    {
      "caption": "...",
      "publish_at": "2026-11-18T08:30:00+01:00",   # no offset = Europe/Paris
      "approved": true,
      "slides": ["slide-01.jpg", ...]               # optional, default: sorted *.jpg
    }
A folder with a published.json is never published again.

Environment: INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_ACCOUNT_ID, DRY_RUN=true|false.
Usage: python3 scripts/publish.py --root . --repo owner/name --sha <commit>
"""
import argparse
import datetime as dt
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://graph.facebook.com/v23.0"
DEFAULT_TZ = ZoneInfo("Europe/Paris")
MAX_POSTS_PER_RUN = 1
CTX = ssl.create_default_context()
if sys.platform == "darwin" and Path("/etc/ssl/cert.pem").exists():
    CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")

TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
IG_ID = os.environ.get("INSTAGRAM_ACCOUNT_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


class PostError(Exception):
    pass


def log(msg):
    print(msg.replace(TOKEN, "<TOKEN>") if TOKEN else msg, flush=True)


def graph(method, path, **params):
    url, data = f"{API}/{path}", None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise PostError(f"API {method} {path}: {e.read().decode().replace(TOKEN, '<TOKEN>')}")
    except (urllib.error.URLError, ValueError, TimeoutError, OSError) as e:
        raise PostError(f"API {method} {path}: {type(e).__name__} (requête non envoyée ou sans réponse)")


def parse_when(value):
    when = dt.datetime.fromisoformat(value)
    return when if when.tzinfo else when.replace(tzinfo=DEFAULT_TZ)


def load_post(folder):
    meta = json.loads((folder / "post.json").read_text(encoding="utf-8"))
    caption = (meta.get("caption") or "").strip()
    if not caption:
        raise PostError("légende vide")
    if len(caption) > 2200:
        raise PostError(f"légende trop longue ({len(caption)} > 2200)")
    if caption.count("#") > 30:
        raise PostError("plus de 30 hashtags")
    slides = meta.get("slides") or sorted(p.name for p in folder.glob("*.jpg"))
    if not 1 <= len(slides) <= 10:
        raise PostError(f"{len(slides)} images (1 à 10 attendues)")
    for s in slides:
        if not (folder / s).is_file():
            raise PostError(f"image manquante: {s}")
        if not s.lower().endswith((".jpg", ".jpeg")):
            raise PostError(f"pas un JPEG: {s}")
    if "publish_at" not in meta:
        raise PostError("publish_at manquant")
    return {"caption": caption, "slides": slides, "approved": meta.get("approved") is True,
            "publish_at": parse_when(meta["publish_at"])}


def check_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": "instagram-publisher"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            ctype = r.headers.get("Content-Type", "")
            r.read()
    except urllib.error.HTTPError as e:
        raise PostError(f"URL inaccessible ({e.code}): {url}")
    if ctype != "image/jpeg":
        raise PostError(f"type {ctype!r} au lieu de image/jpeg: {url}")


def already_on_instagram(caption):
    recent = graph("GET", f"{IG_ID}/media", fields="id,caption,permalink,timestamp", limit=25)
    for m in recent.get("data", []):
        if (m.get("caption") or "").strip() == caption:
            return m
    return None


def wait_ready(container_id, label):
    for _ in range(60):
        status = graph("GET", container_id, fields="status_code,status")
        code = status.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise PostError(f"{label}: {status}")
        time.sleep(3)
    raise PostError(f"{label}: pas prêt après 3 minutes")


def publish(folder, post, repo, sha):
    base = f"https://raw.githubusercontent.com/{repo}/{sha}/{urllib.parse.quote(folder.name)}/"
    urls = [base + urllib.parse.quote(s) for s in post["slides"]]
    for u in urls:
        check_url(u)
    log(f"  {len(urls)} URL vérifiées")

    existing = already_on_instagram(post["caption"])
    if existing:
        log(f"  déjà publié sur Instagram ({existing['permalink']}), enregistrement seulement")
        record(folder, existing["id"], existing["permalink"], existing["timestamp"], note="found existing")
        return

    if DRY_RUN:
        log("  SIMULATION: rien n'est envoyé à Instagram")
        for i, u in enumerate(urls, 1):
            log(f"    {i}. {u}")
        log(f"    légende: {post['caption']}")
        return

    if len(urls) == 1:
        creation_id = graph("POST", f"{IG_ID}/media", image_url=urls[0], caption=post["caption"])["id"]
        wait_ready(creation_id, "image")
    else:
        children = []
        for i, u in enumerate(urls, 1):
            cid = graph("POST", f"{IG_ID}/media", image_url=u, is_carousel_item="true")["id"]
            wait_ready(cid, f"slide {i}")
            children.append(cid)
        creation_id = graph("POST", f"{IG_ID}/media", media_type="CAROUSEL",
                            children=",".join(children), caption=post["caption"])["id"]
        wait_ready(creation_id, "carrousel")
    log("  conteneurs prêts, publication…")

    media_id = graph("POST", f"{IG_ID}/media_publish", creation_id=creation_id)["id"]
    info = graph("GET", media_id, fields="permalink,timestamp")
    record(folder, media_id, info.get("permalink"), info.get("timestamp"))
    log(f"  PUBLIÉ: {info.get('permalink')}")


def record(folder, media_id, permalink, timestamp, note=None):
    data = {"media_id": media_id, "permalink": permalink, "timestamp": timestamp}
    if note:
        data["note"] = note
    (folder / "published.json").write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--sha", required=True)
    args = ap.parse_args()
    if not TOKEN or not IG_ID:
        sys.exit("INSTAGRAM_ACCESS_TOKEN ou INSTAGRAM_ACCOUNT_ID manquant")
    if any(c.isspace() for c in TOKEN) or not IG_ID.isdigit():
        sys.exit("Secret mal collé: le token contient un espace/retour à la ligne ou l'ID n'est pas numérique")

    now = dt.datetime.now(dt.timezone.utc)
    log(f"{now.astimezone(DEFAULT_TZ):%Y-%m-%d %H:%M} (Paris){' — SIMULATION' if DRY_RUN else ''}")
    try:
        account = graph("GET", IG_ID, fields="username")
    except PostError as e:
        sys.exit(f"Token ou compte Instagram invalide: {e}")
    log(f"Compte: @{account.get('username')}")
    failures, done = 0, 0
    for folder in sorted(p for p in Path(args.root).iterdir() if (p / "post.json").is_file()):
        if (folder / "published.json").exists():
            continue
        try:
            post = load_post(folder)
        except (PostError, ValueError, json.JSONDecodeError) as e:
            log(f"[{folder.name}] INVALIDE: {e}")
            failures += 1
            continue
        when = post["publish_at"].astimezone(DEFAULT_TZ)
        if not post["approved"]:
            log(f"[{folder.name}] non approuvé, ignoré")
            continue
        if post["publish_at"] > now:
            log(f"[{folder.name}] prévu le {when:%Y-%m-%d %H:%M}, en attente")
            continue
        if done >= MAX_POSTS_PER_RUN:
            log(f"[{folder.name}] dû, reporté au prochain passage")
            continue
        log(f"[{folder.name}] dû depuis le {when:%Y-%m-%d %H:%M}")
        try:
            publish(folder, post, args.repo, args.sha)
            done += 1
        except PostError as e:
            log(f"[{folder.name}] ÉCHEC: {e}")
            failures += 1
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
