// Entry point that calls into shapes.js (cross-file IMPORTS / CALLS edge).

const { Rectangle } = require("./shapes");

function totalArea(width, height, factor) {
  // width/height/factor are parameters used downstream (data_flow);
  // Rectangle(...) and .scaledArea(...) are CALLS edges into shapes.js.
  const rect = new Rectangle(width, height);
  return rect.scaledArea(factor);
}

function main() {
  const result = totalArea(3, 4, 2);
  console.log(`total area: ${result}`);
}

main();

module.exports = { totalArea };
