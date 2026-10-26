"""Print deployment validation guidance without printing connection strings/secrets.

Run from the backend root: python -m scripts.check_deployment
"""
import sys
from pydantic import ValidationError


def main():
    try:
        from app.core.config import validate_production_settings, ProductionConfigurationError
    except ValidationError as exc:
        print("Invalid environment variable types:")
        for error in exc.errors():
            print("- " + ".".join(map(str, error["loc"])) + " (" + error["type"] + ")")
        return 1
    try:
        production = validate_production_settings()
    except ProductionConfigurationError as exc:
        print(str(exc))
        return 1
    print("Configuration validation passed" + (" (Render/production checks enabled)." if production else " (development checks only)."))
    print("This checks configuration only; MySQL, Redis, migrations, and readiness still need verification.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
