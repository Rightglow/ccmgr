#!/usr/bin/env python3
"""Install one built Railmux wheel into an isolated prefix and smoke its CLIs."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"{' '.join(argv)} failed ({result.returncode}):\n"
            f"{result.stdout}{result.stderr}"
        )
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        parser.error("wheel must name one built .whl file")

    with tempfile.TemporaryDirectory(prefix="railmux-wheel-smoke-") as raw:
        root = Path(raw)
        prefix = root / "install"
        bindir = prefix / ("Scripts" if os.name == "nt" else "bin")
        railmux = bindir / ("railmux.exe" if os.name == "nt" else "railmux")
        env = dict(os.environ)
        env["HOME"] = str(root / "home")
        env["XDG_CONFIG_HOME"] = str(root / "config")
        env["XDG_RUNTIME_DIR"] = str(root / "runtime")
        Path(env["HOME"]).mkdir()
        Path(env["XDG_RUNTIME_DIR"]).mkdir(mode=0o700)

        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--prefix",
                str(prefix),
                "--no-index",
                "--no-deps",
                "--force-reinstall",
                str(wheel),
            ],
            cwd=root,
            env=env,
        )
        site_packages = next(prefix.glob("lib/python*/site-packages"), None)
        if site_packages is None:
            raise RuntimeError("isolated wheel install created no site-packages")
        # Dependencies come from the already-validated dev/CI environment; the
        # isolated prefix is first so Railmux itself cannot resolve to the
        # editable source checkout.
        env["PYTHONPATH"] = str(site_packages)
        imported = json.loads(_run(
            [
                sys.executable,
                "-c",
                (
                    "import json, pathlib, railmux, "
                    "railmux.fast_display_client, pyte; "
                    "print(json.dumps({'version': railmux.__version__, "
                    "'path': str(pathlib.Path(railmux.__file__).resolve())}))"
                ),
            ],
            cwd=root,
            env=env,
        ))
        if prefix.resolve() not in Path(imported["path"]).parents:
            raise RuntimeError(
                f"wheel import escaped isolated environment: {imported['path']}"
            )
        version_line = _run(
            [str(railmux), "--version"],
            cwd=root,
            env=env,
        ).strip()
        if version_line != f"railmux {imported['version']}":
            raise RuntimeError(f"unexpected --version output: {version_line!r}")
        doctor = json.loads(_run(
            [str(railmux), "doctor", "--json"],
            cwd=root,
            env=env,
        ))
        if doctor.get("schema_version") != 1:
            raise RuntimeError("wheel doctor emitted an unexpected schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
