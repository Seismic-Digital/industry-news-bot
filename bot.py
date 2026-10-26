"""Industry news bot: RSS/scraped sources -> Claude triage + summary -> Slack.

Usage:
  python bot.py              # normal run (what GitHub Actions calls)
  python bot.py --dry-run    # print Slack messages instead of posting; state is not saved
  python bot.py --dry-run --test-limit 2   # preview: 2 newest items per source, no seeding
  python bot.py --check-feeds
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import anthropic
import feedparser
import requests
import trafilatura
import yaml

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "sources.yaml"
STATE_PATH = ROOT / "state" / "state.json"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
FALLBACK_USER_AGENT = "Mozilla/5.0 (compatible; IndustryNewsBot/1.0)"
TRIAGE_BATCH = 40
MAX_ARTICLE_CHARS = 20000  # plenty for a summary; keeps cost predictable on very long posts
SEEN_RETENTION_DAYS = 45
POSTED_RETENTION_HOURS = 72


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- state ----------

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def normalize_state(state):
    state.setdefault("seen", {})            # url_id -> iso timestamp
    state.setdefault("seeded_sources", [])  # source names whose backlog was marked seen
    state.setdefault("posted", [])          # recent posts, for duplicate threading
    state.setdefault("daily_counts", {})    # "YYYY-MM-DD" -> {channel: n}
    state.setdefault("digest_queue", {})    # channel -> [items]
    state.setdefault("digest_sent", {})     # channel -> "YYYY-MM-DD"
    return state


def prune_state(state, now):
    seen_cutoff = (now - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= seen_cutoff}
    posted_cutoff = (now - timedelta(hours=POSTED_RETENTION_HOURS)).isoformat()
    state["posted"] = [p for p in state["posted"] if p["at"] >= posted_cutoff]
    queue_cutoff = (now - timedelta(hours=36)).isoformat()
    for ch, items in state["digest_queue"].items():
        state["digest_queue"][ch] = [i for i in items if i["at"] >= queue_cutoff]
    week_ago = (now - timedelta(days=7)).date().isoformat()
    state["daily_counts"] = {d: c for d, c in state["daily_counts"].items() if d >= week_ago}


def save_state(state):
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True))


# ---------- fetching ----------

def clean_url(url):
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, urlencode(query), ""))


def url_id(url):
    return hashlib.sha1(clean_url(url).encode()).hexdigest()[:16]


def http_get(url):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    if 400 <= resp.status_code < 500:  # some sites (e.g. Facebook) reject browser UAs from scripts
        resp = requests.get(url, headers={"User-Agent": FALLBACK_USER_AGENT}, timeout=20)
    resp.raise_for_status()
    return resp


def strip_html(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def fetch_source(source):
    """Return a list of item dicts for one source."""
    if source.get("type") == "scrape":
        html = http_get(source["url"]).text
        links = dict.fromkeys(re.findall(source["link_pattern"], html))  # dedupe, keep order
        items = []
        for path in links:
            url = urljoin(source["url"], path)
            slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
            items.append({"url": url, "title": slug.replace("-", " ").capitalize(), "slug_title": True,
                          "snippet": "", "author": "", "published": None})
        return items

    try:
        feed = feedparser.parse(http_get(source["url"]).content)
    except requests.RequestException:
        if not source.get("fallback_url"):
            raise
        feed = feedparser.parse(http_get(source["fallback_url"]).content)
    items = []
    for e in feed.entries:
        if not e.get("link"):
            continue
        title = re.sub(r"\s+via @\S+(,\s*@\S+)*$", "", strip_html(e.get("title", "")))  # SEJ appends "via @handles"
        suffix = source.get("strip_title_suffix")
        if suffix and title.endswith(suffix):
            title = title[: -len(suffix)]
        published = e.get("published_parsed") or e.get("updated_parsed")
        items.append({
            "url": e.link,
            "title": title,
            "snippet": strip_html(e.get("summary", ""))[:600],
            "author": strip_html(e.get("author", "")),
            "published": datetime(*published[:6], tzinfo=timezone.utc).isoformat() if published else None,
            "image": feed_image(e),
        })
    return items


def feed_image(entry):
    """Image URL from the feed entry's media tags or enclosures, if it has one."""
    for media in entry.get("media_content", []) + entry.get("media_thumbnail", []):
        if media.get("url") and media.get("medium", "image") == "image":
            return media["url"]
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image/") and enc.get("href"):
            return enc["href"]
    return None


