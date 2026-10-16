ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends strace \
    && rm -rf /var/lib/apt/lists/*
USER 501:20
ARG BASE_IMAGE_ID
LABEL org.obp.observer.base-image-id=${BASE_IMAGE_ID}
