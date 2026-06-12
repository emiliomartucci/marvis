# ============================================================
# Console Dockerfile — multi-stage build for Next.js static export
# v1.0.0 - 2026-03-12 - Initial version
# ============================================================

# --- Stage 1: Dependencies ---
FROM node:22-alpine AS deps
WORKDIR /app

# Copy package files only (cache layer for npm install)
COPY package.json package-lock.json ./

# Install all dependencies (including devDependencies for build)
RUN npm ci

# --- Stage 2: Build ---
FROM node:22-alpine AS builder
WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Build-time env vars (baked into the JS bundle)
# Defaults are same-origin for the local OSS package.
ARG NEXT_PUBLIC_API_URL=
ARG NEXT_PUBLIC_WS_URL=
ARG NEXT_PUBLIC_VAPID_PUBLIC_KEY=
ARG NEXT_PUBLIC_ENABLE_GRAPH_UX=false
ARG NEXT_PUBLIC_ENABLE_GRAPH_CANVAS=false
ARG NEXT_PUBLIC_ENABLE_GRAPH_UNIVERSE=false

ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_WS_URL=$NEXT_PUBLIC_WS_URL
ENV NEXT_PUBLIC_VAPID_PUBLIC_KEY=$NEXT_PUBLIC_VAPID_PUBLIC_KEY
ENV NEXT_PUBLIC_ENABLE_GRAPH_UX=$NEXT_PUBLIC_ENABLE_GRAPH_UX
ENV NEXT_PUBLIC_ENABLE_GRAPH_CANVAS=$NEXT_PUBLIC_ENABLE_GRAPH_CANVAS
ENV NEXT_PUBLIC_ENABLE_GRAPH_UNIVERSE=$NEXT_PUBLIC_ENABLE_GRAPH_UNIVERSE
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build

# --- Stage 3: Runtime ---
FROM busybox:1.36 AS runner
WORKDIR /www

COPY --from=builder /app/out ./

EXPOSE 3000

CMD ["httpd", "-f", "-p", "3000", "-h", "/www"]
