# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import json

from mock import patch, Mock
import pytest

from module_build_service.scheduler.greenwave import greenwave
from tests import clean_database, make_module_in_db


class TestGreenwaveQuery():

    def setup_method(self, method):
        clean_database()

    @patch("module_build_service.scheduler.greenwave.requests")
    def test_greenwave_query_decision(self, mock_requests):
        resp_status = 200
        resp_content = {
            "applicable_policies": ["osci_compose_modules"],
            "policies_satisfied": True,
            "satisfied_requirements": [
                {
                    "result_id": 7336633,
                    "testcase": "test-ci.test-module.tier1",
                    "type": "test-result-passed"
                },
                {
                    "result_id": 7336650,
                    "testcase": "test-ci.test-module.tier2",
                    "type": "test-result-passed"
                }
            ],
            "summary": "All required tests passed",
            "unsatisfied_requirements": []
        }
        response = Mock()
        response.json.return_value = resp_content
        response.status_code = resp_status
        mock_requests.post.return_value = response

        fake_build = make_module_in_db(
            "pkg:0.1:1:c1", [{
                "requires": {"platform": ["el8"]},
                "buildrequires": {"platform": ["el8"]},
            }],
        )
        got_response = greenwave.query_decision(fake_build, prod_version="xxxx-8")

        assert got_response == resp_content
        assert json.loads(mock_requests.post.call_args_list[0][1]["data"]) == {
            "decision_context": "test_dec_context",
            "product_version": "xxxx-8", "subject_type": "some-module",
            "subject_identifier": "pkg-0.1-1.c1"}
        assert mock_requests.post.call_args_list[0][1]["headers"] == {
            "Content-Type": "application/json"}
        assert mock_requests.post.call_args_list[0][1]["url"] == \
            "https://greenwave.example.local/api/v1.0/decision"

    @pytest.mark.parametrize("return_all", (False, True))
    @patch("module_build_service.scheduler.greenwave.requests")
    def test_greenwave_query_policies(self, mock_requests, return_all):
        resp_status = 200
        resp_content = {
            "policies": [
                {
                    "decision_context": "test_dec_context",
                    "product_versions": ["ver1", "ver3"],
                    "rules": [],
                    "subject_type": "some-module"
                },
                {
                    "decision_context": "test_dec_context",
                    "product_versions": ["ver1", "ver2"],
                    "rules": [],
                    "subject_type": "some-module"
                },
                {
                    "decision_context": "decision_context_2",
                    "product_versions": ["ver4"],
                    "rules": [],
                    "subject_type": "subject_type_2"
                }
            ]
        }
        selected_policies = {"policies": resp_content["policies"][:-1]}

        response = Mock()
        response.json.return_value = resp_content
        response.status_code = resp_status
        mock_requests.get.return_value = response

        got_response = greenwave.query_policies(return_all)

        if return_all:
            assert got_response == resp_content
        else:
            assert got_response == selected_policies
        assert mock_requests.get.call_args_list[0][1]["url"] == \
            "https://greenwave.example.local/api/v1.0/policies"

    @patch("module_build_service.scheduler.greenwave.requests")
    def test_greenwave_get_product_versions(self, mock_requests):
        resp_status = 200
        resp_content = {
            "policies": [
                {
                    "decision_context": "test_dec_context",
                    "product_versions": ["ver1", "ver3"],
                    "rules": [],
                    "subject_type": "some-module"
                },
                {
                    "decision_context": "test_dec_context",
                    "product_versions": ["ver1", "ver2"],
                    "rules": [],
                    "subject_type": "some-module"
                },
                {
                    "decision_context": "decision_context_2",
                    "product_versions": ["ver4"],
                    "rules": [],
                    "subject_type": "subject_type_2"
                }
            ]
        }
        expected_versions = {"ver1", "ver2", "ver3"}

        response = Mock()
        response.json.return_value = resp_content
        response.status_code = resp_status
        mock_requests.get.return_value = response

        versions_set = greenwave.get_product_versions()

        assert versions_set == expected_versions
        assert mock_requests.get.call_args_list[0][1]["url"] == \
            "https://greenwave.example.local/api/v1.0/policies"

    @pytest.mark.parametrize("policies_satisfied", (True, False))
    @patch("module_build_service.scheduler.greenwave.requests")
    def test_greenwave_check_gating(self, mock_requests, policies_satisfied):
        resp_status = 200
        policies_content = {
            "policies": [
                {
                    "decision_context": "test_dec_context",
                    "product_versions": ["ver1", "ver3"],
                    "rules": [],
                    "subject_type": "some-module"
                }
            ]
        }

        responses = [Mock() for i in range(3)]
        for r in responses:
            r.status_code = resp_status
        responses[0].json.return_value = policies_content
        responses[1].json.return_value = {"policies_satisfied": False}
        responses[2].json.return_value = {"policies_satisfied": policies_satisfied}
        mock_requests.get.return_value = responses[0]
        mock_requests.post.side_effect = responses[1:]

        fake_build = make_module_in_db(
            "pkg:0.1:1:c1", [{
                "requires": {"platform": ["el8"]},
                "buildrequires": {"platform": ["el8"]},
            }],
        )
        result = greenwave.check_gating(fake_build)

        assert result == policies_satisfied
