FROM node:18-alpine
WORKDIR /app
ARG NEXT_PUBLIC_QUERY_API_URL
ENV NEXT_PUBLIC_QUERY_API_URL=$NEXT_PUBLIC_QUERY_API_URL
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build
CMD ["npm", "start"]
