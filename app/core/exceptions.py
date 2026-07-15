class PipelineError(Exception):
    """Base exception for all pipeline errors."""


class ClassificationError(PipelineError):
    """Classifier node failed to produce a valid classification."""


class RewriteError(PipelineError):
    """Rewriter node failed to produce a rewritten query."""


class RetrievalError(PipelineError):
    """Vector search or reranking failed."""


class GenerationError(PipelineError):
    """Generator node failed to produce an answer."""


class FaithfulnessError(PipelineError):
    """Faithfulness check failed — answer not grounded in chunks."""


class AuthenticationError(Exception):
    """JWT validation or Keycloak authentication failed."""


class AuthorizationError(Exception):
    """User lacks required permissions (e.g. is_admin for upload)."""
