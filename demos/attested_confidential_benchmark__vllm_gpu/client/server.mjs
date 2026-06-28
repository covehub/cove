#!/usr/bin/env node

import fs from "node:fs";
import http from "node:http";
import { randomUUID, X509Certificate } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as verifier from "./verifier.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 5177);

const DEFAULTS = {
  endpoint: process.env.COVE_DEMO_ENDPOINT || "",
  apiBase: process.env.COVE_API_BASE || "https://api.covehub.io",
  publisher: process.env.COVE_PUBLISHER || "demo-carol.covehub.io",
  workflow: process.env.COVE_WORKFLOW_ID || "attested_confidential_benchmark__vllm_gpu",
  model: process.env.COVE_MODEL_NAME || "CoveDemoModel",
};

const VERIFICATION_TTL_MS = 5 * 60 * 1000;
const verifiedTargets = new Map();

const CONTENT_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

function sendJson(response, statusCode, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
  });
  response.end(body);
}

function sendText(response, statusCode, contentType, body) {
  response.writeHead(statusCode, {
    "Content-Type": contentType,
    "Content-Length": Buffer.byteLength(body),
  });
  response.end(body);
}

function parseRequestBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    request.on("data", (chunk) => {
      size += chunk.length;
      if (size > 2_000_000) {
        reject(new Error("request body is too large"));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => {
      if (chunks.length === 0) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf-8")));
      } catch (error) {
        reject(new Error(`invalid JSON request body: ${error.message}`));
      }
    });
    request.on("error", reject);
  });
}

async function handleVerify(requestUrl, response) {
  const publisher = requestUrl.searchParams.get("publisher") || DEFAULTS.publisher;
  const workflow = requestUrl.searchParams.get("workflow") || DEFAULTS.workflow;
  const apiBase = requestUrl.searchParams.get("apiBase") || DEFAULTS.apiBase;
  const endpoint = requestUrl.searchParams.get("endpoint") || DEFAULTS.endpoint;
  const model = requestUrl.searchParams.get("model") || DEFAULTS.model;
  const verification = await verifier.verifyDemoTarget({
    endpoint,
    apiBase,
    publisher,
    workflow,
    model,
  });
  const terminalKeypair =
    verification.certificates?.model_deployment?.certificate_body?.ephemeral_keypairs?.ratls_key;
  const certificatePem = terminalKeypair?.certificate_pem;
  const publicKeyPem = terminalKeypair?.public_key_pem;
  if (typeof certificatePem !== "string" || typeof publicKeyPem !== "string") {
    throw new Error("verified model_deployment certificate is missing ratls_key material");
  }
  const verificationId = randomUUID();
  const expiresAt = new Date(Date.now() + VERIFICATION_TTL_MS).toISOString();
  verifiedTargets.set(verificationId, {
    verification,
    expectedCertificateDer: new X509Certificate(certificatePem).raw,
    publicKeyPem,
    expiresAtMs: Date.now() + VERIFICATION_TTL_MS,
  });
  sendJson(response, 200, {
    verification_id: verificationId,
    expires_at: expiresAt,
    ...verification,
  });
}

async function handleChat(request, response) {
  const body = await parseRequestBody(request);
  const verificationId = String(body.verification_id || "");
  const target = verifiedTargets.get(verificationId);
  if (!target || target.expiresAtMs < Date.now()) {
    verifiedTargets.delete(verificationId);
    const error = new Error("a recent successful verification id is required");
    error.statusCode = 403;
    throw error;
  }
  const endpoint = target.verification.endpoint;
  const model = target.verification.model || DEFAULTS.model;
  const prompt = String(body.prompt || "");
  const maxTokens = Math.max(1, Math.min(Number(body.max_tokens || 160), 1024));
  const temperature = Number.isFinite(Number(body.temperature)) ? Number(body.temperature) : 0;
  const url = verifier.endpointUrl(endpoint, "/v1/chat/completions");

  const payload = {
    model,
    messages: body.messages || [{ role: "user", content: prompt }],
    max_tokens: maxTokens,
    temperature,
    stream: false,
  };
  const nonce = randomUUID();
  const result = await verifier.pinnedJsonRequest(url, {
    method: "POST",
    expectedCertificateDer: target.expectedCertificateDer,
    timeoutMs: 180_000,
    headers: {
      "Content-Type": "application/json",
      "X-Cove-Nonce": nonce,
    },
    body: JSON.stringify(payload),
  });
  verifier.verifyModelResponseReceipt({
    requestPayload: payload,
    responsePayload: result.payload,
    receipt: result.payload?.cove_receipt,
    publicKeyPem: target.publicKeyPem,
    expectedNonce: nonce,
  });

  sendJson(response, 200, {
    endpoint: url.href,
    request: {
      model,
      max_tokens: maxTokens,
      temperature,
      messages: payload.messages,
    },
    response: result.payload,
    receipt_verified: true,
  });
}

function serveStatic(requestUrl, response) {
  const pathname = requestUrl.pathname === "/" ? "/index.html" : requestUrl.pathname;
  const safePath = path.normalize(pathname).replace(/^(\.\.[/\\])+/, "");
  const filePath = path.join(__dirname, safePath);
  if (!filePath.startsWith(__dirname)) {
    sendJson(response, 403, { error: "forbidden" });
    return;
  }
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    sendJson(response, 404, { error: "not found" });
    return;
  }
  const extension = path.extname(filePath);
  sendText(response, 200, CONTENT_TYPES[extension] || "application/octet-stream", fs.readFileSync(filePath));
}

const server = http.createServer(async (request, response) => {
  const requestUrl = new URL(request.url || "/", `http://${request.headers.host || "127.0.0.1"}`);
  try {
    if (request.method === "GET" && requestUrl.pathname === "/config.js") {
      sendText(
        response,
        200,
        "text/javascript; charset=utf-8",
        `window.COVE_CLIENT_DEFAULTS = ${JSON.stringify(DEFAULTS)};\n`,
      );
      return;
    }
    if (request.method === "GET" && requestUrl.pathname === "/api/verify") {
      await handleVerify(requestUrl, response);
      return;
    }
    if (request.method === "POST" && requestUrl.pathname === "/api/chat") {
      await handleChat(request, response);
      return;
    }
    if (request.method === "GET") {
      serveStatic(requestUrl, response);
      return;
    }
    sendJson(response, 405, { error: "method not allowed" });
  } catch (error) {
    sendJson(response, error.statusCode || 500, {
      error: error.message,
      statusCode: error.statusCode || null,
      payload: error.payload || null,
    });
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`Cove client listening on http://127.0.0.1:${PORT}`);
});
