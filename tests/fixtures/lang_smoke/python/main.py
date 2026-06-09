"""Entry point that calls into shapes.py (cross-file CALLS / IMPORTS edge)."""

from shapes import Rectangle


def total_area(width: float, height: float, factor: float) -> float:
    # width/height/factor are parameters used downstream (data_flow);
    # Rectangle(...) and .scaled_area(...) are CALLS edges into shapes.py.
    rect = Rectangle(width, height)
    return rect.scaled_area(factor)


def main() -> None:
    result = total_area(3.0, 4.0, 2.0)
    print(f"total area: {result}")


if __name__ == "__main__":
    main()
