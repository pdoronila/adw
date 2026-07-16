def average_price(cart):
    """Average price of the items in a cart."""
    return sum(item["price"] for item in cart) / len(cart)
