import layout


def test_fractions_clamp_and_order():
    box = layout.fractions(500, 100, 50, 900, 500, 800)
    assert box == {"x0": 0.1, "y0": 0.125, "x1": 1.0, "y1": 1.0}


def test_fractions_of_a_quarter():
    assert layout.fractions(0, 0, 250, 400, 500, 800) == {"x0": 0.0, "y0": 0.0, "x1": 0.5, "y1": 0.5}
