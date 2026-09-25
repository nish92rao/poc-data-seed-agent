"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createHash, createHmac } = require("node:crypto");
const { Decimal128, Long, ObjectId } = require("mongodb");
const { _test } = require("./handler");
const POC_ID = "poc_01J8Q8MFYJ6NVJ8B2Q5V2D9Q1A";
const RUN_ID = "run_01J8Q8MFYJ6NVJ8B2Q5V2D9Q1B";
const TASK_ID = "task_01J8Q8MFYJ6NVJ8B2Q5V2D9Q1C";
const HMAC_SECRET = "a".repeat(64);

function signedEvent(body, timestamp, secret = HMAC_SECRET) {
  return {
    headers: {
      "x-validator-timestamp": String(timestamp),
      "x-validator-signature": createHmac("sha256", secret).update(`${timestamp}.${body}`).digest("hex"),
    },
  };
}

function directRequest() {
  const schemaDesign = JSON.stringify({
    collections: [{ name: "users", fields: [{ name: "_id", type: "objectId", required: true }], indexes: [], seed: { count: 1 } }],
  });
  const queryPatterns = JSON.stringify({ patterns: [] });
  const contents = {
    "seed.js": 'const { MongoClient } = require("mongodb"); const uri = process.env.MONGODB_URI; const db = process.env.DB_NAME; const max = process.env.SEED_MAX_DOCS; const caps = process.env.SEED_COLLECTION_CAPS; console.log(JSON.stringify({seed_summary:{users:1}}));',
    "package.json": '{"scripts":{"seed":"node seed.js"},"dependencies":{"mongodb":"^6.0.0"}}',
    "SEED_README.md": "seed notes",
  };
  const prefix = `pocs/${POC_ID}/code/v001/seed`;
  const artifacts = Object.entries(contents).map(([filename, content]) => ({
    key: `${prefix}/${filename}`,
    sha256: createHash("sha256").update(content).digest("hex"),
    bytes: Buffer.byteLength(content),
  }));
  const manifest = {
    poc_id: POC_ID,
    spec_version: "v001",
    code_version: "v001",
    storage: { provider: "github", repository: "nish92rao/magenta-test-repo", branch: `poc/${POC_ID}` },
    inputs: {
      schema_design: { key: `pocs/${POC_ID}/spec/v001/schema_design.json`, sha256: createHash("sha256").update(schemaDesign).digest("hex") },
      query_patterns: { key: `pocs/${POC_ID}/spec/v001/query_patterns.json`, sha256: createHash("sha256").update(queryPatterns).digest("hex") },
    },
    artifacts,
    correlation: { poc_id: POC_ID, run_id: RUN_ID, task_id: TASK_ID, trace_id: "trace_1", producer: "poc-data-seed" },
    producer: "poc-data-seed",
  };
  return {
    poc_id: POC_ID,
    run_id: RUN_ID,
    task_id: TASK_ID,
    trace_id: "trace_1",
    spec_version: "v001",
    code_version: "v001",
    repository: "nish92rao/magenta-test-repo",
    branch: `poc/${POC_ID}`,
    source_commit_sha: "a".repeat(40),
    mongodb_uri: "mongodb://example.test/db",
    manifest_json: JSON.stringify(manifest),
    schema_design_json: schemaDesign,
    query_patterns_json: queryPatterns,
    artifacts: contents,
  };
}

function projectDirectRequest() {
  const request = directRequest();
  request.poc_id = "1790237138344";
  request.branch = "nishit-rao-mongodb-com/triage-support";
  delete request.schema_design_json;
  request.data_model_json = JSON.stringify({ collections: [{ name: "users", document_shape: { _id: { type: "objectId" } } }] });
  request.normalized_data_model_json = JSON.stringify({
    collections: [{ name: "users", fields: [{ name: "_id", type: "objectId" }], indexes: [], relationships: [], seed: { count: 1 } }],
  });
  request.normalized_query_patterns_json = JSON.stringify({ patterns: [] });
  const prefix = "seed/v001";
  const manifest = JSON.parse(request.manifest_json);
  manifest.poc_id = request.poc_id;
  delete manifest.spec_version;
  manifest.storage.branch = request.branch;
  manifest.inputs = {
    data_model: {
      key: "spec_architect/data_model.json",
      commit_sha: "b".repeat(40),
      sha256: createHash("sha256").update(request.data_model_json).digest("hex"),
    },
    query_patterns: {
      key: "spec_architect/query_patterns.json",
      commit_sha: "c".repeat(40),
      sha256: createHash("sha256").update(request.query_patterns_json).digest("hex"),
    },
  };
  manifest.correlation.poc_id = request.poc_id;
  manifest.artifacts = Object.entries(request.artifacts).map(([filename, content]) => ({
    key: `${prefix}/${filename}`,
    sha256: createHash("sha256").update(content).digest("hex"),
    bytes: Buffer.byteLength(content),
  }));
  request.manifest_json = JSON.stringify(manifest);
  return request;
}

