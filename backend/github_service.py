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

    def __init__(self, config: GitHubConfig, host_client=None):
        """Initialize the deployer.

        Args:
            config: GitHub configuration
            host_client: Host client for executing commands (fallback if no SSH configured)
        """
        self.config = config
        self.host_client = host_client
        self._ssh_client = None

    async def _get_ssh_client(self):
        """Get or create SSH client for host commands."""
        if self._ssh_client is not None:
            return self._ssh_client
        
        # If SSH host is configured, use SSH
        if self.config.ssh_host:
            from .ssh_client import SSHClient
            from .config import HostConfig
            
            ssh_config = HostConfig(
                name="github-deploy-host",
                hostname=self.config.ssh_host,
                port=self.config.ssh_port,
                username=self.config.ssh_user,
                ssh_key_path=self.config.ssh_key_path,
                mode="ssh"
            )
            self._ssh_client = SSHClient(ssh_config)
            logger.info("Using SSH for stack operations", 
                       host=self.config.ssh_host, 
                       user=self.config.ssh_user)
        
        return self._ssh_client

    async def close(self):
        """Close SSH connection if open."""
        if self._ssh_client:
            await self._ssh_client.close()
            self._ssh_client = None

    async def _ensure_git_configured(self) -> None:
        """Ensure git is configured with user name and email."""
        if self.config.username:
            await self._run_command(f"git config --global user.name '{self.config.username}'")
        if self.config.useremail:
            await self._run_command(f"git config --global user.email '{self.config.useremail}'")

    async def _ensure_docker_login(self) -> None:
        """Ensure docker is logged in to the registry."""
        if self.config.registry_url and self.config.registry_username and self.config.registry_password:
            # Use echo to pipe password to avoid it showing in command history
            login_cmd = f"echo '{self.config.registry_password}' | docker login {self.config.registry_url} -u '{self.config.registry_username}' --password-stdin"
            success, output = await self._run_command(login_cmd)
            if success:
                logger.info("Docker login successful", registry=self.config.registry_url)
            else:
                logger.warning("Docker login failed", registry=self.config.registry_url, error=output)

    async def _ensure_repo_cloned(self, repo_name: str, ssh_url: str) -> tuple[bool, str]:
        """Ensure the repository is cloned and updated on the host.

        If the repo exists, it will be updated with git fetch + reset to match remote.
        If a directory exists but is not a git repo, it will be backed up, cloned,
        and config files (.env, etc.) will be restored.

        Args:
            repo_name: Name of the repository
            ssh_url: SSH URL for cloning

        Returns:
            Tuple of (success, message)
        """
        # Ensure git is configured before any git operations
        await self._ensure_git_configured()
        
        repos_path = self.config.repos_path
        repo_path = f"{repos_path}/{repo_name}"
        backup_path = f"{repo_path}.backup.{int(__import__('time').time())}"

        # Check if directory exists
        check_dir_cmd = f"test -d {repo_path} && echo 'dir_exists' || echo 'dir_missing'"
        success, dir_output = await self._run_command(check_dir_cmd)

        if not success:
            return False, f"Failed to check directory existence: {dir_output}"

        # Check if it's a valid git repo
        check_git_cmd = f"test -d {repo_path}/.git && echo 'is_git' || echo 'not_git'"
        success, git_output = await self._run_command(check_git_cmd)

        if "dir_missing" in dir_output:
            # Directory doesn't exist - simple clone
            logger.info("Cloning repository", repo=repo_name, path=repo_path)
            clone_cmd = f"mkdir -p {repos_path} && cd {repos_path} && git clone {ssh_url}"
            success, output = await self._run_command(clone_cmd)

            if not success:
                return False, f"Failed to clone repository: {output}"

            return True, "Repository cloned successfully"

        elif "not_git" in git_output:
            # Directory exists but is not a git repo - backup, clone, restore configs
            logger.info("Directory exists but not a git repo, backing up and cloning", 
                       repo=repo_name, backup=backup_path)
            
            # 1. Rename existing directory to backup
            rename_cmd = f"mv {repo_path} {backup_path}"
            success, output = await self._run_command(rename_cmd)
            if not success:
                return False, f"Failed to backup existing directory: {output}"
            
            # 2. Clone the repo
            clone_cmd = f"cd {repos_path} && git clone {ssh_url}"
            success, output = await self._run_command(clone_cmd)
            if not success:
                # Restore backup if clone failed
                await self._run_command(f"mv {backup_path} {repo_path}")
                return False, f"Failed to clone repository: {output}"
            
            # 3. Copy config files from backup (devops/.env, .env, etc.)
            restore_cmd = f"""
                if [ -f {backup_path}/devops/.env ]; then
                    mkdir -p {repo_path}/devops && cp {backup_path}/devops/.env {repo_path}/devops/.env
                fi
                if [ -f {backup_path}/.env ]; then
                    cp {backup_path}/.env {repo_path}/.env
                fi
            """
            await self._run_command(restore_cmd)
            
            # 4. Remove backup
            await self._run_command(f"rm -rf {backup_path}")
            
            return True, "Repository cloned (config files restored from backup)"

        else:
            # Valid git repository - add to safe.directory and force update
            logger.info("Updating repository", repo=repo_name)
            
            # Add repo to git safe.directory to avoid ownership issues
            safe_dir_cmd = f"git config --global --add safe.directory {repo_path}"
            await self._run_command(safe_dir_cmd)
            
            # Fetch latest and reset to origin (preserves untracked files like .env)
            update_cmd = f"cd {repo_path} && git fetch origin && git reset --hard origin/$(git rev-parse --abbrev-ref HEAD)"
            success, output = await self._run_command(update_cmd)

            if not success:
                # Non-fatal, continue with existing code
                logger.warning("Failed to update repo", repo=repo_name, error=output)
                return True, f"Repository exists (update failed: {output})"

            return True, "Repository updated successfully"

    async def _run_command(self, command: str) -> tuple[bool, str]:
        """Run a shell command on the host.

        Prefers SSH if configured (for running on Docker host from container).
        Falls back to host_client or local execution.

        Args:
            command: Shell command to run

        Returns:
            Tuple of (success, output)
        """
        try:
            # First, try to use SSH if configured (for executing on host from container)
            ssh_client = await self._get_ssh_client()
            if ssh_client:
                try:
                    return await ssh_client.run_shell_command(command)
                except OSError as e:
                    # Handle DNS/network resolution errors
                    if e.errno == -2 or "Name or service not known" in str(e):
                        error_msg = f"SSH host '{self.config.ssh_host}' cannot be resolved. Check LOGSCRAWLER_GITHUB__SSH_HOST configuration."
                        logger.error("SSH host resolution failed", host=self.config.ssh_host, error=str(e))
                        return False, error_msg
                    raise
            
            # Fallback: use the host client if available
            if self.host_client and hasattr(self.host_client, 'run_shell_command'):
                return await self.host_client.run_shell_command(command)
            
            # Last resort: run locally using asyncio
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            output = stdout.decode() + stderr.decode()
            return proc.returncode == 0, output.strip()
        except Exception as e:
            logger.error("Command execution failed", command=command[:80], error=str(e))
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
            # Ensure docker is logged in to registry
            await self._ensure_docker_login()
            
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

    async def get_env_file(self, repo_name: str) -> tuple[bool, str]:
        """Get the content of the .env file for a repository.

        Args:
            repo_name: Name of the repository

        Returns:
            Tuple of (success, content_or_error)
        """
        repos_path = self.config.repos_path
        env_path = f"{repos_path}/{repo_name}/devops/.env"

        # Check if file exists
        check_cmd = f"test -f {env_path} && echo 'exists' || echo 'missing'"
        success, output = await self._run_command(check_cmd)

        if not success:
            return False, f"Failed to check .env file: {output}"

        if "missing" in output:
            return True, ""  # Return empty content if file doesn't exist

        # Read the file content
        read_cmd = f"cat {env_path}"
        success, output = await self._run_command(read_cmd)

        if not success:
            return False, f"Failed to read .env file: {output}"

        return True, output

    async def save_env_file(self, repo_name: str, content: str) -> tuple[bool, str]:
        """Save the content of the .env file for a repository.

        Args:
            repo_name: Name of the repository
            content: The content to write to the .env file

        Returns:
            Tuple of (success, message)
        """
        repos_path = self.config.repos_path
        env_path = f"{repos_path}/{repo_name}/devops/.env"
        devops_dir = f"{repos_path}/{repo_name}/devops"

        # Ensure devops directory exists
        mkdir_cmd = f"mkdir -p {devops_dir}"
        await self._run_command(mkdir_cmd)

        # Write the content using a heredoc to handle special characters
        # Escape single quotes in content
        escaped_content = content.replace("'", "'\\''")
        write_cmd = f"cat > {env_path} << 'ENVEOF'\n{content}\nENVEOF"

        success, output = await self._run_command(write_cmd)

        if not success:
            return False, f"Failed to write .env file: {output}"

        return True, "File saved successfully"
