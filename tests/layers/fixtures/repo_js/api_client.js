// HTTP API client. Calls into json_parser for response decoding.
const { parseJSON } = require("./json_parser");

async function fetchAndDecode(url, fetchImpl) {
  const fn = fetchImpl || globalThis.fetch;
  const res = await fn(url);
  const body = await res.text();
  return parseJSON(body);
}

async function postJson(url, payload, fetchImpl) {
  const fn = fetchImpl || globalThis.fetch;
  const res = await fn(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return parseJSON(await res.text());
}

module.exports = { fetchAndDecode, postJson };
