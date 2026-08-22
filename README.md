# Monitor Retail Chile — GitHub Actions + Telegram

Monitorea Falabella, Ripley, ABC, Paris, Hites, Easy, Sodimac, Lider, Mercado Libre Chile y PC Factory.

## Reglas
- GitHub Actions intenta ejecutar cada **5 minutos**.
- Umbral general: **70%**.
- Tecnología/electrodomésticos: **60%**.
- Guarda el precio del mismo producto/SKU entre ejecuciones.
- Solo llama **caída real** a una baja contra el precio observado en la ejecución anterior.
- Un descuento publicado se trata por separado.
- No repite una liquidación si el precio sigue igual.
- Solo alerta si puede abrir y validar una **URL directa del producto**.
- Telegram recibe precio anterior, actual, referencia, porcentajes y link exacto.

> GitHub permite cron cada 5 minutos, pero el inicio puede retrasarse por carga. No es tiempo real duro.

## 1. Sube esta carpeta a un repositorio GitHub
Incluye la carpeta oculta `.github/workflows/`.

## 2. Crea un bot de Telegram
Con `@BotFather`, usa `/newbot`, guarda el token y envíale un mensaje a tu bot.

Para obtener el chat ID abre:
`https://api.telegram.org/botTU_TOKEN/getUpdates`

y busca `"chat":{"id":...}`.

## 3. Crea dos Secrets en GitHub
Repositorio → **Settings → Secrets and variables → Actions → New repository secret**

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## 4. Prueba manual
**Actions → Monitor Retail Chile → Run workflow**.

La primera ejecución arma la línea base. Desde la siguiente puede comparar el mismo producto contra el precio anterior.

## Ajustes
`stores.json` contiene las tiendas, URLs iniciales y patrones de URL directa.

En `.github/workflows/monitor.yml` puedes cambiar:
- `DISCOUNT_THRESHOLD: "70"`
- `TECH_THRESHOLD: "60"`
- cron `*/5 * * * *`

## Estado
`state/prices.json` se conserva entre ejecuciones mediante GitHub Actions Cache. No contiene credenciales.

## Importante
Las tiendas pueden cambiar HTML o bloquear automatización. El monitor prefiere saltarse un caso antes que enviarte un link genérico o una falsa caída de precio.
