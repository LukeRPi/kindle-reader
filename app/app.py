import os
import time
import threading
import hashlib
import re
import json
from urllib.parse import urlparse

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import yaml
from flask import Flask, render_template, request, abort, redirect, url_for
from readability import Document

FEEDS_FILE      = os.environ.get("FEEDS_FILE", "/config/feeds.yaml")
CACHE           = {}
CACHE_LOCK      = threading.Lock()
CACHE_TTL       = int(os.environ.get("CACHE_TTL", 1800))
MAX_ITEMS       = int(os.environ.get("MAX_ITEMS", 25))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", 15))

app = Flask(__name__)

# ── HTTP session with retry and realistic browser headers ──────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def make_session():
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s


@app.context_processor
def inject_globals():
    return {"cache_ttl": CACHE_TTL}


# ── helpers ────────────────────────────────────────────────────────────────────

def load_config():
    with open(FEEDS_FILE) as f:
        return yaml.safe_load(f)


def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def truncate(text, n=220):
    text = strip_html(text)
    return text[:n] + "…" if len(text) > n else text


def reading_time(text):
    """Estimate reading time in minutes.
    Returns None if the text is too short to be reliable
    (likely just an RSS summary rather than the full article)."""
    words = len(strip_html(text).split())
    if words < 120:   # threshold: fewer than ~30 seconds worth of reading
        return None
    return max(1, round(words / 200))


def is_reddit_url(url):
    host = urlparse(url).netloc.lower()
    return "reddit.com" in host


def _parse_feed_url(url):
    """Fetch and parse a single RSS URL. Returns (items, error). Results are cached."""
    now = time.time()
    with CACHE_LOCK:
        entry = CACHE.get(url)
        if entry and now - entry["ts"] < CACHE_TTL:
            return entry["data"], None
    try:
        s = make_session()
        raw = s.get(url, timeout=REQUEST_TIMEOUT)
        raw.raise_for_status()
        parsed = feedparser.parse(raw.content)
        feed_title = parsed.feed.get("title", "")
        items = []
        for e in parsed.entries[:MAX_ITEMS]:
            # Full text from RSS (used as fallback if the page fetch fails)
            content_html = (
                e.get("content", [{}])[0].get("value", "")
                or e.get("summary", "")
            )
            summary = truncate(content_html)
            pub_struct = e.get("published_parsed") or e.get("updated_parsed")
            pub_ts = time.mktime(pub_struct) if pub_struct else 0
            art_id = hashlib.md5(e.get("link", "").encode()).hexdigest()[:8]
            items.append({
                "title":        e.get("title", "–"),
                "link":         e.get("link", "#"),
                "summary":      summary,
                "content_html": content_html,   # raw RSS text, used as fallback
                "published":    e.get("published", ""),
                "pub_ts":       pub_ts,
                "source":       feed_title,
                "read_time":    reading_time(content_html),
                "id":           art_id,
            })
        with CACHE_LOCK:
            CACHE[url] = {"ts": time.time(), "data": items}
        return items, None
    except Exception as exc:
        return [], str(exc)


def fetch_feed(url):
    return _parse_feed_url(url)


def fetch_multi_feed(urls):
    """Fetch multiple feed URLs in parallel, merge and sort by date descending."""
    results = []
    errors  = []
    lock    = threading.Lock()

    def _worker(u):
        items, err = _parse_feed_url(u)
        with lock:
            if err:
                errors.append(err)
            results.extend(items)

    threads = [threading.Thread(target=_worker, args=(u,)) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=REQUEST_TIMEOUT + 5)

    results.sort(key=lambda x: x["pub_ts"], reverse=True)
    error = "; ".join(errors) if errors else None
    return results, error


def fetch_article(url):
    """Fetch strategy:
    1. Reddit URL → use Reddit JSON API (post body + comments)
    2. Everything else → readability-based generic fetch
    """
    if is_reddit_url(url):
        return _fetch_reddit(url)
    return _fetch_generic(url)


def _md_to_html(text):
    """Minimal markdown → HTML conversion for Kindle rendering."""
    if not text:
        return ""
    # Basic HTML escaping
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Headings
    text = re.sub(r"^### (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$",  r"<h2>\1</h2>", text, flags=re.MULTILINE)
    text = re.sub(r"^# (.+)$",   r"<h2>\1</h2>", text, flags=re.MULTILINE)
    # Bold and italic
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*",     r"<em>\1</em>", text)
    # Markdown links [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    # Bare URLs
    text = re.sub(r"(?<![\"'])https?://\S+", r'<a href="\g<0>">\g<0></a>', text)
    # Paragraphs (double newline)
    parts = text.split("\n\n")
    html  = "".join(
        p if p.startswith("<h") else f"<p>{p.replace(chr(10), '<br>')}</p>"
        for p in parts if p.strip()
    )
    return html


