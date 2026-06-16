"""Universal ingestion services."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from defusedxml import defuse_stdlib
except ImportError:  # pragma: no cover - dependency may be absent in old dev envs
    logger.warning("defusedxml is not installed; XML stdlib hardening skipped")
else:
    defuse_stdlib()
