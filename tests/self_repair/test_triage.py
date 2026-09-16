"""The flyte-free half of workflows/self-repair: fingerprints, redaction, the
agent's verdict and the pull request text."""

import json
import urllib.parse
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


class TestRunNames:
    """Flyte caps run names at 30 characters; the pod is `<run>-a0-0`."""

    def setup_method(self):
        import types

        source = (REPO / "workflows/self-repair/self_repair.py").read_text()
        # only the naming helpers: the module imports flyte at the top
        start = source.index("NAME_LIMIT = 30")
        end = source.index("def _git(")
        self.names = types.ModuleType("names")
        self.names.datetime = datetime
        exec(source[start:end], vars(self.names))

    def test_every_run_name_fits(self):
        tick = self.names.tick_of(datetime(2084, 1, 1, tzinfo=timezone.utc))
        assert len(tick) == 5
        for task, fp in (
            ("triage", ""),
            ("watch", ""),
            ("analyze", "189fbee7047b"),
            ("fix", "189fbee7047b"),
        ):
            name = self.names.run_name(task, tick, fp)
            assert name.startswith(f"self-repair-{task}-{tick}"), name
            assert len(name) <= 30, name
        assert self.names.run_name("analyze", tick, "189fbee7047b").endswith("-189f")

    def test_tick_is_the_minute_and_padded(self):
        a = self.names.tick_of(datetime(2026, 9, 16, 16, 36, 5, tzinfo=timezone.utc))
        b = self.names.tick_of(datetime(2026, 9, 16, 16, 36, 59, tzinfo=timezone.utc))
        c = self.names.tick_of(datetime(2026, 9, 16, 16, 37, 0, tzinfo=timezone.utc))
        assert a == b != c and len(a) == 5 and a.islower()
        assert (
            self.names.tick_of(datetime(1970, 1, 1, 0, 1, tzinfo=timezone.utc))
            == "00001"
        )


class TestPrompts:
    def test_both_prompts_state_the_time_budget(self):
        prompts = load_script(
            REPO / "workflows/self-repair/prompts.py", "self_repair_prompts"
        )
        analyze = prompts.ANALYZE.substitute(incident="i", minutes=25)
        fix = prompts.FIX.substitute(
            incident="i", title="t", component="c", reason="r", plan="p", minutes=25
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
        async def spawn(incident):
            raise AssertionError("must not be called")

        assert self.run(triage.analyze_within_budget([], 5, spawn)) == []
        assert self.run(triage.analyze_within_budget(self.incidents(3), 0, spawn)) == []


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
