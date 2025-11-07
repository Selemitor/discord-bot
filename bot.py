import discord
from discord.ext import commands, tasks
import requests
import datetime
from datetime import date, timedelta
import csv
import io
import feedparser
import time
from deep_translator import GoogleTranslator
from zoneinfo import ZoneInfo
import numpy as np
from bs4 import BeautifulSoup

# --- Konfiguracja ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
COINGECKO_API_KEY = os.environ.get('COINGECKO_API_KEY')
ALPHAVANTAGE_API_KEY = os.environ.get('ALPHAVANTAGE_API_KEY')

# Sprawdzenie, czy tokeny istnieją
if not BOT_TOKEN:
    print("BŁĄD: Nie znaleziono BOT_TOKEN. Ustaw go w sekretach Fly.io!")
    exit()
if not COINGECKO_API_KEY:
    print("OSTRZEŻENIE: Nie znaleziono COINGECKO_API_KEY.")
if not ALPHAVANTAGE_API_KEY:
    print("OSTRZEŻENIE: Nie znaleziono ALPHAVANTAGE_API_KEY.")


# Uzupełnij ID swoich kanałów
CHANNEL_ID = 1429744335389458452
WATCHER_GURU_CHANNEL_ID = 1429719129702535248 
FIN_WATCH_CHANNEL_ID = 1429719129702535248

WATCHER_GURU_RSS_URL = "https://rss.app/feeds/bP1lIE9lQ9hTBuSk.xml"
FIN_WATCH_RSS_URL = "https://rss.app/feeds/R0DJZuoPNWe5yCMY.xml"

WATCHER_GURU_SENT_URLS = set()
FIN_WATCH_SENT_URLS = set()
TZ_POLAND = ZoneInfo("Europe/Warsaw")

# <<< POPRAWKA: Prawidłowe nazwy zmiennych >>>
WATCHER_GURU_SENT_URLS = set()
FIN_WATCH_SENT_URLS = set()
TZ_POLAND = ZoneInfo("Europe/Warsaw")
# --------------------

# <<< POPRAWKA: Inicjalizacja bota przeniesiona we właściwe miejsce >>>
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- FUNKCJE POMOCNICZE (bez zmian) ---
def get_fear_and_greed_image():
    timestamp = int(time.time())
    return f"https://alternative.me/crypto/fear-and-greed-index.png?v={timestamp}"
def calculate_rsi(prices, period=14):
    deltas = np.diff(prices)
    seed = deltas[:period+1]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    rs = up / down
    rsi = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period, len(deltas)):
        delta = deltas[i]
        if delta > 0: upval, downval = delta, 0.0
        else: upval, downval = 0.0, -delta
        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period
        rs = up / down
        rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi
