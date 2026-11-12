"""初始化项目的本地数据目录和 SQLite 数据库。"""

import argparse
import sqlite3

from .storage import DEFAULT_DATABASE, Store, exclusive_run


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 X001 的本地数据目录和数据库")
    parser.parse_args()

    database = DEFAULT_DATABASE.resolve()
    try:
        with exclusive_run(database):
            store = Store(database)
            try:
                for folder in ("ocr_images", "ocr_results"):
                    (database.parent / folder).mkdir(exist_ok=True)
                store.captures.mkdir(exist_ok=True)
                store.initialize()
            finally:
                store.db.close()
    except (OSError, RuntimeError, sqlite3.Error) as error:
        parser.exit(1, f"初始化失败：{error}\n")

    print(f"本地数据目录已初始化：{database.parent}")


if __name__ == "__main__":
    main()
