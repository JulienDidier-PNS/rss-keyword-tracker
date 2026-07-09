FROM node:20-slim

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install --omit=dev

COPY . .

# Session Baileys (credentials WhatsApp) : séparée du code exprès, à monter en
# volume pour survivre au remplacement du conteneur (voir docker-compose.yml).
# La perdre oblige à rescanner le QR code.
ENV SESSION_DIR=/app/auth
RUN mkdir -p /app/auth

CMD ["node", "index.js"]
