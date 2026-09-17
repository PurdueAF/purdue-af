"""The flyte-free half of workflows/self-repair: fingerprints, redaction, the
agent's verdict and the pull request text."""

import io
import json
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from unittest.mock import AsyncMock

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

    def test_other_pods_named_in_the_message_do_not_split_the_incident(self):
        """alloy reports one tailer loss per vanished pod; that is one incident."""
        template = (
            'ts=2026-09-16T17:28:57.8Z level=warn msg="tailer stopped; will retry" '
            "component_id=loki.source.kubernetes.pods target=cms/{pod}:triton-server "
            'err="pods \\"{pod}\\" not found"'
        )
        lines = [
            line(
                "alloy-vqlcj",
                "alloy",
                template.format(pod="supersonic-pr-triton-85866ffc9b-mt7n9"),
            ),
            line(
                "alloy-nxhbw",
                "alloy",
                template.format(pod="supersonic-pr-triton-85866ffc9b-vvdxh"),
            ),
            line(
                "alloy-abcde",
                "alloy",
                "tailer stopped target=cms/af-node-probe-eos-x7k2p:probe",
            ),
            line(
                "alloy-abcde",
                "alloy",
                "tailer stopped target=cms/af-node-probe-eos-m9q4z:probe",
            ),
            line(
                "alloy-abcde",
                "alloy",
                "lost dask-worker-96acb57f0729416b83289485f080ac8c-x7k2p",
            ),
            line(
                "alloy-abcde",
                "alloy",
                "lost dask-worker-0123456789abcdef0123456789abcdef-zz9zz",
            ),
        ]
        incidents = triage.cluster(lines)
        messages = sorted(i.key.message for i in incidents)
        assert len(incidents) == 3, messages
        assert (
            "supersonic-pr-triton-<pod>" in messages[2] and "mt7n9" not in messages[2]
        )
        assert messages[1].endswith("af-node-probe-eos-<pod>:probe")
        assert messages[0] == "lost dask-worker-<id>-<pod>"

    def test_ordinary_words_are_not_taken_for_pod_suffixes(self):
        assert triage.normalize(
            "mount /work failed; retry proxy-public shell", "hub-zz9zz"
        ) == ("mount /work failed; retry proxy-public shell")

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
        assert "pod%3D~%22" in url, "the allowlist is applied in the selector"
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
        selector = query.split("|~")[0]
        assert "\\" not in selector, (
            "LogQL parses the string before the regex: a backslash before a dash is "
            "an invalid char escape and Loki answers 400"
        )

    def test_a_prefix_that_needs_escaping_is_refused(self):
        with pytest.raises(ValueError):
            triage.pod_regex(("hub", "web.app"))

    @pytest.mark.parametrize(
        "pod",
        [
            "hub-5f6d7c8b9-zz9zz",
            "proxy-7d9f8c6b5-abcde",
            "alloy-abcde",
            "loki-0",
            "loki-chunks-cache-0",
            "flyte-console-d78d4dc8f-gp26v",
            "af-node-probe-cvmfs-abcde",
            "af-userlist-sync-purdue-29312345-abcde",
            "servicex-eos-did-finder-xrootd-abcde",
            "api-dask-gateway-k8s-slurm-abcde",
        ],
    )
    def test_repo_workloads_are_watched(self, pod):
        assert triage.watched(pod), pod

    @pytest.mark.parametrize(
        "pod",
        [
            "supersonic-pr-triton-7d9f8c6b5-abcde",
            "supersonic-af-envoy-abcde",
            "supersonic-model-manager-abcde",
            "sonic-ray-hvfln-head",
            "kuberay-operator-abcde",
            "interlink-hammer-node-0",
            "interlink-negishi-node-0",
        ],
    )
    def test_ignored_workloads_are_not_read(self, pod):
        assert not triage.watched(pod), pod

    def test_every_ignored_prefix_is_something_the_repo_deploys(self):
        """A typo here would silently ignore nothing."""
        for prefix in triage.IGNORED_WORKLOADS:
            assert prefix in triage.WATCHED_WORKLOADS, prefix

    @pytest.mark.parametrize(
        "pod",
        [
            "purdue-af-182",
            "dask-worker-96acb57f0729416b83289485f080ac8c-x7k2p",
            "dask-scheduler-96acb57f0729416b83289485f080ac8c",
        ],
    )
    def test_user_workloads_are_watched_but_ranked_last(self, pod):
        assert triage.watched(pod), pod
        assert triage.workload_of(pod) in triage.USER_WORKLOADS

    def test_infrastructure_outranks_user_workloads_regardless_of_count(self):
        lines = [line(f"purdue-af-{i}", "notebook", "Error user") for i in range(50)]
        lines += [
            line(
                "dask-worker-96acb57f0729416b83289485f080ac8c-x7k2p",
                "dask-worker",
                "Error dask",
            )
        ] * 20
        lines += [line("hub-5f6d7c8b9-zz9zz", "hub", "Error hub")] * 2
        lines += [line("alloy-abcde", "alloy", "Error alloy")] * 5
        order = [(i.key.workload, i.evidence.count) for i in triage.cluster(lines)]
        assert order == [
            ("alloy", 5),
            ("hub", 2),
            ("purdue-af", 50),
            ("dask-worker", 20),
        ]

    @pytest.mark.parametrize(
        "pod",
        [
            "jupyter-alice",
            "self-repair-analyze-hra3y-60ef-a0-0",  # its own logs quote errors
            "gen3",
            "gen0-abcde",
            "etcd-0",
            "eos-fuse-nopriv-65dcf5689-vpbf9",
            "kaniko-build-dask-hg5pd",
            "hubris-abcde",  # a prefix without its dash is not a match
        ],
    )
    def test_everything_else_is_not(self, pod):
        assert not triage.watched(pod), pod

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