test("calculates a five-percent per-collection cap with a maximum of 100", () => {
  const caps = _test.validationCaps({
    collections: [
      { name: "small", seed: { count: 1 } },
      { name: "orders", seed: { count: 240 } },
      { name: "large", seed: { count: 5000 } },
    ],
  });
  assert.deepEqual(caps, { small: 1, orders: 12, large: 100 });
});

test("uses the sum of independent collection caps as the seed document budget", () => {
  assert.equal(_test.validationDocumentBudget({ accounts: 3, plans: 1, subscriptions: 6, invoices: 12, payments: 9 }), 31);
  assert.equal(_test.validationDocumentBudget({}), 0);
});

test("uses a run-scoped validation database name", () => {
  assert.equal(_test.validationDatabaseName("poc_demo_002", "run_demo_002"), "poc_demo_002_90a57bf6bc9e98cb40b7");
});

test("bounds target and validation database names without losing run isolation", () => {
  const target = _test.targetDatabaseName("poc_subscription_billing_001");
  const first = _test.validationDatabaseName("poc_subscription_billing_001", "run_subscription_billing_001");
  const second = _test.validationDatabaseName("poc_subscription_billing_001", "run_subscription_billing_002");
  assert.equal(target, "poc_subscription_billing_001");
  assert.ok(Buffer.byteLength(target, "utf8") <= 38);
  assert.ok(Buffer.byteLength(first, "utf8") <= 38);
  assert.notEqual(first, second);
  assert.equal(first, _test.validationDatabaseName("poc_subscription_billing_001", "run_subscription_billing_001"));
});

test("truncates an overlong POC identifier for the actual seed database", () => {
  const target = _test.targetDatabaseName("poc_a_very_long_identifier_that_exceeds_mongodb_database_name_limits");
  assert.ok(Buffer.byteLength(target, "utf8") <= 38);
  assert.match(target, /^poc_a_very_long_identifie_[a-f0-9]{12}$/);
});

test("requires a direct collection-count seed summary", () => {
  assert.deepEqual(_test.parseSeedSummary('{"seed_summary":{"orders":12}}\n'), { orders: 12 });
  assert.throws(() => _test.parseSeedSummary('one\ntwo\n'), /exactly one JSON summary line/);
});

test("requires generated seed code to declare matching vector search indexes", () => {
  const patterns = {
    patterns: [{
      id: "QP-2",
      validation_mode: "static_vector",
      vector_dimensions: 8,
      pipeline: [{ $vectorSearch: { index: "ticket_text_embedding_index", path: "text_embedding" } }],
    }],
  };
  assert.throws(
    () => _test.validateVectorSeedContract(Buffer.from("const seed = true;"), patterns),
    (error) => error.code === "SEED_SCRIPT_INVALID",
  );
  assert.doesNotThrow(() => _test.validateVectorSeedContract(
    Buffer.from('if (process.env.SEED_SKIP_SEARCH_INDEXES !== "1") collection.createSearchIndex({name:"ticket_text_embedding_index",definition:{path:"text_embedding",numDimensions:8}});'),
    patterns,
  ));
});

