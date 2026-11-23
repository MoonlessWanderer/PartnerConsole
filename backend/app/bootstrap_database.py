"""One-shot database preparation command for a maintenance window."""
import sys


def main() -> int:
    try:
        # Import inside the guard so malformed connection settings or a missing
        # driver cannot print a raw URL or database error to the console.
        from app.services.bootstrap import initialize_database

        initialize_database()
    except Exception as exc:
        print(f"Database bootstrap failed ({type(exc).__name__}). "
              "Keep workers stopped and follow dev/docs/guides/production-bootstrap.md.", file=sys.stderr)
        return 1
    print("Database bootstrap completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
