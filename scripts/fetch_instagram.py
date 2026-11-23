#!/usr/bin/env python3
"""Holt Profil, Beiträge, Insights, Kommentare und Account-Insights von Instagram.

Nur Python-Standardbibliothek. Ausgabe: data/<JJJJ-MM-TT>/ (JSON + posts.csv).

Authentifizierung
  Variante A (Standard): kein Token im Code. Der Netzwerk-Proxy hängt den
      Authorization-Header an die Anfragen an.
  Variante B: Ist die Umgebungsvariable IG_ACCESS_TOKEN gesetzt, wird sie als
      "Authorization: Bearer ..." gesendet. Der Token wird nie ausgegeben und
      nie gespeichert (auch nicht in URLs, Logs oder Fehlermeldungen).

Optionale Umgebungsvariablen
  IG_API_HOST      Standard: graph.facebook.com (Facebook-Login-/Systemnutzer-
                   Token, "EAA…"). Das Instagram-Konto wird über die Seiten des
                   Tokens gesucht. Für einen Instagram-Login-Token ("IGAA…")
                   graph.instagram.com setzen.
  IG_API_VERSION   Standard: v26.0
  IG_USER_ID       Instagram-Konto-ID, falls sie nicht automatisch gefunden wird.

Metriknamen laut Meta-Doku (geprüft am 2026-11-23):
  developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights
  developers.facebook.com/docs/instagram-platform/api-reference/instagram-user/insights
Metriken, die es für einen Beitrag oder Medientyp nicht gibt, werden abgefangen
und in fetch_report.json protokolliert, statt den Lauf abzubrechen.
"""

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

HOST = os.environ.get("IG_API_HOST", "graph.facebook.com")
VERSION = os.environ.get("IG_API_VERSION", "v26.0")
BASE = f"https://{HOST}/{VERSION}"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECTED_USERNAME = "ademxkartal"
COMMENT_POSTS = 30      # Kommentare der letzten N Beiträge
ACCOUNT_DAYS = 30       # Zeitraum der Account-Insights

PROFILE_FIELDS = [
    "user_id", "username", "name", "account_type", "biography",
    "followers_count", "follows_count", "media_count", "profile_picture_url",
]
MEDIA_FIELDS = [
    "id", "caption", "media_type", "media_product_type", "timestamp",
    "permalink", "like_count", "comments_count", "is_shared_to_feed",
]
COMMENT_FIELDS = [
    "id", "text", "timestamp", "username", "like_count", "hidden", "parent_id",
    "replies{id,text,timestamp,username,like_count}",
]

# Beitrags-Insights nach Art des Beitrags (Spalte "media product type" der Doku).
FEED_METRICS = [
    "views", "reach", "saved", "shares", "total_interactions", "likes",
    "comments", "reposts", "follows", "profile_visits", "profile_activity",
]
REEL_METRICS = [
    "views", "reach", "saved", "shares", "total_interactions", "likes",
    "comments", "reposts",
    "ig_reels_avg_watch_time",           # Millisekunden
    "ig_reels_video_view_total_time",    # Millisekunden
    "reels_skip_rate", "crossposted_views", "facebook_views",
]
ALL_POST_METRICS = list(dict.fromkeys(FEED_METRICS + REEL_METRICS))

# Account-Insights (period=day, metric_type=total_value).
ACCOUNT_TOTALS = [
    "reach", "views", "accounts_engaged", "total_interactions", "likes",
    "comments", "shares", "saves", "replies", "reposts",
    "follows_and_unfollows", "profile_links_taps",
]
ACCOUNT_BREAKDOWNS = [
    ("reach", "media_product_type"), ("reach", "follow_type"),
    ("views", "media_product_type"), ("views", "follow_type"),
    ("total_interactions", "media_product_type"),
    ("follows_and_unfollows", "follow_type"),
]
ACCOUNT_SERIES = ["reach", "follower_count"]   # Tageswerte
DEMOGRAPHIC_METRICS = ["follower_demographics", "engaged_audience_demographics"]
DEMOGRAPHIC_BREAKDOWNS = ["age", "gender", "country", "city"]

