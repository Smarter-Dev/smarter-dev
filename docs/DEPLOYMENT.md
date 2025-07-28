# Kubernetes Deployment Guide

This guide explains how to deploy the Smarter Dev application to a Kubernetes cluster with separate web and bot containers.

## Architecture

The application consists of two main services:
- **Web Service**: Starlette/FastAPI web application with admin interface
- **Bot Service**: Discord bot for community interaction

Both services share the same database and Redis instance but run as independent containers.

## Prerequisites

1. Kubernetes cluster (Digital Ocean recommended)
2. kubectl configured to access your cluster
3. Docker Hub account for container registry
4. Required secrets and credentials

## Required Secrets

Before deploying, you must create the following secrets in your cluster:

### 1. Create the secrets file

Copy the template and fill in your actual values:

```bash
cp k8s/secrets.template.yaml k8s/secrets.yaml
```

Edit `k8s/secrets.yaml` and replace ALL placeholder values with actual secrets:

- `database-url`: PostgreSQL connection string
- `redis-url`: Redis connection string  
- `discord-bot-token`: Discord bot token from Discord Developer Portal
- `discord-application-id`: Discord application ID
- `discord-client-id`: Discord OAuth client ID (for admin interface)
- `discord-client-secret`: Discord OAuth client secret
- `api-secret-key`: Strong random string for API authentication
- `web-session-secret`: Strong random string for web sessions
- `admin-username`: Admin interface username
- `admin-password`: Secure admin password

### 2. Apply secrets to cluster

```bash
kubectl apply -f k8s/secrets.yaml
```

**⚠️ Important**: Never commit `k8s/secrets.yaml` to version control. It contains sensitive credentials.

## Database Setup

The application requires PostgreSQL and Redis. You can use:

### Option 1: Managed Services (Recommended for Production)
- Digital Ocean Managed PostgreSQL
- Digital Ocean Managed Redis
- Update connection strings in secrets accordingly

### Option 2: In-Cluster Services
Deploy PostgreSQL and Redis to your cluster (not covered in this guide).

## GitHub Actions Setup

The CI/CD pipeline requires these GitHub Secrets:

1. **DIGITALOCEAN_ACCESS_TOKEN**: DigitalOcean API token
2. **DOCKER_USERNAME**: Docker Hub username
3. **DOCKER_PASSWORD**: Docker Hub password or access token

Configure these in your GitHub repository settings under Secrets and Variables.

## Manual Deployment

If deploying manually without GitHub Actions:

### 1. Build and push containers

```bash
# Get commit SHA for tagging
TAG=$(git rev-parse --short HEAD)

# Build containers
docker build -f Dockerfile.web -t zzmmrmn/smarter-dev-website:$TAG .
docker build -f Dockerfile.bot -t zzmmrmn/smarter-dev-bot:$TAG .

# Push to registry
docker push zzmmrmn/smarter-dev-website:$TAG
docker push zzmmrmn/smarter-dev-bot:$TAG
```

### 2. Update deployment files

```bash
# Update image tags in deployment files
sed -i 's|<IMAGE_VERSION>|'${TAG}'|' k8s/deploy.yaml
sed -i 's|<IMAGE_VERSION>|'${TAG}'|' k8s/deploy-bot.yaml
```

### 3. Deploy to cluster

```bash
# Create namespace
kubectl apply -f k8s/namespace.yaml

# Apply configuration
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secrets.yaml  # Make sure this exists and is configured

# Deploy services
kubectl apply -f k8s/deploy.yaml
kubectl apply -f k8s/deploy-bot.yaml
kubectl apply -f k8s/site.yaml

# Verify deployment
kubectl rollout status deployment/smarter-dev-website
kubectl rollout status deployment/smarter-dev-bot
```

## Monitoring and Troubleshooting

### Check pod status
```bash
kubectl get pods -n smarter-dev
```

### View logs
```bash
# Web service logs
kubectl logs -f deployment/smarter-dev-website -n smarter-dev

# Bot service logs
kubectl logs -f deployment/smarter-dev-bot -n smarter-dev
```

### Health checks
The web service exposes a health endpoint at `/api/health` that Kubernetes uses for health checks.

The bot service uses process-based health checks since it doesn't expose HTTP endpoints.

## Scaling

### Web Service
Can be scaled horizontally:
```bash
kubectl scale deployment smarter-dev-website --replicas=3 -n smarter-dev
```

### Bot Service
Should remain at 1 replica to avoid Discord API conflicts:
```bash
# Keep at 1 replica
kubectl scale deployment smarter-dev-bot --replicas=1 -n smarter-dev
```

## Environment Variables

All configuration is handled through environment variables defined in:
- `k8s/configmap.yaml`: Non-sensitive configuration
- `k8s/secrets.yaml`: Sensitive credentials

The application uses Pydantic Settings to automatically load these from the environment.

## Security Considerations

1. **Never commit secrets**: Keep `k8s/secrets.yaml` out of version control
2. **Use strong passwords**: Generate random strings for API keys and sessions
3. **Limit permissions**: Use service accounts with minimal required permissions
4. **Network policies**: Consider implementing network policies to restrict pod communication
5. **Resource limits**: Set appropriate CPU and memory limits to prevent resource exhaustion

## Troubleshooting Common Issues

### Bot not connecting to Discord
- Verify `DISCORD_BOT_TOKEN` is correct
- Check bot has necessary permissions in Discord server
- Ensure `DISCORD_APPLICATION_ID` matches your Discord application

### Database connection errors
- Verify `DATABASE_URL` format and credentials
- Check network connectivity to database
- Ensure database exists and is accessible

### Admin interface not working
- Check `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET`
- Verify `DISCORD_REDIRECT_URI` matches your OAuth app settings
- Ensure admin credentials are set correctly