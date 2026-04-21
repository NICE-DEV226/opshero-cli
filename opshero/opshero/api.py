"""
OpsHero API client — wraps httpx with auth, refresh, and error handling.

Usage:
    async with OpsHeroClient(cfg) as client:
        result = await client.analyze(log_text)
"""

import sys
import time
from typing import Any, Optional

import httpx

from opshero.config import Config


# ── Exceptions ─────────────────────────────────────────────────────────────────

class APIError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class AuthError(APIError):
    pass


class NotFoundError(APIError):
    pass


class RateLimitError(APIError):
    pass


# ── Client ─────────────────────────────────────────────────────────────────────

class OpsHeroClient:
    """
    Async HTTP client for the OpsHero backend API.
    Handles token refresh transparently on 401.
    """

    DEFAULT_TIMEOUT = 30.0

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "OpsHeroClient":
        self._client = httpx.AsyncClient(
            base_url=self.cfg.api_url,
            timeout=self.DEFAULT_TIMEOUT,
            headers={"User-Agent": "opshero-cli/0.1.0"},
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _auth_headers(self) -> dict:
        return self.cfg.get_auth_header()

    async def _refresh_if_needed(self) -> None:
        """Silently refresh the access token if it's expired."""
        if not self.cfg.is_token_expired:
            return
        if not self.cfg.refresh_token:
            raise AuthError("Not authenticated — run: opshero login", 401)
        try:
            await self.refresh_tokens()
        except AuthError:
            # Clear bad tokens so user gets a clean error
            self.cfg.clear_auth()
            self.cfg.save()
            raise AuthError(
                "Session expired — run: opshero login", 401
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        authed: bool = True,
        retry_on_401: bool = True,
        **kwargs: Any,
    ) -> Any:
        assert self._client is not None

        if authed:
            await self._refresh_if_needed()
            kwargs.setdefault("headers", {}).update(self._auth_headers())

        resp = await self._client.request(method, path, **kwargs)

        # Transparent token refresh on 401
        if resp.status_code == 401 and authed and retry_on_401:
            await self.refresh_tokens()
            kwargs["headers"].update(self._auth_headers())
            resp = await self._client.request(method, path, **kwargs)

        self._raise_for_status(resp)
        # 202 = "Authorization pending" (device flow polling) — treat as an
        # APIError so the caller can detect it via e.status_code == 202.
        if resp.status_code == 202:
            try:
                detail = resp.json().get("detail", "Accepted")
            except Exception:
                detail = "Accepted"
            raise APIError(detail, 202)
        if resp.status_code == 204:
            return None
        return resp.json()

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text

        if resp.status_code == 401:
            raise AuthError(f"Authentication required: {detail}", 401)
        if resp.status_code == 403:
            raise AuthError(f"Access denied: {detail}", 403)
        if resp.status_code == 404:
            raise NotFoundError(f"Not found: {detail}", 404)
        if resp.status_code == 429:
            raise RateLimitError(f"Rate limit exceeded: {detail}", 429)
        raise APIError(f"API error {resp.status_code}: {detail}", resp.status_code)

    # ── Auth endpoints ─────────────────────────────────────────────────────────

    async def device_code(self) -> dict:
        """Step 1 of GitHub Device Flow — get user_code + device_code."""
        try:
            return await self._request("POST", "/auth/github/device/code", authed=False)
        except httpx.ConnectError as e:
            raise APIError(
                f"Cannot connect to OpsHero backend at {self.cfg.api_url}\n"
                f"Please check:\n"
                f"  1. Your internet connection\n"
                f"  2. The backend URL is correct\n"
                f"  3. No firewall is blocking the connection\n"
                f"Original error: {e}",
                0
            ) from e

    async def device_poll(self, device_code: str) -> dict:
        """Step 2 — poll for token. Raises APIError(202) while pending."""
        return await self._request(
            "POST",
            "/auth/github/device/poll",
            authed=False,
            json={"device_code": device_code},
        )

    async def refresh_tokens(self) -> None:
        """Exchange refresh token for a new token pair."""
        if not self.cfg.refresh_token:
            raise AuthError("No refresh token stored", 401)
        try:
            data = await self._request(
                "POST",
                "/auth/refresh",
                authed=False,
                json={"refresh_token": self.cfg.refresh_token},
                retry_on_401=False,
            )
        except (AuthError, NotFoundError, APIError) as e:
            # Any error during refresh = session expired, need to re-login
            raise AuthError(
                "Session expired — run: opshero login", 401
            ) from e
        self.cfg.access_token = data["access_token"]
        self.cfg.refresh_token = data["refresh_token"]
        self.cfg.token_expires_at = None  # will be parsed lazily
        self.cfg.save()

    async def logout(self) -> None:
        """Blacklist the current access token on the server."""
        if self.cfg.access_token:
            try:
                await self._request(
                    "POST",
                    "/auth/logout",
                    json={"access_token": self.cfg.access_token},
                    authed=False,
                )
            except Exception:
                pass  # best-effort

    async def get_me(self) -> dict:
        return await self._request("GET", "/auth/me")

    # ── Analysis endpoints ─────────────────────────────────────────────────────

    async def get_team_projects(self) -> dict:
        """Get user's team projects."""
        return await self._request("GET", "/projects/team")

    async def get_my_team(self) -> dict:
        """Get current user's team with members and invitations."""
        return await self._request("GET", "/teams/me")

    async def get_project(self, project_id: str) -> dict:
        """Get a specific project with stats."""
        return await self._request("GET", f"/projects/{project_id}")

    async def get_project_analyses(
        self,
        project_id: str,
        page: int = 1,
        per_page: int = 20,
    ) -> dict:
        """Get analyses for a specific project."""
        return await self._request(
            "GET",
            f"/projects/{project_id}/analyses",
            params={"page": page, "per_page": per_page},
        )

    async def get_team_analyses(self, page: int = 1, per_page: int = 20) -> dict:
        """Get all analyses across the team."""
        return await self._request(
            "GET",
            "/teams/me/analyses",
            params={"page": page, "per_page": per_page},
        )

    async def analyze(
        self,
        log: str,
        client_id: str,
        metadata: Optional[dict] = None,
        context: Optional[dict] = None,
        project_id: Optional[str] = None,
    ) -> dict:
        """Submit a CI/CD log for analysis."""
        payload = {
            "log": log,
            "client_id": client_id,
            "metadata": metadata or {},
            "context": context or {},
        }
        
        if project_id:
            payload["project_id"] = project_id
            
        return await self._request(
            "POST",
            "/analyses",
            json=payload,
        )

    async def list_analyses(
        self,
        page: int = 1,
        per_page: int = 20,
        **filters: Any,
    ) -> dict:
        params = {"page": page, "per_page": per_page, **{k: v for k, v in filters.items() if v}}
        return await self._request("GET", "/analyses", params=params)

    async def get_analysis(self, analysis_id: str) -> dict:
        return await self._request("GET", f"/analyses/{analysis_id}")

    async def submit_feedback(self, analysis_id: str, helpful: bool, comment: str = "") -> None:
        await self._request(
            "POST",
            f"/analyses/{analysis_id}/feedback",
            json={"helpful": helpful, "comment": comment},
        )

    async def get_stats(self) -> dict:
        return await self._request("GET", "/analyses/stats/summary")

    # ── Patterns endpoints ─────────────────────────────────────────────────────

    async def get_sync_manifest(self) -> dict:
        return await self._request("GET", "/patterns/meta/sync-manifest")

    async def list_patterns(self, page: int = 1, per_page: int = 200) -> dict:
        return await self._request(
            "GET", "/patterns", params={"page": page, "per_page": per_page}
        )

    # ── Sync endpoints ─────────────────────────────────────────────────────────

    async def sync_push(self, items: list[dict]) -> dict:
        return await self._request("POST", "/sync/push", json={"items": items})

    async def sync_pull(self, since: str, client_id: str) -> dict:
        return await self._request(
            "POST", "/sync/pull", json={"since": since, "client_id": client_id}
        )

    async def sync_status(self, client_id: str) -> dict:
        return await self._request("GET", "/sync/status", params={"client_id": client_id})

    # ── Contribution endpoints ─────────────────────────────────────────────────

    async def submit_contribution(self, payload: dict) -> dict:
        """Submit a community pattern contribution for admin review."""
        return await self._request("POST", "/contributions", json=payload)

    async def list_my_contributions(self, page: int = 1, per_page: int = 20) -> dict:
        """List the authenticated user's own contributions."""
        return await self._request(
            "GET", "/contributions/mine", params={"page": page, "per_page": per_page}
        )

    # ── GitHub proxy endpoints ─────────────────────────────────────────────────

    async def github_repos(self, page: int = 1, per_page: int = 30) -> list:
        """List user's GitHub repositories."""
        return await self._request(
            "GET", "/github/repos", params={"page": page, "per_page": per_page}
        )

    async def github_failed_runs(
        self,
        owner: str,
        repo: str,
        branch: Optional[str] = None,
        per_page: int = 10,
    ) -> list:
        """List failed workflow runs for a repo."""
        params: dict = {"per_page": per_page, "status": "failure"}
        if branch:
            params["branch"] = branch
        return await self._request(
            "GET", f"/github/repos/{owner}/{repo}/runs", params=params
        )

    async def github_run_logs(
        self,
        owner: str,
        repo: str,
        run_id: int,
        max_bytes: int = 500_000,
    ) -> dict:
        """Download and extract logs for a specific workflow run."""
        return await self._request(
            "GET",
            f"/github/runs/{run_id}/logs",
            params={"owner": owner, "repo": repo, "max_bytes": max_bytes},
            timeout=60.0,  # logs can be large
        )

    async def github_latest_failed_logs(
        self,
        owner: str,
        repo: str,
        branch: Optional[str] = None,
    ) -> dict:
        """Fetch logs for the most recent failed run in a repo."""
        params: dict = {}
        if branch:
            params["branch"] = branch
        return await self._request(
            "GET",
            f"/github/repos/{owner}/{repo}/runs/latest-failed",
            params=params,
            timeout=60.0,
        )

    async def github_rerun(
        self,
        owner: str,
        repo: str,
        run_id: int,
        failed_only: bool = False,
    ) -> dict:
        """Re-trigger a GitHub Actions workflow run."""
        return await self._request(
            "POST",
            f"/github/runs/{run_id}/rerun",
            params={"owner": owner, "repo": repo, "failed_only": str(failed_only).lower()},
        )
