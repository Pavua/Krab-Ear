# Конфигурация gunicorn для production-запуска REST API Krab Ear.
# Использование: gunicorn -c KrabEar/gunicorn_config.py "backend.rest_server:create_app()"

bind = "127.0.0.1:5005"

# 2 воркера — оптимально для локального macOS-использования
workers = 2

# REST watchdog ждёт транскрайбер до 600 секунд. Сверху оставляем штатный
# graceful_timeout (30 секунд) и ещё 30 секунд на WSGI response-close.
timeout = 660

# Логи в stdout/stderr (для launchd и консольного вывода)
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Graceful shutdown — дать воркерам завершить текущие запросы
graceful_timeout = 30

# Загрузить приложение (и ML-модели) до форка воркеров — экономит RAM
preload_app = True
