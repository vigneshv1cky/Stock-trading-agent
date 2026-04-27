FROM python:3.11-slim

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY run.py .
COPY web.py .
COPY stock_sentiment/ stock_sentiment/
COPY templates/ templates/
COPY static/ static/

# Expose the port App Runner will use
EXPOSE 8080

# Default: Run the FastAPI web server
ENTRYPOINT ["uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8080"]
