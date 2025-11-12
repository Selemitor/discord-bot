# Plik: bot.py
# OSTATECZNA WERSJA: Łączy Flask (dla Render) i Bota Discord w jednym procesie.

import os
import discord
from discord.ext import commands, tasks
import requests
import datetime
from datetime import date, timedelta
import csv
import io
import feedparser
import time
import numpy as np
from bs4 import BeautifulSoup
from collections import deque
import asyncio
from zoneinfo import ZoneInfo
from threading import Thread # <-- WAŻNE: Importujemy wątki
from flask import Flask # <-- WAŻNE: Importujemy Flask

# Wymaga instalacji: google-genai
import google.generativeai as genai

# --- Konfiguracja Flask (dla UptimeRobot) ---
# Serwer Flask MUSI być zdefiniowany PRZED logiką bota.
app = Flask(__name__)

@app.route('/')
def home():
    """Endpoint dla UptimeRobot, aby utrzymać bota przy życiu."""
    return "Bot jest aktywny!"

@app.route('/healthz')
def health_check():
    """Endpoint dla Render Health Check."""
    return "OK", 200

def run_flask_server():
    """Uruchamia serwer Flask na porcie podanym przez Render."""
    # Render automatycznie ustawi zmienną PORT
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

# --- Konfiguracja Bota Discord ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
COINGECKO_API_KEY = os.environ.get('COINGECKO_API_KEY')
ALPHAVANTAGE_API_KEY = os.environ.get('ALPHAVANTAGE_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

if not BOT_TOKEN:
    print("KRYTYCZNY BŁĄD: Nie znaleziono BOT_TOKEN. Aplikacja nie wystartuje.")
    exit()

# Upewnij się, ze te ID sa poprawne
CHANNEL_ID = 1429744335389458452
WATCHER_GURU_CHANNEL_ID = 1429719129702535248 
FIN_WATCH_CHANNEL_ID = 1429719129702535248

WATCHER_GURU_RSS_URL = "https://rss.app/feeds/bP1lIE9lQ9hTBuSk.xml"
FIN_WATCH_RSS_URL = "https://rss.app/feeds/R0DJZuoPNWe5yCMY.xml"

WATCHER_GURU_SENT_URLS = deque(maxlen=200)
FIN_WATCH_SENT_URLS = deque(maxlen=200)

TZ_POLAND = ZoneInfo("Europe/Warsaw")

# --- Konfiguracja Gemini ---
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
        gemini_model = genai.GenerativeModel(model_name='gemini-2.5-flash', safety_settings=safety_settings)
    except Exception as e:
        print(f"Błąd konfiguracji Gemini: {e}")
        gemini_model = None
else:
    print("OSTRZEŻENIE: Brak GEMINI_API_KEY. Analiza AI będzie niedostępna.")
    gemini_model = None

# --- Inicjalizacja Bota ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- FUNKCJE POMOCNICZE ---

def get_fear_and_greed_image():
    timestamp = int(time.time())
    return f"https://alternative.me/crypto/fear-and-greed-index.png?v={timestamp}"

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0 
    
    deltas = np.diff(prices)
    gains = deltas[deltas > 0]
    losses = -deltas[deltas < 0]
    
    avg_gain = np.mean(gains[:period]) if gains.size > 0 else 0
    avg_loss = np.mean(losses[:period]) if losses.size > 0 else 0

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
    headers = {'x-cg-demo-api-key': COINGECKO_API_KEY.strip()}
    stablecoin_symbols = {'usdt', 'usdc', 'dai', 'busd', 'ust', 'tusd'}

    try:
        params = {'vs_currency': 'usd', 'order': 'market_cap_desc', 'per_page': 100, 'page': 1}
        response = requests.get("https://api.coingecko.com/api/v3/coins/markets", params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        filtered_data = [coin for coin in data if coin['symbol'] not in stablecoin_symbols]
        sorted_gainers = sorted(filtered_data, key=lambda x: x.get('price_change_percentage_24h', 0) or 0, reverse=True)
        gainers_list = [f"🥇 **{c['name']} ({c['symbol'].upper()})**: `+{c.get('price_change_percentage_24h', 0):.2f}%`" for c in sorted_gainers[:count]]
        return "\n".join(gainers_list) if gainers_list else "Brak danych lub wszystkie monety odnotowaly spadek."
    except Exception as e:
        print(f"Blad polaczenia lub przetwarzania CoinGecko: {e}")
        return "Blad: Problem z pobraniem danych."

def get_fed_events():
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
        return "\n".join(fed_events) if fed_events else "Brak kluczowych wydarzeń FED w najblizszych 2 tygodniach."
    except Exception as e:
        return f"Blad podczas pobierania wydarzeń FED: {e}"

def get_btc_eth_analysis():
    analysis_text = ""
    for coin in ["bitcoin", "ethereum"]:
        try:
            headers = {'x-cg-demo-api-key': COINGECKO_API_KEY.strip()}
            response_chart = requests.get(f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart?vs_currency=usd&days=15", headers=headers, timeout=10)
            response_chart.raise_for_status()
            prices = [p[1] for p in response_chart.json()['prices']]
            rsi = calculate_rsi(prices)
            rsi_interpretation = "Neutralnie 😐"
            if rsi > 70: rsi_interpretation = "Rynek wykupiony 📈"
            if rsi < 30: rsi_interpretation = "Rynek wyprzedany 📉"
            prices_7_days = prices[-7:]
            support = min(prices_7_days)
            resistance = max(prices_7_days)
            current_price = prices[-1]
            analysis_text += (f"**{coin.capitalize()} (${current_price:,.2f})**\n- **RSI (14D):** `{rsi:.2f}` ({rsi_interpretation})\n- **Wsparcie (7D):** `${support:,.2f}`\n- **Opor (7D):** `${resistance:,.2f}`\n\n")
        except Exception as e:
            print(f"Blad analizy dla {coin}: {e}")
            analysis_text += f"Blad analizy dla {coin.capitalize()}.\n"
    return analysis_text

def get_realtime_market_snapshot():
    snapshot = {"fear_greed": "Brak danych", "top_gainers": "Brak danych", "latest_headlines": []}
    try:
        response = requests.get("https://api.alternative.me/fng/?limit=1")
        response.raise_for_status()
        data = response.json()['data'][0]
        snapshot['fear_greed'] = f"{data['value']} ({data['value_classification']})"
    except Exception as e:
        print(f"Blad pobierania Fear & Greed: {e}")

    snapshot['top_gainers'] = get_top_gainers(3)
    try:
        feed = feedparser.parse(WATCHER_GURU_RSS_URL)
        snapshot['latest_headlines'] = [entry.title for entry in feed.entries[:5]]
    except Exception as e:
        print(f"Blad pobierania naglowkow RSS: {e}")
        snapshot['latest_headlines'] = ["Brak danych o newsach."]
    return snapshot

async def send_market_report(channel_or_ctx,
                             title: str,
                             color: discord.Color,
                             include_fg: bool = False,
                             include_gainers: bool = False,
                             include_fed: bool = False,
                             include_heatmap: bool = False,
                             include_ai_analysis: bool = False):
    
    if isinstance(channel_or_ctx, (discord.Interaction, discord.Interaction.followup)):
        followup_send = channel_or_ctx.followup.send if isinstance(channel_or_ctx, discord.Interaction) else channel_or_ctx.send
    else:
        followup_send = channel_or_ctx.send

    if include_fg:
        fg_embed = discord.Embed(title=title, color=color)
        fg_embed.add_field(name="Indeks Fear & Greed", value=" ", inline=False)
        fg_embed.set_image(url=get_fear_and_greed_image())
        await followup_send(embed=fg_embed)
        main_embed = discord.Embed(color=color)
    else:
        main_embed = discord.Embed(title=title, color=color)

    if include_ai_analysis and gemini_model:
        ai_summary = await asyncio.to_thread(get_ai_report_analysis)
        main_embed.add_field(name="🤖 Analiza i Prognoza AI", value=ai_summary, inline=False)
    elif include_ai_analysis and not gemini_model:
        main_embed.add_field(name="🤖 Analiza AI", value="Brak klucza API Gemini (GEMINI_API_KEY).", inline=False)

    if include_gainers:
        main_embed.add_field(name="🔥 Top 10 Gainers (24h)", value=get_top_gainers(10), inline=False)

    if include_fed:
        main_embed.add_field(name="🇺🇸 Wydarzenia FED (14 dni)", value=get_fed_events(), inline=False)

    if main_embed.fields:
        await followup_send(embed=main_embed)

    if include_heatmap:
        try:
            timestamp = int(time.time())
            heatmap_url = f"https://quantifycrypto.com/heatmaps/crypto-heatmap.png?v={timestamp}" 
            response = requests.get(heatmap_url)
            response.raise_for_status()
            image_file = discord.File(io.BytesIO(response.content), filename="heatmap.png")
            heatmap_embed = discord.Embed(title="📊 Mapa Cieplna (Top 100)", color=discord.Color.red())
            heatmap_embed.set_image(url="attachment://heatmap.png")
            await followup_send(embed=heatmap_embed, file=image_file)
        except requests.exceptions.RequestException as e:
            print(f"Blad pobierania heatmapy w raporcie: {e}")
            await followup_send("Wystapil blad podczas pobierania mapy cieplnej.", ephemeral=True)

def get_ai_report_analysis():
    if not gemini_model: return "Analiza AI wylaczona (brak klucza)."
    print("Pobieranie danych do analizy AI dla raportu...")
    market_data = get_realtime_market_snapshot()
    headlines_str = "\n- ".join(market_data['latest_headlines'])

    try:
        prompt = (
            f"Jestes analitykiem rynku kryptowalut, tworzacym krotka analizę do automatycznego raportu na Discordzie. Na podstawie ponizszych, aktualnych danych, stworz zwięzle podsumowanie (2-3 zdania) ostatnich kilku godzin i przedstaw krotkoterminowa prognozę (1-2 zdania).\n\n"
            f"--- AKTUALNE DANE ---\n"
            f"1. Sentyment rynkowy (Fear & Greed Index): {market_data['fear_greed']}\n"
            f"2. Najwięksi wygrani (Top Gainers): {market_data['top_gainers']}\n"
            f"3. Ostatnie naglowki wiadomosci:\n- {headlines_str}\n"
            f"--- KONIEC DANYCH ---\n\n"
            f"Zadanie: Napisz krotka analizę. Skup się na ogolnym nastroju, zidentyfikuj kluczowe trendy i wskaz, czy rynek w najblizszych godzinach moze byc niestabilny, czy spodziewasz się kontynuacji trendu. Pisz po polsku, w profesjonalnym, ale przystępnym tonie."
        )

        response = gemini_model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Blad podczas generowania analizy AI do raportu: {e}")
        return "Nie udalo się wygenerowac analizy z powodu blędu."


# --- Komendy ukosnikowe ---

@bot.tree.command(name="raport", description="Generuje pelny raport rynkowy na zadanie.")
async def slash_report(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True) 
    await send_market_report(interaction, title="Raport Rynkowy na zadanie", color=discord.Color.gold(), include_fg=True, include_gainers=True, include_fed=True, include_heatmap=True, include_ai_analysis=True)

@bot.tree.command(name="fg", description="Wyswietla aktualny Indeks Fear & Greed.")
async def slash_fg(interaction: discord.Interaction):
    embed = discord.Embed(title="Fear & Greed Index", color=discord.Color.gold())
    embed.set_image(url=get_fear_and_greed_image())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="gainers", description="Pokazuje 10 kryptowalut z największym wzrostem w ciagu 24h.")
async def slash_gainers(interaction: discord.Interaction):
    description_text = get_top_gainers(10)
    embed = discord.Embed(title="🔥 Top 10 Gainers (24h)", description=description_text, color=discord.Color.green())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="heatmap", description="Wyswietla mapę cieplna krypto.")
async def slash_heatmap(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        timestamp = int(time.time())
        heatmap_url = f"https://quantifycrypto.com/heatmaps/crypto-heatmap.png?v={timestamp}"
        response = requests.get(heatmap_url)
        response.raise_for_status()
        image_file = discord.File(io.BytesIO(response.content), filename="heatmap.png")
        embed = discord.Embed(title="📊 Mapa Cieplna (Top 100)", color=discord.Color.red())
        embed.set_image(url="attachment://heatmap.png")
        await interaction.followup.send(embed=embed, file=image_file)
    except requests.exceptions.RequestException as e:
        print(f"Blad pobierania heatmapy: {e}")
        await interaction.followup.send("Wystapil blad podczas pobierania mapy cieplnej. Sprobuj ponownie.")

@bot.tree.command(name="fed", description="Pokazuje nadchodzace kluczowe wydarzenia FED (14 dni).")
async def slash_fed(interaction: discord.Interaction):
    description_text = get_fed_events()
    embed = discord.Embed(title="🇺🇸 Nadchodzace wydarzenia FED (14 dni)", description=description_text, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="analiza", description="Wyswietla uproszczona analize techniczna dla BTC i ETH.")
async def slash_analysis(interaction: discord.Interaction):
    description_text = get_btc_eth_analysis()
    embed = discord.Embed(title="Analiza BTC & ETH", description=description_text, color=discord.Color.orange())
    await interaction.response.send_message(embed=embed)


# --- Zdarzenia startowe i synchronizacja ---

@bot.event
async def on_ready():
    print(f'Zalogowano jako {bot.user}')
    try:
        # Pamiętaj, aby wywolac start taskow tylko raz
        report_0600.start()
        report_1200.start()
        report_2000.start()
        watcher_guru_forwarder.start()
        fin_watch_forwarder.start()
        
        if gemini_model:
            generate_gemini_news.start() 

        synced = await bot.tree.sync()
        print(f"Zsynchronizowano {len(synced)} komend(y) ukosnikowych.")
    except Exception as e:
        print(f"Blad synchronizacji komend lub startu taskow: {e}")


# --- ZADANIA CYKLICZNE ---

@tasks.loop(time=datetime.time(hour=6, minute=0, tzinfo=TZ_POLAND))
async def report_0600():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return
    title = f"Poranny Raport Rynkowy - {date.today().strftime('%d-%m-%Y')}"
    await send_market_report(channel, title, discord.Color.gold(), include_fg=True, include_gainers=True, include_fed=True, include_heatmap=True, include_ai_analysis=True)

@tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=TZ_POLAND))
async def report_1200():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return
    await send_market_report(channel, "Raport Poludniowy", discord.Color.green(), include_gainers=True, include_heatmap=True, include_ai_analysis=True)

