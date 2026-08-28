"""Narration on concept videos.

Two properties this file exists to hold:

  1. The render container never grows a TTS dependency. It executes
     model-generated Python, and the whole reason narration is a second job on
     a different queue is that render.Dockerfile ships no ElevenLabs client and
     is given no key for one. An innocent-looking import in the render path
     would undo that without any test failing anywhere else.

  2. A narration failure never costs the video. Every failure path leaves the
     silent render exactly as it was.
"""

import ast
from pathlib import Path

import pytest

from app.media.narration import _clean_script

BACKEND = Path(__file__).resolve().parents[2]

# Everything the render worker loads: the Celery entry point, the job
# lifecycle, the renderers and their support modules.
RENDER_MODULES = [
    "app/media/render/tasks.py",
    "app/media/render/service.py",
    "app/media/render/manim_renderer.py",
    "app/media/render/remotion_renderer.py",
    "app/media/render/registry.py",
    "app/media/render/routing.py",
    "app/media/render/sandbox.py",
]

# Modules that are absent from requirements-render.txt on purpose.
FORBIDDEN_IN_RENDER = {"elevenlabs", "openai", "pinecone", "boto3", "unstructured", "pandas"}


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestRenderContainerStaysClean:
    @pytest.mark.parametrize("relative", RENDER_MODULES)
    def test_no_forbidden_client_libraries(self, relative):
        path = BACKEND / relative
        if not path.exists():  # pragma: no cover — renderer set may change
            pytest.skip(f"{relative} not present")

        roots = {name.split(".")[0] for name in _imported_names(path)}
        leaked = roots & FORBIDDEN_IN_RENDER
        assert not leaked, (
            f"{relative} imports {sorted(leaked)}, which requirements-render.txt "
            "deliberately does not install. The render container executes "
            "model-generated code; a client library present there is a "
            "credential a payload can reach."
        )

    @pytest.mark.parametrize("relative", RENDER_MODULES)
    def test_narration_module_is_never_imported(self, relative):
        """app.media.narration pulls the TTS stack. The render worker
        dispatches it by task NAME instead, exactly as the API dispatches the
        render task by name so it never imports manim."""
        path = BACKEND / relative
        if not path.exists():  # pragma: no cover
            pytest.skip(f"{relative} not present")

        assert "app.media.narration" not in _imported_names(path)

    def test_the_render_worker_dispatches_narration_by_name(self):
        source = (BACKEND / "app/media/render/service.py").read_text(encoding="utf-8")
        assert "app.ai.tasks.process_narration_task" in source
        assert "video_tasks" in source

    def test_narration_is_routed_off_the_render_queue(self):
        from app.core.celery_app import celery_app

        routes = celery_app.conf.task_routes
        assert routes["app.ai.tasks.process_narration_task"] == {"queue": "video_tasks"}


class TestScriptCleaning:
    def test_strips_markdown_fences(self):
        assert _clean_script("```\nThe chain rule says\n```") == "The chain rule says"

    def test_drops_headings_and_stage_directions(self):
        """These reach the TTS voice verbatim if they survive, and it reads
        them out — "hash hash Step one" — over the animation."""
        raw = "# Step one\n[pause]\nWe differentiate the outer function.\n"
        assert _clean_script(raw) == "We differentiate the outer function."

    def test_joins_lines_into_continuous_speech(self):
        raw = "First we expand.\nThen we collect terms."
        assert _clean_script(raw) == "First we expand. Then we collect terms."

    def test_strips_bullet_markers(self):
        assert _clean_script("- One\n* Two") == "One Two"

    def test_empty_input_is_empty_output(self):
        assert _clean_script("") == ""
        assert _clean_script(None) == ""


