"use strict";

const { createHash } = require("node:crypto");
const acorn = require("acorn");

const NORMAL_FILES = new Set(["seed.js", "package.json", "SEED_README.md"]);
const REPAIR_FILES = new Set([...NORMAL_FILES, "REPAIR_NOTES.md"]);
const SIZE_LIMITS = {
  "seed.js": 500 * 1024,
  "package.json": 32 * 1024,
  "SEED_README.md": 256 * 1024,
  "REPAIR_NOTES.md": 64 * 1024,
};

class ArtifactValidationError extends Error {
  constructor(code, failureClass, message) {
    super(message);
    this.code = code;
    this.failureClass = failureClass;
  }
}

function validateManifestArtifacts(manifest, request, seedArtifactKey) {
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
    throw malformed("Seed manifest must be a JSON object");
  }
  if (manifest.poc_id !== request.poc_id || manifest.code_version !== request.code_version
    || (!request.project_layout && manifest.spec_version !== request.spec_version)) {
    throw malformed("Seed manifest identity does not match the validation request");
  }
  const entries = manifest.artifacts;
  const expectedFiles = manifest.repair ? REPAIR_FILES : NORMAL_FILES;
  if (!Array.isArray(entries) || entries.length !== expectedFiles.size) {
    throw malformed("Seed manifest has an invalid artifact set");
  }
  const artifacts = {};
  for (const entry of entries) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) throw malformed("Seed manifest artifact must be an object");
    const key = entry.key;
    const filename = typeof key === "string" ? key.split("/").pop() : "";
    if (!expectedFiles.has(filename) || artifacts[filename]) throw malformed("Seed manifest contains an unknown or duplicate artifact");
    const selector = {
      "seed.js": "seed_js",
      "package.json": "package_json",
      "SEED_README.md": "seed_readme",
      "REPAIR_NOTES.md": "repair_notes",
    }[filename];
    if (key !== seedArtifactKey(request.poc_id, request.code_version, selector)) {
      throw malformed("Seed manifest artifact key is not canonical");
    }
    if (typeof entry.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(entry.sha256)) {
      throw malformed("Seed manifest artifact SHA-256 is invalid");
    }
    if (!Number.isSafeInteger(entry.bytes) || entry.bytes < 0) {
      throw malformed("Seed manifest artifact byte count is invalid");
    }
    artifacts[filename] = { ...entry, key };
  }
  return artifacts;
}

function validateDownloadedArtifacts(artifacts, contents) {
  for (const [filename, artifact] of Object.entries(artifacts)) {
    const content = contents[filename];
    if (!Buffer.isBuffer(content)) throw malformed(`Downloaded artifact is missing: ${filename}`);
    if (content.length > SIZE_LIMITS[filename]) {
      throw new ArtifactValidationError("ARTIFACT_SIZE_EXCEEDED", "IMPLEMENTATION_FAILURE", `${filename} exceeds its size limit`);
    }
    if (content.length !== artifact.bytes) {
      throw new ArtifactValidationError("ARTIFACT_SIZE_MISMATCH", "ARTIFACT_INTEGRITY_FAILURE", `${filename} byte count does not match its manifest`);
    }
    const digest = createHash("sha256").update(content).digest("hex");
    if (digest !== artifact.sha256) {
      throw new ArtifactValidationError("ARTIFACT_HASH_MISMATCH", "ARTIFACT_INTEGRITY_FAILURE", `${filename} content does not match its manifest hash`);
    }
  }
}