def _render_comments(children, depth=0, max_depth=4, max_top=15, _count=None):
    """Recursively render Reddit comments to HTML."""
    if _count is None:
        _count = [0]
    if depth > max_depth:
        return ""
    html = ""
    for child in children:
        if child.get("kind") != "t1":
            continue
        if depth == 0 and _count[0] >= max_top:
            break
        d    = child.get("data", {})
        body = d.get("body", "")
        if body in ("[removed]", "[deleted]", ""):
            continue
        author    = d.get("author", "?")
        score     = d.get("score", 0)
        indent    = depth * 16
        body_html = _md_to_html(body)
        html += (
            f'<div class="reddit-comment" style="margin-left:{indent}px">'
            f'<div class="reddit-comment-meta">{author} &mdash; {score} points</div>'
            f'<div class="reddit-comment-body">{body_html}</div>'
            f'</div>'
        )
        if depth == 0:
            _count[0] += 1
        # Nested replies
        replies = d.get("replies", "")
        if isinstance(replies, dict):
            sub   = replies.get("data", {}).get("children", [])
            html += _render_comments(sub, depth + 1, max_depth, max_top, _count)
    return html


def _fetch_reddit(url):
    """Fetch a Reddit post via the JSON API: post body + top comments."""
    clean    = url.rstrip("/").split("?")[0]
    json_url = clean + ".json?limit=50&sort=top"
    s        = make_session()
    s.headers.update({"User-Agent": "KindleReader/1.0 (personal RSS reader)"})
    resp     = s.get(json_url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data     = resp.json()
    try:
        post         = data[0]["data"]["children"][0]["data"]
        title        = post.get("title", "Reddit post")
        score        = post.get("score", 0)
        num_comments = post.get("num_comments", 0)
        subreddit    = post.get("subreddit_name_prefixed", "")
        selftext     = post.get("selftext", "")

        # Post header
        header = (
            f'<p class="reddit-post-meta">{subreddit} &mdash; '
            f'{score} points &mdash; {num_comments} comments</p>'
        )

        # Post body
        if selftext and selftext not in ("[removed]", "[deleted]"):
            post_body = _md_to_html(selftext)
        else:
            # Link post: show destination URL
            dest      = post.get("url", "")
            post_body = (
                f'<p>This is a link post:</p>'
                f'<p><a href="{dest}">{dest}</a></p>'
            )

        # Comments section
        comment_children = data[1]["data"]["children"]
        comments_html    = _render_comments(comment_children)
        if comments_html:
            comments_section = (
                '<hr style="border:2px solid #000; margin:18px 0">'
                '<div class="section-title" style="margin-bottom:12px">Comments</div>'
                + comments_html
            )
        else:
            comments_section = ""

        content = header + post_body + comments_section
        return title, content

    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"Could not parse Reddit response: {e}")


def _fetch_generic(url):
    """Generic article fetch using readability."""
    s = make_session()
    # Polite warm-up request to the domain root to pick up session cookies
    try:
        parsed = urlparse(url)
        home   = f"{parsed.scheme}://{parsed.netloc}/"
        s.get(home, timeout=8)
    except Exception:
        pass
    # Fetch the actual article
    resp = s.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    doc  = Document(resp.text)
    return doc.title(), doc.summary()


# ── routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    config = load_config()
    return render_template("index.html", categories=config.get("categories", []))


@app.route("/category/<int:cat_idx>")
def category(cat_idx):
    config = load_config()
    cats   = config.get("categories", [])
    if cat_idx >= len(cats):
        abort(404)
    cat = cats[cat_idx]
    return render_template("category.html", cat=cat, cat_idx=cat_idx)


@app.route("/feed/<int:cat_idx>/<int:feed_idx>")
def feed(cat_idx, feed_idx):
    config = load_config()
    try:
        feed_info = config["categories"][cat_idx]["feeds"][feed_idx]
    except (IndexError, KeyError):
        abort(404)

    page     = max(1, request.args.get("page", 1, type=int))
    per_page = 10

    urls = feed_info.get("urls") or ([feed_info["url"]] if feed_info.get("url") else [])
    if len(urls) > 1:
        items, error = fetch_multi_feed(urls)
    else:
        items, error = fetch_feed(urls[0]) if urls else ([], "No URL configured")

    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page        = min(page, total_pages)
    paged       = items[(page - 1) * per_page: page * per_page]
    multi       = len(urls) > 1

    return render_template(
        "feed.html",
        feed=feed_info,
        items=paged,
        error=error,
        cat_idx=cat_idx,
        feed_idx=feed_idx,
        page=page,
        total_pages=total_pages,
        multi=multi,
    )


