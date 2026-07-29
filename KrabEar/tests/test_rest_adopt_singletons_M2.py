"""M2: усыновление внешних зависимостей вместо второго комплекта.

Корень: backend импортирует rest_server ради create_app(). Импорт создаёт
свой standalone-комплект; владелец процесса подменяет его своими объектами,
иначе в одном процессе живут ДВА AudioEngine/StateStore.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _import_rest_server():
    """Импортирует модуль с подменёнными конструкторами тяжёлых объектов.

    Тот же приём, что в test_rest_server_unit.py: без него импорт поднимает
    настоящий AudioEngine и читает настоящий data_dir.
    """
    with patch("core.engine.AudioEngine", return_value=MagicMock()), \
            patch("backend.state_store.StateStore", return_value=MagicMock()), \
            patch("backend.transcriber.Transcriber", return_value=MagicMock()), \
            patch("backend.translator.Translator", return_value=MagicMock()), \
            patch("backend.tts_service.TTSService", return_value=MagicMock()):
        import backend.rest_server as rs
        return rs


class AdoptExternalSingletonsTest(unittest.TestCase):
    def setUp(self):
        self.rs = _import_rest_server()
        # Запоминаем, что было до подмены, чтобы вернуть после теста:
        # модуль общий на процесс, и утёкшая подмена сломает соседние файлы.
        self._saved = {
            name: getattr(self.rs, name)
            for name in ("engine", "store", "transcriber", "translator", "tts_service")
        }

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.rs, name, value)

    def test_adopt_replaces_all_five_globals(self):
        mine = {k: MagicMock(name=k) for k in
                ("engine", "store", "transcriber", "translator", "tts_service")}
        self.rs.adopt_external_singletons(**mine)
        for name, obj in mine.items():
            self.assertIs(getattr(self.rs, name), obj, name)

    def test_deps_proxy_sees_adopted_objects(self):
        """Главное свойство: обработчики читают зависимости через _deps()."""
        my_store = MagicMock(name="my_store")
        self.rs.adopt_external_singletons(
            engine=MagicMock(), store=my_store, transcriber=MagicMock(),
            translator=MagicMock(), tts_service=MagicMock(),
        )
        self.assertIs(self.rs._MODULE_DEPS.store, my_store)

    def test_previous_objects_become_unreachable(self):
        """Смысл задачи: прежний комплект должен стать недостижим.

        Проверяем не сборку мусора (она недетерминирована), а само условие
        для неё: модуль больше нигде не держит ссылку на старый объект.

        Сравнение — по идентичности (`is`), а не через assertNotIn/`in`:
        rest_server.py на уровне модуля хранит и Flask-прокси (request/g/
        current_app, LocalProxy), чей __eq__ вне контекста запроса кидает
        RuntimeError. `in` пошёл бы через равенство и упал бы на этих
        прокси раньше, чем добрался бы до store — искомая ссылочная
        недостижимость проверяется identity-сравнением напрямую.
        """
        old_store = self.rs.store
        self.rs.adopt_external_singletons(
            engine=MagicMock(), store=MagicMock(), transcriber=MagicMock(),
            translator=MagicMock(), tts_service=MagicMock(),
        )
        module_values = [
            v for k, v in vars(self.rs).items()
            if not k.startswith("__")
        ]
        self.assertTrue(
            all(v is not old_store for v in module_values),
            "старый store всё ещё достижим через module namespace",
        )

    def test_rejects_positional_arguments(self):
        """Пять однотипных объектов подряд обязаны передаваться по имени."""
        with self.assertRaises(TypeError):
            self.rs.adopt_external_singletons(
                MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock()
            )

    def test_rejects_none(self):
        """None вместо объекта — ошибка вызывающего, а не «оставить как было».

        Молчаливое игнорирование None дало бы обработчик, работающий на
        standalone-объекте при уверенности владельца, что подмена случилась.
        """
        with self.assertRaises(ValueError):
            self.rs.adopt_external_singletons(
                engine=MagicMock(), store=None, transcriber=MagicMock(),
                translator=MagicMock(), tts_service=MagicMock(),
            )


if __name__ == "__main__":
    unittest.main()
