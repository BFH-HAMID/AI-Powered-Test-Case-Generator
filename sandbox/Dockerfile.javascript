FROM node:20-alpine

LABEL maintainer="AI-Powered Test Case Generator"
LABEL description="Non-root Node.js runtime for isolated generated-test dry-runs"

RUN addgroup -g 10001 sandbox && adduser -D -u 10001 -G sandbox sandbox && \
    mkdir -p /workspace /tmp && chown -R sandbox:sandbox /workspace /tmp

WORKDIR /workspace
USER 10001:10001
ENTRYPOINT ["node"]
