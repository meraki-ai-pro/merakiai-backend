"""The render task is dispatched by NAME, which nothing type-checks.

The API cannot import app.media.render.tasks — that module imports manim, which
is installed only in the render container. So it calls send_task with a string.
A rename on either side would break rendering silently, at runtime, in
production, with the job sitting in a queue nobody consumes.

These tests are the compile-time check that arrangement otherwise lacks.
"""

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
TASK_NAME = "app.media.render.tasks.process_render_task"


def _source(relative: str) -> str:
    return (BACKEND / relative).read_text(encoding="utf-8")


def _requirement_names(relative: str) -> set[str]:
    """Package names actually installed, ignoring comments and versions."""
    names = set()
    for line in _source(relative).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        names.add(line.split("==")[0].split("[")[0].split(">")[0].strip().lower())
    return names


class TestNameAgreement:
    def test_the_task_declares_the_expected_name(self):
        """@shared_task(name=...) is explicit so the name cannot drift with
        the module path."""
        assert f'name="{TASK_NAME}"' in _source("app/media/render/tasks.py")

    def test_the_api_dispatches_that_exact_name(self):
        assert TASK_NAME in _source("app/api/v1/render.py")

    def test_there_is_no_static_route_for_the_render_task(self):
        """The queue depends on the ASSET's renderer, not on the task name.

        A static route here would override the queue= passed at dispatch and
        send every render back to one shared queue — the bug the per-renderer
        queues exist to prevent.
        """
        from app.core.celery_app import celery_app

        assert TASK_NAME not in celery_app.conf.task_routes

    @pytest.mark.parametrize("queue_name", ["render_manim", "render_remotion"])
    def test_each_renderer_has_its_own_queue(self, queue_name):
        from app.core.celery_app import celery_app

        assert queue_name in {q.name for q in celery_app.conf.task_queues}

    @pytest.mark.parametrize(
        "queue_name", ["render_manim", "render_remotion", "render_tasks"]
    )
    def test_the_render_queues_dead_letter(self, queue_name):
        """A failed render must not vanish."""
        from app.core.celery_app import celery_app

        queue = next(q for q in celery_app.conf.task_queues if q.name == queue_name)
        assert queue.queue_arguments.get("x-dead-letter-exchange") == "dlx"

    def test_the_legacy_queue_is_still_declared(self):
        """Messages in flight during an upgrade must not be dropped, even
        though nothing dispatches there any more."""
        from app.core.celery_app import celery_app

        assert "render_tasks" in {q.name for q in celery_app.conf.task_queues}


class TestRenderersDoNotStealEachOthersJobs:
    """Each render image registers ONLY the renderer it carries.

    On a shared queue the Remotion worker happily takes a Manim job and fails
    with "No renderer registered under 'manim'" — which reads as a broken
    install rather than a misrouted message, and costs roughly half the renders
    of any deployment that serves both maths and biology.
    """

    def test_the_two_renderers_get_different_queues(self):
        from app.media.render.routing import render_queue

        assert render_queue("manim") != render_queue("remotion")

    @pytest.mark.parametrize(
        "renderer,expected",
        [("manim", "render_manim"), ("remotion", "render_remotion")],
    )
    def test_queue_follows_the_renderer(self, renderer, expected):
        from app.media.render.routing import render_queue

        assert render_queue(renderer) == expected

    def test_an_unknown_renderer_falls_back_rather_than_inventing_a_queue(self):
        """A queue nothing consumes is a render that hangs for ever."""
        from app.media.render.routing import DEFAULT_RENDERER, render_queue

        assert render_queue("wizard") == f"render_{DEFAULT_RENDERER}"
        assert render_queue("") == f"render_{DEFAULT_RENDERER}"

    def test_dispatch_derives_the_queue_from_the_asset(self):
        source = _source("app/api/v1/render.py")
        assert "render_queue(asset[\"renderer\"])" in source
        assert 'queue="render_tasks"' not in source


