from logging import Logger
from typing import Dict, List, Optional

from ..schema.roles import Permission
from ..schema.server import Server
from ..schema.user import User
from .base_api import BaseAPI
from .errors import (
    APIPermissionError,
    CourseNotFoundError,
    GroupNotFoundError,
    RoleNotFoundError,
    TermNotFoundError,
)
from .hub_api import HubAPI


class CourseAPI(BaseAPI):
    """API for managing course memberships.

    Handles adding, removing, and listing course members with proper
    permission checking and JupyterHub group synchronization.

    Attributes:
        hub_api: JupyterHub API client for group management
    """

    def __init__(
        self,
        server: Server,
        hub_api: HubAPI,
        add_users_to_hub: bool = False,
        logger: Optional[Logger] = None,
    ):
        """Initialize the course API.

        Args:
            server: The Server configuration object
            hub_api: JupyterHub API client for group operations
            add_users_to_hub: Whether to add users to JupyterHub when they are created in the course
                service. Defaults to False.
            logger: Optional logger for logging purposes
        """
        super().__init__(server, logger=logger)
        self.hub_api = hub_api
        self.add_users_to_hub = add_users_to_hub

    def _ensure_course_and_term_exist(self, course_id: str, term_id: str):
        """Ensure that the specified course and term exist.

        Args:
            course_id: The course ID
            term_id: The term ID
        Raises:
            CourseNotFoundError: If the course does not exist
            TermNotFoundError: If the term does not exist within the course
        """
        course = self.server.courses.get(course_id)
        if not course:
            raise CourseNotFoundError(course_id)
        term = course.terms.get(term_id)
        if not term:
            raise TermNotFoundError(course_id, term_id)

    def list_roles_user_can_assign(self, user: User, course_id: str, term_id: str) -> List[str]:
        """List all roles the user can assign in the given course and term.

        Args:
            user: The user requesting the role list
            course_id: The course ID
            term_id: The term ID
        Returns:
            List of role IDs the user can assign
        """
        roles = []
        for role_id in self.server.roles.root.keys():
            try:
                self.require_permission(
                    user=user,
                    course_id=course_id,
                    term_id=term_id,
                    permission=Permission.add_course_members(role_id),
                )
                roles.append(role_id)
            except APIPermissionError:
                continue
        return roles

    async def list_course_member_roles(
        self, user: User, course_id: str, term_id: str, usernames: List[str]
    ) -> Dict[str, List[str]]:
        """List all roles for the specified members in the given course and term.

        Args:
            user: The user requesting the role list
            course_id: The course ID
            term_id: The term ID
            usernames: The list of usernames to check

        Returns:
            A dictionary mapping usernames to their roles
        """
        self.require_permission(
            user=user,
            course_id=course_id,
            term_id=term_id,
            permission=Permission.list_course_members(),
        )

        roles = {}
        for role in self.server.roles.root.keys():
            group_name = f"{course_id}.{term_id}.{role}"
            try:
                group = await self.hub_api.get_group(group_name)
                for username in group.get("users", []):
                    if username in usernames:
                        roles.setdefault(username, []).append(role)
            except GroupNotFoundError:
                continue
        return roles

    async def list_course_members(
        self, user: User, course_id: str, term_id: str
    ) -> List[Dict[str, str]]:
        """List all members of the given course and term with their strongest role.

        When a user has multiple roles (e.g., both 'student' and 'grader'),
        only the strongest role (highest priority) is returned.

        Args:
            user: The user requesting the member list
            course_id: The course ID
            term_id: The term ID

        Returns:
            List of dictionaries, each containing a username and their strongest role
                and whether the user can delete them

        Raises:
            PermissionError: If the user doesn't have permission to list members
        """
        self.require_permission(
            user=user,
            course_id=course_id,
            term_id=term_id,
            permission=Permission.list_course_members(),
        )

        # First, collect all roles for each member
        members_with_all_roles = {}
        for role in self.server.roles.root.keys():
            group_name = f"{course_id}.{term_id}.{role}"
            try:
                group = await self.hub_api.get_group(group_name)
                for username in group.get("users", []):
                    members_with_all_roles.setdefault(username, []).append(role)
            except GroupNotFoundError:
                pass

        members = []
        for username, roles in members_with_all_roles.items():
            strongest_role = self.server.roles.get_strongest_role(roles)
            if strongest_role is not None:
                deletable = self.has_permission_in_course(
                    user=user,
                    course_id=course_id,
                    term_id=term_id,
                    permission=Permission.remove_course_members(strongest_role),
                )
                members.append(
                    {
                        "username": username,
                        "role": strongest_role,
                        "deletable": deletable,
                    }
                )
        # Then, reduce to strongest role only
        return members

    def ensure_local_editable(self, course_id, term_id):
        self._ensure_course_and_term_exist(course_id, term_id)
        course = self.server.courses[course_id]
        if course.metadata.source != 'local' or course.config.terms[term_id].source != 'local':
            raise PermissionError('External course memberships are read-only; reviewed migration required')

    async def add_course_members(
        self,
        user: User,
        course_id: str,
        term_id: str,
        role_id: str,
        usernames: List[str],
    ):
        """Add members to the given course and term with the specified role.

        Args:
            user: The user attempting to add members
            course_id: The course ID
            term_id: The term ID
            role_id: The role to assign to the new members
            usernames: The list of usernames to add

        Raises:
            PermissionError: If the user doesn't have permission to add members
            ValueError: If the role doesn't exist or group doesn't exist
        """
        # Check permission
        self.ensure_local_editable(course_id, term_id)
        self.require_permission(
            user=user,
            course_id=course_id,
            term_id=term_id,
            permission=Permission.add_course_members(role_id),
        )

        # Ensure course and term exist
        self._ensure_course_and_term_exist(course_id, term_id)

        # Validate role exists
        if role_id not in self.server.roles.root:
            raise RoleNotFoundError(role_id)

        group_name = f"{course_id}.{term_id}.{role_id}"

        # Ensure the group exists
        try:
            await self.hub_api.get_group(group_name)
        except GroupNotFoundError:
            await self.hub_api.create_group(group_name)

        # Ensure all users exist
        existing_usernames = await self.hub_api.filter_existing_users(usernames)
        non_existing_usernames = set(usernames) - existing_usernames
        if non_existing_usernames and self.add_users_to_hub:
            await self.hub_api.create_users(list(non_existing_usernames))

        # Add users to the group
        await self.hub_api.add_users_to_group(group_name, usernames)

    async def remove_course_member(
        self,
        user: User,
        course_id: str,
        term_id: str,
        username: str,
    ):
        """Remove a single member from the given course and term.

        Args:
            user: The user attempting to remove the member
            course_id: The course ID
            term_id: The term ID
            username: The username to remove
        Raises:
            PermissionError: If the user doesn't have permission to remove the member
        """
        roles = await self.list_course_member_roles(
            user=user, course_id=course_id, term_id=term_id, usernames=[username]
        )
        user_roles = roles.get(username, [])
        if not user_roles:
            return  # User is not a member of the course/term
        strongest_role = self.server.roles.get_strongest_role(user_roles)
        if strongest_role is None:
            return  # No valid role found
        self.ensure_local_editable(course_id, term_id)
        self.require_permission(
            user=user,
            course_id=course_id,
            term_id=term_id,
            permission=Permission.remove_course_members(strongest_role),
        )
        for role in user_roles:
            group_name = f"{course_id}.{term_id}.{role}"
            try:
                await self.hub_api.remove_users_from_group(group_name, [username])
            except GroupNotFoundError:
                continue

    async def remove_course_members(
        self,
        user: User,
        course_id: str,
        term_id: str,
        usernames: List[str],
    ):
        """Remove members from the given course and term.

        Args:
            user: The user attempting to remove members
            course_id: The course ID
            term_id: The term ID
            usernames: The list of usernames to remove
        Raises:
            PermissionError: If the user doesn't have permission to remove members
        """
        roles = await self.list_course_member_roles(
            user=user, course_id=course_id, term_id=term_id, usernames=usernames
        )
        # Create a dictionary mapping roles to usernames
        role_to_usernames: Dict[str, List[str]] = {}
        for username, user_roles in roles.items():
            for role in user_roles:
                role_to_usernames.setdefault(role, []).append(username)
        # Remove users role by role
        self.ensure_local_editable(course_id, term_id)
        for role, users_in_role in role_to_usernames.items():
            self.require_permission(
                user=user,
                course_id=course_id,
                term_id=term_id,
                permission=Permission.remove_course_members(role),
            )
            group_name = f"{course_id}.{term_id}.{role}"
            try:
                await self.hub_api.remove_users_from_group(group_name, users_in_role)
            except GroupNotFoundError:
                continue

    async def remove_course_members1(
        self,
        user: User,
        course_id: str,
        term_id: str,
        role_id: str,
        usernames: List[str],
    ):
        """Remove members from the given course and term with the specified role.

        Args:
            user: The user attempting to remove members
            course_id: The course ID
            term_id: The term ID
            role_id: The role of the members to remove
            usernames: The list of usernames to remove

        Raises:
            PermissionError: If the user doesn't have permission to remove members
            ValueError: If the group doesn't exist
        """
        # Check permission
        self.ensure_local_editable(course_id, term_id)
        self.require_permission(
            user=user,
            course_id=course_id,
            term_id=term_id,
            permission=Permission.remove_course_members(role_id),
        )
        # Ensure course and term exist
        self._ensure_course_and_term_exist(course_id, term_id)

        group_name = f"{course_id}.{term_id}.{role_id}"

        # Check if the group exists
        try:
            await self.hub_api.get_group(group_name)
        except GroupNotFoundError:
            raise ValueError(f"Group '{group_name}' does not exist.")

        # Filter out non-existing users
        existing_usernames = await self.hub_api.filter_existing_users(usernames)
        if not existing_usernames:
            return  # No existing users to remove

        # Remove users from the group
        await self.hub_api.remove_users_from_group(group_name, list(existing_usernames))

    def get_course_metadata(
        self,
        user: User,
        course_id: str,
        term_id: str,
    ) -> Dict[str, str]:
        """Retrieve metadata for the specified course.

        Args:
            user: The user requesting the course metadata
            course_id: The course ID
        Returns:
            Dictionary of course metadata
        """
        self.require_permission(
            user=user,
            course_id=course_id,
            term_id=term_id,
            permission=Permission.view_course_metadata(),
        )
        self._ensure_course_and_term_exist(course_id, term_id)
        course = self.server.courses[course_id]
        term = course.terms[term_id]
        metadata = course.metadata.model_dump(exclude_none=True, exclude_unset=True)
        metadata["profiles"] = list(term.keys())
        metadata["term_id"] = term_id
        return metadata

    def list_courses_user_can_see(self, user: User) -> List[Dict[str, str]]:
        """List all courses the user has permission to manage.

        Args:
            user: The user requesting the course list

        Returns:
            List of dictionaries with course_id and term_id
        """
        manageable_courses = []
        for course_id, course in self.server.courses.items():
            for term_id in course.terms.keys():
                try:
                    self.require_permission(
                        user=user,
                        course_id=course_id,
                        term_id=term_id,
                        permission=Permission.view_course_metadata(),
                    )
                    course_name = course.metadata.course_name or course_id
                    manageable_courses.append(
                        {"course_id": course_id, "term_id": term_id, "course_name": course_name}
                    )
                except APIPermissionError:
                    continue
        return manageable_courses

    async def leave_course(
        self,
        user: User,
        course_id: str,
        term_id: str,
    ):
        """Allow a user to leave a course by removing themselves from all roles.

        Args:
            user: The user attempting to leave the course
            course_id: The course ID
            term_id: The term ID
        """
        self.ensure_local_editable(course_id, term_id)
        self.require_permission(
            user=user,
            course_id=course_id,
            term_id=term_id,
            permission=Permission.leave_course(),
        )
        self._ensure_course_and_term_exist(course_id, term_id)
        for role_id in self.server.roles.root.keys():
            group_name = f"{course_id}.{term_id}.{role_id}"
            try:
                await self.hub_api.remove_users_from_group(group_name, [user.username])
            except GroupNotFoundError:
                continue