test("creates and verifies exact validation vector search index definitions", async () => {
  const calls = [];
  const collection = {
    createSearchIndex: async (definition) => calls.push(definition),
    listSearchIndexes: (name) => ({ toArray: async () => [{ name }] }),
  };
  const db = { collection: (name) => {
    assert.equal(name, "support_tickets");
    return collection;
  } };
  const patterns = {
    patterns: [{
      id: "QP-2",
      collection: "support_tickets",
      validation_mode: "static_vector",
      vector_dimensions: 8,
      pipeline: [{ $vectorSearch: { index: "ticket_text_embedding_index", path: "text_embedding" } }],
    }],
  };
  const result = await _test.createValidationSearchIndexes(db, patterns);
  assert.deepEqual(calls, [{
    name: "ticket_text_embedding_index",
    type: "vectorSearch",
    definition: {
      fields: [{ type: "vector", path: "text_embedding", numDimensions: 8, similarity: "cosine" }],
    },
  }]);
  assert.deepEqual(result, { created: [{ collection: "support_tickets", name: "ticket_text_embedding_index" }] });
});

test("creates and verifies temporary Atlas Search index definitions", async () => {
  const calls = [];
  const collection = {
    createSearchIndex: async (definition) => calls.push(definition),
    listSearchIndexes: (name) => ({ toArray: async () => [{ name }] }),
  };
  const patterns = { patterns: [{
    id: "QP-search",
    collection: "chunks",
    validation_mode: "static_search",
    pipeline: [{ $search: { index: "search_chunks_text", text: { path: "text", query: "<queryText>" } } }],
  }] };
  const result = await _test.createValidationSearchIndexes({ collection: () => collection }, patterns);
  assert.deepEqual(calls, [{
    name: "search_chunks_text",
    type: "search",
    definition: { mappings: { dynamic: true } },
  }]);
  assert.deepEqual(result, { created: [{ collection: "chunks", name: "search_chunks_text" }] });
});

test("authenticates exact bodies at both replay-window boundaries", async () => {
  const now = 1_700_000_000_000;
  const body = '{"request":"exact"}';
  const getSecret = async () => HMAC_SECRET;
  for (const timestamp of [1_699_999_700, 1_700_000_300]) {
    assert.equal(await _test.authenticate(signedEvent(body, timestamp), body, { now: () => now, getSecret }), true);
  }
  assert.equal(await _test.authenticate(signedEvent(body, 1_699_999_699), body, { now: () => now, getSecret }), false);
  assert.equal(await _test.authenticate(signedEvent(body, 1_700_000_301), body, { now: () => now, getSecret }), false);
});

test("rejects missing malformed and tampered HMAC signatures", async () => {
  const body = '{"request":"exact"}';
  const now = () => 1_700_000_000_000;
  const getSecret = async () => HMAC_SECRET;
  assert.equal(await _test.authenticate({ headers: {} }, body, { now, getSecret }), false);
  assert.equal(await _test.authenticate({ headers: { "x-validator-timestamp": "not-a-number", "x-validator-signature": "a".repeat(64) } }, body, { now, getSecret }), false);
  assert.equal(await _test.authenticate({ headers: { "X-VALIDATOR-TIMESTAMP": "1700000000", "X-VALIDATOR-SIGNATURE": "xyz" } }, body, { now, getSecret }), false);
  assert.equal(await _test.authenticate(signedEvent(body, 1_700_000_000), `${body} `, { now, getSecret }), false);
});

test("refreshes the cached HMAC secret once after rotation", async () => {
  const body = "{}";
  const rotatedSecret = "b".repeat(64);
  const calls = [];
  const getSecret = async (options) => {
    calls.push(Boolean(options.forceRefresh));
    return options.forceRefresh ? rotatedSecret : HMAC_SECRET;
  };
  assert.equal(
    await _test.authenticate(signedEvent(body, 1_700_000_000, rotatedSecret), body, {
      now: () => 1_700_000_000_000,
      getSecret,
    }),
    true,
  );
  assert.deepEqual(calls, [false, true]);
});

