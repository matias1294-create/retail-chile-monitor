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

# Marcas de la brand_watchlist:
# Por defecto se alertan desde 50%.
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
    """
    Convierte:
    Estée Lauder -> estee lauder
    L'Oréal -> l oreal
    La Roche-Posay -> la roche posay
    """
    text = str(text or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return WS_RE.sub(" ", text).strip()


def price_int(raw):
    d = re.sub(r"\D", "", raw or "")
    if not d:
        return None

    n = int(d)
    return n if 0 < n < 2_000_000_000 else None


def clp(n):
    return "-" if n is None else "$" + f"{n:,}".replace(",", ".")


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

    for pat in patterns:
        m = re.search(pat, url + " " + text, re.I)

        if m:
            return m.group(1)

    return None


def key_for(p):
    if p.sku:
        return f"{p.store}:{p.sku}"

    return (
        f"{p.store}:"
        + hashlib.sha1(canon(p.url).encode()).hexdigest()[:24]
    )


def is_tech(name):
    s = " " + normalize_search_text(name) + " "

    return any(
        normalize_search_text(word) in s
        for word in TECH_WORDS
    )


def find_watched_brand(product, brand_watchlist):
    """
    Busca la marca tanto en:
    - nombre del producto
    - texto de la tarjeta
    - URL
    - nombre de la tienda
    """

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

    # Primero marcas largas para evitar coincidencias demasiado generales.
    brands = sorted(
        brand_watchlist,
        key=lambda x: len(str(x)),
        reverse=True,
    )

    for brand in brands:
        normalized_brand = normalize_search_text(brand)

        if not normalized_brand:
            continue

        if normalized_brand in haystack:
            return brand

    return None


def deduplicate_stores(stores):
    """
    Si stores.json contiene la misma tienda más de una vez,
    combina sus seed_urls y dominios para no scrapearla dos veces.
    """

    merged = {}

    for store in stores:
        if not store.get("enabled", True):
            continue

        name = store.get("name", "").strip()

        if not name:
            continue

        if name not in merged:
            merged[name] = dict(store)
            merged[name]["seed_urls"] = list(
                dict.fromkeys(store.get("seed_urls", []))
            )
            merged[name]["allowed_domains"] = list(
                dict.fromkeys(store.get("allowed_domains", []))
            )
            continue

        current = merged[name]

        current["seed_urls"] = list(
            dict.fromkeys(
                current.get("seed_urls", [])
                + store.get("seed_urls", [])
            )
        )

        current["allowed_domains"] = list(
            dict.fromkeys(
                current.get("allowed_domains", [])
                + store.get("allowed_domains", [])
            )
        )

        # Si una de las versiones tiene regex, conservarla.
        if (
            not current.get("product_url_regex")
            and store.get("product_url_regex")
        ):
            current["product_url_regex"] = store[
                "product_url_regex"
            ]

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

    for raw in PRICE_RE.findall(card_text):
        n = price_int(raw)

        if n is not None and n not in prices:
            prices.append(n)

    if not prices:
        return None

    current = prices[0]

    larger = [
        p
        for p in prices[1:]
        if p > current
    ]

    reference = max(larger) if larger else None

    m = DISCOUNT_RE.search(card_text)

    published = (
        float(m.group(1))
        if m
        else (
            (reference - current) / reference * 100
            if reference
            else None
        )
    )

    name = anchor_text

    if not name or "$" in name or len(name) < 4:
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
        sku=infer_sku(href, card_text),
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
            STATE_PATH.read_text(encoding="utf-8")
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

    tmp = STATE_PATH.with_suffix(".tmp")

    tmp.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    tmp.replace(STATE_PATH)


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
            "Telegram no configurado; alerta solo en log."
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
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            method="POST",
        )

        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as r:
            r.read()

        return True

    except Exception as e:
        print(
            "ERROR Telegram:",
            e,
            file=sys.stderr,
        )
        return False


def valid_product_url(url, cfg):
    p = urllib.parse.urlsplit(url)

    host = p.netloc.lower()

    allowed = [
        d.lower()
        for d in cfg.get(
            "allowed_domains",
            [],
        )
    ]

    if allowed and not any(
        host == d
        or host.endswith("." + d)
        for d in allowed
    ):
        return False

    regex = cfg.get("product_url_regex")

    if regex and not re.search(
        regex,
        url,
        re.I,
    ):
        return False

    if (
        not regex
        and any(
            x in p.path.lower()
            for x in BAD_PATH_PARTS
        )
    ):
        return False

    return (
        p.scheme in ("http", "https")
        and len(p.path) > 3
    )


