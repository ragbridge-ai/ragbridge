"use strict";

// Everything shown from the server or from a document is put on the page with
// textContent (through el() below), never as markup: a chunk is whatever was in
// the uploaded file, and an answer is whatever a model wrote from it. See
// docs/plans/phase-6-ui.md, decision 6. tests/test_playground.py fails if this
// file ever builds markup from text.

const KEY_STORAGE = "ragbridge.playground.key";
const KEY_PREFIX_LENGTH = 11; // "rb_" + 8 characters: what the server also shows.
const POLL_MS = 2000;
const STATUSES = ["pending", "processing", "ready", "failed"];

// The API accepts exactly these three content types and refuses anything else,
// so the page states the type from the file name instead of trusting the one
// the operating system reports. (Some system and browser combinations report
// no type at all for Markdown; not reproduced on the macOS Chrome this was
// developed on, which reports "text/markdown".)
const CONTENT_TYPES = {
  txt: "text/plain",
  md: "text/markdown",
  markdown: "text/markdown",
  pdf: "application/pdf",
};

let apiKey = "";
let pollTimer = null;

function byId(id) {
  const node = document.getElementById(id);
  if (node === null) {
    throw new Error("missing element #" + id);
  }
  return node;
}

// Build an element. `text` goes in as text, never as markup; children may be
// nodes or strings (strings become text nodes).
function el(tag, options = {}, ...children) {
  const node = document.createElement(tag);
  if (options.className) {
    node.className = options.className;
  }
  if (options.text !== undefined) {
    node.textContent = options.text;
  }
  for (const [name, value] of Object.entries(options.attrs || {})) {
    node.setAttribute(name, value);
  }
  node.append(...children);
  return node;
}

function setStatus(id, message, isError = false) {
  const node = byId(id);
  node.textContent = message;
  node.classList.toggle("error", isError);
}

// --- the key -----------------------------------------------------------------

// sessionStorage is a convenience so a reload keeps the key. It can throw (a
// private window, blocked site data), and the page must work without it.
function readStoredKey() {
  try {
    return sessionStorage.getItem(KEY_STORAGE) || "";
  } catch {
    return "";
  }
}

function storeKey(key) {
  try {
    if (key) {
      sessionStorage.setItem(KEY_STORAGE, key);
    } else {
      sessionStorage.removeItem(KEY_STORAGE);
    }
  } catch {
    // Nothing to do: the key just lives in memory for this page.
  }
}

// --- talking to the API ------------------------------------------------------

class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.status = status;
  }
}

function describeError(status, data) {
  if (status === 401) {
    return "The server rejected this key (401).";
  }
  const detail = data && typeof data === "object" ? data.detail : data;
  if (typeof detail === "string" && detail) {
    return status + ": " + detail;
  }
  if (Array.isArray(detail)) {
    return status + ": " + detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
  }
  return "HTTP " + status;
}

async function api(path, { method = "GET", json, formData } = {}) {
  const headers = { Authorization: "Bearer " + apiKey };
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (formData !== undefined) {
    body = formData; // the browser sets the multipart Content-Type itself
  }

  const started = performance.now();
  let response;
  try {
    response = await fetch(path, { method, headers, body });
  } catch (error) {
    throw new ApiError("Could not reach the server: " + error.message);
  }
  const elapsedMs = performance.now() - started;

  let data = null;
  if (response.status !== 204) {
    const text = await response.text();
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
  }
  if (!response.ok) {
    throw new ApiError(describeError(response.status, data), response.status);
  }
  return { data, status: response.status, elapsedMs };
}

// --- documents ---------------------------------------------------------------

function statusBadge(document) {
  const status = STATUSES.includes(document.status) ? document.status : "unknown";
  const badge = el("span", { className: "status status-" + status, text: status });
  if (status === "failed" && document.error) {
    badge.append(el("span", { className: "doc-error", text: document.error }));
  }
  return badge;
}

function documentRow(document) {
  const remove = el("button", { text: "Delete", attrs: { type: "button" } });
  remove.addEventListener("click", () => deleteDocument(document));
  return el(
    "tr",
    {},
    el("td", { text: document.filename }),
    el("td", {}, statusBadge(document)),
    el("td", { text: new Date(document.created_at).toLocaleString() }),
    el("td", {}, remove),
  );
}

function renderDocuments(documents) {
  const table = byId("documents-table");
  table.hidden = documents.length === 0;
  byId("documents-body").replaceChildren(...documents.map(documentRow));
  setStatus(
    "documents-status",
    documents.length === 0 ? "No documents yet." : documents.length + " document(s).",
  );
}

async function loadDocuments() {
  clearTimeout(pollTimer);
  try {
    const { data } = await api("/documents");
    renderDocuments(data);
    // A large upload is processed by the background worker: keep looking
    // until nothing is waiting any more.
    if (data.some((doc) => doc.status === "pending" || doc.status === "processing")) {
      pollTimer = setTimeout(loadDocuments, POLL_MS);
    }
  } catch (error) {
    setStatus("documents-status", error.message, true);
  }
}

