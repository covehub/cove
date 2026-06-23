import crypto, { X509Certificate } from "node:crypto";
import http from "node:http";
import https from "node:https";
import tls from "node:tls";

export const TERMINAL_NODE = "model_deployment";
export const RATLS_KEYPAIR = "ratls_key";

const PHALA_DSTACK_ATTESTATION_FORMAT = "phala_dstack_v1";
const PHALA_DSTACK_VERIFY_URL = "https://cloud-api.phala.network/api/v1/attestations/verify";
const WORKFLOW_BUNDLE_SIGNATURE_PURPOSE = "cove_workflow_bundle_v1";
const OWNER_IDENTITY_PURPOSE = "cove_owner_identity_v2";
const OWNER_IDENTITY_VERSION = 2;
const NODE_CERTIFICATE_REPORT_LABEL = Buffer.from("cove_node_certificate_v1", "utf-8");
const DSTACK_EVENT_TYPE = 134217729;
const RTMR3_INDEX = 3;
const RTMR_BYTES = 48;
const REPORT_DATA_BYTES = 64;
const RECEIPT_VERSION = "cove_model_response_receipt_v1";
const SAMPLING_FIELD_NAMES = [
  "max_tokens",
  "temperature",
  "top_p",
  "top_k",
  "min_p",
  "presence_penalty",
  "frequency_penalty",
  "repetition_penalty",
  "seed",
  "stream",
];

export class VerificationError extends Error {
  constructor(message) {
    super(message);
    this.name = "VerificationError";
  }
}

export function normalizeEndpoint(input) {
  if (!input || typeof input !== "string") {
    throw new VerificationError("endpoint is required");
  }
  const url = new URL(input);
  if (url.protocol !== "https:") {
    throw new VerificationError("endpoint must be HTTPS for RA-TLS verification");
  }
  url.hash = "";
  url.search = "";
  let pathname = url.pathname.replace(/\/+$/, "");
  if (pathname.endsWith("/v1")) {
    pathname = pathname.slice(0, -3);
  }
  url.pathname = pathname || "/";
  return url;
}

export function endpointUrl(input, suffix) {
  const url = normalizeEndpoint(input);
  const basePath = url.pathname.replace(/\/+$/, "");
  url.pathname = `${basePath}${suffix}`;
  return url;
}

export async function verifyDemoTarget({
  endpoint,
  apiBase,
  publisher,
  workflow,
  model,
}) {
  const workflowObject = await requestJson(
    new URL(
      `/v1/workflows/${encodeURIComponent(publisher)}/${encodeURIComponent(workflow)}/latest`,
      apiBase,
    ),
    { timeoutMs: 60_000 },
  );
  const bundle = verifyWorkflowObject(workflowObject, {
    expectedPublisher: publisher,
    expectedWorkflow: workflow,
  });

  const terminalCertificate = await requestJson(
    new URL(
      `/v1/runtime/${encodeURIComponent(publisher)}/${encodeURIComponent(workflow)}/certificates/${TERMINAL_NODE}/latest`,
      apiBase,
    ),
    { timeoutMs: 60_000 },
  );

  const certificateContext = {
    composeByNode: bundle.composeByNode,
    dependencyEdges: bundle.dependencyEdges,
    certificates: new Map(),
    certificateBodyHashes: new Map(),
  };
  await verifyNodeCertificate(terminalCertificate, {
    expectedWorkflowId: workflow,
    expectedNodeId: TERMINAL_NODE,
    expectedComposeHash: bundle.composeByNode.get(TERMINAL_NODE),
    context: certificateContext,
    seen: new Set(),
  });

  const terminalBody = requiredObject(
    terminalCertificate.certificate_body,
    "terminal certificate body",
  );
  const keypair = requiredObject(
    requiredObject(terminalBody.ephemeral_keypairs, "certificate_body.ephemeral_keypairs")[RATLS_KEYPAIR],
    `certificate_body.ephemeral_keypairs.${RATLS_KEYPAIR}`,
  );
  const certificatePem = requiredString(keypair.certificate_pem, "RA-TLS certificate PEM");
  const publicKeyPem = requiredString(keypair.public_key_pem, "RA-TLS public key PEM");
  const expectedDer = new X509Certificate(certificatePem).raw;

  const tlsInfo = await inspectPinnedTls(endpoint, expectedDer);
  const health = await pinnedJsonRequest(endpointUrl(endpoint, "/health"), {
    expectedCertificateDer: expectedDer,
    timeoutMs: 30_000,
  });
  if (health.payload?.status !== "ok") {
    throw new VerificationError("verified RA-TLS endpoint health check failed");
  }
  const models = await pinnedJsonRequest(endpointUrl(endpoint, "/v1/models"), {
    expectedCertificateDer: expectedDer,
    timeoutMs: 30_000,
  });
  const modelIds = Array.isArray(models.payload?.data)
    ? models.payload.data.map((entry) => entry?.id).filter((id) => typeof id === "string")
    : [];
  if (model && !modelIds.includes(model)) {
    throw new VerificationError(`verified RA-TLS endpoint does not serve model ${model}`);
  }

  const certificates = Object.fromEntries(certificateContext.certificates);
  const checks = buildChecks({
    workflow,
    model,
    modelIds,
    certificates,
    bundle,
    tlsInfo,
    health,
  });
  const failedChecks = checks.filter((check) => !check.pass);
  if (failedChecks.length > 0) {
    throw new VerificationError(
      `demo verification checks failed: ${failedChecks.map((check) => check.label).join(", ")}`,
    );
  }

  return {
    apiBase,
    publisher,
    workflow,
    endpoint: normalizeEndpoint(endpoint).href,
    model,
    manifest_hash: bundle.manifest.manifest_hash,
    workflow_signature: {
      publisher: bundle.manifest.publisher,
      owner_domain: bundle.publisherIdentity.owner_domain,
      owner_public_key_sha256: bundle.publisherIdentity.owner_public_key_sha256,
    },
    terminal_node: TERMINAL_NODE,
    ratls_keypair: {
      name: RATLS_KEYPAIR,
      public_key_pem: publicKeyPem,
      public_key_hash: keypair.public_key_hash,
      certificate_der_sha256: sha256Literal(expectedDer),
    },
    tls: tlsInfo,
    health,
    models,
    certificates,
    expected_compose_hashes: Object.fromEntries(bundle.composeByNode),
    dependency_edges: Object.fromEntries(bundle.dependencyEdges),
    checks,
  };
}

