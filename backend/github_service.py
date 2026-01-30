"""GitHub integration service for LogsCrawler."""

import asyncio
from typing import Dict, List, Optional, Any
from datetime import datetime

import aiohttp
import structlog

from .config import GitHubConfig

logger = structlog.get_logger()


class GitHubService:
    """Service for interacting with GitHub API."""

    def __init__(self, config: GitHubConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            headers = {
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "LogsCrawler",
            }
            if self.config.token:
                headers["Authorization"] = f"token {self.config.token}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self):
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_starred_repos(self) -> List[Dict[str, Any]]:
        """Get list of starred repositories for the configured user.

        Returns:
            List of repo info dicts with name, full_name, description, url, etc.
        """
        if not self.config.token:
            logger.warning("GitHub token not configured")
            return []

        session = await self._get_session()

        # Use authenticated user's starred repos
        url = "https://api.github.com/user/starred"
        params = {"per_page": 100, "sort": "updated"}

        repos = []
        page = 1

        # Log token prefix for debugging (first 10 chars only for security)
        token_prefix = self.config.token[:10] if self.config.token else "none"
        logger.info("Fetching starred repos", token_prefix=f"{token_prefix}...", url=url)

        try:
            while True:
                params["page"] = page
                async with session.get(url, params=params) as response:
                    # Log response headers for debugging scopes
                    scopes = response.headers.get("X-OAuth-Scopes", "none")
                    rate_limit = response.headers.get("X-RateLimit-Remaining", "?")
                    logger.info("GitHub API response", 
                               status=response.status, 
                               page=page,
                               scopes=scopes, 
                               rate_limit=rate_limit)
                    
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error("GitHub API error", status=response.status, error=error_text)
                        break

                    data = await response.json()
                    logger.info("GitHub API data received", page=page, count=len(data) if data else 0)
                    if not data:
                        break

                    for repo in data:
                        repos.append({
                            "id": repo["id"],
                            "name": repo["name"],
                            "full_name": repo["full_name"],
                            "description": repo["description"] or "",
                            "html_url": repo["html_url"],
                            "ssh_url": repo["ssh_url"],
                            "clone_url": repo["clone_url"],
                            "language": repo["language"],
                            "stargazers_count": repo["stargazers_count"],
                            "updated_at": repo["updated_at"],
                            "owner": repo["owner"]["login"],
                            "private": repo["private"],
                        })

                    # Check if there are more pages
                    if len(data) < 100:
                        break
                    page += 1

            logger.info("Fetched starred repos", count=len(repos))
            return repos

        except Exception as e:
            logger.error("Failed to fetch starred repos", error=str(e))
            return []

    def is_configured(self) -> bool:
        """Check if GitHub integration is properly configured."""
        return bool(self.config.token)


class StackDeployer:
    """Service for building and deploying stacks from GitHub repos."""

    def __init__(self, config: GitHubConfig, host_client):
        """Initialize the deployer.

        Args:
            config: GitHub configuration
            host_client: Host client for executing commands (SSH or Docker)
        """
        self.config = config
        self.host_client = host_client

    async def _ensure_repo_cloned(self, repo_name: str, ssh_url: str) -> tuple[bool, str]:
        """Ensure the repository is cloned on the host.

        Args:
            repo_name: Name of the repository
            ssh_url: SSH URL for cloning

        Returns:
            Tuple of (success, message)
        """
        repos_path = self.config.repos_path
        repo_path = f"{repos_path}/{repo_name}"

        # Check if repo exists
        check_cmd = f"test -d {repo_path} && echo 'exists' || echo 'missing'"
        success, output = await self._run_command(check_cmd)

        if not success:
            return False, f"Failed to check repo existence: {output}"

        if "missing" in output:
            # Clone the repo
            logger.info("Cloning repository", repo=repo_name, path=repo_path)
            clone_cmd = f"mkdir -p {repos_path} && cd {repos_path} && git clone {ssh_url}"
            success, output = await self._run_command(clone_cmd)

            if not success:
                return False, f"Failed to clone repository: {output}"

            return True, f"Repository cloned successfully"
        else:
            # Pull latest changes
            logger.info("Pulling latest changes", repo=repo_name)
            pull_cmd = f"cd {repo_path} && git pull"
            success, output = await self._run_command(pull_cmd)

            if not success:
                # Non-fatal, continue with existing code
                logger.warning("Failed to pull latest", repo=repo_name, error=output)

            return True, "Repository already exists"

    async def _run_command(self, command: str) -> tuple[bool, str]:
        """Run a shell command on the host.

        Args:
            command: Shell command to run

        Returns:
            Tuple of (success, output)
        """
        try:
            # Use the host client to run the command
            # This works for both SSH and Docker clients
            if hasattr(self.host_client, 'run_shell_command'):
                return await self.host_client.run_shell_command(command)
            else:
                # Fallback: run locally using asyncio
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                output = stdout.decode() + stderr.decode()
                return proc.returncode == 0, output.strip()
        except Exception as e:
            return False, str(e)

    async def build(self, repo_name: str, ssh_url: str, version: str = "1.0") -> Dict[str, Any]:
        """Build a stack from a repository.

        Args:
            repo_name: Name of the repository
            ssh_url: SSH URL for cloning if needed
            version: Version tag for the build

        Returns:
            Dict with success status, output, and timing info
        """
        start_time = datetime.utcnow()
        result = {
            "action": "build",
            "repo": repo_name,
            "version": version,
            "success": False,
            "output": "",
            "started_at": start_time.isoformat(),
            "completed_at": None,
            "duration_seconds": 0,
        }

        try:
            # Ensure repo is cloned
            clone_success, clone_msg = await self._ensure_repo_cloned(repo_name, ssh_url)
            if not clone_success:
                result["output"] = clone_msg
                return result

            # Run build script
            scripts_path = self.config.scripts_path
            build_cmd = f"cd {scripts_path} && bash build-push.sh {repo_name} {version}"

            logger.info("Running build", repo=repo_name, version=version)
            success, output = await self._run_command(build_cmd)

            result["success"] = success
            result["output"] = f"{clone_msg}\n\n{output}" if clone_msg else output

        except Exception as e:
            result["output"] = str(e)
            logger.error("Build failed", repo=repo_name, error=str(e))

        end_time = datetime.utcnow()
        result["completed_at"] = end_time.isoformat()
        result["duration_seconds"] = (end_time - start_time).total_seconds()

        return result

    async def deploy(self, repo_name: str, ssh_url: str, version: str = "1.0") -> Dict[str, Any]:
        """Deploy a stack from a repository.

        Args:
            repo_name: Name of the repository
            ssh_url: SSH URL for cloning if needed
            version: Version tag for deployment

        Returns:
            Dict with success status, output, and timing info
        """
        start_time = datetime.utcnow()
        result = {
            "action": "deploy",
            "repo": repo_name,
            "version": version,
            "success": False,
            "output": "",
            "started_at": start_time.isoformat(),
            "completed_at": None,
            "duration_seconds": 0,
        }

        try:
            # Ensure repo is cloned
            clone_success, clone_msg = await self._ensure_repo_cloned(repo_name, ssh_url)
            if not clone_success:
                result["output"] = clone_msg
                return result

            # Run deploy script
            scripts_path = self.config.scripts_path
            deploy_cmd = f"cd {scripts_path} && bash deploy-service.sh {repo_name} {version}"

            logger.info("Running deploy", repo=repo_name, version=version)
            success, output = await self._run_command(deploy_cmd)

            result["success"] = success
            result["output"] = f"{clone_msg}\n\n{output}" if clone_msg else output

        except Exception as e:
            result["output"] = str(e)
            logger.error("Deploy failed", repo=repo_name, error=str(e))

        end_time = datetime.utcnow()
        result["completed_at"] = end_time.isoformat()
        result["duration_seconds"] = (end_time - start_time).total_seconds()

        return result