test("expires the Secrets Manager HMAC cache after the replay window", async () => {
  const previousSecretId = process.env.VALIDATOR_AUTH_SECRET_ARN;
  process.env.VALIDATOR_AUTH_SECRET_ARN = "test-secret";
  _test.resetHmacSecretCache();
  let reads = 0;
  const secretClient = {
    send: async () => {
      reads += 1;
      return { SecretString: JSON.stringify({ hmac_secret: HMAC_SECRET }) };
    },
  };
  try {
    await _test.getHmacSecret({ now: () => 1_000, secretClient });
    await _test.getHmacSecret({ now: () => 300_999, secretClient });
    assert.equal(reads, 1);
    await _test.getHmacSecret({ now: () => 301_000, secretClient });
    assert.equal(reads, 2);
  } finally {
    _test.resetHmacSecretCache();
    if (previousSecretId === undefined) delete process.env.VALIDATOR_AUTH_SECRET_ARN;
    else process.env.VALIDATOR_AUTH_SECRET_ARN = previousSecretId;
  }
});

test("accepts intentional replay within the documented window", async () => {
  const body = "{}";
  const event = signedEvent(body, 1_700_000_000);
  const options = { now: () => 1_700_000_100_000, getSecret: async () => HMAC_SECRET };
  assert.equal(await _test.authenticate(event, body, options), true);
  assert.equal(await _test.authenticate(event, body, options), true);
});

test("cleanup drops only the supplied run-scoped validation database", async () => {
  const calls = [];
  const client = {
    connect: async () => calls.push("connect"),
    db: (name) => {
      calls.push(["db", name]);
      return { dropDatabase: async () => calls.push("dropDatabase") };
    },
    close: async () => calls.push("close"),
  };
  const databaseName = _test.validationDatabaseName(POC_ID, RUN_ID);
  await _test.cleanupValidationDatabase(client, databaseName);
  assert.deepEqual(calls, ["connect", ["db", databaseName], "dropDatabase", "close"]);
  assert.notEqual(databaseName, _test.targetDatabaseName(POC_ID));
});

test("cleanup deletes only declared search indexes before dropping the database", async () => {
  const calls = [];
  const collection = {
    listSearchIndexes: () => ({ toArray: async () => [
      { name: "ticket_text_embedding_index" },
      { name: "unrelated_index" },
    ] }),
    dropSearchIndex: async (name) => calls.push(["dropSearchIndex", name]),
  };
  const database = {
    collection: (name) => {
      calls.push(["collection", name]);
      return collection;
    },
    dropDatabase: async () => calls.push("dropDatabase"),
  };
  const client = {
    connect: async () => calls.push("connect"),
    db: (name) => {
      calls.push(["db", name]);
      return database;
    },
    close: async () => calls.push("close"),
  };
  const patterns = {
    patterns: [{
      collection: "support_tickets",
      validation_mode: "static_vector",
      pipeline: [{ $vectorSearch: { index: "ticket_text_embedding_index", path: "text_embedding" } }],
    }],
  };
  await _test.cleanupValidationDatabase(client, "validation_db", patterns);
  assert.deepEqual(calls, [
    "connect",
    ["db", "validation_db"],
    ["collection", "support_tickets"],
    ["dropSearchIndex", "ticket_text_embedding_index"],
    "dropDatabase",
    "close",
  ]);
});

test("search index cleanup failures become sanitized cleanup failures", async () => {
  const client = {
    connect: async () => {},
    db: () => ({
      collection: () => ({
        listSearchIndexes: () => ({ toArray: async () => { throw new Error("mongodb://user:pass@host/secret"); } }),
      }),
      dropDatabase: async () => {},
    }),
    close: async () => {},
  };
  const patterns = {
    patterns: [{
      collection: "support_tickets",
      validation_mode: "static_vector",
      pipeline: [{ $vectorSearch: { index: "ticket_text_embedding_index", path: "text_embedding" } }],
    }],
  };
  await assert.rejects(
    () => _test.cleanupValidationDatabase(client, "validation_db", patterns),
    (error) => error.code === "VALIDATION_CLEANUP_FAILED" && !error.message.includes("mongodb"),
  );
});

test("cleanup failures are sanitized retryable infrastructure failures", async () => {
  const client = {
    connect: async () => {},
    db: () => ({ dropDatabase: async () => { throw new Error("mongodb://user:pass@host/target"); } }),
    close: async () => {},
  };
  await assert.rejects(
    () => _test.cleanupValidationDatabase(client, _test.validationDatabaseName(POC_ID, RUN_ID)),
    (error) => error.code === "VALIDATION_CLEANUP_FAILED"
      && !error.message.includes("mongodb")
      && !error.message.includes("user:pass"),
  );
  const classified = _test.classifyFailure({ code: "VALIDATION_CLEANUP_FAILED" });
  assert.deepEqual(classified, {
    code: "VALIDATION_CLEANUP_FAILED",
    failure_class: "VALIDATOR_INFRASTRUCTURE_FAILURE",
    message: "Validation database cleanup failed.",
    retryable: true,
  });
});