export async function pinnedJsonRequest(url, {
  method = "GET",
  headers = {},
  body,
  expectedCertificateDer,
  timeoutMs = 120_000,
} = {}) {
  if (!(expectedCertificateDer instanceof Buffer)) {
    throw new VerificationError("expectedCertificateDer must be a Buffer");
  }
  const requestBody = body === undefined ? undefined : Buffer.from(body);
  const requestHeaders = {
    Accept: "application/json",
    "User-Agent": "cove-attested-eval-client/0.1",
    ...headers,
  };
  if (requestBody !== undefined) {
    requestHeaders["Content-Length"] = String(requestBody.length);
  }

  return new Promise((resolve, reject) => {
    let settled = false;
    const fail = (error) => {
      if (!settled) {
        settled = true;
        reject(error);
      }
    };
    const request = https.request(
      {
        protocol: url.protocol,
        hostname: url.hostname,
        port: url.port || 443,
        path: `${url.pathname}${url.search}`,
        method,
        headers: requestHeaders,
        timeout: timeoutMs,
        rejectUnauthorized: false,
        agent: false,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          if (settled) return;
          settled = true;
          const text = Buffer.concat(chunks).toString("utf-8");
          let payload = null;
          if (text.length > 0) {
            try {
              payload = JSON.parse(text);
            } catch {
              payload = { raw: text };
            }
          }
          if ((response.statusCode || 0) >= 400) {
            const error = new VerificationError(`HTTP ${response.statusCode} from ${url.href}`);
            error.statusCode = response.statusCode;
            error.payload = payload;
            reject(error);
            return;
          }
          resolve({ statusCode: response.statusCode, headers: response.headers, payload });
        });
      },
    );
    request.on("socket", (socket) => {
      socket.once("secureConnect", () => {
        try {
          const peer = socket.getPeerCertificate(true);
          if (!peer?.raw || !Buffer.from(peer.raw).equals(expectedCertificateDer)) {
            throw new VerificationError("live TLS certificate DER does not match verified node certificate");
          }
          if (requestBody !== undefined) {
            request.write(requestBody);
          }
          request.end();
        } catch (error) {
          request.destroy(error);
        }
      });
    });
    request.on("timeout", () => request.destroy(new VerificationError(`request timed out: ${url.href}`)));
    request.on("error", fail);
    request.flushHeaders();
  });
}

export function verifyModelResponseReceipt({
  requestPayload,
  responsePayload,
  receipt,
  publicKeyPem,
  expectedNonce,
}) {
  const receiptObject = requiredObject(receipt, "cove_receipt");
  if (requiredString(receiptObject.version, "cove_receipt.version") !== RECEIPT_VERSION) {
    throw new VerificationError("model response receipt version is unsupported");
  }
  if (requiredString(receiptObject.nonce, "cove_receipt.nonce") !== expectedNonce) {
    throw new VerificationError("model response receipt nonce mismatch");
  }
  const responseWithoutReceipt = { ...responsePayload };
  delete responseWithoutReceipt.cove_receipt;
  if (requiredString(receiptObject.request_hash, "cove_receipt.request_hash") !== sha256Literal(canonicalJsonBytes(requestPayload))) {
    throw new VerificationError("model response receipt request hash mismatch");
  }
  if (requiredString(receiptObject.response_hash, "cove_receipt.response_hash") !== sha256Literal(canonicalJsonBytes(responseWithoutReceipt))) {
    throw new VerificationError("model response receipt response hash mismatch");
  }
  const model = requiredString(receiptObject.model, "cove_receipt.model");
  if (model !== requestPayload.model) {
    throw new VerificationError("model response receipt model mismatch");
  }
  const sampling = requiredObject(receiptObject.sampling, "cove_receipt.sampling");
  const expectedSampling = samplingFields(requestPayload);
  if (JSON.stringify(sortForCanonicalJson(sampling)) !== JSON.stringify(sortForCanonicalJson(expectedSampling))) {
    throw new VerificationError("model response receipt sampling fields mismatch");
  }
  if (receiptObject.no_hidden_server_context !== true) {
    throw new VerificationError("model response receipt is missing no-hidden-server-context assertion");
  }
  const publicKeyHash = requiredString(receiptObject.public_key_hash, "cove_receipt.public_key_hash");
  if (publicKeyHash !== sha256Literal(Buffer.from(publicKeyPem, "utf-8"))) {
    throw new VerificationError("model response receipt public key hash mismatch");
  }
  const unsignedReceipt = { ...receiptObject };
  delete unsignedReceipt.signature;
  const signature = decodeBase64(
    requiredString(receiptObject.signature, "cove_receipt.signature"),
    "cove_receipt.signature",
  );
  const publicKey = crypto.createPublicKey(publicKeyPem);
  if (!crypto.verify(null, canonicalJsonBytes(unsignedReceipt), publicKey, signature)) {
    throw new VerificationError("model response receipt signature verification failed");
  }
  return receiptObject;
}

