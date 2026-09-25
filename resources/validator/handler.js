"use strict";

const { createHash, createHmac, timingSafeEqual } = require("node:crypto");
const { mkdtemp, rm, writeFile } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const { GetSecretValueCommand, SecretsManagerClient } = require("@aws-sdk/client-secrets-manager");
const { MongoClient } = require("mongodb");
const {
  SIZE_LIMITS,
  validateDownloadedArtifacts,
  validateGeneratedText,
  validateManifestArtifacts,
  validatePackageJsonText,
  validateSeedScriptText,
} = require("./artifact_content_validator");

const execFileAsync = promisify(execFile);
const MAX_REQUEST_AGE_SECONDS = 300;
const HMAC_SECRET_CACHE_SECONDS = MAX_REQUEST_AGE_SECONDS;
const ULID_PATTERN = "[0-9A-HJKMNPQRSTVWXYZ]{26}";
const ALLOWED_FILENAMES = new Set(["seed.js", "package.json", "SEED_README.md"]);
const REPAIR_NOTES_FILENAME = "REPAIR_NOTES.md";
const SEED_ARTIFACTS = new Map([
  ["seed_js", "seed/seed.js"],
  ["package_json", "seed/package.json"],
  ["seed_readme", "seed/SEED_README.md"],
  ["repair_notes", "seed/REPAIR_NOTES.md"],
  ["seed_manifest", "seed/seed.manifest.json"],
]);
const secrets = new SecretsManagerClient({});
let cachedHmacSecret;
let cachedHmacSecretAt = 0;

function response(statusCode, body) {
  return {
    statusCode,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  };
}

function header(event, name) {
  const headers = event.headers || {};
  return headers[name] || headers[name.toLowerCase()] || headers[name.toUpperCase()];
}

function targetDatabaseName(pocId) {
  const name = pocId.replace(/[^A-Za-z0-9_]/g, "_");
  if (Buffer.byteLength(name, "utf8") <= 38) return name;
  const digest = createHash("sha256").update(name).digest("hex").slice(0, 12);
  return `${name.slice(0, 25)}_${digest}`;
}

function validationDatabaseName(pocId, runId) {
  const target = targetDatabaseName(pocId);
  const digest = createHash("sha256").update(`${target}\u0000${runId}`).digest("hex").slice(0, 20);
  return `${target.slice(0, 17)}_${digest}`;
}

function validationCaps(schema) {
  return Object.fromEntries(schema.collections.map((collection) => {
    const count = collection.seed?.count ?? collection.seed_requirements?.max_docs ?? 0;
    return [collection.name, Math.min(Math.ceil(count * 0.05), 100)];
  }));
}

function validationDocumentBudget(caps) {
  return Object.values(caps).reduce((total, cap) => total + cap, 0);
}

function ensureIdentifier(value, name) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new Error(`${name} must contain only letters, numbers, underscores, and hyphens`);
  }
}

function ensureProductionId(value, prefix, name) {
  if (typeof value !== "string" || !new RegExp(`^${prefix}_${ULID_PATTERN}$`).test(value)) {
    throw new Error(`${name} must use its prefixed ULID format`);
  }
}

function ensureRunKey(key, runId) {
  if (typeof key !== "string" || !key.startsWith("pocs/") || key.includes("..")) {
    throw new Error("Artifact keys must be contained in the canonical POC prefix");
  }
}

function seedArtifactKey(pocId, codeVersion, artifact) {
  ensureProductionId(pocId, "poc", "poc_id");
  ensureIdentifier(codeVersion, "code_version");
  const suffix = SEED_ARTIFACTS.get(artifact);
  if (!suffix) throw new Error("Unsupported seed artifact");
  return `pocs/${pocId}/code/${codeVersion}/${suffix}`;
}

function projectSeedArtifactKey(pocId, codeVersion, artifact) {
  ensureIdentifier(pocId, "poc_id");
  ensureIdentifier(codeVersion, "code_version");
  const suffix = SEED_ARTIFACTS.get(artifact);
  if (!suffix) throw new Error("Unsupported seed artifact");
  return `seed/${codeVersion}/${suffix.replace(/^seed\//, "")}`;
}

