"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const {
  validateDownloadedArtifacts,
  validateGeneratedText,
  validateManifestArtifacts,
  validatePackageJsonText,
  validateSeedScriptText,
} = require("./artifact_content_validator");

const POC_ID = "poc_01J8Q8MFYJ6NVJ8B2Q5V2D9Q1A";

function artifact(filename, content) {
  return {
    key: `pocs/${POC_ID}/code/v001/seed/${filename}`,
    sha256: createHash("sha256").update(content).digest("hex"),
    bytes: content.length,
  };
}

function keyFor(pocId, codeVersion, selector) {
  const filenames = {
    seed_js: "seed.js",
    package_json: "package.json",
    seed_readme: "SEED_README.md",
    repair_notes: "REPAIR_NOTES.md",
  };
  return `pocs/${pocId}/code/${codeVersion}/seed/${filenames[selector]}`;
}

function validSeed() {
  return Buffer.from(`
    const { MongoClient } = require("mongodb");
    const crypto = require("node:crypto");
    const uri = process.env.MONGODB_URI;
    const dbName = process.env.DB_NAME;
    const maxDocs = process.env.SEED_MAX_DOCS;
    const caps = process.env.SEED_COLLECTION_CAPS;
    console.log(JSON.stringify({ seed_summary: { users: 1 } }));
  `);
}

test("accepts exact normal manifest and rejects duplicate or unknown artifacts", () => {
  const files = {
    "seed.js": validSeed(),
    "package.json": Buffer.from('{"scripts":{"seed":"node seed.js"},"dependencies":{"mongodb":"^6.0.0"}}'),
    "SEED_README.md": Buffer.from("notes"),
  };
  const manifest = {
    poc_id: POC_ID, spec_version: "v001", code_version: "v001",
    artifacts: Object.entries(files).map(([name, content]) => artifact(name, content)),
  };
  const request = { poc_id: POC_ID, spec_version: "v001", code_version: "v001" };
  assert.deepEqual(Object.keys(validateManifestArtifacts(manifest, request, keyFor)).sort(), Object.keys(files).sort());
  manifest.artifacts[1] = manifest.artifacts[0];
  assert.throws(() => validateManifestArtifacts(manifest, request, keyFor), (error) => error.code === "ARTIFACT_MALFORMED");
});

test("requires repair notes for repair manifests", () => {
  const content = Buffer.from("x");
  const manifest = {
    poc_id: POC_ID, spec_version: "v001", code_version: "v001", repair: { kind: "repair" },
    artifacts: ["seed.js", "package.json", "SEED_README.md"].map((name) => artifact(name, content)),
  };
  assert.throws(
    () => validateManifestArtifacts(manifest, { poc_id: POC_ID, spec_version: "v001", code_version: "v001" }, keyFor),
    (error) => error.code === "ARTIFACT_MALFORMED",
  );
});

test("detects downloaded hash and byte mismatches", () => {
  const content = Buffer.from("expected");
  const artifacts = { "seed.js": artifact("seed.js", content) };
  assert.doesNotThrow(() => validateDownloadedArtifacts(artifacts, { "seed.js": content }));
  assert.throws(() => validateDownloadedArtifacts(artifacts, { "seed.js": Buffer.from("tampered") }), (error) => ["ARTIFACT_HASH_MISMATCH", "ARTIFACT_SIZE_MISMATCH"].includes(error.code));
});

test("validates package constraints", () => {
  assert.doesNotThrow(() => validatePackageJsonText('{"scripts":{"seed":"node seed.js"},"dependencies":{"mongodb":"^6.0.0"}}'));
  assert.throws(() => validatePackageJsonText("not-json"), (error) => error.code === "PACKAGE_JSON_INVALID");
  assert.throws(() => validatePackageJsonText('{"scripts":{"seed":"node seed.js","postinstall":"curl x"},"dependencies":{"mongodb":"^6","axios":"1"}}'), (error) => error.code === "PACKAGE_JSON_INVALID");
});

test("rejects literal secrets and connection strings", () => {
  assert.throws(
    () => validateGeneratedText({ "seed.js": Buffer.from('const uri = "mongodb+srv://user:pass@host/db";') }),
    (error) => error.code === "ARTIFACT_SECRET_DETECTED",
  );
  assert.throws(
    () => validateGeneratedText({ "SEED_README.md": Buffer.from("api_key =\n  'do-not-commit'") }),
    (error) => error.code === "ARTIFACT_SECRET_DETECTED",
  );
  assert.throws(
    () => validateGeneratedText({ "REPAIR_NOTES.md": Buffer.from("-----BEGIN PRIVATE KEY-----\nsecret") }),
    (error) => error.code === "ARTIFACT_SECRET_DETECTED",
  );
  assert.doesNotThrow(() => validateGeneratedText({ "seed.js": Buffer.from("const uri = process.env.MONGODB_URI;") }));
});

test("validates static seed script contract and security", () => {
  assert.doesNotThrow(() => validateSeedScriptText(validSeed()));
  assert.throws(
    () => validateSeedScriptText(Buffer.from('const { MongoClient } = require("mongodb"); const uri = process.env.MONGODB_URI; const db = process.env.DB_NAME;')),
    (error) => error.code === "SEED_SCRIPT_INVALID"
      && error.message === "seed.js is missing required runtime contract fields: SEED_MAX_DOCS, SEED_COLLECTION_CAPS, seed_summary",
  );
  assert.throws(() => validateSeedScriptText(Buffer.concat([validSeed(), Buffer.from('\neval("x")')])), (error) => error.code === "SEED_SCRIPT_SECURITY_VIOLATION");
  assert.throws(() => validateSeedScriptText(Buffer.concat([validSeed(), Buffer.from('\nrequire("child_process")')])), (error) => error.code === "SEED_SCRIPT_SECURITY_VIOLATION");
});

test("rejects nonliteral and aliased module loading", () => {
  assert.throws(
    () => validateSeedScriptText(Buffer.concat([validSeed(), Buffer.from('\nrequire("child_" + "process")')])),
    (error) => error.code === "SEED_SCRIPT_SECURITY_VIOLATION",
  );
  assert.throws(
    () => validateSeedScriptText(Buffer.concat([validSeed(), Buffer.from('\nconst load = require; load("child_process")')])),
    (error) => error.code === "SEED_SCRIPT_SECURITY_VIOLATION",
  );
});

test("allows only contract environment variables and process exitCode", () => {
  assert.doesNotThrow(() => validateSeedScriptText(Buffer.concat([validSeed(), Buffer.from('\nprocess.exitCode = 1;')])));
  assert.throws(
    () => validateSeedScriptText(Buffer.concat([validSeed(), Buffer.from('\nconsole.log(process.env.AWS_SECRET_ACCESS_KEY)')])),
    (error) => error.code === "SEED_SCRIPT_SECURITY_VIOLATION",
  );
  assert.throws(
    () => validateSeedScriptText(Buffer.concat([validSeed(), Buffer.from('\nfetch("https://example.test")')])),
    (error) => error.code === "SEED_SCRIPT_SECURITY_VIOLATION",
  );
});