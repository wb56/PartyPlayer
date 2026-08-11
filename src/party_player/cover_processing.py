"""Decode and resize cover art outside the Tk main thread."""

from io import BytesIO

from PIL import Image, ImageOps


def prepare_cover_canvas(image_data: bytes | None) -> Image.Image | None:
    """Decode, convert and fit cover bytes to the fixed deck canvas.

    This function does not access Tk and is therefore safe to execute in a cover
    worker. Only construction of ``CTkImage`` and widget configuration remain in
    the GUI thread.
    """

    if not image_data:
        return None
    with Image.open(BytesIO(image_data)) as source:
        fitted = ImageOps.contain(source.convert("RGB"), (190, 160))
        canvas = Image.new("RGB", (190, 160), "#20242b")
        offset = ((190 - fitted.width) // 2, (160 - fitted.height) // 2)
        canvas.paste(fitted, offset)
        return canvas
