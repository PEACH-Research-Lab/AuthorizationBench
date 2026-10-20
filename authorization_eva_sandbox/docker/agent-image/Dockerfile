FROM ghcr.io/openhands/agent-server:latest-python
LABEL org.opencontainers.image.title="AuthorizationBench OpenHands 1.49.2"
LABEL authorizationbench.harness="openhands-sdk-1.49.2"

# Agents render PDFs with chromium, then read them back with pdftotext/pdfinfo.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*
USER openhands
