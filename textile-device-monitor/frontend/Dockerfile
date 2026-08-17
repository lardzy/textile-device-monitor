FROM node:18-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
RUN rm -f /etc/nginx/conf.d/default.conf \
    && mkdir -p \
        /etc/nginx/tls \
        /etc/nginx/templates \
        /etc/nginx/templates-available
COPY docker-entrypoint.d/10-select-transport.envsh /docker-entrypoint.d/
COPY docker-entrypoint.d/15-render-management-cidrs.envsh /docker-entrypoint.d/
# 源码在 Windows 侧检出/解压可能变成 CRLF，dash 无法解析 \r 会导致入口脚本
# 启动失败；构建时统一归一为 LF，与检出行尾无关。
RUN sed -i 's/\r$//' \
        /docker-entrypoint.d/10-select-transport.envsh \
        /docker-entrypoint.d/15-render-management-cidrs.envsh \
    && chmod 0755 \
        /docker-entrypoint.d/10-select-transport.envsh \
        /docker-entrypoint.d/15-render-management-cidrs.envsh
COPY nginx.conf /etc/nginx/templates-available/http.conf.template
COPY nginx.https.conf /etc/nginx/templates-available/https.conf.template
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
