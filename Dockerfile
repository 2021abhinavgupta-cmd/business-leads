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
#
# The fonts-* packages are NOT optional polish. python:3.11-slim ships with no
# fonts whatsoever, and this dependency list was hand-written rather than taken
# from `playwright install --with-deps`, so it omitted them. Chromium then has
# nothing to rasterize text with: images, shapes and colours render normally
# while EVERY text node comes out blank. Live-observed on namasteyogaclasses.com
# — the audit screenshot showed the hero photo and buttons but no headline, no
# nav labels and no logo wordmark, and that image is both attached to the
# outgoing email and fed to the vision model, which then has an invisible-text
# "design flaw" to describe on a site that renders perfectly for real visitors.
#
# fonts-indic and fonts-noto-core specifically because the leads are Indian
# businesses: Devanagari copy is common and Liberation/DejaVu do not cover it,
# so without these the same blank-text failure recurs on any Marathi or Hindi
# page even once Latin text works.
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
    fonts-liberation \
    fonts-dejavu-core \
    fonts-noto-core \
    fonts-noto-color-emoji \
    fonts-indic \
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
