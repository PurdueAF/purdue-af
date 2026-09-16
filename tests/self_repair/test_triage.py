"""The flyte-free half of workflows/self-repair: fingerprints, redaction, the
agent's verdict and the pull request text."""

import json
from datetime import datetime, timezone

import pytest
from common import REPO, load_script

triage = load_script(REPO / "workflows/self-repair/triage.py", "self_repair_triage")


def line(pod, container, text, ts="2026-09-15T10:00:00+00:00"):
    return {"pod": pod, "container": container, "ts": ts, "line": text}


class TestWorkload:
    @pytest.mark.parametrize(
        "pod, workload",
        [
            ("jupyter-alice", "jupyter-*"),
            ("jupyter-alice-2dsmith", "jupyter-*"),
            ("dask-worker-96acb57f0729416b83289485f080ac8c-x7k2p", "dask-worker"),
            ("dask-scheduler-96acb57f0729416b83289485f080ac8c", "dask-scheduler"),
            ("agentic-interface-7d9f8c6b5-abcde", "agentic-interface"),
            ("hub-5f6d7c8b9-zz9zz", "hub"),
            ("tempo-0", "tempo"),
            ("af-x509-secrets-29312345-abcde", "af-x509-secrets"),
            ("alloy-abcde", "alloy"),
        ],
    )
    def test_strips_generated_suffixes(self, pod, workload):
        assert triage.workload_of(pod) == workload


class TestRedaction:
    def test_usernames_never_survive(self):
        text = (
            "jupyter-alice: cannot open /home/alice/.bashrc, "
            "/depot/cms/users/alice/x509up, /work/users/alice/env username=alice user=bob"
        )
        out = triage.redact(text)
        assert "alice" not in out and "bob" not in out
        assert "jupyter-<user>" in out and "/home/<user>/.bashrc" in out
        assert "username=<user>" in out and "user=<user>" in out

    def test_samples_are_redacted(self):
        incidents = triage.cluster(
            [line("jupyter-alice", "notebook", "Error: /home/alice/x missing")]
        )
        assert incidents[0].key.workload == "jupyter-*"
        assert "alice" not in incidents[0].key.message
        assert all("alice" not in sample for sample in incidents[0].evidence.samples)


class TestFingerprint:
    def test_same_error_across_pods_and_times_is_one_incident(self):
        lines = [
            line(
                "hub-5f6d7c8b9-aaaaa",
                "hub",
                "[E 2026-09-15 10:00:01.123 JupyterHub] Error at 0x7f3a for id 5f6b1c2d3e4f",
                "2026-09-15T10:00:01+00:00",
            ),
            line(
                "hub-5f6d7c8b9-bbbbb",
                "hub",
                "[E 2026-09-15 10:07:44.999 JupyterHub] Error at 0x7f3b for id 9a8b7c6d5e4f",
                "2026-09-15T10:07:44+00:00",
            ),
        ]
        incidents = triage.cluster(lines)
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident.evidence.count == 2 and incident.evidence.pods == 2
        assert incident.evidence.first_seen == "2026-09-15T10:00:01+00:00"
        assert incident.evidence.last_seen == "2026-09-15T10:07:44+00:00"
        assert (
            incident.key.message == "[E <ts> JupyterHub] Error at <addr> for id <hex>"
        )

    def test_different_containers_are_different_incidents(self):
        lines = [
            line("hub-1-aaaaa", "hub", "Error x"),
            line("hub-1-aaaaa", "proxy", "Error x"),
        ]
        keys = {incident.key.fingerprint for incident in triage.cluster(lines)}
        assert len(keys) == 2

    def test_most_frequent_first_and_samples_capped(self):
        lines = [line("a-1-aaaaa", "c", "Error one")] + [
            line(f"b-1-{i:05d}", "c", "Error two") for i in range(20)
        ]
        incidents = triage.cluster(lines)
        assert [incident.evidence.count for incident in incidents] == [20, 1]
        assert len(incidents[0].evidence.samples) == triage.MAX_SAMPLES

    def test_key_is_hashable_and_stable(self):
        a = triage.fingerprint("hub", "hub", "Error at <addr>")
        assert a == triage.fingerprint("hub", "hub", "Error at <addr>") and len(a) == 12
        assert hash(triage.IncidentKey(a, "hub", "hub", "m")) == hash(
            triage.IncidentKey(a, "hub", "hub", "m")
        )