class TestPrompts:
    def test_both_prompts_state_the_time_budget(self):
        prompts = load_script(
            REPO / "workflows/self-repair/prompts.py", "self_repair_prompts"
        )
        analyze = prompts.ANALYZE.substitute(incident="i", context="c", minutes=25)
        fix = prompts.FIX.substitute(
            incident="i",
            context="c",
            title="t",
            component="c",
            reason="r",
            plan="p",
            minutes=25,
        )
        for text in (analyze, fix):
            assert "about 25 minutes" in text
            assert "$" not in text.replace("$schema", "")


class TestAnalysisBudget:
    """max_incidents caps fresh analyses; cache hits are free and never starve
    the incidents further down the list."""

    def incidents(self, n):
        return [
            triage.Incident(
                triage.IncidentKey(f"fp{i:02d}", "c", "w", f"m{i}"),
                triage.Evidence([], 1, 1, "t", "t"),
            )
            for i in range(n)
        ]

    def run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_hits_do_not_count_and_concurrency_stays_within_budget(self):
        import asyncio

        cached = {"fp00", "fp01", "fp02"}
        in_flight = 0
        peak = 0
        logs = []

        async def spawn(incident):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.001 if incident.key.fingerprint in cached else 0.02)
            in_flight -= 1
            hit = incident.key.fingerprint in cached
            return triage.Verdict(False, 0.5, reason="r"), (
                "CACHE_HIT" if hit else "CACHE_POPULATED"
            )

        outcomes = self.run(
            triage.analyze_within_budget(self.incidents(8), 2, spawn, logs.append)
        )
        attempted = [incident.key.fingerprint for incident, _ in outcomes]
        assert attempted == ["fp00", "fp01", "fp02", "fp03", "fp04"], (
            "3 hits then 2 fresh"
        )
        assert peak <= 2
        assert sum("known, from cache" in line for line in logs) == 3
        assert all(isinstance(result, triage.Verdict) for _, result in outcomes)

    def test_failures_count_as_fresh_and_are_returned(self):
        async def spawn(incident):
            if incident.key.fingerprint == "fp00":
                raise RuntimeError("boom")
            return triage.Verdict(True, 0.9), "CACHE_POPULATED"

        outcomes = self.run(triage.analyze_within_budget(self.incidents(5), 2, spawn))
        assert [i.key.fingerprint for i, _ in outcomes] == ["fp00", "fp01"]
        assert isinstance(outcomes[0][1], RuntimeError)
        assert isinstance(outcomes[1][1], triage.Verdict)

    def test_empty_list_and_zero_budget(self):
        spawn = AsyncMock()

        assert self.run(triage.analyze_within_budget([], 5, spawn)) == []
        assert self.run(triage.analyze_within_budget(self.incidents(3), 0, spawn)) == []
        spawn.assert_not_called()


