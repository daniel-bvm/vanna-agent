from python:3.12-slim

run apt-get update && apt-get install -y socat && apt-get clean && rm -rf /var/lib/apt/lists/*
copy requirements.txt requirements.txt
run pip install --no-cache-dir -r requirements.txt

workdir /workspace
env APP_ENV=production

copy server.py server.py
copy app app
expose 12345

cmd ["python", "server.py"]