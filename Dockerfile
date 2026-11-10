FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/intelligence
COPY pyproject.toml MANIFEST.in README.md LICENSE ./
COPY config/ ./config/
COPY src/ ./src/
COPY skills/investment-intelligence/SKILL.md ./skills/investment-intelligence/
RUN pip install --no-cache-dir '.[cloud]' \
    && useradd --create-home --uid 10001 intelligence \
    && mkdir -p /srv/intelligence/data /srv/intelligence/output \
    && chown -R intelligence:intelligence /srv/intelligence
WORKDIR /srv/intelligence
USER intelligence
CMD ["python", "-m", "investment_intelligence", "serve"]