function verifyWorkflowObject(workflowObject, { expectedPublisher, expectedWorkflow }) {
  const payload = requiredObject(workflowObject, "workflow object");
  if (payload.format !== "cove.workflow.bundle.v1") {
    throw new VerificationError("workflow object format is unsupported");
  }
  const manifest = requiredObject(payload.manifest, "workflow manifest");
  if (manifest.publisher !== expectedPublisher) {
    throw new VerificationError("workflow publisher does not match request");
  }
  if (manifest.workflow_id !== expectedWorkflow) {
    throw new VerificationError("workflow id does not match request");
  }
  verifyManifestHash(manifest);
  const publisherIdentity = verifyPublisherSignature(
    requiredObject(payload.publisher_signature, "workflow publisher_signature"),
    manifest,
    expectedPublisher,
  );

  const files = verifyWorkflowFiles(payload.files, manifest.files);
  const normalizedWorkflow = files.get("workflow.normalized.cove.yaml");
  if (!normalizedWorkflow) {
    throw new VerificationError("workflow bundle is missing workflow.normalized.cove.yaml");
  }
  const dependencyEdges = parseWorkflowDependencies(normalizedWorkflow.toString("utf-8"));
  const composeByNode = new Map();
  for (const node of requiredArray(manifest.nodes, "workflow manifest nodes")) {
    const nodeId = requiredString(node.node_id, "workflow manifest node_id");
    const composeHash = requiredString(node.compose_hash, `manifest node ${nodeId} compose_hash`);
    composeByNode.set(nodeId, composeHash);
    const composePath = requiredString(node.compose_path, `manifest node ${nodeId} compose_path`);
    const composeBytes = files.get(composePath);
    if (!composeBytes) {
      throw new VerificationError(`workflow bundle is missing compose file for node ${nodeId}`);
    }
    if (!dependencyEdges.has(nodeId)) {
      dependencyEdges.set(nodeId, []);
    }
  }

  return {
    manifest,
    files,
    dependencyEdges,
    composeByNode,
    publisherIdentity,
  };
}

function verifyManifestHash(manifest) {
  const payload = {
    publisher: requiredString(manifest.publisher, "manifest.publisher"),
    workflow_id: requiredString(manifest.workflow_id, "manifest.workflow_id"),
    owners: requiredObject(manifest.owners, "manifest.owners"),
    files: requiredArray(manifest.files, "manifest.files").map((entry) => ({
      path: requiredString(entry.path, "manifest.files[].path"),
      sha256: requiredString(entry.sha256, "manifest.files[].sha256"),
    })),
    nodes: requiredArray(manifest.nodes, "manifest.nodes").map((node) => ({
      node_id: requiredString(node.node_id, "manifest.nodes[].node_id"),
      compose_path: requiredString(node.compose_path, "manifest.nodes[].compose_path"),
      compose_hash: requiredString(node.compose_hash, "manifest.nodes[].compose_hash"),
      artifact_provisioner_image: node.artifact_provisioner_image ?? null,
      artifact_provisioner_digest: node.artifact_provisioner_digest ?? null,
      artifacts: requiredArray(node.artifacts, "manifest.nodes[].artifacts").map((artifact) => ({
        name: requiredString(artifact.name, "manifest artifact name"),
        type: requiredString(artifact.type, "manifest artifact type"),
        direction: requiredString(artifact.direction, "manifest artifact direction"),
        hub_path: requiredString(artifact.hub_path, "manifest artifact hub_path"),
        owner: requiredString(artifact.owner, "manifest artifact owner"),
      })),
      runtime_skeleton: requiredArray(node.runtime_skeleton, "manifest runtime_skeleton").map((entry) => ({
        path: requiredString(entry.path, "runtime_skeleton path"),
        kind: requiredString(entry.kind, "runtime_skeleton kind"),
      })),
    })),
  };
  const observedHash = sha256Literal(canonicalJsonBytes(payload));
  if (observedHash !== requiredString(manifest.manifest_hash, "manifest.manifest_hash")) {
    throw new VerificationError("workflow manifest hash mismatch");
  }
}

function verifyWorkflowFiles(rawFiles, manifestFiles) {
  const files = new Map();
  const expected = new Map();
  for (const entry of requiredArray(manifestFiles, "manifest.files")) {
    expected.set(requiredString(entry.path, "manifest file path"), requiredString(entry.sha256, "manifest file sha256"));
  }
  for (const entry of requiredArray(rawFiles, "workflow object files")) {
    const path = requiredString(entry.path, "workflow object file path");
    const expectedSha = expected.get(path);
    if (!expectedSha) {
      throw new VerificationError(`workflow object has unexpected file ${path}`);
    }
    const content = decodeBase64(requiredString(entry.content_b64, `workflow object file ${path}`), `workflow object file ${path}`);
    const declaredSha = requiredString(entry.sha256, "workflow object file sha256");
    const observedSha = sha256Literal(content);
    if (observedSha !== declaredSha || observedSha !== expectedSha) {
      throw new VerificationError(`workflow object file hash mismatch for ${path}`);
    }
    files.set(path, content);
  }
  for (const path of expected.keys()) {
    if (!files.has(path)) {
      throw new VerificationError(`workflow object is missing file ${path}`);
    }
  }
  return files;
}

function verifyPublisherSignature(publisherSignature, manifest, expectedPublisher) {
  if (requiredString(publisherSignature.purpose, "publisher_signature.purpose") !== WORKFLOW_BUNDLE_SIGNATURE_PURPOSE) {
    throw new VerificationError("workflow object publisher signature purpose is unsupported");
  }
  if (requiredString(publisherSignature.signature_algorithm, "publisher_signature.signature_algorithm") !== "ed25519") {
    throw new VerificationError("workflow object publisher signature algorithm must be ed25519");
  }
  const publisherIdentity = verifyOwnerIdentity(
    requiredObject(publisherSignature.owner_identity, "publisher_signature.owner_identity"),
    expectedPublisher,
  );
  const publicKey = crypto.createPublicKey(requiredString(publisherIdentity.owner_public_key_pem, "owner public key"));
  const signature = decodeBase64(
    requiredString(publisherSignature.signature, "publisher_signature.signature"),
    "publisher_signature.signature",
  );
  const payload = {
    purpose: WORKFLOW_BUNDLE_SIGNATURE_PURPOSE,
    manifest,
  };
  if (!crypto.verify(null, canonicalJsonBytes(payload), publicKey, signature)) {
    throw new VerificationError("workflow object publisher signature verification failed");
  }
  return publisherIdentity;
}

