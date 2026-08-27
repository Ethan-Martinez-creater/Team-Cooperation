from pathlib import Path

from coifesp_harness.control_plane.bootstrap import build_application, load_environment_settings


def main() -> None:
    app = build_application(settings=load_environment_settings(Path(".env")))
    print(f"CONTROL_PLANE_BUILD_OK ready={app.state.database_readiness()}")


if __name__ == "__main__":
    main()