function parseSeedSummary(stdout) {
  const lines = stdout.trim().split("\n").filter(Boolean);
  if (lines.length !== 1) throw new Error("seed.js must print exactly one JSON summary line");
  const parsed = JSON.parse(lines[0]);
  if (!parsed.seed_summary || typeof parsed.seed_summary !== "object" || Array.isArray(parsed.seed_summary)) {
    throw new Error("seed.js output must contain an object seed_summary");
  }
  return parsed.seed_summary;
}

function requestContradiction(message) {
  const error = new Error(message);
  error.failureClass = "REQUEST_CONTRADICTION";
  return error;
}

function sanitizeFailureMessage(value) {
  return String(value || "")
    .replace(/^\s*at\s+.*$/gm, "")
    .replace(/\b[a-z][a-z0-9+.-]*:\/\/\S+/gi, "[redacted URI]")
    .replace(/\barn:aws(?:-[a-z]+)?:\S+/g, "[redacted AWS ARN]")
    .replace(/\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b/g, "[redacted network address]")
    .replace(/\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+/gi, "$1=[redacted]")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 500);
}

function classifyFailure(error) {
  if (error?.code === "VALIDATION_CLEANUP_FAILED") {
    return { code: "VALIDATION_CLEANUP_FAILED", failure_class: "VALIDATOR_INFRASTRUCTURE_FAILURE", message: "Validation database cleanup failed.", retryable: true };
  }
  if (error?.killed || error?.code === "ETIMEDOUT") {
    return { code: "VALIDATION_TIMEOUT", failure_class: "VALIDATOR_INFRASTRUCTURE_FAILURE", message: "Seed validation timed out.", retryable: true };
  }
  if (error?.failureClass === "REQUEST_CONTRADICTION") {
    return { code: "VALIDATION_FAILED", failure_class: "REQUEST_CONTRADICTION", message: sanitizeFailureMessage(error.message) || "The seed requirements are contradictory.", retryable: false };
  }
  const artifactFailures = {
    ARTIFACT_MALFORMED: ["IMPLEMENTATION_FAILURE", "The generated artifact bundle is malformed.", false],
    PACKAGE_JSON_INVALID: ["IMPLEMENTATION_FAILURE", "The generated package.json is invalid.", false],
    SEED_SCRIPT_INVALID: ["IMPLEMENTATION_FAILURE", sanitizeFailureMessage(error.message) || "The generated seed script is invalid.", false],
    ARTIFACT_SIZE_EXCEEDED: ["IMPLEMENTATION_FAILURE", "A generated artifact exceeds its size limit.", false],
    ARTIFACT_HASH_MISMATCH: ["ARTIFACT_INTEGRITY_FAILURE", "Artifact content does not match its manifest.", false],
    ARTIFACT_SIZE_MISMATCH: ["ARTIFACT_INTEGRITY_FAILURE", "Artifact size does not match its manifest.", false],
    ARTIFACT_SECRET_DETECTED: ["ARTIFACT_SECURITY_FAILURE", "A generated artifact contains prohibited literal secret data.", false],
    SEED_SCRIPT_SECURITY_VIOLATION: ["ARTIFACT_SECURITY_FAILURE", "The generated seed script violates the security policy.", false],
  };
  if (artifactFailures[error?.code]) {
    const [failureClass, message, retryable] = artifactFailures[error.code];
    return { code: error.code, failure_class: failureClass, message, retryable };
  }
  if ((error?.message || "").startsWith("seed.js failed:")) {
    return { code: "VALIDATION_FAILED", failure_class: "IMPLEMENTATION_FAILURE", message: "The generated seed script exited with an error.", retryable: false };
  }
  const safeImplementationMessage = /^(Generated package\.json|seed\.js must |seed\.js output |Collection |Missing declared index|Broken relationship|Query pattern |Forced validation failure)/.test(error?.message || "");
  if (error?.failureClass === "IMPLEMENTATION_FAILURE" || safeImplementationMessage) {
    return { code: "VALIDATION_FAILED", failure_class: "IMPLEMENTATION_FAILURE", message: sanitizeFailureMessage(error.message) || "The generated seed bundle failed validation.", retryable: false };
  }
  return { code: "VALIDATOR_INFRASTRUCTURE_FAILURE", failure_class: "VALIDATOR_INFRASTRUCTURE_FAILURE", message: "The seed validator is temporarily unavailable.", retryable: true };
}

