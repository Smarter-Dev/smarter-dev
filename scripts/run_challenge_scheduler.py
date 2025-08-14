#!/usr/bin/env python3
"""Challenge scheduler runner script.

This script runs the automated challenge scheduling service as a standalone process.
It continuously monitors active campaigns and releases challenges based on their
scheduled release times.

Usage:
    python scripts/run_challenge_scheduler.py

The scheduler will run continuously until interrupted (Ctrl+C).
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add the project root to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from smarter_dev.services.challenge_scheduler import run_challenge_scheduler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/scheduler.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


async def main():
    """Main entry point for the challenge scheduler."""
    logger.info("Starting challenge scheduler service...")
    
    try:
        await run_challenge_scheduler()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user")
    except Exception as e:
        logger.error(f"Scheduler error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())