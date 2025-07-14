from python:3.12-slim

run apt-get update && apt-get install -y socat libnss3 libatk-bridge2.0-0 libcups2 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libxkbcommon0 libpango-1.0-0 libcairo2 libasound2 && apt-get clean && rm -rf /var/lib/apt/lists/*
copy requirements.txt requirements.txt
run pip install --no-cache-dir -r requirements.txt
run plotly_get_chrome -y

workdir /workspace
env APP_ENV=production

copy server.py server.py
copy app app
copy public public
expose 12345

cmd ["python", "server.py"]