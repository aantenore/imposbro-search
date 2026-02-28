# Stage 1: Base - A common base for all stages
FROM node:18-alpine AS base
WORKDIR /app

# Stage 2: Dependencies - Install dependencies in a separate layer
FROM base AS deps
COPY package.json package-lock.json* ./
RUN npm install

# Stage 3: Builder - Build the Next.js application
FROM base AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Declare the build-time argument
ARG INTERNAL_QUERY_API_URL
# Set the environment variable for the `npm run build` command
ENV INTERNAL_QUERY_API_URL=$INTERNAL_QUERY_API_URL

# Build and merge static assets into standalone (required for CSS/JS to load)
RUN npm run build && cp -R .next/static .next/standalone/.next/

# Stage 4: Runner - The final, small production image
FROM base AS runner
WORKDIR /app

# Set runtime environment variable for the final running container
ENV INTERNAL_QUERY_API_URL=$INTERNAL_QUERY_API_URL
ENV NODE_ENV=production

# Copy the complete standalone output (includes .next/static from builder step)
COPY --from=builder /app/.next/standalone ./

# (Optional) Copy the public folder if you have one
# COPY --from=builder /app/public ./public

EXPOSE 3000

# The command to start the server is now simpler because we are in the correct directory
CMD ["node", "server.js"]
