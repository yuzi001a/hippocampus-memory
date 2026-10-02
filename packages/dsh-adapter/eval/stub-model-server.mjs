#!/usr/bin/env node
/**
 * stub-model-server.mjs
 *
 * A standalone, dependency-free (Node built-ins only) local stub model server that
 * speaks just enough of the Anthropic Messages streaming protocol for isolated
 * integration tests of a real agent CLI.
 *
 * Why it exists
 * -------------
 * The integration test runs the real CLI end to end, but the test machine has no
 * model credential and must not reach the network. The CLI is therefore pointed at
 * this stub, so the real pipeline (turn -> pre-step -> model request -> assistant
 * message -> session event) runs for real while the model reply is synthetic.
 *
 * The stub records every request body it receives to a JSONL file. That recorded
 * request is the evidence that an injected context/memory message actually reached
 * the model request boundary (which no amount of CLI-side logging can prove).
 *
 * Behaviour mirrored from the upstream reference mock server
 * (deepseek-harness @ 639ed015397290b3745d163aafe02ffee4aa3f84,
 *  packages/llm/llm-deepseek/tests/mock-server.ts): a minimal six-event SSE
 * success sequence on POST /v1/messages.
 *
 * Usage
 *   node eval/stub-model-server.mjs --port PORT --record FILE.jsonl [--text "reply text"]
 *
 *   --port    TCP port to listen on, bound to 127.0.0.1 only. Default 8787.
 *   --record  Path of the JSONL file that request evidence is appended to.
 *             This is the only file this process ever writes.
 *   --text    Assistant reply text emitted in the content_block_delta event.
 *             Default: STUB-OK
 *
 * Once listening, one readiness line is printed to stdout: "STUB_READY <port>".
 * The process then stays alive until SIGTERM or SIGINT, at which point it exits 0.
 *
 * Safety / scope
 *   - No credentials of any kind, none read and none required.
 *   - Request headers are never printed or recorded.
 *   - No network egress: bound to 127.0.0.1, no outbound requests.
 *   - No dependencies: node: built-ins only.
 *   - The only filesystem write is the append to --record.
 *   - Test scaffolding for an isolated, disposable environment. Not a product.
 */

import http from 'node:http';
import fs from 'node:fs';
import process from 'node:process';

/** Request path the Anthropic Messages API is served on. */
const MESSAGES_PATH = '/v1/messages';
/** Liveness probe path. */
const HEALTH_PATH = '/health';
/** Literal marker used to detect an injected memory message in a request. */
const MEMORY_MARKER = 'hippocampus memory';
/** Model name echoed back in the message_start event. */
const MOCK_MODEL = 'deepseek-v4-flash';
/** Fixed message id echoed back in the message_start event. */
const MOCK_MESSAGE_ID = 'msg_1';
/** Hard cap on a request body, so a bad client cannot exhaust memory. */
const MAX_BODY_BYTES = 8 * 1024 * 1024;
const HOST = '127.0.0.1';

const USAGE = [
  'Usage: node eval/stub-model-server.mjs --port PORT --record FILE.jsonl [--text "reply text"]',
  '',
  '  --port    TCP port bound to 127.0.0.1 only (default 8787, 0 picks a free port).',
  '  --record  JSONL file that each received model request is appended to.',
  '  --text    Assistant reply text (default STUB-OK).',
].join('\n');

/**
 * Parse argv into a config object. Throws on an unknown flag or a bad value.
 * @param {string[]} argv
 */
function parseArgs(argv) {
  const config = { port: 8787, record: 'stub-model-requests.jsonl', text: 'STUB-OK', help: false };
  const valueFlags = new Set(['--port', '--record', '--text']);

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--help' || arg === '-h') {
      config.help = true;
      continue;
    }
    const eq = arg.indexOf('=');
    const flag = eq === -1 ? arg : arg.slice(0, eq);
    if (!valueFlags.has(flag)) throw new Error(`unknown argument: ${arg}`);

    let value;
    if (eq === -1) {
      value = argv[i + 1];
      i += 1;
    } else {
      value = arg.slice(eq + 1);
    }
    if (value === undefined) throw new Error(`missing value for ${flag}`);
    config[flag.slice(2)] = value;
  }

  const port = Number(config.port);
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    throw new Error(`invalid --port: ${String(config.port)}`);
  }
  config.port = port;
  if (typeof config.text !== 'string') throw new Error('invalid --text value');
  if (typeof config.record !== 'string' || config.record.length === 0) {
    throw new Error('invalid --record value');
  }
  return config;
}

