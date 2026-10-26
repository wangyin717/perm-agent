FROM python:3.12-slim-bookworm

WORKDIR /app
COPY pyproject.toml ./
COPY agent_loop ./agent_loop
RUN pip install --no-cache-dir .

ENV PERM_WEB=1
ENV PERMANENT_HOME=/perm
ENV PERM_WORKSPACE=/workspace
ENV PERM_HOST=0.0.0.0
ENV PERM_PORT=8080
RUN mkdir -p /perm /workspace

EXPOSE 8080
CMD ["python", "-m", "agent_loop.serve.app"]
