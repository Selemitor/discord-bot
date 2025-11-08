#!/bin/bash

# Uruchom Gunicorn (serwer web) w tle
echo "Uruchamianie serwera Gunicorn..."
gunicorn web_server:app &

# Uruchom bota Discord (główny proces)
echo "Uruchamianie bota Discord..."
python bot.py