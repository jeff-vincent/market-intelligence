FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json* ./
ENV npm_config_cache=/tmp/.npm
RUN npm install
COPY . .
RUN npm run build

FROM node:20-slim
WORKDIR /app
RUN addgroup --system appuser && adduser --system --ingroup appuser appuser
ENV npm_config_cache=/tmp/.npm
COPY --from=build /app/package.json /app/package-lock.json* ./
RUN npm install --omit=dev
COPY --from=build /app/server.js ./
COPY --from=build /app/public ./public
USER appuser
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD node -e "require('http').get('http://localhost:3000/', (r) => { process.exit(r.statusCode === 200 ? 0 : 1); }).on('error', () => process.exit(1))" || exit 1
CMD ["node", "server.js"]
