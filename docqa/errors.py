"""Application error taxonomy.

Global errors stop a batch before replacing its output. Item-level errors are
handled by the pipeline for one question without hiding programming defects.
"""


class DocQAError(Exception):
    """Base class for expected application errors."""


class ConfigurationError(DocQAError):
    """The runtime configuration is missing or unsafe."""


class InputBatchError(DocQAError):
    """The input batch cannot satisfy the one-output-per-ID contract."""


class DocumentError(DocQAError):
    """The selected document set cannot be parsed reliably."""


class ContractBlocked(DocQAError):
    """The document contract has unresolved blocking validation issues."""


class TransportExhausted(DocQAError):
    """A model request exhausted its bounded transport attempts."""


class InvalidModelReply(DocQAError):
    """A model response is incomplete or cannot satisfy its schema."""


class AnalysisFailed(DocQAError):
    """One question remains invalid after its semantic repair budget."""
