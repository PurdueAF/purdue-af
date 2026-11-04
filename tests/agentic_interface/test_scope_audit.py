"""Tests for scope_audit.py: labels promql_scope does not cover are checked for
values that name a person's object or another namespace."""

import re
from urllib.parse import parse_qs

import respx
import scope_audit
from httpx import AsyncClient, Response


def test_covered_labels_are_skipped_and_the_rest_checked():
    assert {"pod", "username", "namespace", "__name__"} <= scope_audit.COVERED
    [person] = scope_audit.checks("csi_volume")
    assert 'csi_volume=~".*(?:' in person and "ray\\\\-train\\\\-gateway" in person
    assert len(scope_audit.checks("target_namespace")) == 2


def test_a_label_name_outside_the_legacy_charset_is_quoted():
    [check] = scope_audit.checks("k8s.pod.name")
    assert check.startswith('{__name__=~".+","k8s.pod.name"=~')


def test_names_a_person_matches_their_objects_anywhere_in_a_value():
    regex = re.compile(scope_audit.NAMES_A_PERSON)
    assert regex.fullmatch("k8s_notebook_purdue-af-12_cms_x_0")
    assert regex.fullmatch("ray-job-1dc49bb9e8d9fecb-logs")
    assert not regex.fullmatch("hub-6f7c")


@respx.mock
async def test_audit_counts_series_per_uncovered_label(monkeypatch):
    async def scope(query, owned):
        assert owned.username == ""
        return query

    monkeypatch.setattr(scope_audit, "scope", scope)

    def labels(request):
        return Response(200, json={"data": ["__name__", "pod", "csi_volume", "le"]})

    def query(request):
        asked = parse_qs(request.content.decode())["query"][0]
        count = "4" if "csi_volume" in asked else "0"
        return Response(
            200, json={"data": {"result": [{"metric": {}, "value": [0, count]}]}}
        )

    for url in scope_audit.SOURCES.values():
        respx.post(f"{url}/api/v1/labels").mock(side_effect=labels)
        respx.post(f"{url}/api/v1/query").mock(side_effect=query)
    async with AsyncClient() as client:
        found = await scope_audit.audit(client)
    assert found == {("af", "csi_volume"): 4, ("cluster", "csi_volume"): 4}
