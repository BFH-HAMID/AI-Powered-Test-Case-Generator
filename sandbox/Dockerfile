FROM python:3.11-slim

LABEL maintainer="AI-Powered Test Case Generator"
LABEL description="Hardened non-root sandbox image for isolated dry-run execution of generated tests"

# Prevent interactive prompts and Python bytecode writes
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install minimal testing dependencies
RUN pip install --no-cache-dir \
    pytest==8.3.3 \
    pytest-cov==5.0.0 \
    defusedxml==0.7.1

# Create non-root sandbox user and workspace
RUN groupadd -g 10001 sandbox && \
    useradd -u 10001 -g sandbox -m -s /sbin/nologin sandbox && \
    mkdir -p /workspace /tmp/sandbox-out && \
    chown -R sandbox:sandbox /workspace /tmp/sandbox-out

WORKDIR /workspace
USER 10001:10001

ENTRYPOINT ["pytest"]
CMD ["-v", "--tb=short"]
