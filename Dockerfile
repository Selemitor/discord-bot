# Krok 1: Użyj oficjalnego, lekkiego obrazu Pythona
FROM python:3.10.13-slim

# Krok 2: Ustaw katalog roboczy wewnątrz kontenera
WORKDIR /app

# Krok 3: Skopiuj plik zależności i zainstaluj je
COPY requirements.txt .
# OSTATECZNA PRÓBA PRZEŁAMANIA CACHE
RUN pip install -r requirements.txt && pip install google-genai

# Krok 4: Skopiuj resztę kodu swojego bota
COPY . .

# Krok 5: Określ komendę, która ma uruchomić bota
CMD ["python", "bot.py"]