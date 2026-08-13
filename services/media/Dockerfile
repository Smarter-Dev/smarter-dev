# Build context is services/media, not the repo root.
FROM node:22-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json tsconfig.build.json ./
COPY src ./src
RUN npm run build

FROM node:22-bookworm-slim
# ffmpeg is a hard dependency: the process refuses to start without it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV NODE_ENV=production \
    MEDIA_PORT=8080

COPY --from=deps /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
# The two TTFs, the Pillow glyph-metrics table and the three background plates.
COPY assets ./assets
COPY package.json ./

USER node
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:'+(process.env.MEDIA_PORT||8080)+'/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "dist/main.js"]
