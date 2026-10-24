"""Janela principal: login no portal oficial, depois escolha do servidor e jogo.

Fluxo:
  1. Servidores — a lista pública aparece ao abrir o cliente.
  2. Ao clicar em Jogar, o portal entrega o endereço do jogo. Sem sessão, ele
     redireciona para /login: mostramos a página oficial de login e, assim
     que o portal confirma a sessão, abrimos o servidor escolhido.
O jogo abre em janela própria, no host Flash executado pelo Wine.
"""
import logging
import threading

from PySide6.QtCore import QObject, QSettings, Qt, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from . import game, servers

STATE = game.PROJECT / ".local"
SESSION_COOKIE = "oas_user"
RECENT_LIMIT = 5
LOG = logging.getLogger("legendclient.app")

STYLE = """
QWidget { background: #17141f; color: #e9e2d0; font-size: 14px; }
QLabel#title { font-size: 22px; font-weight: 600; color: #f0c674; }
QLabel#hint { color: #a59d8c; }
QLabel#section { color: #f0c674; font-weight: 600; margin-top: 8px; }
QLineEdit, QComboBox, QListWidget {
    background: #221d2c; border: 1px solid #3a3247; border-radius: 6px; padding: 6px;
}
QListWidget::item { padding: 6px; border-radius: 4px; }
QListWidget::item:selected { background: #6b3d1f; color: #fff3d6; }
QPushButton {
    background: #2c2538; border: 1px solid #4a405a; border-radius: 6px; padding: 8px 16px;
}
QPushButton:hover { background: #3a3149; }
QPushButton#play {
    background: #b5651d; border-color: #d98a3d; color: #fff3d6; font-size: 18px;
    font-weight: 600; padding: 12px;
}
QPushButton#play:hover { background: #c97425; }
QPushButton#play:disabled { background: #4a3a2c; color: #8a7d6c; border-color: #4a3a2c; }
"""


def create_profile(parent):
    profile = QWebEngineProfile("legendclient", parent)
    profile.setPersistentStoragePath(str(STATE / "web"))
    profile.setCachePath(str(STATE / "web-cache"))
    profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
    return profile


class ServerLoader(QObject):
    loaded = Signal(str, list)
    failed = Signal(str, str)

    def load(self, region):
        def work():
            try:
                self.loaded.emit(region, servers.fetch_servers(region))
            except servers.ServerListError as exc:
                self.failed.emit(region, str(exc))
        threading.Thread(target=work, daemon=True, name=f"servers-{region}").start()


class GameResolver(QObject):
    """Abre /serverlist/sN num navegador invisível e lê o endereço do game.jsp."""
    resolved = Signal(object, str)
    expired = Signal(object)
    failed = Signal(object, str)

    FRAME_SCRIPT = """(function () {
        var frame = document.getElementById("home_playFrame")
            || document.querySelector("iframe[src*='/client/game.jsp']");
        return frame ? frame.src : "";
    })()"""

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.page = QWebEnginePage(profile, self)
        self.page.setAudioMuted(True)
        self.page.loadFinished.connect(self._loaded)
        self.target = None

    def resolve(self, region, server):
        self.target = (region, server)
        self.page.load(QUrl(servers.play_url(region, server.id)))

    def _loaded(self, ok):
        if not self.target:
            return
        target = self.target
        if "/login" in self.page.url().path():
            self._finish()
            self.expired.emit(target)
        elif not ok:
            self._finish()
            self.failed.emit(target, "o portal não respondeu")
        else:
            self.page.runJavaScript(self.FRAME_SCRIPT, 0, lambda src: self._frame(target, src))

    def _frame(self, target, src):
        if target is not self.target:
            return
        self._finish()
        if src:
            self.resolved.emit(target, src)
        else:
            self.failed.emit(target, "o portal não entregou o jogo (servidor em manutenção?)")

    def _finish(self):
        self.target = None
        self.page.load(QUrl("about:blank"))


