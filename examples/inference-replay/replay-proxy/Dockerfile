# Build args so this can be rebuilt from a mirrored base; see
# deploy/docker/Dockerfile for the reasoning. Defaults are upstream.
ARG PYTHON_IMAGE=python
ARG PYTHON_VERSION=3.14-slim
FROM ${PYTHON_IMAGE}:${PYTHON_VERSION}
WORKDIR /app
ARG CACHED_RESPONSE_REF=f5d1b16ed594da99ecec323b411a4376a27065b3
RUN pip install fastapi uvicorn httpx \
    "cached-response @ https://github.com/haoxuw/cached-response/archive/${CACHED_RESPONSE_REF}.tar.gz"
COPY replay_proxy.py .
EXPOSE 8080
CMD ["uvicorn", "replay_proxy:app", "--host", "0.0.0.0", "--port", "8080"]
