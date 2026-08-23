#!/usr/bin/env python3

import re
import unicodedata
import urllib.parse

import monitor as core


# ---------------------------------------------------------
# Guardamos las funciones originales
# ---------------------------------------------------------

_original_valid = core.valid_product_url
_original_parse = core.parse_candidate
_original_telegram = core.telegram_send


# ---------------------------------------------------------
# URLs que NO son productos
# ---------------------------------------------------------

BAD_URL_PARTS = (
    "/nosotros",
    "/about",
    "/quienes-somos",
    "/quienes_somos",
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
    "/outlet/",
)


# ---------------------------------------------------------
# Patrones típicos de URL de producto
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# Textos de navegación que no son nombres de productos
# ---------------------------------------------------------

BAD_TEXT = (
    "comprar ahora",
    "compra ahora",
    "ver mas",
    "ver todo",
    "conoce mas",
    "quienes somos",
    "nuestras tiendas",
    "categoria destacada",
    "categorias destacadas",
    "productos destacados",
    "destacado de la semana",
    "previous next",
    "descubre mas",
    "ir a tienda",
    "shop now",
    "learn more",
)


STOP_WORDS = {
    "para",
    "con",
    "sin",
    "del",
    "las",
    "los",
    "una",
    "uno",
    "por",
    "color",
    "modelo",
    "marca",
    "producto",
    "chile",
}


def normalize(value):

    value = unicodedata.normalize(
        "NFKD",
        str(value or "")
    )

    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )

    value = value.lower()

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value
    )

    return re.sub(
        r"\s+",
        " ",
        value
    ).strip()


# ---------------------------------------------------------
# Validar URL de producto
# ---------------------------------------------------------

def strict_product_url(url, cfg):

    if not _original_valid(
        url,
        cfg,
    ):
        return False

    parsed = urllib.parse.urlsplit(
        url
    )
