"""GitHub integration service for LogsCrawler."""

import asyncio
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

import aiohttp
import structlog

from .config import GitHubConfig

logger = structlog.get_logger()

# Cache TTL for starred repos (1 minute)
STARRED_REPOS_CACHE_TTL = timedelta(minutes=1)


class GitHubService:
    """Service for interacting with GitHub API."""

    def __init__(self, config: GitHubConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        # Cache for starred repos
        self._starred_repos_cache: Optional[List[Dict[str, Any]]] = None
        self._starred_repos_cache_time: Optional[datetime] = None

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

    def _is_cache_valid(self) -> bool:
        """Check if the starred repos cache is still valid."""
        if self._starred_repos_cache is None or self._starred_repos_cache_time is None:
            return False
        return datetime.now() - self._starred_repos_cache_time < STARRED_REPOS_CACHE_TTL

    def invalidate_cache(self):
        """Invalidate the starred repos cache."""
        self._starred_repos_cache = None
        self._starred_repos_cache_time = None
        logger.info("Starred repos cache invalidated")

    async def get_starred_repos(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Get list of starred repositories for the configured user.

        Args:
            force_refresh: If True, bypass cache and fetch fresh data.

        Returns:
            List of repo info dicts with name, full_name, description, url, etc.
        """
        # Check cache first (unless force refresh requested)
        if not force_refresh and self._is_cache_valid():
            logger.debug("Returning cached starred repos", count=len(self._starred_repos_cache))
            return self._starred_repos_cache

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

            # Update cache
            self._starred_repos_cache = repos
            self._starred_repos_cache_time = datetime.now()
            logger.info("Fetched and cached starred repos", count=len(repos))
            return repos

        except Exception as e:
            logger.error("Failed to fetch starred repos", error=str(e))
            return []

    def is_configured(self) -> bool:
        """Check if GitHub integration is properly configured."""
        return bool(self.config.token)

    async def get_repo_branches(self, owner: str, repo: str) -> List[Dict[str, Any]]:
        """Get list of branches for a repository.

        Args:
            owner: Repository owner (user or org)
            repo: Repository name

        Returns:
            List of branch info dicts with name, commit sha, etc.
        """
        if not self.config.token:
            logger.warning("GitHub token not configured")
            return []

        session = await self._get_session()
        url = f"https://api.github.com/repos/{owner}/{repo}/branches"
        params = {"per_page": 100}

        branches = []

        try:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error("GitHub API error getting branches", status=response.status, error=error_text)
                    return []

                data = await response.json()
                for branch in data:
                    branches.append({
                        "name": branch["name"],
                        "sha": branch["commit"]["sha"],
                        "protected": branch.get("protected", False),
                    })

            # Sort branches: main/master first, then alphabetically
            def branch_sort_key(b):
                name = b["name"].lower()
                if name == "main":
                    return (0, name)
                elif name == "master":
                    return (1, name)
                else:
                    return (2, name)

            branches.sort(key=branch_sort_key)
            logger.info("Fetched branches", repo=f"{owner}/{repo}", count=len(branches))
            return branches

        except Exception as e:
            logger.error("Failed to fetch branches", repo=f"{owner}/{repo}", error=str(e))
            return []

    async def get_repo_tags(self, owner: str, repo: str, limit: int = 20) -> Dict[str, Any]:
        """Get list of tags for a repository, grouped by the branch they were created from.

        Args:
            owner: Repository owner (user or org)
            repo: Repository name
            limit: Maximum number of tags to return

        Returns:
            Dict with tags grouped by branch and metadata
        """
        if not self.config.token:
            logger.warning("GitHub token not configured")
            return {"tags": [], "branches": {}}

        session = await self._get_session()

        # Get all tags with their commit info
        tags_url = f"https://api.github.com/repos/{owner}/{repo}/tags"
        tags = []

        try:
            async with session.get(tags_url, params={"per_page": min(limit, 100)}) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error("GitHub API error getting tags", status=response.status, error=error_text)
                    return {"tags": [], "branches": {}}

                data = await response.json()
                
                # Fetch commit dates for each tag (in parallel for performance)
                async def get_tag_with_date(tag_data):
                    tag_info = {
                        "name": tag_data["name"],
                        "sha": tag_data["commit"]["sha"],
                        "zipball_url": tag_data.get("zipball_url"),
                        "created_at": None,
                    }
                    # Get commit info to retrieve the date
                    commit_url = tag_data["commit"]["url"]
                    try:
                        async with session.get(commit_url) as commit_response:
                            if commit_response.status == 200:
                                commit_data = await commit_response.json()
                                # Use committer date (when the commit was applied)
                                tag_info["created_at"] = commit_data.get("commit", {}).get("committer", {}).get("date")
                    except Exception as e:
                        logger.debug("Could not fetch commit date for tag", tag=tag_data["name"], error=str(e))
                    return tag_info
                
                # Fetch all tag dates in parallel
                import asyncio
                tag_tasks = [get_tag_with_date(tag) for tag in data[:limit]]
                tags = await asyncio.gather(*tag_tasks)

            # Get branches to associate tags with branches
            branches = await self.get_repo_branches(owner, repo)
            branch_shas = {b["sha"]: b["name"] for b in branches}

            # Try to get commit info for each tag to find which branch it belongs to
            # This is a simplified approach - we'll group tags by checking if they match branch tips
            # or by extracting branch info from commit history
            tags_by_branch = {"main": [], "other": []}

            for tag in tags:
                # For now, put all tags in a list - the frontend will display them
                # In a more sophisticated implementation, we could trace the commit history
                tags_by_branch["main"].append(tag)

            logger.info("Fetched tags", repo=f"{owner}/{repo}", count=len(tags))
            return {
                "tags": tags,
                "branches": [b["name"] for b in branches],
                "default_branch": branches[0]["name"] if branches else "main",
            }

        except Exception as e:
            logger.error("Failed to fetch tags", repo=f"{owner}/{repo}", error=str(e))
            return {"tags": [], "branches": []}

    async def validate_branch(self, owner: str, repo: str, branch: str) -> tuple[bool, str]:
        """Validate that a branch exists in the repository.

        Args:
            owner: Repository owner
            repo: Repository name
            branch: Branch name to validate

        Returns:
            Tuple of (is_valid, error_message)
            Returns (True, "") if API is unavailable (best-effort validation)
        """
        branches = await self.get_repo_branches(owner, repo)
        
        # If we couldn't fetch branches (API error, permissions, etc.), 
        # allow the operation to proceed - actual git commands will validate
        if not branches:
            logger.warning("Could not validate branch via API, allowing operation to proceed", 
                          repo=f"{owner}/{repo}", branch=branch)
            return True, ""
        
        branch_names = [b["name"] for b in branches]

        if branch in branch_names:
            return True, ""
        return False, f"Branch '{branch}' not found. Available branches: {', '.join(branch_names[:5])}"

    async def validate_commit(self, owner: str, repo: str, commit_id: str) -> tuple[bool, str]:
        """Validate that a commit exists in the repository.

        Args:
            owner: Repository owner
            repo: Repository name
            commit_id: Commit SHA to validate

        Returns:
            Tuple of (is_valid, error_message)
            Returns (True, "") if API is unavailable (best-effort validation)
        """
        if not self.config.token:
            logger.warning("GitHub token not configured, skipping commit validation")
            return True, ""

        session = await self._get_session()
        url = f"https://api.github.com/repos/{owner}/{repo}/commits/{commit_id}"

        try:
            async with session.get(url) as response:
                if response.status == 200:
                    return True, ""
                elif response.status == 404:
                    return False, f"Commit '{commit_id}' not found in repository"
                elif response.status == 403:
                    # Permission error - allow operation to proceed, git will validate
                    logger.warning("Could not validate commit via API (permission denied), allowing operation to proceed",
                                  repo=f"{owner}/{repo}", commit=commit_id)
                    return True, ""
                else:
                    # Other errors - log but allow to proceed
                    logger.warning("Could not validate commit via API, allowing operation to proceed",
                                  repo=f"{owner}/{repo}", commit=commit_id, status=response.status)
                    return True, ""
        except Exception as e:
            logger.warning("Error validating commit via API, allowing operation to proceed",
                          repo=f"{owner}/{repo}", commit=commit_id, error=str(e))
            return True, ""


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

    async def build(self, repo_name: str, ssh_url: str, version: str = "1.0", 
                   branch: str = None, commit: str = None) -> Dict[str, Any]:
        """Build a stack from a repository.

        Args:
            repo_name: Name of the repository
            ssh_url: SSH URL for cloning if needed
            version: Version tag for the build
            branch: Optional branch name to build from
            commit: Optional specific commit hash to build from

        Returns:
            Dict with success status, output, and timing info
        """
        start_time = datetime.utcnow()
        result = {
            "action": "build",
            "repo": repo_name,
            "version": version,
            "branch": branch,
            "commit": commit,
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

            # Run build script with optional branch and commit
            scripts_path = self.config.scripts_path
            
            # Build command with optional branch/commit parameters
            # Script format: build-push.sh <folder> <version> [branch] [commit]
            build_cmd = f"cd {scripts_path} && bash build-push.sh \"{repo_name}\" {version}"
            if branch:
                build_cmd += f" {branch}"
                if commit:
                    build_cmd += f" {commit}"
            elif commit:
                # If commit is provided without branch, use current branch
                build_cmd += f" \"\" {commit}"

            logger.info("Running build", repo=repo_name, version=version, branch=branch, commit=commit)
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

    async def deploy(self, repo_name: str, ssh_url: str, version: str = "1.0",
                    tag: str = None) -> Dict[str, Any]:
        """Deploy a stack from a repository.

        Args:
            repo_name: Name of the repository
            ssh_url: SSH URL for cloning if needed
            version: Version tag for deployment
            tag: Optional specific tag to deploy (e.g., v1.0.5)

        Returns:
            Dict with success status, output, and timing info
        """
        start_time = datetime.utcnow()
        
        # If tag is provided, extract version from it (e.g., v1.0.5 -> use full tag)
        deploy_version = tag if tag else version
        
        result = {
            "action": "deploy",
            "repo": repo_name,
            "version": deploy_version,
            "tag": tag,
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
            deploy_cmd = f"cd {scripts_path} && bash deploy-service.sh \"{repo_name}\" {deploy_version}"

            logger.info("Running deploy", repo=repo_name, version=deploy_version, tag=tag)
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

    async def get_deployed_stack_tag(self, repo_name: str) -> tuple[bool, Optional[str]]:
        """Get the deployed image tag for a stack from Docker Swarm.

        Args:
            repo_name: Name of the repository (stack name = repo_name.lower())

        Returns:
            Tuple of (success, tag_or_none)
        """
        stack_name = repo_name.lower()

        # Get the image of the first service in the stack
        # Format: docker service ls --filter name=stackname_ --format '{{.Image}}' | head -1
        cmd = f"docker service ls --filter 'name={stack_name}_' --format '{{{{.Image}}}}' | head -1"
        success, output = await self._run_command(cmd)

        if not success or not output.strip():
            return False, None

        image = output.strip()
        # Extract tag from image (e.g., "registry.example.com/app:1.2.3" -> "1.2.3")
        if ':' in image:
            tag = image.split(':')[-1]
        else:
            tag = "latest"

        return True, tag