class LoginView(QWidget):
    """Página oficial de login; avisa quando o portal confirma a sessão."""
    logged_in = Signal()
    cancelled = Signal()

    SESSION_SCRIPT = f"document.cookie.split('; ').some(c => c.startsWith('{SESSION_COOKIE}='))"

    def __init__(self, profile):
        super().__init__()
        self.region = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        back = QPushButton("← Voltar aos servidores")
        back.clicked.connect(self._cancel)
        self.title = QLabel("Entre na sua conta", objectName="title")
        header_layout.addWidget(back)
        header_layout.addWidget(self.title)
        header_layout.addStretch()
        header_layout.addWidget(QLabel("O jogo abre assim que o login for concluído.",
                                       objectName="hint"))
        self.browser = QWebEngineView()
        self.browser.setPage(QWebEnginePage(profile, self.browser))
        self.browser.loadFinished.connect(self._loaded)
        layout.addWidget(header)
        layout.addWidget(self.browser, 1)

    def open(self, region, server):
        self.region = region
        self.title.setText(f"Entre na sua conta para jogar em {server.label}")
        self.browser.load(QUrl(servers.portal_url(region) + "/login"))

    def close_page(self):
        self.region = None
        self.browser.load(QUrl("about:blank"))

    def _loaded(self, ok):
        if not self.region:
            return
        url = self.browser.url()
        on_portal = url.host() == QUrl(servers.portal_url(self.region)).host()
        if not on_portal or "login" in url.path():
            return  # ainda no formulário, ou num login social fora do portal
        self.browser.page().runJavaScript(self.SESSION_SCRIPT, 0, self._session_checked)

    def _session_checked(self, has_session):
        if self.region and has_session:
            self.close_page()
            self.logged_in.emit()

    def _cancel(self):
        self.close_page()
        self.cancelled.emit()


