FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Données persistantes (config.json, articles.db) : séparées du code exprès,
# pour survivre au remplacement de l'image lors d'une mise à jour. Monter un
# volume sur /data (voir docker-compose.yml).
ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8000

CMD ["waitress-serve", "--host=0.0.0.0", "--port=8000", "app:app"]
