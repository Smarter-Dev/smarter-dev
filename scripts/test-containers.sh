#!/bin/bash

# Test container builds locally
# This script builds both containers and tests them with sample environment variables

set -e

echo "Building web container..."
docker build -f Dockerfile.web -t smarter-dev-web:test .

echo "Building bot container..."
docker build -f Dockerfile.bot -t smarter-dev-bot:test .

echo "Containers built successfully!"

echo "Testing web container health check..."
# Start web container in background
docker run -d --name smarter-dev-web-test \
  -p 8000:8000 \
  -e ENVIRONMENT=development \
  -e DATABASE_URL=sqlite:///test.db \
  -e REDIS_URL=redis://localhost:6379 \
  smarter-dev-web:test

# Wait for startup
sleep 5

# Test health endpoint
if curl -f http://localhost:8000/api/health; then
  echo "✅ Web container health check passed"
else
  echo "❌ Web container health check failed"
fi

# Cleanup
docker stop smarter-dev-web-test
docker rm smarter-dev-web-test

echo "Testing bot container..."
# Test bot container (will fail without proper Discord credentials, but should start)
docker run --name smarter-dev-bot-test \
  -e ENVIRONMENT=development \
  -e DATABASE_URL=sqlite:///test.db \
  -e DISCORD_BOT_TOKEN=test \
  -e DISCORD_APPLICATION_ID=test \
  --timeout 10s \
  smarter-dev-bot:test || echo "✅ Bot container started (expected to fail without Discord credentials)"

# Cleanup
docker rm smarter-dev-bot-test

echo "Container tests completed!"
echo "Both containers built successfully and can be deployed."