function verifyOwnerIdentity(identity, expectedPublisher) {
  if (identity.version !== OWNER_IDENTITY_VERSION) {
    throw new VerificationError("owner identity version is unsupported");
  }
  const ownerUrl = requiredString(identity.owner_url, "owner_identity.owner_url").replace(/\/+$/, "");
  const ownerDomain = requiredString(identity.owner_domain, "owner_identity.owner_domain");
  const parsedUrl = new URL(ownerUrl);
  if (!["http:", "https:"].includes(parsedUrl.protocol) || !parsedUrl.hostname) {
    throw new VerificationError("owner identity owner_url must be an HTTP or HTTPS origin");
  }
  if (parsedUrl.hostname.toLowerCase() !== ownerDomain) {
    throw new VerificationError("owner identity owner_url hostname does not match owner_domain");
  }
  if (ownerDomain !== expectedPublisher) {
    throw new VerificationError("owner identity owner_domain does not match publisher");
  }
  const notBefore = Date.parse(requiredString(identity.not_before, "owner_identity.not_before"));
  const notAfter = Date.parse(requiredString(identity.not_after, "owner_identity.not_after"));
  const now = Date.now();
  if (!Number.isFinite(notBefore) || !Number.isFinite(notAfter)) {
    throw new VerificationError("owner identity validity timestamps are invalid");
  }
  if (now < notBefore) {
    throw new VerificationError("owner identity is not valid yet");
  }
  if (now > notAfter) {
    throw new VerificationError("owner identity is expired");
  }
  const publicKeyPem = requiredString(identity.owner_public_key_pem, "owner_identity.owner_public_key_pem");
  if (sha256Literal(Buffer.from(publicKeyPem, "utf-8")) !== requiredString(identity.owner_public_key_sha256, "owner_identity.owner_public_key_sha256")) {
    throw new VerificationError("owner identity public key hash mismatch");
  }
  if (requiredString(identity.signature_algorithm, "owner_identity.signature_algorithm") !== "ed25519") {
    throw new VerificationError("owner identity signature algorithm must be ed25519");
  }
  const signature = decodeBase64(requiredString(identity.signature, "owner_identity.signature"), "owner_identity.signature");
  const publicKey = crypto.createPublicKey(publicKeyPem);
  const signaturePayload = {
    version: OWNER_IDENTITY_VERSION,
    purpose: OWNER_IDENTITY_PURPOSE,
    owner_url: ownerUrl,
    owner_domain: ownerDomain,
    owner_public_key_pem: publicKeyPem,
    owner_public_key_sha256: requiredString(identity.owner_public_key_sha256, "owner_identity.owner_public_key_sha256"),
    not_before: requiredString(identity.not_before, "owner_identity.not_before"),
    not_after: requiredString(identity.not_after, "owner_identity.not_after"),
  };
  if (!crypto.verify(null, canonicalJsonBytes(signaturePayload), publicKey, signature)) {
    throw new VerificationError("owner identity signature verification failed");
  }
  return { ...identity, owner_url: ownerUrl };
}

async function verifyNodeCertificate(certificate, {
  expectedWorkflowId,
  expectedNodeId,
  expectedComposeHash,
  context,
  seen,
}) {
  const cert = requiredObject(certificate, "node certificate");
  const body = requiredObject(cert.certificate_body, "certificate_body");
  const bodyHash = requiredString(cert.certificate_body_hash, "certificate_body_hash");
  if (sha256Literal(canonicalJsonBytes(body)) !== bodyHash) {
    throw new VerificationError("certificate_body_hash does not match canonical certificate body");
  }
  const nodeId = requiredString(body.node_id, "certificate_body.node_id");
  if (nodeId !== expectedNodeId) {
    throw new VerificationError(`certificate node id ${nodeId} does not match expected ${expectedNodeId}`);
  }
  if (seen.has(nodeId)) {
    throw new VerificationError(`certificate dependency graph contains a cycle at ${nodeId}`);
  }
  const nextSeen = new Set(seen);
  nextSeen.add(nodeId);
  if (requiredString(body.workflow_id, "certificate_body.workflow_id") !== expectedWorkflowId) {
    throw new VerificationError(`certificate workflow id does not match ${expectedWorkflowId}`);
  }
  const composeHash = requiredString(body.generated_node_compose_hash, "certificate_body.generated_node_compose_hash");
  if (composeHash !== expectedComposeHash) {
    throw new VerificationError(`certificate compose hash mismatch for node ${nodeId}`);
  }
  if (context.composeByNode.get(nodeId) !== composeHash) {
    throw new VerificationError(`certificate for node ${nodeId} has stale or unexpected compose hash`);
  }
  const existingBodyHash = context.certificateBodyHashes.get(nodeId);
  if (existingBodyHash && existingBodyHash !== bodyHash) {
    throw new VerificationError(`certificate closure has conflicting certificates for node ${nodeId}`);
  }
  context.certificateBodyHashes.set(nodeId, bodyHash);
  context.certificates.set(nodeId, cert);

  const attestationBundle = requiredObject(cert.attestation_bundle, "attestation_bundle");
  if (requiredString(attestationBundle.quoted_certificate_body_hash, "attestation_bundle.quoted_certificate_body_hash") !== bodyHash) {
    throw new VerificationError("attestation quoted certificate body hash mismatch");
  }
  if (requiredString(attestationBundle.node_id, "attestation_bundle.node_id") !== nodeId) {
    throw new VerificationError("attestation node_id does not match certificate body node_id");
  }
  if (requiredString(attestationBundle.generated_node_compose_hash, "attestation_bundle.generated_node_compose_hash") !== composeHash) {
    throw new VerificationError("attestation compose hash does not match certificate body compose hash");
  }
  const reportData = buildNodeCertificateReportData({
    certificateBodyHash: bodyHash,
    composeHash,
  });
  await verifyAttestationBundle(attestationBundle, {
    expectedReportData: reportData,
    expectedComposeHash: composeHash,
  });

  const dependencies = requiredObject(body.dependencies, "certificate_body.dependencies");
  const expectedDependencies = new Set(context.dependencyEdges.get(nodeId) || []);
  const observedDependencies = new Set(Object.keys(dependencies));
  const missing = [...expectedDependencies].filter((name) => !observedDependencies.has(name));
  if (missing.length > 0) {
    throw new VerificationError(`certificate for node ${nodeId} is missing dependency certificates: ${missing.join(", ")}`);
  }
  const unexpected = [...observedDependencies].filter((name) => !expectedDependencies.has(name));
  if (unexpected.length > 0) {
    throw new VerificationError(`certificate for node ${nodeId} has unexpected dependency certificates: ${unexpected.join(", ")}`);
  }
  for (const dependencyName of Object.keys(dependencies)) {
    const dependencyComposeHash = context.composeByNode.get(dependencyName);
    if (!dependencyComposeHash) {
      throw new VerificationError(`dependency certificate ${dependencyName} is not in workflow bundle`);
    }
    await verifyNodeCertificate(dependencies[dependencyName], {
      expectedWorkflowId,
      expectedNodeId: dependencyName,
      expectedComposeHash: dependencyComposeHash,
      context,
      seen: nextSeen,
    });
  }
}

