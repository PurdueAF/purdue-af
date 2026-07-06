"""Tests for docker/kaniko-build-jobs/analyze_image_build.py.

The script runs after every purdue-af kaniko build; these tests pin down
the log-parsing, manifest-joining, and tar-streaming logic against
synthetic inputs so a kaniko/logrus/registry format change is caught in CI
rather than as a silently empty report.
"""

import base64
import gzip
import io
import json
import tarfile


def _json_log(entries):
    """[(iso_time, msg)] → kaniko --log-format=json lines."""
    return [json.dumps({"level": "info", "msg": msg, "time": t}) for t, msg in entries]


# Mirrors what kaniko actually logs (verified against a real build-af log):
# stages announced by "Resolved base name"/"Executing N build triggers" (FROM
# is never echoed), cache layers pushed mid-build with the same "Pushing
# image to" wording as the final push, and cross-stage tar store/unpack
# lines between stages.
SYNTHETIC_LOG = _json_log(
    [
        ("2026-07-02T10:00:00Z", "Retrieving image manifest alma8-base:20250501-1"),
        ("2026-07-02T10:00:03Z", "Resolved base name alma8-base:20250501-1 to system"),
        ("2026-07-02T10:00:03Z", "Resolved base name system to stack"),
        ("2026-07-02T10:00:05Z", "Executing 0 build triggers"),
        ("2026-07-02T10:00:10Z", "RUN set -euo pipefail  && dnf -y upgrade"),
        ("2026-07-02T10:02:10Z", "Taking snapshot of full filesystem..."),
        (
            "2026-07-02T10:03:00Z",
            "Pushing image to geddes-registry.rcac.purdue.edu/cms/purdue-af/cache:"
            + "a" * 64,
        ),
        (
            "2026-07-02T10:03:10Z",
            "Storing source image from stage 0 at path /kaniko/stages/0",
        ),
        ("2026-07-02T10:03:30Z", "Deleting filesystem..."),
        ("2026-07-02T10:03:40Z", "Executing 0 build triggers"),
        ("2026-07-02T10:04:00Z", "RUN pixi install --locked --environment base-env"),
        (
            "2026-07-02T10:04:01Z",
            "Using caching version of cmd: RUN pixi install --locked --environment base-env",
        ),
        (
            "2026-07-02T10:04:30Z",
            "Pushing image to geddes-registry.rcac.purdue.edu/cms/purdue-af:0.12.5",
        ),
        (
            "2026-07-02T10:05:00Z",
            "Pushed geddes-registry.rcac.purdue.edu/cms/purdue-af@sha256:abc",
        ),
    ]
)


class TestParseLine:
    def test_json_format(self, analyze):
        t, msg = analyze.parse_line(
            '{"level":"info","msg":"RUN dnf -y upgrade","time":"2026-07-02T10:00:10Z"}'
        )
        assert msg == "RUN dnf -y upgrade"
        assert t is not None

    def test_text_kv_format(self, analyze):
        t, msg = analyze.parse_line(
            'time="2026-07-02T10:00:10Z" level=info msg="RUN dnf -y upgrade"'
        )
        assert msg == "RUN dnf -y upgrade"
        assert t is not None

    def test_text_kv_escaped_quotes(self, analyze):
        _, msg = analyze.parse_line(
            'time="2026-07-02T10:00:10Z" level=info msg="RUN echo \\"hi\\""'
        )
        assert msg == 'RUN echo "hi"'

    def test_bracket_format_relative_seconds(self, analyze):
        t, msg = analyze.parse_line("INFO[0042] Taking snapshot of full filesystem...")
        assert t == 42.0
        assert msg == "Taking snapshot of full filesystem..."

    def test_ansi_colored_bracket_format(self, analyze):
        # what `kubectl logs` of a kaniko pod actually emits (default --log-format=color)
        t, msg = analyze.parse_line(
            "\x1b[36mINFO\x1b[0m[0042] RUN dnf -y upgrade                    "
        )
        assert t == 42.0
        assert msg == "RUN dnf -y upgrade"

    def test_garbage_is_ignored(self, analyze):
        assert analyze.parse_line("not a log line") == (None, None)
        assert analyze.parse_line("") == (None, None)


