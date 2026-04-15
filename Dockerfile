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
COPY etf_sentiment/ etf_sentiment/

# Pre-download FinBERT model so it's baked into the image
RUN python -c "from transformers import pipeline; pipeline('sentiment-analysis', model='ProsusAI/finbert', device=-1)"

# Default: cloud mode (save to S3 + email)
ENTRYPOINT ["python", "run.py"]
CMD ["--cloud"]
