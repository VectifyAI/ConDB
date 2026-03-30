"""RAG: retrieve + generate answer."""

from contextdb.llm import LLMProtocol
from contextdb.retriever.base import RetrievalResult


class RAG:
    def __init__(self, llm: LLMProtocol):
        self.llm = llm

    def answer(self, query: str, result: RetrievalResult) -> str:
        if not result.contents:
            return ""

        context = "\n\n".join(c["content"].get("text", "")[:2000] for c in result.contents)
        resp = self.llm.chat([{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}])

        for block in resp.get("content", []):
            if block.get("type") == "text":
                return block["text"]
        return ""