async function deleteDocument(document) {
  if (!window.confirm("Delete " + document.filename + "? Its chunks are deleted too.")) {
    return;
  }
  try {
    await api("/documents/" + encodeURIComponent(document.id), { method: "DELETE" });
  } catch (error) {
    setStatus("documents-status", error.message, true);
    return;
  }
  await loadDocuments();
}

// --- uploading ---------------------------------------------------------------

function contentTypeFor(filename) {
  const dot = filename.lastIndexOf(".");
  return dot === -1 ? undefined : CONTENT_TYPES[filename.slice(dot + 1).toLowerCase()];
}

// What the server's status code means for an upload (see POST /documents).
function describeUpload(status) {
  if (status === 200) {
    return "already uploaded (same content, nothing to do)";
  }
  if (status === 202) {
    return "queued: the background worker is processing it";
  }
  return "ready";
}

function addUploadResult(filename, message, isError = false) {
  byId("upload-results").append(
    el("li", { className: isError ? "error" : "", text: filename + ": " + message }),
  );
}

async function uploadOne(file) {
  const contentType = contentTypeFor(file.name);
  if (contentType === undefined) {
    addUploadResult(file.name, "not a supported file type (use .txt, .md or .pdf)", true);
    return;
  }
  const formData = new FormData();
  formData.append("file", new Blob([file], { type: contentType }), file.name);
  try {
    const { status } = await api("/documents", { method: "POST", formData });
    addUploadResult(file.name, describeUpload(status));
  } catch (error) {
    addUploadResult(file.name, error.message, true);
  }
}

async function uploadSelectedFiles() {
  const input = byId("file-input");
  const files = Array.from(input.files);
  byId("upload-results").replaceChildren();
  if (files.length === 0) {
    setStatus("documents-status", "Choose at least one file first.", true);
    return;
  }
  byId("upload-button").disabled = true;
  setStatus("documents-status", "Uploading and embedding...");
  // One at a time: each upload embeds its chunks with the model, and a laptop
  // running Ollama does better without several at once.
  for (const file of files) {
    await uploadOne(file);
  }
  input.value = "";
  byId("upload-button").disabled = false;
  await loadDocuments();
}

byId("upload-form").addEventListener("submit", (event) => {
  event.preventDefault();
  uploadSelectedFiles();
});

// --- asking ------------------------------------------------------------------

function seconds(elapsedMs) {
  return (elapsedMs / 1000).toFixed(1) + " s";
}

function badge(text, isMiss = false) {
  return el("span", { className: isMiss ? "badge badge-miss" : "badge", text });
}

// "vector #1", or "vector: not found" when that retrieval arm did not find the
// chunk at all (a rank of null in the API).
function rankBadge(label, rank) {
  return rank === null ? badge(label + ": not found", true) : badge(label + " #" + rank);
}

function retrievalBadges(retrieval) {
  return el(
    "div",
    { className: "badges" },
    rankBadge("vector", retrieval.vector_rank),
    rankBadge("keyword", retrieval.keyword_rank),
    badge("fused score " + retrieval.fused_score.toFixed(4)),
    badge("before rerank #" + retrieval.rank_before_rerank),
  );
}

function sourceCard(source, position, textLabel) {
  const text = source.content !== undefined ? source.content : source.snippet;
  const card = el(
    "article",
    { className: "block block-source" },
    el(
      "div",
      { className: "source-head" },
      el("strong", { text: "#" + position + "  " + source.filename }),
      el("span", { text: "chunk " + source.chunk_index }),
      el("span", { text: "score " + source.score.toFixed(4) }),
    ),
    el("p", { className: "origin", text: textLabel }),
  );
  if (source.retrieval) {
    card.append(retrievalBadges(source.retrieval));
  }
  card.append(el("pre", { className: "chunk", text }));
  return card;
}

function answerBlock(answer) {
  return el(
    "section",
    { className: "block block-answer" },
    el("h3", { text: "Answer" }),
    el("p", { className: "origin", text: "Written by the model from the chunks below. It can be wrong." }),
    el("p", { className: "answer-text", text: answer }),
  );
}

function sourcesBlock(sources, textLabel) {
  const parts = [el("h3", { text: "Sources (" + sources.length + ")" })];
  sources.forEach((source, index) => parts.push(sourceCard(source, index + 1, textLabel)));
  return el("section", {}, ...parts);
}

function timingLine(prefix, elapsedMs) {
  return el("p", { className: "timing", text: prefix + " in " + seconds(elapsedMs) });
}

function renderQueryResult(data, elapsedMs) {
  byId("result").replaceChildren(
    timingLine("Answered", elapsedMs),
    answerBlock(data.answer),
    sourcesBlock(data.sources, "From your documents (the first 300 characters of the chunk)"),
  );
}

