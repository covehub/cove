#!/usr/bin/env node

import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import path from "node:path";
import tls from "node:tls";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 5177);

const DEFAULTS = {
  endpoint: process.env.COVE_DEMO_ENDPOINT || "",
  apiBase: process.env.COVE_API_BASE || "https://api.covehub.io",
  publisher: process.env.COVE_PUBLISHER || "demo-carol.covehub.io",
  workflow: process.env.COVE_WORKFLOW_ID || "attested_confidential_benchmark__vllm_cpu",
  model: process.env.COVE_MODEL_NAME || "CoveDemoModel",
};

const CERTIFICATE_NODES = [
  "audit_serving_code",
  "audit_eval_code",
  "compile_serving_code",
  "model_benchmark",
  "model_deployment",
];

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

function normalizeEndpoint(input) {
  if (!input || typeof input !== "string") {
    throw new Error("endpoint is required");
  }
  const url = new URL(input);
  if (!["http:", "https:"].includes(url.protocol)) {
    throw new Error("endpoint must be http or https");
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

function endpointUrl(input, suffix) {
  const url = normalizeEndpoint(input);
  const basePath = url.pathname.replace(/\/+$/, "");
  url.pathname = `${basePath}${suffix}`;
  return url;
}

function requestJson(url, options = {}) {
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
        timeout: options.timeoutMs || 120_000,
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
            const error = new Error(`HTTP ${response.statusCode} from ${url.href}`);
            error.statusCode = response.statusCode;
            error.payload = payload;
            reject(error);
            return;
          }
          resolve({ statusCode: response.statusCode, headers: response.headers, payload });
        });
      },
    );
    request.on("timeout", () => request.destroy(new Error(`request timed out: ${url.href}`)));
    request.on("error", reject);
    if (body !== undefined) {
      request.write(body);
    }
    request.end();
  });
}

function inspectTls(endpoint) {
  const url = normalizeEndpoint(endpoint);
  if (url.protocol !== "https:") {
    return Promise.resolve({ enabled: false, reason: "endpoint is not HTTPS" });
  }
  const port = Number(url.port || 443);

  return new Promise((resolve, reject) => {
    const socket = tls.connect(
      {
        host: url.hostname,
        port,
        servername: url.hostname,
        rejectUnauthorized: false,
        timeout: 15_000,
      },
      () => {
        const peer = socket.getPeerCertificate(true);
        const cipher = socket.getCipher();
        resolve({
          enabled: true,
          authorized: socket.authorized,
          authorizationError: socket.authorizationError || null,
          protocol: socket.getProtocol(),
          cipher: cipher ? cipher.name : null,
          subject: peer.subject || {},
          issuer: peer.issuer || {},
          fingerprint256: peer.fingerprint256 || null,
          valid_from: peer.valid_from || null,
          valid_to: peer.valid_to || null,
          serialNumber: peer.serialNumber || null,
        });
        socket.end();
      },
    );
    socket.on("timeout", () => socket.destroy(new Error("TLS inspection timed out")));
    socket.on("error", reject);
  });
}

async function handleCertificates(requestUrl, response) {
  const publisher = requestUrl.searchParams.get("publisher") || DEFAULTS.publisher;
  const workflow = requestUrl.searchParams.get("workflow") || DEFAULTS.workflow;
  const apiBase = requestUrl.searchParams.get("apiBase") || DEFAULTS.apiBase;
  const certificates = {};
  const errors = {};

  await Promise.all(
    CERTIFICATE_NODES.map(async (node) => {
      const url = new URL(
        `/v1/runtime/${encodeURIComponent(publisher)}/${encodeURIComponent(workflow)}/certificates/${node}/latest`,
        apiBase,
      );
      try {
        const result = await requestJson(url, { timeoutMs: 60_000 });
        certificates[node] = result.payload;
      } catch (error) {
        errors[node] = {
          message: error.message,
          statusCode: error.statusCode || null,
          payload: error.payload || null,
        };
      }
    }),
  );

  sendJson(response, Object.keys(errors).length === 0 ? 200 : 207, {
    apiBase,
    publisher,
    workflow,
    certificates,
    errors,
  });
}

async function handleHealth(requestUrl, response) {
  const endpoint = requestUrl.searchParams.get("endpoint") || DEFAULTS.endpoint;
  const url = endpointUrl(endpoint, "/health");
  const result = await requestJson(url, { rejectUnauthorized: false, timeoutMs: 30_000 });
  sendJson(response, 200, {
    endpoint: url.href,
    statusCode: result.statusCode,
    payload: result.payload,
  });
}

async function handleModels(requestUrl, response) {
  const endpoint = requestUrl.searchParams.get("endpoint") || DEFAULTS.endpoint;
  const url = endpointUrl(endpoint, "/v1/models");
  const result = await requestJson(url, { rejectUnauthorized: false, timeoutMs: 30_000 });
  sendJson(response, 200, {
    endpoint: url.href,
    statusCode: result.statusCode,
    payload: result.payload,
  });
}

async function handleTls(requestUrl, response) {
  const endpoint = requestUrl.searchParams.get("endpoint") || DEFAULTS.endpoint;
  const payload = await inspectTls(endpoint);
  sendJson(response, 200, payload);
}

async function handleChat(request, response) {
  const body = await parseRequestBody(request);
  const endpoint = body.endpoint || DEFAULTS.endpoint;
  const model = body.model || DEFAULTS.model;
  const prompt = String(body.prompt || "");
  const maxTokens = Math.max(1, Math.min(Number(body.max_tokens || 160), 1024));
  const temperature = Number.isFinite(Number(body.temperature)) ? Number(body.temperature) : 0;
  const url = endpointUrl(endpoint, "/v1/chat/completions");

  const payload = {
    model,
    messages: body.messages || [{ role: "user", content: prompt }],
    max_tokens: maxTokens,
    temperature,
    stream: false,
  };
  const result = await requestJson(url, {
    method: "POST",
    rejectUnauthorized: false,
    timeoutMs: 180_000,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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
    if (request.method === "GET" && requestUrl.pathname === "/api/certificates") {
      await handleCertificates(requestUrl, response);
      return;
    }
    if (request.method === "GET" && requestUrl.pathname === "/api/health") {
      await handleHealth(requestUrl, response);
      return;
    }
    if (request.method === "GET" && requestUrl.pathname === "/api/models") {
      await handleModels(requestUrl, response);
      return;
    }
    if (request.method === "GET" && requestUrl.pathname === "/api/tls") {
      await handleTls(requestUrl, response);
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
    sendJson(response, 500, {
      error: error.message,
      statusCode: error.statusCode || null,
      payload: error.payload || null,
    });
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`Cove client listening on http://127.0.0.1:${PORT}`);
});
