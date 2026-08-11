from party_player.product import (
    PRODUCT_DESCRIPTION,
    PRODUCT_NAME,
    PRODUCT_SLUG,
    PRODUCT_VERSION,
)


def test_public_product_identity() -> None:
    assert PRODUCT_NAME == "DeckRelay"
    assert PRODUCT_SLUG == "deckrelay"
    assert PRODUCT_VERSION == "1.0.0-beta.1"
    assert PRODUCT_DESCRIPTION == "Automatische Zwei-Deck-Musikwiedergabe für Veranstaltungen"