def fetch_article(url):
    """Full text + metadata via trafilatura. Returns {} if the page can't be read."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return {}
        data = trafilatura.extract(downloaded, output_format="json", with_metadata=True, url=url)
        return json.loads(data) if data else {}
    except Exception as exc:  # noqa: BLE001 - any parse failure just means "use the feed snippet"
        log(f"  article fetch failed for {url}: {exc}")
        return {}


# ---------- Claude ----------

TRIAGE_SYSTEM = """You triage industry news for a digital marketing agency's Slack channels. The team runs SEO, paid media, and AI-search work for clients, and wants to stay current without noise.

Channels:
{channels}

For each new article, decide:
- channel: the single best channel from that article's allowed_channels, or "none" if it fits none.
- score (1-5), relevance to the agency team:
  5 = must-know today: confirmed Google core/spam update, major Google Ads or Meta platform/policy change, flagship model release (e.g. a new GPT/Claude/Gemini generation), big AI Overviews/AI Mode change.
  4 = clearly useful: new feature or product change practitioners will use, solid original data/research, notable documentation change.
  3 = worth a skim: helpful analysis or commentary with something new in it.
  2 = low value: generic how-to, listicle, evergreen beginner content, rehash.
  1 = irrelevant: off-topic, event promos, hiring/funding fluff, award lists, webinars, company PR with no product news.
  Primary-tier sources (the platform itself) announcing real changes deserve a higher score than press re-reporting them. General-tier tech news should only score 4+ if it directly matters to search or ads.
- duplicate_of: if the article covers the same news as one of the recently posted stories (or another article earlier in this batch), that story's id; otherwise "".
- one_liner: one plain sentence (max 25 words) saying what happened. No hype."""

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "channel": {"type": "string"},
                    "score": {"type": "integer"},
                    "duplicate_of": {"type": "string"},
                    "one_liner": {"type": "string"},
                },
                "required": ["id", "channel", "score", "duplicate_of", "one_liner"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

SUMMARY_SYSTEM = """You write Slack summaries of industry news for a digital marketing agency's {channel_label} channel. Readers are SEO, paid media, and content strategists.

Write in plain, direct language. No hype, no filler phrases like "in today's fast-paced landscape". Stick to what the article actually says; don't invent numbers or details. If the article text is missing or thin, summarize only what the title and snippet support."""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-3 sentences on what happened and the key details."},
        "why_it_matters": {"type": "string", "description": "One sentence on what this means for client campaigns or strategy."},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "2-4 short topic tags, e.g. Google Ads, Core Update, AI Mode."},
    },
    "required": ["summary", "why_it_matters", "tags"],
    "additionalProperties": False,
}


def ask_claude(client, model, system, user, schema, max_tokens):
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
    )
    if response.stop_reason == "refusal":
        return None
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude response hit max_tokens")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def triage(client, config, items, recent_posts):
    channels = "\n".join(f"- {key}: {c['focus']}" for key, c in config["channels"].items())
    system = TRIAGE_SYSTEM.format(channels=channels)
    recent = [{"id": p["id"], "channel": p["channel"], "title": p["title"]} for p in recent_posts]
    results = {}
    for start in range(0, len(items), TRIAGE_BATCH):
        batch = items[start:start + TRIAGE_BATCH]
        payload = {
            "recently_posted": recent,
            "new_articles": [{
                "id": i["id"], "source": i["source"], "tier": i["tier"],
                "allowed_channels": i["channels"], "title": i["title"], "snippet": i["snippet"],
            } for i in batch],
        }
        out = ask_claude(client, config["settings"]["model"], system,
                         json.dumps(payload, ensure_ascii=False), TRIAGE_SCHEMA, 16000)
        for r in (out or {}).get("results", []):
            results[r["id"]] = r
    return results


