#!/usr/bin/env python3
import re
import unicodedata
import urllib.parse

import monitor as core


_original_valid = core.valid_product_url
_original_parse = core.parse_candidate


BAD_URL_PARTS = (
    "/nosotros",
    "/about",
    "/quienes-somos",
    "/tiendas",
    "/stores",
    "/contacto",
    "/contact",
    "/ayuda",
    "/help",
    "/faq",
    "/blog",
    "/blogs",
    "/pages/",
    "/servicios",
    "/services",
    "/account",
    "/login",
    "/checkout",
    "/carrito",
    "/cart",
    "/collections/",
    "/collection/",
    "/category/",
    "/categories/",
    "/categorias/",
    "/search",
    "/busca",
    "/ofertas",
    "/promociones",
    "/sale/",
    "/outlet/",
)

PRODUCT_HINTS = (
    "/product/",
    "/products/",
    "/producto/",
    "/productos/",
    "/p/",
    "/pdp/",
    "/ip/",
    "/sku/",
    "/item/",
)

BAD_TEXT = (
    "comprar ahora",
    "compra ahora",
    "ver mas",
    "ver más",
    "ver todo",
    "conoce mas",
    "conoce más",
    "quienes somos",
    "quiénes somos",
    "nuestras tiendas",
    "categoria destacada",
    "categoría destacada",
    "descubre mas",
    "descubre más",
    "ir a tienda",
    "shop now",
    "learn more",
)


def normalize(value):
    value = unicodedata.normalize(
        "NFKD",
        str(value or "")
    )

    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )

    return re.sub(
        r"\s+",
        " ",
        value.lower()
    ).strip()


def strict_product_url(url, cfg):

    if not _original_valid(url, cfg):
        return False

    parsed = urllib.parse.urlsplit(url)

    path = urllib.parse.unquote(
        parsed.path or ""
    ).lower().rstrip("/")

    if not path or path == "/":
        return False

    if any(
        bad in path
        for bad in BAD_URL_PARTS
    ):
        return False

    # Si la tienda ya tiene una regla
    # específica en stores.json, se respeta.
    if cfg.get("product_url_regex"):
        return True

    # URLs típicas de productos.
    if any(
        hint in path
        for hint in PRODUCT_HINTS
    ):
        return True

    # SKU o código numérico en URL.
    if re.search(
        r"(?:^|[-_/])\d{5,}(?:[-_/\.]|$)",
        path
    ):
        return True

    # Producto .html que contiene modelo/SKU.
    filename = path.rsplit("/", 1)[-1]

    if (
        filename.endswith(".html")
        and re.search(r"\d", filename)
    ):
        return True

    return False


def strict_parse_candidate(
    store,
    href,
    anchor_text,
    card_text,
    category=None,
):

    anchor = normalize(anchor_text)

    # Elimina botones, categorías y navegación.
    if any(
        bad in anchor
        for bad in BAD_TEXT
    ):
        return None

    raw = str(card_text or "")

    lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip()
    ]

    # Si contiene media página,
    # claramente no es una ficha de producto.
    if len(raw) > 1400:
        return None

    if len(lines) > 32:
        return None

    prices = []

    for raw_price in core.PRICE_RE.findall(raw):

        price = core.price_int(raw_price)

        if (
            price is not None
            and price not in prices
        ):
            prices.append(price)

    if not prices:
        return None

    # Una tarjeta de producto no debería
    # contener decenas de precios diferentes.
    if len(prices) > 6:
        return None

    product = _original_parse(
        store,
        href,
        anchor_text,
        card_text,
        category=category,
    )

    if not product:
        return None

    name = normalize(product.name)

    if any(
        bad in name
        for bad in BAD_TEXT
    ):
        return None

    if len(name) < 4:
        return None

    return product


# Reemplaza solamente los filtros problemáticos.
# Todo el resto continúa usando monitor.py.
core.valid_product_url = strict_product_url
core.parse_candidate = strict_parse_candidate


if __name__ == "__main__":
    core.main()
