# A5.3 — source объединён, 2026-10-08

Подтверждённый результат: семь PR объединены в `codex/krab-ear-v2`;
post-merge CI точного SHA прошёл. Это закрывает source-интеграцию,
а не production или encrypted-history acceptance.

База: `9fee1ef4c2f5540636fd4faae1e53c93142054a5`.
Tree: `0a294fb73efb7a7707177384e665f325fd274d36`.
Снимок доказательств: 2026-10-08 20:12 UTC; свежий fetch основной линии
перед этой документацией подтвердил тот же SHA. Любая следующая source-дельта
требует проверки собственного SHA; этот отчёт не переносит PASS на будущие commits.

## Объединённые PR в порядке применения

| PR | Final head (короткий) | Merge SHA |
| --- | --- | --- |
| [#2076](https://github.com/Pavua/Krab-Ear/pull/2076) | `7a034d8f` | `75668000df5bb695807345c5e885426048ab5f09` |
| [#2075](https://github.com/Pavua/Krab-Ear/pull/2075) | `3a253bc5` | `054e75a8ab073ca7118de0eadb48cc3b28046d34` |
| [#2077](https://github.com/Pavua/Krab-Ear/pull/2077) | `00ba3300` | `4f3f2a9d4a63a70f0259193dda5db76d6e371017` |
| [#2078](https://github.com/Pavua/Krab-Ear/pull/2078) | `2c6a5ed5` | `e85ea4d3c231ccd817122399e7d1cf73b6d21687` |
| [#2079](https://github.com/Pavua/Krab-Ear/pull/2079) | `5c658b16` | `1fa47fffabb616e8a6904a4a97ddbc9fa29852e0` |
| [#2081](https://github.com/Pavua/Krab-Ear/pull/2081) | `bb77159c` | `d679f44f6142290e51641d5d50ba309746b90405` |
| [#2080](https://github.com/Pavua/Krab-Ear/pull/2080) | `eb729db2` | `9fee1ef4c2f5540636fd4faae1e53c93142054a5` |

Назначение: #2076 — спецификация/карточки; #2075 — честный startup log;
#2077–#2079 — authorizer/Python sinks/Swift; #2081 — isolated integration;
#2080 — стабильная signing identity и сохранение совместной source-композиции.
B/C reconciled heads сохраняют проверенные trees исходных heads `d24f001a` /
`886f061a`: изменения ancestry не вводят новые behavior-дельты.

## Проверки и точная граница PASS

- Все семь PR подтверждены как MERGED с указанными heads/merge SHA.
  На каждом head прошли оба backend jobs и не менее 27 pre-merge checks.
- Независимый Astra Ultra gate: **SOURCE COMPOSITION PASS**.
  Final tree совпадает с проверенным кандидатом и signing head `eb729db2`.
  Это source gate, без release/deploy GO.
- Совместный Python 3.12 targeted run: **121 passed, 38 subtests passed**.
  Проверены startup/migrator/CSV/signing, synthetic Unix IPC, малый production
  Swift IPC/coordinator harness и изоляция settings.
- Exact final SHA: [CI 37833572070](https://github.com/Pavua/Krab-Ear/actions/runs/37833572070)
  и [krab-ear-ci 37833572064](https://github.com/Pavua/Krab-Ear/actions/runs/37833572064)
  завершились SUCCESS. 21 check: 19 SUCCESS, 2 Swift SKIPPED по changed-path filter.
  Три Swift build jobs на signing head с тем же tree были SUCCESS до merge.
- Full backend job `113505384858`: checkout final SHA подтверждён по логу;
  1091 test files, все 16 chunks завершены, job SUCCESS.
- Пять собственных worktrees были чистыми после объединения; проверенные foreign
  D WIP сохранились. Shared checkout, Main и Gateway source-интеграцией не менялись.

## Ограничения, которые остаются

[Матрица Card D](2026-10-08-a53-card-d-verification.md) описывает synthetic
profile. Python traps — bounded defense, не OS/native sandbox. Crypto provider
запрещён даже при policy ON: plaintext fixture не доказывает encrypted-history E2E.
Настоящие SavePanel/PDF/MeetingReport/QuickCapture UI handlers этим harness
не исполнены. Same-UID capability не является peer-auth.

Timeline UI при ON не передаёт session context; backend отказывает даже после
consent в другом окне. Это сохранённый fail-closed UX/release LIMIT,
не разрешение обходить authorizer и не доказательство полной ON-приёмки.

В этой source-интеграции не применён production package, не было новых
build-install/restart, чтения реальной истории/Keychain, активации encryption,
удаления копий или ротации. Текущие runtime PID/loaded SHA/флаг не проверялись.
Ранее владелец отдельно подтвердил автоматическую вставку диктовки в Codex
после разрешённого Swift signing repair; это отдельная live-приёмка подписи.

## Дальше

[План A5.4](../plans/2026-10-08-a54-release-acceptance.md) разделяет будущий
release, живые UI-проверки, encrypted acceptance и активацию.
[Safe cutover runbook](../plans/2026-09-29-safe-release-cutover.md) остаётся
существующим lifecycle-механизмом; исторические SHA/PID из него не применять
как текущую конфигурацию. Source-блок: 7/7, завершён. Live A5.4: не выполнен.