async def scrape_store(
    browser,
    cfg,
    sem,
):
    async with sem:
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
        category = cfg.get("category")

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
                        wait_until="domcontentloaded",
                        timeout=PAGE_TIMEOUT_MS,
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
                            [...document.querySelectorAll('a[href]')];

                          const out = [];
                          const seen = new Set();

                          for (const a of anchors) {
                            let href = a.href || '';

                            if (!href || seen.has(href))
                              continue;

                            let node = a;
                            let chosen = null;

                            for (
                              let i = 0;
                              i < 7 && node;
                              i++, node = node.parentElement
                            ) {
                              const t =
                                (node.innerText || '').trim();

                              if (
                                t.includes('$') &&
                                t.length >= 12 &&
                                t.length <= 2600
                              ) {
                                chosen = node;

                                if (
                                  t.split('\\n').length >= 3
                                )
                                  break;
                              }
                            }

                            if (!chosen)
                              continue;

                            const txt =
                              (chosen.innerText || '').trim();

                            if (!txt.includes('$'))
                              continue;

                            seen.add(href);

                            out.push({
                              href,
                              anchorText:
                                (
                                  a.innerText ||
                                  a.getAttribute('aria-label') ||
                                  a.title ||
                                  ''
                                ).trim(),
                              cardText: txt
                            });
                          }

                          return out;
                        }
                        """
                    )

                    for row in rows:
                        if len(found) >= MAX_CANDIDATES:
                            break

                        href = canon(
                            row["href"]
                        )

                        if not valid_product_url(
                            href,
                            cfg,
                        ):
                            continue

                        p = parse_candidate(
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

                        if p:
                            found[
                                key_for(p)
                            ] = p

                except PlaywrightTimeoutError:
                    print(
                        "TIMEOUT",
                        store,
                        seed,
                        file=sys.stderr,
                    )

                except Exception as e:
                    print(
                        "ERROR",
                        store,
                        seed,
                        e,
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

        return list(found.values())


async def verify_direct_url(
    browser,
    p,
    cfg,
    sem,
):
    if not valid_product_url(
        p.url,
        cfg,
    ):
        return False

    async with sem:
        c = await browser.new_context(
            locale="es-CL"
        )

        page = await c.new_page()

        try:
            resp = await page.goto(
                p.url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT_MS,
            )

            if (
                resp
                and resp.status >= 400
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

            digits = (
                f"{p.current_price:,}"
                .replace(",", ".")
            )

            price_ok = (
                digits in body
                or str(p.current_price)
                in re.sub(
                    r"\D",
                    "",
                    body,
                )
            )

            words = [
                w.lower()
                for w in re.findall(
                    r"[A-Za-zÁÉÍÓÚáéíóúÑñ0-9]{4,}",
                    p.name,
                )
            ]

            name_ok = (
                True
                if not words
                else sum(
                    w in body.lower()
                    for w in words[:6]
                )
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
            await c.close()


def alert_reason(
    p,
    prev,
):
    # Marca vigilada = menor umbral.
    if p.watched_brand:
        threshold = BRAND_THRESHOLD
        alert_type = "brand"

    elif is_tech(p.name):
        threshold = TECH_THRESHOLD
        alert_type = "technology"

    else:
        threshold = DEFAULT_DISCOUNT
        alert_type = "general"

    historical_drop = None

    if prev:
        old = prev.get("price")

        if (
            isinstance(old, int)
            and old > p.current_price > 0
        ):
            historical_drop = (
                (old - p.current_price)
                / old
                * 100
            )

    published = p.published_discount

    historical_match = (
        historical_drop is not None
        and historical_drop >= threshold
    )

    published_match = (
        published is not None
        and published >= threshold
    )

    published_new = (
        published_match
        and (
            not prev
            or prev.get(
                "last_alert_price"
            )
            != p.current_price
        )
    )

    return (
        historical_match
        or published_new
    ), {
        "threshold": threshold,
        "alert_type": alert_type,
        "watched_brand": p.watched_brand,
        "historical_drop": historical_drop,
        "published_discount": published,
        "historical_match": historical_match,
        "published_match": published_match,
    }


def format_alert(
    p,
    prev,
    meta,
):
    old = (
        prev.get("price")
        if prev
        else None
    )

    lines = [
        "🚨 OFERTA / CAMBIO DE PRECIO"
    ]

    if p.watched_brand:
        lines.append(
            f"🔥 MARCA VIGILADA: "
            f"{p.watched_brand}"
        )

    if p.category:
        category_text = (
            p.category
            .replace("_", " ")
            .title()
        )

        lines.append(
            f"📂 {category_text}"
        )

    lines.extend(
        [
            f"🏬 {p.store}",
            f"📦 {p.name}",
        ]
    )

    if p.sku:
        lines.append(
            f"🔎 SKU: {p.sku}"
        )

    if (
        meta["historical_match"]
        and old
    ):
        lines.extend(
            [
                (
                    "⏱ Precio ejecución anterior: "
                    f"{clp(old)}"
                ),
                (
                    "💥 Precio actual: "
                    f"{clp(p.current_price)}"
                ),
                (
                    "📉 Caída real entre ejecuciones: "
                    f"{meta['historical_drop']:.1f}%"
                ),
            ]
        )

    else:
        lines.append(
            "💥 Precio actual: "
            f"{clp(p.current_price)}"
        )

    if p.reference_price:
        lines.append(
            "🏷 Precio referencia publicado: "
            f"{clp(p.reference_price)}"
        )

    if (
        meta["published_discount"]
        is not None
    ):
        lines.append(
            "🏷 Descuento publicado/calculado: "
            f"{meta['published_discount']:.1f}%"
        )

    lines.append(
        f"🎯 Umbral de alerta: "
        f"{meta['threshold']:.0f}%"
    )

    lines.extend(
        [
            "",
            "🔗 LINK DIRECTO:",
            p.url,
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
        "brand_watchlist": brand_watchlist,
    }

    scrape_sem = asyncio.Semaphore(
        MAX_CONCURRENCY
    )

    verify_sem = asyncio.Semaphore(2)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True
        )

        results = await asyncio.gather(
            *(
                scrape_store(
                    browser,
                    c,
                    scrape_sem,
                )
                for c in stores
            ),
            return_exceptions=True,
        )

        cfg_by = {
            c["name"]: c
            for c in stores
        }

        products = {}

        for cfg, res in zip(
            stores,
            results,
        ):
            if isinstance(
                res,
                Exception,
            ):
                report["stores"][
                    cfg["name"]
                ] = {
                    "error": str(res),
                    "count": 0,
                }

                continue

            report["stores"][
                cfg["name"]
            ] = {
                "count": len(res)
            }

            for p in res:
                p.watched_brand = (
                    find_watched_brand(
                        p,
                        brand_watchlist,
                    )
                )

                products[
                    key_for(p)
                ] = p

        watched_found = sum(
            1
            for p in products.values()
            if p.watched_brand
        )

        print(
            "Productos de marcas vigiladas:",
            watched_found,
        )

        for key, p in products.items():
            prev = old.get(key)

            should, meta = alert_reason(
                p,
                prev,
            )

            verified = False

            if should:
                verified = (
                    await verify_direct_url(
                        browser,
                        p,
                        cfg_by[p.store],
                        verify_sem,
                    )
                )

                if verified:
                    msg = format_alert(
                        p,
                        prev,
                        meta,
                    )

                    print(
                        "\n"
                        + msg
                        + "\n"
                    )

                    sent = telegram_send(
                        msg
                    )

                    report[
                        "alerts"
                    ].append(
                        {
                            "product": asdict(
                                p
                            ),
                            "meta": meta,
                            "previous_price": (
                                prev.get(
                                    "price"
                                )
                                if prev
                                else None
                            ),
                            "telegram_sent": sent,
                        }
                    )

                else:
                    print(
                        "SKIP sin URL "
                        "directa verificable:",
                        p.store,
                        p.name,
                        p.url,
                    )

            entry = prev or {}

            last_price = entry.get(
                "last_alert_price"
            )

            last_at = entry.get(
                "last_alert_at"
            )

            if should and verified:
                last_price = (
                    p.current_price
                )

                last_at = stamp

            old[key] = {
                "store": p.store,
                "category": p.category,
                "watched_brand": p.watched_brand,
                "name": p.name,
                "url": p.url,
                "sku": p.sku,
                "price": p.current_price,
                "reference_price": (
                    p.reference_price
                ),
                "published_discount": (
                    p.published_discount
                ),
                "last_seen": stamp,
                "last_alert_price": (
                    last_price
                ),
                "last_alert_at": last_at,
            }

        await browser.close()

    now = time.time()

    kept = {}

    for k, e in old.items():
        try:
            seen = datetime.fromisoformat(
                e["last_seen"]
            ).timestamp()

        except Exception:
            seen = now

        if now - seen < 45 * 86400:
            kept[k] = e

    save_state(
        {
            "version": 2,
            "last_run": stamp,
            "products": kept,
        }
    )

    report["finished_at"] = now_iso()

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
        len(report["alerts"]),
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
