#!/usr/bin/env python
"""
Test the publish index: digest deliveries are recorded only when the
markdown file is actually written, are linked to the selected article IDs
and the real output file, and stay locatable after subscription URL changes.

Run with: python testing/test_publish_index.py
"""

import os
import sys
import tempfile

# Allow running from the repo root without installing
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rssidian.config import Config
from rssidian.markdown import write_digest_to_obsidian
from rssidian.models import Article, Digest, DigestArticle, Feed, init_db
from rssidian.publish import get_article_deliveries, get_delivery_counts

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

failures = []


def check(name, condition):
    print(f"  [{PASS if condition else FAIL}] {name}")
    if not condition:
        failures.append(name)


def make_config(vault_path):
    config = Config(config_path="/nonexistent/config.toml")
    config.config = {
        "obsidian": {
            "vault_path": vault_path,
            "filename_template": "Digest {from_date} to {to_date}",
        }
    }
    return config


def make_digest(articles, from_date, to_date):
    return {
        "date_range": f"Feed Overview from {from_date} through {to_date}",
        "from_date": from_date,
        "to_date": to_date,
        "summary_items": "items",
        "feed_stats": "stats",
        "articles": articles,
        "filename": f"Digest {from_date} to {to_date}",
    }


def main():
    print("Testing publish index (digest delivery tracking)")
    print("-" * 60)

    tmpdir = tempfile.mkdtemp(prefix="rssidian_publish_test_")
    db_path = os.path.join(tmpdir, "test.db")
    vault_path = os.path.join(tmpdir, "vault")
    os.makedirs(vault_path)

    db = init_db(db_path)

    # A feed and three articles; articles 1 and 3 share the same 正文链接
    feed = Feed(title="Example Feed", url="https://example.com/feed.xml")
    db.add(feed)
    db.commit()

    article1 = Article(feed_id=feed.id, title="Story One", url="https://example.com/story-1",
                       guid="guid-1", processed=True)
    article2 = Article(feed_id=feed.id, title="Story Two", url="https://example.com/story-2",
                       guid="guid-2", processed=True)
    article3 = Article(feed_id=feed.id, title="Story One (mirror)", url="https://example.com/story-1",
                       guid="guid-3", processed=True)
    db.add_all([article1, article2, article3])
    db.commit()

    config = make_config(vault_path)

    print("\n1. Generating a digest without writing it records nothing")
    digest1 = make_digest([article1, article2, article3], "2026-11-18", "2026-11-25")
    check("no digest rows before any write", db.query(Digest).count() == 0)
    check("no delivery rows before any write", db.query(DigestArticle).count() == 0)

    print("\n2. Writing the digest records the delivery linked to article IDs and the real file")
    file_path1 = write_digest_to_obsidian(digest1, config, db_session=db)
    check("write returned a file path", file_path1 is not None)
    check("file really exists on disk", file_path1 is not None and os.path.exists(file_path1))

    digest_rows = db.query(Digest).all()
    check("exactly one digest recorded", len(digest_rows) == 1)
    if digest_rows:
        record = digest_rows[0]
        check("record stores the real output file path", record.file_path == file_path1)
        check("record stores the note filename", record.filename == os.path.basename(file_path1))
        check("record stores the coverage range",
              record.from_date == "2026-11-18" and record.to_date == "2026-11-25")

    links = db.query(DigestArticle).all()
    check("all three selected article IDs are linked",
          sorted(link.article_id for link in links) == [article1.id, article2.id, article3.id])
    check("article URLs (正文链接) are snapshotted",
          sorted(link.article_url for link in links) == [
              "https://example.com/story-1", "https://example.com/story-1", "https://example.com/story-2"])

    print("\n3. A second written digest increments the per-link delivery count")
    digest2 = make_digest([article1, article2], "2026-11-19", "2026-11-25")
    file_path2 = write_digest_to_obsidian(digest2, config, db_session=db)
    check("second write produced a different file",
          file_path2 is not None and file_path2 != file_path1 and os.path.exists(file_path2))

    counts = {row["article_url"]: row for row in get_delivery_counts(db)}
    story1 = counts.get("https://example.com/story-1", {})
    story2 = counts.get("https://example.com/story-2", {})
    check("story-1 link delivered twice", story1.get("delivery_count") == 2)
    check("story-2 link delivered twice", story2.get("delivery_count") == 2)
    check("duplicate URL within one digest counts once per digest",
          story1.get("delivery_count") == 2)  # would be 3 if the shared URL double-counted
    check("delivery count is per link, keyed by stable article IDs",
          story1.get("article_ids") == sorted([article1.id, article3.id]))

    print("\n4. Subscription URL change does not break locating the original digest")
    feed.url = "https://example.com/completely-different-feed.xml"
    db.commit()

    by_url = get_article_deliveries(db, url="https://example.com/story-1")
    check("lookup by article URL still finds both digests", len(by_url) == 2)
    check("located digests point at the real output files",
          sorted(e["file_path"] for e in by_url) == sorted([file_path1, file_path2]))
    first_digest_entry = next((e for e in by_url if e["file_path"] == file_path1), None)
    check("one digest entry per digest even when the link appeared via two article rows",
          first_digest_entry is not None
          and sorted(first_digest_entry["article_ids"]) == sorted([article1.id, article3.id]))

    by_id = get_article_deliveries(db, article_id=article2.id)
    check("lookup by article ID still finds both digests", len(by_id) == 2)
    check("article-ID lookup resolves the same output files",
          sorted(e["file_path"] for e in by_id) == sorted([file_path1, file_path2]))

    print("\n5. A digest that fails to write is not counted as published")
    before_digests = db.query(Digest).count()
    before_links = db.query(DigestArticle).count()

    missing_vault = os.path.join(tmpdir, "does-not-exist")
    bad_config = make_config(missing_vault)
    digest3 = make_digest([article1, article2], "2026-11-20", "2026-11-25")
    result = write_digest_to_obsidian(digest3, bad_config, db_session=db)

    check("failed write returns None", result is None)
    check("no new digest recorded", db.query(Digest).count() == before_digests)
    check("no new deliveries recorded", db.query(DigestArticle).count() == before_links)
    counts_after = {row["article_url"]: row for row in get_delivery_counts(db)}
    check("delivery counts unchanged by the unwritten digest",
          counts_after.get("https://example.com/story-1", {}).get("delivery_count") == 2)

    print("\n6. Detached article objects (expired session) are still recorded")
    article1_id = article1.id
    db.close()  # articles become detached with expired attributes

    db2 = init_db(db_path)
    digest4 = make_digest([article1], "2026-11-21", "2026-11-25")
    file_path4 = write_digest_to_obsidian(digest4, config, db_session=db2)
    check("write with detached articles succeeds",
          file_path4 is not None and os.path.exists(file_path4))
    detached_links = get_article_deliveries(db2, article_id=article1_id)
    check("delivery recorded for detached article", len(detached_links) == 3)
    check("detached article resolved to its stable URL",
          all(e["article_url"] == "https://example.com/story-1" for e in detached_links))
    db2.close()

    print("-" * 60)
    if failures:
        print(f"\n{len(failures)} check(s) failed:")
        for name in failures:
            print(f"  - {name}")
        sys.exit(1)
    print("\nAll checks passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