function validatePackageJsonText(content) {
  if (Buffer.byteLength(content, "utf8") > SIZE_LIMITS["package.json"]) {
    throw new ArtifactValidationError("ARTIFACT_SIZE_EXCEEDED", "IMPLEMENTATION_FAILURE", "package.json exceeds its size limit");
  }
  let parsed;
  try {
    parsed = JSON.parse(content);
  } catch {
    throw new ArtifactValidationError("PACKAGE_JSON_INVALID", "IMPLEMENTATION_FAILURE", "package.json is not valid JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw packageInvalid();
  const dependencies = Object.keys(parsed.dependencies || {}).sort();
  const scripts = parsed.scripts || {};
  if (dependencies.length !== 1 || dependencies[0] !== "mongodb"
    || Object.keys(scripts).length !== 1 || scripts.seed !== "node seed.js") {
    throw packageInvalid();
  }
  for (const field of ["devDependencies", "optionalDependencies", "peerDependencies", "bin"]) {
    if (parsed[field] && Object.keys(parsed[field]).length) throw packageInvalid();
  }
  for (const name of Object.keys(scripts)) {
    if (/^(pre|post)/.test(name)) throw packageInvalid();
  }
  return parsed;
}

function validateGeneratedText(contents) {
  const prohibited = [
    /mongodb(?:\+srv)?:\/\/[^\s'"`]+/i,
    /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/,
    /\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*["'`][^"'`]+["'`]/i,
    /https?:\/\/[^\s'"`]+:[^\s'"`]+@/i,
  ];
  for (const [filename, content] of Object.entries(contents)) {
    if (prohibited.some((pattern) => pattern.test(content.toString("utf8")))) {
      throw new ArtifactValidationError("ARTIFACT_SECRET_DETECTED", "ARTIFACT_SECURITY_FAILURE", `${filename} contains prohibited literal credentials or connection data`);
    }
  }
}

function validateSeedScriptText(content) {
  const text = content.toString("utf8");
  if (Buffer.byteLength(text, "utf8") > SIZE_LIMITS["seed.js"]) {
    throw new ArtifactValidationError("ARTIFACT_SIZE_EXCEEDED", "IMPLEMENTATION_FAILURE", "seed.js exceeds its size limit");
  }
  const requiredPatterns = new Map([
    ["mongodb driver import", /require\(["'](?:mongodb|node:mongodb)["']\)/],
    ["MONGODB_URI", /process\.env\.MONGODB_URI/],
    ["DB_NAME", /process\.env\.DB_NAME/],
    ["SEED_MAX_DOCS", /process\.env\.SEED_MAX_DOCS/],
    ["SEED_COLLECTION_CAPS", /process\.env\.SEED_COLLECTION_CAPS/],
    ["seed_summary", /seed_summary/],
  ]);
  const missing = [...requiredPatterns]
    .filter(([, pattern]) => !pattern.test(text))
    .map(([name]) => name);
  if (missing.length) {
    throw new ArtifactValidationError(
      "SEED_SCRIPT_INVALID",
      "IMPLEMENTATION_FAILURE",
      `seed.js is missing required runtime contract fields: ${missing.join(", ")}`,
    );
  }
  let program;
  try {
    program = acorn.parse(text, { ecmaVersion: 2022, sourceType: "script" });
  } catch {
    throw new ArtifactValidationError("SEED_SCRIPT_INVALID", "IMPLEMENTATION_FAILURE", "seed.js has invalid JavaScript syntax");
  }
  const allowedModules = new Set(["mongodb", "crypto", "node:crypto"]);
  const allowedEnvironmentVariables = new Set(["MONGODB_URI", "DB_NAME", "SEED_MAX_DOCS", "SEED_COLLECTION_CAPS", "SEED_SKIP_SEARCH_INDEXES"]);

  walk(program, null, (node, parent) => {
    if (node.type === "ImportExpression") throw securityViolation("seed.js uses prohibited dynamic imports");
    if ((node.type === "CallExpression" || node.type === "NewExpression")
      && node.callee?.type === "Identifier" && ["eval", "Function", "fetch", "WebSocket"].includes(node.callee.name)) {
      throw securityViolation("seed.js uses prohibited dynamic execution or network access");
    }
    if (node.type === "CallExpression" && node.callee?.type === "Identifier" && node.callee.name === "require") {
      if (node.arguments.length !== 1 || node.arguments[0].type !== "Literal"
        || typeof node.arguments[0].value !== "string" || !allowedModules.has(node.arguments[0].value)) {
        throw securityViolation("seed.js imports a prohibited or nonliteral module");
      }
    }
    if (node.type === "Identifier" && node.name === "require"
      && !(parent?.type === "CallExpression" && parent.callee === node)) {
      throw securityViolation("seed.js aliases or exposes the module loader");
    }
    if (node.type === "MemberExpression"
      && !(parent?.type === "MemberExpression" && parent.object === node)) {
      const path = memberPath(node);
      if (path?.[0] === "process") {
        const allowed = path.length === 2 && path[1] === "exitCode"
          || path.length === 3 && path[1] === "env" && allowedEnvironmentVariables.has(path[2]);
        if (!allowed) throw securityViolation("seed.js accesses a prohibited process capability or environment variable");
      }
    }
  });
}

function memberPath(node) {
  const path = [];
  let current = node;
  while (current?.type === "MemberExpression") {
    const property = current.computed
      ? current.property?.type === "Literal" && typeof current.property.value === "string" ? current.property.value : null
      : current.property?.type === "Identifier" ? current.property.name : null;
    if (property === null) return null;
    path.unshift(property);
    current = current.object;
  }
  if (current?.type !== "Identifier") return null;
  path.unshift(current.name);
  return path;
}

function walk(node, parent, visit) {
  if (!node || typeof node !== "object") return;
  visit(node, parent);
  for (const [key, child] of Object.entries(node)) {
    if (key === "start" || key === "end") continue;
    if (Array.isArray(child)) {
      for (const entry of child) walk(entry, node, visit);
    } else if (child && typeof child.type === "string") {
      walk(child, node, visit);
    }
  }
}

function securityViolation(message) {
  return new ArtifactValidationError("SEED_SCRIPT_SECURITY_VIOLATION", "ARTIFACT_SECURITY_FAILURE", message);
}

function malformed(message) {
  return new ArtifactValidationError("ARTIFACT_MALFORMED", "IMPLEMENTATION_FAILURE", message);
}

function packageInvalid() {
  return new ArtifactValidationError("PACKAGE_JSON_INVALID", "IMPLEMENTATION_FAILURE", "package.json has unsupported dependencies, scripts, or executable fields");
}

module.exports = {
  ArtifactValidationError,
  SIZE_LIMITS,
  validateDownloadedArtifacts,
  validateGeneratedText,
  validateManifestArtifacts,
  validatePackageJsonText,
  validateSeedScriptText,
};