test("classifies contradictory field schemas and ignores query requirements", () => {
  const schema = {
    collections: [
      { name: "accounts", fields: [{ name: "_id", type: "objectId" }] },
      {
        name: "invoices",
        fields: [{ name: "account_id", type: "objectId" }],
        relationships: [{ field: "account_id", references: "accounts._id" }],
      },
    ],
  };
  assert.doesNotThrow(() => _test.validateInputContract(schema, { patterns: [{ id: "p1", collection: "invoices", operation: "find" }] }));
  assert.doesNotThrow(() => _test.validateInputContract(schema, { patterns: [{ id: "p2", collection: "payments", operation: "invalid" }] }));
  assert.throws(
    () => _test.validateInputContract({ collections: [{ name: "bad", fields: [{ name: "value" }] }] }, { patterns: [] }),
    (error) => error.failureClass === "REQUEST_CONTRADICTION",
  );
});

test("validates required optional and BSON field types recursively", async () => {
  const schema = { collections: [{
    name: "records",
    fields: [
      { name: "_id", type: "objectId", required: true },
      { name: "name", type: "string", required: true },
      { name: "enabled", type: "boolean", required: false },
      { name: "count", type: "int", required: true },
      { name: "large", type: "long", required: true },
      { name: "ratio", type: "double", required: true },
      { name: "amount", type: "decimal", required: true },
      { name: "created_at", type: "date", required: true },
      { name: "tags", type: "array<string>", required: true },
      { name: "metadata", type: "document", required: true, fields: [
        { name: "language", type: "string", required: true },
      ] },
      { name: "results", type: "array<document>", required: true, fields: [
        { name: "score", type: "double", required: true },
      ] },
      { name: "anything", type: "mixed", required: true },
    ],
  }] };
  const document = {
    _id: new ObjectId(), name: "record", count: 2, large: Long.fromNumber(10), ratio: 0.5,
    amount: Decimal128.fromString("12.50"), created_at: new Date("2026-01-01T00:00:00Z"),
    tags: ["one", "two"], metadata: { language: "en" }, results: [{ score: 0.9 }], anything: { free: true },
  };
  const db = { collection: () => ({ find: () => ({ toArray: async () => [document] }) }) };
  await assert.doesNotReject(() => _test.verifyFieldSchemas(db, schema));
  document.enabled = null;
  await assert.doesNotReject(() => _test.verifyFieldSchemas(db, schema));
});

test("rejects missing null and incorrectly typed seeded fields with paths", async () => {
  const fields = [
    { name: "name", type: "string", required: true },
    { name: "metadata", type: "document", required: true, fields: [
      { name: "language", type: "string", required: true },
    ] },
    { name: "results", type: "array<document>", required: true, fields: [
      { name: "score", type: "double", required: true },
    ] },
  ];
  assert.throws(() => _test.validateDocumentFields({ metadata: { language: "en" }, results: [] }, fields, "records"), /field name is required/);
  assert.throws(() => _test.validateDocumentFields({ name: null, metadata: { language: "en" }, results: [] }, fields, "records"), /field name is required/);
  assert.throws(() => _test.validateDocumentFields({ name: "ok", metadata: { language: 7 }, results: [] }, fields, "records"), /metadata.language requires string/);
  assert.throws(() => _test.validateDocumentFields({ name: "ok", metadata: { language: "en" }, results: [{ score: "high" }] }, fields, "records"), /results\[0\]\.score requires double/);
});

test("accepts absent optional fields but validates present optional values and enums", () => {
  const fields = [
    { name: "status", type: "string", required: false, enum: ["open", "closed"] },
    { name: "flags", type: "array<boolean>", required: false },
  ];
  assert.doesNotThrow(() => _test.validateDocumentFields({}, fields, "records"));
  assert.doesNotThrow(() => _test.validateDocumentFields({ status: null }, fields, "records"));
  assert.doesNotThrow(() => _test.validateDocumentFields({ status: "open", flags: [true, false] }, fields, "records"));
  assert.throws(() => _test.validateDocumentFields({ status: "invalid" }, fields, "records"), /outside its enum/);
  assert.throws(() => _test.validateDocumentFields({ flags: [true, "false"] }, fields, "records"), /requires array<boolean>/);
});