function forcedImplementationFailure(enabled = process.env.VALIDATION_TEST_FORCE_IMPLEMENTATION_FAILURE) {
  if (enabled !== "true") return null;
  const error = new Error("Forced validation failure for bounded-repair integration test");
  error.failureClass = "IMPLEMENTATION_FAILURE";
  return error;
}

function validateInputContract(schema, patterns) {
  const collections = schema.collections || [];
  const collectionByName = new Map();
  for (const collection of collections) {
    if (!collection?.name || collectionByName.has(collection.name)) {
      throw requestContradiction(`Collection names must be unique: ${collection?.name || "missing name"}`);
    }
    collectionByName.set(collection.name, collection);
  }
  for (const collection of collections) {
    const fieldRoots = new Set((collection.fields || []).map((field) => field.name?.split("[")[0].split(".")[0]));
    const relationshipReferences = new Map();
    for (const relationship of collection.relationships || []) {
      const localRoot = relationship.field?.split("[")[0].split(".")[0];
      const [targetCollection, targetField] = (relationship.references || "").split(".");
      const target = collectionByName.get(targetCollection);
      const targetFields = new Set((target?.fields || []).map((field) => field.name));
      if (!localRoot || !fieldRoots.has(localRoot) || !target || !targetFields.has(targetField)) {
        throw requestContradiction(`Invalid relationship ${collection.name}.${relationship.field || "missing field"}`);
      }
      const existingReference = relationshipReferences.get(relationship.field);
      if (existingReference && existingReference !== relationship.references) {
        throw requestContradiction(`Conflicting relationship declarations for ${collection.name}.${relationship.field}`);
      }
      relationshipReferences.set(relationship.field, relationship.references);
    }
  }
  for (const pattern of patterns.patterns || []) {
    if (!collectionByName.has(pattern.collection)) {
      throw requestContradiction(`Query pattern ${pattern.id || "unknown"} references unknown collection ${pattern.collection}`);
    }
  }
}


async function getHmacSecret({ forceRefresh = false, now = Date.now, secretClient = secrets } = {}) {
  const currentTime = now();
  if (!forceRefresh && cachedHmacSecret
    && currentTime - cachedHmacSecretAt < HMAC_SECRET_CACHE_SECONDS * 1000) {
    return cachedHmacSecret;
  }
  const secretId = process.env.VALIDATOR_AUTH_SECRET_ARN;
  if (!secretId) throw new Error("VALIDATOR_AUTH_SECRET_ARN is not configured");
  const result = await secretClient.send(new GetSecretValueCommand({ SecretId: secretId }));
  const raw = result.SecretString || Buffer.from(result.SecretBinary, "base64").toString("utf8");
  let secret;
  try {
    secret = JSON.parse(raw).hmac_secret;
  } catch {
    secret = raw;
  }
  if (typeof secret !== "string" || secret.length < 32) {
    throw new Error("Validator HMAC secret is invalid");
  }
  cachedHmacSecret = secret;
  cachedHmacSecretAt = currentTime;
  return cachedHmacSecret;
}

function resetHmacSecretCache() {
  cachedHmacSecret = undefined;
  cachedHmacSecretAt = 0;
}

function verifyRequestSignature(secret, timestamp, rawBody, signature) {
  if (typeof signature !== "string" || !/^[0-9a-f]{64}$/i.test(signature)) return false;
  const expected = createHmac("sha256", secret).update(`${timestamp}.${rawBody}`).digest("hex");
  return timingSafeEqual(Buffer.from(signature, "hex"), Buffer.from(expected, "hex"));
}

