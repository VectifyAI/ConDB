# Questions for MongoDB

## Context

We store documents as large hierarchical trees in MongoDB 7.0 Community Edition:
one document per tree node, with `node_id`, `title`, `summary`, a materialized path
string, `parent_id`, `depth`, and a large free-text field on the leaves. Trees run
to a few million nodes and fit comfortably in RAM. The read we care about pulls a
whole subtree's structure: it matches up to tens of thousands of nodes by a
path-range scan and returns only the small fields (`node_id`, `title`, `summary`)
to render a tree view. Node text is read afterward, separately, only for the few
nodes that get selected. The tree is effectively write-once after ingest.

We're doing the tuning ourselves; below are the places where the docs run out and
we'd value your read. We're not asking about things the manual already covers (the
Subset pattern, lean covering indexes, the tree-modeling patterns, prefix
compression, or the lack of columnar/covered-scan features on self-managed).

## Questions

**1.** On 7.0 (classic engine, SBE off by default from 7.0.17), when a query finds
many documents by index but projects only a few small fields, does `FETCH` still
read and decode each whole document, including a large field we never asked for,
before dropping it? The docs don't spell this out. And does SBE in 8.0 change it,
so a scan can skip reading a large sibling field it doesn't project?

**2.** We're planning to move the large text field into its own collection and serve
the tree-view read from a covering index over just the small fields. Does that hold
up, or once everything is in cache does the per-document `FETCH` over a large match
set dominate anyway, making the split pointless for this read?

**3.** For a write-once tree of a few million nodes whose hot read pulls a whole
subtree's structure (small fields for up to tens of thousands of nodes), have you
seen this access pattern in the field? We're doing the modeling ourselves; what
we'd value is a case study or reference deployment of a large tree-view on MongoDB
at this scale, and what those teams ended up using.