async function verifyAttestationBundle(attestationBundle, {
  expectedReportData,
  expectedComposeHash,
}) {
  if (requiredString(attestationBundle.format, "attestation_bundle.format") !== PHALA_DSTACK_ATTESTATION_FORMAT) {
    throw new VerificationError("unsupported attestation format");
  }
  const bundleReportData = requiredString(attestationBundle.report_data, "attestation_bundle.report_data");
  if (!reportDataMatches(bundleReportData, expectedReportData)) {
    throw new VerificationError("attestation_bundle.report_data does not match expected report data");
  }
  const quoteResponse = await requestJson(new URL(PHALA_DSTACK_VERIFY_URL), {
    method: "POST",
    timeoutMs: 30_000,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hex: requiredString(attestationBundle.quote, "attestation_bundle.quote") }),
  });
  if (quoteResponse.success !== true || !quoteResponse.quote || quoteResponse.quote.verified !== true) {
    throw new VerificationError("Phala quote verification failed");
  }
  const header = requiredObject(quoteResponse.quote.header, "quote.header");
  const body = requiredObject(quoteResponse.quote.body, "quote.body");
  if (!requiredString(header.tee_type, "quote.header.tee_type").toLowerCase().includes("tdx")) {
    throw new VerificationError("verified quote is not a TDX quote");
  }
  const verifiedReportData = hexField(body, ["reportdata", "report_data"], "quote.body.reportdata", REPORT_DATA_BYTES);
  if (!reportDataMatches(verifiedReportData, expectedReportData)) {
    throw new VerificationError("verified quote report data does not match expected report data");
  }
  hexField(body, ["mrtd", "mr_td"], "quote.body.mrtd", RTMR_BYTES);
  hexField(body, ["rtmr0", "rt_mr0", "rtmr_0"], "quote.body.rtmr0", RTMR_BYTES);
  hexField(body, ["rtmr1", "rt_mr1", "rtmr_1"], "quote.body.rtmr1", RTMR_BYTES);
  hexField(body, ["rtmr2", "rt_mr2", "rtmr_2"], "quote.body.rtmr2", RTMR_BYTES);
  const rtmr3 = hexField(body, ["rtmr3", "rt_mr3", "rtmr_3"], "quote.body.rtmr3", RTMR_BYTES);

  const eventLog = parseAttestedEventLog(attestationBundle);
  const replayed = replayRtmr3(eventLog);
  if (replayed.rtmr3.toString("hex") !== rtmr3) {
    throw new VerificationError("RTMR3 event log replay mismatch");
  }
  const appCompose = optionalAppCompose(attestationBundle);
  if (appCompose !== null) {
    const appComposeHash = crypto.createHash("sha256").update(Buffer.from(appCompose, "utf-8")).digest("hex");
    if (appComposeHash !== replayed.composeEventPayload) {
      throw new VerificationError("RTMR3 compose-hash event does not match attested app_compose");
    }
    const attestedComposeHash = optionalAttestedComposeHash(attestationBundle);
    if (
      attestedComposeHash !== null &&
      normalizeSha256Literal(attestedComposeHash) !== replayed.composeEventPayload
    ) {
      throw new VerificationError("attested compose hash does not match RTMR3 compose-hash event");
    }
    verifyAppComposeReferencesGeneratedComposeHash(appCompose, expectedComposeHash);
  } else if (replayed.composeEventPayload !== normalizeSha256Literal(expectedComposeHash)) {
    throw new VerificationError("RTMR3 compose-hash event does not match expected compose hash");
  }
}

function buildNodeCertificateReportData({ certificateBodyHash, composeHash }) {
  const payloadHash = crypto
    .createHash("sha256")
    .update(canonicalJsonBytes({
      certificate_body_hash: certificateBodyHash,
      generated_node_compose_hash: composeHash,
    }))
    .digest();
  return Buffer.concat([NODE_CERTIFICATE_REPORT_LABEL, payloadHash]);
}

function parseAttestedEventLog(attestationBundle) {
  const info = attestationBundle.info;
  if (info && typeof info === "object" && !Array.isArray(info)) {
    const tcbInfo = info.tcb_info;
    if (tcbInfo && typeof tcbInfo === "object" && tcbInfo.event_log !== undefined) {
      return parseEventLog(tcbInfo.event_log, "info.tcb_info.event_log");
    }
  }
  return parseEventLog(attestationBundle.event_log, "attestation_bundle.event_log");
}

