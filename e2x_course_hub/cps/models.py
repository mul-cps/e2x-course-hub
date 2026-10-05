"""Normalized provider models; identifiers are never derived from display names."""
from dataclasses import dataclass
from typing import Optional

@dataclass
class Provenance:
    id: str
    source: str = 'local'
    external_id: Optional[str] = None

@dataclass
class Course(Provenance):
    name: str = ''

@dataclass
class CourseTerm(Provenance):
    course_id: str = ''
    name: str = ''

@dataclass
class CourseMembership(Provenance):
    course_id: str = ''
    person_id: str = ''
    role: str = 'student'

@dataclass
class CourseGroup(Provenance):
    course_id: str = ''
    name: str = ''

@dataclass
class CourseGrouping(Provenance):
    course_id: str = ''
    name: str = ''