@tasks.loop(time=datetime.time(hour=20, minute=0, tzinfo=TZ_POLAND))
async def report_2000():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return
    await send_market_report(channel, "Raport Wieczorny", discord.Color.purple(), include_gainers=True, include_heatmap=True, include_ai_analysis=True)

@tasks.loop(hours=2)
async def generate_gemini_news():
    if not gemini_model: return
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return
    
    print("Rozpoczynam generowanie szczegolowej analizy AI...")
    market_data = get_realtime_market_snapshot()
    headlines_str = "\n- ".join(market_data['latest_headlines'])
    current_date = datetime.datetime.now(TZ_POLAND).strftime("%Y-%m-%d %H:%M")
    
    try:
        prompt = (f"Jestes ekspertem i analitykiem rynku kryptowalut. Twoim zadaniem jest stworzenie podsumowania dla kanalu na Discordzie na podstawie ponizszych, aktualnych danych. Analizuj TYLKO dostarczone informacje.\n\n--- POCZĄTEK DANYCH (stan na {current_date}) ---\n1. Ogolny sentyment rynkowy (Fear & Greed Index): {market_data['fear_greed']}\n\n2. Kryptowaluty z największymi wzrostami (Top Gainers):\n{market_data['top_gainers']}\n\n3. Najnowsze naglowki z wiadomosci:\n- {headlines_str}\n--- KONIEC DANYCH ---\n\nZadanie: Na podstawie powyzszych danych, stworz listę **do 10 kluczowych punktow** opisujacych sytuację na rynku. **Posortuj punkty w kolejnosci od najwazniejszego (na gorze) do najmniej waznego (na dole).** Kazdy punkt powinien byc zwięzly i konkretny. Skup się na najwazniejszych wnioskach dotyczacych Bitcoina, Ethereum, sentymentu oraz trendow widocznych w newsach i wzrostach. Pisz po polsku.")
        response = await asyncio.to_thread(gemini_model.generate_content, prompt)
        
        embed = discord.Embed(title="📈 Szczegolowa Analiza Rynku (AI)", description=response.text, color=discord.Color.from_rgb(70, 130, 180))
        embed.set_footer(text=f"Wygenerowano przez Gemini AI | Dane z {current_date}")
        await channel.send(embed=embed)
    except Exception as e:
        print(f"Wystapil blad podczas generowania analizy przez Gemini: {e}")

