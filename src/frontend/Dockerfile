FROM node:20-alpine as frontend_build
ARG BACKEND
WORKDIR /app

# 0. 安装 git 并配置高速国内镜像代理，防止拉取 github 第三方依赖（bisheng-design-system）时网络超时中断
RUN apk add --no-cache git && \
    git config --global url."https://mirror.ghproxy.com/https://github.com/".insteadOf "https://github.com/"

# 1. 复制平台和客户端的 package 描述文件以及本地依赖包
COPY platform/package.json /app/platform/
COPY platform/package-lock.json* /app/platform/
COPY platform/local-packages /app/platform/local-packages
COPY client/package.json /app/client/
COPY client/package-lock.json* /app/client/
COPY client/local-packages /app/client/local-packages

# 2. 预先安装依赖（该层会被 Docker 强力缓存，只要 package.json 不变就不会重复执行）
RUN cd /app/platform && npm install --registry=https://registry.npmmirror.com --no-audit --no-fund --progress=false
RUN cd /app/client && npm install --registry=https://registry.npmmirror.com --no-audit --no-fund --progress=false

# 3. 复制其余所有源码文件
COPY . /app

# 4. 执行生产打包构建
RUN cd /app/platform && npm run build
RUN cd /app/client && npm run build

FROM dataelement/bisheng-frontend:v2.4.0-beta1-fix
COPY --from=frontend_build /app/platform/build/ /usr/share/nginx/html/platform
COPY --from=frontend_build /app/client/build/ /usr/share/nginx/html/client
COPY /nginx.conf /etc/nginx/conf.d/default.conf
