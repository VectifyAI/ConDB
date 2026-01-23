import json
import os
import sys
import time
from pathlib import Path
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextdb import ContextTree
from contextdb.config import Config
from contextdb.retriever import BeamRetriever
from contextdb.metrics import StatisticsRecorder, LLMWithStats
from contextdb.logger import get_logger


EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
QUESTIONS_FILE = Path(__file__).parent / "rag_questions.json"
MAX_EXAMPLES = 2

log = get_logger(__name__)


def _load_examples():
    """Load all example JSON files under examples/."""
    files = sorted(EXAMPLES_DIR.glob("*.json"))
    if not files:
        raise FileNotFoundError("No example JSON files found in examples/")
    data = []
    for f in files:
        with f.open() as fh:
            data.append((f.name, json.load(fh)))
    return data


def _load_questions():
    """Load RAG questions per example file."""
    if not QUESTIONS_FILE.exists():
        raise FileNotFoundError(f"Questions file not found: {QUESTIONS_FILE}")
    with QUESTIONS_FILE.open() as fh:
        questions = json.load(fh)
    if not isinstance(questions, dict):
        raise ValueError("Questions file must be a JSON object {filename: [questions...]}")
    return questions


def _node_brief(storage, tree_id: str, node_id: str):
    """Return a compact summary for a node: title/summary/text snippet."""
    entity = storage.get_entity(tree_id, node_id)
    if entity:
        payload = json.loads(entity.payload_json)
        title = payload.get("title") or ""
        summary = payload.get("summary") or ""
        text = payload.get("text") or payload.get("content") or ""
    else:
        node = storage.get_node(tree_id, node_id)
        attrs = json.loads(node.attrs_json) if node and node.attrs_json else {}
        title = attrs.get("title") or ""
        summary = attrs.get("summary") or ""
        text = ""
    return {
        "node_id": node_id,
        "title": title,
        "summary": summary,
        "text": text[:200] if text else ""
    }

def _path_titles(storage, tree_id: str, node_id: str):
    """Build a human-readable title path from root to this node."""
    titles = []
    node = storage.get_node(tree_id, node_id)
    while node:
        attrs = json.loads(node.attrs_json) if node.attrs_json else {}
        title = (attrs.get("title") or "").strip()
        if title:
            titles.append(title)
        if not node.parent_id:
            break
        node = storage.get_node(tree_id, node.parent_id)
    return " > ".join(reversed(titles))


def _judge_retrieval(llm, query: str, greedy_nodes, beam_nodes):
    """Ask the LLM to judge which retrieval set better fits the query."""
    tools = [
        {
            "name": "judge",
            "description": "Choose which retrieval is more relevant",
            "input_schema": {
                "type": "object",
                "properties": {
                    "choice": {"type": "string", "enum": ["greedy", "beam", "tie"]}
                },
                "required": ["choice"]
            }
        }
    ]
    prompt = ["You are judging retrieval relevance.", f"Query: {query}", ""]
    prompt.append("Greedy retrieval:")
    for n in greedy_nodes:
        prompt.append(f"- {n['title']} | {n['summary']} | {n['text']}")
    prompt.append("")
    prompt.append("Beam retrieval:")
    for n in beam_nodes:
        prompt.append(f"- {n['title']} | {n['summary']} | {n['text']}")
    prompt.append("")
    prompt.append("Return ONE tool call judge({choice}).")
    resp = llm.chat([{"role": "user", "content": "\n".join(prompt)}], tools=tools)
    for block in resp.get("content", []):
        if block.get("type") != "tool_use":
            continue
        if block.get("name") != "judge":
            continue
        choice = block.get("input", {}).get("choice")
        if choice in ("greedy", "beam", "tie"):
            return choice
    raise ValueError("LLM judge did not return a judge tool call")

def _node_label(storage, tree_id: str, node_id: str) -> str:
    """Human-friendly node label: slot + title + short id."""
    node = storage.get_node(tree_id, node_id)
    if not node:
        return str(node_id)[:8]
    attrs = json.loads(node.attrs_json) if node.attrs_json else {}
    title = (attrs.get("title") or "").strip()
    slot = node.slot or ""
    return f"{slot} | {title} | {str(node_id)[:8]}".strip(" |")

