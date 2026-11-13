"""Code shared by the CommitLayer runtime integrations (``commitlayer_temporal``, ``commitlayer_inngest``).

Nothing here imports a workflow SDK; it only shapes what the CommitLayer API returns.
"""

from .verdict import Verdict, poll_until_settled, summarize_verification

__all__ = ["Verdict", "poll_until_settled", "summarize_verification", "__version__"]
__version__ = "0.1.3"  # kept equal to pyproject.toml; tests check it
