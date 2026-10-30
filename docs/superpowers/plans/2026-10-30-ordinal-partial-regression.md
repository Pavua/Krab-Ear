# Консервативная защита частичных порядковых дат

Основа — локальный пакет754c4b81. Проверка базы16552643 против кандидата выявила новое частичное распознавание хвоста составного дня и неразобранного года. Прежние дефекты Number и парсера словесных годов в scope не входят.

CausalRED:4метода/19проверок,13ожидаемыхпадений,6валидныхконтролейPASS. Пять вариантов whitespace:пробел,двапробела,tab,newline,NBSP;три продолжения года:20260,2026xyz,годами. Профиль isolatedstdlib,provider/network/thread-free;процесс завершён и группа пуста. Старый пакет и REDбайты сохранены.

Минимальный repair только для новой цифровой порядковой ветки:сохранятьmatch при предшествующем числовомкомпоненте/неразобранном числовомгоде/окончаниигода внутрислова. Неизвестные составныечислительные не угадывать. NumberNormalizer,порядокэтапов,обычныедаты,конфигурация,runtime не меняются.

Кандидат GREEN35методов/103проверки подготовлен,НЕзапускался. Требуются независимоеreview и отдельныйSTART. Публикация разрешена владельцем,но до закрытия регрессии технически удерживается. CI/parity/fullsuite/runtime остаются отдельными гейтами.

Уточнение после Rootreview:вместо digit-tail guard только самостоятельныйinteger-token `(?<!\S)\d+\s+$`. Добавлено14direct/composed positives для clock/code/decimal/spokenclock переддатой. Сохранены13causalnegatives+6validcontrols. Repair5methods33checks;candidatecombined36methods117checks,НЕзапускался.

RootSOURCE_CLEAR+ONESTART: combined36methods117checksPASS,test0.042s,parent0.209647833s,PID25590exit0/reapedPGIDempty,errors/skips/guards/threads0,8hashstable. Fullraw/receipt personallyread. FINISH/RUN0. Sourcec5627ff3...,fixtureef9c8289...,runner86fb1136... unchanged. FinalindependentAGYdelta reviewpending beforepublicpush;CI/parity separate.