class TestReport:
    def rows(self):
        R = triage.Row
        return [
            R(
                "aaaa",
                "hub",
                "hub",
                3,
                "not fixable",
                0.9,
                reason="upstream",
                cache="CACHE_HIT",
            ),
            R(
                "bbbb",
                "alloy",
                "alloy",
                40,
                "fixable",
                0.8,
                "Fix tailer",
                "apps/monitoring/alloy",
                "because <x>",
                "https://github.com/PurdueAF/purdue-af/pull/99",
            ),
            R("cccc", "purdue-af", "notebook", 25, "not analyzed"),
            R(
                "dddd",
                "flyte",
                "flyte",
                1,
                "failed",
                reason="opencode gave no final answer",
            ),
            R(
                "eeee",
                "hub",
                "proxy",
                7,
                "fixable",
                0.75,
                "Raise limit",
                "apps/jupyterhub",
            ),
        ]

    def test_headline_counts_fixable_over_analyzed_and_orders_fixable_first(self):
        page = triage.report_html("hra3y", "16:26:20..16:46:20", self.rows())
        assert "<h2>2 of 3 analyzed incidents fixable in this repository</h2>" in page
        assert (
            "5 incidents, 3 analyzed (1 from cache), 1 failed, 1 pull request(s)"
            in page
        )
        positions = [
            page.index(f"<code>{fp}</code>")
            for fp in ("bbbb", "eeee", "aaaa", "dddd", "cccc")
        ]
        assert positions == sorted(positions), (
            "fixable, not fixable, failed, not analyzed"
        )
        assert 'href="https://github.com/PurdueAF/purdue-af/pull/99">99</a>' in page
        assert "&lt;x&gt;" in page and "<x>" not in page, "reasons are escaped"
        assert "(cache)" in page


class TestOpencodeLog:
    RATE_LIMIT = (
        'timestamp=2026-09-16T17:45:45.898Z level=ERROR run=dd25936d message="stream error" '
        "providerID=opencode modelID=big-pickle session.id=ses_f54ae4024ffe7ykdrF7I1WCxYg "
        'small=false agent=build mode=primary error.error="AI_APICallError: Rate limit exceeded. Please try again later."'
    )

    def test_a_rate_limit_is_a_provider_error(self):
        summary, error = triage.opencode_log_line(self.RATE_LIMIT)
        assert error == "AI_APICallError: Rate limit exceeded. Please try again later."
        assert summary.startswith("error: message=stream error")
        assert "dd25936d" not in summary and "ses_" not in summary, "ids are noise"
        assert "agent=build" in summary

    def test_info_lines_are_ignored_and_warnings_relayed_without_error(self):
        assert (
            triage.opencode_log_line(
                'timestamp=x level=INFO run=1 message="loop" step=5'
            )
            is None
        )
        summary, error = triage.opencode_log_line(
            'timestamp=x level=WARN run=1 message="tailer stopped; will retry" n=3'
        )
        assert (
            error is None and summary == "warn: message=tailer stopped; will retry n=3"
        )


class TestStructuredMessages:
    """logfmt, JSON and traceback lines are keyed on what happened, not on
    field order, extra fields or file paths."""

    TRAEFIK = [
        'time="2026-09-16T19:14:47Z" level=error msg="Cannot create service: subset not found" providerName=kubernetescrd ingress=dask-abc namespace=cms',
        'time="2026-09-16T19:14:48Z" level=error msg="Cannot create service: subset not found" serviceName=dask-def servicePort="{0 8786 }" ingress=dask-def',
        'time="2026-09-16T19:14:49Z" level=error msg="Cannot create service: subset not found" namespace=cms servicePort="{0 8786 }" providerName=kubernetescrd',
    ]

    def test_logfmt_field_order_and_extra_fields_do_not_matter(self):
        keys = {triage.structured_message(entry) for entry in self.TRAEFIK}
        assert keys == {"error Cannot create service: subset not found"}

    def test_logfmt_keeps_the_error_field(self):
        line = 'ts=2026-09-16T17:28:57Z level=warn msg="tailer stopped; will retry" component_id=loki.source.kubernetes.pods target=cms/x:y err="pods \\"x\\" not found"'
        assert (
            triage.structured_message(line)
            == 'warn tailer stopped; will retry | pods \\"x\\" not found'
        )

    def test_json_lines(self):
        line = '2026-09-16T10:00:00Z {"level": "error", "message": "connection refused", "pod": "hub-1", "attempt": 3}'
        assert triage.structured_message(line) == "error connection refused"

    def test_traceback_is_keyed_on_the_exception(self):
        line = (
            'Traceback: \' File "/work/users/alice/envs/my-env/lib/python3.12/site-packages/coffea/processor/executor.py", line 1, in x\\n'
            '  raise RuntimeError("boom")\\nRuntimeError: Compute Failed while reading file 7\''
        )
        assert (
            triage.structured_message(line)
            == "Traceback: RuntimeError: Compute Failed while reading file 7'"
        )

    def test_plain_lines_are_untouched(self):
        assert (
            triage.structured_message("[E 10:00:00 JupyterHub] Error at 0x7f") is None
        )

    def test_three_traefik_variants_are_one_incident(self):
        lines = [
            line("traefik-dask-gateway-k8s-1-abcde", "traefik", entry)
            for entry in self.TRAEFIK
        ]
        assert len(triage.cluster(lines)) == 1


