#!/usr/bin/env python3
"""Read-only BMHM LinkedIn collector. Python 3.10+, standard library only.

Setup (BMHM must authorize an approved LinkedIn application):
  export LINKEDIN_ACCESS_TOKEN='...'
  export LINKEDIN_ORGANIZATION_ID='123456'
  export LINKEDIN_VERSION='202609'  # use a currently supported YYYYMM version

Examples:
  python BMHM_LinkedIn.py events --out ./BMHM_LinkedIn_Data
  python BMHM_LinkedIn.py posts --out ./BMHM_LinkedIn_Data
  python BMHM_LinkedIn.py registrations --out ./BMHM_LinkedIn_Data
  python BMHM_LinkedIn.py videos --out ./BMHM_LinkedIn_Data
  python BMHM_LinkedIn.py merge --out ./BMHM_LinkedIn_Data

Events requires r_events; posts require r_organization_social; registrations
require separately approved Lead Sync access. Video lookups require approved
access and video IDs obtained from posts. This program does not scrape pages,
download recordings, or infer who actually viewed an event.
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://api.linkedin.com/rest"


def timestamp(ms):
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return ""


def localized(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        choices = value.get("localized", value)
        if isinstance(choices, dict):
            first = next(iter(choices.values()), "")
            return first.get("rawText", "") if isinstance(first, dict) else str(first)
    return ""


def nested(obj, *keys):
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def save_rows(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_json(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


class LinkedIn:
    def __init__(self, token, version):
        self.token, self.version = token, version

    def get(self, route, query):
        url = BASE + route + "?" + urllib.parse.urlencode(query, safe="(),:")
        headers = {
            "Authorization": "Bearer " + self.token,
            "Linkedin-Version": self.version,
            "X-Restli-Protocol-Version": "2.0.0",
            "Accept": "application/json",
        }
        for attempt in range(4):
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=45) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                detail = exc.read(1200).decode("utf-8", "replace")
                raise RuntimeError(f"LinkedIn HTTP {exc.code} on {route}: {detail}") from None
        raise RuntimeError("Request retries exhausted")

    def pages(self, route, query, max_pages):
        results, start, count = [], 0, 100
        for _ in range(max_pages):
            page = self.get(route, {**query, "start": start, "count": count})
            elements = page.get("elements", [])
            if not isinstance(elements, list):
                raise RuntimeError("Unexpected API response: missing elements list")
            results.extend(elements)
            print(f"{route}: fetched {len(elements)} at offset {start}", file=sys.stderr)
            if not elements or len(elements) < count:
                break
            next_start = nested(page, "paging", "start")
            start = (int(next_start) if next_start is not None else start) + len(elements)
        else:
            print("Page limit reached; raise --max-pages to continue a larger collection.", file=sys.stderr)
        return results


def event_rows(items):
    rows = []
    for e in items:
        kind = "live" if nested(e, "type", "online", "format", "liveVideo") is not None else (
            "external" if nested(e, "type", "online", "format", "external") is not None else "in_person")
        rows.append({"event_id": str(e.get("id", "")), "title": localized(e.get("name")),
                     "description": localized(e.get("description")), "starts_at_utc": timestamp(e.get("startsAt")),
                     "type": kind, "organizer": e.get("organizer", ""),
                     "post_urn": e.get("ugcPost", ""), "vanity_name": e.get("vanityName", "")})
    return rows


def post_rows(items):
    rows = []
    for p in items:
        content = p.get("content") or {}
        rows.append({"post_id": p.get("id", ""), "published_at_utc": timestamp(p.get("publishedAt")),
                     "commentary": p.get("commentary", ""), "content_type": next(iter(content), ""),
                     "content_json": json.dumps(content, ensure_ascii=False),
                     "lifecycle_state": p.get("lifecycleState", "")})
    return rows


def video_ids(posts):
    ids = set()
    for p in posts:
        # Search typed URNs anywhere in post content, including multi-content posts.
        ids.update(re.findall(r"urn:li:video:[A-Za-z0-9_-]+", json.dumps(p.get("content", {}))))
    return sorted(ids)


def run(args):
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.command == "merge":
        events, posts = read_json(out / "events.json"), read_json(out / "posts.json")
        erows, prows = event_rows(events), post_rows(posts)
        by_post = {str(p["post_id"]): p for p in prows}
        merged = []
        for e in erows:
            match = by_post.get(e["post_urn"], {})
            merged.append({**e, "linked_post_id": match.get("post_id", ""),
                           "post_commentary": match.get("commentary", ""), "review_status": "unreviewed"})
        save_rows(out / "episode_candidates.csv", merged,
                  list(erows[0]) + ["linked_post_id", "post_commentary", "review_status"] if erows else
                  ["event_id", "title", "description", "starts_at_utc", "type", "organizer", "post_urn",
                   "vanity_name", "linked_post_id", "post_commentary", "review_status"])
        print(f"Wrote {len(merged)} event candidates. Posts without linked events remain in posts.csv.")
        return

    token = os.environ.get("LINKEDIN_ACCESS_TOKEN")
    org_id = os.environ.get("LINKEDIN_ORGANIZATION_ID")
    version = os.environ.get("LINKEDIN_VERSION")
    if not all((token, org_id, version)) or not re.fullmatch(r"\d{6}", version or ""):
        raise RuntimeError("Set LINKEDIN_ACCESS_TOKEN, LINKEDIN_ORGANIZATION_ID and LINKEDIN_VERSION=YYYYMM")
    client, org = LinkedIn(token, version), "urn:li:organization:" + org_id

    if args.command == "events":
        items = client.pages("/events", {"q": "eventsByOrganizer", "organizer": org,
                             "timeBasedFilter": "(lifeCycleState:PAST)", "entryCriteria": "PUBLIC",
                             "sortOrder": "END_TIME_DESC", "excludeCancelled": "false"}, args.max_pages)
        rows, fields = event_rows(items), ["event_id", "title", "description", "starts_at_utc", "type",
                                           "organizer", "post_urn", "vanity_name"]
    elif args.command == "posts":
        items = client.pages("/posts", {"q": "author", "author": org, "sortBy": "LAST_MODIFIED"}, args.max_pages)
        rows, fields = post_rows(items), ["post_id", "published_at_utc", "commentary", "content_type",
                                          "content_json", "lifecycle_state"]
    elif args.command == "registrations":
        # Requires an event registration form and separately approved Lead Sync access.
        items = client.pages("/leadFormResponses", {"q": "owner", "owner": f"(organization:{org})",
                                               "leadType": "(leadType:EVENT)", "limitedToTestLeads": "false"}, args.max_pages)
        rows = [{"response_id": str(x.get("id", "")), "event_urn": nested(x, "associatedEntity", "event") or "",
                 "submitted_at_utc": timestamp(x.get("submittedAt")), "response_json": json.dumps(x, ensure_ascii=False)}
                for x in items]
        fields = ["response_id", "event_urn", "submitted_at_utc", "response_json"]
    elif args.command == "videos":
        posts = read_json(out / "posts.json")
        if not posts:
            raise RuntimeError("Run 'posts' first; videos reads IDs from posts.json")
        items = []
        for urn in video_ids(posts):
            try:
                items.append(client.get("/videos/" + urllib.parse.quote(urn, safe=""), {}))
            except RuntimeError as exc:
                print(f"Video {urn}: {exc}", file=sys.stderr)
        rows = [{"video_urn": x.get("id", ""), "status": x.get("status", ""),
                 "download_url": x.get("downloadUrl", ""), "captions_url": x.get("captions", "")}
                for x in items]
        fields = ["video_urn", "status", "download_url", "captions_url"]
    else:
        raise RuntimeError("Unknown command")

    # Registration responses may contain personal data. Owner-only files; no tokens stored.
    previous_umask = os.umask(0o077)
    try:
        with (out / f"{args.command}.json").open("w", encoding="utf-8") as handle:
            json.dump(items, handle, ensure_ascii=False, indent=2)
        save_rows(out / f"{args.command}.csv", rows, fields)
    finally:
        os.umask(previous_umask)
    print(f"Saved {len(items)} {args.command} records in {out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["events", "posts", "registrations", "videos", "merge"])
    parser.add_argument("--out", default="./BMHM_LinkedIn_Data", help="Output directory")
    parser.add_argument("--max-pages", type=int, default=100, help="Safety limit per paginated endpoint")
    args = parser.parse_args()
    if args.max_pages < 1:
        parser.error("--max-pages must be positive")
    try:
        run(args)
    except (RuntimeError, urllib.error.URLError, OSError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