class TestIsolationFromTheApiProcess:
    def test_the_api_does_not_import_the_render_task_module(self):
        """Importing it would pull manim into a process that has none."""
        assert "from app.media.render.tasks import" not in _source("app/api/v1/render.py")
        assert "import app.media.render.tasks" not in _source("app/api/v1/render.py")

    def test_ai_tasks_no_longer_defines_a_render_task(self):
        import app.ai.tasks as ai_tasks

        assert not hasattr(ai_tasks, "process_render_task")

    def test_the_manim_renderer_is_never_imported_at_app_import_time(self):
        """A stale import here would break `from app.main import app` on any
        machine without manim — including the API container.

        Checked in a FRESH interpreter, not against this process's sys.modules.
        Any other test that imports the renderer directly (the pacing tests do)
        leaves it loaded, and the assertion then fails on test ordering rather
        than on the thing it exists to catch — which is worse than useless,
        because the obvious fix is to weaken it.
        """
        import subprocess
        import sys

        probe = (
            "import sys; import app.main; "
            "sys.exit(1 if 'app.media.render.manim_renderer' in sys.modules else 0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(BACKEND), capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0, (
            "importing app.main pulled in the manim renderer; the API container "
            f"has no manim and would fail to start.\n{result.stderr[-800:]}"
        )

    def test_celery_include_defaults_to_the_web_tasks(self):
        from app.core.celery_app import celery_app

        assert list(celery_app.conf.include) == ["app.ai.tasks"]


class TestRenderWorkerConfiguration:
    def test_the_dockerfile_overrides_celery_include(self):
        """Without this the render worker imports app.ai.tasks and dies on a
        missing elevenlabs/pinecone import."""
        assert "CELERY_INCLUDE=app.media.render.tasks" in _source("render.Dockerfile")

    def test_the_manim_worker_consumes_only_its_own_queue(self):
        assert "--queues=render_manim" in _source("render.Dockerfile")
        assert "render_remotion" not in _source("render.Dockerfile")

    def test_manim_image_installs_the_standalone_latex_class(self):
        """MathTex uses documentclass{standalone}; Debian ships that class in
        texlive-latex-extra."""
        assert "texlive-latex-extra" in _source("render.Dockerfile")

    def test_the_remotion_worker_consumes_only_its_own_queue(self):
        assert "--queues=render_remotion" in _source("remotion.Dockerfile")
        assert "--queues=render_manim" not in _source("remotion.Dockerfile")

    def test_remotion_uses_its_own_requirements(self):
        dockerfile = _source("remotion.Dockerfile")
        assert "requirements-remotion.txt" in dockerfile
        assert "requirements-render.txt" not in dockerfile

    def test_remotion_requirements_exclude_manim(self):
        installed = _requirement_names("requirements-remotion.txt")
        assert "manim" not in installed
        for needed in ("celery", "supabase", "anthropic"):
            assert needed in installed

    def test_the_worker_runs_one_render_at_a_time(self):
        """A render is CPU- and memory-bound; scale by containers, not threads."""
        assert "--concurrency=1" in _source("render.Dockerfile")

    def test_the_worker_runs_as_a_non_root_user(self):
        assert "USER renderer" in _source("render.Dockerfile")

    def test_lean_requirements_exclude_credentialed_clients(self):
        """Every library present is reachable by generated code.

        Comments are stripped first — the file explains which packages are
        deliberately absent, and naming them must not look like installing
        them.
        """
        installed = _requirement_names("requirements-render.txt")
        for excluded in ("pinecone", "openai", "elevenlabs", "boto3", "unstructured", "fastapi"):
            assert excluded not in installed

    @pytest.mark.parametrize("needed", ["celery", "supabase", "anthropic", "manim"])
    def test_lean_requirements_keep_what_a_render_needs(self, needed):
        assert needed in _requirement_names("requirements-render.txt")


