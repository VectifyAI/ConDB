from abc import ABC, abstractmethod


class BaseRetriever(ABC):
    """Shared interface for retrievers (greedy/beam/etc.)."""

    def __init__(self, storage, llm=None):
        self.storage = storage
        self.llm = llm

    @abstractmethod
    def retrieve(self, tree_id, query, **kwargs):
        """Return a RetrievalResult for the given query."""
        raise NotImplementedError
