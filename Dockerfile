FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY . .

# Fail the image build early if a publishable Python module is malformed.
RUN python3 -m py_compile server.py note_knowledge/*.py career_copilot/*.py

EXPOSE 7860

CMD ["sh", "-c", "python3 server.py --host 0.0.0.0 --port ${PORT:-7860} --vault-root demo_vault"]