class TestGroups:
    def incidents(self):
        return [
            triage.Incident(
                triage.IncidentKey(f"fp{i}", "c", "w", f"m{i}"),
                triage.Evidence(
                    [f"s{i}"], 10 - i, 1, f"t{i}", f"t{i}", f"pod{i}", f"t{i}"
                ),
            )
            for i in range(4)
        ]

    def test_prompt_lists_every_incident(self):
        prompt = triage.grouping_prompt(self.incidents())
        assert all(f"- fp{i}:" in prompt for i in range(4)) and '"groups"' in prompt

    def test_parse_validates_and_completes(self):
        reply = 'Here you go:\n{"groups": [{"label": "traefik stale service", "members": ["fp1", "fp0", "nope"]}, {"label": "x", "members": ["fp0"]}]}'
        groups = triage.parse_groups(reply, self.incidents())
        assert [(g.label, g.members) for g in groups] == [
            (
                "traefik stale service",
                ["fp0", "fp1"],
            ),  # representative first: highest count
            ("m2", ["fp2"]),
            ("m3", ["fp3"]),
        ], "unknown dropped, duplicate ignored, missing ones alone, sorted by count"

    def test_garbage_reply_means_singletons(self):
        groups = triage.parse_groups("no json here", self.incidents())
        assert [g.members for g in groups] == [["fp0"], ["fp1"], ["fp2"], ["fp3"]]

    def test_representative_carries_the_group_counts(self):
        incidents = self.incidents()
        rep = triage.representative(triage.Group("g", ["fp0", "fp1", "fp2"]), incidents)
        assert rep.key.fingerprint == "fp0"
        assert rep.evidence.count == 10 + 9 + 8 and rep.evidence.pods == 3
        assert rep.evidence.first_seen == "t0" and rep.evidence.last_seen == "t2"
        assert rep.evidence.samples == ["s0"]

    def test_representative_keeps_the_sample_pointer(self):
        """The analysis fetches the lines around the first sample (a traceback's
        exception is below its header); merging groups must not lose it."""
        incidents = self.incidents()
        rep = triage.representative(triage.Group("g", ["fp1", "fp2"]), incidents)
        assert rep.evidence.first_pod == "pod1" and rep.evidence.first_ts == "t1"