def get_top_gainers(count=10):
    try:
        headers = {'x-cg-demo-api-key': COINGECKO_API_KEY.strip()}
        params = {'vs_currency': 'usd', 'order': 'market_cap_desc', 'per_page': 100, 'page': 1}
        response = requests.get("https://api.coingecko.com/api/v3/coins/markets", params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        sorted_gainers = sorted(data, key=lambda x: x.get('price_change_percentage_24h', 0), reverse=True)
        gainers_list = [f"🥇 **{c['name']} ({c['symbol'].upper()})**: `+{c.get('price_change_percentage_24h', 0):.2f}%`" for c in sorted_gainers[:count]]
        return "\n".join(gainers_list) if gainers_list else "Brak danych."
    except Exception as e: return f"Błąd: {e}"
def get_fed_events():
    try:
        url = f'https://www.alphavantage.co/query?function=ECONOMIC_CALENDAR&horizon=3month&apikey={ALPHAVANTAGE_API_KEY.strip()}'
        response = requests.get(url)
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
                    if event_str not in fed_events: fed_events.append(event_str)
        return "\n".join(fed_events) if fed_events else "Brak kluczowych wydarzeń FED w najbliższych 2 tygodniach."
    except Exception as e: return f"Błąd: {e}"
def get_btc_eth_analysis():
    analysis_text = ""
    for coin in ["bitcoin", "ethereum"]:
        try:
            headers = {'x-cg-demo-api-key': COINGECKO_API_KEY.strip()}
            response_chart = requests.get(f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart?vs_currency=usd&days=15", headers=headers)
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
            analysis_text += (f"**{coin.capitalize()} (${current_price:,.2f})**\n- **RSI (14D):** `{rsi:.2f}` ({rsi_interpretation})\n- **Wsparcie (7D):** `${support:,.2f}`\n- **Opór (7D):** `${resistance:,.2f}`\n\n")
        except Exception:
            analysis_text += f"Błąd analizy dla {coin.capitalize()}.\n"
    return analysis_text

# --- GŁÓWNA PĘTLA ZDARZEŃ ---
@bot.event
async def on_ready():
    print(f'Zalogowano jako {bot.user}')
    report_0600.start()
    analysis_0800.start()
    report_1200.start()
    us_market_alert.start()
    report_2000.start()
    watcher_guru_forwarder.start()
    fin_watch_forwarder.start()

# --- KOMENDY NA ŻĄDANIE ---
@bot.command(name='raport')
async def command_report(ctx):
    embed = discord.Embed(title=f"Raport Rynkowy na Żądanie", color=discord.Color.gold())
    embed.add_field(name="Indeks Fear & Greed", value=" ", inline=False)
    embed.set_image(url=get_fear_and_greed_image())
    await ctx.send(embed=embed)
    gainers_embed = discord.Embed(color=discord.Color.gold())
    gainers_embed.add_field(name="🔥 Top 10 Gainers (24h)", value=get_top_gainers(10), inline=False)
    gainers_embed.add_field(name="🇺🇸 Wydarzenia FED (14 dni)", value=get_fed_events(), inline=False)
    await ctx.send(embed=gainers_embed)
    heatmap_embed = discord.Embed(title="📊 Mapa Cieplna (Top 100)", color=discord.Color.red())
    heatmap_embed.set_image(url="https://finviz.com/crypto_charts.ashx?t=all&tf=d1&p=d&s=n")
    await ctx.send(embed=heatmap_embed)

@bot.command(name='fg')
async def command_fg(ctx):
    embed = discord.Embed(title="Fear & Greed Index", color=discord.Color.gold())
    embed.set_image(url=get_fear_and_greed_image())
    await ctx.send(embed=embed)

@bot.command(name='gainers')
async def command_gainers(ctx):
    embed = discord.Embed(title="🔥 Top 10 Gainers (24h)", description=get_top_gainers(10), color=discord.Color.green())
    await ctx.send(embed=embed)

@bot.command(name='heatmap')
async def command_heatmap(ctx):
    embed = discord.Embed(title="📊 Mapa Cieplna (Top 100)", color=discord.Color.red())
    embed.set_image(url="https://finviz.com/crypto_charts.ashx?t=all&tf=d1&p=d&s=n")
    await ctx.send(embed=embed)

@bot.command(name='fed')
async def command_fed(ctx):
    embed = discord.Embed(title="🇺🇸 Nadchodzące wydarzenia FED (14 dni)", description=get_fed_events(), color=discord.Color.blue())
    await ctx.send(embed=embed)

@bot.command(name='analiza')
async def command_analysis(ctx):
    embed = discord.Embed(title="Analiza BTC & ETH", description=get_btc_eth_analysis(), color=discord.Color.orange())
    await ctx.send(embed=embed)

# --- STANDARDOWE ZAPLANOWANE ZADANIA ---
@tasks.loop(time=datetime.time(hour=6, minute=0, tzinfo=TZ_POLAND))
async def report_0600():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return
    embed_fg = discord.Embed(title=f"Poranny Raport Rynkowy - {date.today().strftime('%d-%m-%Y')}", color=discord.Color.gold())
    embed_fg.add_field(name="Indeks Fear & Greed", value=" ", inline=False)
    embed_fg.set_image(url=get_fear_and_greed_image())
    await channel.send(embed=embed_fg)
    embed_data = discord.Embed(color=discord.Color.gold())
    embed_data.add_field(name="🔥 Top 10 Gainers (24h)", value=get_top_gainers(10), inline=False)
    embed_data.add_field(name="🇺🇸 Wydarzenia FED (14 dni)", value=get_fed_events(), inline=False)
    await channel.send(embed=embed_data)
    heatmap_embed = discord.Embed(title="📊 Mapa Cieplna (Top 100)", color=discord.Color.red())
    heatmap_embed.set_image(url="https://finviz.com/crypto_charts.ashx?t=all&tf=d1&p=d&s=n")
    await channel.send(embed=heatmap_embed)

@tasks.loop(time=datetime.time(hour=8, minute=0, tzinfo=TZ_POLAND))
async def analysis_0800():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return
    embed = discord.Embed(title="Analiza BTC & ETH", description=get_btc_eth_analysis(), color=discord.Color.orange())
    await channel.send(embed=embed)

@tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=TZ_POLAND))
async def report_1200():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return
    embed = discord.Embed(title="Raport Południowy", color=discord.Color.green())
    embed.add_field(name="🔥 Top 10 Gainers (24h)", value=get_top_gainers(10), inline=False)
    await channel.send(embed=embed)
    heatmap_embed = discord.Embed(title="📊 Mapa Cieplna (Top 100)", color=discord.Color.red())
    heatmap_embed.set_image(url="https://finviz.com/crypto_charts.ashx?t=all&tf=d1&p=d&s=n")
    await channel.send(embed=heatmap_embed)