@tasks.loop(minutes=5)
async def watcher_guru_forwarder():
    channel = bot.get_channel(WATCHER_GURU_CHANNEL_ID)
    if not channel: return
    feed = feedparser.parse(WATCHER_GURU_RSS_URL)
    for entry in reversed(feed.entries[:5]): 
        await process_and_send_news(channel, entry, "Watcher Guru", WATCHER_GURU_SENT_URLS)
        await asyncio.sleep(1) 

@tasks.loop(minutes=5)
async def fin_watch_forwarder():
    channel = bot.get_channel(FIN_WATCH_CHANNEL_ID)
    if not channel: return
    feed = feedparser.parse(FIN_WATCH_RSS_URL)
    for entry in reversed(feed.entries[:5]): 
        await process_and_send_news(channel, entry, "Fin Watch (Telegram)", FIN_WATCH_SENT_URLS)
        await asyncio.sleep(1)

async def process_and_send_news(channel, entry, source_name, sent_urls_deque):
    if entry.link in sent_urls_deque: return
    
    tags_to_remove = ["@WatcherGuru", "@WatcherGur", "@WatcherGu", "@WatcherG", "@Watcher", "@Watche", "@Watch", "@Watc", "@FINNWatch", "@Fin_Watch", "@Finn", "@Fin"]
    title_original = entry.title
    for tag in tags_to_remove:
        title_original = title_original.replace(tag, "")
    title_original = title_original.strip()

    try:
        prompt = (f"Jestes profesjonalnym tlumaczem dla kanalu informacyjnego. Twoim zadaniem jest stworzenie jednego, zwięzlego i naturalnie brzmiacego tlumaczenia. Nie podawaj zadnych alternatyw, wariantow w nawiasach, uwag ani dodatkowych wyjasnień. Podaj tylko ostateczna, najlepsza wersję.\n\nPrzetlumacz na polski: \"{title_original}\"")
        response = await asyncio.to_thread(gemini_model.generate_content, prompt)
        title_pl = response.text.strip()
    except Exception as e:
        print(f"Blad tlumaczenia Gemini: {e}")
        title_pl = title_original

    embed = discord.Embed(title=f"📰 {source_name.replace('Watcher Guru', 'Wiadomosci').replace('Fin Watch (Telegram)', 'Wiadomosci Finansowe')}", description=f"**{title_pl}**", color=discord.Color.dark_blue())
    
    image_url = next((enc.href for enc in entry.get('enclosures', []) if 'image' in enc.get('type', '')), None)
    if not image_url and 'media_content' in entry and entry.media_content:
        image_url = next((media['url'] for media in entry.media_content if 'image' in media.get('type', '')), None)
    if image_url:
        embed.set_image(url=image_url)

    await channel.send(embed=embed)
    sent_urls_deque.append(entry.link)


# --- GŁÓWNE URUCHOMIENIE (Flask w wątku, Bot w głównym) ---
if __name__ == "__main__":
    print("Uruchamianie serwera Flask w osobnym wątku...")
    flask_thread = Thread(target=run_flask_server)
    flask_thread.start()
    
    print("Uruchamianie bota Discord...")
    bot.run(BOT_TOKEN)