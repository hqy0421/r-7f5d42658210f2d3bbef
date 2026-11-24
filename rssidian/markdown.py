import os
import re
from typing import Dict, Any, Optional
from datetime import datetime
import logging

from sqlalchemy.orm import Session

from .config import Config

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename to be safe for macOS and other filesystems.
    
    Args:
        filename: Original filename
        
    Returns:
        Sanitized filename
    """
    # Replace characters that are problematic in filenames
    unsafe_chars = r'[<>:"/\\|?*]'
    filename = re.sub(unsafe_chars, '_', filename)
    
    # Limit filename length
    max_length = 200
    if len(filename) > max_length:
        filename = filename[:max_length]
    
    return filename


def write_digest_to_obsidian(
    digest: Dict[str, Any],
    config: Config,
    db_session: Optional[Session] = None,
) -> Optional[str]:
    """
    Write a digest to Obsidian as markdown.

    After the file is actually written, the delivery is recorded in the
    publish index (selected article IDs + article URLs linked to the real
    output file). A digest that is only generated but never written out is
    not recorded as published.

    Args:
        digest: Digest content
        config: Configuration
        db_session: Optional database session for recording the delivery.
            If omitted, a session is opened from the configured database.

    Returns:
        Path to the created markdown file or None if failed
    """
    # Get Obsidian vault path
    vault_path = config.obsidian_vault_path
    
    if not os.path.exists(vault_path):
        logger.error(f"Obsidian vault path does not exist: {vault_path}")
        return None
    
    # Make sure vault path exists
    os.makedirs(vault_path, exist_ok=True)
    
    # Create filename using template
    date_range = digest["date_range"]
    from_date = digest["from_date"]
    to_date = digest["to_date"]
    current_date = datetime.now().strftime("%Y-%m-%d")
    current_datetime = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    
    # Format the filename template with available variables
    filename_template = config.obsidian_filename_template
    formatted_filename = filename_template.format(
        date_range=date_range,
        from_date=from_date,
        to_date=to_date,
        date=current_date,
        datetime=current_datetime
    )
    
    # Sanitize and add .md extension if needed
    filename = sanitize_filename(formatted_filename)
    if not filename.endswith(".md"):
        filename += ".md"
    
    file_path = os.path.join(vault_path, filename)
    
    # Apply template
    template = config.obsidian_template
    
    # Import cost_tracker to get the cost summary
    from .cost_tracker import format_cost_summary
    
    # Get the filename without extension for internal linking
    filename_without_extension = os.path.splitext(filename)[0]
    
    content = template.format(
        date_range=digest["date_range"],
        summary_items=digest["summary_items"],
        feed_stats=digest["feed_stats"],
        aggregated_summary=digest.get("aggregated_summary", "No aggregated summary available."),
        cost_summary=format_cost_summary() or "No cost information available.",
        filename=digest.get("filename", filename_without_extension),
        ingestion_date=current_date,
        from_date=from_date,
        to_date=to_date,
        date=current_date
    )
    
    # Write to file
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"Wrote digest to {file_path}")
    except Exception as e:
        logger.error(f"Failed to write digest to {file_path}: {str(e)}")
        return None

    # Record the delivery only now that the file really exists. A recording
    # failure must not turn a successful write into a failure.
    try:
        from .publish import record_digest_delivery

        own_session = db_session is None
        if own_session:
            from .models import get_db_session
            db_session = get_db_session(config.db_path)
        try:
            record_digest_delivery(digest, file_path, db_session)
        finally:
            if own_session:
                db_session.close()
    except Exception as e:
        logger.error(f"Failed to record digest delivery for {file_path}: {str(e)}")

    return file_path