class TestComposeHardening:
    @pytest.mark.parametrize(
        "flag",
        ["read_only: true", "no-new-privileges:true", "cap_drop", "pids_limit", "mem_limit"],
    )
    def test_container_hardening_is_present(self, flag):
        """Layer 2 of the sandbox. The AST allowlist rejects what it
        recognises; these contain what it does not."""
        assert flag in _source("docker-compose.render.yml")

    def test_the_writable_mount_is_noexec(self):
        """Stops a payload writing a binary and executing it."""
        compose = _source("docker-compose.render.yml")
        assert "noexec" in compose

    def test_retrieval_credentials_are_absent_from_the_render_environment(self):
        compose = _source("docker-compose.render.yml")
        for secret in ("PINECONE_API_KEY:", "OPENAI_API_KEY:", "SUPABASE_JWT_SECRET:"):
            assert secret not in compose

    def test_production_compose_runs_both_render_queues(self):
        compose = _source("deploy/aws/docker-compose.render.production.yml")
        assert "dockerfile: render.Dockerfile" in compose
        assert "dockerfile: remotion.Dockerfile" in compose
        assert "RENDERER: manim" in compose
        assert "RENDERER: remotion" in compose

    def test_production_renderers_are_hardened_and_bounded(self):
        compose = _source("deploy/aws/docker-compose.render.production.yml")
        assert compose.count("read_only: true") == 2
        assert compose.count("no-new-privileges:true") == 2
        assert compose.count("cap_drop:") == 2
        assert compose.count("mem_limit:") == 2
        assert compose.count("cpus:") == 2
        assert compose.count("type: tmpfs") == 2
        assert compose.count("mode: 0o1777") == 2

    def test_production_render_environment_excludes_retrieval_secrets(self):
        compose = _source("deploy/aws/docker-compose.render.production.yml")
        for secret in ("PINECONE_API_KEY", "OPENAI_API_KEY", "SUPABASE_JWT_SECRET"):
            assert secret not in compose


class TestSubprocessEnvironmentIsScrubbed:
    # Names the render subprocess may see. Everything here is a path or a
    # platform fact; none is a credential. The Windows entries exist because
    # CPython cannot start without SYSTEMROOT, so omitting them does not
    # harden anything — it just stops manim running at all.
    ALLOWED = {
        "PATH", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE",
        "TEMP", "TMP", "USERPROFILE",
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATHEXT", "COMSPEC",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    }

    def test_only_safe_variables_are_passed_to_manim(self, tmp_path, monkeypatch):
        """Generated code runs in this subprocess. os.environ of the parent
        holds ANTHROPIC_API_KEY and the Supabase service-role key.

        Asserts on the environment actually built, not on the source text, so
        the guarantee survives a refactor of how it is assembled.
        """
        from app.media.render.manim_renderer import _render_env

        secrets = {
            "ANTHROPIC_API_KEY": "sk-ant-must-not-leak",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-must-not-leak",
            "PINECONE_API_KEY": "pc-must-not-leak",
            "OPENAI_API_KEY": "sk-must-not-leak",
            "DID_API_KEY": "did-must-not-leak",
            "ELEVENLABS_API_KEY": "el-must-not-leak",
        }
        for name, value in secrets.items():
            monkeypatch.setenv(name, value)

        env = _render_env(tmp_path)

        assert set(env) <= self.ALLOWED, f"unexpected vars: {set(env) - self.ALLOWED}"
        for name in secrets:
            assert name not in env
        # Also catch a secret smuggled in under an allowed name.
        leaked = [v for v in secrets.values() if v in set(env.values())]
        assert not leaked, f"secret value present in render env: {leaked}"

        # The sandbox must still point scratch space at the throwaway workdir.
        assert env["HOME"] == str(tmp_path)
        assert env["TMPDIR"] == str(tmp_path)

    def test_env_is_not_built_from_a_dict_literal_elsewhere(self):
        """The subprocess env must come from _render_env, not an inline dict.

        An inline `env={...}` at a second call site would bypass the checks
        above without failing any of them.
        """
        tree = ast.parse(_source("app/media/render/manim_renderer.py"))

        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "env":
                assert isinstance(node.value, ast.Call), (
                    "subprocess env should be built by _render_env()"
                )