class TestLoki:
    def test_url_carries_the_window_in_nanoseconds(self):
        start = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 9, 15, 10, 20, tzinfo=timezone.utc)
        url = triage.loki_url("http://loki:3100", "cms", start, end, 5000)
        assert url.startswith("http://loki:3100/loki/api/v1/query_range?")
        assert f"start={int(start.timestamp()) * 10**9}" in url
        assert f"end={int(end.timestamp()) * 10**9}" in url
        assert "namespace%3D%22cms%22" in url and "limit=5000" in url

    def test_parse_orders_by_time_and_keeps_pod_and_container(self):
        payload = {
            "data": {
                "result": [
                    {
                        "stream": {"pod": "p1", "container": "c1", "username": "alice"},
                        "values": [["1760000002000000000", "later"]],
                    },
                    {
                        "stream": {"pod": "p2", "container": "c2"},
                        "values": [["1760000001000000000", "earlier"]],
                    },
                ]
            }
        }
        lines = triage.parse_loki(payload)
        assert [entry["line"] for entry in lines] == ["earlier", "later"]
        assert lines[1] == {
            "pod": "p1",
            "container": "c1",
            "ts": lines[1]["ts"],
            "line": "later",
        }


class TestVerdict:
    def test_takes_the_last_json_object_with_fixable(self):
        reply = (
            'Looking at {"fixable": true} in the docs...\n'
            "Conclusion:\n"
            '{"fixable": false, "confidence": "0.9", "component": "apps/x", "title": "T", "reason": "R", "plan": ""}\n'
        )
        verdict = triage.parse_verdict(reply)
        assert (
            verdict.fixable is False
            and verdict.confidence == 0.9
            and verdict.component == "apps/x"
        )

    def test_no_verdict_means_not_fixable(self):
        verdict = triage.parse_verdict("I could not decide.")
        assert verdict.fixable is False and "no verdict" in verdict.reason

    def test_garbage_confidence_is_zero(self):
        assert (
            triage.parse_verdict('{"fixable": true, "confidence": "high"}').confidence
            == 0.0
        )


class TestReply:
    def event(self, kind, **part):
        return json.dumps({"type": kind, "sessionID": "s", "part": part})

    def test_text_is_collected_and_reading_stops_at_the_final_turn(self):
        consumed = []

        def events():
            for e in [
                "> build · big-pickle",  # not an event
                self.event("step_start"),
                self.event("text", text="Looking..."),
                self.event("step_finish", reason="tool-calls"),
                self.event("text", text='{"fixable": false}'),
                self.event("step_finish", reason="stop"),
                self.event("text", text="never read"),
            ]:
                consumed.append(e)
                yield e

        reply = triage.collect_reply(events())
        assert reply.finished and not reply.error
        assert reply.text == 'Looking...\n{"fixable": false}'
        assert len(consumed) == 6, "the reader must stop consuming at the final turn"

    def test_error_event_and_truncated_stream_are_not_final(self):
        errored = triage.collect_reply(
            [
                self.event("text", text="x"),
                json.dumps({"type": "error", "error": {"name": "ProviderAuthError"}}),
            ]
        )
        assert (
            not errored.finished
            and "ProviderAuthError" in errored.error
            and errored.text == "x"
        )
        truncated = triage.collect_reply([self.event("text", text="x")])
        assert not truncated.finished and "ended before" in truncated.error