@tasks.loop(time=datetime.time(hour=15, minute=25, tzinfo=TZ_POLAND))
async def us_market_alert():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return
    await channel.send("🚨 **Alert: Giełda w USA otwiera się za 5 minut!** 🚨")

@tasks.loop(time=datetime.time(hour=20, minute=0, tzinfo=TZ_POLAND))
async def report_2000():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return
    embed = discord.Embed(title="Raport Wieczorny", color=discord.Color.purple())
    embed.add_field(name="🔥 Top 10 Gainers (24h)", value=get_top_gainers(10), inline=False)
    await channel.send(embed=embed)
    heatmap_embed = discord.Embed(title="📊 Mapa Cieplna (Top 100)", color=discord.Color.red())
    heatmap_embed.set_image(url="https://finviz.com/crypto_charts.ashx?t=all&tf=d1&p=d&s=n")
    await channel.send(embed=heatmap_embed)

# --- CIĄGŁE PRZEKAZYWANIE NEWSÓW ---
async def process_and_send_news(channel, entry, source_name, sent_urls_set):
    if entry.link in sent_urls_set: return
    title = entry.title
    summary_html = entry.summary if 'summary' in entry else ""
    if summary_html:
        soup = BeautifulSoup(summary_html, 'html.parser')
        summary = soup.get_text(separator=' ', strip=True)
    else: summary = ""
    try:
        title_pl = GoogleTranslator(source='auto', target='pl').translate(title)
        summary_pl = GoogleTranslator(source='auto', target='pl').translate(summary) if summary else ""
    except Exception as e:
        print(f"Błąd tłumaczenia: {e}")
        title_pl, summary_pl = title, summary
    embed = discord.Embed(title=f"📰 {source_name}", description=f"**{title_pl}**", url=entry.link, color=discord.Color.dark_blue())
    if summary_pl: embed.add_field(name="Podsumowanie", value=summary_pl[:1000] + "...", inline=False)
    image_url = None
    if 'media_content' in entry and entry.media_content: image_url = entry.media_content[0]['url']
    elif 'enclosures' in entry and entry.enclosures:
        for enc in entry.enclosures:
            if 'image' in enc.get('type', ''): image_url = enc.href; break
    elif 'links' in entry:
        for link in entry.links:
            if 'image' in link.get('type', ''): image_url = link.href; break
    if image_url: embed.set_image(url=image_url)
    await channel.send(embed=embed)
    sent_urls_set.add(entry.link)
    if len(sent_urls_set) > 200: sent_urls_set.pop()

@tasks.loop(minutes=5)
async def watcher_guru_forwarder():
    channel = bot.get_channel(WATCHER_GURU_CHANNEL_ID)
    if not channel: return
    feed = feedparser.parse(WATCHER_GURU_RSS_URL)
    for entry in reversed(feed.entries[:10]):
        await process_and_send_news(channel, entry, "Watcher Guru", WATCHER_GURU_SENT_URLS)

@tasks.loop(minutes=5)
async def fin_watch_forwarder():
    channel = bot.get_channel(FIN_WATCH_CHANNEL_ID)
    if not channel: return
    feed = feedparser.parse(FIN_WATCH_RSS_URL)
    for entry in reversed(feed.entries[:10]):
        await process_and_send_news(channel, entry, "Fin Watch (Telegram)", FIN_WATCH_SENT_URLS)

# Uruchomienie bota
bot.run(BOT_TOKEN)