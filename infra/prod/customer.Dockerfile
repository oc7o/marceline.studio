# The customer app's Next.js server (ADR 0036). Build context: frontend/.
#
# The build runs here, on the machine that builds images, never on the small production VPS: a
# Next build wants more memory than the VPS has to spare. The result is a standalone server — Next
# traces the modules it needs, so the runtime image carries no node_modules of its own.
#
# The app imports the words and the tested pure logic from `frontend/src` (`@shared/*`,
# ADR 0036), which is why the context is `frontend/` and not `frontend/customer/`.

FROM node:22-slim AS build

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /frontend/customer

COPY customer/package.json customer/package-lock.json customer/.npmrc ./
RUN npm ci --no-audit --no-fund

# Only what the build reads: this app, and the shared modules it imports by path.
COPY customer/ ./
COPY src/ ../src/

RUN npm run build


FROM node:22-slim

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
# Bound inside the container only; Caddy is the only way in (no published port in compose.yaml).
ENV HOSTNAME=0.0.0.0
ENV PORT=3000

WORKDIR /app

# The traced server (`server.js`, its node_modules and the server chunks), then the client files
# it serves itself under /app/_next/static/*. `outputFileTracingRoot` is this project, so the
# standalone tree is flat and `server.js` sits at the working directory.
COPY --from=build /frontend/customer/.next/standalone/ ./
COPY --from=build /frontend/customer/.next/static/ ./.next/static/

USER node
EXPOSE 3000

CMD ["node", "server.js"]
