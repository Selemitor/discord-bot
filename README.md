# Crypto News Desk Bot

Osobny bot Discord do filtrowania i publikowania najważniejszych newsów z rynku krypto.

## Funkcje

- pobieranie RSS z kilku źródeł,
- scoring ważności 0-100,
- deduplikacja newsów,
- alerty tylko powyżej progu ważności,
- digest o wybranych godzinach,
- komendy `/news_scan`, `/news_digest`, `/news_status`, `/news_reset`.

## Start command na Render

```text
gunicorn crypto_news_bot:app --bind 0.0.0.0:$PORT
```

## Zmienne środowiskowe

```text
NEWS_BOT_TOKEN=
NEWS_CHANNEL_ID=
NEWS_ALERT_SCORE_THRESHOLD=70
NEWS_DIGEST_HOURS=9,21
NEWS_FEED_POLL_MINUTES=10
NEWS_STATE_FILE=crypto_news_state.json
PYTHONUNBUFFERED=1
```

Opcjonalnie można nadpisać listę źródeł:

```text
NEWS_FEEDS=https://www.coindesk.com/arc/outboundfeeds/rss/,https://cointelegraph.com/rss,https://decrypt.co/feed
```

## Kategorie

- Regulacje
- ETF
- Giełdy
- Hack / exploit
- Makro
- BTC
- ETH
- Stablecoiny
- Altcoiny

## Lokalna instalacja

```text
pip install -r requirements.txt
```

## Diagnostyka

Jeśli `/news_scan` pokazuje `Nowe wpisy: 0`, użyj:

```text
/news_status
```

Jeśli feedy pobierają wpisy, ale bot ma je już w pamięci, wyczyść pamięć:

```text
/news_reset
/news_scan
```

Do testów można użyć:

```text
/news_scan publish_all:true
```

## Ważne

Nie commituj prawdziwych tokenów. Ustaw je jako zmienne środowiskowe na Render albo w lokalnym `.env`.
