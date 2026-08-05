// Capture the full wire reply of a matrix of find commands, so a base and a patched binary can be
// compared field by field. Printed as one JSON blob on the last line.
const testDB = db.getSiblingDB("expressparity");
testDB.coll.drop();

const docs = [];
for (let i = 0; i < 200; i++) {
    docs.push({_id: i, uniqueField: i, nonUnique: i % 10, payload: "x".repeat(32)});
}
assert.commandWorked(testDB.coll.insert(docs));
assert.commandWorked(testDB.coll.createIndex({uniqueField: 1}, {unique: true}));
assert.commandWorked(testDB.coll.createIndex({nonUnique: 1}));

// Every case is a find command body; "$db" is added below.
const cases = {
    // The shape the change is aimed at: unique-field equality, various batchSize values.
    "unique_nobatch": {find: "coll", filter: {uniqueField: 7}},
    "unique_batch1": {find: "coll", filter: {uniqueField: 7}, batchSize: 1},
    "unique_batch2": {find: "coll", filter: {uniqueField: 7}, batchSize: 2},
    "unique_batch100": {find: "coll", filter: {uniqueField: 7}, batchSize: 100},
    "unique_batch0": {find: "coll", filter: {uniqueField: 7}, batchSize: 0},
    "unique_batch1_singleBatch": {find: "coll", filter: {uniqueField: 7}, batchSize: 1, singleBatch: true},
    "unique_batch1_limit1": {find: "coll", filter: {uniqueField: 7}, batchSize: 1, limit: 1},
    "unique_batch1_proj": {find: "coll", filter: {uniqueField: 7}, batchSize: 1, projection: {uniqueField: 1}},
    "unique_batch1_nomatch": {find: "coll", filter: {uniqueField: 100000}, batchSize: 1},

    // _id equality, the other express shape.
    "id_nobatch": {find: "coll", filter: {_id: 7}},
    "id_batch1": {find: "coll", filter: {_id: 7}, batchSize: 1},
    "id_batch0": {find: "coll", filter: {_id: 7}, batchSize: 0},
    "id_batch1_nomatch": {find: "coll", filter: {_id: 100000}, batchSize: 1},

    // Shapes that must NOT become express-eligible: multi-match, non-unique, sort, returnKey.
    "nonunique_batch1": {find: "coll", filter: {nonUnique: 3}, batchSize: 1},
    "nonunique_batch1_limit1": {find: "coll", filter: {nonUnique: 3}, batchSize: 1, limit: 1},
    "nonunique_batch5": {find: "coll", filter: {nonUnique: 3}, batchSize: 5},
    "unique_batch1_sort": {find: "coll", filter: {uniqueField: 7}, batchSize: 1, sort: {uniqueField: -1}},
    "unique_batch1_returnKey": {find: "coll", filter: {uniqueField: 7}, batchSize: 1, returnKey: true},
    "range_batch1": {find: "coll", filter: {uniqueField: {$gte: 5, $lt: 15}}, batchSize: 1},
};

const out = {};
for (const [name, cmd] of Object.entries(cases)) {
    const body = Object.assign({}, cmd, {$db: "expressparity"});
    const reply = testDB.runCommand(cmd);
    // Normalise the parts that legitimately vary between two servers.
    const record = {ok: reply.ok};
    if (reply.cursor) {
        record.firstBatch = reply.cursor.firstBatch;
        record.cursorIdIsZero = (reply.cursor.id.toString() === "NumberLong(0)" ||
                                 reply.cursor.id.toString() === "0");
        record.ns = reply.cursor.ns;
        // If a cursor was left open, drain it so the comparison covers the full result set and
        // the server is not left holding cursors.
        if (!record.cursorIdIsZero) {
            const drained = [];
            let more = testDB.runCommand({getMore: reply.cursor.id, collection: "coll"});
            while (more.ok && more.cursor) {
                drained.push(...more.cursor.nextBatch);
                if (more.cursor.id.toString() === "NumberLong(0)" ||
                    more.cursor.id.toString() === "0") {
                    break;
                }
                more = testDB.runCommand({getMore: more.cursor.id, collection: "coll"});
            }
            record.drained = drained;
        }
    } else {
        record.errmsg = reply.errmsg;
        record.code = reply.code;
    }

    // Plan shape, which is where the change is expected to be visible.
    const ex = testDB.runCommand({explain: cmd, verbosity: "queryPlanner"});
    if (ex.ok && ex.queryPlanner) {
        record.winningPlanStage = ex.queryPlanner.winningPlan.stage ||
            (ex.queryPlanner.winningPlan.queryPlan && ex.queryPlanner.winningPlan.queryPlan.stage);
        record.isExpress = JSON.stringify(ex.queryPlanner.winningPlan).indexOf("EXPRESS") >= 0;
    }
    out[name] = record;
}

print("PARITY_JSON_BEGIN");
print(JSON.stringify(out, null, 1));
print("PARITY_JSON_END");