def _log_retrieval(label: str, storage, tree_id: str, nodes):
    """Log what each retriever actually returned (title/summary/text/path)."""
    log.info("[%s results] count=%s", label, len(nodes))
    for nid in nodes:
        info = _node_brief(storage, tree_id, nid)
        path = _path_titles(storage, tree_id, nid)
        log.info("  id=%s", str(nid)[:8])
        if path:
            log.info("  path=%s", path)
        if info["title"]:
            log.info("  title=%s", info["title"])
        if info["summary"]:
            log.info("  summary=%s", info["summary"])
        if info["text"]:
            log.info("  text=%s", info["text"])


def _print_trace(label: str, result, storage=None, tree_id: str = ""):
    """Print a compact trace for human inspection (compressed for repeats)."""
    log.info("[%s trace] turns=%s", label, result.turns)

    # Greedy-style trace: compress repeated identical actions.
    if result.trace and "action" in result.trace[0]:
        last = None
        count = 0
        for t in result.trace:
            key = (t.get("action"), t.get("node_id"))
            if key == last:
                count += 1
                continue
            if last is not None:
                action, node_id = last
                label_text = _node_label(storage, tree_id, node_id) if storage else str(node_id)[:8]
                suffix = f" x{count}" if count > 1 else ""
                log.info("  %s %s%s", action, label_text, suffix)
            last = key
            count = 1

        # flush
        if last is not None:
            action, node_id = last
            label_text = _node_label(storage, tree_id, node_id) if storage else str(node_id)[:8]
            suffix = f" x{count}" if count > 1 else ""
            log.info("  %s %s%s", action, label_text, suffix)
        return

    # Beam-style trace: per turn summary.
    for t in result.trace:
        log.info("  turn=%s candidates=%s kept=%s done=%s",
                 t.get("turn"), t.get("candidates"), t.get("kept"), t.get("done"))


def compare_all():
    """Run greedy vs beam retrieval on all example JSONs and report mean latency + win counts."""
    recorder = StatisticsRecorder()
    llm = LLMWithStats(Config.get_llm_client(), recorder)
    examples = _load_examples()[:MAX_EXAMPLES]
    questions = _load_questions()

    greedy_times = []
    beam_times = []
    wins = {"greedy": 0, "beam": 0, "tie": 0}

    for name, data in examples:
        if name not in questions or not questions[name]:
            raise ValueError(f"No questions provided for {name}")

        ct = ContextTree(":memory:", llm=llm)
        try:
            tree_id = ct.index_pageindex(data)
            for query in questions[name]:
                log.info("Task: doc=%s query=%s", name, query)
                t0 = time.perf_counter()
                greedy = BeamRetriever(ct.storage, llm).retrieve(tree_id, query, beam_size=1)
                t1 = time.perf_counter()

                beam = BeamRetriever(ct.storage, llm).retrieve(tree_id, query, beam_size=2)
                t2 = time.perf_counter()

                _log_retrieval("beam_k1", ct.storage, tree_id, greedy.nodes[:3])
                _log_retrieval("beam_k2", ct.storage, tree_id, beam.nodes[:3])
                _print_trace("greedy", greedy, ct.storage, tree_id)
                _print_trace("beam", beam, ct.storage, tree_id)

                greedy_times.append(t1 - t0)
                beam_times.append(t2 - t1)

                g_nodes = [_node_brief(ct.storage, tree_id, nid) for nid in greedy.nodes[:3]]
                b_nodes = [_node_brief(ct.storage, tree_id, nid) for nid in beam.nodes[:3]]
                decision = _judge_retrieval(llm, query, g_nodes, b_nodes)
                wins[decision] += 1
                log.info("%s: %s", name, decision)
        finally:
            ct.close()

    # Print formatted statistics
    print("\n" + recorder.format("Retrieval Statistics"))
    print(f"*** Comparison Results ***")
    print(f"    Retrieval latency (greedy): {mean(greedy_times) * 1000:.0f}ms")
    print(f"    Retrieval latency (beam): {mean(beam_times) * 1000:.0f}ms")
    print(f"    LLM judge: greedy={wins['greedy']} beam={wins['beam']} tie={wins['tie']}")


if __name__ == "__main__":
    compare_all()
