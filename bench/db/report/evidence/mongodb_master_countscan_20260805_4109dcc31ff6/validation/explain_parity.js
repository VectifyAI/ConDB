// Deterministic capture of count / count-like-aggregation explain + profiling output.
// Printed as a single canonical JSON blob on one line prefixed by CAPTURE:.

const testDb = db.getSiblingDB("countparity");
testDb.c.drop();

const docs = [];
for (let i = 0; i < 200; i++) {
    docs.push({_id: i, a: i});
}
assert.commandWorked(testDb.c.insert(docs));
assert.commandWorked(testDb.c.createIndex({a: 1}));

// Second collection with a MULTIKEY index, so CountScan deduplication and its
// memory tracking are exercised too (they sit above the changed branch).
testDb.m.drop();
const mdocs = [];
for (let i = 0; i < 200; i++) {
    mdocs.push({_id: i, a: [i, i + 200]});
}
assert.commandWorked(testDb.m.insert(mdocs));
assert.commandWorked(testDb.m.createIndex({a: 1}));

// Confirm the engine knob actually took effect on this server.
const fwk = assert.commandWorked(
    testDb.adminCommand({getParameter: 1, internalQueryFrameworkControl: 1}));

// ---- 1. indexed count: CountStage over a direct CountScan (the changed path) ----
const countResult = testDb.c.find({a: {$gte: 0}}).count();
const countExplain = testDb.c.explain("executionStats").find({a: {$gte: 0}}).count();

// ---- 2. count-like aggregation: bare CountScan, no CountStage (the at-risk path) ----
const aggResult = testDb.c.aggregate([{$match: {a: {$gte: 0}}}, {$count: "n"}]).toArray();
const aggExplain = testDb.c.explain("executionStats").aggregate([
    {$match: {a: {$gte: 0}}},
    {$count: "n"}
]);

// ---- 3. same two shapes against the MULTIKEY index (dedup + memory tracking) ----
const mcountResult = testDb.m.find({a: {$gte: 0}}).count();
const mcountExplain = testDb.m.explain("executionStats").find({a: {$gte: 0}}).count();
const maggResult = testDb.m.aggregate([{$match: {a: {$gte: 0}}}, {$count: "n"}]).toArray();
const maggExplain = testDb.m.explain("executionStats").aggregate([
    {$match: {a: {$gte: 0}}},
    {$count: "n"}
]);

// ---- 4. profiling parity for every shape ----
assert.commandWorked(testDb.setProfilingLevel(0));
testDb.system.profile.drop();
assert.commandWorked(testDb.setProfilingLevel(2));
assert.commandWorked(
    testDb.runCommand({count: "c", query: {a: {$gte: 0}}, comment: "parity-count"}));
testDb.c.aggregate([{$match: {a: {$gte: 0}}}, {$count: "n"}], {comment: "parity-agg"}).toArray();
assert.commandWorked(
    testDb.runCommand({count: "m", query: {a: {$gte: 0}}, comment: "parity-mcount"}));
testDb.m.aggregate([{$match: {a: {$gte: 0}}}, {$count: "n"}], {comment: "parity-magg"}).toArray();
assert.commandWorked(testDb.setProfilingLevel(0));

function profileFor(comment) {
    const e = testDb.system.profile.find({"command.comment": comment}).toArray();
    if (e.length !== 1) {
        return {matched: e.length};
    }
    const p = e[0];
    return {
        matched: 1,
        op: p.op,
        ns: p.ns,
        keysExamined: p.keysExamined,
        docsExamined: p.docsExamined,
        nreturned: p.nreturned,
        planSummary: p.planSummary,
        fromMultiPlanner: p.fromMultiPlanner === undefined ? null : p.fromMultiPlanner,
        replanned: p.replanned === undefined ? null : p.replanned,
        hasSortStage: p.hasSortStage === undefined ? null : p.hasSortStage,
        usedDisk: p.usedDisk === undefined ? null : p.usedDisk,
        cursorExhausted: p.cursorExhausted === undefined ? null : p.cursorExhausted,
        queryFramework: p.queryFramework,
    };
}

// Recursively pull every stage node out of an executionStages tree.
function collectStages(node, acc) {
    if (node === null || typeof node !== "object") {
        return acc;
    }
    if (node.stage !== undefined) {
        acc.push(node);
    }
    for (const k of ["inputStage", "thenStage", "elseStage"]) {
        if (node[k]) {
            collectStages(node[k], acc);
        }
    }
    for (const k of ["inputStages", "shards"]) {
        if (Array.isArray(node[k])) {
            for (const s of node[k]) {
                collectStages(s, acc);
            }
        }
    }
    return acc;
}

