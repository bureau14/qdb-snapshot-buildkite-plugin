from __future__ import annotations

import pytest

from snapdep.declaration import (
    DEFAULT_GENERATE,
    DeclarationError,
    Dependency,
    generate_command,
    parse_declaration,
    pipeline_entry,
    read_declaration,
)

VALID = """
schema: 1
lines: [master, 3.14.x]
pins: {quasardb-build: sc-1234/topic}
pipelines:
  qdb-api-rest:
    depends:
      - project: quasardb-build
        repo: bureau14/quasardb
      - project: qdb-duck
        repo: bureau14/qdb-duck
    gates: [build-linux, "test:all"]
    produces: [linux-haswell-release]
    generate: python3 .buildkite/pipeline.py
  qdb-api-rest-nightly:
"""


def parse(text: str):
    return parse_declaration(text, "x/.buildkite/snapshot.yml")


def error_of(text: str) -> str:
    with pytest.raises(DeclarationError) as e:
        parse(text)
    return str(e.value)


def test_valid_declaration():
    d = parse(VALID)
    assert d.lines == ("master", "3.14.x")
    assert d.pins == {"quasardb-build": "sc-1234/topic"}
    rest = pipeline_entry(d, "qdb-api-rest")
    assert rest.depends == (
        Dependency("quasardb-build", "bureau14/quasardb"),
        Dependency("qdb-duck", "bureau14/qdb-duck"),
    )
    assert rest.gates == ("build-linux", "test:all")
    assert rest.produces == ("linux-haswell-release",)
    assert generate_command(rest) == "python3 .buildkite/pipeline.py"


def test_empty_entry_has_defaults():
    nightly = pipeline_entry(parse(VALID), "qdb-api-rest-nightly")
    assert nightly.depends == () and nightly.gates == () and nightly.produces == ()
    assert generate_command(nightly) == DEFAULT_GENERATE


def test_missing_entry_and_missing_file_are_opaque():
    assert pipeline_entry(parse(VALID), "other") is None
    assert pipeline_entry(None, "qdb-api-rest") is None


def test_read_declaration_absent_file(tmp_path):
    assert read_declaration(str(tmp_path / "snapshot.yml")) is None


def test_read_declaration_from_file(tmp_path):
    f = tmp_path / "snapshot.yml"
    f.write_text(VALID)
    assert "qdb-api-rest" in read_declaration(str(f)).pipelines


@pytest.mark.parametrize(
    "text, expected",
    [
        ("schema: [", "x/.buildkite/snapshot.yml: not valid YAML"),
        ("- a", "x/.buildkite/snapshot.yml: (top level): expected a mapping"),
        ("schema: 2\nlines: [master]", "x/.buildkite/snapshot.yml: schema: expected 1, got 2"),
        ("schema: 1", "lines: missing"),
        ("schema: 1\nlines: []", "lines: expected at least one release line"),
        ("schema: 1\nlines: [master, master]", "lines: duplicate entries master"),
        ("schema: 1\nlines: [3.14]", "lines[0]: expected a git branch name, got 3.14"),
        ("schema: 1\nlines: [master]\nextra: 1", "(top level): unknown field(s) extra"),
        ("schema: 1\nlines: [master]\npins: {a: 1}", "pins.a: expected a git branch name"),
        (
            "schema: 1\nlines: [master]\npipelines:\n  p:\n    depnds: []",
            "pipelines.p: unknown field(s) depnds",
        ),
        (
            "schema: 1\nlines: [master]\npipelines:\n  p:\n    depends: [{project: q}]",
            "pipelines.p.depends[0].repo: missing",
        ),
        (
            "schema: 1\nlines: [master]\npipelines:\n  p:\n    depends: [{project: q, repo: q}]",
            "pipelines.p.depends[0].repo: expected \"owner/name\", got 'q'",
        ),
        (
            "schema: 1\nlines: [master]\npipelines:\n  p:\n    depends: [{project: p, repo: o/p}]",
            "pipelines.p.depends[0].project: a pipeline does not declare its own project",
        ),
        (
            "schema: 1\nlines: [master]\npipelines:\n  p:\n    depends:\n"
            "      - {project: q, repo: o/q}\n      - {project: q, repo: o/q}",
            "pipelines.p.depends[1].project: q is declared twice",
        ),
        (
            "schema: 1\nlines: [master]\npipelines:\n  p:\n    gates: build",
            "pipelines.p.gates: expected a list, got str",
        ),
        (
            "schema: 1\nlines: [master]\npipelines:\n  p:\n    generate: [a]",
            "pipelines.p.generate: expected a string",
        ),
    ],
)
def test_validation_errors_name_file_and_field(text, expected):
    msg = error_of(text)
    assert msg.startswith("x/.buildkite/snapshot.yml: ")
    assert expected in msg