async function authenticate(event, rawBody, { now = Date.now, getSecret = getHmacSecret } = {}) {
  const timestamp = header(event, "x-validator-timestamp");
  const signature = header(event, "x-validator-signature");
  if (!timestamp || !signature || !/^\d+$/.test(timestamp)) return false;
  if (!Number.isSafeInteger(Number(timestamp))
    || Math.abs(now() - Number(timestamp) * 1000) > MAX_REQUEST_AGE_SECONDS * 1000) return false;
  const secret = await getSecret({ now });
  if (verifyRequestSignature(secret, timestamp, rawBody, signature)) return true;
  const refreshedSecret = await getSecret({ forceRefresh: true, now });
  return verifyRequestSignature(refreshedSecret, timestamp, rawBody, signature);
}

function substitutePlaceholders(value, document, fieldName = "") {
  if (Array.isArray(value)) return value.map((entry) => substitutePlaceholders(entry, document, fieldName));
  if (!value || typeof value !== "object") {
    if (typeof value === "string" && value.startsWith("<") && value.endsWith(">")) {
      if (value.includes("hour|day|week")) return "day";
      if (value.includes("date")) return document[fieldName] instanceof Date ? document[fieldName] : new Date("2100-01-01T00:00:00Z");
      if (value.includes("object_id")) return document[fieldName] || document._id;
      return document[fieldName];
    }
    return value;
  }
  const result = Object.fromEntries(Object.entries(value)
    .map(([key, entry]) => [key, substitutePlaceholders(entry, document, key.startsWith("$") ? fieldName : key)])
    .filter(([, entry]) => entry !== undefined));
  return Object.keys(result).length ? result : undefined;
}

async function verifyIndexes(db, schema) {
  for (const collection of schema.collections) {
    const actual = await db.collection(collection.name).listIndexes().toArray();
    for (const index of collection.indexes || []) {
      const keys = index.keys || Object.fromEntries((index.fields || []).map((field) => [field, 1]));
      const found = actual.some((candidate) => JSON.stringify(candidate.key) === JSON.stringify(keys)
        && Boolean(candidate.unique) === Boolean(index.unique));
      if (!found) throw new Error(`Missing declared index on ${collection.name}: ${JSON.stringify(keys)}`);
    }
  }
}

async function verifyRelationships(db, schema) {
  for (const collection of schema.collections) {
    for (const relationship of collection.relationships || []) {
      const [targetCollection, targetField] = relationship.references.split(".");
      const localField = relationship.field.replace("[]", "");
      const missing = await db.collection(collection.name).aggregate([
        { $unwind: { path: `$${localField.split(".")[0]}`, preserveNullAndEmptyArrays: false } },
        { $lookup: { from: targetCollection, localField, foreignField: targetField, as: "reference" } },
        { $match: { reference: { $eq: [] } } },
        { $limit: 1 },
      ]).toArray();
      if (missing.length) throw new Error(`Broken relationship ${collection.name}.${relationship.field}`);
    }
  }
}