// Strip only fields that are inherently nondeterministic run-to-run or
// build-identity fields. Everything else is compared verbatim.
const VOLATILE = new Set([
    "executionTimeMillis",
    "executionTimeMillisEstimate",
    "executionTimeMicros",
    "executionTimeNanos",
    "serverInfo",
    "serverParameters",
    "host",
    "port",
    "operationTime",
    "$clusterTime",
    "$configTime",
    "$topologyTime",
    "operationMetrics",
    "durationMillis",
    "workingMillis",
    "cpuNanos",
]);

// Drops VOLATILE keys and renders everything else into strict, key-sorted JSON
// values. Non-plain objects (NumberLong, Timestamp, ObjectId, Date, ...) become
// their string form, which is deterministic and therefore still comparable.
function scrub(v) {
    if (v === null || v === undefined) {
        return null;
    }
    if (Array.isArray(v)) {
        return v.map(scrub);
    }
    const t = typeof v;
    if (t === "number" || t === "string" || t === "boolean") {
        return v;
    }
    if (t === "object" && v.constructor === Object) {
        const out = {};
        for (const k of Object.keys(v).sort()) {
            if (VOLATILE.has(k)) {
                continue;
            }
            out[k] = scrub(v[k]);
        }
        return out;
    }
    return String(v);
}

function summarizeExplain(exp) {
    const es = exp.executionStats ||
        (exp.stages && exp.stages[0] && exp.stages[0].$cursor &&
         exp.stages[0].$cursor.executionStats);
    const root = es ? es.executionStages : null;
    const stages = root ? collectStages(root, []) : [];
    const countScan = stages.filter((s) => s.stage === "COUNT_SCAN");
    const countStage = stages.filter((s) => s.stage === "COUNT");
    return {
        // Task-4 requested scalars.
        nCounted: countStage.length ? countStage[0].nCounted : null,
        nSkipped: countStage.length ? countStage[0].nSkipped : null,
        totalKeysExamined: es ? es.totalKeysExamined : null,
        totalDocsExamined: es ? es.totalDocsExamined : null,
        nReturned: es ? es.nReturned : null,
        executionSuccess: es ? es.executionSuccess : null,
        stageChain: stages.map((s) => s.stage),
        hasCountStage: countStage.length > 0,
        countScan: countScan.map((s) => ({
            stage: s.stage,
            works: s.works,
            advanced: s.advanced,
            needTime: s.needTime,
            needYield: s.needYield,
            saveState: s.saveState === undefined ? null : s.saveState,
            restoreState: s.restoreState === undefined ? null : s.restoreState,
            isEOF: s.isEOF,
            keysExamined: s.keysExamined,
            keyPattern: s.keyPattern,
            indexName: s.indexName,
            isMultiKey: s.isMultiKey,
            multiKeyPaths: s.multiKeyPaths,
            isUnique: s.isUnique,
            isSparse: s.isSparse,
            isPartial: s.isPartial,
            indexVersion: s.indexVersion,
            indexBounds: s.indexBounds,
            maxTrackedMemUsageBytes:
                s.maxTrackedMemUsageBytes === undefined ? null : s.maxTrackedMemUsageBytes,
        })),
        planSummary: exp.queryPlanner ? exp.queryPlanner.winningPlan : null,
        queryFramework: exp.explainVersion !== undefined ? exp.explainVersion : null,
    };
}

const capture = {
    frameworkControl: fwk.internalQueryFrameworkControl,
    count: {
        result: countResult,
        summary: summarizeExplain(countExplain),
        fullExplain: scrub(countExplain),
        profile: profileFor("parity-count"),
    },
    agg: {
        result: aggResult,
        summary: summarizeExplain(aggExplain),
        fullExplain: scrub(aggExplain),
        profile: profileFor("parity-agg"),
    },
    multikeyCount: {
        result: mcountResult,
        summary: summarizeExplain(mcountExplain),
        fullExplain: scrub(mcountExplain),
        profile: profileFor("parity-mcount"),
    },
    multikeyAgg: {
        result: maggResult,
        summary: summarizeExplain(maggExplain),
        fullExplain: scrub(maggExplain),
        profile: profileFor("parity-magg"),
    },
};

print("CAPTURE:" + JSON.stringify(scrub(capture)));
