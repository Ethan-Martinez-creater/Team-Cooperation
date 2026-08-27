import json
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KEYCLOAK_ROOT = Path(r"E:\keyclock")
DIST = KEYCLOAK_ROOT / "keycloak-26.7.0"
RUNTIME = KEYCLOAK_ROOT / "runtime"
TEMPLATE = PROJECT_ROOT / "deploy" / "keycloak"


def copy_new(source: Path, target: Path) -> None:
    if target.exists():
        raise RuntimeError(f"refusing to overwrite existing file: {target}")
    shutil.copyfile(source, target)


def main() -> int:
    if not (DIST / "bin" / "kc.bat").is_file():
        raise RuntimeError("verified Keycloak 26.7.0 distribution is missing")
    realm = json.loads((TEMPLATE / "coifesp-realm.json").read_text(encoding="utf-8"))
    if realm.get("realm") != "coifesp":
        raise RuntimeError("realm template identity is invalid")
    RUNTIME.mkdir(exist_ok=True)
    import_dir = DIST / "data" / "import"
    import_dir.mkdir(parents=True, exist_ok=True)
    copy_new(TEMPLATE / "keycloak.env.example", RUNTIME / "keycloak.env.example")
    copy_new(
        TEMPLATE / "create-keycloak-database.sql.example",
        RUNTIME / "create-keycloak-database.sql.example",
    )
    copy_new(TEMPLATE / "coifesp-realm.json", import_dir / "coifesp-realm.json")
    print(
        "KEYCLOAK_RUNTIME_PREPARED "
        f"distribution={DIST} runtime={RUNTIME} realm=coifesp secrets=absent"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
