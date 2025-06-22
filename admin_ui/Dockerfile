# Use a lightweight, official Node.js image as a base
FROM node:18-alpine

# Set the working directory inside the container
WORKDIR /app

# 1. Declare variables that will be passed during the build process.
# These must be provided in your docker-compose.yml under `build.args`.
ARG INTERNAL_QUERY_API_URL

# 2. Set the runtime environment variables using the build arguments.
# This makes the variables available to both `RUN npm run build` and the final `CMD`.
ENV INTERNAL_QUERY_API_URL=$INTERNAL_QUERY_API_URL

# Copy package.json and package-lock.json first to leverage Docker's layer caching
COPY package*.json ./

# Install project dependencies
RUN npm install

# Copy the rest of the application source code into the container
COPY . .

# Build the Next.js application for production
# This command needs access to the environment variables set above
RUN npm run build

# The correct command to start a Next.js app that uses "output: 'standalone'"
# This is the standard for production Docker deployments
CMD ["node", ".next/standalone/server.js"]
