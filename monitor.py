#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
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


# ============================================================
# CONFIGURACIÓN
# ============================================================

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

# Precio máximo razonable en CLP.
MAX_REASONABLE_PRICE = 30_000_000

# Una referencia no puede superar 10 veces el precio actual.
MAX_REFERENCE_MULTIPLIER = 10

# Descuento calculado superior a esto se considera sospechoso.
MAX_CALCULATED_DISCOUNT = 95.0


# ============================================================
# REGEX DE PRECIOS
# ============================================================

# IMPORTANTE:
# Esta expresión NO permite espacios dentro del número.
#
# Correcto:
#   $729.990
#   $1.299.990
#   $9990
#
# No convierte:
#   "$799.990 10" -> 79.999.010
PRICE_RE = re.compile(
    r"\$\s*([0-9]{1,3}(?:\.[0-9]{3})+|[0-9]{4,9})(?![\d.])"
)

DISCOUNT_RE = re.compile(
    r"(?<!\d)-?\s*(\d{1,3})\s*%"
)

WS_RE = re.compile(r"\s+")

TECH_WORDS = {
    "notebook",
    "laptop",
    "tablet",
    "celular",
    "smartphone",
    "iphone",
    "televisor",
    "smart tv",
    "oled",
    "qled",
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
    "refrigerador",
    "lavadora",
    "secadora",
    "lavavajillas",
    "microondas",
    "horno eléctrico",
    "horno electrico",
    "freidora",
    "aspiradora",
    "aire acondicionado",
    "estufa eléctrica",
    "estufa electrica",
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


# ============================================================
# MODELO
# ============================================================

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


# ============================================================
# UTILIDADES
# ============================================================

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def norm(value: str) -> str:
    return WS_RE.sub(" ", value or "").strip()


def clp(value: Optional[int]) -> str:
    if value is None:
        return "-"
    return "$" + f"{value:,}".replace(",", ".")


def canon(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)

    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )


def parse_clp_number(raw: str) -> Optional[int]:
    """
    Convierte:
      729.990 -> 729990
      1.299.990 -> 1299990

    No concatena números separados por espacios.
    """

    if not raw:
        return None

    raw = raw.strip()

    # Solo admitimos dígitos y puntos.
    if not re.fullmatch(r"[0-9.]+", raw):
        return None

    digits = raw.replace(".", "")

    if not digits.isdigit():
        return None

    value = int(digits)

    if value <= 0:
        return None

    if value > MAX_REASONABLE_PRICE:
        return None

    return value


def extract_prices(text: str) -> list[int]:
    """
    Extrae precios únicos en el orden en que aparecen.
    """

    values: list[int] = []

    for match in PRICE_RE.finditer(text or ""):
        value = parse_clp_number(match.group(1))

        if value is None:
            continue

        if value not in values:
            values.append(value)

    return values


def infer_sku(url: str, text: str) -> Optional[str]:
    patterns = [
        r"/product/\d+/[^/]+/(\d+)",
        r"/product/(\d+)",
        r"/ip/[^/]+/(\d+)",
        r"/(\d{7,16})p(?:$|[?#])",
        r"/(\d{7,16})\.html(?:$|[?#])",
        r"\bSKU[:\s#-]*(\d{4,18})\b",
        r"\bPLU[:\s#-]*(\d{4,18})\b",
        r"\bMLC[- ]?(\d{6,15})\b",
    ]

    combined = f"{url} {text}"

    for pattern in patterns:
        match = re.search(pattern, combined, re.I)

        if match:
            return match.group(1)

    return None


def key_for(product: Product) -> str:
    if product.sku:
        return f"{product.store}:{product.sku}"

    digest = hashlib.sha1(
        canon(product.url).encode("utf-8")
    ).hexdigest()[:24]

    return f"{product.store}:{digest}"


def is_tech(name: str) -> bool:
    text = f" {name.lower()} "
    return any(word in text for word in TECH_WORDS)


# ============================================================
# VALIDACIÓN DE PRECIO DE REFERENCIA
# ============================================================

