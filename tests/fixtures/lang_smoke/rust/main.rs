// Entry point: uses the shapes module, calls into it (CALLS edge), typed via the Shape trait.

mod shapes;

use shapes::{Rectangle, Shape};

// width/height/factor are parameters used downstream (data_flow);
// Rectangle{..} construction and .scaled_area(..) are CALLS edges into shapes.rs.
fn total_area(width: f64, height: f64, factor: f64) -> f64 {
    let rect: Box<dyn Shape> = Box::new(Rectangle { width, height });
    rect.scaled_area(factor)
}

fn main() {
    let result = total_area(3.0, 4.0, 2.0);
    println!("total area: {}", result);
}