_TOKEN_RE = re.compile(r"(access_token=)[^&\s\"']+")


class ApiError(Exception):
    def __init__(self, status, message, code=None, subcode=None):
        super().__init__(message)
        self.status, self.message, self.code, self.subcode = status, message, code, subcode

    def as_dict(self):
        return {"http_status": self.status, "code": self.code,
                "subcode": self.subcode, "message": self.message}


def scrub(text):
    """Entfernt Tokens aus Texten, bevor sie ausgegeben oder gespeichert werden."""
    text = _TOKEN_RE.sub(r"\1***", str(text))
    token = os.environ.get("IG_ACCESS_TOKEN")
    return text.replace(token, "***") if token else text


def log(msg):
    print(scrub(msg), file=sys.stderr, flush=True)


def strip_token(url):
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k != "access_token"]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def api_get(path_or_url, params=None):
    """GET gegen die Graph-API. Wiederholt bei Netzwerk-, 5xx- und Rate-Limit-Fehlern."""
    url = path_or_url if path_or_url.startswith("https://") else f"{BASE}/{path_or_url.lstrip('/')}"
    url = strip_token(url)
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "fetch-instagram/1.0"}
    token = os.environ.get("IG_ACCESS_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last = None
    for attempt in range(5):
        if attempt:
            time.sleep(2 ** attempt)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                err = json.loads(body).get("error", {})
            except (ValueError, AttributeError):
                err = {}
            last = ApiError(exc.code, scrub(err.get("message") or body[:300] or exc.reason),
                            err.get("code"), err.get("error_subcode"))
            rate_limited = exc.code == 429 or err.get("code") in (4, 17, 32, 613)
            transient = exc.code >= 500 or err.get("is_transient") or err.get("code") in (1, 2)
            if rate_limited:
                time.sleep(60)
            if not (rate_limited or transient):
                raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
            last = ApiError(None, scrub(f"Netzwerk-/Antwortfehler: {exc}"))
    raise last


def paginate(path, params, max_items=None):
    items, url, query = [], path, params
    while url:
        page = api_get(url, query)
        items.extend(page.get("data", []))
        if max_items and len(items) >= max_items:
            return items[:max_items]
        url, query = page.get("paging", {}).get("next"), None
    return items


def get_with_field_fallback(fetch, fields, required):
    """Ruft fetch(fields) auf; lehnt die API optionale Felder ab, werden sie einzeln entfernt."""
    fields, dropped = list(fields), []
    while True:
        try:
            return fetch(fields), dropped
        except ApiError as exc:
            if exc.status != 400 or exc.code != 100:
                raise
            named = [f for f in fields if f not in required and f.split("{")[0] in exc.message]
            optional = [f for f in fields if f not in required]
            if not optional:
                raise
            victim = named[0] if named else optional[-1]
            fields.remove(victim)
            dropped.append({"field": victim, "error": exc.message})


def post_kind(media):
    """REEL, CAROUSEL, IMAGE oder VIDEO (Feed-Video) – Basis für den Typ-Vergleich."""
    if media.get("media_product_type") == "REELS" or "/reel/" in (media.get("permalink") or ""):
        return "REEL"
    return {"CAROUSEL_ALBUM": "CAROUSEL"}.get(media.get("media_type"), media.get("media_type") or "UNKNOWN")


def metric_value(entry):
    if "total_value" in entry:
        return entry["total_value"].get("value")
    values = entry.get("values") or []
    return values[0].get("value") if values else None


class PostInsights:
    """Holt Beitrags-Insights; fängt Metriken ab, die es für den Beitrag nicht gibt."""

    def __init__(self):
        self.failures = {}      # (kind, metric) -> Anzahl Fehlschläge in Folge
        self.dropped = {}       # kind -> {metric: Fehlermeldung}

    def metrics_for(self, kind):
        base = REEL_METRICS if kind == "REEL" else FEED_METRICS
        return [m for m in base if m not in self.dropped.get(kind, {})]

    def fetch(self, media):
        kind, media_id = media["kind"], media["id"]
        metrics = self.metrics_for(kind)
        values, errors = {}, {}
        try:
            data = api_get(f"{media_id}/insights", {"metric": ",".join(metrics)}).get("data", [])
            values = {e["name"]: metric_value(e) for e in data}
        except ApiError as batch_error:
            # Sammelabruf abgelehnt: jede Metrik einzeln, um die ungültige zu finden.
            for metric in metrics:
                try:
                    data = api_get(f"{media_id}/insights", {"metric": metric}).get("data", [])
                    values.update({e["name"]: metric_value(e) for e in data})
                except ApiError as exc:
                    errors[metric] = exc.message
            if not values:
                # Kein einziger Wert: Problem des Beitrags (z. B. vor Umstellung auf
                # Business-Konto gepostet), nicht der einzelnen Metrik.
                return {}, {"_all": batch_error.message}
        for metric in metrics:
            key = (kind, metric)
            if metric in errors:
                self.failures[key] = self.failures.get(key, 0) + 1
                if self.failures[key] >= 3:     # dreimal in Folge: für diesen Typ nicht verfügbar
                    self.dropped.setdefault(kind, {})[metric] = errors[metric]
                    log(f"  Metrik '{metric}' für {kind} nicht verfügbar – wird übersprungen.")
            else:
                self.failures[key] = 0
                if metric not in values:
                    errors[metric] = "von der API nicht geliefert"
        return values, errors


def fetch_insight_set(user_id, label, params, report):
    """Ein Account-Insights-Abruf; bei Ablehnung des Sammelabrufs jede Metrik einzeln."""
    metrics = params["metric"].split(",")
    try:
        return api_get(f"{user_id}/insights", params).get("data", [])
    except ApiError as exc:
        if len(metrics) == 1:
            report["account_insight_errors"].append({"request": label, "metric": metrics[0], "error": exc.message})
            return []
    data = []
    for metric in metrics:
        try:
            data.extend(api_get(f"{user_id}/insights", {**params, "metric": metric}).get("data", []))
        except ApiError as exc:
            report["account_insight_errors"].append({"request": label, "metric": metric, "error": exc.message})
    return data


def resolve_user():
    """Liefert (Pfad-ID des Instagram-Kontos, Profil, abgelehnte Felder)."""
    user_id = os.environ.get("IG_USER_ID")
    if not user_id and HOST == "graph.facebook.com":
        # me/accounts: Seiten eines Nutzers; me/assigned_pages: Seiten eines Systemnutzers.
        accounts = []
        for edge in ("me/accounts", "me/assigned_pages"):
            try:
                pages = paginate(edge, {"fields": "name,instagram_business_account{id,username}"})
            except ApiError:
                continue
            accounts += [p["instagram_business_account"] for p in pages if p.get("instagram_business_account")]
        if not accounts:
            raise ApiError(None, "Über graph.facebook.com ist kein Instagram-Konto erreichbar: "
                                 "weder /me/accounts noch /me/assigned_pages liefern eine Seite mit "
                                 "verknüpftem Instagram-Konto. Nötig: instagram_basic, "
                                 "instagram_manage_insights und eine zugewiesene Seite – "
                                 "oder IG_USER_ID setzen.")
        match = [a for a in accounts if (a.get("username") or "").lower() == EXPECTED_USERNAME]
        user_id = (match or accounts)[0]["id"]
    target = user_id or "me"
    required = ["username"]
    fields = [f for f in PROFILE_FIELDS if not (HOST == "graph.facebook.com" and f in ("user_id", "account_type"))]
    if HOST == "graph.facebook.com":
        fields.insert(0, "id")
    profile, dropped = get_with_field_fallback(
        lambda fs: api_get(target, {"fields": ",".join(fs)}), fields, required)
    return str(profile.get("user_id") or profile.get("id") or target), profile, dropped


def write_json(folder, name, payload):
    with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def ratio(a, b):
    return round(a / b, 4) if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b else None


def write_csv(folder, posts, followers):
    columns = (["id", "timestamp", "kind", "media_type", "media_product_type", "permalink", "caption",
                "like_count", "comments_count"] + ALL_POST_METRICS +
               ["avg_watch_time_s", "total_watch_time_s", "reach_per_follower", "save_rate",
                "share_rate", "insights_missing"])
    with open(os.path.join(folder, "posts.csv"), "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for post in posts:
            ins = post.get("insights", {})
            flat = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                    for k, v in ins.items()}
            row = {**{c: post.get(c) for c in columns[:9]}, **flat}
            row["avg_watch_time_s"] = ratio(ins.get("ig_reels_avg_watch_time"), 1000)
            row["total_watch_time_s"] = ratio(ins.get("ig_reels_video_view_total_time"), 1000)
            row["reach_per_follower"] = ratio(ins.get("reach"), followers)
            row["save_rate"] = ratio(ins.get("saved"), ins.get("reach"))
            row["share_rate"] = ratio(ins.get("shares"), ins.get("reach"))
            row["insights_missing"] = ";".join(sorted(post.get("insights_errors", {})))
            writer.writerow(row)


def main():
    now = dt.datetime.now(dt.timezone.utc)
    report = {
        "fetched_at": now.isoformat(timespec="seconds"), "api": BASE,
        "auth": "IG_ACCESS_TOKEN" if os.environ.get("IG_ACCESS_TOKEN") else "proxy",
        "warnings": [], "dropped_fields": [], "post_insight_errors": [],
        "metrics_unavailable_by_kind": {}, "comment_errors": [], "account_insight_errors": [],
    }

    log(f"Profil abrufen ({BASE}) …")
    try:
        user_id, profile, dropped = resolve_user()
    except ApiError as exc:
        log(f"FEHLER: Profil nicht abrufbar: {exc.message}")
        if exc.code == 190 or exc.status == 401:
            log("Der Zugriffstoken fehlt oder ist ungültig. Variante A: Der Proxy muss für "
                f"{HOST} einen gültigen Token anhängen. Variante B: IG_ACCESS_TOKEN setzen. "
                "Nötige Rechte (Instagram-Login): instagram_business_basic, "
                "instagram_business_manage_insights, instagram_business_manage_comments. "
                "Hinweis: graph.instagram.com nimmt nur Instagram-Login-Tokens an; ein "
                "Facebook-Login-Token gehört zu IG_API_HOST=graph.facebook.com.")
        return 1
    report["dropped_fields"] += [{"object": "profile", **d} for d in dropped]
    followers = profile.get("followers_count")
    log(f"  @{profile.get('username')}: {followers} Follower, {profile.get('media_count')} Beiträge")
    if (profile.get("username") or "").lower() != EXPECTED_USERNAME:
        report["warnings"].append(f"Konto ist @{profile.get('username')}, erwartet war @{EXPECTED_USERNAME}.")

    folder = os.path.join(ROOT, "data", now.astimezone().strftime("%Y-%m-%d"))
    os.makedirs(folder, exist_ok=True)
    write_json(folder, "profile.json", profile)

    log("Beiträge abrufen …")
    posts, dropped = get_with_field_fallback(
        lambda fs: paginate(f"{user_id}/media", {"fields": ",".join(fs), "limit": 50}),
        MEDIA_FIELDS, ["id", "media_type", "timestamp", "permalink"])
    report["dropped_fields"] += [{"object": "media", **d} for d in dropped]
    posts.sort(key=lambda p: p.get("timestamp", ""), reverse=True)
    # media_count des Profils zählt offenbar nur Beiträge im Raster; Reels, die nicht im
    # Feed geteilt wurden (is_shared_to_feed = false), liefert die API zusätzlich.
    off_grid = sum(p.get("is_shared_to_feed") is False for p in posts)
    if profile.get("media_count") is not None and len(posts) - off_grid != profile["media_count"]:
        report["warnings"].append(
            f"{len(posts)} Beiträge geladen, davon {off_grid} Reels außerhalb des Rasters "
            f"(bleiben {len(posts) - off_grid}); Profil meldet {profile['media_count']}.")

    log(f"Insights für {len(posts)} Beiträge abrufen …")
    insights = PostInsights()
    for i, post in enumerate(posts, 1):
        post["kind"] = post_kind(post)
        post["insights"], post["insights_errors"] = insights.fetch(post)
        if post["insights_errors"]:
            report["post_insight_errors"].append({
                "id": post["id"], "kind": post["kind"], "timestamp": post.get("timestamp"),
                "errors": post["insights_errors"]})
        if i % 20 == 0:
            log(f"  {i}/{len(posts)}")
    report["metrics_unavailable_by_kind"] = insights.dropped
    write_json(folder, "media.json", posts)
    write_csv(folder, posts, followers)

    log(f"Kommentare der letzten {COMMENT_POSTS} Beiträge abrufen …")
    comments = []
    for post in posts[:COMMENT_POSTS]:
        entry = {"media_id": post["id"], "permalink": post.get("permalink"),
                 "timestamp": post.get("timestamp"), "comments_count": post.get("comments_count"),
                 "comments": []}
        if post.get("comments_count") != 0:
            try:
                entry["comments"], dropped = get_with_field_fallback(
                    lambda fs, pid=post["id"]: paginate(f"{pid}/comments", {"fields": ",".join(fs), "limit": 50}),
                    COMMENT_FIELDS, ["id", "text", "timestamp"])
                for d in dropped:
                    if {"object": "comment", **d} not in report["dropped_fields"]:
                        report["dropped_fields"].append({"object": "comment", **d})
            except ApiError as exc:
                report["comment_errors"].append({"media_id": post["id"], "error": exc.message})
        comments.append(entry)
    write_json(folder, "comments.json", comments)

    log(f"Account-Insights der letzten {ACCOUNT_DAYS} Tage abrufen …")
    until = int(now.timestamp())
    since = until - ACCOUNT_DAYS * 86400
    window = {"period": "day", "since": since, "until": until}
    account = {
        "since": dt.datetime.fromtimestamp(since, dt.timezone.utc).isoformat(timespec="seconds"),
        "until": now.isoformat(timespec="seconds"),
        "totals": fetch_insight_set(user_id, "totals", {
            **window, "metric": ",".join(ACCOUNT_TOTALS), "metric_type": "total_value"}, report),
        "breakdowns": [],
        "daily": fetch_insight_set(user_id, "daily", {
            **window, "metric": ",".join(ACCOUNT_SERIES), "metric_type": "time_series"}, report),
    }
    for metric, breakdown in ACCOUNT_BREAKDOWNS:
        for entry in fetch_insight_set(user_id, f"breakdown:{breakdown}", {
                **window, "metric": metric, "metric_type": "total_value", "breakdown": breakdown}, report):
            account["breakdowns"].append({"breakdown": breakdown, **entry})
    write_json(folder, "account_insights.json", account)

    log("Demografie abrufen …")
    demographics = {}
    for metric in DEMOGRAPHIC_METRICS:
        demographics[metric] = {}
        for breakdown in DEMOGRAPHIC_BREAKDOWNS:
            data = fetch_insight_set(user_id, f"demographics:{breakdown}", {
                "metric": metric, "period": "lifetime", "metric_type": "total_value",
                "timeframe": "this_month", "breakdown": breakdown}, report)
            demographics[metric][breakdown] = data[0] if data else None
    write_json(folder, "demographics.json", demographics)

    report["counts"] = {
        "posts": len(posts),
        "posts_by_kind": {k: sum(p["kind"] == k for p in posts) for k in sorted({p["kind"] for p in posts})},
        "posts_without_any_insights": sum(not p["insights"] for p in posts),
        "posts_with_partial_insights": sum(bool(p["insights"]) and bool(p["insights_errors"]) for p in posts),
        "comments": sum(len(c["comments"]) for c in comments),
        "comment_posts": len(comments),
    }
    write_json(folder, "fetch_report.json", report)

    log(f"Fertig: {os.path.relpath(folder, ROOT)}/")
    log(json.dumps(report["counts"], ensure_ascii=False))
    for warning in report["warnings"]:
        log(f"WARNUNG: {warning}")
    problems = (len(report["post_insight_errors"]) + len(report["comment_errors"])
                + len(report["account_insight_errors"]))
    if problems:
        log(f"{problems} Lücken/Fehler protokolliert – siehe fetch_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