class ServersView(QWidget):
    play_requested = Signal(str, object)
    region_changed = Signal(str)
    logout_requested = Signal()

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.servers = []

        self.region = QComboBox()
        for code, name in servers.REGIONS.items():
            self.region.addItem(f"{name} ({code})", code)
        index = self.region.findData(settings.value("region", "lobr"))
        self.region.setCurrentIndex(max(index, 0))
        logout = QPushButton("Sair da conta")
        top = QHBoxLayout()
        top.addWidget(QLabel("Escolha o servidor", objectName="title"))
        top.addStretch()
        top.addWidget(QLabel("Região"))
        top.addWidget(self.region)
        top.addWidget(logout)

        self.recent = QListWidget()
        self.recent.setMaximumHeight(4 + 34 * RECENT_LIMIT)
        self.filter = QLineEdit(placeholderText="Buscar por número ou nome (ex.: 1237, Gatinho)")
        self.list = QListWidget()
        self.status = QLabel(objectName="hint", wordWrap=True)
        self.play = QPushButton("Jogar", objectName="play")
        self.play.setEnabled(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.addLayout(top)
        self.recent_label = QLabel("Jogados recentemente", objectName="section")
        layout.addWidget(self.recent_label)
        layout.addWidget(self.recent)
        layout.addWidget(QLabel("Todos os servidores", objectName="section"))
        layout.addWidget(self.filter)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.play)

        self.region.currentIndexChanged.connect(
            lambda: self.region_changed.emit(self.current_region()))
        logout.clicked.connect(self.logout_requested)
        self.filter.textChanged.connect(self.show_servers)
        for widget in (self.list, self.recent):
            widget.currentItemChanged.connect(self._selection_changed)
            widget.itemDoubleClicked.connect(self._play_item)
        self.play.clicked.connect(self._play_selected)

    def current_region(self):
        return self.region.currentData()

    def set_status(self, text):
        self.status.setText(text)

    def set_loading(self):
        self.servers = []
        self.list.clear()
        self.recent.clear()
        self.play.setEnabled(False)
        self.set_status("Baixando lista de servidores…")

    def set_servers(self, found):
        self.servers = found
        self.show_servers()
        self.set_status(f"{len(found)} servidores. Selecione um e clique em Jogar.")

    def recent_ids(self):
        value = self.settings.value(f"recent/{self.current_region()}", [])
        if isinstance(value, str):
            value = [value]
        return [int(v) for v in value or []]

    def remember(self, server):
        ids = [server.id] + [i for i in self.recent_ids() if i != server.id]
        self.settings.setValue(f"recent/{self.current_region()}", ids[:RECENT_LIMIT])
        self.show_servers()

    def show_servers(self):
        needle = self.filter.text().strip().lower()
        by_id = {server.id: server for server in self.servers}
        self.recent.clear()
        for server_id in self.recent_ids():
            if server_id in by_id:
                self._add(self.recent, by_id[server_id])
        has_recent = self.recent.count() > 0
        self.recent.setVisible(has_recent)
        self.recent_label.setVisible(has_recent)
        self.list.clear()
        for server in self.servers:
            if not needle or needle in server.label.lower() or needle == str(server.id):
                self._add(self.list, server)
        self._selection_changed()

    @staticmethod
    def _add(widget, server):
        item = QListWidgetItem(server.label)
        item.setData(Qt.UserRole, server)
        widget.addItem(item)

    def _selected(self):
        for widget in (self.list, self.recent):
            if widget.hasFocus() and widget.currentItem():
                return widget.currentItem().data(Qt.UserRole)
        for widget in (self.recent, self.list):
            if widget.currentItem():
                return widget.currentItem().data(Qt.UserRole)
        return None

    def _selection_changed(self, *args):
        server = self._selected()
        self.play.setEnabled(server is not None)
        self.play.setText(f"Jogar — {server.label}" if server else "Jogar")

    def _play_item(self, item):
        self.play_requested.emit(self.current_region(), item.data(Qt.UserRole))

    def _play_selected(self):
        server = self._selected()
        if server:
            self.play_requested.emit(self.current_region(), server)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Legend Online")
        self.resize(1100, 780)
        self.setStyleSheet(STYLE)
        self.settings = QSettings(str(STATE / "settings.ini"), QSettings.IniFormat)
        self.pending = None
        self.games = []

        self.profile = create_profile(self)
        self.login = LoginView(self.profile)
        self.servers_view = ServersView(self.settings)
        self.stack = QStackedWidget()
        for view in (self.servers_view, self.login):
            self.stack.addWidget(view)
        self.setCentralWidget(self.stack)

        self.loader = ServerLoader()
        self.loader.loaded.connect(self.on_servers)
        self.loader.failed.connect(self.on_servers_failed)
        self.resolver = GameResolver(self.profile, self)
        self.resolver.resolved.connect(self.on_game_frame)
        self.resolver.failed.connect(self.on_game_failed)
        self.resolver.expired.connect(self.on_session_expired)
        self.servers_view.play_requested.connect(self.play)
        self.servers_view.region_changed.connect(self.load_servers)
        self.servers_view.logout_requested.connect(self.logout)

        self.login.logged_in.connect(self.on_logged_in)
        self.login.cancelled.connect(self.on_login_cancelled)
        self.load_servers(self.region())

    def region(self):
        return self.servers_view.current_region()

    def on_session_expired(self, target):
        LOG.info("sem sessão; pedindo login")
        self.pending = target
        self.login.open(*target)
        self.stack.setCurrentWidget(self.login)

    def on_logged_in(self):
        LOG.info("sessão confirmada pelo portal")
        self.stack.setCurrentWidget(self.servers_view)
        if self.pending:
            target, self.pending = self.pending, None
            self.play(*target)

    def on_login_cancelled(self):
        self.pending = None
        self.stack.setCurrentWidget(self.servers_view)
        self.servers_view.set_status("Login cancelado.")
        self.servers_view._selection_changed()

    def logout(self):
        LOG.info("saindo da conta")
        self.profile.cookieStore().deleteAllCookies()
        self.servers_view.set_status("Você saiu da conta. O login será pedido ao jogar.")

    def load_servers(self, region):
        self.settings.setValue("region", region)
        self.servers_view.set_loading()
        self.loader.load(region)

    def on_servers(self, region, found):
        if region != self.region():
            return
        LOG.info("%s: %d servidores", region, len(found))
        self.servers_view.set_servers(found)

    def on_servers_failed(self, region, error):
        if region != self.region():
            return
        LOG.error("%s: lista de servidores falhou: %s", region, error)
        self.servers_view.set_status(f"Não foi possível obter a lista: {error}")

    def play(self, region, server):
        LOG.info("abrindo servidor %s (%s)", server.id, server.label)
        self.servers_view.play.setEnabled(False)
        self.servers_view.set_status(f"{server.label}: preparando o jogo…")
        self.resolver.resolve(region, server)

    def on_game_frame(self, target, src):
        region, server = target
        try:
            process = game.launch(game.start_from_game_frame(src),
                                  f"Legend Online — {server.label}",
                                  STATE / "logs/flashhost.log")
        except game.GameError as exc:
            self.on_game_failed(target, str(exc))
            return
        self.games.append(process)
        self.servers_view.remember(server)
        self.servers_view.set_status(f"{server.label}: jogo aberto em janela própria.")

    def on_game_failed(self, target, error):
        region, server = target
        LOG.error("falha ao abrir %s: %s", server.label, error)
        self.servers_view.set_status(f"{server.label}: {error}")
        self.servers_view._selection_changed()
        QMessageBox.critical(self, "Não foi possível abrir o jogo", error)