def summarize(client, config, item, article):
    label = item["channel"].replace("_", " ").title()
    text = (article.get("text") or "")[:MAX_ARTICLE_CHARS]
    user = (f"Source: {item['source']}\nTitle: {item['title']}\nFeed snippet: {item['snippet']}\n\n"
            f"Article text:\n{text or '(not available)'}")
    return ask_claude(client, config["settings"]["model"], SUMMARY_SYSTEM.format(channel_label=label),
                      user, SUMMARY_SCHEMA, 2000)


# ---------- Slack ----------

def esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_date(iso, tz):
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(tz).strftime("%b %-d, %Y")


def article_blocks(item, summary, tz, breaking):
    icon = "🚨" if breaking else "📰"
    meta = [item["source"]]
    if item.get("author"):
        meta.append(item["author"])
    if item.get("published"):
        meta.append(format_date(item["published"], tz))
    if item.get("read_minutes"):
        meta.append(f"{item['read_minutes']} min read")
    tags = " · ".join(summary["tags"])
    blocks = []
    if item.get("image"):
        blocks.append({"type": "image", "image_url": item["image"], "alt_text": item["title"][:2000]})
    blocks += [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{icon} *<{item['url']}|{esc(item['title'])}>*"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": esc(" · ".join(meta))}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": esc(summary["summary"])}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Why it matters:* {esc(summary['why_it_matters'])}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
                                          "text": f"🏷️ {esc(tags)}    Relevance: {item['score']}/5"}]},
        {"type": "divider"},
    ]
    return blocks


def digest_blocks(items, tz, now):
    lines = [f"• *<{i['url']}|{esc(i['title'])}>* ({esc(i['source'])}): {esc(i['one_liner'])}" for i in items]
    return [
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": f"🗂️ *Also worth a look: {now.astimezone(tz).strftime('%a %b %-d')}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]


class Slack:
    def __init__(self, token, dry_run):
        self.token = token
        self.dry_run = dry_run

    def post(self, channel, text, blocks=None, thread_ts=None):
        if self.dry_run:
            print(f"\n----- would post to {channel}{' (thread)' if thread_ts else ''} -----")
            for b in blocks or []:
                if "text" in b:
                    print(b["text"]["text"])
                elif b["type"] == "image":
                    print(f"[image: {b['image_url']}]")
                elif b["type"] == "divider":
                    print("─" * 40)
                for el in b.get("elements", []):
                    print(el["text"])
            if not blocks:
                print(text)
            return f"dry-{time.time()}"
        payload = {"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        resp = requests.post("https://slack.com/api/chat.postMessage", json=payload, timeout=20,
                             headers={"Authorization": f"Bearer {self.token}"})
        data = resp.json()
        if data.get("error") == "invalid_blocks" and any(b["type"] == "image" for b in payload.get("blocks", [])):
            # Slack couldn't download the image (blocked, too big, bad URL); post without it
            payload["blocks"] = [b for b in payload["blocks"] if b["type"] != "image"]
            resp = requests.post("https://slack.com/api/chat.postMessage", json=payload, timeout=20,
                                 headers={"Authorization": f"Bearer {self.token}"})
            data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack error posting to {channel}: {data.get('error')}")
        return data["ts"]


# ---------- main flow ----------

