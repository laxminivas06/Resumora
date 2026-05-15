# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Install system dependencies for Playwright and other tools
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    librandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium

# Copy the rest of the application code
COPY . .

# Set working directory to the app folder
WORKDIR /app/resume_builder

# Make port 8080 available to the world outside this container
EXPOSE 8080

# Run the app using gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"]
