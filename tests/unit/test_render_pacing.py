"""Concept videos have to be slow enough to follow.

The client's complaint was that generated videos play too fast, and the
evidence backed it: the shipped chain-rule render had 26 `self.play(...)` calls
with NO run_time at all — manim's one-second default — separated by waits as
short as half a second. A visual change every 2.3 seconds, with half a second
to read an equation.

Two fixes, and the split matters:

  * the PROMPT asks for humane pacing, which produces naturally slower
    animations but depends on the model complying; and
  * the PLAYBACK STRETCH guarantees a floor regardless, because a model
    reliably drifts back towards one-second animations.

Testing only the first would leave the guarantee untested; testing only the
second would let the prompt rot.
"""

import inspect
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]


class TestManimPromptAsksForPacing:
    def test_it_demands_an_explicit_run_time(self):
        from app.media.render.manim_renderer import _SCENE_SYSTEM

        assert "run_time" in _SCENE_SYSTEM

    def test_it_asks_for_pauses_long_enough_to_read(self):
        """self.wait(1) was the old instruction and is what produced the
        complaint."""
        from app.media.render.manim_renderer import _SCENE_SYSTEM

        assert "self.wait(2)" in _SCENE_SYSTEM
        assert "self.wait(1) between steps" not in _SCENE_SYSTEM

    def test_it_names_the_failure_it_is_preventing(self):
        from app.media.render.manim_renderer import _SCENE_SYSTEM

        assert "too fast" in _SCENE_SYSTEM.lower()


class TestPlaybackStretch:
    def test_the_default_is_slower_than_real_time(self):
        from app.media.render.manim_renderer import MANIM_SPEED

        assert 0 < MANIM_SPEED < 1, "the default must actually slow renders down"

    def test_a_speed_of_one_is_a_no_op(self, tmp_path):
        """So a deployment can turn it off without the code inventing work."""
        from app.media.render.manim_renderer import _slow_down

        video = tmp_path / "in.mp4"
        video.write_bytes(b"x")
        assert _slow_down(video, 1.0) is video

    def test_ffmpeg_failure_ships_the_original(self, tmp_path, monkeypatch):
        """A video that plays slightly fast beats no video at all."""
        from app.media.render import manim_renderer

        monkeypatch.setattr(
            manim_renderer.subprocess, "run",
            lambda *a, **k: type("P", (), {"returncode": 1, "stderr": "boom"})(),
        )
        video = tmp_path / "in.mp4"
        video.write_bytes(b"x")
        assert manim_renderer._slow_down(video, 0.8) is video

    def test_ffmpeg_missing_ships_the_original(self, tmp_path, monkeypatch):
        from app.media.render import manim_renderer

        def explode(*_a, **_k):
            raise OSError("ffmpeg not found")

        monkeypatch.setattr(manim_renderer.subprocess, "run", explode)
        video = tmp_path / "in.mp4"
        video.write_bytes(b"x")
        assert manim_renderer._slow_down(video, 0.8) is video

    def test_the_stretch_uses_setpts_and_drops_audio(self, tmp_path, monkeypatch):
        """setpts re-times existing frames — nothing dropped or invented. The
        render carries no audio; narration is muxed on later, sized from the
        probed duration of whatever comes out of here."""
        from app.media.render import manim_renderer

        captured = {}

        def fake_run(cmd, **_kwargs):
            captured["cmd"] = cmd
            Path(cmd[-1]).write_bytes(b"stretched")
            return type("P", (), {"returncode": 0, "stderr": ""})()

        monkeypatch.setattr(manim_renderer.subprocess, "run", fake_run)
        video = tmp_path / "in.mp4"
        video.write_bytes(b"x")

        out = manim_renderer._slow_down(video, 0.8)

        assert out != video and out.exists()
        assert any("setpts" in str(a) for a in captured["cmd"])
        assert "-an" in captured["cmd"]

    def test_narration_is_sized_from_the_stretched_file(self):
        """Otherwise a slowed video would get narration written for the
        original length and finish talking well before the animation does."""
        from app.media.render import manim_renderer

        source = inspect.getsource(manim_renderer.ManimRenderer._render_source)
        assert source.index("_slow_down") < source.index("probe_duration")


class TestRemotionPacing:
    def test_a_step_gets_long_enough_to_read_its_detail(self):
        from app.media.render.remotion_spec import STEP_SECONDS

        assert STEP_SECONDS >= 4.5, "three seconds was the 'too fast' setting"

    def test_a_slide_cannot_be_shorter_than_readable(self):
        from app.media.render.remotion_spec import RemotionSpec

        field = RemotionSpec.model_fields["slides"]
        # The bound lives on Slide, not the list; assert through validation.
        from pydantic import ValidationError

        from app.media.render.remotion_spec import validate_spec

        with pytest.raises(ValidationError):
            validate_spec({
                "archetype": "composited_explainer", "title": "T",
                "slides": [{"title": "a", "seconds": 1.5}],
            })
        assert field is not None

    def test_python_and_react_agree_on_the_beat(self):
        """They must: Python computes the frame range handed to the CLI, and
        the composition computes its own duration. A mismatch is a hard render
        failure."""
        from app.media.render.remotion_spec import CHART_SECONDS, STEP_SECONDS

        types_ts = (BACKEND / "remotion" / "src" / "types.ts").read_text(encoding="utf-8")
        assert f"export const STEP_SECONDS = {STEP_SECONDS};" in types_ts
        assert f"export const CHART_SECONDS = {int(CHART_SECONDS)};" in types_ts

    def test_the_composition_uses_the_shared_constant(self):
        """A hard-coded 3 in Lesson.tsx would reveal itself as steps appearing
        out of time with the frames budgeted for them."""
        lesson = (BACKEND / "remotion" / "src" / "Lesson.tsx").read_text(encoding="utf-8")
        assert "STEP_SECONDS * FPS" in lesson
        assert "const perStep = 3 * FPS" not in lesson
