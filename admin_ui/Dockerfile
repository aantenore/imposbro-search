FROM node:18-alpine
WORKDIR /app
ENV INTERNAL_QUERY_API_URL=$INTERNAL_QUERY_API_URL
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build
CMD ["node", ".next/standalone/server.js"]
