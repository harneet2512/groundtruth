package main

import (
	"fmt"

	"example.com/langsmoke/shapes"
)

// totalArea constructs a Rectangle and calls into it (CALLS edge into shapes pkg).
// width/height/factor are parameters used downstream (data_flow).
func totalArea(width, height, factor float64) float64 {
	var s shapes.Shape = shapes.Rectangle{Width: width, Height: height}
	return s.ScaledArea(factor)
}

func main() {
	result := totalArea(3, 4, 2)
	fmt.Printf("total area: %v\n", result)
}
