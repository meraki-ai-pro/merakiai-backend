"""Beat-aligned narration: the voice stays on the step being shown.

The client reported narration out of sync with the animation. One track sized
to the total length drifts by construction; these tests pin the replacement —
lines start on beats, every second of footage is used exactly once, and a line
that runs long holds ITS OWN last frame rather than spilling onto the next.
"""

import shutil
import subprocess

import pytest

from app.media.narration import aligned_filter, plan_segments
from app.media.render.remotion_spec import (
    CHART_SECONDS, STEP_SECONDS, TAIL_SECONDS, TITLE_SECONDS, validate_spec,
)

BEATS = [
    {"start": 0.0, "end": 2.0, "label": "title"},
    {"start": 2.0, "end": 3.0, "label": "pause"},
    {"start": 3.0, "end": 7.0, "label": "equation"},
    {"start": 7.0, "end": 9.0, "label": "result"},
]


class TestRemotionBeats:
    def spec(self):
        return validate_spec({
            "archetype": "process_flow",
            "title": "Photosynthesis",
            "steps": [{"label": "Light hits the leaf"}, {"label": "Water splits"}],
        })

    def test_beats_cover_the_whole_video_contiguously(self):
        spec = self.spec()
        beats = spec.beats()
        assert beats[0]["start"] == 0
        for a, b in zip(beats, beats[1:]):
            assert a["end"] == b["start"]
        assert beats[-1]["end"] == pytest.approx(spec.duration_seconds)

    def test_beats_use_the_same_section_lengths_as_the_composition(self):
        beats = self.spec().beats()
        lengths = [round(b["end"] - b["start"], 3) for b in beats]
        assert lengths == [TITLE_SECONDS, STEP_SECONDS, STEP_SECONDS, TAIL_SECONDS]

    def test_labels_say_what_is_on_screen(self):
        labels = " ".join(b["label"] for b in self.spec().beats())
        assert "Light hits the leaf" in labels and "Water splits" in labels

    def test_chart_gets_its_own_beat(self):
        spec = validate_spec({
            "archetype": "data_story", "title": "Yield",
            "chart": {"x_labels": ["a", "b"], "series": [{"name": "y", "values": [1, 2]}]},
        })
        assert any(round(b["end"] - b["start"], 3) == CHART_SECONDS for b in spec.beats())


class TestLessonTsxAgreesWithPython:
    def test_no_hard_coded_section_lengths(self):
        """Lesson.tsx used literals (2.5s, 3s/step, 6s) that disagreed with the
        composition's length, cutting steps off and ending on empty frames."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2] / "remotion/src/Lesson.tsx").read_text(encoding="utf-8")
        assert "* 3 * FPS" not in source
        assert "Math.round(6 * FPS)" not in source
        assert "Math.round(2.5 * FPS)" not in source


class TestPlanSegments:
    def test_lines_start_on_their_beats_and_cover_everything(self):
        segs = plan_segments(BEATS, [{"from": 0, "text": "A"}, {"from": 2, "text": "B"}])
        assert segs == [
            {"start": 0.0, "end": 3.0, "text": "A"},
            {"start": 3.0, "end": 9.0, "text": "B"},
        ]

    def test_first_line_is_pinned_to_the_start(self):
        segs = plan_segments(BEATS, [{"from": 1, "text": "A"}, {"from": 3, "text": "B"}])
        assert segs[0]["start"] == 0.0

    def test_bad_indices_and_blank_text_are_dropped(self):
        segs = plan_segments(BEATS, [
            {"from": 0, "text": "A"}, {"from": 99, "text": "x"}, {"from": "two", "text": "y"},
            {"from": 2, "text": "   "}, {"from": 0, "text": "duplicate"},
        ])
        assert [s["text"] for s in segs] == ["A"]
        assert segs[0]["end"] == 9.0

    def test_last_segment_runs_to_the_probed_end(self):
        segs = plan_segments(BEATS, [{"from": 0, "text": "A"}], total_seconds=9.4)
        assert segs[-1]["end"] == 9.4

    def test_nothing_usable_means_no_plan(self):
        assert plan_segments(BEATS, []) == []
        assert plan_segments([], [{"from": 0, "text": "A"}]) == []


class TestAlignedFilter:
    SEGS = [{"start": 0.0, "end": 3.0, "text": "A"}, {"start": 3.0, "end": 6.0, "text": "B"}]

    def test_a_short_line_holds_nothing(self):
        graph, total = aligned_filter(self.SEGS, [1.2, 2.0], fps=30)
        assert "tpad" not in graph
        assert total == pytest.approx(6.0)

    def test_a_long_line_holds_only_its_own_stretch(self):
        graph, total = aligned_filter(self.SEGS, [1.2, 4.5], fps=30)
        first, second = graph.split("[v0]")[0], graph.split("[v1]")[0].split(";")[-1]
        assert "tpad" not in first
        assert "stop_duration=1.800" in second  # 4.5 + 0.3 gap - 3.0 span
        assert total == pytest.approx(7.8)

    def test_frame_rate_is_set_before_tpad(self):
        """tpad after trim is a silent no-op without it (ffmpeg 7.1)."""
        graph, _ = aligned_filter(self.SEGS, [1.2, 4.5], fps=30)
        assert graph.index("fps=30") < graph.index("tpad")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_mux_starts_each_line_on_its_beat(tmp_path):
    """The artefact, not the command: line two's audio must begin at 3.0s."""
    from app.media.narration import mux_aligned

    def run(*args):
        subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)

    video = tmp_path / "v.mp4"
    run("-f", "lavfi", "-i", "testsrc=d=6:s=160x120:r=15", "-c:v", "libx264", str(video))
    clips = []
    for i, seconds in enumerate([1.2, 4.5]):
        clip = tmp_path / f"a{i}.mp3"
        run("-f", "lavfi", "-i", f"sine=f={440 + 220 * i}:d={seconds}", "-ar", "44100", str(clip))
        clips.append(clip)

    out = tmp_path / "out.mp4"
    assert mux_aligned(video, TestAlignedFilter.SEGS, clips, out)

    detect = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(out), "-af", "silencedetect=n=-40dB:d=0.3", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    ends = [float(line.split("silence_end: ")[1].split()[0]) for line in detect.splitlines() if "silence_end" in line]
    assert ends and ends[0] == pytest.approx(3.0, abs=0.05)

    from app.media.render.media_probe import probe_duration
    assert probe_duration(out) == pytest.approx(7.8, abs=0.1)