class TestContextAndGuards:
    def test_context_url_covers_five_seconds_around_the_sample_forward(self):
        url = triage.context_url(
            "http://loki:3100",
            "cms",
            "purdue-af-182",
            "af-pod-monitor",
            "2026-09-16T20:03:25.905093+00:00",
        )
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert q["query"] == [
            '{namespace="cms", pod="purdue-af-182", container="af-pod-monitor"}'
        ]
        assert int(q["end"][0]) - int(q["start"][0]) == 10 * 10**9
        assert q["direction"] == ["forward"] and q["limit"] == [
            str(triage.CONTEXT_LINES)
        ]

    def test_cluster_remembers_where_the_first_sample_came_from(self):
        lines = [
            line("hub-1-aaaaa", "hub", "Error x", "2026-09-16T10:00:00+00:00"),
            line("hub-1-bbbbb", "hub", "Error x", "2026-09-16T10:00:05+00:00"),
        ]
        (incident,) = triage.cluster(lines)
        assert incident.evidence.first_pod == "hub-1-aaaaa"
        assert incident.evidence.first_ts == "2026-09-16T10:00:00+00:00"

    @pytest.mark.parametrize(
        "diff, expected",
        [
            (
                '-    log.error("%s did not return in %ss", cmd, t)\n+    log.warning("%s did not return in %ss", cmd, t)\n',
                True,
            ),
            ('-    logger.exception("boom")\n+    logger.debug("boom")\n', True),
            ('-    log.error("x")\n+    log.error("x")\n+    retry()\n', False),
            (
                "-    directories = discover_directories()\n+    directories = init_directories()\n",
                False,
            ),
            ("", False),
        ],
    )
    def test_silences_detects_log_level_only_changes(self, diff, expected):
        assert triage.silences("--- a\n+++ b\n" + diff) is expected

    def test_rate_limit_errors_are_recognised(self):
        assert triage.is_rate_limit(
            '{"name": "APIError", "data": {"message": "GenAI Studio rate limit exceeded", "statusCode": 429}}'
        )
        assert triage.is_rate_limit(
            "AI_APICallError: Rate limit exceeded. Please try again later."
        )
        assert not triage.is_rate_limit("Bad Request: model not found")


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

    def test_pull_requests_are_titled_and_labelled_as_self_repair(self, monkeypatch):
        calls = []

        class Response:
            def __init__(self, status, payload):
                self.status, self.payload = status, payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(self.payload).encode()

        def urlopen(request, timeout):
            calls.append(
                (
                    request.get_method(),
                    request.full_url.split("api.github.com")[1],
                    json.loads(request.data) if request.data else None,
                )
            )
            path = calls[-1][1]
            if path.endswith("/labels/self-repair"):
                raise urllib.error.HTTPError(
                    request.full_url, 404, "Not Found", {}, None
                )
            if path.endswith("/pulls"):
                return Response(
                    201,
                    {
                        "html_url": "https://github.com/PurdueAF/purdue-af/pull/7",
                        "number": 7,
                    },
                )
            return Response(200, {})

        monkeypatch.setattr(triage.urllib.request, "urlopen", urlopen)
        url = triage.create_pull_request(
            "PurdueAF/purdue-af", "tok", "self-repair-abc", "main", "Fix it", "B"
        )
        assert url.endswith("/pull/7")
        methods = [(m, p) for m, p, _ in calls]
        assert methods == [
            ("POST", "/repos/PurdueAF/purdue-af/pulls"),
            ("GET", "/repos/PurdueAF/purdue-af/labels/self-repair"),
            ("POST", "/repos/PurdueAF/purdue-af/labels"),
            ("POST", "/repos/PurdueAF/purdue-af/issues/7/labels"),
        ]
        assert (
            calls[0][2]["title"] == "[self-repair] Fix it"
            and calls[0][2]["draft"] is True
        )
        assert calls[3][2] == {"labels": ["self-repair"]}

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
            if request.full_url.endswith("/pulls"):
                seen["create"] = json.loads(request.data)
            return Response(
                {
                    "html_url": "https://github.com/PurdueAF/purdue-af/pull/2",
                    "number": 2,
                }
            )

        monkeypatch.setattr(triage.urllib.request, "urlopen", urlopen_create)
        triage.create_pull_request(
            "PurdueAF/purdue-af", "tok", "self-repair/abc", "main", "T", "B"
        )
        assert seen["create"] == {
            "title": "[self-repair] T",
            "head": "self-repair/abc",
            "base": "main",
            "body": "B",
            "draft": True,
        }


class TestModelChoice:
    def test_first_answering_model_wins_and_nothing_after_it_is_probed(self):
        asked = []

        def probe(model):
            asked.append(model)
            return (model == "genai/b", 1.5, "x")

        model, probes = triage.pick_model(["genai/a", "genai/b", "genai/c"], probe)
        assert model == "genai/b"
        assert asked == ["genai/a", "genai/b"]
        assert [(p.model, p.available) for p in probes] == [
            ("genai/a", False),
            ("genai/b", True),
        ]

    def test_no_answer_means_no_model_and_every_probe_reported(self):
        model, probes = triage.pick_model(
            ["a", "b"], lambda m: (False, 30.0, "timeout")
        )
        assert model is None and [p.model for p in probes] == ["a", "b"]

    def test_probe_is_tiny_and_not_streamed(self):
        body = triage.probe_body("gemma4:26b-a4b")
        assert body["model"] == "gemma4:26b-a4b"
        assert body["stream"] is False and body["max_tokens"] <= 16


