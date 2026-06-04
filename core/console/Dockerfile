# ============================================================
# Console Dockerfile — multi-stage build for Next.js standalone
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
# Defaults match production; override with --build-arg if needed
ARG NEXT_PUBLIC_API_URL=https://api.justaskmarvis.com
ARG NEXT_PUBLIC_WS_URL=wss://api.justaskmarvis.com
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
FROM node:22-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Create non-root user
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Copy standalone output (includes server.js + minimal node_modules)
COPY --from=builder /app/.next/standalone ./
# Copy static assets (not included in standalone by default)
COPY --from=builder /app/.next/static ./.next/static
# Copy public dir if it exists
COPY --from=builder /app/public ./public

# Set correct ownership
RUN chown -R nextjs:nodejs /app

USER nextjs

EXPOSE 3000

ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

CMD ["node", "server.js"]
