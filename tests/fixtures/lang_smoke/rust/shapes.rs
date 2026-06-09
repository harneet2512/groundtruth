// Trait + implementor (IMPLEMENTS/EXTENDS), struct + method (CONTAINS),
// method call (CALLS), and a parameter used downstream (data_flow).

/// Shape is implemented by Rectangle -> IMPLEMENTS edge.
pub trait Shape {
    fn area(&self) -> f64;
    fn scaled_area(&self, factor: f64) -> f64;
}

/// Rectangle is a struct with methods -> CONTAINS edges.
pub struct Rectangle {
    pub width: f64,
    pub height: f64,
}

impl Shape for Rectangle {
    fn area(&self) -> f64 {
        self.width * self.height
    }

    fn scaled_area(&self, factor: f64) -> f64 {
        // `factor` flows downstream (data_flow); self.area() is a CALLS edge.
        let base = self.area();
        base * factor
    }
}