@app.route("/article")
def article():
    url      = request.args.get("url", "").strip()
    back     = request.args.get("back", "/")
    fallback = request.args.get("fallback", "").strip()  # RSS summary passed as fallback
    if not url:
        abort(400)

    error   = None
    content = ""
    title   = ""

    try:
        title, content = fetch_article(url)
    except Exception as exc:
        error = str(exc)
        # Use the RSS summary as fallback if available
        if fallback:
            title   = "Feed preview"
            content = f"<p>{fallback}</p>"

    return render_template(
        "article.html",
        title=title,
        content=content,
        url=url,
        back=back,
        error=error,
        used_fallback=(bool(error) and bool(fallback)),
        read_time=reading_time(content),
    )


@app.route("/refresh")
def refresh():
    """Clear the entire feed cache."""
    with CACHE_LOCK:
        CACHE.clear()
    return redirect(url_for("index"))


# ── saved articles ─────────────────────────────────────────────────────────────

DATA_FILE  = os.environ.get("DATA_FILE", "/data/saved.json")
API_KEY    = os.environ.get("API_KEY", "")
SAVED_LOCK = threading.Lock()


def _load_saved():
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_saved(articles):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)


def _require_api_key():
    key = request.headers.get("X-API-Key") or request.args.get("api_key", "")
    if not API_KEY:
        abort(503, "API_KEY is not configured on the server")
    if key != API_KEY:
        abort(401, "Invalid API key")


# ── API endpoint ───────────────────────────────────────────────────────────────

@app.route("/api/save", methods=["POST"])
def api_save():
    """Receive a {url} payload, fetch the full article and save it."""
    _require_api_key()
    data = request.get_json(force=True, silent=True) or {}
    url  = (data.get("url") or "").strip()
    if not url:
        return json_response({"error": "missing url"}, 400)

    # Fetch article
    try:
        title, content = fetch_article(url)
    except Exception as exc:
        title   = url
        content = f"<p>Could not load content: {exc}</p>"

    article_id = hashlib.md5(f"{url}{time.time()}".encode()).hexdigest()[:12]
    entry = {
        "id":        article_id,
        "url":       url,
        "title":     title or url,
        "content":   content,
        "saved_at":  time.strftime("%Y-%m-%d %H:%M"),
        "read_time": reading_time(content),
    }

    with SAVED_LOCK:
        articles = _load_saved()
        articles.insert(0, entry)   # newest first
        articles = articles[:200]   # hard cap
        _write_saved(articles)

    return json_response({"ok": True, "id": article_id, "title": entry["title"]})


# ── Kindle UI for saved articles ───────────────────────────────────────────────

@app.route("/saved")
def saved_list():
    with SAVED_LOCK:
        articles = _load_saved()
    page        = max(1, request.args.get("page", 1, type=int))
    per_page    = 10
    total       = len(articles)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page        = min(page, total_pages)
    paged       = articles[(page - 1) * per_page: page * per_page]
    return render_template("saved_list.html",
                           articles=paged, page=page,
                           total_pages=total_pages, total=total)


@app.route("/saved/<article_id>")
def saved_read(article_id):
    with SAVED_LOCK:
        articles = _load_saved()
    entry = next((a for a in articles if a["id"] == article_id), None)
    if not entry:
        abort(404)
    return render_template("saved_article.html", entry=entry,
                           read_time=reading_time(entry.get("content", "")))


@app.route("/saved/delete/<article_id>")
def saved_delete(article_id):
    with SAVED_LOCK:
        articles = _load_saved()
        articles = [a for a in articles if a["id"] != article_id]
        _write_saved(articles)
    return redirect(url_for("saved_list"))


@app.route("/save-article")
def save_article_direct():
    """Save an article directly from the Kindle browser (no API key required)."""
    url   = request.args.get("url", "").strip()
    title = request.args.get("title", "").strip()
    back  = request.args.get("back", "/")
    if not url:
        abort(400)

    # Try full fetch; fall back to the title passed as a query param
    try:
        title_fetched, content = fetch_article(url)
        title = title_fetched or title or url
    except Exception as exc:
        content = f"<p>Could not load content: {exc}</p>"
        title   = title or url

    article_id = hashlib.md5(f"{url}{time.time()}".encode()).hexdigest()[:12]
    entry = {
        "id":        article_id,
        "url":       url,
        "title":     title,
        "content":   content,
        "saved_at":  time.strftime("%Y-%m-%d %H:%M"),
        "read_time": reading_time(content),
    }

    with SAVED_LOCK:
        articles = _load_saved()
        # Skip duplicates (same URL)
        if any(a["url"] == url for a in articles):
            return redirect(url_for("saved_list"))
        articles.insert(0, entry)
        articles = articles[:200]
        _write_saved(articles)

    return redirect(url_for("saved_list"))


# ── JSON helper ────────────────────────────────────────────────────────────────

def json_response(data, status=200):
    from flask import Response
    return Response(json.dumps(data), status=status,
                    mimetype="application/json")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
