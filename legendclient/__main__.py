"""Ponto de entrada: python -m legendclient"""
import logging
from logging.handlers import RotatingFileHandler
import sys

from .game import PROJECT


def configure_logging():
    logdir = PROJECT / ".local/logs"
    logdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(logdir / "legendclient.log", maxBytes=5 * 1024 * 1024,
                                  backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S%z")


def main():
    configure_logging()
    from PySide6.QtWidgets import QApplication
    from .app import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("legendclient")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
