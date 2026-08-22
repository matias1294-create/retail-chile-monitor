name: Monitor Retail Chile

on:
  workflow_dispatch: {}

  schedule:
    - cron: "*/5 * * * *"

permissions:
  contents: read

concurrency:
  group: retail-price-monitor
  cancel-in-progress: true

jobs:
  monitor:
    runs-on: ubuntu-latest
    timeout-minutes: 12

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: "pip"

      - name: Restore price state
        uses: actions/cache/restore@v4
        with:
          path: state/prices.json
          key: price-state-${{ github.run_id }}
          restore-keys: |
            price-state-

      - name: Install dependencies
        run: |
          python -m pip install -r requirements.txt
          python -m playwright install --with-deps chromium

      - name: Run monitor
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}

          DISCOUNT_THRESHOLD: "70"
          TECH_THRESHOLD: "60"
          BRAND_THRESHOLD: "50"

          MAX_CONCURRENCY: "3"
          MAX_CANDIDATES_PER_STORE: "220"
          PAGE_TIMEOUT_MS: "35000"
          SCROLL_ROUNDS: "4"

        run: python monitor.py

      - name: Save price state
        if: always()
        uses: actions/cache/save@v4
        with:
          path: state/prices.json
          key: price-state-${{ github.run_id }}
