"""uvicorn 入口：HOST/PORT/DATA_DIR 环境变量可覆盖。"""

import uvicorn

from app import config


def main() -> None:
    uvicorn.run("app.main:app", host=config.host(), port=config.port())


if __name__ == "__main__":
    main()
