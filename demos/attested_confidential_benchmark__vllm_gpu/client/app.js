const DEFAULTS = window.COVE_CLIENT_DEFAULTS || {
  endpoint: "",
  publisher: "demo-carol.covehub.io",
  workflow: "attested_confidential_benchmark__vllm_gpu",
  model: "CoveDemoModel",
};

const NODES = [
  "audit_serving_code",
  "audit_eval_code",
  "compile_serving_code",
  "model_benchmark",
  "model_deployment",
];
const X509_COMMON_NAME_MAX_BYTES = 64;
const FNV1A64_OFFSET = 0xcbf29ce484222325n;
const FNV1A64_PRIME = 0x100000001b3n;
const FNV1A64_MASK = 0xffffffffffffffffn;
const UTF8_ENCODER = new TextEncoder();

const elements = {
  endpoint: document.querySelector("#endpointInput"),
  publisher: document.querySelector("#publisherInput"),
  workflow: document.querySelector("#workflowInput"),
  model: document.querySelector("#modelInput"),
  verifyButton: document.querySelector("#verifyButton"),
  sendButton: document.querySelector("#sendButton"),
  chatForm: document.querySelector("#chatForm"),
  prompt: document.querySelector("#promptInput"),
  maxTokens: document.querySelector("#maxTokensInput"),
  temperature: document.querySelector("#temperatureInput"),
  transcript: document.querySelector("#transcript"),
  payloadOutput: document.querySelector("#payloadOutput"),
  checkList: document.querySelector("#checkList"),
  hashList: document.querySelector("#hashList"),
  benchmarkGrid: document.querySelector("#benchmarkGrid"),
  tlsGrid: document.querySelector("#tlsGrid"),
  overallStatus: document.querySelector("#overallStatus"),
  modelBadge: document.querySelector("#modelBadge"),
};

const state = {
  certificates: {},
  certificateErrors: {},
  tls: null,
  health: null,
  models: null,
  verification: null,
  verificationId: null,
  lastPayload: null,
};

function applyQueryDefaults() {
  const params = new URLSearchParams(window.location.search);
  elements.endpoint.value = params.get("endpoint") || DEFAULTS.endpoint || "";
  elements.publisher.value = params.get("publisher") || DEFAULTS.publisher || "demo-carol.covehub.io";
  elements.workflow.value = params.get("workflow") || DEFAULTS.workflow || "attested_confidential_benchmark__vllm_gpu";
  elements.model.value = params.get("model") || DEFAULTS.model || "CoveDemoModel";
}

function setBusy(button, busy) {
  button.disabled = busy;
  button.dataset.originalText ||= button.textContent;
  button.textContent = busy ? "Working" : button.dataset.originalText;
}

async function apiJson(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const payload = await response.json();
  if (!response.ok && response.status !== 207) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.payload = payload;
    throw error;
  }
  return payload;
}

function getBody(node) {
  return state.certificates[node]?.certificate_body || {};
}

function getResult(node, service) {
  return getBody(node).results?.[service] || {};
}

function getInputHash(node, artifact) {
  return getBody(node).inputs?.[artifact]?.plaintext_hash || null;
}

function getOutputHash(node, artifact) {
  return getBody(node).outputs?.[artifact]?.plaintext_hash || null;
}

function isSha256(value) {
  return typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value);
}

function shortHash(value) {
  if (!value) return "missing";
  return value.length > 24 ? `${value.slice(0, 18)}...${value.slice(-8)}` : value;
}

function sameHash(a, b) {
  return isSha256(a) && a === b;
}

function renderDefinitionList(element, entries) {
  element.innerHTML = "";
  for (const [key, value] of entries) {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value ?? "missing";
    element.append(dt, dd);
  }
}

function renderChecks(checks) {
  elements.checkList.innerHTML = "";
  for (const check of checks) {
    const item = document.createElement("li");
    item.className = `check-item ${check.pass ? "pass" : "fail"}`;
    const icon = document.createElement("div");
    icon.className = "check-icon";
    icon.textContent = check.pass ? "OK" : "!";
    const body = document.createElement("div");
    const title = document.createElement("div");
    title.className = "check-title";
    title.textContent = check.label;
    const detail = document.createElement("div");
    detail.className = "check-detail";
    detail.textContent = check.detail || "";
    body.append(title, detail);
    item.append(icon, body);
    elements.checkList.append(item);
  }
}

function renderHashes(entries) {
  elements.hashList.innerHTML = "";
  for (const [name, value] of entries) {
    const row = document.createElement("div");
    row.className = "hash-row";
    const label = document.createElement("div");
    label.className = "hash-name";
    label.textContent = name;
    const hash = document.createElement("div");
    hash.className = "hash-value";
    hash.textContent = value || "missing";
    row.append(label, hash);
    elements.hashList.append(row);
  }
}

