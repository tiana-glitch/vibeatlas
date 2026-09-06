"""Multimodal note knowledge-base pipeline.

The package is dependency-free by default so the demo can run locally without
credentials.  A Wenxin-compatible provider can be injected into ``OCRAgent``
when real multimodal extraction is available.
"""

from .agents import (
    ArchiveAgent,
    ClassifierAgent,
    CompressionAgent,
    KnowledgeArchiveAgent,
    OCRAgent,
    SummarizerAgent,
    TopicClassifierAgent,
)
from .models import (
    ArchiveResult,
    LabelResult,
    NoteState,
    OCRResult,
    SummaryResult,
    to_primitive,
)
from .orchestrator import NoteOrchestrator, OCRRevisionConflict
from .prompts import PROMPT_VERSION
from .retrieval import PublicSearchProvider, ResearchAgent, normalize_keywords
from .vault import NotePromotionCommitter, NoteVaultCommitter, NoteVaultError
from .source_lifecycle import SourceLifecycleManager

__all__ = [
    "ArchiveAgent",
    "ArchiveResult",
    "ClassifierAgent",
    "CompressionAgent",
    "KnowledgeArchiveAgent",
    "LabelResult",
    "NoteOrchestrator",
    "OCRRevisionConflict",
    "NoteState",
    "OCRAgent",
    "OCRResult",
    "SummarizerAgent",
    "SummaryResult",
    "TopicClassifierAgent",
    "PROMPT_VERSION",
    "PublicSearchProvider",
    "ResearchAgent",
    "normalize_keywords",
    "NoteVaultCommitter",
    "NotePromotionCommitter",
    "NoteVaultError",
    "SourceLifecycleManager",
    "to_primitive",
]
