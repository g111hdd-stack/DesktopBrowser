import os
import json
import threading
import pyautogui
import webbrowser

from contextlib import suppress
from PyQt5 import QtWidgets, QtGui, QtCore
from selenium.common.exceptions import WebDriverException, NoSuchWindowException, InvalidSessionIdException

from log_api import logger
from database.db import DbConnection
from config import ICON_PATH, INFO_ICON_PATH, NAME
from web_driver.wd import WebDriver, AuthException


class BrowserApp(QtWidgets.QWidget):
    """Окно программы"""

    # Сигналы для обновления интерфейса из потоков
    browser_loaded = QtCore.pyqtSignal(bool)
    error_message = QtCore.pyqtSignal(str)

    def __init__(self, user: str, group: str, db_conn: DbConnection) -> None:
        super().__init__()
        self.info_icon = None
        self.auto_checkbox = None
        self.launch_button = None
        self.market_select = None
        self.clear_checkbox = None
        self.marketplace_select = None
        self.credentials_file = 'credentials.json'

        self.user = user
        self.group = group

        # Установка названия и иконки приложения
        self.setWindowTitle(NAME)
        self.setWindowIcon(QtGui.QIcon(ICON_PATH))

        # Центрирование окна на экране
        screen_width, screen_height = pyautogui.size()
        x_position = (screen_width - 425) // 2
        y_position = (screen_height - 100) // 2
        self.setGeometry(x_position, y_position, 425, 100)

        self.web_drivers = []  # Список активных браузеров
        self.db_conn = db_conn
        self.markets = self.db_conn.info(group)  # Загрузка доступных маркетов из БД

        # Подключение сигналов
        self.browser_loaded.connect(self.on_browser_loaded)
        self.error_message.connect(self.on_error_message)

        self.init_ui()
        self.load_credentials()

    def init_ui(self) -> None:
        """Инициализация интерфейса"""

        # Список уникальных маркетплейсов
        marketplaces = sorted(list({m.marketplace for m in self.markets}))

        self.marketplace_select = QtWidgets.QComboBox()
        self.marketplace_select.addItems(marketplaces)
        self.marketplace_select.currentTextChanged.connect(self.update_markets)

        self.market_select = QtWidgets.QComboBox()
        self.update_markets()

        self.launch_button = QtWidgets.QPushButton("Запуск браузера")
        self.launch_button.clicked.connect(self.launch_browser)

        # Чекбокс для автоматической авторизации
        self.clear_checkbox = QtWidgets.QCheckBox("С очисткой профиля", self)
        self.clear_checkbox.setChecked(False)
        self.clear_checkbox.hide()

        # Чекбокс для автоматической авторизации
        self.auto_checkbox = QtWidgets.QCheckBox("Автоматическая авторизация", self)
        self.auto_checkbox.setChecked(True)

        # Иконка с подсказкой по авторизации
        self.info_icon = QtWidgets.QToolButton(self)
        self.info_icon.setIcon(QtGui.QIcon(INFO_ICON_PATH))
        self.info_icon.setToolTip("Если установлена галочка\n"
                                  "при запуске браузера будет\n"
                                  "включена автоматическая\n"
                                  "авторизация в ЛК.\n"
                                  "Если Вы уже авторизованы\n"
                                  "уберите галочку либо\n"
                                  "дождитесь входа в ЛК.")
        self.info_icon.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.WhatsThisCursor))
        self.info_icon.setIconSize(QtCore.QSize(16, 16))

        self.clear_checkbox.stateChanged.connect(self.clear_text_button)
        self.auto_checkbox.stateChanged.connect(self.auto_text_button)

        # Макет для чекбокса и иконки
        auto_auth_layout = QtWidgets.QHBoxLayout()
        auto_auth_layout.addWidget(self.auto_checkbox)
        auto_auth_layout.addWidget(self.clear_checkbox)
        auto_auth_layout.addWidget(self.info_icon)
        auto_auth_layout.addStretch()

        # Основной макет окна
        form_layout = QtWidgets.QFormLayout()
        form_layout.addRow("Выберите маркетплейс:", self.marketplace_select)
        form_layout.addRow("Выберите рынок:", self.market_select)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(form_layout)
        layout.addLayout(auto_auth_layout)
        layout.addWidget(self.launch_button)

        self.launch_button.setEnabled(True)
        self.auto_text_button()
        self.setLayout(layout)

    def clear_text_button(self) -> None:
        """Обновление текста на кнопке в зависимости от типа автоматизации"""

        if self.launch_button.isEnabled():
            if self.clear_checkbox.isChecked() and self.auto_checkbox.isChecked():
                self.launch_button.setText("🤖Запуск браузера С автоматической авторизацией и очисткой профиля🤖")
            else:
                self.launch_button.setText("🤖Запуск браузера С автоматической авторизацией🤖")

    def auto_text_button(self) -> None:
        """Обновление текста на кнопке в зависимости от типа авторизации"""

        if self.launch_button.isEnabled():
            if self.auto_checkbox.isChecked():
                self.clear_checkbox.show()
                self.clear_text_button()
                self.info_icon.setToolTip("Если установлена галочка\n"
                                          "на «Автоматическая авторизация»\n"
                                          "при запуске браузера будет\n"
                                          "включена автоматическая\n"
                                          "авторизация в ЛК.\n"
                                          "Если установлена галочка\n"
                                          "на «С очисткой профиля»\n"
                                          "профиль браузера по кабинету\n"
                                          "будет создан заново\n"
                                          "Если Вы уже авторизованы\n"
                                          "уберите галочку либо\n"
                                          "дождитесь входа в ЛК.")
            else:
                self.clear_checkbox.hide()
                self.clear_checkbox.setChecked(False)
                self.launch_button.setText("🖐🏻Запуск браузера БЕЗ автоматической авторизации🖐🏻")
                self.info_icon.setToolTip("Если установлена галочка\n"
                                          "при запуске браузера будет\n"
                                          "включена автоматическая\n"
                                          "авторизация в ЛК.\n"
                                          "Если Вы уже авторизованы\n"
                                          "уберите галочку либо\n"
                                          "дождитесь входа в ЛК.")

    def update_markets(self) -> None:
        """Обновление выпадающего списка компаний при выборе маркетплейса"""

        selected_marketplace = self.marketplace_select.currentText()
        filtered_companies = sorted([m.name_company for m in self.markets if m.marketplace == selected_marketplace])
        self.market_select.clear()
        self.market_select.addItems(filtered_companies)

    def launch_browser(self) -> None:
        """Обработка запуска браузера (в отдельном потоке)"""

        self.launch_button.setEnabled(False)
        self.launch_button.setText('Загружаю браузер...')
        threading.Thread(target=self.launch_browser_thread, daemon=True).start()

    def launch_browser_thread(self) -> None:
        """Запуск браузера"""

        marketplace = self.marketplace_select.currentText()
        name_company = self.market_select.currentText()
        auto = self.auto_checkbox.isChecked()
        clear = self.clear_checkbox.isChecked()

        if clear:
            result = QtWidgets.QMessageBox.question(
                self,
                "Подтверждение очистки",
                f"Профиль браузера для «{name_company} {marketplace}» будет удалён и создан заново.\n\n"
                f"Это приведёт к потере всех данных, связанных с этим профилем: "
                f"установленные расширения, история, кэш, cookies и другие настройки.\n\n"
                f"Вы действительно хотите продолжить?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No
            )
            if result != QtWidgets.QMessageBox.Yes:
                self.browser_loaded.emit(True)
                return

        # Получение данных о маркете из БД
        try:
            market = self.db_conn.get_market(marketplace=marketplace, name_company=name_company)
        except RuntimeError as er:
            try:
                text = f"{str(er)}"
                logger.error(description=text)
            except Exception as e:
                text = str(e)
            QtWidgets.QMessageBox.critical(self, "Ошибка", text + '\nПроверте интернет соединение')
            self.browser_loaded.emit(True)
            return

        self.cleanup_inactive_drivers()

        browser_id = f"{market.connect_info.phone}_{market.marketplace.lower()}"
        log_startswith = f"{market.marketplace} - {market.name_company}: "

        try:
            with suppress(NoSuchWindowException, InvalidSessionIdException):
                # Проверка, не открыт ли уже браузер с этим аккаунтом
                if browser_id not in [driver.browser_id for driver in self.web_drivers]:
                    # Запуск браузера
                    web_driver = WebDriver(market=market, user=self.user, auto=auto, clear=clear, db_conn=self.db_conn)
                    self.web_drivers.append(web_driver)

                    url = market.marketplace_info.link

                    web_driver.load_url(url=url)

        except WebDriverException as e:
            # Обработка ошибок драйвера Chrome
            if "session not created" in str(e):
                logger.error(user=self.user, description="Неудалось запустить сессию")
                self.error_message.emit("Неудалось запустить сессию.\n\nВозможно открыта ещё одна версия программы")
            else:
                logger.error(user=self.user, description=f"{log_startswith}Ошибка WebDriver. {str(e).splitlines()[0]}")
                self.error_message.emit(str(e))

        except AuthException as e:
            # Обработка кастомной ошибки
            self.error_message.emit(str(e))

        except Exception as e:
            # Обработка не предвиденной ошибки
            if 'Отказано в доступе' in str(e).splitlines()[0]:
                logger.error(user=self.user,
                             description=f"{log_startswith}Ошибка браузера. {str(e).splitlines()[0]}\n\n"
                                         f"Убедитесь что файл приложения расположен в отдельной папке,"
                                         f" и эта папка не находится в системной директории.")
            else:
                logger.error(user=self.user, description=f"{log_startswith}Ошибка браузера. {str(e).splitlines()[0]}")

        self.browser_loaded.emit(True)

    def on_browser_loaded(self, success) -> None:
        """Обработка сигнала после загрузки браузера"""

        self.launch_button.setEnabled(True)
        self.auto_text_button()

    @staticmethod
    def on_error_message(text) -> None:
        """Обработка сигнала ошибки"""

        if text:
            QtWidgets.QMessageBox.critical(None, 'Ошибка автоматизации', text)

    def cleanup_inactive_drivers(self):
        """Удаление неактивных драйверов из памяти"""

        self.web_drivers = [driver for driver in self.web_drivers if driver.is_browser_active()]

    def save_credentials(self) -> None:
        """Сохранение выбора маркетплейса, компании и режима авторизации"""

        if os.path.exists(self.credentials_file):
            with open(self.credentials_file, 'r') as f:
                try:
                    credentials = json.load(f)
                except json.JSONDecodeError:
                    credentials = {}
        else:
            credentials = {}

        credentials.update({
            "marketplace": self.marketplace_select.currentText(),
            "name_company": self.market_select.currentText(),
            "auto": self.auto_checkbox.isChecked()
        })
        with open(self.credentials_file, 'w') as f:
            json.dump(credentials, f, indent=4)

    def load_credentials(self) -> None:
        """Загрузка предыдущих сохранённых настроек"""

        if os.path.exists(self.credentials_file):
            with open(self.credentials_file, 'r') as f:
                credentials = json.load(f)
                marketplace = credentials.get("marketplace", "")
                name_company = credentials.get("name_company", "")
                auto = credentials.get("auto", True)

                index_marketplace = self.marketplace_select.findText(marketplace)
                if index_marketplace != -1:
                    self.marketplace_select.setCurrentIndex(index_marketplace)
                    index_name_company = self.market_select.findText(name_company)
                    if index_name_company != -1:
                        self.market_select.setCurrentIndex(index_name_company)
                    else:
                        self.market_select.setCurrentIndex(0)
                else:
                    self.marketplace_select.setCurrentIndex(0)
                    self.market_select.setCurrentIndex(0)

                self.auto_checkbox.setChecked(auto)

    def closeEvent(self, event) -> None:
        """Завершение работы: сохранение настроек, закрытие браузеров"""

        self.save_credentials()
        self.cleanup_inactive_drivers()
        for driver in self.web_drivers:
            driver.quit()
        logger.info(user=self.user, description="Выход из приложения")
        event.accept()
