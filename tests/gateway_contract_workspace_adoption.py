"""Run explicitly with the pinned gateway SDK and its dependency environment."""

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from cps_compute.internal import validate_workspace_registration


class GatewayRegistrationContractTest(unittest.TestCase):
    def setUp(self):
        raw = json.loads(
            (Path(__file__).parent / "fixtures" / "source-adoption-gateway-registration.json")
            .read_text()
        )
        # The GET adds centrally generated custody fields to the validated PUT body.
        self.body = {
            key: value for key, value in raw.items()
            if key not in {"storage", "workspace_hash", "workspace_label"}
        }
        self.policy = SimpleNamespace(
            hash="fictional-policy-hash",
            catalog={"profiles": {"interactive-cpu": {"kind": "interactive"}}},
        )
        self.people = {"cps": {"fictional-user": "fictional-person"}}

    def test_shared_source_fixture_is_accepted_by_actual_gateway_validator(self):
        before = copy.deepcopy(self.body)
        validate_workspace_registration(self.policy, self.body, "cps", self.people)
        self.assertEqual(self.body, before)

    def test_owner_suffix_and_foreign_principals_are_rejected_by_actual_validator(self):
        for principal in (
            "workspace:cps:legacy-owner", "workspace:cit:w", "workspace:cps:w:suffix", None,
        ):
            body = {**copy.deepcopy(self.body), "principal": principal}
            with self.subTest(principal=principal), self.assertRaises(ValueError):
                validate_workspace_registration(self.policy, body, "cps", self.people)
