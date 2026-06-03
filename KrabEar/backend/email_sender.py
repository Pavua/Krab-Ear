"""EmailSender — отправка email из Krab Ear через SMTP или macOS Mail.app.

Поддерживает два бэкенда:
- smtp   : smtplib (SMTP/STARTTLS/SSL), конфигурация через settings.
- mail_app: macOS Mail.app через osascript (fallback, не требует SMTP).

Пароль SMTP считывается из macOS Keychain (ключ «KrabEar SMTP password»).
Если Keychain недоступен, принимается пустой пароль.
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
import subprocess
import textwrap
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("KrabEar.Backend.EmailSender")

# Имя ключа в macOS Keychain для пароля SMTP
_KEYCHAIN_SERVICE = "KrabEar SMTP password"

# Максимальный размер HTML-тела перед стриппингом (защита от ReDoS на <[^>]+>).
# 200 000 символов ≈ 200 KB — достаточно для любого реального дайджеста.
_STRIP_HTML_MAX_BYTES = 200_000

# Минимальный regex для валидации email-адреса получателя (W1764).
# Отклоняет пустую строку, адреса начинающиеся с «-» (флаги osascript),
# пробельные символы и addr без «@» + домена с точкой.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _get_smtp_password_from_keychain(account: str) -> str:
    """Читает пароль SMTP из macOS Keychain через security(1).

    Возвращает пустую строку при любой ошибке (Keychain недоступен, ключ
    не найден, нет разрешения).
    """
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s", _KEYCHAIN_SERVICE,
                "-a", account,
                "-w",  # вывести только пароль
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as exc:
        logger.debug("Keychain unavailable: %s", exc)
    return ""


def _set_smtp_password_in_keychain(account: str, password: str) -> bool:
    """Сохраняет пароль SMTP в macOS Keychain.

    Возвращает True при успехе, False при ошибке.
    """
    try:
        result = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-s", _KEYCHAIN_SERVICE,
                "-a", account,
                "-w", password,
                "-U",  # обновить если существует
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception as exc:
        logger.debug("Keychain write error: %s", exc)
        return False


class EmailSender:
    """Универсальный отправщик email для Krab Ear.

    Выбор бэкенда: backend_name="smtp" или "mail_app".

    Конфигурация SMTP::

        sender = EmailSender(
            backend_name="smtp",
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_user="user@gmail.com",
            smtp_use_tls=True,
            # Пароль берётся из Keychain для smtp_user; при необходимости
            # его можно передать явно через smtp_password=...
        )

    Mail.app не требует SMTP-конфигурации, но зависит от настроенных в macOS
    учётных записей Mail.app и может открыть окно приложения.
    """

    def __init__(
        self,
        backend_name: str = "smtp",
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_user: str = "",
        smtp_password: str = "",
        smtp_use_tls: bool = True,
        smtp_use_ssl: bool = False,
        smtp_from: str = "",
        use_keychain: bool = True,
        smtp_tls_insecure: bool = False,
    ) -> None:
        """
        Args:
            backend_name: "smtp" или "mail_app".
            smtp_host: SMTP-сервер (например smtp.gmail.com).
            smtp_port: TCP-порт (587 для STARTTLS, 465 для SSL, 25 для plain).
            smtp_user: Логин SMTP-аккаунта (и адрес From если smtp_from пуст).
            smtp_password: Пароль SMTP. Если пусто и use_keychain=True —
                берётся из Keychain.
            smtp_use_tls: Использовать STARTTLS (по умолчанию True, порт 587).
            smtp_use_ssl: Использовать SMTP_SSL (порт 465).
            smtp_from: Адрес From. Если пуст — используется smtp_user.
            use_keychain: Искать пароль в macOS Keychain если smtp_password пуст.
            smtp_tls_insecure: Явный opt-out из верификации TLS-сертификата
                (CERT_NONE + check_hostname=False). По умолчанию False — все
                соединения используют ssl.create_default_context() (CERT_REQUIRED
                + проверка hostname через системный trust store). Устанавливайте
                True ТОЛЬКО для локальных тестовых серверов без реального TLS;
                никогда не включайте в production — открывает MITM-уязвимость.
        """
        self.backend_name = backend_name
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self._smtp_password = smtp_password
        self.smtp_use_tls = smtp_use_tls
        self.smtp_use_ssl = smtp_use_ssl
        self.smtp_from = smtp_from or smtp_user
        self.use_keychain = use_keychain
        self.smtp_tls_insecure = smtp_tls_insecure

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(
        self,
        to: str,
        subject: str,
        body_html: str,
        body_text: str = "",
    ) -> None:
        """Отправляет письмо выбранным бэкендом.

        Args:
            to: Адрес получателя.
            subject: Тема письма.
            body_html: HTML-тело письма.
            body_text: Текстовая альтернатива (plain-text). Если пуст —
                генерируется заглушка.

        Raises:
            ValueError: Если адрес получателя невалиден (пуст, содержит пробелы,
                начинается с «-», нет «@» или домена с точкой).
            RuntimeError: Если отправка не удалась.
        """
        if not to:
            raise ValueError("Адрес получателя не указан")
        # W1764: валидация получателя перед передачей в osascript/smtplib.
        # Отклоняем любые значения начинающиеся с «-» (osascript FLAG injection),
        # содержащие пробелы или не соответствующие минимальному email-шаблону.
        if not _EMAIL_RE.match(to):
            raise ValueError(
                f"Невалидный адрес получателя: {to!r}. "
                "Ожидается формат user@domain.tld (без пробелов и ведущего «-»)."
            )
        if not subject:
            raise ValueError("Тема письма не указана")

        if self.backend_name == "mail_app":
            self._send_via_mail_app(to=to, subject=subject, body_html=body_html)
        else:
            self._send_via_smtp(
                to=to,
                subject=subject,
                body_html=body_html,
                body_text=body_text or self._strip_html(body_html),
            )

    # ------------------------------------------------------------------
    # Keychain helpers (public для тестов)
    # ------------------------------------------------------------------

    def get_smtp_password(self) -> str:
        """Возвращает пароль: из конструктора или Keychain."""
        if self._smtp_password:
            return self._smtp_password
        if self.use_keychain and self.smtp_user:
            return _get_smtp_password_from_keychain(self.smtp_user)
        return ""

    def save_smtp_password(self, password: str) -> bool:
        """Сохраняет пароль SMTP в Keychain и обновляет текущий экземпляр."""
        self._smtp_password = password
        if self.smtp_user:
            return _set_smtp_password_in_keychain(self.smtp_user, password)
        return False

    # ------------------------------------------------------------------
    # SMTP backend
    # ------------------------------------------------------------------

    def _send_via_smtp(
        self,
        to: str,
        subject: str,
        body_html: str,
        body_text: str,
    ) -> None:
        """Отправляет письмо через smtplib."""
        if not self.smtp_host:
            raise RuntimeError(
                "SMTP-сервер не настроен. Укажите smtp_host или переключитесь на mail_app."
            )

        password = self.get_smtp_password()

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.smtp_from
        msg["To"] = to
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        # Build TLS context: default = CERT_REQUIRED + check_hostname via system trust store.
        # Opt-out (smtp_tls_insecure=True) disables verification — local test servers only;
        # never enable in production (MITM risk: captures login password + email body).
        if self.smtp_tls_insecure:
            logger.warning(
                "SMTP TLS cert verification DISABLED (smtp_tls_insecure=True) — "
                "MITM risk; do NOT use in production",
                extra={"event": "email.tls.insecure_mode", "host": self.smtp_host},
            )
            ssl_ctx: ssl.SSLContext = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        else:
            ssl_ctx = ssl.create_default_context()  # CERT_REQUIRED + check_hostname=True

        try:
            if self.smtp_use_ssl:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=ssl_ctx) as server:
                    if self.smtp_user and password:
                        server.login(self.smtp_user, password)
                    server.sendmail(self.smtp_from, [to], msg.as_string())
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                    if self.smtp_use_tls:
                        server.starttls(context=ssl_ctx)
                    if self.smtp_user and password:
                        server.login(self.smtp_user, password)
                    server.sendmail(self.smtp_from, [to], msg.as_string())
        except smtplib.SMTPException as exc:
            raise RuntimeError(f"SMTP ошибка: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"Сетевая ошибка SMTP: {exc}") from exc

        logger.info(
            "Email отправлен через SMTP: to=%s subject=%r host=%s",
            to, subject, self.smtp_host,
        )

    # ------------------------------------------------------------------
    # Mail.app backend (osascript)
    # ------------------------------------------------------------------

    def _send_via_mail_app(
        self,
        to: str,
        subject: str,
        body_html: str,
    ) -> None:
        """Отправляет письмо через macOS Mail.app (osascript).

        Создаёт письмо и сразу отправляет его через настроенный аккаунт Mail.app.
        Требует настроенных учётных записей в Mail.app; может открыть окно Mail.app.

        Безопасность: to/subject/body передаются как argv-аргументы osascript,
        а не интерполируются в текст скрипта, что предотвращает AppleScript-инъекцию
        через transcript-derived данные (W1747).

        W1764: добавлен разделитель «--» между флагами osascript и позиционными
        аргументами — osascript(1) использует getopt(3) и трактует любой токен
        начинающийся с «-» как флаг ПОКА не встретит «--». Без разделителя
        значение to="-e" превратилось бы во второй флаг -e (второй скрипт),
        а subject интерпретировался бы как AppleScript-код → silent DoS.
        """
        # Mail.app принимает plain-text; конвертируем HTML в текст для простоты
        plain_body = self._strip_html(body_html)

        # Значения передаются как отдельные argv-аргументы (item 1/2/3 of argv),
        # а не интерполируются в строковые литералы AppleScript — инъекция невозможна.
        script = textwrap.dedent('''\
            on run argv
                set theTo to item 1 of argv
                set theSubject to item 2 of argv
                set theBody to item 3 of argv
                tell application "Mail"
                    set newMessage to make new outgoing message with properties \\
                        {subject:theSubject, content:theBody, visible:false}
                    tell newMessage
                        make new to recipient with properties {address:theTo}
                    end tell
                    send newMessage
                end tell
            end run
        ''')

        try:
            # «--» завершает флаги osascript; всё после — позиционные argv-аргументы
            # скрипта (on run argv). Без «--» значение вида «-e» поглощалось бы
            # getopt как второй флаг -e, а subject стал бы вторым скриптом (W1764).
            result = subprocess.run(
                ["osascript", "-e", script, "--", to, subject, plain_body],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"osascript завершился с кодом {result.returncode}: {result.stderr.strip()}"
                )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("osascript завис (timeout 30s)") from exc
        except FileNotFoundError as exc:
            raise RuntimeError("osascript не найден (не macOS?)") from exc

        logger.info("Email отправлен через Mail.app: to=%s subject=%r", to, subject)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(html: str) -> str:
        """Грубо удаляет HTML-теги для получения plain-text fallback.

        W1764 (ReDoS): паттерн <[^>]+> имеет квадратичную сложность на входе
        с множеством «<» без закрывающего «>» (58s на 1 MB в тесте).

        Двухуровневая защита:
        1. Обрезаем вход до _STRIP_HTML_MAX_BYTES.
        2. Ограничиваем длину захватываемого тега до 2000 символов
           (<[^>]{0,2000}>): ни один реальный HTML-тег не длиннее нескольких
           сотен символов; это исключает квадратичное backtracking на «<<<<...».
        """
        if len(html) > _STRIP_HTML_MAX_BYTES:
            logger.warning(
                "HTML-тело письма обрезано до %d символов перед strip_html (было %d)",
                _STRIP_HTML_MAX_BYTES, len(html),
                extra={"event": "email.strip_html.truncated",
                       "original_len": len(html), "limit": _STRIP_HTML_MAX_BYTES},
            )
            html = html[:_STRIP_HTML_MAX_BYTES]
        text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        # Bounded quantifier {0,2000} предотвращает O(n²) backtracking на
        # последовательностях «<» без закрывающего «>» (W1764 ReDoS).
        text = re.sub(r"<[^>]{0,2000}>", "", text)
        # Убираем множественные пробелы/строки
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # ------------------------------------------------------------------
    # Конфигурация из объекта settings
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls, cfg: object) -> "EmailSender":
        """Создаёт EmailSender на основе атрибутов объекта settings.

        Ожидаемые атрибуты (все опциональны):
            RECAP_BACKEND, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
            SMTP_USE_TLS, SMTP_USE_SSL.
        """
        backend = getattr(cfg, "RECAP_BACKEND", "smtp")
        return cls(
            backend_name=backend,
            smtp_host=getattr(cfg, "SMTP_HOST", ""),
            smtp_port=int(getattr(cfg, "SMTP_PORT", 587)),
            smtp_user=getattr(cfg, "SMTP_USER", ""),
            smtp_password=getattr(cfg, "SMTP_PASSWORD", ""),
            smtp_use_tls=bool(getattr(cfg, "SMTP_USE_TLS", True)),
            smtp_use_ssl=bool(getattr(cfg, "SMTP_USE_SSL", False)),
            smtp_tls_insecure=bool(getattr(cfg, "SMTP_TLS_INSECURE", False)),
        )