class TestParseKanikoLog:
    def test_step_durations_and_stages(self, analyze):
        timing = analyze.parse_kaniko_log(SYNTHETIC_LOG)
        steps = timing["steps"]
        assert [s["stage"] for s in steps] == ["system", "stack"]
        # RUN dnf: 10:00:10 → boundary (Storing source image) at 10:03:10
        dnf = steps[0]
        assert dnf["duration_s"] == 180.0
        # snapshot from 10:02:10 to step end at 10:03:10
        assert dnf["snapshot_s"] == 60.0
        assert not dnf["cached"]

    def test_cache_hit_flag(self, analyze):
        timing = analyze.parse_kaniko_log(SYNTHETIC_LOG)
        pixi = timing["steps"][-1]
        assert pixi["cached"]
        # 10:04:00 → final push at 10:04:30
        assert pixi["duration_s"] == 30.0

    def test_cache_layer_push_is_not_the_final_push(self, analyze):
        timing = analyze.parse_kaniko_log(SYNTHETIC_LOG)
        # push phase starts at the 10:04:30 destination push, NOT the
        # 10:03:00 mid-build cache-layer push
        assert timing["push_s"] == 30.0

    def test_cross_stage_overhead(self, analyze):
        timing = analyze.parse_kaniko_log(SYNTHETIC_LOG)
        # Storing source image at 10:03:10 → next instruction at 10:04:00
        assert timing["overhead_s"] == 50.0

    def test_setup_total(self, analyze):
        timing = analyze.parse_kaniko_log(SYNTHETIC_LOG)
        assert timing["setup_s"] == 10.0  # first line → first instruction
        assert timing["total_s"] == 300.0

    def test_from_echo_still_supported(self, analyze):
        # other builders (or future kaniko) may echo FROM lines
        timing = analyze.parse_kaniko_log(
            _json_log(
                [
                    ("2026-07-02T10:00:00Z", "FROM alpine:3.20 AS base"),
                    ("2026-07-02T10:00:05Z", "RUN apk add curl"),
                    ("2026-07-02T10:00:30Z", "Pushed image"),
                ]
            )
        )
        assert [s["stage"] for s in timing["steps"]] == ["base", "base"]

    def test_empty_log(self, analyze):
        timing = analyze.parse_kaniko_log([])
        assert timing["steps"] == []
        assert timing["total_s"] == 0.0


class TestParseImageRef:
    def test_registry_repo_tag(self, analyze):
        assert analyze.parse_image_ref(
            "geddes-registry.rcac.purdue.edu/cms/purdue-af:0.12.5"
        ) == ("geddes-registry.rcac.purdue.edu", "cms/purdue-af", "0.12.5")

    def test_registry_with_port_and_default_tag(self, analyze):
        assert analyze.parse_image_ref("localhost:5000/foo/bar") == (
            "localhost:5000",
            "foo/bar",
            "latest",
        )

    def test_rejects_ref_without_registry_host(self, analyze):
        import pytest

        with pytest.raises(ValueError):
            analyze.parse_image_ref("purdue-af:0.12.5")


class TestLoadAuthHeader:
    def test_prebaked_auth_field(self, analyze, tmp_path):
        token = base64.b64encode(b"robot:hunter2").decode()
        (tmp_path / "config.json").write_text(
            json.dumps({"auths": {"geddes-registry.rcac.purdue.edu": {"auth": token}}})
        )
        header = analyze.load_auth_header(tmp_path, "geddes-registry.rcac.purdue.edu")
        assert header == "Basic " + token

    def test_username_password_fields(self, analyze, tmp_path):
        (tmp_path / "config.json").write_text(
            json.dumps(
                {"auths": {"reg.example.org": {"username": "u", "password": "p"}}}
            )
        )
        header = analyze.load_auth_header(tmp_path, "reg.example.org")
        assert header == "Basic " + base64.b64encode(b"u:p").decode()

    def test_url_style_registry_key(self, analyze, tmp_path):
        token = base64.b64encode(b"a:b").decode()
        (tmp_path / "config.json").write_text(
            json.dumps({"auths": {"https://reg.example.org": {"auth": token}}})
        )
        assert analyze.load_auth_header(tmp_path, "reg.example.org") is not None

    def test_no_match(self, analyze, tmp_path):
        (tmp_path / "config.json").write_text(json.dumps({"auths": {}}))
        assert analyze.load_auth_header(tmp_path, "reg.example.org") is None


