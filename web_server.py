# Plik: web_server.py
# Ten plik uruchamia TYLKO serwer Flask dla Gunicorn.

import os
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    """Endpoint dla UptimeRobot, aby utrzymać bota przy życiu."""
    return "Bot jest aktywny (Serwer Web)!"

@app.route('/healthz')
def health_check():
    """Endpoint dla Render Health Check."""
    return "OK", 200

# Gunicorn automatycznie znajdzie obiekt 'app'