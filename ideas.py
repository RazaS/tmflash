import csv
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.error import URLError
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from third_party.pubmed_sieve import helpers as sieve_helpers
    from third_party.pubmed_sieve import pubmed_sieve as sieve_query_builder
    PUBMED_SIEVE_IMPORT_ERROR = ""
except Exception as exc:
    sieve_helpers = None
    sieve_query_builder = None
    PUBMED_SIEVE_IMPORT_ERROR = str(exc)


APP_ROOT = Path(__file__).resolve().parent
FEEDS_CSV_PATH = APP_ROOT / "feeds.csv"
REFRESH_SECONDS = 15 * 60
FETCH_INTERVAL_SECONDS = 7 * 24 * 60 * 60
RUNTIME_APPEND_WINDOW_SECONDS = 7 * 24 * 60 * 60
MAX_STUDY_AGE_DAYS = 183
NOT_TRANSFUSION_VOTE_THRESHOLD = 2
MAX_SUMMARY_LEN = 900
MAX_ABSTRACT_LEN = 4000
PUBMED_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+\b", re.IGNORECASE)
TRANSFUSION_TERM_RE = re.compile(r"\b(?:transfusion|transfused|transfusing)\b", re.IGNORECASE)
PUBMED_SIEVE_QUERY_TERMS = ["transfusion", "transfused", "transfusing"]
PUBMED_SIEVE_DATE_FILTER = "\"last 6 months\"[dp]"
PUBMED_SIEVE_MAX_ITEMS = 150
DATA_DIR = APP_ROOT / "data"
STUDIES_CACHE_PATH = DATA_DIR / "studies_cache.json"
LEGACY_STUDIES_CACHE_PATH = APP_ROOT / "cache" / "studies_cache.json"
APP_DB_PATH = DATA_DIR / "app.db"
FRONTEND_DIST_DIR = APP_ROOT / "frontend" / "dist"


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1].lower()
    return tag.lower()


def get_child_text(node: ET.Element, candidates: List[str]) -> str:
    for child in node:
        if local_name(child.tag) in candidates and child.text:
            text = child.text.strip()
            if text:
                return text
    return ""


