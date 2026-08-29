from pathlib import Path


def test_latex_diagnostic_extracts_the_compiler_error(tmp_path):
    from app.media.render.manim_renderer import _latex_diagnostic

    tex_dir = tmp_path / "media" / "Tex"
    tex_dir.mkdir(parents=True)
    (tex_dir / "scene.log").write_text(
        "ordinary preamble output\n"
        "! Undefined control sequence.\n"
        "l.17 \\badcommand{x}\n"
        "The control sequence was never defined.\n"
        "ordinary trailing output\n",
        encoding="utf-8",
    )

    diagnostic = _latex_diagnostic(tmp_path)

    assert diagnostic.startswith("LaTeX compiler diagnostic:")
    assert "Undefined control sequence" in diagnostic
    assert r"\badcommand{x}" in diagnostic
    assert "ordinary preamble output" in diagnostic


def test_failed_manim_puts_latex_error_first_for_the_repair_prompt(
    tmp_path, monkeypatch
):
    from app.media.render import manim_renderer

    tex_dir = tmp_path / "media" / "Tex"
    tex_dir.mkdir(parents=True)
    (tex_dir / "scene.log").write_text(
        "! Missing $ inserted.\nl.9 x_1 + x_2\n",
        encoding="utf-8",
    )

    class FailedProcess:
        returncode = 1
        stdout = "many generic lines\n" * 200
        stderr = "ValueError: latex error converting to dvi"

    monkeypatch.setattr(
        manim_renderer.subprocess,
        "run",
        lambda *_args, **_kwargs: FailedProcess(),
    )

    code, output = manim_renderer._run_manim(
        Path("scene.py"), "GeneratedScene", tmp_path
    )

    assert code == 1
    assert output.startswith("LaTeX compiler diagnostic:")
    assert "Missing $ inserted" in output[:500]
    assert "Renderer output (tail):" in output


def test_latex_diagnostic_is_empty_when_manim_wrote_no_log(tmp_path):
    from app.media.render.manim_renderer import _latex_diagnostic

    assert _latex_diagnostic(tmp_path) == ""
