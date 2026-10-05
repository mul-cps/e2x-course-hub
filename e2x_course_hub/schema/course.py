from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .profile import BaseProfile
from .runtime import RuntimePartial


def apply_profile_runtime_overrides(
    profiles: Dict[str, BaseProfile], profile_runtime_overrides: Dict[str, RuntimePartial]
) -> Dict[str, BaseProfile]:
    """
    Apply runtime overrides to a dictionary of profiles.

    Args:
        profiles (Dict[str, BaseProfile]): A mapping of profile names to BaseProfile objects.
        profile_runtime_overrides (Dict[str, RuntimePartial]): A mapping of profile names to
            RuntimePartial objects representing the runtime overrides.
    Returns:
        Dict[str, BaseProfile]: A mapping of profile names to BaseProfile objects with applied
            runtime overrides.
    """
    profiles_with_overrides = {}
    for profile_name, profile in profiles.items():
        profile_copy = BaseProfile(**profile.model_dump())
        if profile_name in profile_runtime_overrides:
            profile_copy.runtime = profile_copy.runtime.merge(
                profile_runtime_overrides[profile_name]
            )
        profiles_with_overrides[profile_name] = profile_copy
    return profiles_with_overrides


class CourseMetadata(BaseModel):
    """
    Metadata information about a course.
    """

    source: str = "local"
    external_id: Optional[str] = None

    course_id: str = Field(..., description="The id of the course.")
    course_name: str = Field(..., description="The full name of the course")
    description: Optional[str] = Field(
        default=None, description="An optional text describing the course."
    )


class ConfigWithRuntimeOverrides(BaseModel):
    """
    Base config model that includes profile runtime overrides.
    """

    profile_runtime_overrides: Dict[str, RuntimePartial] = Field(
        default_factory=dict,
        description="Optional runtime overrides for specific profiles.",
    )


class TermConfig(ConfigWithRuntimeOverrides):
    """
    Configuration for a specific term/session within a course.
    """

    source: str = "local"
    external_id: Optional[str] = None

    allowed_profiles: List[str] = Field(..., description="List of profiles enabled for the term.")


class CourseConfig(ConfigWithRuntimeOverrides):
    """
    Configuration for a course, including metadata and term configurations.
    """

    metadata: CourseMetadata = Field(..., description="The course metadata.")
    terms: Dict[str, TermConfig] = Field(
        default_factory=dict, description="The active sessions for this course."
    )


class Course(BaseModel):
    """
    Represents a course with its configuration and term profiles.
    Allowed profiles per term are determined based on the course and term configs.
    """

    config: CourseConfig = Field(..., description="The course config")
    terms: Dict[str, Dict[str, BaseProfile]] = Field(
        ..., description="The mapping between term id and term profiles"
    )

    @property
    def metadata(self):
        return self.config.metadata

    @classmethod
    def from_course_config(
        cls, course_config: CourseConfig, server_profiles: Dict[str, BaseProfile]
    ):
        """
        Create a Course instance from a CourseConfig and server-wide profiles,
        applying runtime overrides.

        Args:
            course_config (CourseConfig): The configuration for the course.
            server_profiles (Dict[str, BaseProfile]): The server-wide profiles available.
        Returns:
            Course: The constructed Course instance.
        """
        course_profiles = apply_profile_runtime_overrides(
            server_profiles, course_config.profile_runtime_overrides
        )
        terms = {}
        for term_id, term_config in course_config.terms.items():
            term_profiles = apply_profile_runtime_overrides(
                course_profiles, term_config.profile_runtime_overrides
            )
            enabled_term_profiles = {
                profile_name: term_profile
                for profile_name, term_profile in term_profiles.items()
                if profile_name in term_config.allowed_profiles
            }
            terms[term_id] = enabled_term_profiles
        return cls(config=course_config, terms=terms)
