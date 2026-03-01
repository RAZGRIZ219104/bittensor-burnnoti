"""Bittensor subnet repository discovery from taostats.io."""

import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
import httpx
import structlog

logger = structlog.get_logger()

# Taostats metagraph URL (contains all subnet GitHub repos)
TAOSTATS_METAGRAPH_URL = "https://taostats.io/subnets/1/metagraph?order=stake%3Adesc"


def extract_github_repo(url: str) -> Optional[str]:
    """Extract owner/repo from a GitHub URL."""
    if not url:
        return None
    
    url = url.strip().rstrip("/")
    
    patterns = [
        r"github\.com/([^/]+/[^/]+)",
        r"^([^/]+/[^/]+)$",
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            result = match.group(1)
            result = result.replace(".git", "")
            return result
    
    return None


class SubnetDiscovery:
    """Discover Bittensor subnet repositories from taostats.io."""
    
    def __init__(self, github_client, custom_repos_file: Optional[str] = None):
        self.github = github_client
        self.custom_repos_file = custom_repos_file
        self._discovered_repos: Dict[str, Dict[str, Any]] = {}
    
    async def discover_all(self) -> List[Dict[str, Any]]:
        """Discover all Bittensor subnet repositories."""
        logger.info("Starting subnet repository discovery from taostats.io")
        self._discovered_repos.clear()
        
        # 1. Scrape from taostats.io (primary source)
        await self._discover_from_taostats()
        
        # 2. Load custom repos if specified
        if self.custom_repos_file:
            await self._load_custom_repos()
        
        repos = list(self._discovered_repos.values())
        logger.info("Discovery complete", repo_count=len(repos))
        
        return repos
    
    async def _discover_from_taostats(self):
        """Scrape GitHub repos from taostats.io metagraph page."""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            
            logger.info("Fetching taostats.io metagraph page")
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    TAOSTATS_METAGRAPH_URL,
                    headers=headers,
                    follow_redirects=True,
                    timeout=60
                )
                
                if response.status_code != 200:
                    logger.error("Failed to fetch taostats", status=response.status_code)
                    return
                
                # Extract GitHub repos from page content
                github_repos = re.findall(
                    r'github\.com/([a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+)',
                    response.text
                )
                
                # Clean and deduplicate
                cleaned_repos = set()
                for repo in github_repos:
                    # Remove trailing special chars
                    repo = re.sub(r'[\\"\'<>,\s].*', '', repo)
                    repo = repo.rstrip('.')
                    if '/' in repo:
                        parts = repo.split('/')
                        if len(parts[0]) > 1 and len(parts[1]) > 1:
                            cleaned_repos.add(repo.lower())
                
                logger.info("Found repos on taostats", count=len(cleaned_repos))
                
                # Validate and add each repo
                for repo_name in cleaned_repos:
                    # Skip org-only entries
                    if repo_name.startswith('orgs/'):
                        continue
                    await self._add_repo_from_name(repo_name)
                    
        except Exception as e:
            logger.error("Failed to scrape taostats.io", error=str(e))
    
    async def _load_custom_repos(self):
        """Load custom repository list from file."""
        try:
            path = Path(self.custom_repos_file)
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
                
                # Support both {"repos": [...]} and [...] formats
                repos_list = data.get("repos", data) if isinstance(data, dict) else data
                
                for repo_name in repos_list:
                    if "/" in repo_name:
                        await self._add_repo_from_name(repo_name)
                
                logger.info("Loaded custom repos", count=len(repos_list))
        except Exception as e:
            logger.error("Failed to load custom repos", error=str(e))
    
    async def _add_repo_from_name(self, repo_name: str):
        """Add a repository by owner/repo name."""
        # Normalize
        repo_name = repo_name.lower().strip()
        
        if repo_name in self._discovered_repos:
            return
        
        try:
            parts = repo_name.split("/", 1)
            if len(parts) != 2:
                return
            
            owner, repo = parts
            repo_data = await self.github.get_repo(owner, repo)
            
            if repo_data:
                self._add_repo(repo_data)
                logger.debug("Added repo", repo=repo_name)
            else:
                logger.debug("Repo not found on GitHub", repo=repo_name)
        except Exception as e:
            logger.debug("Failed to fetch repo", repo=repo_name, error=str(e))
    
    def _add_repo(self, repo_data: Dict[str, Any]):
        """Add a repository to the discovered list."""
        full_name = repo_data["full_name"].lower()
        
        # Skip archived repos
        if repo_data.get("archived"):
            return
        
        if full_name not in self._discovered_repos:
            owner = repo_data["owner"]
            if isinstance(owner, dict):
                owner = owner.get("login", "")
            
            self._discovered_repos[full_name] = {
                "full_name": repo_data["full_name"],
                "name": repo_data["name"],
                "owner": owner,
                "description": repo_data.get("description"),
                "html_url": repo_data["html_url"],
                "default_branch": repo_data.get("default_branch", "main"),
            }
    
    def get_discovered_repos(self) -> List[Dict[str, Any]]:
        """Get list of discovered repositories."""
        return list(self._discovered_repos.values())