test("ignores relationship metadata in input contracts", () => {
  const schema = { collections: [{
    name: "customers",
    fields: [{ name: "customer_id", type: "string", required: true }],
    relationships: ["malformed", { type: "one_to_many", to_collection: "missing", via: "anything" }],
  }] };
  assert.doesNotThrow(() => _test.validateInputContract(schema, { patterns: [{ operation: "invalid" }] }));
});

test("enables forced implementation failure only when explicitly requested", () => {
  assert.equal(_test.forcedImplementationFailure("false"), null);
  const failure = _test.forcedImplementationFailure("true");
  assert.equal(failure.failureClass, "IMPLEMENTATION_FAILURE");
  assert.match(failure.message, /Forced validation failure/);
});

test("substitutes query placeholders deterministically", () => {
  const match = _test.substitutePlaceholders(
    { customer_id: "<object_id>", status: "paid", created_at: { "$lt": "<cursor_date_optional>" } },
    { _id: "fallback-id", customer_id: "customer-1", created_at: new Date("2026-01-01T00:00:00Z") },
  );
  assert.deepEqual(match, {
    customer_id: "customer-1",
    status: "paid",
    created_at: { "$lt": new Date("2026-01-01T00:00:00Z") },
  });
  assert.equal(_test.substitutePlaceholders("<hour|day|week>", {}), "day");
  assert.deepEqual(_test.substitutePlaceholders("<query_embedding>", {}), Array(8).fill(0));
  assert.equal(_test.substitutePlaceholders("<score_1>", {}), 0.5);
  assert.equal(_test.substitutePlaceholders("<top_k>", {}), 10);
  assert.equal(_test.substitutePlaceholders("<query_text>", {}), "query_text");
});

test("executes ordered insertOne and findOne patterns with read-back", async () => {
  const calls = [];
  const documents = [{ _id: "seed-1", query_text: "existing", created_at: new Date("2026-01-01T00:00:00Z") }];
  const collection = {
    findOne: async (match, options) => {
      calls.push(["findOne", match, options]);
      if (!match || Object.keys(match).length === 0) return documents[0];
      return documents.find((document) => Object.entries(match).every(([key, value]) => document[key] === value)) || null;
    },
    insertOne: async (document) => {
      calls.push(["insertOne", document]);
      const inserted = { _id: "inserted-1", ...document };
      documents.push(inserted);
      return { acknowledged: true, insertedId: inserted._id };
    },
  };
  const result = await _test.verifyQueryPatterns(
    { collection: () => collection },
    { patterns: [
      {
        id: "insert",
        collection: "search_queries",
        operation: "insertOne",
        document: { query_text: "<query_text>", created_at: "<now>" },
      },
      {
        id: "read",
        collection: "search_queries",
        operation: "findOne",
        match: { _id: "inserted-1" },
        projection: { query_text: 1 },
      },
    ] },
  );
  assert.deepEqual(result, {
    runtime_executed: 2,
    read_operations_executed: 1,
    write_operations_executed: 1,
    static_vector_validated: 0,
    static_search_validated: 0,
  });
  assert.equal(calls.filter(([name]) => name === "insertOne").length, 1);
  assert.ok(calls.some(([name, match, options]) => name === "findOne"
    && match?._id === "inserted-1" && options?.projection?.query_text === 1));
});

test("rejects unacknowledged insertOne validation", async () => {
  const collection = {
    findOne: async () => ({ _id: "seed-1" }),
    insertOne: async () => ({ acknowledged: false }),
  };
  await assert.rejects(
    () => _test.verifyQueryPatterns(
      { collection: () => collection },
      { patterns: [{ id: "insert", collection: "queries", operation: "insertOne", document: { value: "<value>" } }] },
    ),
    /insertOne was not acknowledged/,
  );
});

