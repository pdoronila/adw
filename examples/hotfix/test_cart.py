from cart import average_price


def test_average_of_two():
    assert average_price([{"price": 10}, {"price": 20}]) == 15
