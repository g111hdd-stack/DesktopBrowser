import os
import time
import shutil
import platform
import datetime
from tkinter import messagebox
import tkinter as tk

from typing import Type
from selenium import webdriver
from contextlib import suppress

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions
from selenium.common.exceptions import NoSuchWindowException, TimeoutException, NoSuchElementException
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

from database.models import Market
from database.db import DbConnection
from email_api import YandexMailClient
from log_api import logger, get_moscow_time
from .create_extension_proxy import create_firefox_proxy_addon

TIME_AWAIT = 5


class AuthException(Exception):
    """Кастомный класс ошибки"""

    def __init__(self, message: str = ''):
        self.message = message
        super().__init__(self.message)


class WebDriver:
    """Управляет браузером Chrome с прокси и автоматизацией входа в маркетплейсы (Ozon, WB, Yandex)."""

    def __init__(self, market: Type[Market], user: str, auto: bool, clear: bool, db_conn: DbConnection) -> None:

        self.user = user
        self.auto = auto
        self.clear = clear
        self.db_conn = db_conn
        self.client_id = market.client_id
        self.mail = market.connect_info.mail
        self.proxy = market.connect_info.proxy
        self.phone = market.connect_info.phone
        self.token = market.connect_info.token
        self.name_company = market.name_company
        self.marketplace = market.marketplace_info
        self.pass_mail = market.connect_info.pass_mail
        self.browser_id = f"{self.phone}_{self.marketplace.marketplace.lower()}"
        self.log_startswith = f"{self.marketplace.marketplace} - {market.name_company}: "

        self.profile_path = os.path.join(os.getcwd(), f"profile/{self.browser_id}")
        if clear and os.path.exists(self.profile_path):
            try:
                shutil.rmtree(self.profile_path)
            except PermissionError:
                raise AuthException(f'Ошибка удаления профиля по пути {self.profile_path}')
            except Exception as e:
                raise AuthException(str(e))
        os.makedirs(self.profile_path, exist_ok=True)

        self.proxy_auth_path = os.path.join(os.getcwd(), f"proxy_auth")
        os.makedirs(self.proxy_auth_path, exist_ok=True)

        ext_path = create_firefox_proxy_addon(self.proxy_auth_path, self.proxy)

        bit = '64' if platform.machine().endswith('64') else ''

        self.options = Options()

        self.options.add_argument("-no-remote")
        self.options.add_argument("-profile")
        self.options.add_argument(self.profile_path)

        self.options.set_preference(
            "general.useragent.override",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) Gecko/20100101 Firefox/142.0"
        )

        self.options.set_preference("dom.webdriver.enabled", False)
        self.options.set_preference("useAutomationExtension", False)
        self.options.set_preference("media.peerconnection.enabled", True)
        self.options.set_preference("privacy.trackingprotection.enabled", False)
        self.options.set_preference("intl.accept_languages", "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7")
        self.options.set_preference("toolkit.telemetry.reportingpolicy.firstRun", False)
        self.options.set_preference("app.update.auto", False)
        self.options.set_preference("app.update.enabled", False)

        self.options.binary_location = str(os.path.join(os.getcwd(),
                                                        f"browser/FirefoxPortable/App/Firefox{bit}/firefox.exe"))

        self.service = Service(executable_path=str(os.path.join(os.getcwd(), f"browser/geckodriver{bit}.exe")))

        self.driver = webdriver.Firefox(service=self.service, options=self.options)
        self.driver.install_addon(ext_path, temporary=True)

        self.driver.maximize_window()

    def check_auth(self) -> None:
        """
        Проверяет, загружена ли страница, и запускает соответствующую стратегию авторизации
        (WB, Ozon, Yandex) по URL. Добавляет и убирает визуальный оверлей во время работы.
        """


        try:
            self.add_overlay()  # затемнение экрана с сообщением
            WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                lambda driver: driver.execute_script("return document.readyState") == "complete"
            )

            last_url = None
            # Пытаемся дождаться полной загрузки страницы (до 6 попыток)
            for _ in range(6):
                self.add_overlay()
                if last_url == self.driver.current_url:
                    break  # URL не меняется — считаем, что загрузка завершена
                last_url = self.driver.current_url
                WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    lambda driver: driver.execute_script("return document.readyState") == "complete"
                )
                time.sleep(TIME_AWAIT)
            else:
                Exception("Превышено время загрузки страницы")

            # Если URL совпадает с ссылкой на маркетплейс — запускаем авторизацию
            if self.marketplace.link in last_url:
                self.add_overlay()
                logger.info(user=self.user, proxy=self.proxy,
                            description=f"{self.log_startswith}Автоматизация запущена")

                # Выбор сценария в зависимости от платформы
                if self.marketplace.marketplace == 'Ozon':
                    self.ozon_auth(self.marketplace)
                elif self.marketplace.marketplace == 'WB':
                    self.wb_auth(self.marketplace)
                elif self.marketplace.marketplace == 'Yandex':
                    self.ya_auth(self.marketplace)
                elif self.marketplace.marketplace == 'МВидео':
                    self.mvideo_auth(self.marketplace)

            # Если уже перешли на личный кабинет — логируем успешный вход
            if self.marketplace.domain in last_url:
                logger.info(user=self.user, proxy=self.proxy,
                            description=f"{self.log_startswith}Вход в ЛК выполнен")

            # Для Yandex — редирект вручную в ЛК
            if 'https://id.yandex.ru' in last_url:
                self.driver.get(f'{self.marketplace.domain}/{self.client_id}/marketplace')
                logger.info(user=self.user, proxy=self.proxy,
                            description=f"{self.log_startswith}Вход в ЛК выполнен")



            self.remove_overlay()  # убираем затемнение

        except (NoSuchWindowException, InvalidSessionIdException):
            self.quit('Окно браузера было преждевременно закрыто')
        except Exception as e:
            self.quit(str(e).splitlines()[0])

    def wb_auth(self, marketplace: Market) -> None:
        """Авторизация в личный кабинет Wildberries по номеру телефона и СМС-коду"""

        logger.info(user=self.user, proxy=self.proxy, description=f"{self.log_startswith}Ввод номера {self.phone}")

        # Пытаемся найти поле и кнопку ввода телефона (до 3 раз)
        for _ in range(3):
            try:
                time.sleep(TIME_AWAIT)
                input_phone = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='phone-input']")))
                input_phone.send_keys(self.phone)

                time.sleep(TIME_AWAIT)
                button_phone = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable((By.XPATH,
                                                                 '//*[@data-testid="submit-phone-button"]')))
                break
            except TimeoutException:
                # Если не удалось — перезагружаем страницу и пробуем снова
                self.driver.refresh()
                self.add_overlay()
        else:
            raise Exception('Страница не получена')

        logger.info(user=self.user, proxy=self.proxy,
                    description=f"{self.log_startswith}Проверка заявки на СМС на номер {self.phone}")

        # Отмечаем время начала запроса кода
        time_request = get_moscow_time()

        # Проверка на незавершённую авторизацию с этим номером
        self.db_conn.check_phone_message(user=self.user, phone=self.phone, time_request=time_request)

        self.remove_overlay()
        button_phone.click()  # Нажимаем кнопку "Получить код"
        self.add_overlay()

        # Добавляем новую запись о попытке запроса кода (до 3 раз при конфликте)
        for _ in range(3):
            try:
                self.db_conn.add_phone_message(user=self.user,
                                               phone=self.phone,
                                               marketplace=marketplace.marketplace,
                                               time_request=time_request)
                break
            except IntegrityError:
                time.sleep(TIME_AWAIT)
        else:
            raise Exception('Ошибка параллельных запросов')

        # Если WB вернул сообщение об ошибке — выкидываем исключение с его текстом
        with suppress(TimeoutException):
            timer_element = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                expected_conditions.presence_of_element_located(
                    (By.XPATH, "//div[contains(@class, 'FormPhoneInputBorderless__error-fWDiaZx8UW')]//span")))
            raise Exception(timer_element.text)

        logger.info(user=self.user, proxy=self.proxy,
                    description=f"{self.log_startswith}Ожидание кода на номер {self.phone}")

        # Получаем код подтверждения из базы
        mes = self.db_conn.get_phone_message(user=self.user,
                                             phone=self.phone,
                                             marketplace=marketplace.marketplace)

        logger.info(user=self.user, proxy=self.proxy,
                    description=f"{self.log_startswith}Код на номер {self.phone} получен: {mes}")
        logger.info(user=self.user, proxy=self.proxy, description=f"{self.log_startswith}Ввод кода {mes}")

        try:
            time.sleep(TIME_AWAIT)
            # Ждём появления полей ввода кода
            inputs_code = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                expected_conditions.presence_of_all_elements_located((By.CSS_SELECTOR,
                                                                      "[data-testid='sms-code-input']")))

            # Вводим код посимвольно в каждое поле
            if len(mes) == len(inputs_code):
                self.remove_overlay()
                for i, input_code in enumerate(inputs_code):
                    input_code.send_keys(mes[i])
                self.add_overlay()
            else:
                raise Exception('Ошибка ввода кода')
        except TimeoutException:
            raise Exception('Отсутствует поле ввода кода')

        logger.info(user=self.user, proxy=self.proxy, description=f"{self.log_startswith}Вход в ЛК")

        # Проверяем вход по домену личного кабинета (4 попытки)
        for _ in range(4):
            time.sleep(TIME_AWAIT)
            if marketplace.domain in self.driver.current_url:
                logger.info(user=self.user, proxy=self.proxy,
                            description=f"{self.log_startswith}Вход в ЛК выполнен")
                return
        else:
            logger.info(user=self.user, proxy=self.proxy,
                        description=f"{self.log_startswith}Автоматизация завершена, вход не подтверждён")

    def ozon_auth(self, marketplace: Market) -> None:
        """Авторизация в Ozon: сначала по email, затем при необходимости — по СМС на телефон"""

        def create_phone_message(btn) -> datetime.datetime:
            """
                Проверяет нет ли конфликта авторизации и создаёт в таблице запись в таблице phone_message

                Параметры:
                    btn: кнопка отправки сообщения

                Результат:
                    datetime.datetime: Время запроса сообщения
            """
            self.add_overlay()
            logger.info(user=self.user, proxy=self.proxy,
                        description=f"{self.log_startswith}Проверка на незавершённую авторизацию")

            # Отмечаем время начала запроса кода
            time_request = get_moscow_time()

            # Проверка на незавершённую авторизацию с этим номером
            self.db_conn.check_phone_message(user=self.user, phone=self.phone, time_request=time_request)

            self.remove_overlay()
            btn.click()  # Нажимаем кнопку "Получить код"
            self.add_overlay()

            logger.info(user=self.user, proxy=self.proxy,
                        description=f"{self.log_startswith}Ожидание кода")

            # Добавляем новую запись о попытке запроса кода (до 3 раз при конфликте)
            for _ in range(3):
                try:
                    self.db_conn.add_phone_message(user=self.user, phone=self.phone,
                                                   marketplace=marketplace.marketplace,
                                                   time_request=time_request)
                    break
                except IntegrityError:
                    time.sleep(5)
            else:
                raise Exception('Ошибка параллельных запросов')

            return time_request

        def check_login(r: int = 1):
            """
                Проверка на удачный вход в ЛК

                Параметры:
                    r(int, optional): количество проверок
            """
            self.add_overlay()
            for _ in range(r):
                time.sleep(TIME_AWAIT)
                self.driver.get(marketplace.domain)
                if marketplace.domain in self.driver.current_url:
                    logger.info(user=self.user, proxy=self.proxy,
                                description=f"{self.log_startswith}Вход в ЛК выполнен")
                    return
            else:
                logger.info(user=self.user, proxy=self.proxy,
                            description=f"{self.log_startswith}Автоматизация завершена, вход не подтверждён")

        def email_code(tr: datetime.datetime):
            """
                Проверка кода на email и ввод кода

                Параметры:
                    tr(datetime.datetime): время запроса
            """
            # Получаем письмо с кодом (до 20 попыток с паузами)
            self.add_overlay()
            mail_client = YandexMailClient(mail=self.mail, token=self.token, db_conn=self.db_conn)
            exception = None
            for _ in range(20):
                try:
                    mail_client.connect()
                    mail_client.fetch_emails(user=self.user, phone=self.phone, time_request=tr)
                    break
                except Exception as e:
                    time.sleep(TIME_AWAIT)
                    exception = e
                    continue
                finally:
                    mail_client.close()
            else:
                raise Exception(exception)

            # Получаем код подтверждения из базы
            mes = self.db_conn.get_phone_message(user=self.user, phone=self.phone, marketplace=marketplace.marketplace)
            logger.info(user=self.user, proxy=self.proxy,
                        description=f"{self.log_startswith}Код на Email {self.mail} получен: {mes}")
            logger.info(user=self.user, proxy=self.proxy,
                        description=f"{self.log_startswith}Ввод кода {mes}")

            # Вводим код
            try:
                time.sleep(TIME_AWAIT)
                input_code = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable((By.CSS_SELECTOR, "input[type='text']")))
                self.remove_overlay()
                input_code.send_keys(mes)
                self.add_overlay()

            except TimeoutException:
                raise Exception('Отсутствует поле ввода email кода')

        def phone_code(tr: datetime.datetime):
            """
                Проверка кода на номер и ввод кода

                Параметры:
                    tr(datetime.datetime): время запроса
            """
            self.add_overlay()
            # Получаем код подтверждения из базы
            mes = self.db_conn.get_phone_message(user=self.user, phone=self.phone, marketplace=marketplace.marketplace)
            logger.info(user=self.user, proxy=self.proxy,
                        description=f"{self.log_startswith}Код на номер {self.phone} получен: {mes}")
            logger.info(user=self.user, proxy=self.proxy, description=f"{self.log_startswith}Ввод кода {mes}")

            # Вводим код
            try:
                time.sleep(TIME_AWAIT)
                input_code = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable((By.CSS_SELECTOR, "input[type='text']")))

                self.remove_overlay()
                input_code.send_keys(mes)
                self.add_overlay()
            except TimeoutException:
                raise Exception('Отсутствует поле ввода Phone кода')

        def selection_func():
            """
                Выбирается функция в зависимости от запроса

                Результат:
                    функция выполнения
            """
            self.add_overlay()
            phone_text = self.phone[-4:][:2] + ' ' + self.phone[-4:][2:]
            for _ in range(3):
                try:
                    select_func = None
                    time.sleep(TIME_AWAIT)
                    spans = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                        expected_conditions.presence_of_all_elements_located((By.TAG_NAME, 'span')))
                    for span in spans:
                        if self.mail in span.text.lower():
                            select_func = email_code
                        elif phone_text in span.text.lower():
                            select_func = phone_code
                        if select_func:
                            return select_func
                except TimeoutException:
                    self.driver.refresh()
            else:
                raise Exception('Страница не получена')

        # Если пользователь уже авторизован — сразу переходим в ЛК
        with suppress(TimeoutException):
            h2 = WebDriverWait(self.driver, TIME_AWAIT).until(expected_conditions.presence_of_element_located((
                By.XPATH, "//h2[@qa-id='ozonIdCredentialSettingsTitle']")))
            if h2:
                self.driver.get(marketplace.domain)
                logger.info(user=self.user, proxy=self.proxy,
                            description=f"{self.log_startswith}Вход в ЛК выполнен")
                return

        logger.info(user=self.user, proxy=self.proxy, description=f"{self.log_startswith}Ввод почты {self.mail}")

        # Пытаемся найти форму ввода email (до 3 попыток)
        for _ in range(3):
            try:
                time.sleep(TIME_AWAIT)
                button_mail = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.presence_of_all_elements_located((By.CSS_SELECTOR, '.content button')))[-2]

                self.remove_overlay()
                button_mail.click()
                self.add_overlay()

                time.sleep(TIME_AWAIT)
                input_mail = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable((By.ID, "email")))
                input_mail.send_keys(Keys.CONTROL, 'a')

                self.remove_overlay()
                input_mail.send_keys(Keys.DELETE)
                input_mail.send_keys(self.mail)
                self.add_overlay()

                time.sleep(TIME_AWAIT)
                button_push = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.presence_of_all_elements_located((By.CSS_SELECTOR, '.content button')))[-3]
                break
            except (TimeoutException, IndexError):
                self.driver.refresh()
        else:
            raise Exception('Страница не получена')

        time_request = create_phone_message(button_push)

        func = selection_func()

        func(tr=time_request)

        logger.info(user=self.user, proxy=self.proxy, description=f"{self.log_startswith}Вход в ЛК")

        # Пытаемся найти форму 'Что-то пошло не так'
        with suppress(TimeoutException):
            error_title = WebDriverWait(self.driver, TIME_AWAIT * 2).until(
                expected_conditions.visibility_of_element_located(
                    (By.XPATH, "//*[contains(text(), 'Что-то пошло не так')]")))

            if error_title:
                self.driver.get(marketplace.domain)

        # Проверка: загрузился ли личный кабинет
        time.sleep(TIME_AWAIT)
        with suppress(TimeoutException):
            WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                expected_conditions.presence_of_element_located((By.CLASS_NAME, 'csma-ozon-id-page')))
            check_login()
            return

        # Если вход не завершён
        try:
            button_push2 = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                expected_conditions.presence_of_all_elements_located((By.CSS_SELECTOR, '.content button')))[-2]
        except (TimeoutException, IndexError) as e:
            raise Exception(f'Нет кнопки подтверждения телефона. {e}')

        time_request = create_phone_message(button_push2)

        func = selection_func()

        func(tr=time_request)

        logger.info(user=self.user, proxy=self.proxy, description=f"{self.log_startswith}Вход в ЛК")

        # Финальная проверка перехода в ЛК
        check_login(4)

    def ya_auth(self, marketplace: Market) -> None:
        """Авторизация в Яндекс.Маркет. Используется логин, пароль и код подтверждения по SMS"""

        def check_login() -> bool:
            if 'https://id.yandex.ru' in self.driver.current_url:
                self.driver.get(f'{self.marketplace.domain}/{self.client_id}/marketplace')
                logger.info(user=self.user,proxy=self.proxy,
                    description=f"{self.log_startswith}Вход в ЛК выполнен")
                return True
            return False

        def confirm_phone_challenge() -> bool:
            """
            Нажимает кнопку 'Подтвердить' на экране подтверждения входа,
            создаёт заявку на SMS и запускает ввод кода.

            Возвращает:
                True  - если шаг подтверждения выполнен
                False - если экран подтверждения не найден
            """

            with suppress(TimeoutException):
                # Ожидаем кнопку подтверждения входа
                button_enter = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable(
                        (By.XPATH, "//button[.//span[contains(text(), 'Подтвердить')]]")))

                logger.info(user=self.user,proxy=self.proxy,
                    description=f"{self.log_startswith}Проверка заявки на СМС на номер {self.phone}")

                # Отмечаем время начала запроса кода
                time_request = get_moscow_time()

                # Проверка на незавершённую авторизацию с этим номером
                self.db_conn.check_phone_message(user=self.user,phone=self.phone,time_request=time_request)

                self.remove_overlay()
                button_enter.click()
                self.add_overlay()

                enter(time_request)
                return True

            return False

        def enter(tr):
            logger.info(user=self.user, proxy=self.proxy,
                        description=f"{self.log_startswith}Ожидание кода на номер {self.phone}")

            # Добавляем заявку на получение кода (до 3 раз при конфликте)
            for _ in range(3):
                try:
                    self.db_conn.add_phone_message(user=self.user,
                                                   phone=self.phone,
                                                   marketplace=marketplace.marketplace,
                                                   time_request=tr)
                    break
                except IntegrityError:
                    time.sleep(TIME_AWAIT)
            else:
                raise Exception('Ошибка параллельных запросов')

            # Получаем код подтверждения из базы
            mes = self.db_conn.get_phone_message(user=self.user, phone=self.phone, marketplace=marketplace.marketplace)
            logger.info(user=self.user, proxy=self.proxy,
                        description=f"{self.log_startswith}Код на номер {self.phone} получен: {mes}")
            logger.info(user=self.user, proxy=self.proxy,
                        description=f"{self.log_startswith}Ввод кода {mes}")
            try:
                time.sleep(TIME_AWAIT)

                try:
                    # Старый вариант: одно поле ввода
                    input_code = WebDriverWait(self.driver, TIME_AWAIT * 2).until(
                        expected_conditions.element_to_be_clickable(
                            (By.XPATH, "//input[@data-testid='field:input-phoneCode']")))

                    self.remove_overlay()
                    input_code.send_keys(mes)
                    self.add_overlay()

                except TimeoutException:
                    # Новый вариант: код разбит на несколько полей
                    code_inputs = WebDriverWait(self.driver, TIME_AWAIT * 2).until(
                        expected_conditions.presence_of_all_elements_located(
                            (By.CSS_SELECTOR, "input[data-testid='code-field-segment']")))

                    if not code_inputs:
                        raise TimeoutException()

                    self.remove_overlay()
                    for input_el, digit in zip(code_inputs, mes):
                        input_el.send_keys(digit)
                    self.add_overlay()

            except TimeoutException:
                raise Exception('Отсутствует поле ввода кода')

            logger.info(user=self.user, proxy=self.proxy, description=f"{self.log_startswith}Вход в ЛК")

            # Проверка перехода в ЛК
            for _ in range(4):
                self.add_overlay()
                time.sleep(TIME_AWAIT)
                if check_login():
                    return
            else:
                logger.info(user=self.user, proxy=self.proxy,
                            description=f"{self.log_startswith}Автоматизация завершена, вход не подтверждён")



        def re_login() -> bool:
            """Проверка сценария с уже выбранным аккаунтом Яндекса"""

            with suppress(TimeoutException, NoSuchElementException):
                self.add_overlay()
                time.sleep(TIME_AWAIT)

                current_account = WebDriverWait(self.driver, TIME_AWAIT * 2).until(
                    expected_conditions.element_to_be_clickable(
                        (By.CSS_SELECTOR, ".UserLogin.AuthListReturnBtn-user-login")))

                account_login = current_account.find_element(
                    By.CSS_SELECTOR, ".UserLogin-loginElement"
                ).text.strip().lower()

                if account_login != self.mail.split('@')[0].lower():
                    return False

                logger.info(user=self.user,proxy=self.proxy,
                    description=f"{self.log_startswith}Найден аккаунт {account_login}, вводим пароль")

                input_pass = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable(
                        (By.XPATH, "//input[@data-testid='text-field-input']")))

                self.remove_overlay()
                input_pass.send_keys(self.pass_mail)
                self.add_overlay()

                button_enter_pass = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable(
                        (By.XPATH, "//button[@data-testid='password-next']")))

                self.remove_overlay()
                button_enter_pass.click()
                self.add_overlay()

                time.sleep(TIME_AWAIT)

                if check_login():
                    return True
                if confirm_phone_challenge():
                    return True

                return False

            return False

        def login_by_mail() -> bool:
            """
            Сценарий входа через 'Ещё' -> вход по логину -> почта/пароль.

            Возвращает:
                True  - если сценарий выполнен
                False - если элементы сценария не найдены
            """

            # Нажимаем кнопку «Ещё», чтобы выбрать вход по логину
            with suppress(TimeoutException):
                time.sleep(TIME_AWAIT)

                # Если уже авторизован — переходим в ЛК
                if 'https://id.yandex.ru' in self.driver.current_url:
                    self.driver.get(f'{self.marketplace.domain}/{self.client_id}/marketplace')
                    logger.info(user=self.user,proxy=self.proxy,
                                description=f"{self.log_startswith}Вход в ЛК выполнен")
                    return True

                button_more = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable(
                        (By.XPATH, "//button[@data-testid='split-add-user-more-button']")))

                self.remove_overlay()
                button_more.click()
                self.add_overlay()

                time.sleep(TIME_AWAIT)

                button_by_login = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.visibility_of_element_located(
                        (By.XPATH, "//div[@data-testid='menu-option-switchToLogin']")))

                self.remove_overlay()
                button_by_login.click()
                self.add_overlay()

            logger.info(user=self.user,proxy=self.proxy,description=f"{self.log_startswith}Ввод почты {self.mail}")

            # Ввод логина (email)
            with suppress(TimeoutException):
                time.sleep(TIME_AWAIT)

                input_mail = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable(
                        (By.XPATH, "//input[@data-testid='text-field-input']")))

                self.remove_overlay()
                input_mail.send_keys(self.mail)
                self.add_overlay()

                time.sleep(TIME_AWAIT)

                button_enter_mail = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable(
                        (By.XPATH, "//button[@data-testid='split-add-user-next-login']")))

                # Отмечаем время начала запроса кода
                time_request = get_moscow_time()

                # Проверка на незавершённую авторизацию с этим номером
                self.db_conn.check_phone_message(user=self.user,phone=self.phone,time_request=time_request)

                self.remove_overlay()
                button_enter_mail.click()
                self.add_overlay()

                # Если сразу появилось поле SMS-кода
                with suppress(TimeoutException):
                    time.sleep(TIME_AWAIT)

                    WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                        expected_conditions.element_to_be_clickable(
                            (By.XPATH, "//input[@data-testid='field:input-phoneCode']")))

                    self.remove_overlay()
                    enter(time_request)
                    self.add_overlay()
                    return True

            logger.info(user=self.user,proxy=self.proxy,description=f"{self.log_startswith}Ввод пароля от {self.mail}")

            self.add_overlay()

            # Ввод пароля от почты
            with suppress(TimeoutException):
                time.sleep(TIME_AWAIT)

                input_pass = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable(
                        (By.XPATH, "//input[@data-testid='text-field-input']")))

                self.remove_overlay()
                input_pass.send_keys(self.pass_mail)
                self.add_overlay()

                button_enter_pass = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable(
                        (By.XPATH, "//button[@data-testid='password-next']")))

                self.remove_overlay()
                button_enter_pass.click()
                self.add_overlay()

                time.sleep(TIME_AWAIT)

                # Если уже авторизован — переходим в ЛК
                if check_login():
                    return True

                if confirm_phone_challenge():
                    return True

            return False

        for _ in range(3):
            if re_login():
                return

            if login_by_mail():
                return
        else:
            raise Exception('Страница не получена')

    def mvideo_auth(self, marketplace: Market) -> bool | None:
        """Авторизация в МВидео по номеру телефона"""

        def check_login() -> bool:
            if 'https://sellers.mvideo.ru/mpa' in self.driver.current_url:
                self.driver.get(f'{self.marketplace.domain}/{self.client_id}/marketplace')
                logger.info(user=self.user,proxy=self.proxy,
                    description=f"{self.log_startswith}Вход в ЛК выполнен")
                return True
            return False

        def enter(tr):
            self.add_overlay()

            logger.info(user=self.user,proxy=self.proxy,
                        description=f"{self.log_startswith}Ожидание кода на номер {self.phone}")

            # Добавляем заявку на получение кода
            for _ in range(3):
                try:
                    self.db_conn.add_phone_message( user=self.user,
                                                    phone=self.phone,
                                                    marketplace=marketplace.marketplace,
                                                    time_request=tr)
                    break
                except IntegrityError:
                    time.sleep(TIME_AWAIT)
            else:
                raise Exception('Ошибка параллельных запросов')

            # Получаем код подтверждения из базы
            mes = self.db_conn.get_phone_message(user=self.user,phone=self.phone,
                                                 marketplace=marketplace.marketplace)

            # Оставляем только цифры
            mes = ''.join(ch for ch in mes if ch.isdigit())

            logger.info(user=self.user,proxy=self.proxy,
                        description=f"{self.log_startswith}Код на номер {self.phone} получен: {mes}")
            logger.info(user=self.user,proxy=self.proxy,
                        description=f"{self.log_startswith}Ввод кода {mes}")

            check_login()
            with suppress(TimeoutException):
                time.sleep(TIME_AWAIT)

                input_code = WebDriverWait(self.driver, TIME_AWAIT * 2).until(
                    expected_conditions.element_to_be_clickable(
                        (By.CSS_SELECTOR, "mpa-ui-input[formcontrolname='code'] input")))

                self.remove_overlay()
                input_code.send_keys(mes)
                self.add_overlay()

                button_confirm = WebDriverWait(self.driver, TIME_AWAIT * 2).until(
                    expected_conditions.element_to_be_clickable(
                        (By.XPATH, "//button[contains(., 'Подтвердить')]")))

                logger.info(user=self.user,proxy=self.proxy,
                            description=f"{self.log_startswith} Нажимаем на кнопку подтвердить ")

                self.remove_overlay()
                button_confirm.click()


                return
            raise Exception('Отсутствует поле ввода кода или кнопка подтверждения')

        for _ in range(3):
            try:
                self.add_overlay()
                time.sleep(TIME_AWAIT)
                logger.info(user=self.user,proxy=self.proxy,
                            description=f"{self.log_startswith}Ввод номера телефона {self.phone}")

                # Поле телефона
                input_phone = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable(
                        (By.CSS_SELECTOR, "input[name='phone']")))

                self.remove_overlay()
                input_phone.clear()
                input_phone.send_keys(self.phone)
                self.add_overlay()

                logger.info(user=self.user,proxy=self.proxy,
                            description=f"{self.log_startswith}Нажимаем кнопку Войти")

                # Отмечаем время начала запроса кода
                time_request = get_moscow_time()

                # Кнопка "Войти"
                button_login = WebDriverWait(self.driver, TIME_AWAIT * 4).until(
                    expected_conditions.element_to_be_clickable(
                        (By.XPATH, "//button[contains(., 'Войти')]")))

                # Проверка на незавершённую авторизацию с этим номером
                self.db_conn.check_phone_message(user=self.user,phone=self.phone,time_request=time_request)

                self.remove_overlay()
                button_login.click()
                self.add_overlay()

                logger.info(user=self.user,proxy=self.proxy,
                            description=f"{self.log_startswith}Номер телефона введён, кнопка Войти нажата")

                # Переходим к ожиданию и вводу кода
                enter(time_request)
                return

            except TimeoutException:
                logger.info(user=self.user,proxy=self.proxy,
                    description=f"{self.log_startswith}Не удалось найти поле телефона или кнопку Войти, повторная попытка")

        raise Exception('Страница не получена')

    def add_overlay(self) -> None:
        """
        Добавляет полупрозрачный затемняющий слой (оверлей) поверх страницы в браузере,
        чтобы заблокировать действия пользователя во время автоматизации.
        Отображает название компании и сообщение: "Идёт авторизация...".
        """
        pass
        self.driver.execute_script(f"""
            (function () {{
                if (document.getElementById('block-overlay')) return;

                const overlay = document.createElement('div');
                overlay.id = 'block-overlay';
                Object.assign(overlay.style, {{
                    position: 'fixed',
                    top: '0',
                    left: '0',
                    width: '100%',
                    height: '100%',
                    backgroundColor: 'rgba(0, 0, 0, 0.5)',
                    zIndex: '2147483647',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    color: 'white',
                    fontSize: '24px',
                    fontFamily: 'Arial, sans-serif'
                }});
                overlay.innerText = 'Идёт авторизация, пожалуйста, подождите. Кабинет {self.name_company}.';

                const parent = document.body || document.documentElement;
                if (!parent) return;

                parent.appendChild(overlay);
            }})();
        """)

    def remove_overlay(self) -> None:
        """
        Удаляет оверлей с экрана браузера, восстанавливая возможность взаимодействия со страницей.
        Используется после завершения автоматизации или при ошибке.
        """
        pass
        self.driver.execute_script("""
            (function () {
                const overlay = document.getElementById('block-overlay');
                if (overlay && overlay.parentNode) {
                    overlay.parentNode.removeChild(overlay);
                }
            })();
        """)

    def is_browser_active(self) -> bool:
        """Проверяет, активен ли браузер. Возвращает True, если браузер всё ещё работает, иначе False"""

        try:
            if self.driver.session_id is None:
                return False
            if not self.driver.service.is_connectable():
                return False
            return bool(self.driver.current_url)
        except (NoSuchWindowException, InvalidSessionIdException, WebDriverException):
            return False

    def load_url(self, url: str) -> None:
        """
        Загружает указанный URL в браузер и, если включена автоавторизация,
        запускает процесс входа. Для Яндекса вручную переходит по клиентскому пути.
        """

        logger.info(user=self.user, proxy=self.proxy, description=f"{self.log_startswith}Браузер открыт")
        if self.auto:
            logger.info(user=self.user, proxy=self.proxy, description=f"{self.log_startswith}Авторизация")
            if self.marketplace.marketplace == 'Ozon':
                self.driver.get('https://sso.ozon.ru/auth')
            else:
                self.driver.get(url)
            self.add_overlay()
            self.check_auth()
        else:
            if self.marketplace.marketplace == 'Yandex':
                self.driver.get(f'{self.marketplace.domain}/{self.client_id}/marketplace')
            else:
                self.driver.get(self.marketplace.domain)

    def quit(self, text: str = None) -> None:
        """
        Завершает сессию браузера.

        Если передан текст ошибки — логирует как ошибку и выбрасывает исключение AuthException.
        В противном случае — просто закрывает браузер и логирует завершение.
        """

        if text:
            logger.error(user=self.user, proxy=self.proxy,
                         description=f"{self.log_startswith}Ошибка автоматизации: {text}")
            self.driver.quit()
            raise AuthException(f"{text}\n\nПопробуйте позднее")
        else:
            logger.info(user=self.user, proxy=self.proxy, description=f"{self.log_startswith}Браузер закрыт")
            self.driver.quit()