async function verifyQueryPatterns(db, patterns) {
  let staticVectorPatterns = 0;
  for (const pattern of patterns.patterns || []) {
    if (pattern.validation_mode === "static_vector") {
      const vectorStage = (pattern.pipeline || []).find((stage) => stage?.$vectorSearch)?.$vectorSearch;
      if (!vectorStage || typeof vectorStage.index !== "string" || typeof vectorStage.path !== "string"
        || !Number.isSafeInteger(pattern.vector_dimensions) || pattern.vector_dimensions <= 0) {
        throw new Error(`Query pattern ${pattern.id || pattern.description} has an invalid vector search contract`);
      }
      staticVectorPatterns += 1;
      continue;
    }
    try {
      const collection = db.collection(pattern.collection);
      const sample = await collection.findOne({});
      if (!sample) throw new Error("has no seed data");
      if (pattern.operation === "aggregate") {
        await collection.aggregate(substitutePlaceholders(pattern.pipeline || [], sample)).limit(1).toArray();
        continue;
      }
      const match = substitutePlaceholders(pattern.match || {}, sample);
      let cursor = collection.find(match);
      if (pattern.sort) cursor = cursor.sort(pattern.sort);
      await cursor.limit(pattern.limit || 1).toArray();
    } catch (error) {
      const failure = new Error(`Query pattern ${pattern.id || pattern.description} failed validation: ${sanitizeFailureMessage(error.message)}`);
      failure.failureClass = "IMPLEMENTATION_FAILURE";
      throw failure;
    }
  }
  return { runtime_executed: (patterns.patterns || []).length - staticVectorPatterns, static_vector_validated: staticVectorPatterns };
}

function validateVectorSeedContract(seedScript, patterns) {
  const text = seedScript.toString("utf8");
  for (const pattern of patterns.patterns || []) {
    if (pattern.validation_mode !== "static_vector") continue;
    const vector = (pattern.pipeline || []).find((stage) => stage?.$vectorSearch)?.$vectorSearch;
    if (!vector || !text.includes("createSearchIndex") || !text.includes("process.env.SEED_SKIP_SEARCH_INDEXES") || !text.includes(vector.index)
      || !text.includes(vector.path) || !text.includes(String(pattern.vector_dimensions))) {
      const error = new Error(`Query pattern ${pattern.id || "unknown"} requires a matching vector search index declaration`);
      error.code = "SEED_SCRIPT_INVALID";
      error.failureClass = "IMPLEMENTATION_FAILURE";
      throw error;
    }
  }
}