def choose_reference_price(
    current: int,
    prices: list[int],
) -> Optional[int]:

    candidates = []

    for price in prices:
        if price <= current:
            continue

        # Nunca aceptar precios físicamente absurdos.
        if price > MAX_REASONABLE_PRICE:
            continue

        # Protección contra el bug tipo:
        # 729.990 -> referencia 79.999.010
        if price > current * MAX_REFERENCE_MULTIPLIER:
            continue

        discount = (price - current) / price * 100

        # Si el descuento calculado es >95%,
        # probablemente hubo una lectura incorrecta.
        if discount > MAX_CALCULATED_DISCOUNT:
            continue

        candidates.append(price)

    if not candidates:
        return None

    return max(candidates)


# ============================================================
# PARSER DE TARJETAS DE PRODUCTO
# ============================================================

def parse_candidate(
    store: str,
    href: str,
    anchor_text: str,
    card_text: str,
) -> Optional[Product]:

    href = canon(href)
    card_text = norm(card_text)
    anchor_text = norm(anchor_text)

    prices = extract_prices(card_text)

    if not prices:
        return None

    # Normalmente el primer precio visible de la tarjeta
    # es el precio de venta.
    current = prices[0]

    if current <= 0 or current > MAX_REASONABLE_PRICE:
        return None

    reference = choose_reference_price(
        current,
        prices[1:],
    )

    discount_match = DISCOUNT_RE.search(card_text)

    published_discount: Optional[float] = None

    if discount_match:
        published_discount = float(discount_match.group(1))

        # Nunca aceptar porcentajes fuera de rango.
        if published_discount <= 0 or published_discount > 95:
            published_discount = None

    # Si la tienda no muestra porcentaje,
    # lo calculamos solo si tenemos referencia válida.
    if published_discount is None and reference:
        calculated = (reference - current) / reference * 100

        if 0 < calculated <= MAX_CALCULATED_DISCOUNT:
            published_discount = calculated

    name = anchor_text

    if not name or "$" in name or len(name) < 4:
        chunks = [
            norm(part)
            for part in re.split(
                r"\$|-?\s*\d+%",
                card_text,
            )
            if norm(part)
        ]

        name = chunks[0][:180] if chunks else "Producto"

    return Product(
        store=store,
        name=name[:220],
        url=href,
        current_price=current,
        reference_price=reference,
        published_discount=published_discount,
        raw_text=card_text[:1800],
        sku=infer_sku(href, card_text),
    )


# ============================================================
# ESTADO
# ============================================================

def load_state() -> dict:
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


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = STATE_PATH.with_suffix(".tmp")

    temp.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp.replace(STATE_PATH)


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(text: str) -> bool:
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
    ).encode("utf-8")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:
            response.read()

        return True

    except Exception as exc:
        print(
            "ERROR Telegram:",
            exc,
            file=sys.stderr,
        )
        return False


# ============================================================
# VALIDACIÓN DE URL
# ============================================================

def valid_product_url(
    url: str,
    cfg: dict,
) -> bool:

    parsed = urllib.parse.urlsplit(url)

    host = parsed.netloc.lower()

    allowed_domains = [
        domain.lower()
        for domain in cfg.get(
            "allowed_domains",
            [],
        )
    ]

    if allowed_domains:
        valid_domain = any(
            host == domain
            or host.endswith("." + domain)
            for domain in allowed_domains
        )

        if not valid_domain:
            return False

    regex = cfg.get("product_url_regex")

    if regex:
        if not re.search(regex, url, re.I):
            return False

    else:
        lower_path = parsed.path.lower()

        if any(
            bad_part in lower_path
            for bad_part in BAD_PATH_PARTS
        ):
            return False

    return (
        parsed.scheme in ("http", "https")
        and len(parsed.path) > 3
    )


# ============================================================
# SCRAPING
# ============================================================

