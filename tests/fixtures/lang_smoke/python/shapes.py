"""Minimal Python fixture exercising CONTAINS (class -> method), a CALLS edge,
and a parameter used downstream (data_flow)."""


class Rectangle:
    """A rectangle shape with a width and height."""

    def __init__(self, width: float, height: float) -> None:
        self.width = width
        self.height = height

    def area(self) -> float:
        # method on a class -> CONTAINS edge (Rectangle contains area)
        return self.width * self.height

    def scaled_area(self, factor: float) -> float:
        # `factor` (a parameter) flows into the multiply -> data_flow.
        # self.area() is a method call -> CALLS edge.
        base = self.area()
        return base * factor
