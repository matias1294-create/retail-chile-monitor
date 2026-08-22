#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


CONFIG_PATH = Path(os.getenv("MONITOR_CONFIG", "stores.json"))
STATE_PATH = Path(os.getenv("MONITOR_STATE", "state/prices.json"))
REPORT_PATH = Path(os.getenv("MONITOR_REPORT", "state/last_report.json"))

DEFAULT_DISCOUNT = float(os.getenv("DISCOUNT_THRESHOLD", "70"))
TECH_THRESHOLD = float(os.getenv("TECH_THRESHOLD", "60"))
BRAND_THRESHOLD = float(os.getenv("BRAND_THRESHOLD", "50"))

MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
PAGE_TIMEOUT_MS = int(os.getenv("PAGE_TIMEOUT_MS", "35000"))
SCROLL_ROUNDS = int(os.getenv("SCROLL_ROUNDS", "4"))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES_PER_STORE", "220"))

PRICE_RE = re.compile(r"\$\s*([0-9][0-9.\s]{1,14})")
DISCOUNT_RE = re.compile(r"(?<!\d)-\s*(\d{1,3})\s*%")
WS_RE = re.compile(r"\s+")

TECH_WORDS = {
    "notebook",
    "laptop",
    "tablet",
    "celular",
    "smartphone",
    "iphone",
    "macbook",
    "ipad",
    "imac",
    "computador",
    "computadora",
    "pc gamer",
    "desktop",
    "televisor",
    "smart tv",
    "monitor",
    "audífono",
    "audifono",
    "parlante",
    "consola",
    "playstation",
    "xbox",
    "nintendo",
    "ssd",
    "disco duro",
    "memoria ram",
    "procesador",
    "gpu",
    "tarjeta gráfica",
    "tarjeta grafica",
    "router",
    "impresora",
    "smartwatch",
    "galaxy",
    "motorola",
    "xiaomi",
}

BAD_PATH_PARTS = (
    "/category/",
    "/categor",
    "/collection/",
    "/coleccion",
    "/marca/",
    "/brand/",
    "/search",
    "/busca",
    "/ofertas",
    "/outlet",
    "/landing",
    "/page/",
    "/especial",
    "/campana",
    "/campaign",
)


@dataclass
class Product:
    store: str
    name: str
    url: str
    current_price: int
    reference_price: Optional[int]
    published_discount: Optional[float]
    raw_text: str
    sku: Optional[str] = None
    watched_brand: Optional[str] = None
    category: Optional[str] = None


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def norm(s):
    return WS_RE.sub(" ", s or "").strip()


