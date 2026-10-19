FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv
FROM python:3.13.16-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=0
COPY pyproject.toml uv.lock ./
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
RUN uv sync --frozen --no-dev --no-editable
RUN useradd --create-home tracemind
USER tracemind
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