def collect_new_items(config, state, now, test_limit=None):
    max_age = now - timedelta(hours=config["settings"]["max_item_age_hours"])
    new_items, seen_this_run = [], set()
    for source in config["sources"]:
        try:
            items = fetch_source(source)
        except Exception as exc:  # noqa: BLE001 - one broken feed shouldn't stop the run
            log(f"FEED ERROR {source['name']}: {exc}")
            continue
        first_time = source["name"] not in state["seeded_sources"] and not test_limit
        if test_limit:
            items = items[:test_limit]
        for item in items:
            item["id"] = url_id(item["url"])
            if item["id"] in state["seen"]:
                state["seen"][item["id"]] = now.isoformat()  # still in the feed; keep it from aging out
                continue
            if item["id"] in seen_this_run:
                continue
            seen_this_run.add(item["id"])
            if first_time:
                state["seen"][item["id"]] = now.isoformat()
                continue
            if item["published"] and datetime.fromisoformat(item["published"]) < max_age and not test_limit:
                state["seen"][item["id"]] = now.isoformat()
                continue
            item.update(source=source["name"], tier=source.get("tier", "press"), channels=source["channels"])
            new_items.append(item)
        if first_time:
            state["seeded_sources"].append(source["name"])
            log(f"Seeded {source['name']}: {len(items)} existing items marked seen")
    return new_items


def enrich_item(item, article):
    """Fill in title, author, date, image, and reading time from the full article page."""
    if item.get("slug_title") and article.get("title"):
        item["title"] = article["title"]
    item["author"] = item["author"] or article.get("author") or ""
    item["published"] = item["published"] or article.get("date")
    image = article.get("image") or item.get("image")
    item["image"] = image if image and image.startswith("https://") else None
    words = len((article.get("text") or "").split())
    item["read_minutes"] = max(1, round(words / 230)) if words > 150 else None


def post_sample(config, client, slack, source_name):
    """Post the newest article from one source to the sample channel, to preview formatting. State is untouched."""
    source = next(src for src in config["sources"] if src["name"] == source_name)
    item = fetch_source(source)[0]
    channel = source["channels"][0]
    item.update(source=source["name"], channel=channel, score=4)
    article = fetch_article(item["url"])
    enrich_item(item, article)
    summary = summarize(client, config, item, article)
    tz = ZoneInfo(config["settings"]["timezone"])
    slack.post(config["settings"]["sample_channel"], item["title"], article_blocks(item, summary, tz, False))
    log(f"Posted sample [{channel}] {item['title']}")


