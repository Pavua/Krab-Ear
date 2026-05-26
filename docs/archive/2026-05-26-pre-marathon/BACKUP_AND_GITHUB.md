<!--
Инструкция по резервным копиям и подключению GitHub для Krab Ear.
-->

# Backup & GitHub

## 1. Локальный стабильный бэкап (one-click)

Запуск:
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Create Stable Backup.command`

Что создаётся:
- `/_STABLE_BACKUPS/krabear_stable_YYYYMMDD_HHMMSS.tar.gz`
- `/_STABLE_BACKUPS/krabear_stable_YYYYMMDD_HHMMSS.sha256`
- `/_STABLE_BACKUPS/krabear_stable_YYYYMMDD_HHMMSS.metadata.txt`

`Оптимальная практика`: делать такой снимок перед любым крупным рефактором.

## 2. Проверка целостности архива

Пример:

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/_STABLE_BACKUPS"
shasum -a 256 -c krabear_stable_YYYYMMDD_HHMMSS.sha256
```

## 3. Подключение GitHub (когда будешь готов)

Базовый сценарий:

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git add .
git commit -m "stable: krab ear native baseline"
git branch -M main
git remote add origin <YOUR_GITHUB_REPO_URL>
git push -u origin main
```

Если remote уже добавлен:

```bash
git remote set-url origin <YOUR_GITHUB_REPO_URL>
git push -u origin main
```
