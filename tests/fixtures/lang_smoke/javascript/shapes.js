// Minimal JavaScript fixture: class -> method (CONTAINS), method call (CALLS),
// and a parameter used downstream (data_flow).

class Rectangle {
  constructor(width, height) {
    this.width = width;
    this.height = height;
  }

  area() {
    // method on a class -> CONTAINS edge
    return this.width * this.height;
  }

  scaledArea(factor) {
    // `factor` (parameter) flows into the multiply -> data_flow;
    // this.area() is a method call -> CALLS edge.
    const base = this.area();
    return base * factor;
  }
}

module.exports = { Rectangle };