function setOverallStatus(ok, label) {
  elements.overallStatus.textContent = label;
  elements.overallStatus.className = `status-pill ${ok ? "ok" : "bad"}`;
  elements.modelBadge.textContent = ok ? "verified" : "not verified";
  elements.modelBadge.className = `badge ${ok ? "ok" : "bad"}`;
}

function expectedTlsCn() {
  return keypairCertificateCommonName(elements.workflow.value.trim(), "model_deployment", "ratls_key");
}

function keypairCertificateCommonName(workflowId, nodeName, keypairName) {
  const rawCommonName = `${workflowId}.${nodeName}.${keypairName}`;
  if (utf8ByteLength(rawCommonName) <= X509_COMMON_NAME_MAX_BYTES) {
    return rawCommonName;
  }
  const suffix = `-${fnv1a64Hex(rawCommonName)}`;
  const prefix = `cove-${nodeName}.${keypairName}`;
  return `${truncateUtf8(prefix, X509_COMMON_NAME_MAX_BYTES - utf8ByteLength(suffix))}${suffix}`;
}

function fnv1a64Hex(value) {
  let hash = FNV1A64_OFFSET;
  for (const byte of UTF8_ENCODER.encode(value)) {
    hash ^= BigInt(byte);
    hash = (hash * FNV1A64_PRIME) & FNV1A64_MASK;
  }
  return hash.toString(16).padStart(16, "0");
}

function utf8ByteLength(value) {
  return UTF8_ENCODER.encode(value).length;
}

function truncateUtf8(value, maxBytes) {
  let output = "";
  let usedBytes = 0;
  for (const char of value) {
    const charBytes = utf8ByteLength(char);
    if (usedBytes + charBytes > maxBytes) {
      break;
    }
    output += char;
    usedBytes += charBytes;
  }
  return output;
}

