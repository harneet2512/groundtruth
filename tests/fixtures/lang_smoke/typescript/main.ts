// Entry point: cross-file IMPORTS / CALLS into shape.ts, typed against the Shape interface.

import { Rectangle, Shape } from "./shape";

export function totalArea(width: number, height: number, factor: number): number {
  // width/height/factor are parameters used downstream (data_flow);
  // Rectangle(...) and .scaledArea(...) are CALLS edges into shape.ts.
  const rect: Shape = new Rectangle(width, height);
  return rect.scaledArea(factor);
}

function main(): void {
  const result = totalArea(3, 4, 2);
  console.log(`total area: ${result}`);
}

main();