def run(config, state, client, slack, now, test_limit=None):
    s = config["settings"]
    tz = ZoneInfo(s["timezone"])
    today = now.astimezone(tz).date().isoformat()
    counts = state["daily_counts"].setdefault(today, {})
    channel_ids = {k: c["slack_channel"] for k, c in config["channels"].items()}

    items = collect_new_items(config, state, now, test_limit)
    log(f"{len(items)} new items to triage")

    if items:
        verdicts = triage(client, config, items, state["posted"])
        posted_by_id = {p["id"]: p for p in state["posted"]}
        broken_channels = set()  # Slack rejected these this run; their items stay unseen and retry next run
        # originals first, so duplicates in the same batch can thread under them
        items.sort(key=lambda i: bool(verdicts.get(i["id"], {}).get("duplicate_of")))

        for item in items:
            v = verdicts.get(item["id"])
            if v is None:
                continue  # Claude skipped it; leave unseen so the next run retries
            state["seen"][item["id"]] = now.isoformat()
            channel, score = v["channel"], max(1, min(5, v["score"]))
            if channel not in channel_ids or channel not in item["channels"] or score < s["min_score"]:
                continue
            item.update(channel=channel, score=score, one_liner=v["one_liner"])

            original = posted_by_id.get(v["duplicate_of"])
            if original:
                slack.post(channel_ids[original["channel"]], f"Also covered by {item['source']}",
                           thread_ts=original["ts"],
                           blocks=[{"type": "section", "text": {"type": "mrkdwn",
                                    "text": f"Also covered by {esc(item['source'])}: <{item['url']}|{esc(item['title'])}>"}}])
                continue
            if v["duplicate_of"]:
                continue  # duplicate of something we didn't post; skip it too

            if channel in broken_channels:
                del state["seen"][item["id"]]
                continue
            breaking = score >= s["breaking_score"]
            under_cap = counts.get(channel, 0) < s["daily_realtime_cap"]
            if breaking or (score >= s["realtime_score"] and under_cap):
                article = fetch_article(item["url"])
                enrich_item(item, article)
                summary = summarize(client, config, item, article)
                if summary is None:
                    continue
                try:
                    ts = slack.post(channel_ids[channel], item["title"], article_blocks(item, summary, tz, breaking))
                except RuntimeError as exc:
                    log(f"SLACK ERROR [{channel}]: {exc}")
                    broken_channels.add(channel)
                    del state["seen"][item["id"]]
                    continue
                counts[channel] = counts.get(channel, 0) + 1
                post = {"id": item["id"], "channel": channel, "title": item["title"], "ts": ts, "at": now.isoformat()}
                state["posted"].append(post)
                posted_by_id[item["id"]] = post
                log(f"Posted [{channel}] ({score}) {item['title']}")
            else:
                state["digest_queue"].setdefault(channel, []).append({
                    "id": item["id"], "url": item["url"], "title": item["title"], "source": item["source"],
                    "one_liner": item["one_liner"], "score": score, "at": now.isoformat()})
                log(f"Queued for digest [{channel}] ({score}) {item['title']}")

    # daily digest
    if now.astimezone(tz).hour >= s["digest_hour"]:
        for channel, queue in state["digest_queue"].items():
            if not queue or state["digest_sent"].get(channel) == today or channel not in channel_ids:
                continue
            unique = {i["title"].strip().lower(): i for i in sorted(queue, key=lambda i: i["score"])}
            top = sorted(unique.values(), key=lambda i: -i["score"])[: s["digest_max_items"]]
            try:
                slack.post(channel_ids[channel], "Also worth a look", digest_blocks(top, tz, now))
            except RuntimeError as exc:
                log(f"SLACK ERROR [{channel}] digest: {exc}")
                continue
            state["digest_sent"][channel] = today
            state["digest_queue"][channel] = []
            log(f"Posted digest to {channel} ({len(top)} items)")


def check_feeds(config):
    for source in config["sources"]:
        try:
            items = fetch_source(source)
            newest = max((i["published"] for i in items if i["published"]), default="no dates")
            print(f"OK    {len(items):>4} items  newest={newest[:10]}  {source['name']}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {source['name']}: {exc}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-feeds", action="store_true")
    parser.add_argument("--test-limit", type=int, help="with --dry-run: skip seeding and age limits, "
                        "take the N newest items from every source (to preview output)")
    parser.add_argument("--sample", metavar="SOURCE_NAME",
                        help="post the newest article from this source to Slack, to preview formatting")
    args = parser.parse_args()

    config = yaml.safe_load(CONFIG_PATH.read_text())
    if args.check_feeds:
        check_feeds(config)
        return

    now = datetime.now(timezone.utc)
    state = normalize_state(load_state())
    prune_state(state, now)
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token and not args.dry_run:
        sys.exit("SLACK_BOT_TOKEN is not set")

    try:
        # Org-level API keys (not tied to a workspace) must name the workspace on every request.
        workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        client = anthropic.Anthropic(default_headers={"anthropic-workspace-id": workspace} if workspace else None)
        if args.sample:
            post_sample(config, client, Slack(token, args.dry_run), args.sample)
            return
        run(config, state, client, Slack(token, args.dry_run), now,
            args.test_limit if args.dry_run else None)
    finally:
        # save progress even if something failed midway, so posted items aren't re-posted
        if not args.dry_run and not args.sample:
            save_state(state)


if __name__ == "__main__":
    main()
