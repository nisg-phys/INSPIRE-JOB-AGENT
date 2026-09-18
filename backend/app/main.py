from app.config import get_settings


def main() -> None:
    get_settings()
    print("backend: config loaded OK")


if __name__ == "__main__":
    main()
