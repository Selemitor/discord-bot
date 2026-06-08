# Plik: bot.py
# OSTATECZNA WERSJA: Łączy Flask (dla Gunicorn) i Bota Discord (w wątku).

import os
import discord
from discord.ext import commands, tasks
import requests
import datetime
from datetime import date, timedelta
import csv
import io
import json
import time
import numpy as np
import asyncio
import re
from pathlib import Path
from zoneinfo import ZoneInfo
from threading import Thread # <-- WAŻNE: Importujemy wątki
from flask import Flask # <-- WAŻNE: Importujemy Flask

# Wymaga instalacji: google-genai
from google import genai
from google.genai import types

# --- Konfiguracja Flask (dla UptimeRobot/Gunicorn) ---
# Gunicorn będzie szukał obiektu 'app'
app = Flask(__name__)

@app.route('/')
def home():
    """Endpoint dla UptimeRobot, aby utrzymać bota przy życiu."""
    return "Bot jest aktywny!"

@app.route('/healthz')
def health_check():
    """Endpoint dla Render Health Check."""
    return "OK", 200

# --- Konfiguracja Bota Discord ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
COINGECKO_API_KEY = os.environ.get('COINGECKO_API_KEY')
ALPHAVANTAGE_API_KEY = os.environ.get('ALPHAVANTAGE_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

if not BOT_TOKEN:
    print("KRYTYCZNY BŁĄD: Nie znaleziono BOT_TOKEN. Aplikacja nie wystartuje.")
else:
    print("BOT_TOKEN znaleziony.")

# --- Reszta Konfiguracji ---
CHANNEL_ID = int(os.environ.get('CHANNEL_ID', '1429744335389458452'))
DAILY_REPORT_HOURS = [
    int(hour.strip())
    for hour in os.environ.get('DAILY_REPORT_HOURS', '8').split(',')
    if hour.strip()
]
DAILY_REPORT_CATCHUP_UNTIL_HOUR = int(os.environ.get('DAILY_REPORT_CATCHUP_UNTIL_HOUR', '23'))
MARKET_STATE_FILE = Path(os.environ.get('MARKET_STATE_FILE', 'crypto_market_state.json'))
VOLATILITY_ALERT_THRESHOLD = float(os.environ.get('VOLATILITY_ALERT_THRESHOLD', '5'))
VOLATILITY_ALERT_COINS = ["bitcoin", "ethereum"]
volatility_alert_sent = {}
MARKET_STATE = {"daily_reports": {}}

TZ_POLAND = ZoneInfo("Europe/Warsaw")


def load_market_state():
    global MARKET_STATE
    try:
        if MARKET_STATE_FILE.exists():
            MARKET_STATE = json.loads(MARKET_STATE_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        print(f"Blad ladowania stanu market bota: {e}")


def save_market_state():
    try:
        MARKET_STATE_FILE.write_text(json.dumps(MARKET_STATE, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        print(f"Blad zapisu stanu market bota: {e}")


def cleanup_market_state():
    cutoff = (datetime.datetime.now(TZ_POLAND) - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
    daily_reports = MARKET_STATE.setdefault("daily_reports", {})
    MARKET_STATE["daily_reports"] = {
        key: value for key, value in daily_reports.items()
        if key[:10] >= cutoff
    }


def get_due_daily_report(now):
    if now.hour > DAILY_REPORT_CATCHUP_UNTIL_HOUR:
        return None, None

    daily_reports = MARKET_STATE.setdefault("daily_reports", {})
    for report_hour in sorted(DAILY_REPORT_HOURS):
        if now.hour < report_hour:
            continue
        key = f"{now.strftime('%Y-%m-%d')}-{report_hour:02d}"
        if not daily_reports.get(key):
            return report_hour, key
    return None, None

# --- Konfiguracja Gemini (POPRAWIONA) ---
gemini_client = None
gemini_disabled_reason = None
gemini_model_name = os.environ.get('GEMINI_MODEL_NAME', 'gemini-2.5-flash').strip() # Domyślny model dla ANALIZ
market_report_cache = {"timestamp": None, "text": None}
MARKET_REPORT_CACHE_SECONDS = int(os.environ.get('MARKET_REPORT_CACHE_SECONDS', '1800'))
gemini_safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]
# Konfiguracja bezpieczeństwa dla generowania treści
gemini_generation_config = types.GenerateContentConfig(safety_settings=gemini_safety_settings)


# --- POCZĄTEK BLOKU: INTERAKTYWNY KALKULATOR MM ---

class KalkulatorMMModal(discord.ui.Modal, title="Kalkulator Wielkości Pozycji"):
    """
    Definicja interaktywnego okna (Modala) do obliczania
    wielkości pozycji (Money Management).
    """

    # --- Pola formularza, które zobaczy użytkownik ---

    balance = discord.ui.TextInput(
        label="Całkowite Saldo Konta (np. 10000)",
        placeholder="Wpisz swoje całkowite saldo w USD (tylko liczba)",
        required=True,
        style=discord.TextStyle.short
    )

    risk_percent = discord.ui.TextInput(
        label="Ryzyko na transakcję w % (np. 1 lub 2)",
        placeholder="Tylko liczba, np. '1' dla 1%",
        required=True,
        max_length=5 # Max 5 znaków, np. "1.25"
    )

    entry_price = discord.ui.TextInput(
        label="Cena Wejścia (np. 60000)",
        placeholder="Cena, po której planujesz kupić / shortować",
        required=True
    )

    stop_loss = discord.ui.TextInput(
        label="Cena Stop Loss (np. 59000)",
        placeholder="Cena, po której zamykasz stratę",
        required=True
    )

    # --- Logika po wysłaniu formularza ---

    async def on_submit(self, interaction: discord.Interaction):
        """
        Ta funkcja uruchamia się, gdy użytkownik kliknie "Wyślij" w formularzu.
        """
        try:
            # 1. Pobieramy wartości z formularza i konwertujemy na liczby (float)
            # Używamy .replace(',', '.'), aby akceptować zarówno kropki, jak i przecinki
            balance_val = float(self.balance.value.replace(',', '.'))
            risk_val = float(self.risk_percent.value.replace(',', '.'))
            entry_val = float(self.entry_price.value.replace(',', '.'))
            stop_val = float(self.stop_loss.value.replace(',', '.'))

            # 2. Walidacja danych
            if balance_val <= 0 or risk_val <= 0 or entry_val <= 0 or stop_val <= 0:
                raise ValueError("Wszystkie wartości muszą być liczbami dodatnimi.")
            
            if entry_val == stop_val:
                raise ValueError("Cena wejścia i Stop Loss nie mogą być takie same.")

            # 3. Rozpoznanie typu pozycji (Long vs Short)
            is_long = entry_val > stop_val
            
            if is_long:
                # Pozycja DŁUGA (LONG)
                risk_per_unit = entry_val - stop_val
                position_type = "Long (Kupno)"
            else:
                # Pozycja KRÓTKA (SHORT)
                risk_per_unit = stop_val - entry_val
                position_type = "Short (Sprzedaż)"

            # 4. Główne kalkulacje
            amount_to_risk = balance_val * (risk_val / 100.0)
            position_size = amount_to_risk / risk_per_unit
            position_value_usd = position_size * entry_val

            # 5. Tworzenie ładnej odpowiedzi (Embed)
            embed = discord.Embed(
                title="✅ Wynik Kalkulatora Money Management",
                color=discord.Color.green()
            )
            embed.add_field(name="Twoje Dane Wejściowe", value=(
                f"**Saldo:** `${balance_val:,.2f}`\n"
                f"**Ryzyko:** `{risk_val:.2f}%`\n"
                f"**Wejście:** `${entry_val:,.2f}`\n"
                f"**Stop Loss:** `${stop_val:,.2f}`"
            ), inline=True)
            
            embed.add_field(name="Zarządzanie Ryzykiem", value=(
                f"**Typ Pozycji:** `{position_type}`\n"
                f"**Kwota Ryzykowana:** `${amount_to_risk:,.2f}`\n"
                f"**Ryzyko na 1 jednostkę:** `${risk_per_unit:,.2f}`"
            ), inline=True)
            
            embed.add_field(name="Sugerowana Wielkość Pozycji", value=(
                f"**Wielkość pozycji (np. w BTC/ETH):**\n"
                f"`{position_size:.8f}` **jednostek**\n\n"
                f"**Wartość tej pozycji w USD:**\n"
                f"`${position_value_usd:,.2f}`"
            ), inline=False)
            
            embed.set_footer(text="Ta wiadomość jest widoczna tylko dla Ciebie.")

            # 6. Wysłanie odpowiedzi - `ephemeral=True` oznacza, że widzi ją tylko ten, co wywołał
            await interaction.response.send_message(embed=embed, ephemeral=True)

        except ValueError as e:
            # Obsługa błędu, jeśli ktoś wpisze "abc" zamiast "100"
            await interaction.response.send_message(
                f"BŁĄD! Wprowadziłeś niepoprawne dane. Upewnij się, że używasz tylko liczb (np. 10000 lub 1.5).\n*Szczegóły: {e}*",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"Wystąpił nieoczekiwany błąd: {e}", ephemeral=True)

# --- KONIEC BLOKU: INTERAKTYWNY KALKULATOR MM ---

if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY.strip())
        print(f"Konfiguracja Gemini OK. Model raportu: {gemini_model_name}.")
    except Exception as e:
        print(f"Błąd konfiguracji Gemini: {e}")
        gemini_client = None
else:
    print("OSTRZEŻENIE: Brak GEMINI_API_KEY. Analiza AI będzie niedostępna.")

# --- Inicjalizacja Bota ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Funkcja uruchamiająca Bota (w wątku) (POPRAWIONA) ---
def run_discord_bot_sync():
    """Uruchamia bota w synchronicznej funkcji, zarządzając własną pętlą asyncio."""
    if not BOT_TOKEN:
        print("Bot nie może wystartować, brak BOT_TOKEN.")
        return
    print("Uruchamianie bota Discord w osobnym wątku...")
    
    # Tworzymy nową pętlę zdarzeń dla tego wątku, aby uniknąć błędu 'atexit'
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Używamy bot.start() zamiast bot.run()
        loop.run_until_complete(bot.start(BOT_TOKEN))
    except Exception as e:
        print(f"Krytyczny błąd podczas uruchamiania bota Discord: {e}")
    finally:
        loop.run_until_complete(bot.close())
        loop.close()

# --- FUNKCJE POMOCNICZE, KOMENDY, TASKI ---

def get_fear_and_greed_image():
    timestamp = int(time.time())
    return f"https://alternative.me/crypto/fear-and-greed-index.png?v={timestamp}"


def get_fear_and_greed_status():
    try:
        response = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        response.raise_for_status()
        data = response.json().get("data", [{}])[0]
        value = data.get("value", "brak danych")
        classification = data.get("value_classification", "brak klasyfikacji")
        updated_at = data.get("timestamp")
        updated_text = ""
        if updated_at:
            updated_dt = datetime.datetime.fromtimestamp(int(updated_at), tz=TZ_POLAND)
            updated_text = f"\nAktualizacja: {updated_dt.strftime('%Y-%m-%d %H:%M')}"
        return f"Indeks: **{value}/100**\nKlasyfikacja: **{classification}**{updated_text}"
    except Exception as e:
        print(f"Blad pobierania Fear & Greed Index: {e}")
        return "Nie udało się pobrać aktualnej wartości indeksu."

def format_usd(value, decimals=2):
    if value is None:
        return "brak danych"
    return f"${value:,.{decimals}f}"

def format_pct(value):
    if value is None:
        return "brak danych"
    return f"{value:+.2f}%"

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0 
    
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period, len(deltas)):
        delta = deltas[i]
        gain = delta if delta > 0 else 0
        loss = -delta if delta < 0 else 0
        
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_loss == 0:
            rs = float('inf')
        else:
            rs = avg_gain / avg_loss
        
        rsi = 100.0 - (100.0 / (1.0 + rs))
        
    return rsi

def get_top_gainers(count=10):
    if not COINGECKO_API_KEY: return "Brak klucza API CoinGecko."
    headers = {'x-cg-demo-api-key': COINGECKO_API_KEY.strip()}
    stablecoin_symbols = {'usdt', 'usdc', 'dai', 'busd', 'ust', 'tusd'}

    try:
        params = {'vs_currency': 'usd', 'order': 'market_cap_desc', 'per_page': 100, 'page': 1}
        response = requests.get("https://api.coingecko.com/api/v3/coins/markets", params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        filtered_data = [coin for coin in data if coin['symbol'] not in stablecoin_symbols]
        sorted_gainers = sorted(filtered_data, key=lambda x: x.get('price_change_percentage_24h', 0) or 0, reverse=True)
        gainers_list = [f"**{c['name']} ({c['symbol'].upper()})**: `+{c.get('price_change_percentage_24h', 0):.2f}%`" for c in sorted_gainers[:count]]
        return "\n".join(gainers_list) if gainers_list else "Brak danych lub wszystkie monety odnotowały spadek."
    except Exception as e:
        print(f"Błąd połączenia lub przetwarzania CoinGecko: {e}")
        return "Błąd: problem z pobraniem danych."

def get_market_overview():
    if not COINGECKO_API_KEY:
        return "Brak klucza API CoinGecko."

    headers = {'x-cg-demo-api-key': COINGECKO_API_KEY.strip()}
    try:
        global_response = requests.get(
            "https://api.coingecko.com/api/v3/global",
            headers=headers,
            timeout=10
        )
        global_response.raise_for_status()
        global_data = global_response.json().get("data", {})

        markets_response = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": "bitcoin,ethereum,solana,binancecoin,ripple",
                "order": "market_cap_desc",
                "per_page": 5,
                "page": 1,
                "price_change_percentage": "24h,7d"
            },
            headers=headers,
            timeout=10
        )
        markets_response.raise_for_status()
        coins = markets_response.json()

        total_cap = global_data.get("total_market_cap", {}).get("usd")
        cap_change = global_data.get("market_cap_change_percentage_24h_usd")
        total_volume = global_data.get("total_volume", {}).get("usd")
        btc_dom = global_data.get("market_cap_percentage", {}).get("btc")
        eth_dom = global_data.get("market_cap_percentage", {}).get("eth")

        lines = []
        if total_cap is not None:
            lines.append(f"- Kapitalizacja rynku: ${total_cap:,.0f} ({cap_change:+.2f}% 24h)" if cap_change is not None else f"- Kapitalizacja rynku: ${total_cap:,.0f}")
        if total_volume is not None:
            lines.append(f"- Wolumen 24h: ${total_volume:,.0f}")
        if btc_dom is not None:
            eth_text = f", ETH {eth_dom:.2f}%" if eth_dom is not None else ""
            lines.append(f"- Dominacja: BTC {btc_dom:.2f}%{eth_text}")

        for coin in coins:
            name = coin.get("name", "Unknown")
            symbol = coin.get("symbol", "").upper()
            price = coin.get("current_price")
            change_24h = coin.get("price_change_percentage_24h")
            change_7d = coin.get("price_change_percentage_7d_in_currency")
            if price is None:
                continue
            line = f"- {name} ({symbol}): ${price:,.2f}"
            if change_24h is not None:
                line += f", {change_24h:+.2f}% 24h"
            if change_7d is not None:
                line += f", {change_7d:+.2f}% 7d"
            lines.append(line)

        return "\n".join(lines) if lines else "Brak danych rynkowych."
    except Exception as e:
        print(f"Błąd pobierania przeglądu rynku: {e}")
        return "Błąd: problem z pobraniem przeglądu rynku."

def get_fed_events():
    if not ALPHAVANTAGE_API_KEY: return "Brak klucza API AlphaVantage."
    try:
        url = f'https://www.alphavantage.co/query?function=ECONOMIC_CALENDAR&horizon=3month&apikey={ALPHAVANTAGE_API_KEY.strip()}'
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        csv_file = io.StringIO(response.text)
        reader = csv.DictReader(csv_file)
        today = date.today()
        next_14_days = today + timedelta(days=14)
        fed_events = []
        keywords = ["FOMC", "Fed", "Interest Rate", "Inflation Rate"]
        for row in reader:
            event_date_str = row.get('releaseDate')
            if not event_date_str: continue
            event_date = datetime.datetime.strptime(event_date_str, '%Y-%m-%d').date()
            if today <= event_date <= next_14_days:
                event_name = row.get('event', '')
                if any(k.lower() in event_name.lower() for k in keywords):
                    event_str = f"🗓️ **{event_date.strftime('%Y-%m-%d')}**: `{event_name}`"
                    if event_str not in fed_events:
                        fed_events.append(event_str)
        return "\n".join(fed_events) if fed_events else "Brak kluczowych wydarzeń FED w najbliższych 2 tygodniach."
    except Exception as e:
        return f"Błąd podczas pobierania wydarzeń FED: {e}"

# --- NOWA FUNKCJA ANALIZY DLA POJEDYNCZEJ KRYPTO ---
def get_single_coin_analysis(coin_id: str):
    """Pobiera i analizuje dane dla JEDNEJ krypto (synchronicznie)"""
    if not COINGECKO_API_KEY: 
        return "Brak klucza API CoinGecko.", None
    
    try:
        headers = {'x-cg-demo-api-key': COINGECKO_API_KEY.strip()}
        
        # Pobieramy dane z ostatnich 15 dni do obliczeń
        chart_url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency=usd&days=15"
        response_chart = requests.get(chart_url, headers=headers, timeout=10)
        response_chart.raise_for_status() # Zwróci błąd 404 jeśli ID jest złe
        
        prices = [p[1] for p in response_chart.json()['prices']]
        if not prices:
             return f"Brak danych o cenach dla `{coin_id}`.", None

        # Obliczenia
        rsi = calculate_rsi(prices)
        rsi_interpretation = "Neutralnie 😐"
        if rsi > 70: rsi_interpretation = "Rynek wykupiony 📈"
        if rsi < 30: rsi_interpretation = "Rynek wyprzedany 📉"
        
        points_for_7d = min(len(prices), 7 * 24)
        prices_7_days = prices[-points_for_7d:]
        support = min(prices_7_days)
        resistance = max(prices_7_days)
        current_price = prices[-1]
        
        analysis_text = (
            f"- **RSI (14D):** `{rsi:.2f}` ({rsi_interpretation})\n"
            f"- **Wsparcie (7D):** `${support:,.2f}`\n"
            f"- **Opór (7D):** `${resistance:,.2f}`"
        )
        
        return analysis_text, current_price # Zwracamy tekst i aktualną cenę
        
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return f"Nie znaleziono kryptowaluty o ID: `{coin_id}`. Użyj pełnego ID (np. 'bitcoin', 'ethereum', 'solana').", None
        else:
            return f"Błąd API CoinGecko: {e}", None
    except Exception as e:
        print(f"Błąd analizy dla {coin_id}: {e}")
        return f"Błąd analizy dla {coin_id}.", None
# --- KONIEC NOWEJ FUNKCJI ---

def get_coin_technical_snapshot(coin_id: str):
    if not COINGECKO_API_KEY:
        raise ValueError("Brak klucza API CoinGecko.")

    headers = {'x-cg-demo-api-key': COINGECKO_API_KEY.strip()}
    markets_response = requests.get(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={
            "vs_currency": "usd",
            "ids": coin_id,
            "price_change_percentage": "24h,7d"
        },
        headers=headers,
        timeout=10
    )
    markets_response.raise_for_status()
    markets = markets_response.json()
    if not markets:
        raise ValueError(f"Nie znaleziono kryptowaluty `{coin_id}`.")

    chart_response = requests.get(
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
        params={"vs_currency": "usd", "days": 30},
        headers=headers,
        timeout=10
    )
    chart_response.raise_for_status()
    prices = [p[1] for p in chart_response.json().get("prices", [])]
    if len(prices) < 15:
        raise ValueError(f"Za mało danych cenowych dla `{coin_id}`.")

    market = markets[0]
    current_price = market.get("current_price") or prices[-1]
    points_7d = min(len(prices), 7 * 24)
    prices_7d = prices[-points_7d:]
    rsi = calculate_rsi(prices)
    support = min(prices_7d)
    resistance = max(prices_7d)
    distance_to_support = ((current_price - support) / current_price) * 100 if current_price else 0
    distance_to_resistance = ((resistance - current_price) / current_price) * 100 if current_price else 0

    if rsi >= 70:
        condition = "wykupienie"
    elif rsi <= 30:
        condition = "wyprzedanie"
    else:
        condition = "neutralnie"

    return {
        "id": coin_id,
        "name": market.get("name", coin_id.capitalize()),
        "symbol": market.get("symbol", "").upper(),
        "price": current_price,
        "change_24h": market.get("price_change_percentage_24h"),
        "change_7d": market.get("price_change_percentage_7d_in_currency"),
        "rsi": rsi,
        "condition": condition,
        "support": support,
        "resistance": resistance,
        "distance_to_support": distance_to_support,
        "distance_to_resistance": distance_to_resistance,
        "volume": market.get("total_volume"),
        "market_cap": market.get("market_cap")
    }

def format_coin_technical_report(snapshot):
    scenario = "Scenariusz bazowy: obserwuj reakcję ceny między wsparciem i oporem."
    invalidation = "Unieważnienie: wybicie poza wskazany zakres 7D przy rosnącym wolumenie."
    if snapshot["rsi"] >= 70:
        scenario = "Scenariusz bazowy: momentum jest mocne, ale rynek może być podatny na realizację zysków."
        invalidation = "Unieważnienie: utrzymanie ceny powyżej oporu i schłodzenie RSI bez spadku ceny."
    elif snapshot["rsi"] <= 30:
        scenario = "Scenariusz bazowy: rynek jest technicznie wyprzedany, możliwe odbicie korekcyjne."
        invalidation = "Unieważnienie: utrata wsparcia 7D i dalszy wzrost wolumenu sprzedaży."

    return (
        f"**Cena:** `{format_usd(snapshot['price'])}`\n"
        f"**Zmiana:** `{format_pct(snapshot['change_24h'])} 24h`, `{format_pct(snapshot['change_7d'])} 7d`\n"
        f"**RSI 14D:** `{snapshot['rsi']:.2f}` ({snapshot['condition']})\n"
        f"**Wsparcie 7D:** `{format_usd(snapshot['support'])}` ({snapshot['distance_to_support']:.2f}% niżej)\n"
        f"**Opór 7D:** `{format_usd(snapshot['resistance'])}` ({snapshot['distance_to_resistance']:.2f}% wyżej)\n"
        f"**Wolumen 24h:** `{format_usd(snapshot['volume'], 0)}`\n\n"
        f"{scenario}\n{invalidation}\n"
        "To nie jest porada inwestycyjna."
    )

def get_core_technical_overview():
    lines = []
    for coin_id in ("bitcoin", "ethereum"):
        try:
            snapshot = get_coin_technical_snapshot(coin_id)
            lines.append(
                f"- **{snapshot['symbol']}**: `{format_usd(snapshot['price'])}`, "
                f"{format_pct(snapshot['change_24h'])} 24h, RSI `{snapshot['rsi']:.1f}`, "
                f"wsparcie `{format_usd(snapshot['support'])}`, opór `{format_usd(snapshot['resistance'])}`"
            )
        except Exception as e:
            lines.append(f"- **{coin_id.upper()}**: błąd pobierania danych ({e})")
    return "\n".join(lines)

def get_volatility_alerts():
    alerts = []
    today_key = date.today().isoformat()
    for coin_id in VOLATILITY_ALERT_COINS:
        try:
            snapshot = get_coin_technical_snapshot(coin_id)
        except Exception as e:
            print(f"Błąd alertu zmienności dla {coin_id}: {e}")
            continue

        change = snapshot.get("change_24h")
        if change is None or abs(change) < VOLATILITY_ALERT_THRESHOLD:
            continue

        alert_key = f"{today_key}:{coin_id}:{'up' if change > 0 else 'down'}"
        if volatility_alert_sent.get(alert_key):
            continue

        direction = "wzrost" if change > 0 else "spadek"
        alerts.append(
            f"**{snapshot['name']} ({snapshot['symbol']})**: {direction} `{format_pct(change)}` w 24h. "
            f"Cena `{format_usd(snapshot['price'])}`, RSI `{snapshot['rsi']:.1f}`, "
            f"wsparcie `{format_usd(snapshot['support'])}`, opór `{format_usd(snapshot['resistance'])}`."
        )
        volatility_alert_sent[alert_key] = True
    return alerts


# --- NOWA ZAKTUALIZOWANA FUNKCJA POMOCNICZA DLA GEMINI ---
def _generate_content_with_fallback(prompt: str, model_name: str):
    """
    Uruchamia Gemini z logiką ponawiania prób i przełączania awaryjnego.
    Przyjmuje model_name, aby wiedzieć, który model ma być podstawowym.
    """
    global gemini_client, gemini_disabled_reason

    if not gemini_client:
        raise Exception("Klient Gemini nie jest skonfigurowany.")

    primary_model = model_name
    fallback_model = None
    max_retries = 5 # Liczba prób dla modelu podstawowego

    # Ustaw model awaryjny tylko jeśli podstawowy to 'pro'
    if primary_model == 'gemini-2.5-pro':
        fallback_model = 'gemini-2.5-flash'

    # --- Próba 1: Model Podstawowy (Pro lub Flash) z ponowieniami ---
    for attempt in range(max_retries):
        try:
            response = gemini_client.models.generate_content(
                model=primary_model,
                contents=prompt,
                config=gemini_generation_config
            )
            print(f"Model '{primary_model}' zadziałał za {attempt + 1} próbą.")
            return response
        except Exception as e:
            error_str = str(e)
            if "location is not supported" in error_str.lower() or "FAILED_PRECONDITION" in error_str:
                gemini_disabled_reason = "Gemini API jest niedostępne z lokalizacji/regionu tej usługi."
                gemini_client = None
                print(f"Wyłączam Gemini dla tego procesu: {gemini_disabled_reason}")
                raise e
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota" in error_str.lower():
                gemini_disabled_reason = "Limit Gemini został wyczerpany dla tego projektu/modelu."
                gemini_client = None
                print(f"Wyłączam Gemini dla tego procesu: {gemini_disabled_reason}")
                raise e
            # Sprawdzamy, czy to błąd przeciążenia (503) LUB limitu (429)
            if "503 UNAVAILABLE" in error_str or "overloaded" in error_str:
                print(f" próba {attempt + 1}/{max_retries} na '{primary_model}' nie powiodła się (przeciążenie). Próbuję ponownie...")
                time.sleep(1 + attempt)
                continue # Przejdź do kolejnej próby
            else:
                # Jeśli to inny błąd (np. 400 Bad Request), przerwij od razu
                print(f"Krytyczny błąd Gemini (nie do ponowienia): {e}")
                raise e # Rzuć błędem, aby zewnętrzna funkcja go złapała

    # --- Próba 2: Model Awaryjny (tylko jeśli podstawowy to 'pro') ---
    if fallback_model:
        print(f"Wszystkie {max_retries} prób na '{primary_model}' nie powiodły się. Przełączam na model awaryjny '{fallback_model}'...")
        try:
            response = gemini_client.models.generate_content(
                model=fallback_model,
                contents=prompt,
                config=gemini_generation_config
            )
            print(f"Model awaryjny '{fallback_model}' zadziałał.")
            return response
        except Exception as e:
            print(f"Model awaryjny '{fallback_model}' również zawiódł.")
            raise e # Rzuć ostatecznym błędem
    else:
        # Jeśli nie było modelu awaryjnego (bo podstawowy to flash), rzuć błędem
        raise Exception(f"Wszystkie {max_retries} prób na '{primary_model}' nie powiodły się. Brak modelu awaryjnego.")
# --- KONIEC NOWEJ FUNKCJI ---


def get_realtime_market_snapshot():
    snapshot = {
        "fear_greed": "Brak danych",
        "market_overview": "Brak danych",
        "technical_overview": "Brak danych",
        "top_gainers": "Brak danych",
        "fed_events": "Brak danych"
    }
    try:
        response = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        response.raise_for_status()
        data = response.json()['data'][0]
        snapshot['fear_greed'] = f"{data['value']} ({data['value_classification']})"
    except Exception as e:
        print(f"Błąd pobierania Fear & Greed: {e}")

    snapshot['market_overview'] = get_market_overview()
    snapshot['technical_overview'] = get_core_technical_overview()
    snapshot['top_gainers'] = get_top_gainers(3)
    snapshot['fed_events'] = get_fed_events()
    return snapshot

def get_market_risk_score(snapshot):
    score = 50
    reasons = []

    fng_match = re.search(r"\d+", snapshot.get("fear_greed", ""))
    if fng_match:
        fng_value = int(fng_match.group(0))
        if fng_value >= 75:
            score += 18
            reasons.append("skrajna chciwość w sentymencie")
        elif fng_value >= 60:
            score += 8
            reasons.append("podwyższony apetyt na ryzyko")
        elif fng_value <= 25:
            score += 15
            reasons.append("skrajny strach i podwyższona zmienność")
        elif fng_value <= 40:
            score += 6
            reasons.append("ostrożny sentyment")
        else:
            reasons.append("neutralny sentyment")

    overview = snapshot.get("market_overview", "")
    cap_match = re.search(r"\(([+-]?\d+(?:\.\d+)?)% 24h\)", overview)
    if cap_match:
        cap_change = float(cap_match.group(1))
        if cap_change <= -3:
            score += 18
            reasons.append("mocny spadek kapitalizacji rynku")
        elif cap_change <= -1:
            score += 8
            reasons.append("słabsza kapitalizacja rynku")
        elif cap_change >= 3:
            score += 10
            reasons.append("dynamiczny wzrost kapitalizacji")
        elif cap_change >= 1:
            score -= 3
            reasons.append("umiarkowanie pozytywny przepływ kapitału")

    score = max(0, min(100, score))
    if score >= 70:
        label = "Risk-off / wysoka ostrożność"
    elif score >= 55:
        label = "Podwyższone ryzyko"
    elif score >= 40:
        label = "Neutralnie"
    else:
        label = "Risk-on / umiarkowany apetyt na ryzyko"

    return {
        "score": score,
        "label": label,
        "reasons": "; ".join(reasons[:4]) if reasons else "brak wystarczających danych"
    }


def build_rule_based_market_report(snapshot, risk):
    return (
        f"**1. Sentyment i ryzyko:** Risk score wynosi {risk['score']}/100: {risk['label']}. "
        f"Główne czynniki: {risk['reasons']}.\n"
        f"**2. BTC / ETH / rynek:** {snapshot.get('market_overview', 'Brak danych rynkowych.')}\n"
        f"**3. Poziomy techniczne:**\n{snapshot.get('technical_overview', 'Brak danych technicznych.')}\n"
        f"**4. Altcoiny i momentum:** Największe wzrosty 24h:\n{snapshot.get('top_gainers', 'Brak danych.')}\n"
        f"**5. Makro / wydarzenia:** {snapshot.get('fed_events', 'Brak danych makro.')}\n"
        f"**6. Scenariusz i ryzyka:** Bazowo obserwuj reakcję ceny przy poziomach wsparcia/oporu. Główne ryzyka: zmienność po danych makro, szybka rotacja kapitału w altcoinach oraz skrajne odczyty sentymentu.\n"
        "To nie jest porada inwestycyjna."
    )


def get_ai_error_message(error):
    error_text = str(error)
    lowered = error_text.lower()
    if "location is not supported" in lowered or "failed_precondition" in lowered:
        return "Gemini API jest niedostępne z lokalizacji/regionu tej usługi. Bot użył raportu regułowego na podstawie danych rynkowych."
    if "api key" in lowered or "permission" in lowered or "unauthenticated" in lowered or "403" in error_text:
        return "Nie udało się wygenerować briefingu AI. Sprawdź `GEMINI_API_KEY` oraz dostęp tego klucza do wybranego modelu."
    if "429" in error_text or "resource_exhausted" in lowered or "quota" in lowered:
        return "Nie udało się wygenerować briefingu AI. Limit Gemini został chwilowo wyczerpany."
    if "not found" in lowered or "404" in error_text or "model" in lowered:
        return "Nie udało się wygenerować briefingu AI. Wybrany model Gemini może być niedostępny dla tego klucza. Ustaw `GEMINI_MODEL_NAME=gemini-2.5-flash` i wdroż ponownie."
    if "503" in error_text or "overloaded" in lowered or "unavailable" in lowered:
        return "Nie udało się wygenerować briefingu AI. Model Gemini jest chwilowo przeciążony."
    return "Nie udało się wygenerować profesjonalnego raportu z powodu błędu Gemini. Szczegóły są w logach Render."


def get_ai_report_analysis():
    now_ts = time.time()
    cached_text = market_report_cache.get("text")
    cached_ts = market_report_cache.get("timestamp")
    if cached_text and cached_ts and now_ts - cached_ts < MARKET_REPORT_CACHE_SECONDS:
        print("Używam cache raportu rynkowego.")
        return cached_text

    market_data = get_realtime_market_snapshot()
    risk = get_market_risk_score(market_data)

    if not gemini_client:
        reason = gemini_disabled_reason or "brak aktywnego klienta Gemini"
        print(f"Gemini niedostępne ({reason}). Generuję raport regułowy.")
        report = build_rule_based_market_report(market_data, risk)
        market_report_cache["timestamp"] = now_ts
        market_report_cache["text"] = report
        return report

    print(f"Pobieranie danych do raportu Pro Desk Morning Briefing (Model: {gemini_model_name})...")
    current_date = datetime.datetime.now(TZ_POLAND).strftime("%Y-%m-%d %H:%M")

    try:
        prompt = (
            "Jesteś profesjonalnym analitykiem rynku kryptowalut, makro i ryzyka. "
            "Piszesz codzienny poranny briefing dla społeczności inwestorów na Discordzie. "
            "Analizuj TYLKO dostarczone dane. Nie wymyślaj cen, wydarzeń ani rekomendacji. "
            "Nie dawaj porady inwestycyjnej.\n\n"
            f"--- DANE ({current_date}, Europe/Warsaw) ---\n"
            f"Fear & Greed Index: {market_data['fear_greed']}\n"
            f"Risk score systemowy: {risk['score']}/100 - {risk['label']} ({risk['reasons']})\n"
            f"Przeglad rynku:\n{market_data['market_overview']}\n"
            f"Poziomy techniczne BTC/ETH:\n{market_data['technical_overview']}\n"
            f"Największe wzrosty 24h:\n{market_data['top_gainers']}\n"
            f"Makro/FED:\n{market_data['fed_events']}\n"
            "--- KONIEC DANYCH ---\n\n"
            "Napisz raport po polsku w stylu profesjonalnego biurka analitycznego. "
            "Maksymalnie 950 znaków, bez lania wody, bez emoji, bez markdownowych tabel. "
            "Użyj dokładnie tych sekcji:\n"
            "**1. Sentyment i ryzyko:** 1-2 zdania.\n"
            "**2. BTC / ETH / rynek:** 2-3 zdania o kierunku, dominacji, kapitalizacji i głównych aktywach.\n"
            "**3. Poziomy techniczne:** 1-2 zdania o RSI, wsparciu i oporze BTC/ETH.\n"
            "**4. Altcoiny i momentum:** 1-2 zdania o największych wzrostach i rotacji kapitału.\n"
            "**5. Makro / wydarzenia:** 1 zdanie, jeśli dane są dostępne; jeśli nie, napisz czego brakuje.\n"
            "**6. Scenariusz i ryzyka:** bazowy scenariusz, warunek unieważnienia i 2 najważniejsze ryzyka.\n"
            "Zakończ krótko: 'To nie jest porada inwestycyjna.'"
        )

        response = _generate_content_with_fallback(prompt, model_name=gemini_model_name)
        report = response.text.strip()
        market_report_cache["timestamp"] = now_ts
        market_report_cache["text"] = report
        return report
    except Exception as e:
        print(f"Błąd podczas generowania raportu Pro Desk Morning Briefing: {e}")
        fallback = build_rule_based_market_report(market_data, risk)
        print(f"Fallback raportu regułowego: {get_ai_error_message(e)}")
        report = fallback
        market_report_cache["timestamp"] = now_ts
        market_report_cache["text"] = report
        return report


def _fit_embed_value(value, limit=1024):
    value = str(value or "Brak danych")
    if len(value) <= limit:
        return value
    return value[:limit - 3].rstrip() + "..."


def get_channel_access_error(channel):
    if not channel:
        return f"Nie znaleziono kanału CHANNEL_ID={CHANNEL_ID}."
    if not getattr(channel, "guild", None):
        return None
    if not bot.user:
        return "Bot nie jest jeszcze zalogowany."

    member = channel.guild.get_member(bot.user.id)
    if not member:
        return "Bot nie jest członkiem serwera, na którym znajduje się kanał raportu."

    permissions = channel.permissions_for(member)
    missing = []
    if not permissions.view_channel:
        missing.append("View Channel")
    if not permissions.send_messages:
        missing.append("Send Messages")
    if not permissions.embed_links:
        missing.append("Embed Links")

    if missing:
        return "Bot nie ma wymaganych uprawnień na kanale raportu: " + ", ".join(missing) + "."
    return None


async def send_market_report(channel_or_ctx,
                             title: str,
                             color: discord.Color,
                             include_fg: bool = False,
                             include_gainers: bool = False,
                             include_fed: bool = False,
                             include_ai_analysis: bool = False):
    if isinstance(channel_or_ctx, discord.Interaction):
        followup_send = channel_or_ctx.followup.send
    else:
        followup_send = channel_or_ctx.send

    main_embed = discord.Embed(
        title=title,
        description="Poranny briefing rynku krypto: sentyment, momentum, makro i ryzyka.",
        color=color
    )
    main_embed.set_footer(text=f"Dane automatyczne | Europe/Warsaw | {datetime.datetime.now(TZ_POLAND).strftime('%Y-%m-%d %H:%M')}")

    if include_fg:
        fg_text = await asyncio.to_thread(get_fear_and_greed_status)
        main_embed.add_field(name="Fear & Greed Index", value=_fit_embed_value(fg_text, 1024), inline=False)

    if include_ai_analysis:
        ai_summary = await asyncio.to_thread(get_ai_report_analysis)
        main_embed.description = _fit_embed_value(ai_summary, 4096)

    if include_gainers and not include_ai_analysis:
        gainers_text = await asyncio.to_thread(get_top_gainers, 10)
        main_embed.add_field(name="Momentum: największe wzrosty 24h", value=_fit_embed_value(gainers_text, 1024), inline=False)

    if include_fed and not include_ai_analysis:
        fed_text = await asyncio.to_thread(get_fed_events)
        main_embed.add_field(name="Makro: FED / wydarzenia 14 dni", value=_fit_embed_value(fed_text, 1024), inline=False)

    await followup_send(embed=main_embed)


# --- Komendy ukosnikowe ---

@bot.tree.command(name="raport", description="Generuje profesjonalny briefing rynku krypto.")
async def slash_report(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    await send_market_report(interaction, title="Profesjonalny briefing rynku krypto", color=discord.Color.gold(), include_fg=True, include_gainers=True, include_fed=True, include_ai_analysis=True)


@bot.tree.command(name="raport_status", description="Pokazuje diagnostykę automatycznego raportu.")
async def slash_report_status(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    load_market_state()
    channel = bot.get_channel(CHANNEL_ID)
    access_error = get_channel_access_error(channel)
    current_channel_error = get_channel_access_error(interaction.channel)
    now = datetime.datetime.now(TZ_POLAND)
    report_hour, due_key = get_due_daily_report(now)
    daily_reports = MARKET_STATE.get("daily_reports", {})
    latest_key = max(daily_reports.keys()) if daily_reports else "brak"
    latest_value = daily_reports.get(latest_key, {})
    message = (
        f"CHANNEL_ID: `{CHANNEL_ID}`\n"
        f"DAILY_REPORT_HOURS: `{','.join(str(h) for h in DAILY_REPORT_HOURS)}`\n"
        f"Catch-up do godziny: `{DAILY_REPORT_CATCHUP_UNTIL_HOUR}`\n"
        f"Dostęp do CHANNEL_ID: `{access_error or 'OK'}`\n"
        f"Dostęp do tego kanału: `{current_channel_error or 'OK'}`\n"
        f"Należny raport teraz: `{due_key or 'brak'}`\n"
        f"Ostatni zapisany raport: `{latest_key}`\n"
        f"Ostatni wysłany o: `{latest_value.get('sent_at', 'brak')}`"
    )
    await interaction.followup.send(message[:1900], ephemeral=True)

@bot.tree.command(name="market", description="Pokazuje profesjonalny snapshot rynku krypto.")
async def slash_market(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    snapshot = await asyncio.to_thread(get_realtime_market_snapshot)
    risk = get_market_risk_score(snapshot)

    embed = discord.Embed(
        title="Snapshot rynku krypto",
        description=f"Risk score: **{risk['score']}/100** - **{risk['label']}**\n{risk['reasons']}",
        color=discord.Color.from_rgb(70, 130, 180)
    )
    embed.add_field(name="Rynek", value=_fit_embed_value(snapshot["market_overview"], 1024), inline=False)
    embed.add_field(name="Poziomy BTC/ETH", value=_fit_embed_value(snapshot["technical_overview"], 1024), inline=False)
    embed.add_field(name="Momentum 24h", value=_fit_embed_value(snapshot["top_gainers"], 1024), inline=False)
    embed.add_field(name="Makro/FED", value=_fit_embed_value(snapshot["fed_events"], 1024), inline=False)
    embed.set_footer(text=f"Europe/Warsaw | {datetime.datetime.now(TZ_POLAND).strftime('%Y-%m-%d %H:%M')}")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="risk", description="Pokazuje dashboard ryzyka rynku krypto.")
async def slash_risk(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    snapshot = await asyncio.to_thread(get_realtime_market_snapshot)
    risk = get_market_risk_score(snapshot)
    embed = discord.Embed(
        title="Dashboard ryzyka rynku krypto",
        description=f"**{risk['score']}/100** - **{risk['label']}**\n{risk['reasons']}",
        color=discord.Color.red() if risk["score"] >= 70 else discord.Color.orange() if risk["score"] >= 55 else discord.Color.green()
    )
    embed.add_field(name="Sentyment", value=snapshot["fear_greed"], inline=True)
    embed.add_field(name="Rynek", value=_fit_embed_value(snapshot["market_overview"], 1024), inline=False)
    embed.add_field(name="BTC/ETH technicznie", value=_fit_embed_value(snapshot["technical_overview"], 1024), inline=False)
    embed.set_footer(text=f"Europe/Warsaw | {datetime.datetime.now(TZ_POLAND).strftime('%Y-%m-%d %H:%M')}")
    await interaction.followup.send(embed=embed)

async def send_coin_report(interaction: discord.Interaction, coin_id: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        snapshot = await asyncio.to_thread(get_coin_technical_snapshot, coin_id)
        embed = discord.Embed(
            title=f"{snapshot['name']} ({snapshot['symbol']}) - raport techniczny",
            description=format_coin_technical_report(snapshot),
            color=discord.Color.orange()
        )
    except Exception as e:
        embed = discord.Embed(
            title=f"Błąd raportu dla {coin_id}",
            description=str(e),
            color=discord.Color.red()
        )
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="btc", description="Pokazuje szybki raport techniczny BTC.")
async def slash_btc(interaction: discord.Interaction):
    await send_coin_report(interaction, "bitcoin")

@bot.tree.command(name="eth", description="Pokazuje szybki raport techniczny ETH.")
async def slash_eth(interaction: discord.Interaction):
    await send_coin_report(interaction, "ethereum")

@bot.tree.command(name="fg", description="Wyświetla aktualny indeks Fear & Greed.")
async def slash_fg(interaction: discord.Interaction):
    status = await asyncio.to_thread(get_fear_and_greed_status)
    embed = discord.Embed(title="Fear & Greed Index", description=status, color=discord.Color.gold())
    embed.set_image(url=get_fear_and_greed_image())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="gainers", description="Pokazuje 10 kryptowalut z największym wzrostem w ciągu 24h.")
async def slash_gainers(interaction: discord.Interaction):
    description_text = get_top_gainers(10)
    embed = discord.Embed(title="Największe wzrosty 24h", description=description_text, color=discord.Color.green())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- USUNIĘTO KOMENDĘ /heatmap ---

@bot.tree.command(name="fed", description="Pokazuje nadchodzące kluczowe wydarzenia FED (14 dni).")
async def slash_fed(interaction: discord.Interaction):
    description_text = get_fed_events()
    embed = discord.Embed(title="Nadchodzące wydarzenia FED (14 dni)", description=description_text, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- ZMODYFIKOWANA KOMENDA /analiza ---
@bot.tree.command(name="analiza", description="Wyświetla uproszczoną analizę techniczną dla wybranej krypto.")
@discord.app_commands.describe(coin="ID kryptowaluty (np. 'bitcoin', 'ethereum', 'solana')")
async def slash_analysis(interaction: discord.Interaction, coin: str):
    await interaction.response.defer(ephemeral=True) # Używamy defer, bo robimy API call
    
    coin_id = coin.lower().strip()
    
    # Wywołujemy nową funkcję w osobnym wątku, aby nie blokować bota
    analysis_text, current_price = await asyncio.to_thread(get_single_coin_analysis, coin_id)
    
    if current_price:
        # Sukces
        embed = discord.Embed(
            title=f"Analiza {coin_id.capitalize()} (${current_price:,.2f})", 
            description=analysis_text, 
            color=discord.Color.orange()
        )
    else:
        # Błąd (obsłużony w funkcji pomocniczej)
        embed = discord.Embed(
            title=f"Błąd Analizy dla {coin_id.capitalize()}", 
            description=analysis_text, # Tutaj będzie wiadomość błędu
            color=discord.Color.red()
        )
        
    await interaction.followup.send(embed=embed) # Odpowiedź jest już efemeryczna
# --- KONIEC ZMIAN W /analiza ---


@bot.tree.command(name="kalkulator", description="Otwiera interaktywny kalkulator Money Management (wielkość pozycji).")
async def slash_kalkulator(interaction: discord.Interaction):
    """
    Wysyła do użytkownika interaktywny modal (okno)
    do wypełnienia danych kalkulatora.
    """
    # Ta komenda po prostu tworzy instancję naszego Modala i go wysyła
    # Modal sam w sobie obsługuje ephemeral=True
    await interaction.response.send_modal(KalkulatorMMModal())

@bot.tree.command(name="analiza_ai", description="Generuje szczegółową analizę rynkową AI na żądanie.")
async def slash_analiza_ai(interaction: discord.Interaction):
    # Dajemy znać Discordowi, że "myślimy", bo Gemini potrzebuje czasu
    await interaction.response.defer(thinking=True, ephemeral=True) # <-- ZMIANA: ephemeral=True
    
    # Wywołujemy naszą nową funkcję, aby pobrała embed
    analysis_embed = await get_detailed_ai_analysis_embed() # Ta funkcja teraz używa nowej logiki
    
    # Wysyłamy wynik jako followup
    await interaction.followup.send(embed=analysis_embed)


# --- Zdarzenia startowe i synchronizacja ---

@bot.event
async def on_ready():
    print(f'Zalogowano jako {bot.user}')
    try:
        load_market_state()
        # Sprawdzanie, czy taski już działają, aby uniknąć restartu
        if not daily_report_loop.is_running(): daily_report_loop.start()
        if not volatility_alert_loop.is_running(): volatility_alert_loop.start()
        
        # POPRAWKA: Usunięto wywołanie fin_watch_forwarder (z Twojego kodu)
        
        synced = await bot.tree.sync()
        print(f"Zsynchronizowano {len(synced)} komend(y) ukosnikowych.")
    except Exception as e:
        print(f"Błąd synchronizacji komend lub startu zadań: {e}")


# --- ZADANIA CYKLICZNE (tasks.loop) ---

@tasks.loop(minutes=1)
async def daily_report_loop():
    now = datetime.datetime.now(TZ_POLAND)
    cleanup_market_state()
    report_hour, key = get_due_daily_report(now)
    if key is None:
        return

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"Nie znaleziono kanału raportu CHANNEL_ID={CHANNEL_ID}.")
        return

    access_error = get_channel_access_error(channel)
    if access_error:
        print(f"Problem dostepu do kanalu raportu: {access_error}")
        return

    try:
        title = f"Profesjonalny Raport Krypto - {now.strftime('%d-%m-%Y')} {report_hour:02d}:00"
        await send_market_report(channel, title, discord.Color.gold(), include_fg=True, include_gainers=True, include_fed=True, include_ai_analysis=True)
        MARKET_STATE.setdefault("daily_reports", {})[key] = {
            "sent_at": now.isoformat(),
            "channel_id": CHANNEL_ID,
        }
        save_market_state()
        print(f"Raport automatyczny opublikowany dla klucza {key}.")
    except Exception as e:
        print(f"Błąd automatycznego raportu dla okna {key}: {e}")

@tasks.loop(minutes=30)
async def volatility_alert_loop():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    alerts = await asyncio.to_thread(get_volatility_alerts)
    for alert in alerts:
        embed = discord.Embed(
            title=f"Alert zmienności >= {VOLATILITY_ALERT_THRESHOLD:.1f}% 24h",
            description=alert,
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Europe/Warsaw | {datetime.datetime.now(TZ_POLAND).strftime('%Y-%m-%d %H:%M')}")
        await channel.send(embed=embed)


# --- Szczegółowa analiza na żądanie ---
async def get_detailed_ai_analysis_embed():
    analysis_text = await asyncio.to_thread(get_ai_report_analysis)
    embed = discord.Embed(
        title="Szczegółowa analiza rynku",
        description=_fit_embed_value(analysis_text, 4096),
        color=discord.Color.from_rgb(70, 130, 180)
    )
    embed.set_footer(text=f"Gemini AI | Europe/Warsaw | {datetime.datetime.now(TZ_POLAND).strftime('%Y-%m-%d %H:%M')}")
    return embed


# --- GŁÓWNE URUCHOMIENIE (Flask przez Gunicorn, Bot w wątku) ---
# Gunicorn uruchomi ten plik i będzie szukał obiektu 'app'.
# My wykorzystujemy ten fakt, aby uruchomić bota w osobnym wątku.

print("Inicjalizacja wątku bota Discord...")
bot_thread = Thread(target=run_discord_bot_sync)
bot_thread.start()

# Blok 'if __name__ == "__main__":' nie jest już potrzebny, 
# ponieważ Gunicorn importuje ten plik jako moduł, aby znaleźć 'app'.
