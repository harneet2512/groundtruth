// Package shapes: interface + implementor (IMPLEMENTS), struct + method (CONTAINS),
// method call (CALLS), and a parameter used downstream (data_flow).
package shapes

// Shape is implemented by Rectangle -> IMPLEMENTS edge.
type Shape interface {
	Area() float64
	ScaledArea(factor float64) float64
}

// Rectangle is a struct with methods -> CONTAINS edges.
type Rectangle struct {
	Width  float64
	Height float64
}

// Area returns the rectangle's area.
func (r Rectangle) Area() float64 {
	return r.Width * r.Height
}

// ScaledArea multiplies the area by factor.
func (r Rectangle) ScaledArea(factor float64) float64 {
	// `factor` flows downstream (data_flow); r.Area() is a CALLS edge.
	base := r.Area()
	return base * factor
}