function buildVerificationChecks() {
  const checks = [];
  const add = (label, pass, detail) => checks.push({ label, pass: Boolean(pass), detail });

  const auditServing = getResult("audit_serving_code", "audit_agent");
  const auditEval = getResult("audit_eval_code", "audit_agent");
  const compile = getResult("compile_serving_code", "compile_serving_wheel");
  const benchmark = getResult("model_benchmark", "benchmark_runner");
  const deploymentInputs = getBody("model_deployment").inputs || {};
  const selectedWorkflow = elements.workflow.value.trim();

  const allCertsPresent = NODES.every((node) => state.certificates[node]);
  add("All workflow certificates fetched", allCertsPresent, NODES.filter((node) => !state.certificates[node]).join(", ") || "latest certificates present");

  const allIdsMatch = NODES.every((node) => {
    const body = getBody(node);
    return body.workflow_id === selectedWorkflow && body.node_id === node;
  });
  add("Certificates bind to selected workflow", allIdsMatch, selectedWorkflow || "workflow missing");

  const allAttested = NODES.every((node) => {
    const cert = state.certificates[node];
    const bundle = cert?.attestation_bundle || {};
    const body = cert?.certificate_body || {};
    return bundle.format === "phala_dstack_v1" && isSha256(bundle.generated_node_compose_hash || body.generated_node_compose_hash);
  });
  add("Phala dstack attestation bundles present", allAttested, "format and generated node compose hashes are present");

  add("Serving patch audit used public audit model", auditServing.llm_used === true, "audit model path is a compose-measured trust assumption");
  add("Eval-code audit used public audit model", auditEval.llm_used === true, "audit model path is a compose-measured trust assumption");
  add("Serving patch audit passed", auditServing.pass === true, shortHash(auditServing.audited_sha256));
  add("Eval-code audit passed", auditEval.pass === true, shortHash(auditEval.audited_sha256));
  add("Compile node passed", compile.pass === true, shortHash(compile.compiled_wheel_sha256));
  add("Benchmark refusal rate > 40%", benchmark.pass === true && benchmark.passes_threshold === true, `refusal ${benchmark.refusal_rate ?? "missing"}`);

  add(
    "Serving audit hash matches input",
    sameHash(auditServing.audited_sha256, getInputHash("audit_serving_code", "alice_private_serving_patch")),
    `${shortHash(auditServing.audited_sha256)} == ${shortHash(getInputHash("audit_serving_code", "alice_private_serving_patch"))}`,
  );
  add(
    "Eval audit hash matches input",
    sameHash(auditEval.audited_sha256, getInputHash("audit_eval_code", "bob_private_eval_code")),
    `${shortHash(auditEval.audited_sha256)} == ${shortHash(getInputHash("audit_eval_code", "bob_private_eval_code"))}`,
  );
  add(
    "Compile consumed audited serving patch",
    sameHash(compile.serving_patch_sha256, auditServing.audited_sha256),
    `${shortHash(compile.serving_patch_sha256)} == ${shortHash(auditServing.audited_sha256)}`,
  );
  add(
    "Compile output hash matches result",
    sameHash(getOutputHash("compile_serving_code", "compiled_serving_wheel"), compile.compiled_wheel_sha256),
    `${shortHash(getOutputHash("compile_serving_code", "compiled_serving_wheel"))} == ${shortHash(compile.compiled_wheel_sha256)}`,
  );
  add(
    "Benchmark consumed audited eval code",
    sameHash(benchmark.eval_code_sha256, auditEval.audited_sha256),
    `${shortHash(benchmark.eval_code_sha256)} == ${shortHash(auditEval.audited_sha256)}`,
  );
  add(
    "Benchmark used compiled serving wheel",
    sameHash(benchmark.serving_wheel_sha256, compile.compiled_wheel_sha256)
      && sameHash(getInputHash("model_benchmark", "compiled_serving_wheel"), compile.compiled_wheel_sha256),
    `${shortHash(benchmark.serving_wheel_sha256)} == ${shortHash(compile.compiled_wheel_sha256)}`,
  );
  add(
    "Benchmark model input matches result",
    sameHash(benchmark.model_archive_sha256, getInputHash("model_benchmark", "alice_private_model")),
    shortHash(benchmark.model_archive_sha256),
  );
  add(
    "Deployment model matches benchmark",
    sameHash(deploymentInputs.alice_private_model?.plaintext_hash, benchmark.model_archive_sha256),
    `${shortHash(deploymentInputs.alice_private_model?.plaintext_hash)} == ${shortHash(benchmark.model_archive_sha256)}`,
  );
  add(
    "Deployment wheel matches benchmark",
    sameHash(deploymentInputs.compiled_serving_wheel?.plaintext_hash, benchmark.serving_wheel_sha256),
    `${shortHash(deploymentInputs.compiled_serving_wheel?.plaintext_hash)} == ${shortHash(benchmark.serving_wheel_sha256)}`,
  );

  const healthOk = state.health?.payload?.status === "ok";
  add("Endpoint health passed", healthOk, state.health?.endpoint || "health unavailable");

  const tlsCn = state.tls?.subject?.CN || "";
  add(
    "RA-TLS endpoint certificate matches workflow",
    tlsCn === expectedTlsCn(),
    tlsCn || "certificate unavailable",
  );

  const modelIds = (state.models?.payload?.data || []).map((model) => model.id);
  add(
    "Endpoint serves CoveDemoModel",
    modelIds.includes(elements.model.value.trim()),
    modelIds.join(", ") || "model list unavailable",
  );

  return checks;
}

function renderVerification() {
  const benchmark = getResult("model_benchmark", "benchmark_runner");
  const compile = getResult("compile_serving_code", "compile_serving_wheel");
  const auditServing = getResult("audit_serving_code", "audit_agent");
  const auditEval = getResult("audit_eval_code", "audit_agent");
  const deploymentInputs = getBody("model_deployment").inputs || {};

  renderDefinitionList(elements.benchmarkGrid, [
    ["Name", benchmark.benchmark_name || "missing"],
    ["Refusal rate", benchmark.refusal_rate === undefined ? "missing" : String(benchmark.refusal_rate)],
    ["ASR", benchmark.attack_success_rate === undefined ? "missing" : String(benchmark.attack_success_rate)],
    ["Pass", benchmark.passes_threshold === true ? "yes" : "no"],
    ["Responses", `${benchmark.successful_responses ?? "?"}/${benchmark.total_prompts ?? "?"}`],
  ]);

  renderDefinitionList(elements.tlsGrid, [
    ["CN", state.tls?.subject?.CN || "missing"],
    ["Protocol", state.tls?.protocol || "missing"],
    ["Fingerprint", state.tls?.fingerprint256 || "missing"],
    ["Health", state.health?.payload?.status || "missing"],
  ]);

  renderHashes([
    ["Alice model", benchmark.model_archive_sha256 || getInputHash("model_benchmark", "alice_private_model")],
    ["Alice serving patch", auditServing.audited_sha256 || getInputHash("audit_serving_code", "alice_private_serving_patch")],
    ["Bob eval code", auditEval.audited_sha256 || getInputHash("audit_eval_code", "bob_private_eval_code")],
    ["Bob eval data", benchmark.eval_data_sha256 || getInputHash("model_benchmark", "bob_private_eval_data")],
    ["Compiled wheel", compile.compiled_wheel_sha256 || getOutputHash("compile_serving_code", "compiled_serving_wheel")],
    ["Deployment model", deploymentInputs.alice_private_model?.plaintext_hash],
    ["Deployment wheel", deploymentInputs.compiled_serving_wheel?.plaintext_hash],
  ]);

  const checks = state.verification?.checks || buildVerificationChecks();
  renderChecks(checks);
  const ok = checks.length > 0 && checks.every((check) => check.pass);
  setOverallStatus(ok, ok ? "verified" : "blocked");
  elements.payloadOutput.textContent = JSON.stringify(
    {
      certificates: state.certificates,
      verification_id: state.verificationId,
      tls: state.tls,
      health: state.health,
      models: state.models,
      manifest_hash: state.verification?.manifest_hash,
      expected_compose_hashes: state.verification?.expected_compose_hashes,
      dependency_edges: state.verification?.dependency_edges,
      checks,
    },
    null,
    2,
  );
}

