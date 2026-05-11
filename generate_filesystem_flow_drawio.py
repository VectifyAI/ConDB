#!/usr/bin/env python3
"""Generate a draw.io diagram for ConDB filesystem retrieval.

Usage:
    python generate_filesystem_flow_drawio.py

Output:
    filesystem_retrieval_flowchart.drawio
"""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom


OUT = Path("filesystem_retrieval_flowchart.drawio")


BASE_VERTEX = (
    "rounded=1;whiteSpace=wrap;html=1;arcSize=10;"
    "strokeColor=#dbe4ee;fillColor=#ffffff;fontColor=#0f172a;"
    "fontFamily=Arial;fontSize=14;spacing=12;shadow=1;"
)
EDGE_STYLE = (
    "edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;jettySize=auto;"
    "html=1;strokeColor=#64748b;strokeWidth=2;endArrow=block;endFill=1;"
)


def label(title: str, body: str = "") -> str:
    title_html = f"<b>{html.escape(title)}</b>"
    if not body:
        return title_html
    return (
        f"{title_html}<br>"
        f"<font style=\"font-size: 12px\" color=\"#475569\">{html.escape(body)}</font>"
    )


def add_cell(root: ET.Element, cell_id: str, value: str = "", **attrs) -> ET.Element:
    cell = ET.SubElement(root, "mxCell", {"id": cell_id, "value": value, **attrs})
    return cell


def add_vertex(
    root: ET.Element,
    cell_id: str,
    value: str,
    x: int,
    y: int,
    w: int,
    h: int,
    style: str = BASE_VERTEX,
) -> None:
    cell = add_cell(root, cell_id, value, style=style, vertex="1", parent="1")
    ET.SubElement(
        cell,
        "mxGeometry",
        {"x": str(x), "y": str(y), "width": str(w), "height": str(h), "as": "geometry"},
    )


def add_edge(
    root: ET.Element,
    cell_id: str,
    source: str,
    target: str,
    value: str = "",
    style: str = EDGE_STYLE,
) -> None:
    cell = add_cell(
        root,
        cell_id,
        html.escape(value),
        style=style,
        edge="1",
        parent="1",
        source=source,
        target=target,
    )
    ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})