function parseEventLog(value, label) {
  const decoded = typeof value === "string" ? JSON.parse(value) : value;
  if (!Array.isArray(decoded)) {
    throw new VerificationError(`${label} must be a JSON array`);
  }
  for (const event of decoded) {
    requiredObject(event, `${label}[]`);
  }
  return decoded;
}

function replayRtmr3(eventLog) {
  let digest = Buffer.alloc(RTMR_BYTES);
  let observedAny = false;
  const composePayloads = [];
  for (const event of eventLog) {
    const imr = requiredInteger(event.imr, "event_log[].imr");
    if (imr !== RTMR3_INDEX) continue;
    observedAny = true;
    const eventType = requiredInteger(event.event_type, "event_log[].event_type");
    if (eventType !== DSTACK_EVENT_TYPE) {
      throw new VerificationError("RTMR3 event has unsupported event_type");
    }
    const eventName = requiredString(event.event, "event_log[].event");
    const eventPayload = requiredStringValue(event.event_payload, "event_log[].event_payload");
    const payloadBytes = hexBytes(eventPayload, "event_log[].event_payload");
    const expectedEventDigest = eventDigest({
      eventType,
      eventName,
      eventPayload: payloadBytes,
    });
    const eventDigestBytes = bytesField(event.digest, "event_log[].digest", RTMR_BYTES);
    if (!eventDigestBytes.equals(expectedEventDigest)) {
      throw new VerificationError("RTMR3 event digest does not match event payload");
    }
    digest = crypto.createHash("sha384").update(Buffer.concat([digest, eventDigestBytes])).digest();
    if (eventName === "compose-hash") {
      const composePayload = normalizeHex(eventPayload);
      if (!composePayload) {
        throw new VerificationError("RTMR3 compose-hash event payload is empty");
      }
      composePayloads.push(composePayload);
    }
  }
  if (!observedAny) {
    throw new VerificationError("attestation event log has no RTMR3 events");
  }
  if (composePayloads.length !== 1) {
    throw new VerificationError("attestation event log must contain exactly one RTMR3 compose-hash event");
  }
  return { rtmr3: digest, composeEventPayload: composePayloads[0] };
}

function eventDigest({ eventType, eventName, eventPayload }) {
  const type = Buffer.alloc(4);
  type.writeUInt32LE(eventType);
  return crypto
    .createHash("sha384")
    .update(Buffer.concat([
      type,
      Buffer.from(":", "utf-8"),
      Buffer.from(eventName, "utf-8"),
      Buffer.from(":", "utf-8"),
      eventPayload,
    ]))
    .digest();
}

function optionalAppCompose(attestationBundle) {
  const tcbInfo = attestationBundle.info?.tcb_info;
  const appCompose = tcbInfo?.app_compose;
  return typeof appCompose === "string" && appCompose.trim() ? appCompose : null;
}

function optionalAttestedComposeHash(attestationBundle) {
  const info = attestationBundle.info;
  const tcbInfo = info?.tcb_info;
  const value = tcbInfo?.compose_hash ?? info?.compose_hash;
  return typeof value === "string" && value.trim() ? value : null;
}

function verifyAppComposeReferencesGeneratedComposeHash(appCompose, expectedComposeHash) {
  let payload;
  try {
    payload = JSON.parse(appCompose);
  } catch (error) {
    throw new VerificationError("attested app_compose is not valid JSON");
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new VerificationError("attested app_compose must be a JSON object");
  }
  const dockerComposeFile = payload.docker_compose_file;
  if (typeof dockerComposeFile !== "string" || !dockerComposeFile.trim()) {
    throw new VerificationError("attested app_compose is missing docker_compose_file");
  }
  if (!dockerComposeFile.includes(`COVE_COMPOSE_HASH: ${expectedComposeHash}`)) {
    throw new VerificationError("attested docker_compose_file does not reference expected generated compose hash");
  }
}

async function inspectPinnedTls(endpoint, expectedDer) {
  const url = normalizeEndpoint(endpoint);
  return new Promise((resolve, reject) => {
    const socket = tls.connect(
      {
        host: url.hostname,
        port: Number(url.port || 443),
        servername: url.hostname,
        rejectUnauthorized: false,
        timeout: 15_000,
      },
      () => {
        try {
          const peer = socket.getPeerCertificate(true);
          if (!peer?.raw || !Buffer.from(peer.raw).equals(expectedDer)) {
            throw new VerificationError("live TLS certificate DER does not match verified node certificate");
          }
          const cipher = socket.getCipher();
          resolve({
            protocol: socket.getProtocol(),
            cipher: cipher ? cipher.name : null,
            authorized: socket.authorized,
            authorizationError: socket.authorizationError || null,
            subject: peer.subject || {},
            issuer: peer.issuer || {},
            fingerprint256: peer.fingerprint256 || null,
            valid_from: peer.valid_from || null,
            valid_to: peer.valid_to || null,
            serialNumber: peer.serialNumber || null,
            certificate_der_sha256: sha256Literal(Buffer.from(peer.raw)),
          });
        } catch (error) {
          reject(error);
        } finally {
          socket.end();
        }
      },
    );
    socket.on("timeout", () => socket.destroy(new VerificationError("TLS inspection timed out")));
    socket.on("error", reject);
  });
}

