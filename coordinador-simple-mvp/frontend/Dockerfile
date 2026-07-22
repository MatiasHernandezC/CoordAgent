FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# Valores horneados en el build. Override con --build-arg VITE_API_URL=... etc.
ARG VITE_API_URL=http://127.0.0.1:8000
ENV VITE_API_URL=${VITE_API_URL}
ARG VITE_BOT_PHONE_NUMBER="+56 9 3527 1985"
ENV VITE_BOT_PHONE_NUMBER=${VITE_BOT_PHONE_NUMBER}
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
