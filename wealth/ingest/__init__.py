"""Statement, export, image and conversation ingestion into confirmable proposals.

Every path returns the shared envelope (``status``, ``result``, ``missing``,
``warnings``, ``sources``, ``assumptions``).  Status is ``ready_to_confirm``
only when the extraction reconciles; otherwise ``needs_review``,
``needs_extraction`` (host LLM structured extraction), ``needs_input`` or
``rejected``.  Nothing is saved here: :func:`proposal_to_facts` converts a
person-confirmed proposal into ``remember`` payloads.
"""

from .chat import proposal_from_chat
from .connectors import Connector
from .files import ingest_bytes, ingest_file
from .llm import EXTRACTION_SCHEMA, extraction_request, validate_llm_extraction
from .model import IngestProposal, build_proposal, diff_proposals, merge_household, proposal_digest, proposal_to_facts
from .redact import mask_account, redact, redact_text
from .transactions import TYPES as TRANSACTION_TYPES

__all__ = [
    "Connector", "EXTRACTION_SCHEMA", "IngestProposal", "TRANSACTION_TYPES", "build_proposal", "diff_proposals",
    "extraction_request", "ingest_bytes", "ingest_file", "mask_account", "merge_household", "proposal_digest",
    "proposal_from_chat", "proposal_to_facts", "redact", "redact_text", "validate_llm_extraction",
]