function parseDirectValidation(request) {
  ensureIdentifier(request.poc_id, "poc_id");
  ensureProductionId(request.run_id, "run", "run_id");
  ensureProductionId(request.task_id, "task", "task_id");
  ensureIdentifier(request.trace_id, "trace_id");
  ensureIdentifier(request.spec_version, "spec_version");
  ensureIdentifier(request.code_version, "code_version");
  if (typeof request.mongodb_uri !== "string" || !request.mongodb_uri.startsWith("mongodb")) {
    throw new Error("mongodb_uri is required");
  }
  if (typeof request.repository !== "string" || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(request.repository)) {
    throw new Error("repository must use owner/repo format");
  }
  if (typeof request.branch !== "string" || !/^[A-Za-z0-9._/-]+$/.test(request.branch)
    || request.branch.includes("..") || request.branch.includes("//")
    || typeof request.source_commit_sha !== "string" || !/^[0-9a-f]{40}$/.test(request.source_commit_sha)) {
    throw new Error("GitHub branch or source commit is invalid");
  }
  const projectLayout = typeof request.data_model_json === "string";
  const requiredStrings = projectLayout
    ? ["manifest_json", "data_model_json", "normalized_data_model_json", "query_patterns_json", "normalized_query_patterns_json"]
    : ["manifest_json", "schema_design_json", "query_patterns_json"];
  if (requiredStrings.some((field) => typeof request[field] !== "string")) {
    throw new Error("Direct validation requires exact JSON file contents");
  }
  if (!request.artifacts || typeof request.artifacts !== "object" || Array.isArray(request.artifacts)) {
    throw new Error("Direct validation artifacts must be an object");
  }
  const manifestContent = Buffer.from(request.manifest_json, "utf8");
  const schemaContent = Buffer.from(projectLayout ? request.data_model_json : request.schema_design_json, "utf8");
  const patternsContent = Buffer.from(request.query_patterns_json, "utf8");
  if (manifestContent.length > 256 * 1024) {
    const error = new Error("seed.manifest.json exceeds its size limit");
    error.code = "ARTIFACT_SIZE_EXCEEDED";
    error.failureClass = "IMPLEMENTATION_FAILURE";
    throw error;
  }
  let manifest;
  let schema;
  let patterns;
  try {
    manifest = JSON.parse(request.manifest_json);
    schema = JSON.parse(projectLayout ? request.normalized_data_model_json : request.schema_design_json);
    patterns = JSON.parse(projectLayout ? request.normalized_query_patterns_json : request.query_patterns_json);
  } catch {
    const error = new Error("A required JSON artifact is malformed");
    error.code = "ARTIFACT_MALFORMED";
    error.failureClass = "IMPLEMENTATION_FAILURE";
    throw error;
  }
  if (manifest.storage?.provider !== "github" || manifest.storage.repository !== request.repository || manifest.storage.branch !== request.branch) {
    const error = new Error("Manifest GitHub storage identity does not match the validation request");
    error.code = "ARTIFACT_HASH_MISMATCH";
    error.failureClass = "ARTIFACT_INTEGRITY_FAILURE";
    throw error;
  }
  const correlation = manifest.correlation;
  if (!correlation || correlation.poc_id !== request.poc_id || correlation.run_id !== request.run_id
    || correlation.task_id !== request.task_id || correlation.trace_id !== request.trace_id
    || correlation.producer !== "poc-data-seed") {
    const error = new Error("Manifest correlation does not match the validation request");
    error.code = "ARTIFACT_HASH_MISMATCH";
    error.failureClass = "ARTIFACT_INTEGRITY_FAILURE";
    throw error;
  }
  const expectedInputs = projectLayout ? [
    ["data_model", "spec_architect/data_model.json", schemaContent],
    ["query_patterns", "spec_architect/query_patterns.json", patternsContent],
  ] : [
    ["schema_design", `pocs/${request.poc_id}/spec/${request.spec_version}/schema_design.json`, schemaContent],
    ["query_patterns", `pocs/${request.poc_id}/spec/${request.spec_version}/query_patterns.json`, patternsContent],
  ];
  for (const [name, key, content] of expectedInputs) {
    const declared = manifest.inputs?.[name];
    const digest = createHash("sha256").update(content).digest("hex");
    if (declared?.key !== key || declared?.sha256 !== digest) {
      const error = new Error("Committed input content does not match the manifest");
      error.code = "ARTIFACT_HASH_MISMATCH";
      error.failureClass = "ARTIFACT_INTEGRITY_FAILURE";
      throw error;
    }
  }
  validateInputContract(schema, patterns);
  const validationRequest = { ...request, project_layout: projectLayout };
  const artifacts = validateManifestArtifacts(manifest, validationRequest, projectLayout ? projectSeedArtifactKey : seedArtifactKey);
  const contents = {};
  for (const filename of Object.keys(artifacts)) {
    const value = request.artifacts[filename];
    if (typeof value !== "string") {
      const error = new Error(`Direct validation artifact is missing: ${filename}`);
      error.code = "ARTIFACT_MALFORMED";
      error.failureClass = "IMPLEMENTATION_FAILURE";
      throw error;
    }
    contents[filename] = Buffer.from(value, "utf8");
  }
  if (Object.keys(request.artifacts).some((filename) => !artifacts[filename])) {
    const error = new Error("Direct validation contains an unexpected artifact");
    error.code = "ARTIFACT_MALFORMED";
    error.failureClass = "IMPLEMENTATION_FAILURE";
    throw error;
  }
  validateDownloadedArtifacts(artifacts, contents);
  return { manifest, schema, patterns, artifacts, contents };
}

