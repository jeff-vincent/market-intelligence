FROM node:20-slim
WORKDIR /app
COPY package.json package-lock.json* ./
ENV npm_config_cache=/tmp/.npm
RUN npm install --omit=dev
COPY . .
RUN npm run build
CMD ["node", "server.js"]