def normalize_search_text(text):
    text = str(text or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return WS_RE.sub(" ", text).strip()


def price_int(raw):
    digits = re.sub(r"\D", "", raw or "")

    if not digits:
        return None

    number = int(digits)

    if 0 < number < 2_000_000_000:
        return number

    return None


def clp(number):
    if number is None:
        return "-"

    return "$" + f"{number:,}".replace(",", ".")


def canon(url):
    p = urllib.parse.urlsplit(url)

    return urllib.parse.urlunsplit(
        (
            p.scheme,
            p.netloc.lower(),
            p.path.rstrip("/"),
            "",
            "",
        )
    )


def infer_sku(url, text):
    patterns = [
        r"/product/\d+/[^/]+/(\d+)",
        r"/product/(\d+)",
        r"/ip/[^/]+/(\d+)",
        r"/(\d{7,16})p(?:$|[?#])",
        r"/(\d{7,16})\.html(?:$|[?#])",
        r"\bSKU[:\s#-]*(\d{4,18})\b",
        r"\bMLC[- ]?(\d{6,15})\b",
    ]

    combined = url + " " + text

    for pattern in patterns:
        match = re.search(
            pattern,
            combined,
            re.I,
        )

        if match:
            return match.group(1)

    return None


def key_for(product):
    if product.sku:
        return f"{product.store}:{product.sku}"

    hashed = hashlib.sha1(
        canon(product.url).encode()
    ).hexdigest()[:24]

    return f"{product.store}:{hashed}"


def is_tech(name):
    search = " " + normalize_search_text(name) + " "

    return any(
        normalize_search_text(word) in search
        for word in TECH_WORDS
    )


def find_watched_brand(
    product,
    brand_watchlist,
):
    haystack = normalize_search_text(
        " ".join(
            [
                product.name,
                product.raw_text,
                product.url,
                product.store,
            ]
        )
    )

    brands = sorted(
        brand_watchlist,
        key=lambda x: len(str(x)),
        reverse=True,
    )

    for brand in brands:
        normalized_brand = normalize_search_text(
            brand
        )

        if not normalized_brand:
            continue

        if normalized_brand in haystack:
            return brand

    return None


def deduplicate_stores(stores):
    merged = {}

    for store in stores:
        if not store.get("enabled", True):
            continue

        name = store.get(
            "name",
            "",
        ).strip()

        if not name:
            continue

        if name not in merged:
            merged[name] = dict(store)

            merged[name]["seed_urls"] = list(
                dict.fromkeys(
                    store.get(
                        "seed_urls",
                        [],
                    )
                )
            )

            merged[name]["allowed_domains"] = list(
                dict.fromkeys(
                    store.get(
                        "allowed_domains",
                        [],
                    )
                )
            )

            continue

        current = merged[name]

        current["seed_urls"] = list(
            dict.fromkeys(
                current.get(
                    "seed_urls",
                    [],
                )
                + store.get(
                    "seed_urls",
                    [],
                )
            )
        )

        current["allowed_domains"] = list(
            dict.fromkeys(
                current.get(
                    "allowed_domains",
                    [],
                )
                + store.get(
                    "allowed_domains",
                    [],
                )
            )
        )

        if (
            not current.get("product_url_regex")
            and store.get("product_url_regex")
        ):
            current["product_url_regex"] = (
                store["product_url_regex"]
            )

    return list(merged.values())


def parse_candidate(
    store,
    href,
    anchor_text,
    card_text,
    category=None,
):
    href = canon(href)
    card_text = norm(card_text)
    anchor_text = norm(anchor_text)

    prices = []

    for raw in PRICE_RE.findall(
        card_text
    ):
        number = price_int(raw)

        if (
            number is not None
            and number not in prices
        ):
            prices.append(number)

    if not prices:
        return None

    current = prices[0]

    larger = [
        price
        for price in prices[1:]
        if price > current
    ]

    reference = (
        max(larger)
        if larger
        else None
    )

    match = DISCOUNT_RE.search(
        card_text
    )

    if match:
        published = float(
            match.group(1)
        )

    elif reference:
        published = (
            (reference - current)
            / reference
            * 100
        )

    else:
        published = None

    name = anchor_text

    if (
        not name
        or "$" in name
        or len(name) < 4
    ):
        chunks = [
            norm(x)
            for x in re.split(
                r"\$|-\s*\d+%",
                card_text,
            )
            if norm(x)
        ]

        name = (
            chunks[0][:180]
            if chunks
            else "Producto"
        )

    return Product(
        store=store,
        name=name[:220],
        url=href,
        current_price=current,
        reference_price=reference,
        published_discount=published,
        raw_text=card_text[:1800],
        sku=infer_sku(
            href,
            card_text,
        ),
        category=category,
    )


def load_state():
    if not STATE_PATH.exists():
        return {
            "version": 2,
            "products": {},
            "last_run": None,
        }

    try:
        return json.loads(
            STATE_PATH.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return {
            "version": 2,
            "products": {},
            "last_run": None,
        }


def save_state(state):
    STATE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = STATE_PATH.with_suffix(
        ".tmp"
    )

    temp.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp.replace(
        STATE_PATH
    )


def telegram_send(text):
    token = os.getenv(
        "TELEGRAM_BOT_TOKEN",
        "",
    ).strip()

    chat = os.getenv(
        "TELEGRAM_CHAT_ID",
        "",
    ).strip()

    if not token or not chat:
        print(
            "Telegram no configurado; "
            "alerta solo en log."
        )
        return False

    data = urllib.parse.urlencode(
        {
            "chat_id": chat,
            "text": text,
            "disable_web_page_preview": "false",
        }
    ).encode()

    try:
        request = urllib.request.Request(
            (
                "https://api.telegram.org/"
                f"bot{token}/sendMessage"
            ),
            data=data,
            method="POST",
        )

        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:
            response.read()

        return True

    except Exception as error:
        print(
            "ERROR Telegram:",
            error,
            file=sys.stderr,
        )

        return False


def valid_product_url(
    url,
    cfg,
):
    parsed = urllib.parse.urlsplit(
        url
    )

    host = parsed.netloc.lower()

    allowed = [
        domain.lower()
        for domain in cfg.get(
            "allowed_domains",
            [],
        )
    ]

    if allowed:
        valid_domain = any(
            host == domain
            or host.endswith(
                "." + domain
            )
            for domain in allowed
        )

        if not valid_domain:
            return False

    regex = cfg.get(
        "product_url_regex"
    )

    if regex:
        if not re.search(
            regex,
            url,
            re.I,
        ):
            return False

    else:
        path = parsed.path.lower()

        if any(
            bad in path
            for bad in BAD_PATH_PARTS
        ):
            return False

    return (
        parsed.scheme
        in ("http", "https")
        and len(parsed.path) > 3
    )


async def scrape_store(
    browser,
    cfg,
    semaphore,
):
    async with semaphore:
        context = await browser.new_context(
            locale="es-CL",
            viewport={
                "width": 1440,
                "height": 1050,
            },
            user_agent=(
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "Chrome/126 Safari/537.36"
            ),
        )

        page = await context.new_page()

        found = {}

        store = cfg["name"]
        category = cfg.get(
            "category"
        )

        try:
            for seed in cfg.get(
                "seed_urls",
                [],
            ):
                print(
                    f"[{now_iso()}] "
                    f"{store}: {seed}"
                )

                try:
                    await page.goto(
                        seed,
                        wait_until=(
                            "domcontentloaded"
                        ),
                        timeout=(
                            PAGE_TIMEOUT_MS
                        ),
                    )

                    await page.wait_for_timeout(
                        1200
                    )

                    for _ in range(
                        SCROLL_ROUNDS
                    ):
                        await page.mouse.wheel(
                            0,
                            1700,
                        )

                        await page.wait_for_timeout(
                            350
                        )

                    rows = await page.evaluate(
                        """
                        () => {
                          const anchors =
                            [...document.querySelectorAll(
                              'a[href]'
                            )];

                          const out = [];
                          const seen =
                            new Set();

                          for (
                            const a of anchors
                          ) {
                            let href =
                              a.href || '';

                            if (
                              !href ||
                              seen.has(href)
                            ) continue;

                            let node = a;
                            let chosen = null;

                            for (
                              let i = 0;
                              i < 7 && node;
                              i++,
                              node =
                                node.parentElement
                            ) {
                              const text =
                                (
                                  node.innerText ||
                                  ''
                                ).trim();

                              if (
                                text.includes('$')
                                &&
                                text.length >= 12
                                &&
                                text.length <= 2600
                              ) {
                                chosen = node;

                                if (
                                  text
                                    .split('\\n')
                                    .length >= 3
                                ) {
                                  break;
                                }
                              }
                            }

                            if (!chosen)
                              continue;

                            const cardText =
                              (
                                chosen.innerText
                                || ''
                              ).trim();

                            if (
                              !cardText
                                .includes('$')
                            ) {
                              continue;
                            }

                            seen.add(href);

                            out.push({
                              href,
                              anchorText:
                                (
                                  a.innerText
                                  ||
                                  a.getAttribute(
                                    'aria-label'
                                  )
                                  ||
                                  a.title
                                  ||
                                  ''
                                ).trim(),
                              cardText
                            });
                          }

                          return out;
                        }
                        """
                    )

                    for row in rows:
                        if (
                            len(found)
                            >= MAX_CANDIDATES
                        ):
                            break

                        href = canon(
                            row["href"]
                        )

                        if not valid_product_url(
                            href,
                            cfg,
                        ):
                            continue

                        product = parse_candidate(
                            store,
                            href,
                            row.get(
                                "anchorText",
                                "",
                            ),
                            row.get(
                                "cardText",
                                "",
                            ),
                            category=category,
                        )

                        if product:
                            found[
                                key_for(product)
                            ] = product

                except PlaywrightTimeoutError:
                    print(
                        "TIMEOUT",
                        store,
                        seed,
                        file=sys.stderr,
                    )

                except Exception as error:
                    print(
                        "ERROR",
                        store,
                        seed,
                        error,
                        file=sys.stderr,
                    )

        finally:
            await context.close()

        print(
            store,
            ":",
            len(found),
            "productos candidatos",
        )

        return list(
            found.values()
        )


async def verify_direct_url(
    browser,
    product,
    cfg,
    semaphore,
):
    if not valid_product_url(
        product.url,
        cfg,
    ):
        return False

    async with semaphore:
        context = await browser.new_context(
            locale="es-CL"
        )

        page = await context.new_page()

        try:
            response = await page.goto(
                product.url,
                wait_until=(
                    "domcontentloaded"
                ),
                timeout=PAGE_TIMEOUT_MS,
            )

            if (
                response
                and response.status >= 400
            ):
                return False

            await page.wait_for_timeout(
                700
            )

            body = norm(
                await page.locator(
                    "body"
                ).inner_text(
                    timeout=5000
                )
            )

            formatted_price = (
                f"{product.current_price:,}"
                .replace(",", ".")
            )

            body_digits = re.sub(
                r"\D",
                "",
                body,
            )

            price_ok = (
                formatted_price in body
                or str(
                    product.current_price
                ) in body_digits
            )

            words = [
                word.lower()
                for word in re.findall(
                    (
                        r"[A-Za-zÁÉÍÓÚ"
                        r"áéíóúÑñ0-9]{4,}"
                    ),
                    product.name,
                )
            ]

            if not words:
                name_ok = True

            else:
                body_lower = (
                    body.lower()
                )

                matches = sum(
                    word in body_lower
                    for word in words[:6]
                )

                name_ok = (
                    matches
                    >= min(
                        2,
                        len(words),
                    )
                )

            return bool(
                price_ok
                and name_ok
            )

        except Exception:
            return False

        finally:
            await context.close()


def alert_reason(
    product,
    previous,
):
    if product.watched_brand:
        threshold = BRAND_THRESHOLD
        alert_type = "brand"

    elif is_tech(
        product.name
    ):
        threshold = TECH_THRESHOLD
        alert_type = "technology"

    else:
        threshold = DEFAULT_DISCOUNT
        alert_type = "general"

    historical_drop = None

    if previous:
        old_price = previous.get(
            "price"
        )

        if (
            isinstance(
                old_price,
                int,
            )
            and old_price
            > product.current_price
            > 0
        ):
            historical_drop = (
                (
                    old_price
                    - product.current_price
                )
                / old_price
                * 100
            )

    published = (
        product.published_discount
    )

    historical_match = (
        historical_drop
        is not None
        and historical_drop
        >= threshold
    )

    published_match = (
        published is not None
        and published >= threshold
    )

    published_new = (
        published_match
        and (
            not previous
            or previous.get(
                "last_alert_price"
            )
            != product.current_price
        )
    )

    should_alert = (
        historical_match
        or published_new
    )

    metadata = {
        "threshold": threshold,
        "alert_type": alert_type,
        "watched_brand": (
            product.watched_brand
        ),
        "historical_drop": (
            historical_drop
        ),
        "published_discount": (
            published
        ),
        "historical_match": (
            historical_match
        ),
        "published_match": (
            published_match
        ),
    }

    return (
        should_alert,
        metadata,
    )


def format_alert(
    product,
    previous,
    metadata,
):
    old_price = (
        previous.get("price")
        if previous
        else None
    )

    lines = [
        "🚨 OFERTA / CAMBIO DE PRECIO"
    ]

    if product.watched_brand:
        lines.append(
            "🔥 MARCA VIGILADA: "
            f"{product.watched_brand}"
        )

    if product.category:
        category_text = (
            product.category
            .replace("_", " ")
            .title()
        )

        lines.append(
            f"📂 {category_text}"
        )

    lines.extend(
        [
            f"🏬 {product.store}",
            f"📦 {product.name}",
        ]
    )

    if product.sku:
        lines.append(
            f"🔎 SKU: {product.sku}"
        )

    if (
        metadata[
            "historical_match"
        ]
        and old_price
    ):
        lines.extend(
            [
                (
                    "⏱ Precio anterior: "
                    f"{clp(old_price)}"
                ),
                (
                    "💥 Precio actual: "
                    f"{clp(product.current_price)}"
                ),
                (
                    "📉 Caída real: "
                    f"{metadata['historical_drop']:.1f}%"
                ),
            ]
        )

    else:
        lines.append(
            "💥 Precio actual: "
            f"{clp(product.current_price)}"
        )

    if product.reference_price:
        lines.append(
            "🏷 Precio referencia: "
            f"{clp(product.reference_price)}"
        )

    if (
        metadata[
            "published_discount"
        ]
        is not None
    ):
        lines.append(
            "🏷 Descuento: "
            f"{metadata['published_discount']:.1f}%"
        )

    lines.append(
        "🎯 Umbral de alerta: "
        f"{metadata['threshold']:.0f}%"
    )

    lines.extend(
        [
            "",
            "🔗 LINK DIRECTO:",
            product.url,
        ]
    )

    return "\n".join(lines)


async def main_async():
    config = json.loads(
        CONFIG_PATH.read_text(
            encoding="utf-8"
        )
    )

    brand_watchlist = config.get(
        "brand_watchlist",
        [],
    )

    stores = deduplicate_stores(
        config.get(
            "stores",
            [],
        )
    )

    print(
        "Tiendas activas:",
        len(stores),
    )

    print(
        "Marcas vigiladas:",
        len(brand_watchlist),
    )

    state = load_state()

    old = state.get(
        "products",
        {},
    )

    stamp = now_iso()

    report = {
        "started_at": stamp,
        "stores": {},
        "alerts": [],
        "brand_watchlist": (
            brand_watchlist
        ),
    }

    scrape_sem = asyncio.Semaphore(
        MAX_CONCURRENCY
    )

    verify_sem = asyncio.Semaphore(
        2
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True
        )

        results = await asyncio.gather(
            *(
                scrape_store(
                    browser,
                    config_store,
                    scrape_sem,
                )
                for config_store in stores
            ),
            return_exceptions=True,
        )

        config_by_store = {
            store["name"]: store
            for store in stores
        }

        products = {}

        for store_config, result in zip(
            stores,
            results,
        ):
            if isinstance(
                result,
                Exception,
            ):
                report["stores"][
                    store_config["name"]
                ] = {
                    "error": str(
                        result
                    ),
                    "count": 0,
                }

                continue

            report["stores"][
                store_config["name"]
            ] = {
                "count": len(
                    result
                )
            }

            for product in result:
                product.watched_brand = (
                    find_watched_brand(
                        product,
                        brand_watchlist,
                    )
                )

                products[
                    key_for(product)
                ] = product

        watched_found = sum(
            1
            for product
            in products.values()
            if product.watched_brand
        )

        print(
            "Productos de marcas "
            "vigiladas:",
            watched_found,
        )

        for key, product in (
            products.items()
        ):
            previous = old.get(
                key
            )

            should_alert, metadata = (
                alert_reason(
                    product,
                    previous,
                )
            )

            verified = False

            if should_alert:
                verified = (
                    await verify_direct_url(
                        browser,
                        product,
                        config_by_store[
                            product.store
                        ],
                        verify_sem,
                    )
                )

                if verified:
                    message = format_alert(
                        product,
                        previous,
                        metadata,
                    )

                    print(
                        "\n"
                        + message
                        + "\n"
                    )

                    sent = telegram_send(
                        message
                    )

                    report[
                        "alerts"
                    ].append(
                        {
                            "product": asdict(
                                product
                            ),
                            "meta": metadata,
                            "previous_price": (
                                previous.get(
                                    "price"
                                )
                                if previous
                                else None
                            ),
                            "telegram_sent": (
                                sent
                            ),
                        }
                    )

                else:
                    print(
                        "SKIP sin URL "
                        "directa verificable:",
                        product.store,
                        product.name,
                        product.url,
                    )

            entry = (
                previous or {}
            )

            last_price = entry.get(
                "last_alert_price"
            )

            last_at = entry.get(
                "last_alert_at"
            )

            if (
                should_alert
                and verified
            ):
                last_price = (
                    product.current_price
                )

                last_at = stamp

            old[key] = {
                "store": product.store,
                "category": (
                    product.category
                ),
                "watched_brand": (
                    product.watched_brand
                ),
                "name": product.name,
                "url": product.url,
                "sku": product.sku,
                "price": (
                    product.current_price
                ),
                "reference_price": (
                    product.reference_price
                ),
                "published_discount": (
                    product.published_discount
                ),
                "last_seen": stamp,
                "last_alert_price": (
                    last_price
                ),
                "last_alert_at": (
                    last_at
                ),
            }

        await browser.close()

    now = time.time()

    kept = {}

    for key, entry in (
        old.items()
    ):
        try:
            seen = (
                datetime.fromisoformat(
                    entry["last_seen"]
                ).timestamp()
            )

        except Exception:
            seen = now

        if (
            now - seen
            < 45 * 86400
        ):
            kept[key] = entry

    save_state(
        {
            "version": 2,
            "last_run": stamp,
            "products": kept,
        }
    )

    report[
        "finished_at"
    ] = now_iso()

    REPORT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    REPORT_PATH.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        "Fin:",
        len(products),
        "productos |",
        watched_found,
        "de marcas vigiladas |",
        len(
            report["alerts"]
        ),
        "alertas verificadas",
    )

    return 0


def main():
    raise SystemExit(
        asyncio.run(
            main_async()
        )
    )


if __name__ == "__main__":
    main()