class TestJoinLayersHistory:
    def test_empty_layers_are_skipped(self, analyze):
        history = [
            {"created_by": "RUN dnf install", "empty_layer": False},
            {"created_by": "ENV FOO=bar", "empty_layer": True},
            {"created_by": "COPY /opt/pixi /opt/pixi"},
        ]
        layers = [
            {"digest": "sha256:aaa", "size": 100},
            {"digest": "sha256:bbb", "size": 5000},
        ]
        rows = analyze.join_layers_history(history, layers)
        assert [r["created_by"] for r in rows] == [
            "RUN dnf install",
            "COPY /opt/pixi /opt/pixi",
        ]
        assert [r["size"] for r in rows] == [100, 5000]

    def test_more_history_than_layers_is_tolerated(self, analyze):
        rows = analyze.join_layers_history(
            [{"created_by": "RUN a"}, {"created_by": "RUN b"}],
            [{"digest": "sha256:aaa", "size": 1}],
        )
        assert len(rows) == 1


class TestAggregateTarDirs:
    @staticmethod
    def _tar_gz(entries):
        """{path: nbytes} → gzipped tar stream, like a registry layer blob."""
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            for path, size in entries.items():
                info = tarfile.TarInfo(path)
                info.size = size
                tar.addfile(info, io.BytesIO(b"\0" * size))
        return io.BytesIO(gzip.compress(raw.getvalue()))

    def test_sizes_grouped_by_depth(self, analyze):
        blob = self._tar_gz(
            {
                "opt/pixi/envs/base-env/lib/libbig.so": 4000,
                "opt/pixi/envs/base-env/bin/python": 1000,
                "usr/local/cuda/lib64/libcudnn.so": 3000,
                "etc/hosts": 10,
            }
        )
        dirs = dict(analyze.aggregate_tar_dirs(blob, depth=3))
        assert dirs["opt/pixi/envs"] == 5000
        assert dirs["usr/local/cuda"] == 3000
        assert dirs["etc/hosts"] == 10

    def test_leading_dot_slash_stripped(self, analyze):
        blob = self._tar_gz({"./var/cache/dnf/x": 7})
        assert dict(analyze.aggregate_tar_dirs(blob, depth=2)) == {"var/cache": 7}

    def test_top_limit(self, analyze):
        blob = self._tar_gz({f"d{i}/f": i + 1 for i in range(30)})
        assert len(analyze.aggregate_tar_dirs(blob, depth=1, top=5)) == 5


class TestCli:
    def test_log_only_run(self, analyze, tmp_path, capsys):
        log = tmp_path / "kaniko.log"
        log.write_text("\n".join(SYNTHETIC_LOG))
        out_json = tmp_path / "report.json"
        analyze.main(["--log", str(log), "--json", str(out_json)])
        printed = capsys.readouterr().out
        assert "BUILD TIME BY DOCKERFILE INSTRUCTION" in printed
        assert "IMAGE SIZE" not in printed  # no --image → no registry access
        report = json.loads(out_json.read_text())
        assert report["timing"]["total_s"] == 300.0

    def test_requires_log_or_image(self, analyze):
        import pytest

        with pytest.raises(SystemExit):
            analyze.main([])

    def test_default_docker_config_is_home(self, analyze):
        args = analyze.build_arg_parser().parse_args(["--log", "-"])
        assert args.docker_config.endswith(".docker")


class TestRendering:
    def test_reports_render_without_error(self, analyze):
        timing = analyze.parse_kaniko_log(SYNTHETIC_LOG)
        out = analyze.render_time_report(timing)
        assert "RUN set -euo pipefail" in out
        assert "stage stack" in out
        rows = [
            {
                "created_by": "COPY /opt/pixi /opt/pixi",
                "size": 9 << 30,
                "digest": "sha256:a",
            },
            {
                "created_by": "RUN dnf -y install cuda-toolkit-12-4",
                "size": 3 << 30,
                "digest": "sha256:b",
            },
        ]
        out = analyze.render_size_report(rows)
        assert "9.0 GB" in out and "75.0%" in out
