# Stage 1: Base - A common base for all stages
FROM node:24-alpine@sha256:a0b9bf06e4e6193cf7a0f58816cc935ff8c2a908f81e6f1a95432d679c54fbfd AS base
WORKDIR /app

# Stage 2: Dependencies - Install dependencies in a separate layer
FROM base AS deps
COPY package.json package-lock.json ./
RUN npm ci

# Stage 3: Builder - Build the Next.js application
FROM base AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Declare the build-time argument
ARG INTERNAL_QUERY_API_URL
# Set the environment variable for the `npm run build` command
ENV INTERNAL_QUERY_API_URL=$INTERNAL_QUERY_API_URL
ENV NEXT_TELEMETRY_DISABLED=1

# Build and merge static assets into standalone (required for CSS/JS to load)
RUN npm run build && cp -R .next/static .next/standalone/.next/

# Stage 4: Runner - The final, small production image
FROM base AS runner
WORKDIR /app

# The standalone runtime only needs Node. Removing npm eliminates an unused
# privileged package-management surface and its transitive vulnerability set.
RUN npm uninstall --global npm

# INTERNAL_QUERY_API_URL is intentionally supplied at runtime so the same
# immutable image can be promoted across environments without rebuilding it.
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Copy the complete standalone output (includes .next/static from builder step)
COPY --from=builder --chown=1000:1000 /app/.next/standalone ./

# (Optional) Copy the public folder if you have one
# COPY --from=builder /app/public ./public

EXPOSE 3000

# The command to start the server is now simpler because we are in the correct directory
USER 1000
CMD ["node", "server.js"]