test("substitutes find limit placeholders before execution", async () => {
  const calls = [];
  const cursor = {
    project: () => cursor,
    sort: () => cursor,
    limit: (value) => {
      calls.push(value);
      return cursor;
    },
    toArray: async () => [],
  };
  const collection = {
    findOne: async () => ({ _id: "seed-1" }),
    find: () => cursor,
  };
  const result = await _test.verifyQueryPatterns(
    { collection: () => collection },
    { patterns: [{
      id: "history",
      collection: "search_queries",
      operation: "find",
      match: {},
      limit: "<page_size>",
    }] },
  );
  assert.deepEqual(calls, [10]);
  assert.equal(result.read_operations_executed, 1);
});

test("executes aggregate_merge only into its declared validation collection", async () => {
  const calls = [];
  let mergedDocument;
  const source = {
    findOne: async () => ({ _id: "source-1", value: 3 }),
    aggregate: (pipeline) => ({
      toArray: async () => {
        calls.push(["aggregate", pipeline]);
        mergedDocument = { _id: "snapshot-1", value: 3 };
        return [];
      },
    }),
  };
  const target = { findOne: async () => mergedDocument || null };
  const db = { collection: (name) => name === "source" ? source : target };
  const patterns = { patterns: [{
    id: "refresh",
    collection: "source",
    operation: "aggregate_merge",
    pipeline: [{ "$project": { value: 1 } }, { "$merge": { into: "snapshots" } }],
  }] };
  _test.validateInputContract({
    collections: [
      { name: "source", fields: [{ name: "_id", type: "string" }, { name: "value", type: "int" }] },
      { name: "snapshots", fields: [{ name: "_id", type: "string" }, { name: "value", type: "int" }] },
    ],
  }, patterns);
  const result = await _test.verifyQueryPatterns(db, patterns);
  assert.equal(calls.length, 1);
  assert.deepEqual(result, {
    runtime_executed: 1,
    read_operations_executed: 0,
    write_operations_executed: 1,
    static_vector_validated: 0,
    static_search_validated: 0,
  });
});

test("accepts production manifest artifact keys", () => {
  const artifact = { key: `pocs/${POC_ID}/code/v001/seed/seed.js` };
  assert.equal(artifact.key.split("/").pop(), "seed.js");
});

test("rejects malformed production ULIDs", () => {
  assert.throws(() => _test.ensureProductionId("poc_01J8Q8MFYJ6NVJ8B2Q5V2D9Q1I", "poc", "poc_id"), /poc_id/);
  assert.throws(() => _test.ensureProductionId("run_readable", "run", "run_id"), /run_id/);
  assert.doesNotThrow(() => _test.ensureProductionId(POC_ID, "poc", "poc_id"));
});

test("sanitizes and classifies raw infrastructure failures", () => {
  const failure = _test.classifyFailure(new Error("ECONNREFUSED mongodb+srv://user:pass@example.test at 10.2.3.4:27017 arn:aws:iam::123:role/test"));
  assert.equal(failure.code, "VALIDATOR_INFRASTRUCTURE_FAILURE");
  assert.equal(failure.message, "The seed validator is temporarily unavailable.");
  assert.equal(failure.retryable, true);
});

test("preserves only sanitized implementation findings", () => {
  const failure = _test.classifyFailure(new Error("Query pattern billing-01 has no seed data mongodb://secret@host"));
  assert.equal(failure.failure_class, "IMPLEMENTATION_FAILURE");
  assert.doesNotMatch(failure.message, /mongodb|secret@host/);
});

test("does not expose arbitrary seed process stderr", () => {
  const failure = _test.classifyFailure(new Error("seed.js failed: password=secret at /var/task/seed.js"));
  assert.equal(failure.code, "VALIDATION_FAILED");
  assert.equal(failure.message, "The generated seed script exited with an error.");
  assert.doesNotMatch(failure.message, /password|var\/task/);
});

test("classifies artifact content violations without leaking details", () => {
  const secret = new Error("mongodb+srv://user:pass@host/db");
  secret.code = "ARTIFACT_SECRET_DETECTED";
  secret.failureClass = "ARTIFACT_SECURITY_FAILURE";
  const failure = _test.classifyFailure(secret);
  assert.equal(failure.code, "ARTIFACT_SECRET_DETECTED");
  assert.equal(failure.message, "A generated artifact contains prohibited literal secret data.");
  assert.doesNotMatch(failure.message, /user:pass|mongodb/);
});

