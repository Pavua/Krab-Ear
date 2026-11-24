"""G3-audio-03 (A03-01) / G3-audio-02 (A02-11): dispatch-гарды смотрят ТОЛЬКО в реальную таблицу.

Раньше `_dispatch_block()` возвращал весь service.py: ключ, оставшийся лишь в постороннем
словаре, засчитывался как зарегистрированный IPC-обработчик — удаление хендлера из
`_build_dispatch_table` не краснело. Здесь закреплён контракт хелпера на синтетическом
исходнике и на реальном service.py.
"""
from __future__ import annotations

import unittest

from test_dispatch_invariants_wave768_critical import (
    _all_dispatch_keys as keys768,
    _dispatch_block as block768,
    _dispatch_rhs as rhs768,
)
from test_dispatch_invariants_wave790_full import (
    _all_dispatch_keys as keys790,
    _dispatch_block as block790,
    _dispatch_rhs as rhs790,
    _read_dispatch_source,
)

_SYNTHETIC = '''
class BackendService:
    def _build_dispatch_table(self):
        return {"health_check": self._handle_health_check,}

    def unrelated_dict(self):
        return {"purge_all_data": self._handle_purge_all_data}
'''


class DispatchGuardScopeTests(unittest.TestCase):
    def test_key_only_in_unrelated_dict_is_not_a_registered_handler(self) -> None:
        for block, keys, rhs in ((block790, keys790, rhs790), (block768, keys768, rhs768)):
            with self.subTest(helper=block.__module__):
                b = block(_SYNTHETIC)
                self.assertIn("health_check", keys(b))
                self.assertNotIn("purge_all_data", keys(b))
                self.assertIsNone(rhs(b, "purge_all_data"))
                self.assertEqual(rhs(b, "health_check"), "svc._handle_health_check")

    def test_real_service_dispatch_block_is_only_the_table_method(self) -> None:
        src = _read_dispatch_source()
        b = block790(src)
        self.assertTrue(b.lstrip().startswith("def _build_dispatch_table"))
        self.assertLess(len(b), len(src) // 2)  # не весь service.py
        self.assertIn("purge_all_data", keys790(b))


if __name__ == "__main__":
    unittest.main()
