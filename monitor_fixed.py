#!/usr/bin/env python3

import os
import re
import unicodedata
import urllib.parse

import monitor as core


_original_valid = core.valid_product_url
_original_parse = core.parse_candidate
_original_telegram = core.telegram_send
_original_alert_reason = core.alert_reason


REPEAT_ALERTS = os.getenv(
    "REPEAT_ALERTS",
    "false"
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


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
    "/promociones",
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


# PERFUMES BLOQUEADOS
PERFUME_TERMS = (
    "perfume",
    "perfumes",
    "parfum",
    "eau de parfum",
    "eau de toilette",
    "edp",
    "edt",
    "fragancia",
    "fragancias",
    "colonia",
)


def normalize(value):
    value = unicodedata.normalize(
        "NFKD",
        str(value or "")
    )

    value = "".join(
        c
        for c in value
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


def contains_phrase(
    value,
    phrases,
):
    padded = (
        f" {normalize(value)} "
    )

    for phrase in phrases:
        normalized_phrase = normalize(
            phrase
        )

        if (
            normalized_phrase
            and
            f" {normalized_phrase} "
            in padded
        ):
            return True

    return False


def is_perfume_product(
    store,
    href,
    anchor_text,
    card_text,
):
    return contains_phrase(
        " ".join(
            [
                str(store or ""),
                str(href or ""),
                str(anchor_text or ""),
                str(card_text or ""),
            ]
        ),
        PERFUME_TERMS,
    )


def strict_product_url(
    url,
    cfg,
):
    if not _original_valid(
        url,
        cfg,
    ):
        return False

    parsed = urllib.parse.urlsplit(
        url
    )

    path = urllib.parse.unquote(
        parsed.path or ""
    ).lower().rstrip("/")

    if (
        not path
        or path == "/"
    ):
        return False

    if any(
        bad in path
        for bad in BAD_URL_PARTS
    ):
        return False

    # Si stores.json tiene regex,
    # confiamos en ella.
    if cfg.get(
        "product_url_regex"
    ):
        return True

    if any(
        hint in path
        for hint in PRODUCT_HINTS
    ):
        return True

    if re.search(
        r"(?:^|[-_/])\d{5,}(?:[-_/\.]|$)",
        path,
    ):
        return True

    filename = path.rsplit(
        "/",
        1
    )[-1]

    if (
        filename.endswith(".html")
        and re.search(
            r"\d",
            filename
        )
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
    # Bloqueo de perfumes antes
    # de procesar el producto.
    if is_perfume_product(
        store,
        href,
        anchor_text,
        card_text,
    ):
        return None

    anchor = normalize(
        anchor_text
    )

    if any(
        bad in anchor
        for bad in BAD_TEXT
    ):
        return None

    raw = str(
        card_text or ""
    )

    normalized_raw = normalize(
        raw
    )

    # Evita que una sección completa
    # sea interpretada como producto.
    if len(raw) > 1200:
        return None

    lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip()
    ]

    if len(lines) > 30:
        return None

    if any(
        bad in normalized_raw
        for bad in (
            "quienes somos",
            "nuestras tiendas",
            "productos destacados destacado de la semana",
        )
    ):
        return None

    prices = []

    for raw_price in (
        core.PRICE_RE.findall(
            raw
        )
    ):
        price = core.price_int(
            raw_price
        )

        if (
            price is not None
            and price not in prices
        ):
            prices.append(
                price
            )

    if not prices:
        return None

    # Muchas cifras generalmente
    # significa que tomamos una sección
    # completa y no una tarjeta.
    if len(prices) > 7:
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

    name = normalize(
        product.name
    )

    if len(name) < 4:
        return None

    if any(
        bad in name
        for bad in BAD_TEXT
    ):
        return None

    if contains_phrase(
        product.name,
        PERFUME_TERMS,
    ):
        return None

    return product


def fixed_find_watched_brand(
    product,
    brand_watchlist,
):
    """
    Corrige el falso positivo:
    MAC ya NO coincide con
    MacBook o MacOnline.
    """

    haystack = normalize(
        " ".join(
            [
                product.name,
                product.raw_text,
                product.url,
                product.store,
            ]
        )
    )

    padded = (
        f" {haystack} "
    )

    brands = sorted(
        brand_watchlist,
        key=lambda x: len(
            normalize(x)
        ),
        reverse=True,
    )

    for brand in brands:
        normalized_brand = normalize(
            brand
        )

        if not normalized_brand:
            continue

        if (
            f" {normalized_brand} "
            in padded
        ):
            return brand

    return None


def repeat_alert_reason(
    product,
    previous,
):
    should_alert, metadata = (
        _original_alert_reason(
            product,
            previous,
        )
    )

    # Si sigue cumpliendo el descuento,
    # vuelve a avisar en la próxima
    # ejecución de 5 minutos.
    if (
        REPEAT_ALERTS
        and metadata.get(
            "published_match"
        )
    ):
        should_alert = True

    metadata[
        "repeat_alerts"
    ] = REPEAT_ALERTS

    return (
        should_alert,
        metadata,
    )


async def verify_direct_url_strict(
    browser,
    product,
    cfg,
    semaphore,
):
    if not strict_product_url(
        product.url,
        cfg,
    ):
        return False

    async with semaphore:
        context = (
            await browser.new_context(
                locale="es-CL",
                user_agent=(
                    "Mozilla/5.0 "
                    "(X11; Linux x86_64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/126.0.0.0 "
                    "Safari/537.36"
                ),
            )
        )

        page = (
            await context.new_page()
        )

        try:
            response = await page.goto(
                product.url,
                wait_until=(
                    "domcontentloaded"
                ),
                timeout=(
                    core.PAGE_TIMEOUT_MS
                ),
            )

            if (
                response
                and response.status >= 400
            ):
                return False

            await page.wait_for_timeout(
                1200
            )

            try:
                body = (
                    await page.locator(
                        "body"
                    ).inner_text(
                        timeout=8000
                    )
                )
            except Exception:
                body = ""

            try:
                title = (
                    await page.title()
                )
            except Exception:
                title = ""

            combined_raw = (
                title
                + " "
                + body
            )

            combined = normalize(
                combined_raw
            )

            if len(combined) < 30:
                return False

            bad_pages = (
                "pagina no encontrada",
                "page not found",
                "producto no encontrado",
                "product not found",
                "404 not found",
            )

            if any(
                marker in combined
                for marker in bad_pages
            ):
                return False

            current = int(
                product.current_price
            )

            price_variants = (
                str(current),
                f"{current:,}".replace(
                    ",",
                    "."
                ),
                f"$ {current:,}".replace(
                    ",",
                    "."
                ),
                f"${current:,}".replace(
                    ",",
                    "."
                ),
            )

            price_ok = any(
                variant in combined_raw
                for variant
                in price_variants
            )

            words = [
                word
                for word in normalize(
                    product.name
                ).split()
                if (
                    len(word) >= 4
                    and word
                    not in STOP_WORDS
                )
            ]

            words = list(
                dict.fromkeys(
                    words
                )
            )[:8]

            matches = sum(
                word in combined
                for word in words
            )

            name_ok = (
                bool(words)
                and matches
                >= min(
                    2,
                    len(words)
                )
            )

            # AHORA EXIGE LAS DOS:
            # precio correcto Y nombre.
            verified = (
                price_ok
                and name_ok
            )

            print(
                (
                    "VERIFY OK:"
                    if verified
                    else "VERIFY FAIL:"
                ),
                product.store,
                product.name[:90],
                "| precio:",
                price_ok,
                "| nombre:",
                name_ok,
            )

            return verified

        except Exception as error:
            print(
                "VERIFY ERROR:",
                product.store,
                product.url,
                str(error)[:180],
            )

            return False

        finally:
            await context.close()


def telegram_verbose(
    text,
):
    result = (
        _original_telegram(
            text
        )
    )

    print(
        "✅ TELEGRAM ENVIADO"
        if result
        else "❌ TELEGRAM NO ENVIADO"
    )

    return result


# Aplicar correcciones al monitor principal.
core.valid_product_url = (
    strict_product_url
)

core.parse_candidate = (
    strict_parse_candidate
)

core.find_watched_brand = (
    fixed_find_watched_brand
)

core.alert_reason = (
    repeat_alert_reason
)

core.verify_direct_url = (
    verify_direct_url_strict
)

core.telegram_send = (
    telegram_verbose
)


if __name__ == "__main__":
    print(
        "✅ Monitor fixed activo"
    )

    print(
        "🔁 Repetir alertas:",
        REPEAT_ALERTS,
    )

    print(
        "🚫 Perfumes: bloqueados"
    )

    core.main()