function parseWorkflowDependencies(yamlText) {
  const edges = new Map();
  let inNodes = false;
  let currentNode = null;
  let inDependencies = false;
  for (const line of yamlText.split(/\r?\n/)) {
    if (/^nodes:\s*$/.test(line)) {
      inNodes = true;
      currentNode = null;
      inDependencies = false;
      continue;
    }
    if (!inNodes) continue;
    if (line.length > 0 && !line.startsWith(" ")) {
      break;
    }
    const nodeMatch = /^  ([A-Za-z0-9._-]+):\s*$/.exec(line);
    if (nodeMatch) {
      currentNode = nodeMatch[1];
      edges.set(currentNode, []);
      inDependencies = false;
      continue;
    }
    if (!currentNode) continue;
    if (/^    dependencies:\s*$/.test(line)) {
      inDependencies = true;
      continue;
    }
    if (inDependencies) {
      const dependencyMatch = /^    -\s*([A-Za-z0-9._-]+)\s*$/.exec(line);
      if (dependencyMatch) {
        edges.get(currentNode).push(dependencyMatch[1]);
        continue;
      }
      if (/^    [A-Za-z0-9._-]+:/.test(line) || /^  [A-Za-z0-9._-]+:/.test(line)) {
        inDependencies = false;
      }
    }
  }
  return edges;
}

function buildChecks({ workflow, model, modelIds, certificates, bundle, tlsInfo, health }) {
  const checks = [];
  const add = (label, pass, detail) => checks.push({ label, pass: Boolean(pass), detail: detail || "" });
  const nodes = [...bundle.composeByNode.keys()];
  const getBody = (node) => certificates[node]?.certificate_body || {};
  const getResult = (node, service) => getBody(node).results?.[service] || {};
  const getInputHash = (node, artifact) => getBody(node).inputs?.[artifact]?.plaintext_hash || null;
  const getOutputHash = (node, artifact) => getBody(node).outputs?.[artifact]?.plaintext_hash || null;
  const auditServing = getResult("audit_serving_code", "audit_agent");
  const auditEval = getResult("audit_eval_code", "audit_agent");
  const compile = getResult("compile_serving_code", "compile_serving_wheel");
  const benchmark = getResult("model_benchmark", "benchmark_runner");
  const deploymentInputs = getBody("model_deployment").inputs || {};

  add("Workflow bundle signature verified", true, bundle.manifest.manifest_hash);
  add("Publisher identity verified", true, bundle.publisherIdentity.owner_domain);
  add("All expected node certificates verified", nodes.every((node) => certificates[node]), nodes.join(", "));
  add("Dependency certificate closure verified", true, `${bundle.dependencyEdges.size} nodes`);
  add("RTMR3 compose measurements verified", true, "event logs replay to quoted RTMR3");
  add("Live RA-TLS certificate pinned", true, tlsInfo.certificate_der_sha256);
  add(
    "Certificates bind to selected workflow",
    nodes.every((node) => getBody(node).workflow_id === workflow && getBody(node).node_id === node),
    workflow,
  );
  add("Serving patch audit used public audit model", auditServing.llm_used === true, "public model path is a compose-measured trust assumption");
  add("Eval-code audit used public audit model", auditEval.llm_used === true, "public model path is a compose-measured trust assumption");
  add("Serving patch audit passed", auditServing.pass === true, auditServing.audited_sha256 || "missing");
  add("Eval-code audit passed", auditEval.pass === true, auditEval.audited_sha256 || "missing");
  add("Compile node passed", compile.pass === true, compile.compiled_wheel_sha256 || "missing");
  add("Benchmark refusal rate > 40%", benchmark.pass === true && benchmark.passes_threshold === true, `refusal ${benchmark.refusal_rate ?? "missing"}`);
  add(
    "Serving audit hash matches input",
    sameHash(auditServing.audited_sha256, getInputHash("audit_serving_code", "alice_private_serving_patch")),
    `${auditServing.audited_sha256 || "missing"} == ${getInputHash("audit_serving_code", "alice_private_serving_patch") || "missing"}`,
  );
  add(
    "Eval audit hash matches input",
    sameHash(auditEval.audited_sha256, getInputHash("audit_eval_code", "bob_private_eval_code")),
    `${auditEval.audited_sha256 || "missing"} == ${getInputHash("audit_eval_code", "bob_private_eval_code") || "missing"}`,
  );
  add(
    "Compile consumed audited serving patch",
    sameHash(compile.serving_patch_sha256, auditServing.audited_sha256),
    `${compile.serving_patch_sha256 || "missing"} == ${auditServing.audited_sha256 || "missing"}`,
  );
  add(
    "Compile output hash matches result",
    sameHash(getOutputHash("compile_serving_code", "compiled_serving_wheel"), compile.compiled_wheel_sha256),
    `${getOutputHash("compile_serving_code", "compiled_serving_wheel") || "missing"} == ${compile.compiled_wheel_sha256 || "missing"}`,
  );
  add(
    "Benchmark consumed audited eval code",
    sameHash(benchmark.eval_code_sha256, auditEval.audited_sha256),
    `${benchmark.eval_code_sha256 || "missing"} == ${auditEval.audited_sha256 || "missing"}`,
  );
  add(
    "Benchmark used compiled serving wheel",
    sameHash(benchmark.serving_wheel_sha256, compile.compiled_wheel_sha256)
      && sameHash(getInputHash("model_benchmark", "compiled_serving_wheel"), compile.compiled_wheel_sha256),
    `${benchmark.serving_wheel_sha256 || "missing"} == ${compile.compiled_wheel_sha256 || "missing"}`,
  );
  add(
    "Benchmark model input matches result",
    sameHash(benchmark.model_archive_sha256, getInputHash("model_benchmark", "alice_private_model")),
    benchmark.model_archive_sha256 || "missing",
  );
  add(
    "Deployment model matches benchmark",
    sameHash(deploymentInputs.alice_private_model?.plaintext_hash, benchmark.model_archive_sha256),
    `${deploymentInputs.alice_private_model?.plaintext_hash || "missing"} == ${benchmark.model_archive_sha256 || "missing"}`,
  );
  add(
    "Deployment wheel matches benchmark",
    sameHash(deploymentInputs.compiled_serving_wheel?.plaintext_hash, benchmark.serving_wheel_sha256),
    `${deploymentInputs.compiled_serving_wheel?.plaintext_hash || "missing"} == ${benchmark.serving_wheel_sha256 || "missing"}`,
  );
  add("Endpoint health passed", health.payload?.status === "ok", health.payload?.status || "missing");
  add("Endpoint serves selected model", !model || modelIds.includes(model), modelIds.join(", ") || "model list unavailable");
  return checks;
}