class TestMetrics:
    def metrics(self, **overrides):
        base = dict(
            started=1789651043.0,
            outcome="ok",
            model="genai/b",
            models=["genai/a", "genai/b"],
            probes=[
                triage.Probe("genai/a", False, 30.0, "timeout"),
                triage.Probe("genai/b", True, 0.4, "answered"),
            ],
            duration=61.5,
            error_lines=47,
            incidents=9,
            groups=4,
            analyses={"fresh": 2, "cache_hit": 1, "failed": 1},
            fixable=1,
            pull_requests=1,
        )
        base.update(overrides)
        return triage.TickMetrics(**base)

    def test_counters_carry_on_from_what_the_gateway_holds(self):
        page = (
            'push_time_seconds{instance="",job="self-repair"} 1.7e+09\n'
            'self_repair_ticks_total{instance="",job="self-repair",outcome="ok"} 4\n'
            'self_repair_ticks_total{instance="",job="self-repair",outcome="failed"} 2\n'
            'self_repair_analyses_total{instance="",job="self-repair",result="fresh"} 10\n'
            'self_repair_last_tick_incidents{instance="",job="self-repair"} 3\n'
        )
        previous = triage.parse_counters(page)
        assert previous == {
            ("self_repair_ticks_total", (("outcome", "ok"),)): 4.0,
            ("self_repair_ticks_total", (("outcome", "failed"),)): 2.0,
            ("self_repair_analyses_total", (("result", "fresh"),)): 10.0,
        }, "gauges and the gateway's own series are not counters"
        text = triage.exposition(self.metrics(), previous)
        assert 'self_repair_ticks_total{outcome="ok"} 5' in text
        assert 'self_repair_ticks_total{outcome="failed"} 2' in text, "untouched, kept"
        assert 'self_repair_analyses_total{result="fresh"} 12' in text
        assert 'self_repair_analyses_total{result="failed"} 1' in text

    def test_gauges_describe_the_newest_tick(self):
        text = triage.exposition(self.metrics(), {})
        assert "self_repair_last_tick_timestamp_seconds 1789651043\n" in text
        assert "self_repair_last_tick_duration_seconds 61.5\n" in text
        assert 'self_repair_last_tick_outcome{outcome="ok"} 1' in text
        assert 'self_repair_last_tick_outcome{outcome="no_model"} 0' in text
        assert 'self_repair_model_available{model="genai/a"} 0' in text
        assert 'self_repair_model_available{model="genai/b"} 1' in text
        assert 'self_repair_model_probe_seconds{model="genai/a"} 30' in text
        assert 'self_repair_model_selected{model="genai/a"} 0' in text
        assert 'self_repair_model_selected{model="genai/b"} 1' in text
        assert (
            'self_repair_model_probes_total{available="false",model="genai/a"} 1'
            in text
        )
        assert "self_repair_last_tick_error_lines 47\n" in text

    def test_every_series_has_help_and_type_and_a_known_name(self):
        text = triage.exposition(self.metrics(), {})
        names = {
            line.split("{")[0].split(" ")[0]
            for line in text.splitlines()
            if not line.startswith("#")
        }
        assert names <= set(triage.METRICS)
        for name in names:
            kind = triage.METRICS[name][0]
            assert f"# TYPE {name} {kind}" in text and f"# HELP {name} " in text

    def test_a_tick_without_a_model_still_counts(self):
        metrics = self.metrics(
            outcome="no_model", model="", analyses={}, fixable=0, pull_requests=0
        )
        text = triage.exposition(metrics, {})
        assert 'self_repair_ticks_total{outcome="no_model"} 1' in text
        assert 'self_repair_last_tick_outcome{outcome="ok"} 0' in text
        assert 'self_repair_model_selected{model="genai/b"} 0' in text

    def test_label_values_are_escaped(self):
        metrics = self.metrics(models=['we"ird\\'], model='we"ird\\', probes=[])
        text = triage.exposition(metrics, {})
        assert 'self_repair_model_selected{model="we\\"ird\\\\"} 1' in text


