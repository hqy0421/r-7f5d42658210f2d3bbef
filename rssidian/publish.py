"""Delivery tracking for digests published to Obsidian.

A "delivery" is recorded only when a digest markdown file has actually been
written to the vault. A digest that was generated but never written out is
not a publication and must not appear here.

The index links each published digest (its real output file) to the selected
article IDs and their article URLs (正文链接). It is keyed by those stable
article identifiers — never by the subscription feed URL — so the original
digest remains locatable after a subscription's URL changes.
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from .models import Article, Base, Digest, DigestArticle

# Configure logging
logging.basicConfig(
    level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _resolve_article_link(article: Any, db_session: Session) -> Tuple[Optional[int], Optional[str]]:
    """
    Read (id, url) from an article object, re-resolving through the session
    if the instance is detached with expired attributes.

    Args:
        article: Article-like object from the digest
        db_session: Database session

    Returns:
        (article_id, article_url) or (None, None) if it cannot be resolved
    """
    try:
        return article.id, article.url
    except Exception:
        # Detached instance with expired attributes: get the primary key
        # without triggering a refresh, then load a bound copy.
        identity = sa_inspect(article).identity
        if not identity:
            return None, None
        bound = db_session.get(Article, identity[0])
        if bound is None:
            return None, None
        return bound.id, bound.url


def record_digest_delivery(digest: Dict[str, Any], file_path: str, db_session: Session) -> Digest:
    """
    Record that a digest was actually written to the vault (a real delivery).

    Only call this after the digest markdown file has been successfully
    written to disk.

    Args:
        digest: Digest content dict (uses "articles", "from_date", "to_date")
        file_path: Real path of the markdown file that was written
        db_session: Database session

    Returns:
        The created Digest record
    """
    # Make sure the index tables exist (idempotent, cheap)
    Base.metadata.create_all(db_session.bind)

    record = Digest(
        filename=os.path.basename(file_path),
        file_path=file_path,
        from_date=digest.get("from_date"),
        to_date=digest.get("to_date"),
        created_at=datetime.utcnow(),
    )

    # Link every selected article ID and its stable article URL to this file.
    # Dedupe by article ID in case the same article object appears twice.
    seen_article_ids = set()
    for article in digest.get("articles") or []:
        article_id, article_url = _resolve_article_link(article, db_session)
        if article_id is None or not article_url or article_id in seen_article_ids:
            continue
        seen_article_ids.add(article_id)
        record.deliveries.append(DigestArticle(article_id=article_id, article_url=article_url))

    delivery_count = len(record.deliveries)

    db_session.add(record)
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise

    logger.info(f"Recorded digest delivery: {record.filename} ({delivery_count} article links)")
    return record


def get_delivery_counts(db_session: Session) -> List[Dict[str, Any]]:
    """
    Aggregate how many times each article link was actually delivered.

    Counts distinct published digests per article URL, so a link that appears
    twice inside a single digest still counts as one delivery.

    Args:
        db_session: Database session

    Returns:
        One row per article URL ever delivered, sorted by delivery count desc
    """
    rows = (
        db_session.query(DigestArticle, Digest, Article)
        .join(Digest, Digest.id == DigestArticle.digest_id)
        .outerjoin(Article, Article.id == DigestArticle.article_id)
        .all()
    )

    by_url: Dict[str, Dict[str, Any]] = {}
    for digest_article, digest, article in rows:
        entry = by_url.setdefault(digest_article.article_url, {
            "article_url": digest_article.article_url,
            "title": None,
            "article_ids": set(),
            "digest_ids": set(),
            "last_delivered_at": None,
        })
        entry["article_ids"].add(digest_article.article_id)
        entry["digest_ids"].add(digest_article.digest_id)
        if article is not None and entry["title"] is None:
            entry["title"] = article.title
        if digest.created_at is not None and (
            entry["last_delivered_at"] is None or digest.created_at > entry["last_delivered_at"]
        ):
            entry["last_delivered_at"] = digest.created_at

    results = []
    for entry in by_url.values():
        results.append({
            "article_url": entry["article_url"],
            "title": entry["title"],
            "article_ids": sorted(entry["article_ids"]),
            "delivery_count": len(entry["digest_ids"]),
            "last_delivered_at": entry["last_delivered_at"],
        })

    results.sort(key=lambda r: (-r["delivery_count"], r["article_url"]))
    return results


def get_article_deliveries(
    db_session: Session,
    article_id: Optional[int] = None,
    url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List every published digest that delivered a given article.

    Lookup works by article ID and/or article URL — both are stable across
    subscription (feed) URL changes, so the original digest stays locatable.
    Returns one entry per digest: if a link was delivered via several article
    rows in the same digest, that digest still appears only once.

    Args:
        db_session: Database session
        article_id: Optional article ID to look up
        url: Optional article URL (正文链接) to look up

    Returns:
        Matching digests, most recent first
    """
    query = (
        db_session.query(DigestArticle, Digest)
        .join(Digest, Digest.id == DigestArticle.digest_id)
    )
    if article_id is not None:
        query = query.filter(DigestArticle.article_id == article_id)
    if url:
        query = query.filter(DigestArticle.article_url == url)

    by_digest: Dict[int, Dict[str, Any]] = {}
    for digest_article, digest in query.order_by(Digest.created_at.desc()).all():
        entry = by_digest.setdefault(digest.id, {
            "digest_id": digest.id,
            "filename": digest.filename,
            "file_path": digest.file_path,
            "from_date": digest.from_date,
            "to_date": digest.to_date,
            "delivered_at": digest.created_at,
            "article_ids": [],
            "article_url": digest_article.article_url,
        })
        entry["article_ids"].append(digest_article.article_id)
    return list(by_digest.values())
