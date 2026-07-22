# Build Stage
FROM node:20-alpine as build

WORKDIR /app

# Copy package files first to leverage Docker cache
COPY package*.json ./
RUN npm install

COPY . .

# Build the Vite app (BACKEND_URL is injected at runtime, not build time)
RUN npm run build

# Serve Stage
FROM nginx:alpine

# Replace default nginx config with our custom one (port 8080, SPA fallback)
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy the built app
COPY --from=build /app/dist /usr/share/nginx/html

# Copy the runtime entrypoint that injects BACKEND_URL
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]