class Body(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


LOKI_PAYLOAD = {
    "data": {
        "result": [
            {
                "stream": {"pod": "jupyter-alice", "container": "notebook"},
                "values": [["1789552800000000000", "Error in /home/alice/x"]],
            }
        ]
    }
}


class TestLokiCalls:
    def test_query_loki_fetches_and_parses(self, monkeypatch):
        seen = {}

        def urlopen(url, timeout):
            seen["url"] = url
            return Body(json.dumps(LOKI_PAYLOAD).encode())

        monkeypatch.setattr(triage.urllib.request, "urlopen", urlopen)
        start = datetime(2026, 9, 15, 10, tzinfo=timezone.utc)

        lines = triage.query_loki("http://loki", "cms", start, start, 10, ("hub",))

        assert seen["url"].startswith("http://loki/loki/api/v1/query_range?")
        assert lines == [
            {
                "pod": "jupyter-alice",
                "container": "notebook",
                "ts": "2026-09-16T10:00:00+00:00",
                "line": "Error in /home/alice/x",
            }
        ]

    def test_context_lines_are_redacted(self, monkeypatch):
        monkeypatch.setattr(
            triage.urllib.request,
            "urlopen",
            lambda url, timeout: Body(json.dumps(LOKI_PAYLOAD).encode()),
        )

        lines = triage.query_context(
            "http://loki", "cms", "p", "c", "2026-09-16T10:00:00+00:00"
        )

        assert lines == ["Error in /home/<user>/x"]

    @pytest.mark.parametrize(
        "failure", [ConnectionRefusedError("refused"), b"<html>bad gateway</html>"]
    )
    def test_context_is_empty_when_loki_fails(self, monkeypatch, failure):
        def urlopen(url, timeout):
            if isinstance(failure, Exception):
                raise failure
            return Body(failure)

        monkeypatch.setattr(triage.urllib.request, "urlopen", urlopen)

        assert (
            triage.query_context("http://loki", "cms", "p", "c", "2026-09-16T10:00:00")
            == []
        )


class TestParsingEdges:
    def test_blank_lines_make_no_incident(self):
        assert triage.cluster([line("hub-1", "hub", "   ")]) == []

    def test_brace_that_is_not_json_falls_through(self):
        assert triage.structured_message("Error: bad dict {not: json}") is None

    def test_groups_skip_invalid_json_and_non_object_entries(self):
        incidents = TestGroups().incidents()
        reply = '{oops} {"groups": ["fp0", {"label": "", "members": ["fp1", "fp2"]}]}'

        groups = triage.parse_groups(reply, incidents)

        assert groups[0].members == ["fp1", "fp2"]
        assert groups[0].label == "m1", "an empty label falls back to the message"

    def test_reply_skips_unparsable_events(self):
        reply = triage.collect_reply(
            [
                "{truncated",
                json.dumps({"type": "text", "part": {"text": "ok"}}),
                json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
            ]
        )
        assert reply == triage.Reply("ok", True)

    def test_verdict_after_a_stray_brace(self):
        verdict = triage.parse_verdict('see {this} then {"fixable": false}')
        assert verdict.fixable is False and verdict.reason == ""

    def test_counters_skip_comments_and_unparsable_values(self):
        page = (
            "# HELP self_repair_ticks_total Ticks by outcome\n"
            "\n"
            'self_repair_ticks_total{outcome="ok"} NaNish\n'
            'self_repair_ticks_total{outcome="failed"} 2\n'
        )
        assert triage.parse_counters(page) == {
            ("self_repair_ticks_total", (("outcome", "failed"),)): 2.0
        }


class TestLabelCreation:
    def test_an_existing_label_is_left_alone(self, monkeypatch):
        calls = []

        def urlopen(request, timeout):
            calls.append(request.get_method())
            return Body(b"{}")

        monkeypatch.setattr(triage.urllib.request, "urlopen", urlopen)
        triage.ensure_label("PurdueAF/purdue-af", "tok")
        assert calls == ["GET"]

    def test_other_github_errors_propagate(self, monkeypatch):
        def urlopen(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

        monkeypatch.setattr(triage.urllib.request, "urlopen", urlopen)
        with pytest.raises(urllib.error.HTTPError):
            triage.ensure_label("PurdueAF/purdue-af", "tok")
