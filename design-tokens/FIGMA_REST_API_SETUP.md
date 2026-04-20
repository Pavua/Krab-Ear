# Figma REST API — настройка для Krab Ear

## 1. Генерация Personal Access Token

1. Зайди на [figma.com](https://www.figma.com) → аватар (правый верхний угол) → **Settings**
2. Вкладка **Security** → раздел **Personal access tokens**
3. Нажми **Generate new token**, дай имя `krab-ear-design`
4. Скопируй токен сразу — он показывается один раз
5. Для работы с Variables API нужен **Dev Mode** токен (выбери scope `file_content:read` + `variables:read` + `variables:write`)

## 2. Сохранить токен локально

Добавь в `.env` в корне проекта (или `~/Antigravity_AGENTS/Krab Ear/.env`):

```
FIGMA_PAT=figd_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Загрузи в сессию:
```bash
export FIGMA_PAT=$(grep FIGMA_PAT .env | cut -d= -f2)
```

## 3. Что можно делать через REST без rate limit

| Операция | Rate limit |
|---|---|
| GET /files/:key | 60 req/min |
| POST /files/:key/variables | без жёсткого лимита (chunked OK) |
| GET /files/:key/variables/local | 60 req/min |

Variables write (`POST /variables`) работает в батчах до 500 переменных за запрос — это обход MCP rate limit.

## 4. Ключевые endpoints

| Метод | URL | Назначение |
|---|---|---|
| GET | `/v1/files/:key` | Получить структуру файла |
| GET | `/v1/files/:key/variables/local` | Получить все переменные файла |
| POST | `/v1/files/:key/variables` | Создать/обновить переменные (batch) |
| GET | `/v1/files/:key/nodes?ids=...` | Конкретные узлы |
| GET | `/v1/teams/:id/components` | Компоненты команды |

Документация: https://www.figma.com/developers/api#variables

File key для Krab Ear: `IPngmhIJEH93vCoeliJkuV`

## 5. Пример: создать Variable через curl

```bash
FILE_KEY="IPngmhIJEH93vCoeliJkuV"

curl -s -X POST \
  "https://api.figma.com/v1/files/${FILE_KEY}/variables" \
  -H "X-Figma-Token: ${FIGMA_PAT}" \
  -H "Content-Type: application/json" \
  -d '{
    "variableCollections": [],
    "variableModes": [],
    "variables": [
      {
        "action": "CREATE",
        "id": "temp:1",
        "name": "card/shadowBlur",
        "variableCollectionId": "<collection_id>",
        "resolvedType": "FLOAT"
      }
    ],
    "variableModeValues": [
      {
        "variableId": "temp:1",
        "modeId": "<mode_id>",
        "value": 6
      }
    ]
  }'
```

Получить `collection_id` и `mode_id`:
```bash
curl -s "https://api.figma.com/v1/files/${FILE_KEY}/variables/local" \
  -H "X-Figma-Token: ${FIGMA_PAT}" | python3 -m json.tool | grep -E '"id"|"name"' | head -20
```

## 6. Security

> **Токен даёт полный доступ ко всем файлам аккаунта.** Никогда не коммить `.env` в git.

`.gitignore` уже должен содержать `.env` — проверь:
```bash
grep '.env' .gitignore || echo '.env' >> .gitignore
```
