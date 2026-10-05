import json

from jupyterhub.services.auth import HubOAuth
from jupyterhub.utils import url_path_join as ujoin
from tornado.httpclient import AsyncHTTPClient, HTTPClientError, HTTPRequest, HTTPResponse

from .errors import GroupNotFoundError, HubAPIError, InvalidInputError, UserNotFoundError


class HubAPI(HubOAuth):
    """Client for interacting with JupyterHub's REST API.

    Provides methods for managing users, groups, and group memberships
    with proper error handling and exception wrapping.

    Attributes:
        client: Async HTTP client for making requests
    """

    def __init__(self, *args, **kwargs):
        """Initialize the Hub API client.

        Args:
            *args: Positional arguments passed to HubOAuth
            **kwargs: Keyword arguments passed to HubOAuth
        """
        super().__init__(*args, **kwargs)
        self.client = AsyncHTTPClient()

    @property
    def auth_header(self):
        """Get authorization header for Hub API requests."""
        return {"Authorization": f"token {self.api_token}"}

    async def request(self, url, method="GET", body=None) -> HTTPResponse:
        """Make an authenticated request to the Hub API.

        Args:
            url: Full URL to request
            method: HTTP method (GET, POST, DELETE, etc.)
            body: Optional request body (as string)

        Returns:
            HTTP response from the Hub API

        Raises:
            HubAPIError: If the request fails
        """
        req = HTTPRequest(
            url, method=method, headers=self.auth_header, body=body, allow_nonstandard_methods=True
        )
        provider = getattr(self, 'audit_provider', None)
        mutation = method in ('POST','PUT','PATCH','DELETE')
        snapshot_url = url
        if '/groups/' in url and url.endswith('/users'): snapshot_url = url.rsplit('/users',1)[0]
        if '/users/' in url and '/servers/' in url: snapshot_url = url.split('/servers/',1)[0]
        async def snapshot():
            try:
                response = await self.client.fetch(HTTPRequest(snapshot_url,headers=self.auth_header))
                return json.loads(response.body) if response.body else None
            except HTTPClientError as error:
                return None if error.code == 404 else {'unavailable':error.code}
        previous = await snapshot() if provider and mutation else None
        outcome = 'failure'
        result = None
        try:
            result = await self.client.fetch(req)
            outcome = 'success'
            return result
        except HTTPClientError as e:
            outcome = 'denied' if e.code in (401,403) else 'failure'
            if e.code == 404: raise
            raise HubAPIError(f"Hub API request failed: {e.message}") from e
        finally:
            if provider and mutation:
                from ..cps.audit import actor_context
                from datetime import datetime, timezone
                from urllib.parse import urlsplit
                current = await snapshot()
                try: requested = json.loads(body or '{}')
                except ValueError: requested = {'invalid_json':True}
                with provider.db:
                    provider.db.execute('INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?, ?, ?)',(
                        actor_context.get() or 'service:'+provider.console,provider.console,'hub:'+method,
                        urlsplit(url).path,json.dumps(previous,sort_keys=True),
                        json.dumps({'observed':current,'requested':requested},sort_keys=True),
                        datetime.now(timezone.utc).isoformat(),outcome))

    def _handle_not_found(self, error: HTTPClientError, resource_type: str, resource_name: str):
        """Convert 404 errors to specific exceptions.

        Args:
            error: The HTTP error to handle
            resource_type: Type of resource ("group" or "user")
            resource_name: Name of the resource

        Raises:
            GroupNotFoundError: If resource_type is "group"
            UserNotFoundError: If resource_type is "user"
            HTTPClientError: For other error codes
        """
        if error.code == 404:
            if resource_type == "group":
                raise GroupNotFoundError(resource_name) from error
            elif resource_type == "user":
                raise UserNotFoundError(resource_name) from error
        raise error

    async def list_users(self, offset=None):
        """List users from the Hub API with optional pagination.

        Args:
            offset: Starting offset for pagination, None to get all users

        Returns:
            List of user dictionaries from Hub API

        Raises:
            HubAPIError: If the request fails
        """
        if offset is None:
            return await self.list_all_users()
        url = ujoin(self.api_url, f"users?offset={offset}")
        resp: HTTPResponse = await self.request(url, method="GET")
        return json.loads(resp.body)

    async def list_all_users(self):
        """List all users by paginating through results.

        Returns:
            Complete list of all user dictionaries from Hub API

        Raises:
            HubAPIError: If any request fails
        """
        offset = 0
        users = []
        while True:
            batch = await self.list_users(offset)
            if not batch:
                break
            users.extend(batch)
            offset += len(batch)
        return users

    async def filter_existing_users(self, usernames: list):
        """Filter a list of usernames to only those that exist in Hub.

        Args:
            usernames: List of usernames to check

        Returns:
            Set of usernames that exist in the Hub

        Raises:
            HubAPIError: If the user list request fails
        """
        if not usernames:
            return set()

        existing_users = await self.list_all_users()
        existing_usernames = {user["name"] for user in existing_users}
        return set(usernames).intersection(existing_usernames)

    async def get_user(self, username: str):
        """Get information about a user.

        Args:
            username: Username to look up

        Returns:
            User dictionary from Hub API

        Raises:
            UserNotFoundError: If the user doesn't exist
            HubAPIError: If the request fails
        """
        url = ujoin(self.api_url, "users", username)
        try:
            resp: HTTPResponse = await self.request(url, method="GET")
            return json.loads(resp.body)
        except HTTPClientError as e:
            self._handle_not_found(e, "user", username)

    async def create_user(self, username: str):
        """Create a single user in the Hub.

        Args:
            username: Username to create

        Returns:
            User dictionary from Hub API

        Raises:
            InvalidInputError: If username is empty
            HubAPIError: If the creation fails
        """
        if not username or not username.strip():
            raise InvalidInputError("Username cannot be empty")

        url = ujoin(self.api_url, "users", username)
        resp: HTTPResponse = await self.request(url, method="POST", body="{}")
        return json.loads(resp.body)

    async def create_users(self, usernames: list):
        """Create multiple users in the Hub in a single request.

        Args:
            usernames: List of usernames to create

        Returns:
            Response from Hub API

        Raises:
            InvalidInputError: If usernames list is empty or contains invalid names
            HubAPIError: If the creation fails
        """
        if not usernames:
            raise InvalidInputError("Usernames list cannot be empty")

        # Filter out empty usernames
        valid_usernames = [u.strip() for u in usernames if u and u.strip()]
        if not valid_usernames:
            raise InvalidInputError("No valid usernames provided")

        url = ujoin(self.api_url, "users")
        body = {
            "usernames": valid_usernames,
            "admin": False,
        }
        resp: HTTPResponse = await self.request(url, method="POST", body=json.dumps(body))
        return json.loads(resp.body)

    async def get_group(self, groupname: str):
        """Get information about a group.

        Args:
            groupname: Name of the group

        Returns:
            Group dictionary from Hub API

        Raises:
            GroupNotFoundError: If the group doesn't exist
            HubAPIError: If the request fails
        """
        url = ujoin(self.api_url, "groups", groupname)
        try:
            resp: HTTPResponse = await self.request(url, method="GET")
            return json.loads(resp.body)
        except HTTPClientError as e:
            self._handle_not_found(e, "group", groupname)

    async def create_group(self, groupname: str):
        """Create a new group in the Hub.

        Args:
            groupname: Name of the group to create

        Returns:
            Group dictionary from Hub API

        Raises:
            InvalidInputError: If groupname is empty
            HubAPIError: If the creation fails
        """
        if not groupname or not groupname.strip():
            raise InvalidInputError("Group name cannot be empty")

        url = ujoin(self.api_url, "groups", groupname)
        resp: HTTPResponse = await self.request(url, method="POST", body="{}")
        return json.loads(resp.body)

    async def delete_group(self, groupname: str):
        """Delete a group from the Hub.

        Args:
            groupname: Name of the group to delete

        Returns:
            Response from Hub API

        Raises:
            GroupNotFoundError: If the group doesn't exist
            HubAPIError: If the deletion fails
        """
        url = ujoin(self.api_url, "groups", groupname)
        try:
            resp: HTTPResponse = await self.request(url, method="DELETE")
            return json.loads(resp.body)
        except HTTPClientError as e:
            self._handle_not_found(e, "group", groupname)

    async def add_users_to_group(self, groupname: str, usernames: list):
        """Add users to a group.

        Args:
            groupname: Name of the group
            usernames: List of usernames to add

        Returns:
            Response from Hub API

        Raises:
            GroupNotFoundError: If the group doesn't exist
            InvalidInputError: If usernames list is empty
            HubAPIError: If the operation fails
        """
        if not usernames:
            raise InvalidInputError("Usernames list cannot be empty")

        url = ujoin(self.api_url, "groups", groupname, "users")
        body = {"users": usernames}
        try:
            resp: HTTPResponse = await self.request(url, method="POST", body=json.dumps(body))
            return json.loads(resp.body)
        except HTTPClientError as e:
            self._handle_not_found(e, "group", groupname)

    async def remove_users_from_group(self, groupname: str, usernames: list):
        """Remove users from a group.

        Args:
            groupname: Name of the group
            usernames: List of usernames to remove

        Returns:
            Response from Hub API

        Raises:
            GroupNotFoundError: If the group doesn't exist
            InvalidInputError: If usernames list is empty
            HubAPIError: If the operation fails
        """
        if not usernames:
            raise InvalidInputError("Usernames list cannot be empty")

        url = ujoin(self.api_url, "groups", groupname, "users")
        body = {"users": usernames}
        try:
            resp: HTTPResponse = await self.request(url, method="DELETE", body=json.dumps(body))
            return json.loads(resp.body)
        except HTTPClientError as e:
            self._handle_not_found(e, "group", groupname)