def build() -> ET.ElementTree:
    mxfile = ET.Element(
        "mxfile",
        {
            "host": "app.diagrams.net",
            "modified": "2026-04-27T00:00:00.000Z",
            "agent": "ConDB flow generator",
            "version": "24.7.8",
        },
    )
    diagram = ET.SubElement(mxfile, "diagram", {"name": "Filesystem Retrieval"})
    model = ET.SubElement(
        diagram,
        "mxGraphModel",
        {
            "dx": "1600",
            "dy": "1000",
            "grid": "1",
            "gridSize": "10",
            "guides": "1",
            "tooltips": "1",
            "connect": "1",
            "arrows": "1",
            "fold": "1",
            "page": "1",
            "pageScale": "1",
            "pageWidth": "1800",
            "pageHeight": "1450",
            "math": "0",
            "shadow": "0",
        },
    )
    root = ET.SubElement(model, "root")
    add_cell(root, "0")
    add_cell(root, "1", parent="0")

    # Routing: keep the top compact so the algorithm split is the focus.
    add_vertex(
        root,
        "query",
        label("ConDB.query()", "filesystem tree_id + user question"),
        725,
        35,
        350,
        70,
    )
    add_vertex(
        root,
        "strategy",
        label("Algorithm Strategy", "auto: small tree -> Beam, large tree -> Block"),
        690,
        145,
        420,
        82,
        BASE_VERTEX + "strokeColor=#cbd5e1;fillColor=#ffffff;fontSize=16;",
    )
    add_edge(root, "e_query_strategy", "query", "strategy")

    # Lanes
    add_vertex(
        root,
        "beam_lane",
        "",
        55,
        275,
        790,
        1040,
        "rounded=1;whiteSpace=wrap;html=1;arcSize=8;strokeColor=#d9e2ec;fillColor=#ffffff;shadow=1;",
    )
    add_vertex(
        root,
        "block_lane",
        "",
        895,
        275,
        850,
        1040,
        "rounded=1;whiteSpace=wrap;html=1;arcSize=8;strokeColor=#d9e2ec;fillColor=#ffffff;shadow=1;",
    )
    add_vertex(
        root,
        "beam_header",
        "<b>Filesystem BeamRetriever</b><br>"
        "<font style=\"font-size: 12px\" color=\"#64748b\">small tree, layer-by-layer navigation</font>",
        55,
        275,
        790,
        65,
        "rounded=1;whiteSpace=wrap;html=1;arcSize=12;strokeColor=none;fillColor=#eff6ff;"
        "fontFamily=Arial;fontSize=22;fontColor=#0f172a;",
    )
    add_vertex(
        root,
        "block_header",
        "<b>Filesystem BlockRetriever</b><br>"
        "<font style=\"font-size: 12px\" color=\"#64748b\">large tree, token-bounded blocks</font>",
        895,
        275,
        850,
        65,
        "rounded=1;whiteSpace=wrap;html=1;arcSize=12;strokeColor=none;fillColor=#f0fdfa;"
        "fontFamily=Arial;fontSize=22;fontColor=#0f172a;",
    )
    add_edge(root, "e_strategy_beam", "strategy", "beam_header", "node_count <= 50")
    add_edge(root, "e_strategy_block", "strategy", "block_header", "node_count > 50")

    # Beam path
    beam_nodes = [
        ("beam_1", "Initialize frontier", "F0 = {root}. Frontier is the directory set to expand.", 120, 370),
        ("beam_2", "Build candidate set", "C_t = direct children of each node in F_t. Fields: path + type; metadata reserved, disabled.", 120, 475),
        ("beam_3", "LLM ranks candidates", "rank(C_t) -> ranked_ids + done.", 120, 580),
        ("beam_4", "Split ranked ids", "files(ranked_ids) -> top_candidate_ids; dirs(ranked_ids) -> next frontier.", 120, 685),
    ]
    for cell_id, title, body, x, y in beam_nodes:
        add_vertex(root, cell_id, label(title, body), x, y, 660, 70)
    add_vertex(
        root,
        "beam_decision",
        label("File or directory?"),
        340,
        805,
        220,
        100,
        "rhombus;whiteSpace=wrap;html=1;strokeColor=#cbd5e1;fillColor=#ffffff;"
        "fontColor=#0f172a;fontFamily=Arial;fontSize=16;shadow=1;",
    )
    add_vertex(root, "beam_file", label("File", "Add to top_candidate_ids A."), 105, 930, 280, 66)
    add_vertex(root, "beam_dir", label("Directory", "F_{t+1} = returned directories."), 520, 930, 280, 66)
    add_vertex(
        root,
        "beam_stop",
        "Repeat while F has expandable directories and done is false.<br><b>Final: return top k top_candidate_ids + contents.</b>",
        210,
        1070,
        490,
        70,
        "rounded=1;whiteSpace=wrap;html=1;arcSize=8;strokeColor=#dbe4ee;fillColor=#f8fafc;"
        "fontFamily=Arial;fontSize=13;fontColor=#475569;",
    )
    add_edge(root, "e_b1_b2", "beam_1", "beam_2")
    add_edge(root, "e_b2_b3", "beam_2", "beam_3")
    add_edge(root, "e_b3_b4", "beam_3", "beam_4")
    add_edge(root, "e_b4_dec", "beam_4", "beam_decision")
    add_edge(root, "e_dec_file", "beam_decision", "beam_file", "file")
    add_edge(root, "e_dec_dir", "beam_decision", "beam_dir", "directory")
    add_edge(root, "e_file_stop", "beam_file", "beam_stop")
    add_edge(root, "e_dir_stop", "beam_dir", "beam_stop")
    add_edge(
        root,
        "e_dir_loop",
        "beam_dir",
        "beam_2",
        "recursive next level",
        EDGE_STYLE + "dashed=1;",
    )

    # Block path
    block_nodes = [
        ("block_1", "Initialize frontier", "F0 = {root}; collapse virtual single-child directories.", 960, 360),
        ("block_2", "Create block specs from frontier", "turn 0: top block. Later turns: subtree blocks from each frontier directory.", 960, 450),
        ("block_3", "Candidate universe per block", "For each block i: C_i = block.node_ids. Fields: path + type; metadata reserved, disabled.", 960, 540),
        ("block_4", "LLM ranks each block", "rank(C_i) -> ranked_i + done_i. These are returned candidates.", 960, 630),
    ]
    for cell_id, title, body, x, y in block_nodes:
        add_vertex(root, cell_id, label(title, body), x, y, 710, 68)
    add_vertex(
        root,
        "block_decision",
        label("Multiple blocks?"),
        1200,
        730,
        250,
        100,
        "rhombus;whiteSpace=wrap;html=1;strokeColor=#cbd5e1;fillColor=#ffffff;"
        "fontColor=#0f172a;fontFamily=Arial;fontSize=16;shadow=1;",
    )
    add_vertex(root, "block_single", label("Single block", "merged_ids = ranked_0."), 935, 855, 300, 66)
    add_vertex(
        root,
        "block_parallel",
        label("Parallel blocks", "Merge all ranked_i into merged_ids."),
        1410,
        855,
        310,
        66,
    )
    add_vertex(
        root,
        "block_global",
        label("Deterministic merge", "merged_ids = unique(L1 + L2 + ...); no global ranking after merge."),
        1130,
        955,
        500,
        74,
    )
    add_vertex(
        root,
        "block_update",
        label("Split merged ids", "append files(M) until |top_candidate_ids| = k; dirs(M) -> F_{t+1}."),
        1040,
        1070,
        680,
        74,
    )
    add_vertex(
        root,
        "block_continue",
        label("Continue?"),
        1220,
        1175,
        250,
        92,
        "rhombus;whiteSpace=wrap;html=1;strokeColor=#cbd5e1;fillColor=#ffffff;"
        "fontColor=#0f172a;fontFamily=Arial;fontSize=16;shadow=1;",
    )
    add_vertex(
        root,
        "block_return",
        "Stop when done, no expandable frontier, or max_turns.<br><b>Final: return top k top_candidate_ids + contents.</b>",
        1045,
        1280,
        650,
        70,
        "rounded=1;whiteSpace=wrap;html=1;arcSize=8;strokeColor=#dbe4ee;fillColor=#f8fafc;"
        "fontFamily=Arial;fontSize=13;fontColor=#475569;",
    )
    add_edge(root, "e_bl1_bl2", "block_1", "block_2")
    add_edge(root, "e_bl2_bl3", "block_2", "block_3")
    add_edge(root, "e_bl3_bl4", "block_3", "block_4")
    add_edge(root, "e_bl4_dec", "block_4", "block_decision")
    add_edge(root, "e_dec_single", "block_decision", "block_single", "no")
    add_edge(root, "e_dec_parallel", "block_decision", "block_parallel", "yes")
    add_edge(root, "e_single_global", "block_single", "block_global", "already merged")
    add_edge(root, "e_parallel_global", "block_parallel", "block_global", "merged candidates")
    add_edge(root, "e_global_update", "block_global", "block_update")
    add_edge(root, "e_update_continue", "block_update", "block_continue")
    add_edge(
        root,
        "e_continue_loop",
        "block_continue",
        "block_2",
        "yes: recurse into F_{t+1}",
        EDGE_STYLE + "dashed=1;",
    )
    add_edge(root, "e_continue_return", "block_continue", "block_return", "no")

    # Footer comparison
    add_vertex(
        root,
        "summary",
        "<b>candidate_set</b>: shown to LLM. &nbsp;&nbsp; | &nbsp;&nbsp; "
        "<b>ranked_ids</b>: block-local LLM output. &nbsp;&nbsp; | &nbsp;&nbsp; "
        "<b>pick_limit</b>: per-call output cap. &nbsp;&nbsp; | &nbsp;&nbsp; "
        "<b>frontier</b>: directories to expand. &nbsp;&nbsp; | &nbsp;&nbsp; "
        "<b>k</b>: final result limit.",
        260,
        1370,
        1260,
        48,
        "rounded=1;whiteSpace=wrap;html=1;arcSize=6;strokeColor=#dbe4ee;fillColor=#ffffff;"
        "fontFamily=Arial;fontSize=14;fontColor=#334155;",
    )

    return ET.ElementTree(mxfile)


def write_pretty(tree: ET.ElementTree, path: Path) -> None:
    raw = ET.tostring(tree.getroot(), encoding="utf-8")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")
    path.write_bytes(pretty)


def main() -> None:
    write_pretty(build(), OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
