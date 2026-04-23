FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt boto3

# Copy app code
COPY run.py .
COPY web.py .
COPY stock_sentiment/ stock_sentiment/
COPY templates/ templates/
COPY static/ static/

# Pre-download FinBERT model so it's baked into the image
RUN python -c "from transformers import pipeline; pipeline('sentiment-analysis', model='ProsusAI/finbert', device=-1)"

# Expose the port App Runner will use
EXPOSE 8080

# Default: Run the FastAPI web server
ENTRYPOINT ["uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8080"]
