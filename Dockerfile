FROM python:3.11-slim as backend-builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install fastapi uvicorn

FROM node:20 as frontend-builder
WORKDIR /app/frontend
# Set VITE_API_KEY as a Railway Build Variable (Project Settings > Variables >
# mark as build-time) so it's baked into the built JS at image-build time —
# never committed to git, but still readable by anyone who inspects the
# shipped JS bundle (same caveat as any client-side "secret").
ARG VITE_API_KEY
ENV VITE_API_KEY=$VITE_API_KEY
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ .
RUN npm run build

FROM python:3.11-slim
WORKDIR /app

# Install Playwright dependencies + Node.js runtime. Node is required at
# RUNTIME (not just for the frontend build) because analyzer/lighthouse.py
# shells out to the `lighthouse` CLI binary — without a `node` executable in
# this final image, every Lighthouse run silently fails (falls through to
# the PageSpeed API fallback, or to hardcoded 0 scores if that also fails).
RUN apt-get update && apt-get install -y \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    curl \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY --from=backend-builder /usr/local/lib/python3.11/site-packages/ /usr/local/lib/python3.11/site-packages/
COPY --from=backend-builder /usr/local/bin/ /usr/local/bin/
COPY . .
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

# Install the Lighthouse CLI (root package.json) so analyzer/lighthouse.py
# finds it at node_modules/.bin/lighthouse.
RUN npm install --omit=dev

# Install playwright browsers
RUN playwright install chromium

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
