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
import subprocess
import textwrap
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("KrabEar.Backend.EmailSender")

# Имя ключа в macOS Keychain для пароля SMTP
_KEYCHAIN_SERVICE = "KrabEar SMTP password"


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
            RuntimeError: Если отправка не удалась.
        """
        if not to:
            raise ValueError("Адрес получателя не указан")
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

        try:
            if self.smtp_use_ssl:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port) as server:
                    if self.smtp_user and password:
                        server.login(self.smtp_user, password)
                    server.sendmail(self.smtp_from, [to], msg.as_string())
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                    if self.smtp_use_tls:
                        server.starttls()
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
        """
        escaped_to = to.replace('"', '\\"')
        escaped_subject = subject.replace('"', '\\"').replace("\\n", " ")
        # Mail.app принимает plain-text; конвертируем HTML в текст для простоты
        plain_body = self._strip_html(body_html)
        escaped_body = plain_body.replace('"', '\\"').replace("\n", "\\n")

        script = textwrap.dedent(f'''\
            tell application "Mail"
                set newMessage to make new outgoing message with properties \\
                    {{subject:"{escaped_subject}", content:"{escaped_body}", \\
                    visible:false}}
                tell newMessage
                    make new to recipient with properties {{address:"{escaped_to}"}}
                end tell
                send newMessage
            end tell
        ''')

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
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
        """Грубо удаляет HTML-теги для получения plain-text fallback."""
        text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
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
        )
