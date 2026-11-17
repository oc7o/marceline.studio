# Production edge image: Caddy plus the static SPA build (ADR 0028). Build context: frontend/.
#
# The build runs here, on the machine that builds images, never on the small production VPS: a
# Vite + tsc build wants more memory than the VPS has to spare. The Caddyfile is mounted by
# infra/prod/compose.yaml rather than baked in, so a routing fix is a restart, not a rebuild.

FROM node:22-slim AS build

WORKDIR /frontend
COPY package.json package-lock.json .npmrc ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build


FROM caddy:2.10-alpine

COPY --from=build /frontend/dist /srv/spa