function sameHash(a, b) {
  return typeof a === "string" && /^sha256:[0-9a-f]{64}$/.test(a) && a === b;
}

function samplingFields(payload) {
  const output = {};
  for (const name of SAMPLING_FIELD_NAMES) {
    if (Object.prototype.hasOwnProperty.call(payload, name)) {
      output[name] = payload[name];
    }
  }
  return output;
}

async function requestJson(url, options = {}) {
  const body = options.body === undefined ? undefined : Buffer.from(options.body);
  const headers = {
    Accept: "application/json",
    "User-Agent": "cove-attested-eval-client/0.1",
    ...(options.headers || {}),
  };
  if (body !== undefined) {
    headers["Content-Length"] = String(body.length);
  }
  return new Promise((resolve, reject) => {
    const isHttps = url.protocol === "https:";
    const transport = isHttps ? https : http;
    const request = transport.request(
      {
        protocol: url.protocol,
        hostname: url.hostname,
        port: url.port || (isHttps ? 443 : 80),
        path: `${url.pathname}${url.search}`,
        method: options.method || "GET",
        headers,
        timeout: options.timeoutMs || 60_000,
        rejectUnauthorized: options.rejectUnauthorized !== false,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf-8");
          let payload = null;
          if (text.length > 0) {
            try {
              payload = JSON.parse(text);
            } catch {
              payload = { raw: text };
            }
          }
          if ((response.statusCode || 0) >= 400) {
            const error = new VerificationError(`HTTP ${response.statusCode} from ${url.href}`);
            error.statusCode = response.statusCode;
            error.payload = payload;
            reject(error);
            return;
          }
          resolve(payload);
        });
      },
    );
    request.on("timeout", () => request.destroy(new VerificationError(`request timed out: ${url.href}`)));
    request.on("error", reject);
    if (body !== undefined) {
      request.write(body);
    }
    request.end();
  });
}

function canonicalJsonBytes(value) {
  return Buffer.from(JSON.stringify(sortForCanonicalJson(value)), "utf-8");
}

function sortForCanonicalJson(value) {
  if (Array.isArray(value)) {
    return value.map(sortForCanonicalJson);
  }
  if (value && typeof value === "object" && !(value instanceof Buffer)) {
    const sorted = {};
    for (const key of Object.keys(value).sort()) {
      sorted[key] = sortForCanonicalJson(value[key]);
    }
    return sorted;
  }
  return value;
}

function sha256Literal(payload) {
  return `sha256:${crypto.createHash("sha256").update(payload).digest("hex")}`;
}

function reportDataMatches(observedHex, expectedReportData) {
  const observed = normalizeHex(observedHex);
  const expected = expectedReportData.toString("hex");
  if (observed === expected) return true;
  if (expectedReportData.length < REPORT_DATA_BYTES) {
    return observed === Buffer.concat([
      expectedReportData,
      Buffer.alloc(REPORT_DATA_BYTES - expectedReportData.length),
    ]).toString("hex");
  }
  return false;
}

function hexField(payload, keys, label, expectedLength) {
  for (const key of keys) {
    if (typeof payload[key] === "string" && payload[key].trim()) {
      const value = hexBytes(payload[key], label);
      if (value.length !== expectedLength) {
        throw new VerificationError(`${label} must be ${expectedLength} bytes`);
      }
      return normalizeHex(payload[key]);
    }
  }
  throw new VerificationError(`${label} must be a non-empty hex string`);
}

function bytesField(value, label, expectedLength) {
  let bytes;
  if (typeof value === "string") {
    bytes = hexBytes(value, label);
  } else if (Array.isArray(value) && value.every((item) => Number.isInteger(item))) {
    bytes = Buffer.from(value);
  } else {
    throw new VerificationError(`${label} must be hex or a byte array`);
  }
  if (bytes.length !== expectedLength) {
    throw new VerificationError(`${label} must be ${expectedLength} bytes`);
  }
  return bytes;
}

function hexBytes(value, label) {
  const normalized = normalizeHex(value);
  if (!/^[0-9a-f]*$/.test(normalized) || normalized.length % 2 !== 0) {
    throw new VerificationError(`${label} must be hex`);
  }
  return Buffer.from(normalized, "hex");
}

function normalizeHex(value) {
  const lowered = value.trim().toLowerCase();
  return lowered.startsWith("0x") ? lowered.slice(2) : lowered;
}

function normalizeSha256Literal(value) {
  const normalized = value.trim().toLowerCase().replace(/^sha256:/, "");
  if (!/^[0-9a-f]{64}$/.test(normalized)) {
    throw new VerificationError("expected compose hash must be sha256");
  }
  return normalized;
}

function decodeBase64(value, label) {
  if (typeof value !== "string" || !value.trim()) {
    throw new VerificationError(`${label} must be base64`);
  }
  try {
    return Buffer.from(value, "base64");
  } catch (error) {
    throw new VerificationError(`${label} must be base64`);
  }
}

function requiredArray(value, label) {
  if (!Array.isArray(value)) {
    throw new VerificationError(`${label} must be an array`);
  }
  return value;
}

function requiredObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new VerificationError(`${label} must be an object`);
  }
  return value;
}

function requiredString(value, label) {
  if (typeof value !== "string" || !value.trim()) {
    throw new VerificationError(`${label} must be a non-empty string`);
  }
  return value;
}

function requiredStringValue(value, label) {
  if (typeof value !== "string") {
    throw new VerificationError(`${label} must be a string`);
  }
  return value;
}

function requiredInteger(value, label) {
  if (!Number.isInteger(value)) {
    throw new VerificationError(`${label} must be an integer`);
  }
  return value;
}