async function executePreparedValidation(request, schema, patterns, contents) {
  const forcedFailure = forcedImplementationFailure();
  if (forcedFailure) throw forcedFailure;
  validateGeneratedText(contents);
  validatePackageJsonText(contents["package.json"].toString("utf8"));
  validateSeedScriptText(contents["seed.js"]);
  validateVectorSeedContract(contents["seed.js"], patterns);
  const directory = await mkdtemp(join(tmpdir(), "seed-validator-"));
  const databaseName = validationDatabaseName(request.poc_id, request.run_id);
  const caps = validationCaps(schema);
  let client;
  try {
    for (const [filename, content] of Object.entries(contents)) {
      await writeFile(join(directory, filename), content);
    }
    try {
      await execFileAsync(process.execPath, ["--check", "seed.js"], { cwd: directory, timeout: 5000, maxBuffer: 256 * 1024 });
    } catch {
      const error = new Error("seed.js has invalid JavaScript syntax");
      error.code = "SEED_SCRIPT_INVALID";
      error.failureClass = "IMPLEMENTATION_FAILURE";
      throw error;
    }
    client = new MongoClient(request.mongodb_uri, { serverSelectionTimeoutMS: 5000, connectTimeoutMS: 5000 });
    let stdout;
    try {
      ({ stdout } = await execFileAsync(process.execPath, ["seed.js"], {
        cwd: directory,
        timeout: 15000,
        env: {
          PATH: process.env.PATH,
          NODE_PATH: process.env.LAMBDA_TASK_ROOT ? join(process.env.LAMBDA_TASK_ROOT, "node_modules") : process.env.NODE_PATH,
          MONGODB_URI: request.mongodb_uri,
          DB_NAME: databaseName,
          SEED_MAX_DOCS: String(validationDocumentBudget(caps)),
          SEED_COLLECTION_CAPS: JSON.stringify(caps),
          SEED_SKIP_SEARCH_INDEXES: "1",
        },
        maxBuffer: 1024 * 1024,
      }));
    } catch (execError) {
      const stderrText = (execError.stderr || "").toString().trim().slice(0, 2000);
      const message = stderrText
        ? `seed.js failed: ${stderrText}`
        : execError.killed || execError.signal
          ? "seed.js timed out with no output; it likely hung connecting to MONGODB_URI (check Atlas network access allows this Lambda's egress IP)"
          : `seed.js failed: ${execError.message}`;
      const augmented = new Error(message);
      augmented.killed = execError.killed;
      augmented.code = execError.code;
      throw augmented;
    }
    const summary = parseSeedSummary(stdout);
    for (const [name, cap] of Object.entries(caps)) {
      if (summary[name] > cap) throw new Error(`Collection ${name} exceeds its validation cap`);
    }
    await client.connect();
    const db = client.db(databaseName);
    const search_index_validation = await createValidationSearchIndexes(db, patterns);
    await verifyIndexes(db, schema);
    await verifyRelationships(db, schema);
    const query_validation = await verifyQueryPatterns(db, patterns);
    return { status: "succeeded", database_name: databaseName, target_database_name: targetDatabaseName(request.poc_id), caps, seed_summary: summary, query_validation, search_index_validation };
  } finally {
    let cleanupFailure;
    if (client) {
      try {
        await cleanupValidationDatabase(client, databaseName, patterns);
      } catch (error) {
        cleanupFailure = error;
      }
    }
    await rm(directory, { recursive: true, force: true });
    if (cleanupFailure) throw cleanupFailure;
  }
}

function declaredSearchIndexes(patterns) {
  const declared = new Map();
  for (const pattern of patterns.patterns || []) {
    if (pattern.validation_mode !== "static_vector" || typeof pattern.collection !== "string") continue;
    const vector = (pattern.pipeline || []).find((stage) => stage?.$vectorSearch)?.$vectorSearch;
    if (!vector || typeof vector.index !== "string") continue;
    if (!declared.has(pattern.collection)) declared.set(pattern.collection, new Set());
    declared.get(pattern.collection).add(vector.index);
  }
  return declared;
}