def parse_pub_date(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None

    # Handles common RSS formats like RFC 2822.
    try:
        dt = parsedate_to_datetime(raw)
        if dt:
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass

    # Handles ISO8601-like timestamps.
    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def clean_html(raw: str) -> str:
    if not raw:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", raw)
    unescaped = html.unescape(no_tags)
    compact = re.sub(r"\s+", " ", unescaped).strip()
    if len(compact) <= MAX_SUMMARY_LEN:
        return compact
    return compact[:MAX_SUMMARY_LEN].rstrip() + "..."


def matches_transfusion_terms(title: str, abstract_or_summary: str) -> bool:
    cleaned = clean_html(abstract_or_summary)
    blob = f"{title or ''} {cleaned or ''}".strip()
    if not blob:
        return False
    return bool(TRANSFUSION_TERM_RE.search(blob))


class StudyDeck:
    def __init__(self, feeds_csv_path: Path, refresh_seconds: int = REFRESH_SECONDS) -> None:
        self.feeds_csv_path = feeds_csv_path
        self.refresh_seconds = refresh_seconds
        self.lock = threading.Lock()
        self.items: List[Dict] = []
        self.deck: List[Dict] = []
        self.pubmed_cache: Dict[str, Dict] = {}
        self.last_refresh_ts = 0.0
        self.last_fetch_ts = 0.0
        self.last_error = ""
        self._load_cache_from_disk()

    def load_feeds(self) -> List[Dict[str, str]]:
        feeds: List[Dict[str, str]] = []
        if not self.feeds_csv_path.exists():
            return feeds

        with self.feeds_csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                name = (row.get("name") or "").strip()
                url = (row.get("url") or "").strip()
                enabled = (row.get("enabled") or "1").strip().lower()
                if not url:
                    continue
                if enabled in {"0", "false", "no", "off"}:
                    continue
                feeds.append({"name": name or "Unnamed Feed", "url": url})
        return feeds

    def fetch_feed(self, url: str) -> bytes:
        request = Request(
            url,
            headers={
                "User-Agent": "DoomScrollStudies/1.0 (+https://example.local)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
            },
        )
        with urlopen(request, timeout=12) as response:
            return response.read()

    def fetch_url(self, url: str, accept: str = "*/*") -> bytes:
        request = Request(
            url,
            headers={
                "User-Agent": "DoomScrollStudies/1.0 (+https://example.local)",
                "Accept": accept,
            },
        )
        with urlopen(request, timeout=12) as response:
            return response.read()

    def parse_feed_items(self, xml_bytes: bytes, configured_name: str, feed_url: str) -> List[Dict]:
        root = ET.fromstring(xml_bytes)
        root_tag = local_name(root.tag)
        parsed_items: List[Dict] = []

        if root_tag == "rss":
            channel = None
            for child in root:
                if local_name(child.tag) == "channel":
                    channel = child
                    break
            if channel is None:
                return parsed_items

            channel_title = get_child_text(channel, ["title"]) or configured_name
            for item in channel:
                if local_name(item.tag) != "item":
                    continue

                title = get_child_text(item, ["title"]) or "Untitled study"
                link = get_child_text(item, ["link"])
                published_raw = get_child_text(item, ["pubdate", "published", "updated", "date"])
                summary_raw = get_child_text(item, ["description", "summary", "content", "encoded"])
                guid = get_child_text(item, ["guid"])
                published_dt = parse_pub_date(published_raw)
                if not matches_transfusion_terms(title, summary_raw):
                    continue

                parsed_items.append(
                    self._build_study(
                        title=title,
                        link=link,
                        summary=summary_raw,
                        published_dt=published_dt,
                        journal_name=channel_title,
                        feed_url=feed_url,
                        stable_key=guid or link or title,
                    )
                )
            return parsed_items

        if root_tag == "feed":  # Atom
            feed_title = get_child_text(root, ["title"]) or configured_name
            for entry in root:
                if local_name(entry.tag) != "entry":
                    continue

                title = get_child_text(entry, ["title"]) or "Untitled study"
                link = ""
                for child in entry:
                    if local_name(child.tag) == "link":
                        rel = (child.attrib.get("rel") or "alternate").lower()
                        href = (child.attrib.get("href") or "").strip()
                        if rel == "alternate" and href:
                            link = href
                            break
                        if not link and href:
                            link = href

                published_raw = get_child_text(entry, ["published", "updated", "date"])
                summary_raw = get_child_text(entry, ["summary", "content"])
                entry_id = get_child_text(entry, ["id"])
                published_dt = parse_pub_date(published_raw)
                if not matches_transfusion_terms(title, summary_raw):
                    continue

                parsed_items.append(
                    self._build_study(
                        title=title,
                        link=link,
                        summary=summary_raw,
                        published_dt=published_dt,
                        journal_name=feed_title,
                        feed_url=feed_url,
                        stable_key=entry_id or link or title,
                    )
                )
            return parsed_items

        return parsed_items

    def _build_study(
        self,
        title: str,
        link: str,
        summary: str,
        published_dt: Optional[datetime],
        journal_name: str,
        feed_url: str,
        stable_key: str,
    ) -> Dict:
        now_utc = datetime.now(timezone.utc)
        if not stable_key:
            stable_key = f"{title}:{link}:{feed_url}"
        stable_hash = hashlib.sha1(stable_key.encode("utf-8", errors="ignore")).hexdigest()[:16]
        if published_dt:
            published_label = published_dt.strftime("%b %d, %Y")
            published_iso = published_dt.isoformat()
            published_sort_ts = published_dt.timestamp()
        else:
            published_label = "Date unavailable"
            published_iso = ""
            published_sort_ts = 0.0

        cleaned_summary = clean_html(summary) or "No abstract/summary included in this feed item."
        return {
            "id": stable_hash,
            "title": title.strip(),
            "link": link.strip(),
            "summary": cleaned_summary,
            "journal": journal_name.strip() or "Unknown journal",
            "feed_url": feed_url,
            "published_iso": published_iso,
            "published_label": published_label,
            "published_sort_ts": published_sort_ts,
            "first_seen_iso": now_utc.isoformat(),
            "first_seen_ts": now_utc.timestamp(),
        }

    def _ensure_first_seen(self, item: Dict, fallback_ts: float = 0.0) -> Dict:
        ts = float(item.get("first_seen_ts") or 0.0)
        if ts <= 0:
            ts = float(fallback_ts or 0.0)
        if ts <= 0:
            ts = datetime.now(timezone.utc).timestamp()

        item["first_seen_ts"] = ts
        first_seen_iso = item.get("first_seen_iso")
        if not first_seen_iso:
            item["first_seen_iso"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        return item

    def _preserve_first_seen(self, item: Dict, existing_by_key: Dict[str, Dict]) -> Dict:
        key = self._study_dedupe_key(item)
        existing = existing_by_key.get(key) if key else None
        if existing:
            return self._ensure_first_seen(
                item,
                fallback_ts=float(existing.get("first_seen_ts") or 0.0),
            )
        return self._ensure_first_seen(item)

    def _rebuild_deck_locked(self) -> None:
        self.deck = self.items.copy()
        random.shuffle(self.deck)

    def _prune_old_studies(self, studies: List[Dict]) -> List[Dict]:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=MAX_STUDY_AGE_DAYS)
        cutoff_ts = cutoff_dt.timestamp()
        pruned: List[Dict] = []
        for item in studies:
            ts = float(item.get("first_seen_ts") or 0.0)
            if ts <= 0:
                self._ensure_first_seen(item)
                ts = float(item.get("first_seen_ts") or 0.0)
            if ts >= cutoff_ts:
                pruned.append(item)
        return pruned

    def _load_cache_from_disk(self) -> None:
        try:
            source_path = STUDIES_CACHE_PATH
            if not source_path.exists() and LEGACY_STUDIES_CACHE_PATH.exists():
                source_path = LEGACY_STUDIES_CACHE_PATH
            if not source_path.exists():
                return
            payload = json.loads(source_path.read_text(encoding="utf-8"))
            cached_items = payload.get("items", [])
            if not isinstance(cached_items, list):
                return
            self.last_fetch_ts = float(payload.get("last_fetch_ts") or 0.0)
            saved_at_ts = 0.0
            saved_at_iso = str(payload.get("saved_at_iso") or "").strip()
            if saved_at_iso:
                try:
                    saved_at_ts = datetime.fromisoformat(saved_at_iso).timestamp()
                except ValueError:
                    saved_at_ts = 0.0

            first_seen_fallback_ts = self.last_fetch_ts or saved_at_ts
            normalized_items = [
                self._ensure_first_seen(dict(item), fallback_ts=first_seen_fallback_ts)
                for item in cached_items
                if isinstance(item, dict)
            ]
            self.items = self._prune_old_studies(normalized_items)
            self._apply_exclusions_locked()
            self.last_refresh_ts = time.time()
            self._rebuild_deck_locked()
        except Exception:
            # Ignore cache read errors and continue with live fetch path.
            self.items = []
            self.deck = []
            self.last_fetch_ts = 0.0

    def _save_cache_to_disk(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "last_fetch_ts": self.last_fetch_ts,
                "saved_at_iso": datetime.now(timezone.utc).isoformat(),
                "items": self.items,
            }
            temp_path = STUDIES_CACHE_PATH.with_suffix(".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
            os.replace(temp_path, STUDIES_CACHE_PATH)
        except Exception:
            pass

    def _filter_excluded_studies_locked(self, excluded_ids: set[str]) -> None:
        if not excluded_ids:
            return
        self.items = [item for item in self.items if item.get("id") not in excluded_ids]
        self.deck = [item for item in self.deck if item.get("id") not in excluded_ids]
        self.pubmed_cache = {k: v for k, v in self.pubmed_cache.items() if k not in excluded_ids}

    def _apply_exclusions_locked(self) -> None:
        self._filter_excluded_studies_locked(get_excluded_study_ids())

    def exclude_study_id(self, study_id: str) -> None:
        if not study_id:
            return
        with self.lock:
            before_items = len(self.items)
            before_deck = len(self.deck)
            self._filter_excluded_studies_locked({study_id})
            if len(self.items) != before_items or len(self.deck) != before_deck:
                self.last_refresh_ts = time.time()
                self._save_cache_to_disk()

    def _fetch_due(self) -> bool:
        if self.last_fetch_ts <= 0:
            return True
        return (time.time() - self.last_fetch_ts) >= FETCH_INTERVAL_SECONDS

    def _study_dedupe_key(self, item: Dict) -> str:
        key = str(item.get("link") or item.get("id") or "").strip().lower()
        return key or str(item.get("id") or "")

    def _fetch_external_items(self) -> tuple[List[Dict], List[str]]:
        feeds = self.load_feeds()
        all_items: List[Dict] = []
        errors: List[str] = []
        if not feeds:
            errors.append("No RSS feeds configured. Add at least one row to feeds.csv.")

        for feed in feeds:
            name = feed["name"]
            url = feed["url"]
            try:
                xml_bytes = self.fetch_feed(url)
                parsed = self.parse_feed_items(xml_bytes, configured_name=name, feed_url=url)
                all_items.extend(parsed)
            except (URLError, TimeoutError, ET.ParseError, ValueError) as exc:
                errors.append(f"{name}: {exc}")

        try:
            all_items.extend(self.fetch_pubmed_sieve_items())
        except Exception as exc:
            errors.append(f"PubMed (pubmed-sieve): {exc}")

        deduped: Dict[str, Dict] = {}
        for item in all_items:
            key = self._study_dedupe_key(item)
            if key and key not in deduped:
                deduped[key] = item
        deduped_items = list(deduped.values())
        excluded_ids = get_excluded_study_ids()
        if excluded_ids:
            deduped_items = [item for item in deduped_items if item.get("id") not in excluded_ids]
        return deduped_items, errors

    def _append_runtime_new_items_locked(self) -> None:
        all_items, errors = self._fetch_external_items()
        existing_keys = {self._study_dedupe_key(i) for i in self.items}
        cutoff_ts = time.time() - RUNTIME_APPEND_WINDOW_SECONDS

        new_items = []
        for item in all_items:
            key = self._study_dedupe_key(item)
            published_ts = float(item.get("published_sort_ts") or 0.0)
            if not key or key in existing_keys:
                continue
            if published_ts <= 0 or published_ts < cutoff_ts:
                continue
            existing_keys.add(key)
            new_items.append(self._ensure_first_seen(item))

        if new_items:
            # Keep append order deterministic by publication timestamp.
            new_items.sort(key=lambda x: float(x.get("published_sort_ts") or 0.0))
            self.items.extend(new_items)
            self.items = self._prune_old_studies(self.items)
            # Append to queue tail so current browsing order is preserved.
            self.deck.extend(new_items)

        self._apply_exclusions_locked()
        valid_ids = {item["id"] for item in self.items}
        self.deck = [item for item in self.deck if item.get("id") in valid_ids]
        self.pubmed_cache = {k: v for k, v in self.pubmed_cache.items() if k in valid_ids}
        self.last_fetch_ts = time.time()
        self.last_error = "; ".join(errors) if errors else ""
        self.last_refresh_ts = time.time()
        self._save_cache_to_disk()

    def fetch_pubmed_sieve_items(self) -> List[Dict]:
        if sieve_helpers is None or sieve_query_builder is None:
            raise RuntimeError(
                "pubmed-sieve unavailable. Install requirements or check third_party/pubmed_sieve. "
                f"Import error: {PUBMED_SIEVE_IMPORT_ERROR}"
            )

        # NCBI requests a contact email when using Entrez.
        if hasattr(sieve_helpers, "Entrez"):
            sieve_helpers.Entrez.email = "doomscroll-studies@example.com"

        keyword_base_query = sieve_query_builder.build_keyword_and_journal_query(
            keywords=PUBMED_SIEVE_QUERY_TERMS,
            journals=[],
            require_hasabstract=False,
        )
        if not keyword_base_query:
            return []
        query = f"({keyword_base_query}) AND ({PUBMED_SIEVE_DATE_FILTER})"

        records: List[Dict] = []
        seen_pmids = set()
        df = sieve_helpers.pubmed_articles_for_query(query)
        if df is not None and len(df) > 0:
            if len(df) > PUBMED_SIEVE_MAX_ITEMS:
                df = df.head(PUBMED_SIEVE_MAX_ITEMS)
            for row in df.to_dict(orient="records"):
                pmid = str(row.get("PMID") or "").strip()
                dedupe_key = pmid or str(row.get("Title") or "").strip().lower()
                if dedupe_key in seen_pmids:
                    continue
                seen_pmids.add(dedupe_key)
                records.append(row)

        if not records:
            return []

        parsed: List[Dict] = []
        for row in records:
            pmid = str(row.get("PMID") or "").strip()
            title = str(row.get("Title") or "").strip() or "Untitled PubMed study"
            journal = str(row.get("Journal") or "").strip() or "PubMed"
            abstract = str(row.get("Abstract") or "").strip()
            published_dt = self._year_to_datetime(row.get("Year"))
            parsed.append(
                self._build_study(
                    title=title,
                    link=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                    summary=abstract or "No abstract available from PubMed.",
                    published_dt=published_dt,
                    journal_name=journal,
                    feed_url=f"pubmed-sieve:{'|'.join(PUBMED_SIEVE_QUERY_TERMS)}",
                    stable_key=f"pubmed-sieve:{pmid or title}",
                )
            )

        return parsed

    def _year_to_datetime(self, value: object) -> Optional[datetime]:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw or raw.lower() == "nan":
            return None
        try:
            year = int(float(raw))
        except ValueError:
            return None
        if year < 1800 or year > 2200:
            return None
        return datetime(year, 1, 1, tzinfo=timezone.utc)

    def _refresh_locked(self, allow_external_fetch: bool = False, force_external_fetch: bool = False) -> None:
        # Runtime mode: never hit external sources. Serve from cached file only.
        if not allow_external_fetch:
            self.items = self._prune_old_studies(self.items)
            self._apply_exclusions_locked()
            valid_ids = {item["id"] for item in self.items}
            self.deck = [item for item in self.deck if item.get("id") in valid_ids]
            if not self.deck and self.items:
                self._rebuild_deck_locked()
            if not self.items:
                self.last_error = (
                    "No cached studies found. Run `python3 scripts/update_studies_cache.py` "
                    "to refresh data offline."
                )
            self.last_refresh_ts = time.time()
            return

        # Offline update mode: fetch at most weekly unless explicitly forced.
        if not force_external_fetch and not self._fetch_due() and self.items:
            self.items = self._prune_old_studies(self.items)
            self._apply_exclusions_locked()
            self._rebuild_deck_locked()
            self.last_refresh_ts = time.time()
            return

        all_items, errors = self._fetch_external_items()
        existing_by_key = {
            self._study_dedupe_key(item): item for item in self.items if self._study_dedupe_key(item)
        }
        all_items = [self._preserve_first_seen(item, existing_by_key) for item in all_items]
        fresh_items = sorted(
            all_items,
            key=lambda x: x.get("published_sort_ts", 0.0),
            reverse=True,
        )
        self.items = self._prune_old_studies(fresh_items)
        self._apply_exclusions_locked()
        valid_ids = {item["id"] for item in self.items}
        self.pubmed_cache = {k: v for k, v in self.pubmed_cache.items() if k in valid_ids}
        self._rebuild_deck_locked()
        self.last_fetch_ts = time.time()
        self.last_error = "; ".join(errors) if errors else ""
        self.last_refresh_ts = time.time()
        self._save_cache_to_disk()

    def force_refresh(self, allow_external_fetch: bool = False, force_external_fetch: bool = False) -> None:
        with self.lock:
            self._refresh_locked(
                allow_external_fetch=allow_external_fetch,
                force_external_fetch=force_external_fetch,
            )

    def reload_cache_from_disk(self) -> None:
        with self.lock:
            self._load_cache_from_disk()
            self._refresh_locked(allow_external_fetch=False)

    def maybe_refresh(self) -> None:
        with self.lock:
            stale = (time.time() - self.last_refresh_ts) > self.refresh_seconds
            if stale:
                self._refresh_locked(allow_external_fetch=False)
            if self._fetch_due():
                self._append_runtime_new_items_locked()

    def get_next(self, excluded_ids: Optional[set[str]] = None) -> Dict:
        self.maybe_refresh()
        with self.lock:
            self._apply_exclusions_locked()
            if not self.deck and self.items:
                self._rebuild_deck_locked()

            if not self.deck:
                return {
                    "ok": False,
                    "message": (
                        self.last_error
                        or "No cached studies available. Run `python3 scripts/update_studies_cache.py`."
                    ),
                    "study": None,
                    "total_loaded": 0,
                    "remaining_in_deck": 0,
                    "last_refresh_iso": self._last_refresh_iso(),
                }

            hidden_ids = excluded_ids or set()
            study = None
            if hidden_ids:
                attempts = len(self.deck)
                while attempts > 0 and self.deck:
                    candidate = self.deck.pop(0)
                    if candidate.get("id") in hidden_ids:
                        # Keep hidden studies in global pool for other users.
                        self.deck.append(candidate)
                    else:
                        study = candidate
                        break
                    attempts -= 1
            else:
                study = self.deck.pop(0)

            if study is None:
                return {
                    "ok": False,
                    "message": "No visible studies available for your account right now.",
                    "study": None,
                    "total_loaded": len(self.items),
                    "remaining_in_deck": len(self.deck),
                    "last_refresh_iso": self._last_refresh_iso(),
                }

            return {
                "ok": True,
                "message": self.last_error,
                "study": {
                    "id": study["id"],
                    "title": study["title"],
                    "link": study["link"],
                    "summary": study["summary"],
                    "journal": study["journal"],
                    "published_iso": study["published_iso"],
                    "published_label": study["published_label"],
                    "feed_url": study["feed_url"],
                },
                "total_loaded": len(self.items),
                "remaining_in_deck": len(self.deck),
                "last_refresh_iso": self._last_refresh_iso(),
            }

    def get_study_by_id(self, study_id: str) -> Optional[Dict]:
        self.maybe_refresh()
        with self.lock:
            self._apply_exclusions_locked()
            return next((item for item in self.items if item.get("id") == study_id), None)

    def _last_refresh_iso(self) -> str:
        if not self.last_refresh_ts:
            return ""
        return datetime.fromtimestamp(self.last_refresh_ts, tz=timezone.utc).isoformat()

    def get_abstract(self, study_id: str) -> Dict:
        self.maybe_refresh()
        with self.lock:
            self._apply_exclusions_locked()
            study = next((item for item in self.items if item["id"] == study_id), None)
            if study is None:
                return {"ok": False, "message": "Study not found.", "study_id": study_id}

            cached = self.pubmed_cache.get(study_id)
            if cached is not None:
                return {
                    "ok": True,
                    "study_id": study_id,
                    "abstract": cached["abstract"],
                    "source": cached["source"],
                    "message": cached["message"],
                }

            title = study.get("title", "")
            link = study.get("link", "")
            feed_summary = study.get("summary", "")

        lookup_result = self._lookup_pubmed_abstract(title=title, link=link, fallback=feed_summary)

        with self.lock:
            self.pubmed_cache[study_id] = lookup_result

        return {
            "ok": True,
            "study_id": study_id,
            "abstract": lookup_result["abstract"],
            "source": lookup_result["source"],
            "message": lookup_result["message"],
        }

    def _lookup_pubmed_abstract(self, title: str, link: str, fallback: str) -> Dict:
        doi = self._extract_doi(title, link, fallback)
        if doi:
            term = f"{doi}[doi]"
        elif title.strip():
            term = f"\"{title.strip()}\"[Title]"
        else:
            term = ""

        if not term:
            return {
                "abstract": fallback or "No abstract available.",
                "source": "feed",
                "message": "No title/DOI available for PubMed lookup.",
            }

        try:
            query = urlencode(
                {
                    "db": "pubmed",
                    "retmode": "json",
                    "retmax": "1",
                    "sort": "relevance",
                    "term": term,
                    "tool": "doomscroll-studies",
                }
            )
            search_url = f"{PUBMED_ESEARCH_URL}?{query}"
            search_bytes = self.fetch_url(search_url, accept="application/json")
            payload = json.loads(search_bytes.decode("utf-8", errors="replace"))
            id_list = payload.get("esearchresult", {}).get("idlist", [])
            if not id_list:
                return {
                    "abstract": fallback or "No abstract available.",
                    "source": "feed",
                    "message": "PubMed match not found; using feed summary.",
                }

            pmid = str(id_list[0]).strip()
            fetch_query = urlencode(
                {
                    "db": "pubmed",
                    "id": pmid,
                    "retmode": "xml",
                    "tool": "doomscroll-studies",
                }
            )
            fetch_url = f"{PUBMED_EFETCH_URL}?{fetch_query}"
            fetch_bytes = self.fetch_url(fetch_url, accept="application/xml,text/xml;q=0.9,*/*;q=0.1")
            abstract_text = self._extract_abstract_from_pubmed_xml(fetch_bytes)
            if abstract_text:
                return {
                    "abstract": abstract_text,
                    "source": "pubmed",
                    "message": f"Loaded from PubMed PMID {pmid}.",
                }

            return {
                "abstract": fallback or "No abstract available.",
                "source": "feed",
                "message": f"PubMed PMID {pmid} has no abstract text; using feed summary.",
            }
        except (URLError, TimeoutError, ET.ParseError, ValueError, json.JSONDecodeError) as exc:
            return {
                "abstract": fallback or "No abstract available.",
                "source": "feed",
                "message": f"PubMed lookup failed ({exc}); using feed summary.",
            }

    def _extract_doi(self, *values: str) -> str:
        for raw in values:
            if not raw:
                continue
            text = unquote(raw)
            match = DOI_RE.search(text)
            if match:
                return match.group(0)
        return ""

    def _extract_abstract_from_pubmed_xml(self, xml_bytes: bytes) -> str:
        root = ET.fromstring(xml_bytes)
        chunks: List[str] = []
        for node in root.findall(".//Abstract/AbstractText"):
            text = "".join(node.itertext()).strip()
            if not text:
                continue
            label = (node.attrib.get("Label") or "").strip()
            if label:
                chunks.append(f"{label}: {text}")
            else:
                chunks.append(text)
        combined = "\n\n".join(chunks).strip()
        return combined if len(combined) <= MAX_ABSTRACT_LEN else combined[:MAX_ABSTRACT_LEN].rstrip() + "..."


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(APP_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                study_id TEXT NOT NULL,
                title TEXT NOT NULL,
                journal TEXT,
                published_label TEXT,
                link TEXT,
                abstract TEXT,
                saved_at TEXT NOT NULL,
                UNIQUE(user_id, study_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                event_type TEXT NOT NULL,
                meta_json TEXT,
                user_agent TEXT,
                ip_address TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS study_exclusions (
                study_id TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                excluded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS study_exclusion_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                study_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(study_id, user_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_study_exclusion_votes_study_id
            ON study_exclusion_votes (study_id);

            CREATE TABLE IF NOT EXISTS user_hidden_studies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                study_id TEXT NOT NULL,
                hidden_at TEXT NOT NULL,
                UNIQUE(user_id, study_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_user_hidden_studies_user_id
            ON user_hidden_studies (user_id);

            CREATE TABLE IF NOT EXISTS user_seen_studies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                study_id TEXT NOT NULL,
                seen_at TEXT NOT NULL,
                UNIQUE(user_id, study_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_user_seen_studies_user_id
            ON user_seen_studies (user_id);
            """
        )


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_auth_payload() -> Dict:
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    return {"username": username, "password": password}


def current_user_id() -> Optional[int]:
    value = session.get("user_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def current_user_username() -> str:
    return str(session.get("username") or "")


def require_user() -> Optional[Dict]:
    user_id = current_user_id()
    if user_id is None:
        return {"ok": False, "message": "Login required."}
    return None


def get_excluded_study_ids() -> set[str]:
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT study_id FROM study_exclusions").fetchall()
        return {str(r["study_id"] or "").strip() for r in rows if str(r["study_id"] or "").strip()}
    except Exception:
        # If table does not exist yet or DB is unavailable, treat as no exclusions.
        return set()


def get_user_hidden_study_ids(user_id: int) -> set[str]:
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT study_id FROM user_hidden_studies WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return {str(r["study_id"] or "").strip() for r in rows if str(r["study_id"] or "").strip()}
    except Exception:
        return set()


def hide_study_for_user(user_id: int, study_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO user_hidden_studies (user_id, study_id, hidden_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, study_id) DO NOTHING
            """,
            (user_id, study_id, now_iso_utc()),
        )


def get_user_seen_study_ids(user_id: int) -> set[str]:
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT study_id FROM user_seen_studies WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return {str(r["study_id"] or "").strip() for r in rows if str(r["study_id"] or "").strip()}
    except Exception:
        return set()


def mark_study_seen_for_user(user_id: int, study_id: str) -> None:
    if not study_id:
        return
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO user_seen_studies (user_id, study_id, seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, study_id) DO NOTHING
                """,
                (user_id, study_id, now_iso_utc()),
            )
    except Exception:
        return


def register_not_transfusion_vote(study_id: str, user_id: int) -> Dict:
    with get_db() as conn:
        existing_exclusion = conn.execute(
            "SELECT 1 FROM study_exclusions WHERE study_id = ?",
            (study_id,),
        ).fetchone()
        already_voted = (
            conn.execute(
                "SELECT 1 FROM study_exclusion_votes WHERE study_id = ? AND user_id = ?",
                (study_id, user_id),
            ).fetchone()
            is not None
        )

        if existing_exclusion is None and not already_voted:
            conn.execute(
                """
                INSERT INTO study_exclusion_votes (study_id, user_id, created_at)
                VALUES (?, ?, ?)
                """,
                (study_id, user_id, now_iso_utc()),
            )

        votes = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM study_exclusion_votes WHERE study_id = ?",
                (study_id,),
            ).fetchone()["c"]
        )

        excluded_now = False
        excluded = existing_exclusion is not None
        if votes >= NOT_TRANSFUSION_VOTE_THRESHOLD and not excluded:
            conn.execute(
                """
                INSERT INTO study_exclusions (study_id, reason, excluded_at)
                VALUES (?, ?, ?)
                """,
                (study_id, "not_transfusion", now_iso_utc()),
            )
            excluded = True
            excluded_now = True

    if excluded:
        votes = max(votes, NOT_TRANSFUSION_VOTE_THRESHOLD)
    return {
        "study_id": study_id,
        "votes": votes,
        "threshold": NOT_TRANSFUSION_VOTE_THRESHOLD,
        "already_voted": already_voted,
        "excluded": excluded,
        "excluded_now": excluded_now,
    }


def client_ip() -> str:
    xff = str(request.headers.get("X-Forwarded-For") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return str(request.remote_addr or "")


def track_usage_event(event_type: str, meta: Optional[Dict] = None) -> None:
    try:
        meta = meta or {}
        user_id = current_user_id()
        username = current_user_username() if user_id is not None else ""
        user_agent = str(request.headers.get("User-Agent") or "")
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO usage_events (user_id, username, event_type, meta_json, user_agent, ip_address, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    event_type,
                    json.dumps(meta, ensure_ascii=True),
                    user_agent,
                    client_ip(),
                    now_iso_utc(),
                ),
            )
    except Exception:
        # Usage tracking must never break app flows.
        return


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or "dev-secret-change-me"
init_db()
deck = StudyDeck(FEEDS_CSV_PATH)


@app.get("/")
def index():
    index_path = FRONTEND_DIST_DIR / "index.html"
    if index_path.exists():
        return send_from_directory(FRONTEND_DIST_DIR, "index.html")
    return (
        "React frontend not built. Run `npm --prefix frontend install` then "
        "`npm --prefix frontend run build` and reload.",
        503,
    )


@app.get("/assets/<path:asset_path>")
def frontend_assets(asset_path: str):
    assets_dir = FRONTEND_DIST_DIR / "assets"
    if assets_dir.exists():
        return send_from_directory(assets_dir, asset_path)
    return ("Not Found", 404)


@app.get("/api/next")
def api_next():
    track_usage_event("next_api")
    user_id = current_user_id()
    excluded_ids: set[str] = set()
    if user_id is not None:
        user_id = int(user_id)
        excluded_ids = get_user_hidden_study_ids(user_id)
        excluded_ids.update(get_user_seen_study_ids(user_id))

    payload = deck.get_next(excluded_ids=excluded_ids)

    if user_id is not None and payload.get("ok"):
        study = payload.get("study") or {}
        study_id = str(study.get("id") or "").strip()
        if study_id:
            mark_study_seen_for_user(user_id, study_id)

    return jsonify(payload)


@app.get("/api/feeds")
def api_feeds():
    return jsonify({"feeds": deck.load_feeds()})


@app.post("/api/refresh")
def api_refresh():
    deck.reload_cache_from_disk()
    return jsonify(
        {
            "ok": True,
            "message": deck.last_error,
            "total_loaded": len(deck.items),
            "last_refresh_iso": deck._last_refresh_iso(),
        }
    )


@app.get("/api/abstract/<study_id>")
def api_abstract(study_id: str):
    track_usage_event("abstract_api", {"study_id": study_id})
    return jsonify(deck.get_abstract(study_id))


@app.get("/api/me")
def api_me():
    user_id = current_user_id()
    if user_id is None:
        return jsonify({"ok": True, "authenticated": False, "username": ""})
    return jsonify({"ok": True, "authenticated": True, "username": current_user_username()})


@app.post("/api/signup")
def api_signup():
    auth = parse_auth_payload()
    username = auth["username"]
    password = auth["password"]
    if len(username) < 3:
        return jsonify({"ok": False, "message": "Username must be at least 3 characters."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "message": "Password must be at least 6 characters."}), 400

    password_hash = generate_password_hash(password)
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, now_iso_utc()),
            )
            user_id = int(conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"])
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "message": "Username already exists."}), 409

    session["user_id"] = user_id
    session["username"] = username
    track_usage_event("signup", {"username": username})
    return jsonify({"ok": True, "message": "Account created.", "username": username})


@app.post("/api/login")
def api_login():
    auth = parse_auth_payload()
    username = auth["username"]
    password = auth["password"]
    if not username or not password:
        return jsonify({"ok": False, "message": "Username and password are required."}), 400

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None or not check_password_hash(str(row["password_hash"]), password):
        return jsonify({"ok": False, "message": "Invalid username or password."}), 401

    session["user_id"] = int(row["id"])
    session["username"] = str(row["username"])
    track_usage_event("login", {"username": str(row["username"])})
    return jsonify({"ok": True, "message": "Logged in.", "username": str(row["username"])})


@app.post("/api/logout")
def api_logout():
    track_usage_event("logout")
    session.clear()
    return jsonify({"ok": True, "message": "Logged out."})


@app.get("/api/archive")
def api_archive_list():
    unauth = require_user()
    if unauth:
        return jsonify(unauth), 401

    user_id = int(current_user_id() or 0)
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT study_id, title, journal, published_label, link, abstract, saved_at
            FROM archives
            WHERE user_id = ?
            ORDER BY saved_at DESC
            """,
            (user_id,),
        ).fetchall()

    entries = [
        {
            "study_id": str(r["study_id"] or ""),
            "title": str(r["title"] or ""),
            "journal": str(r["journal"] or ""),
            "published_label": str(r["published_label"] or ""),
            "link": str(r["link"] or ""),
            "abstract": str(r["abstract"] or ""),
            "saved_at": str(r["saved_at"] or ""),
        }
        for r in rows
    ]
    return jsonify({"ok": True, "entries": entries})


@app.post("/api/archive")
def api_archive_add():
    unauth = require_user()
    if unauth:
        return jsonify(unauth), 401

    payload = request.get_json(silent=True) or {}
    study_id = str(payload.get("study_id") or "").strip()
    if not study_id:
        return jsonify({"ok": False, "message": "study_id is required."}), 400

    study = deck.get_study_by_id(study_id)
    base = study or {}
    title = str(payload.get("title") or base.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "message": "Study title is required."}), 400

    journal = str(payload.get("journal") or base.get("journal") or "").strip()
    published_label = str(payload.get("published_label") or base.get("published_label") or "").strip()
    link = str(payload.get("link") or base.get("link") or "").strip()
    abstract_text = str(payload.get("abstract") or payload.get("summary") or base.get("summary") or "").strip()
    saved_at = now_iso_utc()
    user_id = int(current_user_id() or 0)

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO archives (user_id, study_id, title, journal, published_label, link, abstract, saved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, study_id) DO UPDATE SET
                title=excluded.title,
                journal=excluded.journal,
                published_label=excluded.published_label,
                link=excluded.link,
                abstract=excluded.abstract,
                saved_at=excluded.saved_at
            """,
            (user_id, study_id, title, journal, published_label, link, abstract_text, saved_at),
        )

    track_usage_event("archive_save", {"study_id": study_id})
    return jsonify({"ok": True, "message": "Saved to your archive."})


@app.post("/api/study/not-transfusion")
def api_study_not_transfusion():
    unauth = require_user()
    if unauth:
        return jsonify(unauth), 401

    payload = request.get_json(silent=True) or {}
    study_id = str(payload.get("study_id") or "").strip()
    if not study_id:
        return jsonify({"ok": False, "message": "study_id is required."}), 400

    user_id = int(current_user_id() or 0)
    hide_study_for_user(user_id, study_id)
    vote = register_not_transfusion_vote(study_id, user_id)
    if vote["excluded"]:
        deck.exclude_study_id(study_id)

    if vote["excluded_now"]:
        message = (
            f"Removed for everyone ({vote['votes']}/{vote['threshold']} votes). "
            "Hidden from your deck and removed globally."
        )
    elif vote["excluded"]:
        message = "Already removed for everyone. Hidden from your deck."
    elif vote["already_voted"]:
        message = f"Already hidden for you. You already voted ({vote['votes']}/{vote['threshold']})."
    else:
        message = f"Hidden from your deck. Vote recorded ({vote['votes']}/{vote['threshold']})."

    track_usage_event(
        "not_transfusion_vote",
        {
            "study_id": study_id,
            "votes": vote["votes"],
            "threshold": vote["threshold"],
            "excluded": vote["excluded"],
            "already_voted": vote["already_voted"],
            "hidden_for_user": True,
        },
    )
    return jsonify({"ok": True, "message": message, "hidden_for_user": True, **vote})


@app.post("/api/usage/event")
def api_usage_event():
    payload = request.get_json(silent=True) or {}
    event_type = str(payload.get("event_type") or "").strip().lower()
    if not event_type:
        return jsonify({"ok": False, "message": "event_type is required."}), 400
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    track_usage_event(event_type, meta)
    return jsonify({"ok": True})


@app.get("/api/usage/summary")
def api_usage_summary():
    unauth = require_user()
    if unauth:
        return jsonify(unauth), 401

    user_id = int(current_user_id() or 0)
    with get_db() as conn:
        total_user_events = int(
            conn.execute("SELECT COUNT(*) AS c FROM usage_events WHERE user_id = ?", (user_id,)).fetchone()["c"]
        )
        total_user_archive_saves = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM usage_events WHERE user_id = ? AND event_type = 'archive_save'",
                (user_id,),
            ).fetchone()["c"]
        )
        total_global_events = int(conn.execute("SELECT COUNT(*) AS c FROM usage_events").fetchone()["c"])
        recent_global_events = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM usage_events WHERE created_at >= ?",
                ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),),
            ).fetchone()["c"]
        )
    return jsonify(
        {
            "ok": True,
            "user": {
                "username": current_user_username(),
                "total_events": total_user_events,
                "archive_saves": total_user_archive_saves,
            },
            "global": {
                "total_events": total_global_events,
                "last_7d_events": recent_global_events,
            },
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
