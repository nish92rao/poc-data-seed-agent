"use strict";

const assert = require("node:assert/strict");
const { createRequire } = require("node:module");
const { join, resolve } = require("node:path");
const test = require("node:test");

const GOLDEN_ROOT = resolve(__dirname, "../golden/subscription_billing/v013");
const requireGolden = createRequire(join(GOLDEN_ROOT, "package.json"));
const { MongoClient } = requireGolden("mongodb");

function connectionIdentity(uri) {
  if (typeof uri !== "string" || !/^mongodb(?:\+srv)?:\/\//.test(uri)) {
    throw new Error("Security isolation URIs must be MongoDB connection strings");
  }
  const withoutScheme = uri.replace(/^mongodb(?:\+srv)?:\/\//, "");
  const authority = withoutScheme.split("/", 1)[0];
  const at = authority.lastIndexOf("@");
  const credentials = at >= 0 ? authority.slice(0, at) : "";
  const hosts = (at >= 0 ? authority.slice(at + 1) : authority).toLowerCase();
  const username = credentials.split(":", 1)[0];
  return { hosts, username };
}

function assertDistinctConnections(validationUri, targetUri) {
  const validation = connectionIdentity(validationUri);
  const target = connectionIdentity(targetUri);
  assert.notEqual(validation.hosts, target.hosts, "validation and target MongoDB endpoints must be distinct");
}

test("validation credentials cannot modify the actual target database", { timeout: 30_000 }, async () => {
  const validationUri = process.env.SECURITY_VALIDATION_MONGODB_URI;
  const targetUri = process.env.SECURITY_TARGET_MONGODB_URI;
  assertDistinctConnections(validationUri, targetUri);

  const suffix = `${process.pid.toString(36)}${Date.now().toString(36)}`;
  const databaseName = `security_${suffix}`;
  const sentinel = { _id: `sentinel_${suffix}`, protected: true };
  const validationClient = new MongoClient(validationUri, { serverSelectionTimeoutMS: 5_000 });
  const targetClient = new MongoClient(targetUri, { serverSelectionTimeoutMS: 5_000 });

  try {
    await Promise.all([validationClient.connect(), targetClient.connect()]);
    await targetClient.db(databaseName).collection("sentinel").insertOne(sentinel);

    await validationClient.db(databaseName).collection("sentinel").deleteMany({});
    await validationClient.db(databaseName).dropDatabase();

    assert.deepEqual(
      await targetClient.db(databaseName).collection("sentinel").findOne({ _id: sentinel._id }),
      sentinel,
      "validation-cluster operations must not affect the target-cluster sentinel",
    );
  } finally {
    await Promise.allSettled([
      validationClient.db(databaseName).dropDatabase(),
      targetClient.db(databaseName).dropDatabase(),
    ]);
    await Promise.allSettled([validationClient.close(), targetClient.close()]);
  }
});

module.exports = { assertDistinctConnections, connectionIdentity };