async def scrape_store(
    browser,
    cfg: dict,
    semaphore: asyncio.Semaphore,
) -> list[Product]:

    async with semaphore:

        context = await browser.new_context(
            locale="es-CL",
            viewport={
                "width": 1440,
                "height": 1050,
            },
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "Chrome/126 Safari/537.36"
            ),
        )

        page = await context.new_page()

        found: dict[str, Product] = {}

        store = cfg["name"]

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

                    await page.wait_for_timeout(1200)

                    for _ in range(SCROLL_ROUNDS):
                        await page.mouse.wheel(
                            0,
                            1700,
                        )
                        await page.wait_for_timeout(350)

                    rows = await page.evaluate(
                        """
                        () => {
                          const anchors = [
                            ...document.querySelectorAll('a[href]')
                          ];

                          const out = [];
                          const seen = new Set();

                          for (const a of anchors) {

                            const href = a.href || '';

                            if (!href || seen.has(href))
                              continue;

                            let node = a;
                            let chosen = null;

                            for (
                              let i = 0;
                              i < 7 && node;
                              i++, node = node.parentElement
                            ) {

                              const text =
                                (node.innerText || '').trim();

                              if (
                                text.includes('$') &&
                                text.length >= 12 &&
                                text.length <= 2600
                              ) {
                                chosen = node;

                                if (
                                  text.split('\\n').length >= 3
                                )
                                  break;
                              }
                            }

                            if (!chosen)
                              continue;

                            const cardText =
                              (chosen.innerText || '').trim();

                            if (!cardText.includes('$'))
                              continue;

                            seen.add(href);

                            out.push({
                              href,
                              anchorText:
                                (
                                  a.innerText ||
                                  a.getAttribute(
                                    'aria-label'
                                  ) ||
                                  a.title ||
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

                        if len(found) >= MAX_CANDIDATES:
                            break

                        href = canon(
                            row.get("href", "")
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

                except Exception as exc:
                    print(
                        "ERROR",
                        store,
                        seed,
                        exc,
                        file=sys.stderr,
                    )

        finally:
            await context.close()

        print(
            f"{store}: "
            f"{len(found)} productos candidatos"
        )

        return list(found.values())


# ============================================================
# VERIFICACIÓN DE FICHA DIRECTA
# ============================================================

async def verify_direct_url(
    browser,
    product: Product,
    cfg: dict,
    semaphore: asyncio.Semaphore,
) -> bool:

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
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT_MS,
            )

            if response and response.status >= 400:
                return False

            await page.wait_for_timeout(700)

            body = norm(
                await page.locator(
                    "body"
                ).inner_text(
                    timeout=5000
                )
            )

            # Confirmamos el precio actual exacto.
            formatted = (
                f"{product.current_price:,}"
                .replace(",", ".")
            )

            price_ok = (
                formatted in body
                or str(
                    product.current_price
                )
                in re.sub(
                    r"\D",
                    "",
                    body,
                )
            )

            words = [
                word.lower()
                for word in re.findall(
                    r"[A-Za-zÁÉÍÓÚáéíóúÑñ0-9]{4,}",
                    product.name,
                )
            ]

            if not words:
                name_ok = True
            else:
                matches = sum(
                    word in body.lower()
                    for word in words[:6]
                )

                name_ok = (
                    matches >= min(
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


# ============================================================
# REGLAS DE ALERTA
# ============================================================

def alert_reason(
    product: Product,
    previous: Optional[dict],
) -> tuple[bool, dict]:

    threshold = (
        TECH_THRESHOLD
        if is_tech(product.name)
        else DEFAULT_DISCOUNT
    )

    historical_drop: Optional[float] = None

    if previous:
        old_price = previous.get("price")

        if (
            isinstance(old_price, int)
            and old_price > product.current_price > 0
        ):
            historical_drop = (
                (old_price - product.current_price)
                / old_price
                * 100
            )

    published = product.published_discount

    historical_match = (
        historical_drop is not None
        and historical_drop >= threshold
    )

    published_match = (
        published is not None
        and published >= threshold
        and published <= MAX_CALCULATED_DISCOUNT
    )

    # No repetir la misma oferta al mismo precio.
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

    return should_alert, {
        "threshold": threshold,
        "historical_drop": historical_drop,
        "published_discount": published,
        "historical_match": historical_match,
        "published_match": published_match,
    }


# ============================================================
# MENSAJE TELEGRAM
# ============================================================

def format_alert(
    product: Product,
    previous: Optional[dict],
    meta: dict,
) -> str:

    old_price = (
        previous.get("price")
        if previous
        else None
    )

    lines = [
        "🚨 OFERTA / CAMBIO DE PRECIO",
        "📂 Multitienda",
        f"🏬 {product.store}",
        f"📦 {product.name}",
    ]

    if product.sku:
        lines.append(
            f"🔎 SKU: {product.sku}"
        )

    if (
        meta["historical_match"]
        and old_price
    ):
        lines.extend(
            [
                (
                    "⏱ Precio ejecución anterior: "
                    f"{clp(old_price)}"
                ),
                (
                    "💥 Precio actual: "
                    f"{clp(product.current_price)}"
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
            f"{clp(product.current_price)}"
        )

    if product.reference_price:
        lines.append(
            "🏷 Precio referencia: "
            f"{clp(product.reference_price)}"
        )

    if (
        meta["published_discount"]
        is not None
    ):
        lines.append(
            "🏷 Descuento: "
            f"{meta['published_discount']:.1f}%"
        )

    lines.append(
        "🎯 Umbral de alerta: "
        f"{meta['threshold']:.0f}%"
    )

    lines.extend(
        [
            "",
            "🔗 LINK DIRECTO:",
            product.url,
        ]
    )

    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================

async def main_async() -> int:

    config = json.loads(
        CONFIG_PATH.read_text(
            encoding="utf-8"
        )
    )

    stores = [
        store
        for store in config["stores"]
        if store.get(
            "enabled",
            True,
        )
    ]

    state = load_state()

    old_products = state.get(
        "products",
        {},
    )

    stamp = now_iso()

    report = {
        "started_at": stamp,
        "stores": {},
        "alerts": [],
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
                    cfg,
                    scrape_sem,
                )
                for cfg in stores
            ),
            return_exceptions=True,
        )

        cfg_by_name = {
            cfg["name"]: cfg
            for cfg in stores
        }

        products: dict[str, Product] = {}

        for cfg, result in zip(
            stores,
            results,
        ):

            if isinstance(
                result,
                Exception,
            ):
                report["stores"][
                    cfg["name"]
                ] = {
                    "error": str(result),
                    "count": 0,
                }

                continue

            report["stores"][
                cfg["name"]
            ] = {
                "count": len(result)
            }

            for product in result:
                products[
                    key_for(product)
                ] = product

        for key, product in products.items():

            previous = old_products.get(key)

            should_alert, meta = alert_reason(
                product,
                previous,
            )

            verified = False

            if should_alert:

                # Protección final extra:
                # nunca mandar alerta con referencias absurdas.
                if product.reference_price:

                    if (
                        product.reference_price
                        > product.current_price
                        * MAX_REFERENCE_MULTIPLIER
                    ):
                        print(
                            "SKIP referencia absurda:",
                            product.store,
                            product.name,
                            product.reference_price,
                        )

                        should_alert = False

                if should_alert:

                    verified = await verify_direct_url(
                        browser,
                        product,
                        cfg_by_name[
                            product.store
                        ],
                        verify_sem,
                    )

                if verified:

                    message = format_alert(
                        product,
                        previous,
                        meta,
                    )

                    print(
                        "\n"
                        + message
                        + "\n"
                    )

                    sent = telegram_send(
                        message
                    )

                    report["alerts"].append(
                        {
                            "product": asdict(
                                product
                            ),
                            "meta": meta,
                            "previous_price": (
                                previous.get(
                                    "price"
                                )
                                if previous
                                else None
                            ),
                            "telegram_sent": sent,
                        }
                    )

                elif should_alert:
                    print(
                        "SKIP sin URL directa "
                        "verificable:",
                        product.store,
                        product.name,
                        product.url,
                    )

            entry = previous or {}

            last_alert_price = entry.get(
                "last_alert_price"
            )

            last_alert_at = entry.get(
                "last_alert_at"
            )

            if should_alert and verified:
                last_alert_price = (
                    product.current_price
                )
                last_alert_at = stamp

            old_products[key] = {
                "store": product.store,
                "name": product.name,
                "url": product.url,
                "sku": product.sku,
                "price": product.current_price,
                "reference_price": (
                    product.reference_price
                ),
                "published_discount": (
                    product.published_discount
                ),
                "last_seen": stamp,
                "last_alert_price": (
                    last_alert_price
                ),
                "last_alert_at": (
                    last_alert_at
                ),
            }

        await browser.close()

    # Limpiar productos que llevan
    # más de 45 días sin verse.
    current_time = time.time()

    kept = {}

    for key, entry in old_products.items():

        try:
            seen = datetime.fromisoformat(
                entry["last_seen"]
            ).timestamp()

        except Exception:
            seen = current_time

        if (
            current_time - seen
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
        len(report["alerts"]),
        "alertas verificadas",
    )

    return 0


def main() -> None:
    raise SystemExit(
        asyncio.run(
            main_async()
        )
    )


if __name__ == "__main__":
    main()
