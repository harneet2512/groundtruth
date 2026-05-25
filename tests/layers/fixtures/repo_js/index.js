// Application entry. Wires the Store and the api client.
const { Store } = require("./store");
const { fetchAndDecode } = require("./api_client");

async function bootstrap(seedUrl) {
  const cache = new Store(30000);
  const data = await fetchAndDecode(seedUrl);
  cache.set("seed", data);
  return cache;
}

module.exports = { bootstrap };