function renderSearchResult(data, elapsedMs) {
  const found =
    data.candidate_count === undefined ? "" : " Retrieval found " + data.candidate_count + " in all.";
  byId("result").replaceChildren(
    timingLine("Searched", elapsedMs),
    el("p", { className: "timing", text: "No model was asked to write an answer." + found }),
    sourcesBlock(data.results, "From your documents (the whole chunk)"),
  );
}

// The queries are written by the planner model, so they are shown as text like
// everything else.
function stepsBlock(steps) {
  const items = steps.map((step) =>
    el("li", { text: "searched for \u201c" + step.query + "\u201d and found " + step.results + " chunk(s)" }),
  );
  return el(
    "section",
    { className: "block" },
    el("h3", { text: "Searches the agent ran (" + steps.length + ")" }),
    el("p", { className: "origin", text: "The first search is always your question itself." }),
    el("ol", { className: "steps" }, ...items),
  );
}

function renderAgentResult(data, elapsedMs) {
  byId("result").replaceChildren(
    timingLine("Answered", elapsedMs),
    stepsBlock(data.steps),
    answerBlock(data.answer),
    sourcesBlock(data.sources, "From your documents (the first 300 characters of the chunk)"),
  );
}

function renderError(message) {
  byId("result").replaceChildren(el("p", { className: "error", text: message }));
}

// What /query and /search share: how many chunks, which retrieval, and whether
// to explain. Left out of the request when not chosen, so the server's own
// default applies.
function retrievalOptions() {
  const options = { top_k: Number(byId("top-k").value) };
  const mode = byId("retrieval-mode").value;
  if (mode) {
    options.mode = mode;
  }
  if (byId("explain").checked) {
    options.explain = true;
  }
  return options;
}

const ENDPOINTS = {
  query: {
    path: "/query",
    payload: (question) => ({ question, ...retrievalOptions() }),
    render: renderQueryResult,
  },
  search: {
    path: "/search",
    payload: (question) => ({ query: question, ...retrievalOptions() }),
    render: renderSearchResult,
  },
  agent: {
    path: "/agent",
    payload: (question) => ({ question, max_steps: Number(byId("max-steps").value) }),
    render: renderAgentResult,
  },
};

function selectedEndpoint() {
  return document.querySelector('input[name="endpoint"]:checked').value;
}

// Only show the options the chosen endpoint understands.
function showOptionsFor(endpoint) {
  byId("retrieval-options").hidden = endpoint === "agent";
  byId("agent-options").hidden = endpoint !== "agent";
}

async function ask() {
  const question = byId("question").value.trim();
  if (!question) {
    setStatus("ask-status", "Type a question first.", true);
    return;
  }
  const endpoint = ENDPOINTS[selectedEndpoint()];

  byId("ask-button").disabled = true;
  byId("result").replaceChildren();
  setStatus("ask-status", "Working... a local model can take a while.");
  try {
    const { data, elapsedMs } = await api(endpoint.path, {
      method: "POST",
      json: endpoint.payload(question),
    });
    endpoint.render(data, elapsedMs);
    setStatus("ask-status", "");
  } catch (error) {
    setStatus("ask-status", "");
    renderError(error.message);
  } finally {
    byId("ask-button").disabled = false;
  }
}

for (const radio of document.querySelectorAll('input[name="endpoint"]')) {
  radio.addEventListener("change", () => showOptionsFor(selectedEndpoint()));
}

byId("ask-form").addEventListener("submit", (event) => {
  event.preventDefault();
  ask();
});

// --- signing in and out ------------------------------------------------------

function showSignedIn(signedIn) {
  byId("documents-section").hidden = !signedIn;
  byId("ask-section").hidden = !signedIn;
  byId("forget-key").hidden = !signedIn;
  byId("key-input").value = "";
  if (!signedIn) {
    clearTimeout(pollTimer);
    byId("upload-results").replaceChildren();
    byId("result").replaceChildren();
  }
}

// Checks the key by using it, so a wrong key is reported here, not on the
// first thing the person tries to do.
async function useKey(key) {
  apiKey = key;
  try {
    await api("/documents");
  } catch (error) {
    apiKey = "";
    storeKey("");
    showSignedIn(false);
    setStatus("key-status", error.message, true);
    return;
  }
  storeKey(key);
  showSignedIn(true);
  setStatus("key-status", "Using key " + key.slice(0, KEY_PREFIX_LENGTH) + "...");
  await loadDocuments();
}

function forgetKey() {
  apiKey = "";
  storeKey("");
  showSignedIn(false);
  setStatus("key-status", "Key forgotten.");
}

byId("key-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const key = byId("key-input").value.trim();
  if (!key) {
    setStatus("key-status", "Paste a key first.", true);
    return;
  }
  useKey(key);
});
byId("forget-key").addEventListener("click", forgetKey);

const storedKey = readStoredKey();
if (storedKey) {
  useKey(storedKey);
}