async function createValidationSearchIndexes(db, patterns) {
  const created = [];
  for (const pattern of patterns.patterns || []) {
    if (pattern.validation_mode !== "static_vector") continue;
    const vector = (pattern.pipeline || []).find((stage) => stage?.$vectorSearch)?.$vectorSearch;
    if (!vector) continue;
    const collection = db.collection(pattern.collection);
    await collection.createSearchIndex({
      name: vector.index,
      type: "vectorSearch",
      definition: {
        fields: [{
          type: "vector",
          path: vector.path,
          numDimensions: pattern.vector_dimensions,
          similarity: "cosine",
        }],
      },
    });
    const actual = await collection.listSearchIndexes(vector.index).toArray();
    if (!actual.some((index) => index?.name === vector.index)) {
      const error = new Error(`Query pattern ${pattern.id || "unknown"} search index was not created`);
      error.failureClass = "IMPLEMENTATION_FAILURE";
      throw error;
    }
    created.push({ collection: pattern.collection, name: vector.index });
  }
  return { created };
}

async function dropValidationSearchIndexes(db, patterns) {
  const dropped = [];
  for (const [collectionName, expectedNames] of declaredSearchIndexes(patterns)) {
    const collection = db.collection(collectionName);
    const actual = await collection.listSearchIndexes().toArray();
    const actualNames = new Set(actual.map((index) => index?.name).filter((name) => typeof name === "string"));
    for (const name of expectedNames) {
      if (!actualNames.has(name)) continue;
      await collection.dropSearchIndex(name);
      dropped.push({ collection: collectionName, name });
    }
  }
  return dropped;
}

async function cleanupValidationDatabase(client, databaseName, patterns = { patterns: [] }) {
  let failure;
  try {
    await client.connect();
    const db = client.db(databaseName);
    await dropValidationSearchIndexes(db, patterns);
    await db.dropDatabase();
  } catch {
    failure = new Error("Validation database cleanup failed.");
    failure.code = "VALIDATION_CLEANUP_FAILED";
  }
  try {
    await client.close();
  } catch {
    failure ||= Object.assign(new Error("Validation database cleanup failed."), {
      code: "VALIDATION_CLEANUP_FAILED",
    });
  }
  if (failure) throw failure;
}

async function validateDirect(request) {
  const prepared = parseDirectValidation(request);
  const result = await executePreparedValidation(request, prepared.schema, prepared.patterns, prepared.contents);
  return {
    ...result,
    source_commit_sha: request.source_commit_sha,
    correlation: {
      poc_id: request.poc_id,
      run_id: request.run_id,
      task_id: request.task_id,
      trace_id: request.trace_id,
      producer: "seed-validator",
    },
  };
}

exports.handler = async (event) => {
  if (event.requestContext?.http?.method === "GET" && event.rawPath === "/health") {
    return response(200, { status: "ok" });
  }
  const rawBody = event.body || "";
  try {
    if (!(await authenticate(event, rawBody))) return response(401, { status: "failed", error: { code: "UNAUTHORIZED", failure_class: "AUTHORIZATION_FAILURE", message: "The validator request was not authorized.", retryable: false } });
    if (event.requestContext?.http?.method !== "POST") {
      return response(404, { error: { code: "NOT_FOUND", message: "Unsupported route" } });
    }
    if (event.rawPath !== "/v1/validations/direct") {
      return response(404, { error: { code: "NOT_FOUND", message: "Unsupported route" } });
    }
    return response(200, await validateDirect(JSON.parse(rawBody)));
  } catch (error) {
    const classified = classifyFailure(error);
    const code = classified.code;
    const failure = { status: "failed", error: classified };
    const statusCode = classified.failure_class === "VALIDATOR_INFRASTRUCTURE_FAILURE" ? 503 : 422;
    return response(statusCode, failure);
  }
};

exports._test = {
  parseSeedSummary,
  substitutePlaceholders,
  targetDatabaseName,
  validationDocumentBudget,
  forcedImplementationFailure,
  validateInputContract,
  validationCaps,
  validationDatabaseName,
  ensureProductionId,
  parseDirectValidation,
  validateDirect,
  cleanupValidationDatabase,
  declaredSearchIndexes,
  createValidationSearchIndexes,
  dropValidationSearchIndexes,
  validateVectorSeedContract,
  authenticate,
  getHmacSecret,
  resetHmacSecretCache,
  verifyRequestSignature,
  sanitizeFailureMessage,
  classifyFailure,
};
