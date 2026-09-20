FROM ubuntu:24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/ubuntu
USER ubuntu
ENV HOME=/home/ubuntu \
    USER=ubuntu

CMD ["bash", "-l"]
