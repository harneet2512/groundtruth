// JSON parsing helpers used by the API client.

/**
 * parseJSON validates and parses a JSON payload string. Returns null when
 * the input is empty; throws on malformed JSON.
 */
function parseJSON(raw) {
  if (raw == null || raw === "") {
    return null;
  }
  if (typeof raw !== "string") {
    throw new TypeError("parseJSON: expected string");
  }
  return JSON.parse(raw);
}

/**
 * stringifyJSON serialises a value, with stable key ordering for testability.
 */
function stringifyJSON(value) {
  if (value === undefined) {
    return "null";
  }
  return JSON.stringify(value, Object.keys(value || {}).sort());
}

module.exports = { parseJSON, stringifyJSON };
