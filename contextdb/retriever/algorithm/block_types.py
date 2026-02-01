"""Data structures for Block-level Beam Search."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class BlockType(Enum):
    """Type of block cutting."""

    VERTICAL = "vertical"
    HORIZONTAL = "horizontal"


@dataclass
class Block:
    """A cacheable unit containing nodes within token budget."""

    block_id: str
    block_type: BlockType
    tree_id: str
    depth_start: int
    depth_end: int
    node_ids: list[str] = field(default_factory=list)
    horizontal_group_id: Optional[str] = None
    horizontal_index: int = 0
    total_tokens: int = 0
    max_tokens: int = 0
    cached_content: Optional[str] = None
    content_hash: Optional[str] = None


@dataclass
class BlockResult:
    """Result from processing a single Block."""

    block_id: str
    ranked_node_ids: list[str]
    selected_node_ids: list[str]
    done: bool
    usage: Optional[dict[str, int]] = None


@dataclass
class HorizontalBlockGroup:
    """A group of horizontal blocks from same-level nodes."""

    group_id: str
    tree_id: str
    depth: int
    blocks: list[Block] = field(default_factory=list)
    parent_node_ids: list[str] = field(default_factory=list)


@dataclass
class BlockTreePlan:
    """Complete block cutting plan for a tree."""

    tree_id: str
    max_tokens_per_block: int
    blocks: list[Block] = field(default_factory=list)
    horizontal_groups: list[HorizontalBlockGroup] = field(default_factory=list)
    block_map: dict[str, Block] = field(default_factory=dict)
    total_tokens: int = 0


@dataclass
class BlockRetrievalResult:
    """Extended retrieval result with block-level tracing."""

    nodes: list[str]
    contents: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    turns: int
    blocks_processed: int = 0
    horizontal_groups_processed: int = 0
    total_llm_calls: int = 0
    cache_hits: int = 0
    nodes_pruned: int = 0
    block_traces: list[dict[str, Any]] = field(default_factory=list)
