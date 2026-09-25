"use strict";

const assert = require("node:assert/strict");
const { execFile } = require("node:child_process");
const { createRequire } = require("node:module");
const { performance } = require("node:perf_hooks");
const { join, resolve } = require("node:path");
const test = require("node:test");
const { promisify } = require("node:util");
const { readFileSync } = require("node:fs");
const { _test: validator } = require("../../resources/validator/handler");

const execFileAsync = promisify(execFile);
const GOLDEN_ROOT = resolve(__dirname, "../golden/subscription_billing/v013");
const requireGolden = createRequire(join(GOLDEN_ROOT, "package.json"));
const { MongoClient, ObjectId } = requireGolden("mongodb");
const schema = JSON.parse(readFileSync(join(GOLDEN_ROOT, "schema_design.json"), "utf8"));

const EXPECTED_COUNTS = {
  accounts: 3,
  plans: 1,
  subscriptions: 6,
  invoices: 12,
  payments: 9,
};
const MAX_ACCEPTANCE_MILLISECONDS = 60_000;

function integrationUri() {
  const uri = process.env.INTEGRATION_MONGODB_URI;
  if (!uri) throw new Error("INTEGRATION_MONGODB_URI is required");
  const parsed = new URL(uri);
  const localHosts = new Set(["127.0.0.1", "localhost", "[::1]"]);
  if (!localHosts.has(parsed.hostname) && process.env.ALLOW_NONLOCAL_INTEGRATION_MONGODB !== "1") {
    throw new Error("Integration tests require localhost MongoDB unless ALLOW_NONLOCAL_INTEGRATION_MONGODB=1");
  }
  return uri;
}

function databaseName() {
  return `poc_seed_golden_${process.pid}_${Date.now()}`;
}

async function runSeed(uri, database) {
  const { stdout, stderr } = await execFileAsync(process.execPath, [join(GOLDEN_ROOT, "seed.js")], {
    cwd: GOLDEN_ROOT,
    env: {
      ...process.env,
      MONGODB_URI: uri,
      DB_NAME: database,
      SEED_MAX_DOCS: "100",
      SEED_COLLECTION_CAPS: JSON.stringify(EXPECTED_COUNTS),
    },
    timeout: 30_000,
    maxBuffer: 1024 * 1024,
  });
  assert.equal(stderr.trim(), "");
  const lines = stdout.trim().split("\n").filter(Boolean);
  assert.equal(lines.length, 1, "seed.js must emit exactly one summary line");
  const result = JSON.parse(lines[0]);
  assert.deepEqual(result.seed_summary, EXPECTED_COUNTS);
  return result.seed_summary;
}

function keyEntries(key) {
  return Object.entries(key).map(([field, direction]) => [field, Number(direction)]);
}

async function assertIndexes(db) {
  for (const collection of schema.collections) {
    const actual = await db.collection(collection.name).listIndexes().toArray();
    for (const expected of collection.indexes || []) {
      const match = actual.find((index) => (
        JSON.stringify(keyEntries(index.key)) === JSON.stringify(keyEntries(expected.keys))
      ));
      assert.ok(match, `Missing index on ${collection.name}: ${JSON.stringify(expected.keys)}`);
      assert.equal(Boolean(match.unique), Boolean(expected.unique), `Wrong uniqueness for ${collection.name}`);
    }
  }
}

async function assertFieldSchemas(db) {
  for (const collection of schema.collections) {
    const documents = await db.collection(collection.name).find({}).toArray();
    for (const document of documents) validator.validateDocumentFields(document, collection.fields, collection.name);
  }
}

function canonicalize(value) {
  if (value instanceof ObjectId) return { $oid: value.toHexString() };
  if (value instanceof Date) return { $date: value.toISOString() };
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonicalize(value[key])]),
    );
  }
  return value;
}

async function databaseSnapshot(db) {
  const snapshot = {};
  for (const collection of schema.collections) {
    const documents = await db.collection(collection.name).find({}).sort({ _id: 1 }).toArray();
    const indexes = await db.collection(collection.name).listIndexes().toArray();
    snapshot[collection.name] = {
      documents: canonicalize(documents),
      indexes: indexes
        .map((index) => ({ key: keyEntries(index.key), unique: Boolean(index.unique) }))
        .sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right))),
    };
  }
  return snapshot;
}

async function assertCounts(db) {
  const actual = {};
  for (const collection of schema.collections) {
    actual[collection.name] = await db.collection(collection.name).countDocuments({});
  }
  assert.deepEqual(actual, EXPECTED_COUNTS);
}

test("golden capped validation is deterministic and completes within 60 seconds", { timeout: 70_000 }, async () => {
  const started = performance.now();
  const uri = integrationUri();
  const database = databaseName();
  assert.match(database, /^poc_seed_golden_/);
  const client = new MongoClient(uri, { serverSelectionTimeoutMS: 5_000, connectTimeoutMS: 5_000 });
  let failure;

  try {
    await client.connect();
    const db = client.db(database);

    await runSeed(uri, database);
    await assertCounts(db);
    await assertIndexes(db);
    await assertFieldSchemas(db);
    const firstSnapshot = await databaseSnapshot(db);

    await runSeed(uri, database);
    await assertCounts(db);
    await assertIndexes(db);
    await assertFieldSchemas(db);
    const secondSnapshot = await databaseSnapshot(db);

    assert.deepEqual(secondSnapshot, firstSnapshot, "rerun must reproduce documents and indexes exactly");
  } catch (error) {
    failure = error;
  } finally {
    try {
      await client.db(database).dropDatabase();
    } catch (cleanupError) {
      failure ||= cleanupError;
    }
    await client.close().catch((error) => { failure ||= error; });
  }

  const elapsed = performance.now() - started;
  assert.ok(elapsed < MAX_ACCEPTANCE_MILLISECONDS, `capped validation took ${Math.round(elapsed)}ms`);
  if (failure) throw failure;
});