class TestNarration:
    def test_observe_sees_every_parsed_event_in_order(self):
        seen = []
        events = [
            json.dumps({"type": "step_start", "part": {}}),
            "not json",
            json.dumps({"type": "text", "part": {"text": "hi"}}),
            json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
        ]
        triage.collect_reply(events, seen.append)
        assert [e["type"] for e in seen] == ["step_start", "text", "step_finish"]

    @pytest.mark.parametrize(
        "event, expected",
        [
            (
                {"type": "text", "part": {"text": "  Looking at apps/x  "}},
                "says: Looking at apps/x",
            ),
            ({"type": "text", "part": {"text": "   "}}, None),
            ({"type": "step_start", "part": {}}, None),
            (
                {
                    "type": "tool",
                    "part": {
                        "tool": "bash",
                        "state": {"status": "running", "input": {"command": "ls"}},
                    },
                },
                None,
            ),
            (
                {
                    "type": "tool",
                    "part": {
                        "tool": "bash",
                        "state": {"status": "completed", "title": "git status"},
                    },
                },
                "bash completed: git status",
            ),
            (
                {
                    "type": "tool",
                    "part": {
                        "tool": "read",
                        "state": {
                            "status": "completed",
                            "input": {"filePath": "/r/a.py"},
                        },
                    },
                },
                "read completed: filePath=/r/a.py",
            ),
            (
                {
                    "type": "step_finish",
                    "part": {
                        "reason": "tool-calls",
                        "tokens": {"input": 12, "output": 3},
                    },
                },
                "step tool-calls (12 in / 3 out tokens)",
            ),
            (
                {"type": "error", "error": {"name": "ProviderAuthError"}},
                "error: name=ProviderAuthError",
            ),
        ],
    )
    def test_describe_event(self, event, expected):
        assert triage.describe_event(event) == expected

    def test_long_text_is_cut(self):
        line = triage.describe_event({"type": "text", "part": {"text": "x" * 500}})
        assert line is not None and len(line) < 220 and line.endswith("…")


class TestGitHub:
    def test_pull_request_body_is_draft_evidence_without_usernames(self):
        key = triage.IncidentKey("abc123def456", "notebook", "jupyter-*", "Error <hex>")
        evidence = triage.Evidence(
            ["jupyter-<user>/notebook: Error 1"], 3, 2, "t0", "t1"
        )
        verdict = triage.Verdict(
            True, 0.9, "docker/purdue-af", "Fix it", "Because.", "Edit x."
        )
        body = triage.pull_request_body(
            key, evidence, verdict, "I changed x.", "run-1", "opencode/big-pickle"
        )
        assert (
            "abc123def456" in body
            and "3 in 2 pod(s)" in body
            and "I changed x." in body
        )
        assert "Draft on purpose" in body and "run-1" in body

    def test_requests_are_authenticated_and_open_prs_are_looked_up_by_head(
        self, monkeypatch
    ):
        seen = {}

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(self.payload).encode()

        def urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = request.data
            return Response(
                [{"html_url": "https://github.com/PurdueAF/purdue-af/pull/1"}]
            )

        monkeypatch.setattr(triage.urllib.request, "urlopen", urlopen)
        url = triage.open_pull_request("PurdueAF/purdue-af", "self-repair/abc", "tok")
        assert url.endswith("/pull/1")
        assert seen["method"] == "GET" and seen["auth"] == "Bearer tok"
        assert (
            "head=PurdueAF%3Aself-repair%2Fabc" in seen["url"]
            and "state=open" in seen["url"]
        )

        def urlopen_create(request, timeout):
            seen["create"] = json.loads(request.data)
            return Response(
                {"html_url": "https://github.com/PurdueAF/purdue-af/pull/2"}
            )

        monkeypatch.setattr(triage.urllib.request, "urlopen", urlopen_create)
        triage.create_pull_request(
            "PurdueAF/purdue-af", "tok", "self-repair/abc", "main", "T", "B"
        )
        assert seen["create"] == {
            "title": "T",
            "head": "self-repair/abc",
            "base": "main",
            "body": "B",
            "draft": True,
        }