async function verifyAttestation() {
  setBusy(elements.verifyButton, true);
  try {
    const params = new URLSearchParams({
      publisher: elements.publisher.value.trim(),
      workflow: elements.workflow.value.trim(),
      endpoint: elements.endpoint.value.trim(),
      model: elements.model.value.trim(),
    });

    const verificationPayload = await apiJson(`/api/verify?${params}`);
    state.verification = verificationPayload;
    state.verificationId = verificationPayload.verification_id;
    state.certificates = verificationPayload.certificates || {};
    state.certificateErrors = {};
    state.tls = verificationPayload.tls;
    state.health = verificationPayload.health;
    state.models = verificationPayload.models;
    renderVerification();
  } catch (error) {
    state.lastPayload = error.payload || { error: error.message };
    elements.payloadOutput.textContent = JSON.stringify(state.lastPayload, null, 2);
    renderChecks([{ label: "Verification request failed", pass: false, detail: error.message }]);
    setOverallStatus(false, "blocked");
  } finally {
    setBusy(elements.verifyButton, false);
  }
}

function addMessage(role, content, kind = "") {
  if (elements.transcript.querySelector(".empty-state")) {
    elements.transcript.innerHTML = "";
  }
  const message = document.createElement("article");
  message.className = `message ${role} ${kind}`.trim();
  const label = document.createElement("div");
  label.className = "role";
  label.textContent = role;
  const body = document.createElement("div");
  body.className = "content";
  body.textContent = content;
  message.append(label, body);
  elements.transcript.append(message);
  elements.transcript.scrollTop = elements.transcript.scrollHeight;
}

async function sendPrompt(event) {
  event.preventDefault();
  const prompt = elements.prompt.value.trim();
  if (!prompt) return;

  addMessage("user", prompt);
  setBusy(elements.sendButton, true);
  try {
    const payload = await apiJson("/api/chat", {
      method: "POST",
      body: JSON.stringify({
        verification_id: state.verificationId,
        prompt,
        max_tokens: Number(elements.maxTokens.value || 160),
        temperature: Number(elements.temperature.value || 0),
      }),
    });
    state.lastPayload = payload;
    const choice = payload.response?.choices?.[0]?.message?.content;
    addMessage("assistant", choice || JSON.stringify(payload.response));
    elements.payloadOutput.textContent = JSON.stringify(payload, null, 2);
  } catch (error) {
    const payload = error.payload || { error: error.message };
    addMessage("assistant", error.message, "error");
    elements.payloadOutput.textContent = JSON.stringify(payload, null, 2);
  } finally {
    setBusy(elements.sendButton, false);
  }
}

function init() {
  applyQueryDefaults();
  elements.transcript.innerHTML = '<div class="empty-state">No messages yet.</div>';
  renderDefinitionList(elements.benchmarkGrid, [
    ["Name", "missing"],
    ["Refusal rate", "missing"],
    ["ASR", "missing"],
    ["Pass", "missing"],
    ["Responses", "missing"],
  ]);
  renderDefinitionList(elements.tlsGrid, [
    ["CN", "missing"],
    ["Protocol", "missing"],
    ["Fingerprint", "missing"],
    ["Health", "missing"],
  ]);
  renderHashes([
    ["Alice model", ""],
    ["Alice serving patch", ""],
    ["Bob eval code", ""],
    ["Bob eval data", ""],
    ["Compiled wheel", ""],
  ]);
  renderChecks([{ label: "No server verification loaded", pass: false, detail: "pending" }]);
  elements.verifyButton.addEventListener("click", verifyAttestation);
  elements.chatForm.addEventListener("submit", sendPrompt);
}

init();