class TestFailuresNeverCostTheVideo:
    @pytest.mark.asyncio
    async def test_a_missing_voice_leaves_the_asset_alone(self, monkeypatch):
        from app.media import narration

        updates = []

        class FakeTable:
            def __init__(self, name):
                self.name = name

            def select(self, *_a, **_k):
                return self

            def update(self, fields):
                updates.append(fields)
                return self

            def eq(self, *_a, **_k):
                return self

            def execute(self):
                return type("R", (), {"data": [{
                    "id": "asset-1", "course_id": "maths-101", "status": "ready",
                    "storage_path": "maths-101/asset-1.mp4", "has_audio": False,
                    "concept_key": "chain-rule", "source_script": "show it",
                }]})()

        monkeypatch.setattr(
            narration, "get_supabase", lambda: type("S", (), {"table": staticmethod(FakeTable)})()
        )
        # Takes the asset course_id now — the voice follows the COURSE.
        monkeypatch.setattr(narration, "_resolve_voice_id", lambda _course_id=None: None)

        out = await narration.narrate_asset("asset-1")

        assert out["status"] == "failed"
        # The only write is the narration state. Nothing touched storage_path,
        # status or has_audio, so the silent video is still exactly as rendered.
        assert updates == [{"narration_status": "failed"}]

    @pytest.mark.asyncio
    async def test_disabled_narration_marks_skipped_without_work(self, monkeypatch):
        from app.media import narration

        updates = []

        class FakeTable:
            def __init__(self, name):
                self.name = name

            def select(self, *_a, **_k):
                return self

            def update(self, fields):
                updates.append(fields)
                return self

            def eq(self, *_a, **_k):
                return self

            def execute(self):
                return type("R", (), {"data": [{"id": "a", "status": "ready"}]})()

        monkeypatch.setattr(
            narration, "get_supabase", lambda: type("S", (), {"table": staticmethod(FakeTable)})()
        )
        monkeypatch.setattr(narration, "NARRATION_ENABLED", False)

        out = await narration.narrate_asset("a")

        assert out["status"] == "skipped"
        assert updates == [{"narration_status": "skipped"}]


class TestMuxing:
    def test_short_narration_copies_the_video_stream(self, monkeypatch, tmp_path):
        """No filter means no re-encode, so the render stays bit-for-bit what
        manim produced."""
        from app.media import narration

        captured = {}

        def fake_run(cmd, **_kwargs):
            captured["cmd"] = cmd
            (tmp_path / "out.mp4").write_bytes(b"x")
            return type("P", (), {"returncode": 0, "stderr": ""})()

        monkeypatch.setattr(narration.subprocess, "run", fake_run)
        monkeypatch.setattr(narration, "probe_duration", lambda p: 60.0)

        assert narration.mux(tmp_path / "v.mp4", tmp_path / "a.mp3", tmp_path / "out.mp4")
        assert "copy" in captured["cmd"]
        assert not any(str(a).startswith("tpad") for a in captured["cmd"])

    def test_overrunning_narration_holds_the_last_frame(self, monkeypatch, tmp_path):
        """Cutting the voice off mid-sentence is much worse than a few seconds
        of a still frame, which is why -shortest is not used."""
        from app.media import narration

        captured = {}
        durations = {"v.mp4": 40.0, "a.mp3": 55.0}

        def fake_run(cmd, **_kwargs):
            captured["cmd"] = cmd
            (tmp_path / "out.mp4").write_bytes(b"x")
            return type("P", (), {"returncode": 0, "stderr": ""})()

        monkeypatch.setattr(narration.subprocess, "run", fake_run)
        monkeypatch.setattr(
            narration, "probe_duration", lambda p: durations.get(Path(p).name, 0.0)
        )

        assert narration.mux(tmp_path / "v.mp4", tmp_path / "a.mp3", tmp_path / "out.mp4")
        assert any("tpad=stop_mode=clone" in str(a) for a in captured["cmd"])
        assert "-shortest" not in captured["cmd"]

    def test_ffmpeg_failure_is_reported_not_raised(self, monkeypatch, tmp_path):
        from app.media import narration

        monkeypatch.setattr(
            narration.subprocess, "run",
            lambda *a, **k: type("P", (), {"returncode": 1, "stderr": "boom"})(),
        )
        monkeypatch.setattr(narration, "probe_duration", lambda p: 10.0)

        assert narration.mux(tmp_path / "v.mp4", tmp_path / "a.mp3", tmp_path / "o.mp4") is False
