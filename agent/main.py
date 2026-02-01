"""LogsCrawler Agent - Main entry point.

This agent runs on each host and:
1. Collects Docker container logs and metrics locally
2. Writes data directly to OpenSearch
3. Polls the backend for actions to execute (start, stop, exec, etc.)
"""

import asyncio
import signal
import sys

import structlog

from .config import load_agent_config
from .docker_collector import DockerCollector
from .opensearch_writer import OpenSearchWriter
from .action_poller import ActionPoller

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


class Agent:
    """Main agent class that orchestrates collection and action polling."""

    def __init__(self):
        self.config = load_agent_config()
        self._running = False
        self._tasks = []

        # Initialize components
        self.docker = DockerCollector(
            docker_url=self.config.docker_url,
            host_name=self.config.agent_id,
        )
        self.opensearch = OpenSearchWriter(self.config.opensearch)
        self.action_poller = ActionPoller(
            backend_url=self.config.backend_url,
            agent_id=self.config.agent_id,
            docker_collector=self.docker,
            poll_interval=self.config.action_poll_interval,
        )

    async def start(self):
        """Start the agent."""
        logger.info(
            "Starting LogsCrawler Agent",
            agent_id=self.config.agent_id,
            backend_url=self.config.backend_url,
            opensearch_hosts=self.config.opensearch.hosts,
        )

        # Initialize OpenSearch indices
        await self.opensearch.initialize()

        self._running = True

        # Start background tasks
        self._tasks = [
            asyncio.create_task(self._log_collection_loop()),
            asyncio.create_task(self._metrics_collection_loop()),
            asyncio.create_task(self.action_poller.run()),
        ]

        logger.info("Agent started successfully")

        # Wait for all tasks
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("Agent tasks cancelled")

    async def stop(self):
        """Stop the agent gracefully."""
        logger.info("Stopping agent...")
        self._running = False
        self.action_poller.stop()

        # Cancel all tasks
        for task in self._tasks:
            task.cancel()

        # Wait for tasks to complete
        await asyncio.gather(*self._tasks, return_exceptions=True)

        # Close connections
        await self.docker.close()
        await self.opensearch.close()
        await self.action_poller.close()

        logger.info("Agent stopped")

    async def _log_collection_loop(self):
        """Periodically collect logs from all containers."""
        logger.info("Log collection loop started", interval=self.config.log_interval)

        while self._running:
            try:
                logs = await self.docker.collect_all_logs(
                    tail=self.config.log_lines_per_fetch
                )

                if logs:
                    await self.opensearch.index_logs(logs)
                    logger.debug("Collected logs", count=len(logs))

            except Exception as e:
                logger.error("Log collection error", error=str(e))

            await asyncio.sleep(self.config.log_interval)

    async def _metrics_collection_loop(self):
        """Periodically collect metrics from host and containers."""
        logger.info("Metrics collection loop started", interval=self.config.metrics_interval)

        while self._running:
            try:
                host_metrics, container_stats = await self.docker.collect_all_stats()

                # Index host metrics
                await self.opensearch.index_host_metrics(host_metrics)

                # Index container stats
                for stats in container_stats:
                    await self.opensearch.index_container_stats(stats)

                logger.debug(
                    "Collected metrics",
                    host_cpu=host_metrics.get("cpu_percent"),
                    containers=len(container_stats),
                )

            except Exception as e:
                logger.error("Metrics collection error", error=str(e))

            await asyncio.sleep(self.config.metrics_interval)


async def main():
    """Main entry point."""
    agent = Agent()

    # Setup signal handlers for graceful shutdown
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(agent.stop())

    # Handle SIGINT and SIGTERM
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await agent.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        await agent.stop()
    except Exception as e:
        logger.error("Agent failed", error=str(e))
        await agent.stop()
        sys.exit(1)


def run():
    """Entry point for console script."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
