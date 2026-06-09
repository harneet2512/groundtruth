// Interface + implementor -> IMPLEMENTS/EXTENDS edge.
// Class -> method -> CONTAINS edge. method call -> CALLS edge.
// parameter used downstream -> data_flow.

export interface Shape {
  area(): number;
  scaledArea(factor: number): number;
}

export class Rectangle implements Shape {
  constructor(
    private readonly width: number,
    private readonly height: number,
  ) {}

  area(): number {
    return this.width * this.height;
  }

  scaledArea(factor: number): number {
    // `factor` flows downstream (data_flow); this.area() is a CALLS edge.
    const base = this.area();
    return base * factor;
  }
}
