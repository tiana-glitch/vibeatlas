"""Shared coordination primitives for vault-changing transactions.

The web server handles document ingest while :mod:`note_knowledge.vault`
handles NoteFlow and inbox promotion commits.  They must coordinate through
one lock so a concurrent request cannot interleave index/log replacements.
The lock is intentionally process-local; hash checks remain the protection
for callers that preview and commit across requests or processes.
"""

from __future__ import annotations

import threading


VAULT_TRANSACTION_LOCK = threading.RLock()


__all__ = ["VAULT_TRANSACTION_LOCK"]
