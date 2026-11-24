# Keep the browser and Node binaries tied to the exact supported Playwright build.
# Its all-browser OS and npm build tools never enter the final runtime image.
ARG PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright:v1.65.1-noble@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579
ARG RUNTIME_IMAGE=ubuntu:26.04@sha256:d66b366dac61b575bee2aef07e9c26e8070096fbe84a3d4d3eb0da069b5d76db
FROM ${PLAYWRIGHT_IMAGE} AS browser
ARG PLAYWRIGHT_VERSION=1.65.1
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 NODE_ENV=production
WORKDIR /opt/renderer
RUN npm install --omit=dev --ignore-scripts --no-audit --no-fund playwright@${PLAYWRIGHT_VERSION} \
    && mkdir -p /opt/chromium-runtime \
    && cp -a /ms-playwright/chromium_headless_shell-* /opt/chromium-runtime/

FROM ${RUNTIME_IMAGE}
ENV NODE_ENV=production PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /opt/renderer
# Playwright 1.65.1's Ubuntu 26.04 Chromium libraries and fonts. This runner uses
# headless Chromium only: no WebKit, Firefox, video recording or npm build tools.
# Retain dpkg metadata so vulnerability scanners can inventory every OS package.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       ca-certificates libatomic1 libstdc++6 \
       libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
       libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 \
       libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 \
       libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
       libfontconfig1 libfreetype6 fonts-noto-color-emoji fonts-unifont \
       fonts-liberation fonts-ipafont-gothic fonts-wqy-zenhei \
       fonts-tlwg-loma-otf fonts-freefont-ttf \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /usr/bin/pebble \
    && groupadd --gid 10001 renderer \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin renderer
COPY --from=browser /usr/bin/node /usr/bin/node
COPY --from=browser /opt/renderer/node_modules /opt/renderer/node_modules
COPY --from=browser /opt/chromium-runtime /ms-playwright
COPY scripts/native-renderer.mjs /opt/renderer/native-renderer.mjs
LABEL org.roundrobin.native-renderer="native-interface-browser-v1"
USER 10001:10001
ENTRYPOINT ["node", "/opt/renderer/native-renderer.mjs"]