test("preserves actionable missing seed runtime fields", () => {
  const error = new Error("seed.js is missing required runtime contract fields: SEED_MAX_DOCS, SEED_COLLECTION_CAPS, seed_summary");
  error.code = "SEED_SCRIPT_INVALID";
  error.failureClass = "IMPLEMENTATION_FAILURE";
  const failure = _test.classifyFailure(error);
  assert.equal(failure.code, "SEED_SCRIPT_INVALID");
  assert.match(failure.message, /SEED_MAX_DOCS, SEED_COLLECTION_CAPS, seed_summary/);
});

test("accepts an exact committed-content direct validation payload", () => {
  const prepared = _test.parseDirectValidation(directRequest());
  assert.equal(prepared.manifest.storage.provider, "github");
  assert.deepEqual(Object.keys(prepared.contents).sort(), ["SEED_README.md", "package.json", "seed.js"]);
});

test("accepts project-layout inputs with numeric POC identity and normalized execution data", () => {
  const prepared = _test.parseDirectValidation(projectDirectRequest());
  assert.equal(prepared.manifest.storage.branch, "nishit-rao-mongodb-com/triage-support");
  assert.equal(prepared.schema.collections[0].name, "users");
  assert.deepEqual(Object.keys(prepared.contents).sort(), ["SEED_README.md", "package.json", "seed.js"]);
});

test("ignores missing malformed and tampered query-pattern inputs", () => {
  const request = projectDirectRequest();
  request.query_patterns_json = "not-json";
  request.normalized_query_patterns_json = "also-not-json";
  const manifest = JSON.parse(request.manifest_json);
  manifest.inputs.query_patterns = { key: "wrong/path.json", sha256: "not-a-digest" };
  request.manifest_json = JSON.stringify(manifest);
  const prepared = _test.parseDirectValidation(request);
  assert.deepEqual(prepared.patterns, { patterns: [] });

  delete request.query_patterns_json;
  delete request.normalized_query_patterns_json;
  assert.deepEqual(_test.parseDirectValidation(request).patterns, { patterns: [] });
});

test("rejects direct validation with wrong branch or committed content", () => {
  const wrongBranch = directRequest();
  wrongBranch.branch = "poc/other";
  assert.throws(() => _test.parseDirectValidation(wrongBranch), /storage identity/);

  const changed = directRequest();
  changed.artifacts["seed.js"] += "\n// changed";
  assert.throws(
    () => _test.parseDirectValidation(changed),
    (error) => error.code === "ARTIFACT_SIZE_MISMATCH" || error.code === "ARTIFACT_HASH_MISMATCH",
  );
});

test("rejects tampered manifest storage and exact specification inputs", () => {
  const wrongStorage = directRequest();
  const storageManifest = JSON.parse(wrongStorage.manifest_json);
  storageManifest.storage.repository = "other/repo";
  wrongStorage.manifest_json = JSON.stringify(storageManifest);
  assert.throws(
    () => _test.parseDirectValidation(wrongStorage),
    (error) => error.code === "ARTIFACT_HASH_MISMATCH",
  );

  const changedSchema = directRequest();
  changedSchema.schema_design_json = JSON.stringify({ collections: [] });
  assert.throws(
    () => _test.parseDirectValidation(changedSchema),
    (error) => error.code === "ARTIFACT_HASH_MISMATCH",
  );

  const wrongArtifactHash = directRequest();
  const hashManifest = JSON.parse(wrongArtifactHash.manifest_json);
  hashManifest.artifacts[0].sha256 = "b".repeat(64);
  wrongArtifactHash.manifest_json = JSON.stringify(hashManifest);
  assert.throws(
    () => _test.parseDirectValidation(wrongArtifactHash),
    (error) => error.code === "ARTIFACT_HASH_MISMATCH",
  );
});

test("rejects unexpected direct validation artifacts", () => {
  const request = directRequest();
  request.artifacts["extra.txt"] = "unexpected";
  assert.throws(() => _test.parseDirectValidation(request), (error) => error.code === "ARTIFACT_MALFORMED");
});