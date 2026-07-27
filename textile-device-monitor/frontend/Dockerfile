FROM node:18-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
RUN rm -f /etc/nginx/conf.d/default.conf \
    && mkdir -p /etc/nginx/tls
COPY docker-entrypoint.d/15-render-management-cidrs.envsh /docker-entrypoint.d/
RUN chmod 0755 /docker-entrypoint.d/15-render-management-cidrs.envsh
COPY nginx.conf /etc/nginx/templates/default.conf.template
EXPOSE 443
CMD ["nginx", "-g", "daemon off;"]
