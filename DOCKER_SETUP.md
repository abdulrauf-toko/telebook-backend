# Docker Compose Usage Guide for Voice Orchestrator

## Quick Start

1. **Copy environment file:**
   ```bash
   cp .env.example .env
   ```

2. **Build and start services:**
   ```bash
   docker-compose up -d
   ```

3. **Run migrations:**
   ```bash
   docker-compose exec django python manage.py migrate
   ```

4. **Create superuser:**
   ```bash
   docker-compose exec django python manage.py createsuperuser
   ```

5. **Verify services are running:**
   ```bash
   docker-compose ps
   ```

## Service Details

### PostgreSQL (Port 5432)
- Database for Django models
- Credentials from .env file
- Data persisted in `postgres_data` volume

### Redis (Port 6379)
- Celery broker
- Cache backend
- Agent/call state management
- Data persisted in `redis_data` volume

### Django (Port 8000)
- Main application server
- Admin panel at http://localhost:8000/admin
- Runs migrations on startup
- Depends on PostgreSQL and Redis health

### Celery Worker
- Processes async tasks
- Handles FreeSWITCH events
- Dialer orchestration logic
- Runs with 4 concurrent workers

### Celery Beat
- Periodic task scheduler
- Triggers dialer cycles
- Requires `django-celery-beat` package

## Common Commands

### View logs
```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f django
docker-compose logs -f celery_worker
```

### Database commands
```bash
# Makemigrations
docker-compose exec django python manage.py makemigrations

# Migrate
docker-compose exec django python manage.py migrate

# Shell
docker-compose exec django python manage.py shell
```

### Celery commands
```bash
# Check worker status
docker-compose exec celery_worker celery -A CELERY_INIT inspect active

# Purge all tasks
docker-compose exec celery_worker celery -A CELERY_INIT purge
```

### Redis commands
```bash
# Access Redis CLI
docker-compose exec redis redis-cli

# Inside Redis:
# Check all keys: KEYS *
# Get agent states: HGETALL AGENT_STATES
# Get active calls: HGETALL SALES_ACTIVE_CALLS
```

### Stop all services
```bash
docker-compose down
```

### Stop and remove volumes
```bash
docker-compose down -v
```

## Testing the Flow

1. **Access Django shell:**
   ```bash
   docker-compose exec django python manage.py shell
   ```

2. **Add agents to queue:**
   ```python
   from dialer.utils import add_sales_agent_to_queue
   add_sales_agent_to_queue('agent_1')
   add_sales_agent_to_queue('agent_2')
   ```

3. **Verify in Redis:**
   ```bash
   docker-compose exec redis redis-cli
   > ZRANGE SALES_AGENT_QUEUE 0 -1
   ```

4. **Trigger dialer cycle:**
   ```python
   from dialer.tasks import initiate_dialer_cycle
   initiate_dialer_cycle.delay()
   ```

5. **Monitor Celery worker logs:**
   ```bash
   docker-compose logs -f celery_worker
   ```

## Troubleshooting

### Services won't start
- Check .env file exists and has correct values
- Ensure ports 5432, 6379, 8000 are available
- Run `docker-compose logs` to see error details

### Database connection errors
- Ensure PostgreSQL is healthy: `docker-compose ps`
- Run migrations: `docker-compose exec django python manage.py migrate`

### Celery tasks not running
- Check Redis is running: `docker-compose exec redis redis-cli ping`
- Check worker logs: `docker-compose logs celery_worker`
- Verify broker URL in settings.py

### Permission errors
- Check volume permissions: `ls -la` on mounted directories
- May need to rebuild: `docker-compose down && docker-compose up --build`