/**
 * The exact six-event minimal SSE success sequence, in order.
 * @param {string} replyText
 */
function buildSseEvents(replyText) {
  return [
    {
      type: 'message_start',
      message: {
        id: MOCK_MESSAGE_ID,
        type: 'message',
        role: 'assistant',
        model: MOCK_MODEL,
        content: [],
        stop_reason: null,
        stop_sequence: null,
        usage: { input_tokens: 3, output_tokens: 0 },
      },
    },
    { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } },
    { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: replyText } },
    { type: 'content_block_stop', index: 0 },
    {
      type: 'message_delta',
      delta: { stop_reason: 'end_turn', stop_sequence: null },
      usage: { output_tokens: 1 },
    },
    { type: 'message_stop' },
  ];
}

/** Serialise the six events as SSE frames: "data: <json>\n\n" each. */
function buildSseBody(replyText) {
  return buildSseEvents(replyText)
    .map((event) => `data: ${JSON.stringify(event)}\n\n`)
    .join('');
}

/**
 * Collect a request body, rejecting oversized bodies and client aborts.
 * @param {import('node:http').IncomingMessage} req
 * @returns {Promise<string>}
 */
function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let settled = false;

    const fail = (err) => {
      if (settled) return;
      settled = true;
      reject(err);
    };

    req.on('data', (chunk) => {
      if (settled) return;
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        fail(new Error(`request body exceeds ${MAX_BODY_BYTES} bytes`));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (settled) return;
      settled = true;
      resolve(Buffer.concat(chunks).toString('utf8'));
    });
    req.on('error', fail);
    req.on('aborted', () => fail(new Error('request aborted by client')));
  });
}

/**
 * Every text block of every message, in order. Handles both the string form and the
 * block-array form of message content.
 * @param {unknown} messages
 * @returns {string[]}
 */
function collectTextBlocks(messages) {
  const blocks = [];
  if (!Array.isArray(messages)) return blocks;
  for (const message of messages) {
    if (!message || typeof message !== 'object') continue;
    const content = /** @type {{content?: unknown}} */ (message).content;
    if (typeof content === 'string') {
      blocks.push(content);
    } else if (Array.isArray(content)) {
      for (const block of content) {
        if (block && typeof block === 'object' && typeof (/** @type {{text?: unknown}} */ (block).text) === 'string') {
          blocks.push(/** @type {{text: string}} */ (block).text);
        }
      }
    }
  }
  return blocks;
}

/**
 * Append JSONL evidence lines. Never throws: a record that cannot be written must
 * not take the stub down mid-test, it is reported on stderr instead.
 * @param {string} recordPath
 * @param {object[]} lines
 */
function appendRecord(recordPath, lines) {
  try {
    fs.appendFileSync(recordPath, `${lines.map((line) => JSON.stringify(line)).join('\n')}\n`, 'utf8');
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    process.stderr.write(`[stub-model-server] could not append record file: ${message}\n`);
  }
}

/** @param {import('node:http').ServerResponse} res */
function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'content-type': 'application/json',
    'content-length': Buffer.byteLength(body),
  });
  res.end(body);
}

/**
 * @param {import('node:http').ServerResponse} res
 * @param {import('node:http').IncomingMessage} req
 * @param {string} pathname
 */
function sendNotFound(res, req, pathname) {
  sendJson(res, 404, {
    error: { type: 'not_found', message: `no stub route for ${req.method} ${pathname}` },
  });
}

