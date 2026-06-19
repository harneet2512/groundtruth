import * as path from "path";

export function scan(a: string, b: string): string {
  return path.join(a, b);
}
