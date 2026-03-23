import os
import sys
import json
import shutil
import zipfile
import requests
import threading
import pyautogui
import subprocess

from packaging import version
from PyQt5 import QtWidgets, QtGui, QtCore
from cryptography.fernet import Fernet, InvalidToken

from log_api import logger
from .browser_app import BrowserApp
from database.db import DbConnection
from config import ICON_PATH, VERSION, INFO_ICON_PATH, NAME


class LoginWorker(QtCore.QThread):
    """Поток авторизации, чтобы не блокировать GUI"""

    login_checked = QtCore.pyqtSignal(bool, str, str, str)
    error_occurred = QtCore.pyqtSignal(str)

    def __init__(self, db_conn, login, password) -> None:
        super().__init__()

        self.db_conn = db_conn
        self.login = login
        self.password = password

    def run(self) -> None:
        """Авторизация пользователя"""
        try:
            group = self.db_conn.check_user(login=self.login, password=self.password)
            self.login_checked.emit(group is not None, self.login, self.password, group)
        except Exception as e:
            self.error_occurred.emit(str(e))


class LoginWindow(QtWidgets.QWidget):
    """Окно авторизации"""
    def __init__(self):
        super().__init__()

        self.key = None
        self.worker = None
        self.db_conn = None
        self.version = None
        self.info_icon = None
        self.login_input = None
        self.browser_app = None
        self.login_button = None
        self.password_input = None
        self.remember_me_checkbox = None

        # Установка названия и иконки приложения
        self.setWindowTitle(NAME)
        self.setWindowIcon(QtGui.QIcon(ICON_PATH))

        self.credentials_file = 'credentials.json'

        # Центрирование окна на экране
        screen_width, screen_height = pyautogui.size()
        width = max(screen_width // 6, 300)
        x_position = (screen_width - width) // 2
        y_position = (screen_height - 100) // 2
        self.setGeometry(x_position, y_position, width, 100)

        self.init_ui()

        # Подключение к базе в отдельном потоке
        threading.Thread(target=self.connect_to_db, daemon=True).start()

    def init_ui(self) -> None:
        """Инициализация интерфейса"""

        form_layout = QtWidgets.QFormLayout()

        self.login_input = QtWidgets.QLineEdit(self)
        self.password_input = QtWidgets.QLineEdit(self)
        self.password_input.setEchoMode(QtWidgets.QLineEdit.Password)

        form_layout.addRow("Логин:", self.login_input)
        form_layout.addRow("Пароль:", self.password_input)

        self.remember_me_checkbox = QtWidgets.QCheckBox("Запомнить", self)

        # Подсказка к чекбоксу
        self.info_icon = QtWidgets.QToolButton(self)
        self.info_icon.setIcon(QtGui.QIcon(INFO_ICON_PATH))
        self.info_icon.setToolTip("Если установлена галочка\n"
                                  "сохранит логин и пароль\n"
                                  "для следующего входа.\n"
                                  "Если убрать галочку логин\n"
                                  "и пароль будут забыты.")
        self.info_icon.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.WhatsThisCursor))
        self.info_icon.setIconSize(QtCore.QSize(16, 16))

        auto_auth_layout = QtWidgets.QHBoxLayout()
        auto_auth_layout.addWidget(self.remember_me_checkbox)
        auto_auth_layout.addWidget(self.info_icon)
        auto_auth_layout.addStretch()

        self.login_button = QtWidgets.QPushButton("Подключение...", self)
        self.login_button.clicked.connect(self.check_login)
        self.login_button.setEnabled(False)
        self.login_button.setDefault(True)

        # Изначально всё выключено до соединения с БД
        self.remember_me_checkbox.setEnabled(False)
        self.login_input.setEnabled(False)
        self.password_input.setEnabled(False)

        # Вход по Enter
        self.login_input.returnPressed.connect(self.login_button.click)
        self.password_input.returnPressed.connect(self.login_button.click)

        main_layout = QtWidgets.QVBoxLayout()
        main_layout.addLayout(form_layout)
        main_layout.addLayout(auto_auth_layout)
        main_layout.addWidget(self.login_button)

        self.setLayout(main_layout)

    def connect_to_db(self) -> None:
        """Подключение к БД и проверка версии"""

        try:
            self.db_conn = DbConnection()
            self.key = self.db_conn.get_key()
            actual_version = self.db_conn.get_version()

            if actual_version.version != VERSION and actual_version.version != '1.0.9':
                self.login_button.setText("Доступно обновление")
                logger.info(description=f"Обнаружена новая версия: {actual_version.version} (у вас {VERSION})")
                self.prompt_update_required(url=actual_version.url, actual_ver=actual_version.version)
                return

            # Удаление старых версий
            try:
                for file_name in os.listdir(os.getcwd()):
                    if file_name.startswith("ProxyBrowser") and file_name.endswith(".exe"):
                        ver = file_name[:-4].split()[1]
                        if len(ver.split('.')) == 3:
                            if version.parse(ver) != version.parse(VERSION):
                                os.remove(file_name)
                        else:
                            os.remove(file_name)
            except Exception as e:
                logger.error(description=f"Ошибка при удалении старой версии. {str(e)}\n\n"
                                         f"Удалите другие версии отличные от '{NAME}'.")

            # Разблокировка полей входа
            self.load_credentials()
            self.remember_me_checkbox.setEnabled(True)
            self.login_input.setEnabled(True)
            self.password_input.setEnabled(True)
            self.login_button.setText("Войти")
            self.login_button.setEnabled(True)

        except Exception as e:
            self.show_error_message(str(e))

    def check_login(self) -> None:
        """Запуск проверки логина"""

        self.remember_me_checkbox.setEnabled(False)
        self.login_input.setEnabled(False)
        self.password_input.setEnabled(False)
        self.login_button.setText("Проверка...")
        self.login_button.setEnabled(False)

        login = self.login_input.text()
        password = self.password_input.text()

        # Поток проверки
        self.worker = LoginWorker(self.db_conn, login, password)
        self.worker.error_occurred.connect(self.show_error_message)
        self.worker.login_checked.connect(self.update_ui_after_login)
        self.worker.start()

    def show_error_message(self, error_message: str) -> None:
        """Показ окна ошибки"""

        try:
            text = f"{error_message}"
            logger.error(description=text)
        except Exception as e:
            text = str(e)
        QtWidgets.QMessageBox.critical(self, "Ошибка", text + '\nПроверте интернет соединение')
        self.close()

    def update_ui_after_login(self, is_valid_user: bool, login: str, password: str, group: str) -> None:
        """Обновление интерфейса после проверки логина"""

        self.remember_me_checkbox.setEnabled(True)
        self.login_input.setEnabled(True)
        self.password_input.setEnabled(True)
        self.login_button.setText("Войти")
        self.login_button.setEnabled(True)

        if is_valid_user:
            logger.info(user=login, description="Вход в приложение")
            if self.remember_me_checkbox.isChecked():
                self.save_credentials(login, password)
            self.open_browser_app(login, group)
        else:
            logger.waring(description=f"Неудачная попытка входа в приложение. Логин: {login} Пароль: {password}")
            QtWidgets.QMessageBox.warning(self, "Ошибка", "Неправильный логин или пароль")

    def open_browser_app(self, login: str, group: str) -> None:
        """Переход в BrowserApp"""

        self.browser_app = BrowserApp(user=login, group=group, db_conn=self.db_conn)
        self.browser_app.show()
        self.close()

    def save_credentials(self, login: str = None, password: str = None) -> None:
        """Сохранение логина/пароля в файл"""

        if os.path.exists(self.credentials_file):
            with open(self.credentials_file, 'r') as f:
                try:
                    credentials = json.load(f)
                except json.JSONDecodeError:
                    credentials = {}
        else:
            credentials = {}

        if login is None and password is None:
            credentials.update({
                "login": "",
                "password": "",
                "remember_me": self.remember_me_checkbox.isChecked()
            })
        else:
            fernet = Fernet(self.key)
            credentials.update({
                "login": fernet.encrypt(login.encode()).decode(),
                "password": fernet.encrypt(password.encode()).decode(),
                "remember_me": self.remember_me_checkbox.isChecked()
            })

        with open(self.credentials_file, 'w') as f:
            json.dump(credentials, f, indent=4)

    def load_credentials(self) -> None:
        """Загрузка сохранённых логина/пароля"""

        if os.path.exists(self.credentials_file):
            with open(self.credentials_file, 'r') as f:
                credentials = json.load(f)
                fernet = Fernet(self.key)
                try:
                    login = fernet.decrypt(credentials.get("login", "").encode()).decode()
                    password = fernet.decrypt(credentials.get("password", "").encode()).decode()
                except InvalidToken:
                    login, password = "", ""
                remember_me = credentials.get("remember_me", False)

                self.login_input.setText(login)
                self.password_input.setText(password)
                self.remember_me_checkbox.setChecked(remember_me)

    def closeEvent(self, event) -> None:
        """Закрытие окна, сохранить логин"""
        if not self.remember_me_checkbox.isChecked():
            self.save_credentials()
        event.accept()

    def prompt_update_required(self, url: str, actual_ver: str) -> None:
        dlg = QtWidgets.QMessageBox(self)
        dlg.setIconPixmap(QtGui.QPixmap(INFO_ICON_PATH))
        dlg.setWindowTitle("Доступно обновление")
        dlg.setTextFormat(QtCore.Qt.TextFormat.RichText)
        dlg.setText(
            f"<b>Требуется обновление приложения.</b><br>"
            f"Текущая версия: <code>{VERSION}</code><br>"
            f"Актуальная версия: <code>{actual_ver}</code><br><br>"
            f"Скачайте новую версию по ссылке:<br>"
            f'<a href="{url}">{url}</a><br><br>'
            f"После загрузки — замените файлы в папке приложения."
        )
        dlg.setStandardButtons(QtWidgets.QMessageBox.Open | QtWidgets.QMessageBox.Close)

        dlg.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextBrowserInteraction)
        dlg.setEscapeButton(QtWidgets.QMessageBox.Close)

        btn = dlg.button(QtWidgets.QMessageBox.Open)
        btn.setText("Открыть ссылку")

        res = dlg.exec_()
        if res == QtWidgets.QMessageBox.Open:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

        QtWidgets.QApplication.quit()
        sys.exit(0)