function createServer(config) {
  let requestCount = 0;

  const server = http.createServer((req, res) => {
    // Path only; any query string is ignored. Headers are never logged.
    let pathname = '/';
    try {
      pathname = new URL(req.url ?? '/', `http://${HOST}`).pathname;
    } catch {
      pathname = String(req.url ?? '/');
    }

    const handle = async () => {
      if (pathname === HEALTH_PATH) {
        if (req.method !== 'GET' && req.method !== 'HEAD') {
          sendNotFound(res, req, pathname);
          return;
        }
        sendJson(res, 200, {
          ok: true,
          port: config.port,
          model: MOCK_MODEL,
          requestsRecorded: requestCount,
        });
        return;
      }

      if (pathname !== MESSAGES_PATH || req.method !== 'POST') {
        sendNotFound(res, req, pathname);
        return;
      }

      let raw;
      try {
        raw = await readBody(req);
      } catch (err) {
        const message = err && err.message ? err.message : String(err);
        process.stderr.write(`[stub-model-server] unreadable request body: ${message}\n`);
        sendJson(res, 400, {
          error: { type: 'invalid_request_error', message: `could not read request body: ${message}` },
        });
        return;
      }

      let body;
      try {
        body = JSON.parse(raw);
      } catch (err) {
        const message = err && err.message ? err.message : String(err);
        process.stderr.write(`[stub-model-server] rejected malformed JSON body: ${message}\n`);
        sendJson(res, 400, {
          error: { type: 'invalid_request_error', message: `request body is not valid JSON: ${message}` },
        });
        return;
      }

      if (!body || typeof body !== 'object' || Array.isArray(body)) {
        sendJson(res, 400, {
          error: { type: 'invalid_request_error', message: 'request body must be a JSON object' },
        });
        return;
      }

      // Valid model request: record it before answering, so the evidence file
      // cannot lag behind a response the test already observed.
      requestCount += 1;
      const receivedAt = new Date().toISOString();
      const messages = Array.isArray(body.messages) ? body.messages : [];
      const matched = collectTextBlocks(messages).filter((text) => text.includes(MEMORY_MARKER));

      appendRecord(config.record, [
        {
          receivedAt,
          path: pathname,
          model: typeof body.model === 'string' ? body.model : null,
          messageCount: messages.length,
          messages,
        },
        {
          receivedAt,
          event: 'request',
          textPresent: matched.length > 0,
          // Full text of every block containing the marker, verbatim and complete.
          matchedText: matched.join('\n'),
        },
      ]);

      const sse = buildSseBody(config.text);
      res.writeHead(200, {
        'content-type': 'text/event-stream',
        'content-length': Buffer.byteLength(sse),
        'cache-control': 'no-cache',
        connection: 'close',
      });
      res.end(sse);
    };

    // No request may crash the process.
    handle().catch((err) => {
      const message = err && err.stack ? err.stack : String(err);
      process.stderr.write(`[stub-model-server] unhandled request error: ${message}\n`);
      if (!res.headersSent) {
        sendJson(res, 500, { error: { type: 'internal_error', message: 'stub failure' } });
      } else {
        res.end();
      }
    });
  });

  // A malformed HTTP request must not take the server down either.
  server.on('clientError', (err, socket) => {
    process.stderr.write(`[stub-model-server] client error: ${err && err.message ? err.message : String(err)}\n`);
    if (socket && !socket.destroyed && socket.writable) {
      socket.end('HTTP/1.1 400 Bad Request\r\ncontent-type: application/json\r\nconnection: close\r\n\r\n');
    } else if (socket && !socket.destroyed) {
      socket.destroy();
    }
  });

  return server;
}

function main() {
  let config;
  try {
    config = parseArgs(process.argv.slice(2));
  } catch (err) {
    process.stderr.write(`[stub-model-server] ${err && err.message ? err.message : String(err)}\n${USAGE}\n`);
    process.exit(2);
    return;
  }
  if (config.help) {
    process.stdout.write(`${USAGE}\n`);
    process.exit(0);
    return;
  }

  const server = createServer(config);

  server.on('error', (err) => {
    const message = err && err.message ? err.message : String(err);
    process.stderr.write(`[stub-model-server] server error: ${message}\n`);
    process.exit(1);
  });

  server.listen(config.port, HOST, () => {
    const address = server.address();
    const port = address && typeof address === 'object' ? address.port : config.port;
    // Single readiness line: STUB_READY <port>
    process.stdout.write(`STUB_READY ${port}\n`);
  });

  let shuttingDown = false;
  const shutdown = () => {
    if (shuttingDown) return;
    shuttingDown = true;
    try {
      server.close();
    } catch {
      // already closing
    }
    process.exit(0);
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);

  // Log, never die: a stub that vanishes mid-test hides the real failure.
  process.on('uncaughtException', (err) => {
    const message = err && err.stack ? err.stack : String(err);
    process.stderr.write(`[stub-model-server] uncaught exception: ${message}\n`);
  });
  process.on('unhandledRejection', (reason) => {
    const message = reason && reason.stack ? reason.stack : String(reason);
    process.stderr.write(`[stub-model-server] unhandled rejection: ${message}\n`);
  });
}

main();
